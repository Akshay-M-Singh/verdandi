"""FastAPI app for the desktop engine service (D66).

Binds 127.0.0.1 only — the app window talks to it locally; nothing is
exposed to the network. The routes wrap the job queue + run artifacts; the
heavy lifting stays in the core pipeline (runner.py). Review write-through
and evidence reuse the Streamlit UI's pure helpers (ui.lib, reviewing.py).

    GET  /health
    GET  /settings                       defaults + key status
    POST /settings                       save defaults / store the API key
    GET  /defaults                       profile + model pickers (config)
    POST /jobs                           {paths, doc_id?, model?, workers?, mode?}
    GET  /jobs                           newest first (disk-backed history)
    GET  /jobs/{id}                      state + per-file progress
    POST /jobs/{id}/retry                re-queue unfinished files as a new job
    GET  /jobs/{id}/documents            per-document summaries
    GET  /jobs/{id}/documents/{stem}     full extraction JSON
    GET  /jobs/{id}/documents/{stem}/page/{page}     page render PNG
    GET  /jobs/{id}/documents/{stem}/crop            crop PNG (?page=&bbox=)
    GET  /jobs/{id}/flags                flags + review state + evidence
    POST /jobs/{id}/review               confirm/issue a flag (write-through)
    GET  /exports                        export profiles (rules/exports.yaml)
    POST /jobs/{id}/export               build a workbook -> {file}
    GET  /jobs/{id}/export/{file}        download a built workbook
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import platform
import sys
import zipfile

import messages
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import __version__
from . import (
    cataloging,
    exporting,
    modelsapi,
    reviewing,
    settings,
    templates,
    userconfig,
    workflows,
)
from .jobs import Job, JobManager
from .runner import engine_defaults
from .workspace import expand_pdf_paths, workspace_root

# Tauri v2 serves the UI from tauri://localhost (macOS) / http://tauri.localhost
# (Windows); the Vite dev server runs on localhost:5173.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
]


class JobRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    doc_id: str | None = None
    doc_ids: list[str] | None = None
    workflow: str | None = None
    model: str | None = None
    workers: int | None = None
    mode: str = "full"  # full | capture
    verification_model: str | None = None
    name: str | None = None


class SettingsRequest(BaseModel):
    api_key: str | None = None
    remove_key: bool | None = None
    profile: str | None = None
    model: str | None = None
    workers: int | None = None
    onboarded: bool | None = None
    auto_prune_renders: bool | None = None
    verification_model: str | None = None


class ReviewRequest(BaseModel):
    row: dict
    status: str
    note: str = ""
    value: str | None = None


class ExportRequest(BaseModel):
    profile: str
    template: str | None = None


class ModelRequest(BaseModel):
    model: str


class RenameRequest(BaseModel):
    name: str = ""


class ExtractRequest(BaseModel):
    pinned_pages: str | None = None
    pinned_label: str | None = None
    stems: list[str] | None = None


class CatalogJobRequest(BaseModel):
    stem: str | None = None


class IdsRequest(BaseModel):
    ids: list[str]


class PathsRequest(BaseModel):
    paths: list[str] = Field(min_length=1)


class TemplateRequest(BaseModel):
    profile: dict
    original_id: str | None = None


class WorkflowRequest(BaseModel):
    workflow: dict
    original_id: str | None = None


class ExportProfileRequest(BaseModel):
    profile: dict
    original_id: str | None = None


class CatalogRequest(BaseModel):
    profile: dict | None = None


class SeedRequest(BaseModel):
    workflow_id: str


class PreviewRequest(BaseModel):
    profile: dict
    run_id: str | None = None


class TextRequest(BaseModel):
    text: str


class IdRequest(BaseModel):
    id: str


_watch_started = False


def watch_parent() -> None:
    """Exit when the process that started us dies (idempotent, all platforms).

    The Tauri shell kills the engine on a clean quit; this covers the crash
    case (killed window / Task Manager) so the engine never orphans itself on
    port 8787 — and workers (spawned by the engine) never keep making paid
    calls after the app is gone. Enabled by the sidecar entry
    (VERDANDI_WATCH_PARENT=1); tests and library use never start it.

    Unix: the process re-parents on parent death, so poll getppid().
    Windows: PIDs never change on parent death, so hold a SYNCHRONIZE handle
    to the parent process object (opened once — immune to PID reuse) and
    wait on it.
    """
    global _watch_started
    if _watch_started:
        return
    _watch_started = True
    import threading
    import time

    parent = os.getppid()

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent)
        if not handle:
            os._exit(0)  # the parent is already gone — never start orphaned

        def _loop_win() -> None:
            while True:
                # WAIT_OBJECT_0 (0) = the parent process has exited.
                if kernel32.WaitForSingleObject(handle, 2000) == 0:
                    os._exit(0)

        target = _loop_win
    else:
        def _loop_unix() -> None:
            while True:
                time.sleep(2)
                if os.getppid() != parent:
                    os._exit(0)

        target = _loop_unix

    threading.Thread(target=target, daemon=True, name="parent-watch").start()


def _settings_view() -> dict:
    base = engine_defaults()
    saved = settings.load_settings()
    return {
        "profiles": base["profiles"],
        "profile": saved.get("profile") or base["default_profile"],
        "model": saved.get("model") or base["primary_model"],
        "primary_model": base["primary_model"],
        "verification_model": saved.get("verification_model") or "",
        "workers": saved.get("workers") or 2,
        "key_present": settings.key_present(),
        "keychain_backend": settings.keychain_backend(),
        "workspace": str(workspace_root()),
        "onboarded": bool(saved.get("onboarded")),
        "auto_prune_renders": saved.get("auto_prune_renders", True),
    }


def _fallback_model(view: dict) -> dict | None:
    """When the effective model left GO: switch to the first still-offered
    choice (config primary, then the verified list). Never fires offline or
    without a known catalog; the config YAML is never edited (2026-10-05)."""
    ids = set(view.get("catalog_ids") or [])
    current = view.get("current") or ""
    if not ids or not current or current in ids:
        return None
    for choice in view.get("choices") or []:
        candidate = choice.get("id")
        if candidate and candidate != current and candidate in ids:
            return {"from": current, "to": candidate}
    return None


def _documents(job: Job) -> list[dict]:
    run = Path(job.run_dir)
    rows: list[dict] = []
    for stem in job.stems:
        out = run / "json" / f"{stem}.json"
        row = {"stem": stem, "status": "pending", "found": None,
               "selected_name": None, "fields": 0, "line_items": 0,
               "pages": 0}
        row["pages"] = _page_count(run, stem)
        if out.exists():
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                row["status"] = "error"
                rows.append(row)
                continue
            status = data.get("status") or "extracted"
            row["status"] = "failed" if status == "failed" else "extracted"
            entries = data.get("documents") or []
            if entries:  # multi-document / workflow run (D27, 2026-09-05)
                row["found"] = any(e.get("found") for e in entries)
                row["selected_name"] = next(
                    ((e.get("selected_document") or {}).get("name")
                     for e in entries if e.get("found")), None)
                row["fields"] = sum(len(e.get("header_fields") or [])
                                    for e in entries)
                row["line_items"] = sum(len(e.get("line_items") or [])
                                        for e in entries)
            else:
                row["found"] = status != "flag_no_document_match"
                sel = data.get("selected_document") or {}
                row["selected_name"] = sel.get("name")
                row["fields"] = len(data.get("header_fields") or [])
                row["line_items"] = len(data.get("line_items") or [])
        elif (run / "raw" / stem / "report.json").exists():
            row["status"] = "captured"
        rows.append(row)
    return rows


def _run_metrics(run: Path) -> dict:
    """Flag/review counters + token usage for one run (local, read-only)."""
    try:
        counts = reviewing.flag_counts(run)
    except Exception:  # noqa: BLE001 — flags are optional
        counts = {"flags": 0, "pending": 0, "confirmed": 0, "issues": 0}
    usage = {"prompt_tokens": 0, "completion_tokens": 0,
             "recheck_prompt_tokens": 0, "recheck_completion_tokens": 0,
             "rechecks": 0}
    per_file: dict[str, dict] = {}
    for jp in sorted((run / "json").glob("*.json")):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = data.get("_meta") or {}
        call = meta.get("usage") or {}
        recheck = (meta.get("verification") or {}).get("recheck") or {}
        rusage = recheck.get("usage") or {}
        per_file[jp.stem] = {
            "prompt_tokens": call.get("prompt_tokens") or 0,
            "completion_tokens": call.get("completion_tokens") or 0,
            "recheck_prompt_tokens": rusage.get("prompt_tokens") or 0,
            "recheck_completion_tokens": rusage.get("completion_tokens") or 0,
            "rechecks": 1 if rusage else 0,
        }
        usage["prompt_tokens"] += call.get("prompt_tokens") or 0
        usage["completion_tokens"] += call.get("completion_tokens") or 0
        if rusage:
            usage["rechecks"] += 1
        usage["recheck_prompt_tokens"] += rusage.get("prompt_tokens") or 0
        usage["recheck_completion_tokens"] += rusage.get("completion_tokens") or 0
    out = {**counts, **usage, "per_file": per_file}
    out["review_summary"] = messages.reason_summary(counts.get("reasons") or {})
    return out


def _page_count(run: Path, stem: str) -> int:
    report = run / "raw" / stem / "report.json"
    try:
        return int(json.loads(report.read_text(encoding="utf-8")).get("pages") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _safe_export_path(run_dir: Path, name: str) -> Path:
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="bad export file name")
    path = run_dir / "export" / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no export {name}")
    return path


def create_app(manager: JobManager | None = None) -> FastAPI:
    if os.environ.get("VERDANDI_WATCH_PARENT") == "1":
        watch_parent()
    config_info = userconfig.bootstrap()
    settings.apply_key()
    # Production runs every job in its own worker process (cancellable,
    # crash-isolated); tests inject a manager with an in-process runner.
    manager = manager or JobManager(root=workspace_root())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(title="Verdandi", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.manager = manager
    app.state.config_info = config_info
    exporting.bind_manager(manager)  # preview needs to find runs

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "app": "verdandi-desktop", "version": __version__}

    @app.get("/defaults")
    def defaults() -> dict:
        try:
            return engine_defaults()
        except Exception as exc:  # noqa: BLE001 — surface config errors to the UI
            raise HTTPException(status_code=500, detail=f"config not loaded: {exc}")

    @app.get("/settings")
    def get_settings() -> dict:
        try:
            return _settings_view()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"config not loaded: {exc}")

    @app.post("/settings")
    def post_settings(body: SettingsRequest) -> dict:
        stored: bool | None = None
        if body.api_key:
            stored = settings.set_key(body.api_key)
        if body.remove_key:
            settings.remove_key()
        if body.profile or body.model or body.workers:
            settings.save_settings({"profile": body.profile, "model": body.model,
                                    "workers": body.workers})
        if body.verification_model is not None:
            settings.save_settings(
                {"verification_model": body.verification_model})
        if body.onboarded is not None or body.auto_prune_renders is not None:
            settings.save_settings({"onboarded": body.onboarded,
                                    "auto_prune_renders": body.auto_prune_renders})
        view = _settings_view()
        if stored is not None:
            view["key_stored"] = stored  # keychain persisted? (honest toast)
        return view

    @app.get("/storage")
    def workspace_storage() -> dict:
        """Workspace usage summary (runs + total bytes) for Settings."""
        root = manager.root
        runs = 0
        total = 0
        for run in (root / "runs").glob("*/*"):
            if not run.is_dir():
                continue
            runs += 1
            for path in run.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        return {"runs": runs, "total_bytes": total, "workspace": str(root)}

    @app.post("/settings/reset")
    def reset_defaults() -> dict:
        files = userconfig.reset_defaults()
        return {"reset": files, "settings": _settings_view()}

    @app.get("/diagnostics")
    def diagnostics() -> Response:
        """Support bundle: versions, sanitized settings, recent worker logs.

        NEVER document contents — logs + metadata only (the key is not even in
        settings.json; it lives in the OS keychain).
        """
        buf = io.BytesIO()
        view = _settings_view()
        jobs = [j.to_dict() for j in manager.list()][:50]
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("info.json", json.dumps({
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "app_version": __version__,
                "python": sys.version,
                "platform": platform.platform(),
                "settings": {k: v for k, v in view.items()},
                "recent_jobs": jobs,
            }, indent=2))
            for entry in jobs[:20]:
                log = Path(entry.get("run_dir", "")) / "logs" / "worker.log"
                if not log.exists():
                    continue
                try:
                    text = log.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                bundle.writestr(f"logs/{entry.get('id', 'run')}.log",
                                text[-20000:])
            # Service logs: the shell watchdog + the engine's own log (the
            # console is absent in packaged apps — these files are the only
            # record of startup/crash failures).
            for name in ("shell.log", "engine.log"):
                path = workspace_root() / "logs" / name
                if not path.exists():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                bundle.writestr(f"logs/{name}", text[-50000:])
        return Response(
            content=buf.getvalue(), media_type="application/zip",
            headers={"Content-Disposition":
                     'attachment; filename="verdandi-diagnostics.zip"'})

    @app.get("/workflows")
    def workflows_list() -> dict:
        try:
            return workflows.list_workflows()
        except Exception as exc:  # noqa: BLE001 — config issues
            raise HTTPException(status_code=500, detail=f"workflows failed: {exc}")

    @app.post("/workflows")
    def workflows_save(body: WorkflowRequest) -> dict:
        try:
            return workflows.save_workflow(body.workflow, body.original_id)
        except Exception as exc:  # noqa: BLE001 — validation + IO
            raise HTTPException(status_code=400, detail=f"save failed: {exc}")

    @app.delete("/workflows/{workflow_id}")
    def workflows_delete(workflow_id: str) -> dict:
        try:
            return workflows.delete_workflow(workflow_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/workflows/duplicate")
    def workflows_duplicate(body: IdRequest) -> dict:
        try:
            return workflows.duplicate_workflow(body.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/workflows/yaml")
    def workflows_yaml() -> dict:
        try:
            return {"text": workflows.get_yaml()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"yaml failed: {exc}")

    @app.post("/workflows/validate")
    def workflows_validate(body: TextRequest) -> dict:
        return workflows.validate_yaml(body.text)

    @app.post("/workflows/yaml")
    def workflows_save_yaml(body: TextRequest) -> dict:
        try:
            return workflows.save_yaml(body.text)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"save failed: {exc}")

    @app.get("/models")
    def models(force: bool = False) -> dict:
        try:
            current = _settings_view()["model"]
            data = modelsapi.snapshot(current, force=force)
            fallback = _fallback_model(data)
            if fallback:
                # Auto-fallback (2026-10-05, owner): persist the switch, then
                # re-snapshot from cache so the payload reflects the new model.
                settings.save_settings({"model": fallback["to"]})
                data = modelsapi.snapshot(fallback["to"], force=False)
                data["fallback"] = fallback
            return {**data, "config_root": app.state.config_info["root"]}
        except Exception as exc:  # noqa: BLE001 — catalog/config issues
            raise HTTPException(status_code=500, detail=f"model catalog failed: {exc}")

    @app.get("/models/rates")
    def model_rates() -> dict:
        try:
            return modelsapi.rates()
        except Exception as exc:  # noqa: BLE001 — catalog/config issues
            raise HTTPException(status_code=500,
                                detail=f"rates unavailable: {exc}")

    @app.post("/models/verify")
    def verify_model(body: ModelRequest) -> dict:
        try:
            return {"message": modelsapi.verify(body.model)}
        except Exception as exc:  # noqa: BLE001 — paid probe + config write
            raise HTTPException(status_code=400, detail=f"verify failed: {exc}")

    @app.post("/models/default")
    def set_default_model(body: ModelRequest) -> dict:
        settings.save_settings({"model": body.model})
        return _settings_view()

    @app.post("/jobs", status_code=202)
    def submit(body: JobRequest) -> dict:
        view = _settings_view()
        doc_ids = [d for d in (body.doc_ids or []) if d]
        if body.workflow and (body.doc_id or doc_ids):
            raise HTTPException(status_code=400,
                                detail="choose either a workflow or profile(s)")
        if doc_ids and body.doc_id:
            raise HTTPException(status_code=400,
                                detail="choose either one profile or a profile set")
        doc_id = body.doc_id or (doc_ids[0] if doc_ids else view["profile"])
        model = body.model or view["model"]
        workers = body.workers or view["workers"]
        vmodel = (body.verification_model if body.verification_model is not None
                  else view["verification_model"])
        if not doc_id or not model:
            raise HTTPException(status_code=500, detail="no profile/model configured")
        try:
            job = app.state.manager.submit(
                body.paths, doc_id=doc_id, model=model, workers=workers,
                mode=body.mode, doc_ids=doc_ids or None, workflow=body.workflow,
                verification_model=vmodel, name=body.name or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return job.to_dict()

    @app.get("/jobs")
    def list_jobs() -> list[dict]:
        return [j.to_dict() for j in app.state.manager.list()]

    def _job_or_404(job_id: str) -> Job:
        job = app.state.manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
        return job

    @app.post("/paths/summary")
    def paths_summary(body: PathsRequest) -> dict:
        """Pre-run intake summary: how many PDFs and how big (cost hint)."""
        try:
            files = expand_pdf_paths(body.paths)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        total = sum(f.stat().st_size for f in files)
        return {"files": len(files), "bytes": total,
                "calls_estimate": len(files)}

    @app.get("/templates")
    def templates_list() -> dict:
        try:
            return templates.list_templates()
        except Exception as exc:  # noqa: BLE001 — config issues
            raise HTTPException(status_code=500, detail=f"templates failed: {exc}")

    @app.post("/templates")
    def templates_save(body: TemplateRequest) -> dict:
        try:
            return templates.save_template(body.profile, body.original_id)
        except Exception as exc:  # noqa: BLE001 — validation + IO
            raise HTTPException(status_code=400, detail=f"save failed: {exc}")

    @app.delete("/templates/{doc_id}")
    def templates_delete(doc_id: str) -> dict:
        try:
            return templates.delete_template(doc_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/templates/duplicate")
    def templates_duplicate(body: IdRequest) -> dict:
        try:
            return templates.duplicate_template(body.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/templates/yaml")
    def templates_yaml() -> dict:
        try:
            return {"text": templates.get_yaml()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"yaml failed: {exc}")

    @app.post("/templates/validate")
    def templates_validate(body: TextRequest) -> dict:
        return templates.validate_yaml(body.text)

    @app.post("/templates/yaml")
    def templates_save_yaml(body: TextRequest) -> dict:
        try:
            return templates.save_yaml(body.text)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"save failed: {exc}")

    @app.get("/jobs/{job_id}")
    def job_detail(job_id: str) -> dict:
        job = _job_or_404(job_id)
        data = job.to_dict()
        data["files"] = app.state.manager.progress(job)
        if job.state == "queued":
            queued = [j for j in reversed(app.state.manager.list())
                      if j.state == "queued"]
            data["queue_position"] = next(
                (i + 1 for i, j in enumerate(queued) if j.id == job_id), None)
        return data

    @app.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str) -> dict:
        # Retry re-queues unfinished files as a NEW job.
        _job_or_404(job_id)
        try:
            job = app.state.manager.retry(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return job.to_dict()

    @app.post("/jobs/{job_id}/rename")
    def rename_job(job_id: str, body: RenameRequest) -> dict:
        _job_or_404(job_id)
        try:
            return app.state.manager.rename(job_id, body.name).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: str) -> dict:
        try:
            job = app.state.manager.cancel(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return job.to_dict()

    @app.get("/jobs/{job_id}/storage")
    def job_storage(job_id: str) -> dict:
        try:
            return app.state.manager.storage(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/jobs/{job_id}/prune")
    def prune(job_id: str) -> dict:
        try:
            return app.state.manager.prune_renders(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/jobs/{job_id}")
    def delete_job(job_id: str) -> dict:
        try:
            return app.state.manager.delete(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/jobs/{job_id}/logs")
    def job_logs(job_id: str) -> dict:
        job = _job_or_404(job_id)
        path = Path(job.run_dir) / "logs" / "worker.log"
        if not path.exists():
            return {"log": ""}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"unreadable log: {exc}")
        return {"log": text[-20000:]}

    @app.post("/jobs/{job_id}/extract", status_code=202)
    def extract(job_id: str, body: ExtractRequest) -> dict:
        try:
            job = app.state.manager.extract(
                job_id, pinned_pages=body.pinned_pages,
                pinned_label=body.pinned_label, stems=body.stems)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return job.to_dict()

    @app.post("/jobs/{job_id}/catalog")
    def run_catalog(job_id: str, body: CatalogJobRequest | None = None) -> dict:
        job = _job_or_404(job_id)
        files = {body.stem} if body and body.stem else None
        try:
            code = cataloging.run(Path(job.run_dir), model=job.model, files=files)
        except Exception as exc:  # noqa: BLE001 — paid sweep + engine errors
            raise HTTPException(status_code=400, detail=f"catalog failed: {exc}")
        return {"exit_code": code, "cards": cataloging.cards(Path(job.run_dir))}

    @app.get("/jobs/{job_id}/summary")
    def job_summary(job_id: str) -> list[dict]:
        """Per (file, template) summary rows (workflow runs write summary.csv)."""
        job = _job_or_404(job_id)
        path = Path(job.run_dir) / "summary.csv"
        if not path.exists():
            return []
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    @app.get("/jobs/{job_id}/metrics")
    def job_metrics(job_id: str) -> dict:
        """Flag/review counters + token usage (one-off; not polled)."""
        job = _job_or_404(job_id)
        return _run_metrics(Path(job.run_dir))

    @app.post("/jobs/metrics")
    def jobs_metrics(body: IdsRequest) -> dict:
        """Batch metrics for the Runs table (newest ids first)."""
        out: dict[str, dict] = {}
        for jid in body.ids[:60]:
            job = app.state.manager.get(jid)
            if job is not None:
                out[jid] = _run_metrics(Path(job.run_dir))
        return out

    @app.get("/jobs/{job_id}/catalog")
    def catalog_cards(job_id: str) -> list[dict]:
        job = _job_or_404(job_id)
        return cataloging.cards(Path(job.run_dir))

    @app.get("/jobs/{job_id}/documents")
    def job_documents(job_id: str) -> list[dict]:
        return _documents(_job_or_404(job_id))

    @app.get("/jobs/{job_id}/documents/{stem}")
    def job_document(job_id: str, stem: str) -> dict:
        job = _job_or_404(job_id)
        out = Path(job.run_dir) / "json" / f"{stem}.json"
        if not out.exists():
            raise HTTPException(status_code=404, detail=f"no output for {stem}")
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"unreadable output: {exc}")

    @app.get("/jobs/{job_id}/documents/{stem}/page/{page}")
    def page_image(job_id: str, stem: str, page: int) -> Response:
        job = _job_or_404(job_id)
        data = reviewing.crop_png(Path(job.run_dir), stem, page, None)
        if data is None:
            raise HTTPException(status_code=404, detail="no page render")
        return Response(content=data, media_type="image/png")

    @app.get("/jobs/{job_id}/documents/{stem}/crop")
    def crop_image(job_id: str, stem: str, page: int = 1, bbox: str | None = None) -> Response:
        job = _job_or_404(job_id)
        box = None
        if bbox:
            try:
                parts = [float(v) for v in bbox.split(",")]
                if len(parts) != 4:
                    raise ValueError
                box = tuple(parts)
            except ValueError:
                raise HTTPException(status_code=400, detail="bbox must be x0,y0,x1,y1")
        data = reviewing.crop_png(Path(job.run_dir), stem, page, box)
        if data is None:
            raise HTTPException(status_code=404, detail="no page render")
        return Response(content=data, media_type="image/png")

    @app.get("/jobs/{job_id}/flags")
    def job_flags(job_id: str) -> list[dict]:
        job = _job_or_404(job_id)
        try:
            return reviewing.load_flags_enriched(Path(job.run_dir))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"unreadable flags: {exc}")

    @app.post("/jobs/{job_id}/flags/recompute")
    def recompute_flags(job_id: str) -> dict:
        job = _job_or_404(job_id)
        try:
            return {"flags": reviewing.recompute_flags(Path(job.run_dir))}
        except Exception as exc:  # noqa: BLE001 — engine issues
            raise HTTPException(status_code=500, detail=f"recompute failed: {exc}")

    @app.post("/jobs/{job_id}/review/apply-all")
    def apply_all_reviews(job_id: str) -> dict:
        job = _job_or_404(job_id)
        try:
            return reviewing.apply_all_reviews(Path(job.run_dir))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"apply failed: {exc}")

    @app.post("/jobs/{job_id}/review")
    def review_flag(job_id: str, body: ReviewRequest) -> dict:
        job = _job_or_404(job_id)
        try:
            return reviewing.apply_review(Path(job.run_dir), body.row,
                                          body.status, body.note, body.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/exports")
    def exports() -> list[dict]:
        return exporting.list_profiles()

    @app.post("/exports/catalog")
    def exports_catalog(body: CatalogRequest) -> dict:
        try:
            return exporting.catalog(body.profile)
        except Exception as exc:  # noqa: BLE001 — config issues
            raise HTTPException(status_code=500, detail=f"catalog failed: {exc}")

    @app.post("/exports/seed")
    def exports_seed(body: SeedRequest) -> dict:
        try:
            return exporting.seed_from_workflow(body.workflow_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/exports/preview")
    def exports_preview(body: PreviewRequest) -> dict:
        try:
            return exporting.preview(body.profile, body.run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/exports")
    def exports_save(body: ExportProfileRequest) -> dict:
        try:
            return exporting.save_profile(body.profile, body.original_id)
        except Exception as exc:  # noqa: BLE001 — validation + IO
            raise HTTPException(status_code=400, detail=f"save failed: {exc}")

    @app.delete("/exports/{profile_id}")
    def exports_delete(profile_id: str) -> dict:
        try:
            return exporting.delete_profile(profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/exports/duplicate")
    def exports_duplicate(body: IdRequest) -> dict:
        try:
            return exporting.duplicate_profile(body.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/exports/yaml")
    def exports_yaml() -> dict:
        try:
            return {"text": exporting.get_yaml()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"yaml failed: {exc}")

    @app.post("/exports/validate")
    def exports_validate(body: TextRequest) -> dict:
        return exporting.validate_yaml(body.text)

    @app.post("/exports/yaml")
    def exports_save_yaml(body: TextRequest) -> dict:
        try:
            return exporting.save_yaml(body.text)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"save failed: {exc}")

    @app.post("/jobs/{job_id}/export")
    def build_export(job_id: str, body: ExportRequest) -> dict:
        job = _job_or_404(job_id)
        try:
            path = exporting.build(Path(job.run_dir), body.profile)
        except Exception as exc:  # noqa: BLE001 — ExportError + config issues
            raise HTTPException(status_code=400, detail=f"export failed: {exc}")
        pruned = None
        if _settings_view()["auto_prune_renders"]:
            try:
                pruned = app.state.manager.prune_renders(job_id)
            except ValueError:
                pruned = None
        return {"file": path.name, "path": str(path), "pruned": pruned}

    @app.get("/jobs/{job_id}/export/{file}")
    def download_export(job_id: str, file: str) -> FileResponse:
        job = _job_or_404(job_id)
        path = _safe_export_path(Path(job.run_dir), file)
        return FileResponse(path, filename=file,
                            media_type="application/vnd.openxmlformats-officedocument"
                                       ".spreadsheetml.sheet")

    return app
