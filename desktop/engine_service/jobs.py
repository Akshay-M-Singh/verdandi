"""Job queue for the desktop engine service (D66, Phase 0).

One background worker thread runs jobs sequentially (the engine itself can
use per-file parallelism inside a job via `workers`). The runner is injected
so tests never touch the network or the paid pipeline.

Job lifecycle: queued -> running -> done | failed. Progress is derived from
the run directory on disk (capture writes raw/<stem>/report.json, extraction
writes json/<stem>.json) — the same observable contract the CLI produced, so
the UI can poll safely and a crash never corrupts state.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import workspace as ws

logger = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    state: str                     # queued | running | done | failed
    created_at: str
    paths: list[str]               # original user-selected paths
    stems: list[str]               # staged input stems (after copy)
    run_dir: str
    doc_id: str
    model: str
    workers: int
    mode: str = "full"             # full = capture + extract | capture = capture only
                                   # | extract = extract an existing capture
    name: str = ""                 # user-editable run label ("" = unnamed)
    verification_model: str = ""   # backup/recheck model: "" = same as primary,
                                   # "off" = no rechecks, else a model id
    doc_ids: list[str] = field(default_factory=list)   # multi-profile runs
    workflow: str | None = None                        # bulk workflow runs
    extract_stems: list[str] = field(default_factory=list)  # per-file extract scope
    pinned_pages: str | None = None   # D26 pin, e.g. "1-3,5" (extract stage)
    pinned_label: str | None = None
    renders_pruned: bool = False       # page renders deleted to free disk (D66)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    files: list[dict] = field(default_factory=list)   # per-stem progress rows

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, run_dir: Path | None = None) -> "Job":
        """Rebuild a job from its persisted job.json (restart / worker mode)."""
        return cls(
            id=data.get("id", ""),
            state=data.get("state", "failed"),
            created_at=data.get("created_at", ""),
            paths=data.get("paths") or [],
            stems=data.get("stems") or [],
            run_dir=str(run_dir or data.get("run_dir", "")),
            doc_id=data.get("doc_id", ""),
            model=data.get("model", ""),
            workers=data.get("workers", 1),
            mode=data.get("mode", "full"),
            name=data.get("name") or "",
            verification_model=data.get("verification_model") or "",
            doc_ids=data.get("doc_ids") or [],
            workflow=data.get("workflow"),
            extract_stems=data.get("extract_stems") or [],
            pinned_pages=data.get("pinned_pages"),
            pinned_label=data.get("pinned_label"),
            renders_pruned=bool(data.get("renders_pruned")),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
            files=data.get("files") or [],
        )


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class JobManager:
    """Sequential background job runner.

    Two execution modes:
    - SUBPROCESS (production, default): each job runs in its own worker
      process (`sidecar --run-job`), so Cancel is a clean kill and a worker
      crash can never take the service down. Progress stays disk-derived.
    - IN-PROCESS (tests / embedding): pass `runner` and it is called directly.
    `worker_cmd` overrides the spawned command (tests use a stub worker).
    """

    def __init__(self, runner: Callable[[Job], None] | None = None,
                 root: Path | None = None,
                 worker_cmd: list[str] | None = None):
        self._runner = runner
        self._subprocess = runner is None
        self._worker_cmd = worker_cmd
        self._root = root or ws.workspace_root()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._procs: dict[str, subprocess.Popen] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, name="verdandi-jobs", daemon=True)
        self._stopped = threading.Event()
        self._load_from_disk()
        self._worker.start()

    # -- public API ---------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def submit(self, paths: list[str], *, doc_id: str, model: str, workers: int = 2,
               mode: str = "full", pinned_pages: str | None = None,
               pinned_label: str | None = None,
               doc_ids: list[str] | None = None,
               workflow: str | None = None,
               verification_model: str = "",
               name: str = "") -> Job:
        files = ws.expand_pdf_paths(paths)
        run_dir = ws.allocate_run(self._root)
        doc_ids = [d for d in (doc_ids or []) if d] or ([doc_id] if doc_id else [])
        job = Job(
            id=uuid.uuid4().hex[:12],
            state="queued",
            created_at=_now(),
            paths=[str(p) for p in paths],
            stems=[f.stem for f in files],
            run_dir=str(run_dir),
            doc_id=doc_id or (doc_ids[0] if doc_ids else ""),
            model=model,
            workers=max(1, min(int(workers), 8)),
            mode="capture" if mode == "capture" else "full",
            name=(name or "").strip()[:80],
            verification_model=verification_model or "",
            doc_ids=doc_ids,
            workflow=workflow or None,
            pinned_pages=pinned_pages or None,
            pinned_label=pinned_label or None,
        )
        # keep the resolved file list for staging; not part of the JSON contract
        job._files = files  # type: ignore[attr-defined]
        job._needs_stage = True  # type: ignore[attr-defined]
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._write(job)
        self._queue.put(job.id)
        return job

    def extract(self, job_id: str, *, pinned_pages: str | None = None,
                pinned_label: str | None = None,
                stems: list[str] | None = None) -> Job:
        """Run (or re-run) extraction on an existing capture, with optional pins.

        Reuses the SAME run: capture output is already there, so this just
        re-queues the job in `extract` mode. Pins can change between attempts.
        `stems` scopes the extraction to specific files (the Streamlit
        per-file pin flow); None = every staged file.
        """
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        if job.state in ("queued", "running"):
            raise ValueError("the run is still busy")
        run = Path(job.run_dir)
        raw = run / "raw"
        if not raw.is_dir() or not any(raw.iterdir()):
            raise ValueError("no capture output to extract from — run capture first")
        if pinned_pages:
            ws.parse_pages(pinned_pages)  # validate early, clear error
        scope = [s for s in (stems or []) if s in job.stems]
        if stems and not scope:
            raise ValueError("none of the requested files are in this run")
        job.mode = "extract"
        job.extract_stems = scope
        job.pinned_pages = (pinned_pages or "").strip() or None
        job.pinned_label = (pinned_label or "").strip() or None
        job.state = "queued"
        job.error = None
        job.started_at = None
        job.finished_at = None
        job.files = []
        self._write(job)
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order)]

    def rename(self, job_id: str, name: str) -> Job:
        """Set/clear a run's display name (trimmed, 80 chars, persisted)."""
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        job.name = (name or "").strip()[:80]
        self._write(job)
        return job

    def retry(self, job_id: str) -> Job:
        """Re-queue a job's unfinished files as a NEW job (same options).

        A file counts as finished when its extraction JSON exists (full mode)
        or its capture report exists (capture mode). Inputs are taken from the
        original run's staged inbox, so nothing outside the workspace is
        touched.
        """
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        run = Path(job.run_dir)
        todo: list[str] = []
        for stem in (job.extract_stems or job.stems):
            if job.mode == "capture":
                done = (run / "raw" / stem / "report.json").exists()
            else:
                done = (run / "json" / f"{stem}.json").exists()
                if done:
                    try:
                        data = json.loads((run / "json" / f"{stem}.json").read_text(
                            encoding="utf-8"))
                        done = data.get("status") != "failed"
                    except (OSError, json.JSONDecodeError):
                        done = False
            if not done:
                pdf = run / "inbox" / f"{stem}.pdf"
                if pdf.exists():
                    todo.append(str(pdf))
        if not todo:
            raise ValueError("nothing to retry — every file finished")
        # An extract-stage retry becomes a fresh full run for the unfinished
        # files only (capture is free) — pins are carried over.
        mode = "full" if job.mode == "extract" else job.mode
        return self.submit(todo, doc_id=job.doc_id, model=job.model,
                           workers=job.workers, mode=mode,
                           pinned_pages=job.pinned_pages,
                           pinned_label=job.pinned_label,
                           doc_ids=job.doc_ids or None,
                           workflow=job.workflow,
                           verification_model=job.verification_model,
                           name=job.name)

    # -- restart persistence -------------------------------------------------

    def _load_from_disk(self) -> None:
        """Register past jobs from <root>/runs/*/*/job.json (newest last)."""
        runs = sorted(self._root.glob("runs/*/*"))
        for run in runs:
            meta = run / "job.json"
            if not meta.exists():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not data.get("id") or data["id"] in self._jobs:
                continue
            job = Job.from_dict(data, run_dir=run)
            if job.state in ("queued", "running"):
                # the engine restarted mid-job — its outputs may be partial,
                # but the artifacts on disk are intact; surface it honestly.
                job.state = "failed"
                job.error = "interrupted by an engine restart"
                job.finished_at = job.finished_at or _now()
                self._write(job)
            self._jobs[job.id] = job
            self._order.append(job.id)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopped.set()
        self._queue.put("")  # wake the worker
        self._worker.join(timeout=timeout)

    def wait(self, job_id: str, timeout: float = 30.0) -> Job:
        """Test/dev helper: block until the job leaves queued/running."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job and job.state in ("done", "failed"):
                return job
            time.sleep(0.02)
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    # -- internals ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stopped.is_set():
            job_id = self._queue.get()
            if not job_id or self._stopped.is_set():
                continue
            job = self.get(job_id)
            if job is None or job.state != "queued":
                continue  # cancelled while queued
            job.state = "running"
            job.started_at = _now()
            self._write(job)
            outcome: tuple[str, str | None] | None = None
            try:
                if getattr(job, "_needs_stage", False):
                    files: list[Path] = getattr(job, "_files", [])
                    job.stems = ws.stage_inputs(files, Path(job.run_dir) / "inbox")
                    job._needs_stage = False  # type: ignore[attr-defined]
                    job.files = self._progress(job)
                    self._write(job)
                if job.state != "running":
                    continue  # cancelled during staging
                if self._subprocess:
                    outcome = self._run_worker(job)
                else:
                    self._runner(job)  # type: ignore[misc]
                    outcome = ("done", None)
            except Exception as exc:  # noqa: BLE001 — surface, never crash the worker
                logger.exception("job %s failed", job.id)
                outcome = ("failed", f"{type(exc).__name__}: {exc}")
            # Publish the terminal state and the refreshed per-file rows in ONE
            # critical section: a poller that sees done/failed must never read
            # stale rows (wait() in tests and the UI's 1 s poll both rely on
            # this). The state is set first because _progress labels a file
            # with no capture output as failed only once the job is terminal.
            with self._lock:
                if outcome is not None and job.state == "running":
                    job.state, job.error = outcome
                job.finished_at = job.finished_at or _now()
                job.files = self._progress(job)
                self._write(job)

    # -- worker processes ----------------------------------------------------

    def _worker_command(self, job: Job) -> list[str]:
        if self._worker_cmd is not None:
            return list(self._worker_cmd(job) if callable(self._worker_cmd)
                        else self._worker_cmd)
        spec = str(Path(job.run_dir) / "job.json")
        if getattr(sys, "frozen", False):
            return [sys.executable, "--run-job", spec]
        return [sys.executable, "-m", "desktop.sidecar", "--run-job", spec]

    def _run_worker(self, job: Job) -> tuple[str, str | None] | None:
        """Run the job in its own process; returns the outcome (None when the
        operator cancelled — cancel() already set the state)."""
        run = Path(job.run_dir)
        logs = run / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "VERDANDI_WATCH_PARENT": "1"}
        with (logs / "worker.log").open("wb") as log:
            proc = subprocess.Popen(
                self._worker_command(job),
                cwd=str(Path(__file__).resolve().parents[2]),
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env=env)
            self._procs[job.id] = proc
            code = proc.wait()
        self._procs.pop(job.id, None)
        if job.state == "cancelled":
            return None
        if code == 0:
            return ("done", None)
        return ("failed", f"worker exited with code {code} (see logs/worker.log)")

    def cancel(self, job_id: str) -> Job:
        """Stop a queued/running job (kills its worker process group)."""
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        if job.state not in ("queued", "running"):
            raise ValueError(f"job is {job.state} — nothing to cancel")
        job.state = "cancelled"
        job.error = "cancelled by the operator"
        job.finished_at = _now()
        proc = self._procs.get(job.id)
        if proc is not None and proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:  # pragma: no cover — Windows
                    proc.terminate()
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:  # pragma: no cover
                        proc.kill()
                except ProcessLookupError:
                    pass
        job.files = self._progress(job)
        self._write(job)
        return job

    # -- disk lifecycle ------------------------------------------------------

    def storage(self, job_id: str) -> dict:
        """Size breakdown of a run: page renders vs everything else."""
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        return self._storage(job)

    def prune_renders(self, job_id: str) -> dict:
        """Delete the page-render PNGs of a finished run (keeps JSONs/text)."""
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        if job.state in ("queued", "running"):
            raise ValueError("the run is still busy — cancel it first")
        freed = files = 0
        for path in Path(job.run_dir).rglob("page*.png"):
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
                files += 1
        if files:
            job.renders_pruned = True
            self._write(job)
        return {"files": files, "freed_bytes": freed}

    def delete(self, job_id: str) -> dict:
        """Remove a finished run's directory and its history entry."""
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"unknown job {job_id}")
        if job.state in ("queued", "running"):
            raise ValueError("the run is still busy — cancel it first")
        run = Path(job.run_dir)
        # Windows: a just-terminated worker's log handle can lag a moment
        # behind cancel() — retry briefly before giving up.
        last_exc: OSError | None = None
        for _ in range(10):
            try:
                shutil.rmtree(run)
                last_exc = None
                break
            except FileNotFoundError:
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.2)
        if last_exc is not None:
            raise ValueError(f"could not delete {run}: {last_exc}")
        with self._lock:
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        return {"deleted": job_id}

    def _storage(self, job: Job) -> dict:
        run = Path(job.run_dir)
        renders_bytes = renders_files = 0
        raw = run / "raw"
        if raw.is_dir():
            for path in raw.rglob("page*.png"):
                if path.is_file():
                    renders_bytes += path.stat().st_size
                    renders_files += 1
        total = 0
        for path in run.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return {"renders_bytes": renders_bytes, "renders_files": renders_files,
                "total_bytes": total, "renders_pruned": job.renders_pruned}

    def _write(self, job: Job) -> None:
        """Persist job.json ATOMICALLY.

        The worker thread writes progress while API calls (rename) and test
        readers may read the same file — a plain write_text let a reader see
        a torn document (json "Extra data" on the CI release runner,
        2026-10-06). Write a sibling temp file, then os.replace.

        The temp name is UNIQUE per call (2026-10-09, Windows CI): two
        concurrent writers sharing `job.json.tmp` race — one replace wins and
        the other fails with OSError on Windows, silently dropping that write
        (the rename test then read the stale file)."""
        path = Path(job.run_dir) / "job.json"
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(job.to_dict(), indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("could not persist job.json for %s", job.id)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def progress(self, job: Job) -> list[dict]:
        """Fresh per-file progress rows for a job (empty for queued jobs)."""
        rows = self._progress(job)
        job.files = rows
        return rows

    def _progress(self, job: Job) -> list[dict]:
        run = Path(job.run_dir)
        rows: list[dict] = []
        for stem in job.stems:
            captured = (run / "raw" / stem / "report.json").exists()
            out = run / "json" / f"{stem}.json"
            status = "pending"
            found = None
            error = None
            if out.exists():
                status = "extracted"
                try:
                    data = json.loads(out.read_text(encoding="utf-8"))
                    if data.get("status") == "failed":
                        status = "failed"
                        notes = data.get("notes") or []
                        error = (notes[0] if notes else None) or data.get("error")
                    else:
                        found = bool(data.get("status") != "flag_no_document_match")
                except (OSError, json.JSONDecodeError):
                    status = "error"
                    error = "unreadable extraction JSON"
            elif captured:
                status = "captured"
            elif job.state in ("done", "failed"):
                # terminal job + no capture output = this file's capture failed
                status = "failed"
                error = "capture produced no output — see the worker log"
            rows.append({"stem": stem, "status": status, "found": found,
                         "error": error})
        return rows
