"""Import benchmark Verdandi runs into the desktop app workspace (view only).

The benchmark harness writes `benchmark/data/results/verdandi/<date>/<n>/`
(json/, flags.csv, raw/, run.json) — the desktop Console reads
`<workspace>/runs/<date>/<n>/` with a `job.json`. This script bridges the two
so the benchmark runs can be inspected in the app (Results, Review, evidence,
metrics), without touching the benchmark artifacts:

- json/, flags.csv  -> COPIED (review write-through / flag recompute edit only
  the workspace copies)
- raw/              -> HARDLINKED file-by-file (no extra disk; deleting or
  pruning the view-run only removes its links, the benchmark renders stay)
- review.json {}, job.json -> created (state done, name, model, timings)

Idempotent: an existing target run dir is skipped. ACU runs are NOT imported
(their JSON shape is not the Verdandi extraction schema).

    python benchmark/import_to_ui.py                 # all runs, default ws
    python benchmark/import_to_ui.py --dry-run
    python benchmark/import_to_ui.py --runs 11,12
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "data" / "results" / "verdandi"
PDFS = HERE / "data" / "pdfs"


def _workspace(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("VERDANDI_WORKSPACE")
    return Path(env).expanduser() if env else Path.home() / ".verdandi"


def _hardlink_tree(src: Path, dst: Path) -> int:
    """Recreate src as dst with hardlinked files (fallback: copy)."""
    linked = 0
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            target = dst / rel / name
            if target.exists():
                continue
            try:
                os.link(Path(root) / name, target)
                linked += 1
            except OSError:  # cross-device or FS without links -> copy
                shutil.copy2(Path(root) / name, target)
    return linked


def _vision_label(run_meta: dict) -> str:
    vision = str(run_meta.get("vision_input") or "")
    if "full" in vision:
        return "full-res"
    return "capped"


def _files_rows(raw: Path, json_dir: Path, stems: list[str]) -> list[dict]:
    rows = []
    for stem in stems:
        out = json_dir / f"{stem}.json"
        captured = (raw / stem / "report.json").exists()
        status, found, error = "pending", None, None
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
                status, error = "error", "unreadable extraction JSON"
        elif captured:
            status = "captured"
        rows.append({"stem": stem, "status": status, "found": found,
                     "error": error})
    return rows


def _job_dict(run: Path, target: Path, n: int) -> dict:
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    raw, json_dir = run / "raw", run / "json"
    stems = sorted(p.name for p in raw.iterdir() if p.is_dir())
    started = meta.get("started_at") or ""
    elapsed = float(meta.get("elapsed_s") or 0)
    finished = started
    if started and elapsed:
        import datetime as dt

        finished = (dt.datetime.fromisoformat(started)
                    + dt.timedelta(seconds=elapsed)).isoformat(timespec="seconds")
    model = meta.get("model") or ""
    files = meta.get("files") or len(stems)
    return {
        "id": f"bench{run.parent.name.replace('-', '')}{n:02d}",
        "state": "done",
        "created_at": started,
        "name": (f"Benchmark {n} · {files} docs · {model} · "
                 f"{_vision_label(meta)}"),
        "paths": [str(PDFS / f"{s}.pdf") for s in stems],
        "stems": stems,
        "run_dir": str(target),
        "doc_id": "benchmark_document",
        "model": model,
        "workers": 3,
        "mode": "full",
        "verification_model": "gpt-5.6-luna",
        "doc_ids": ["benchmark_document"],
        "workflow": None,
        "extract_stems": [],
        "pinned_pages": None,
        "pinned_label": None,
        "renders_pruned": False,
        "started_at": started,
        "finished_at": finished,
        "error": None,
        "files": _files_rows(raw, json_dir, stems),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", help="app workspace (default ~/.verdandi)")
    ap.add_argument("--date", help="benchmark date dir (default: all dates)")
    ap.add_argument("--runs", help="comma list of run numbers to import")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    workspace = _workspace(args.workspace)
    wanted = ({int(x) for x in args.runs.split(",") if x.strip()}
              if args.runs else None)
    imported = skipped = 0
    for date_dir in sorted(RESULTS.glob("*")):
        if not date_dir.is_dir() or (args.date and date_dir.name != args.date):
            continue
        for run in sorted(date_dir.iterdir(), key=lambda p: int(p.name)
                          if p.name.isdigit() else 0):
            if not run.is_dir() or not (run / "run.json").exists():
                continue
            n = int(run.name)
            if wanted and n not in wanted:
                continue
            target = workspace / "runs" / date_dir.name / run.name
            if target.exists():
                skipped += 1
                continue
            if args.dry_run:
                print(f"would import {run} -> {target}")
                continue
            target.mkdir(parents=True)
            shutil.copytree(run / "json", target / "json")
            if (run / "flags.csv").exists():
                shutil.copy2(run / "flags.csv", target / "flags.csv")
            linked = _hardlink_tree(run / "raw", target / "raw")
            (target / "review.json").write_text("{}\n", encoding="utf-8")
            job = _job_dict(run, target, n)
            (target / "job.json").write_text(
                json.dumps(job, indent=2) + "\n", encoding="utf-8")
            print(f"imported {date_dir.name}/{n} -> {target} "
                  f"({len(job['stems'])} files, {linked} hardlinked renders)")
            imported += 1
    print(f"done: {imported} imported, {skipped} already present, "
          f"workspace {workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
