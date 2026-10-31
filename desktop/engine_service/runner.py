"""The real engine glue: capture + extract for one desktop job (D66).

This is the ONLY place the desktop app touches the pipeline, and it touches
it exactly like the CLI does — `text_layer.run_batch` then
`extract.run_batch` — so desktop results are byte-for-byte the same code
path as `make ocr-corpus` + `make extract-corpus`. Imports are inside the
functions so the service can be imported (and tested) without the engine.

Defaults (profiles, model) come from the shipped config, hot-loaded at call
time (D10).
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import workspace as ws
from .jobs import Job

_ENGINE_ROOT = Path(__file__).resolve().parents[2]


def _ensure_engine_on_path() -> None:
    if (_ENGINE_ROOT / "extract.py").exists() and str(_ENGINE_ROOT) not in sys.path:
        sys.path.insert(0, str(_ENGINE_ROOT))


def engine_defaults() -> dict:
    """Profile ids + model names for the UI pickers (read from config)."""
    _ensure_engine_on_path()
    import config  # noqa: PLC0415 — engine import kept lazy

    cfg = config.load_all()
    profiles = [
        {"id": d.get("id"), "label": d.get("label") or d.get("id")}
        for d in cfg["documents"]["documents"]
    ]
    models = cfg["models"]
    return {
        "profiles": profiles,
        "default_profile": profiles[0]["id"] if profiles else None,
        "primary_model": models.get("primary_model"),
    }


def resolve_verification_model(job: Job) -> str:
    """Backup/recheck model for a job.

    Settings semantics: "" = same as primary (the job's own model, which may
    be a per-run override), "off" = no gated rechecks, else the model id.
    """
    vmodel = (job.verification_model or "").strip()
    if vmodel == "off":
        return ""
    return vmodel or job.model


def run_job(job: Job) -> None:
    """Capture then extract one job's staged files (raises on hard failure).

    `mode="extract"` reuses the run's existing capture output (the inspect →
    pin → extract stage); pins are passed through (D26). Flags are always
    recomputed from the stored JSONs afterwards so a re-extraction can never
    leave stale review rows (the Streamlit flow did this after retries).
    """
    _ensure_engine_on_path()

    from text_layer import run_batch as capture_batch  # noqa: PLC0415
    import extract as extract_mod  # noqa: PLC0415
    from ui import lib as ui_lib  # noqa: PLC0415

    run = Path(job.run_dir)
    if job.mode != "extract":
        rc = capture_batch(
            run / "inbox",
            run / "raw",
            vision_cap=1536,  # cost lever, same as the CLI default (2026-09-03)
        )
        if rc != 0:
            missing = [s for s in job.stems
                       if not (run / "raw" / s / "report.json").exists()]
            if missing and len(missing) == len(job.stems):
                raise RuntimeError(
                    f"capture failed for all {len(missing)} file(s) — "
                    "see the worker log"
                )
    if job.mode == "capture":
        return  # free preview: renders + text channel ready for review timing
    extract_mod.run_batch(
        run / "raw",
        run / "json",
        doc_id=job.doc_id,
        doc_ids=job.doc_ids or None,     # multi-profile (D27)
        workflow=job.workflow or None,   # bulk bundles + completeness (D32)
        model=job.model,
        workers=job.workers,
        files_filter=set(job.extract_stems) if job.extract_stems else None,
        pinned_pages=ws.parse_pages(job.pinned_pages),
        pinned_label=job.pinned_label,
        verification_model=resolve_verification_model(job),
    )
    ui_lib.recompute_flags(run / "raw", run / "json")
