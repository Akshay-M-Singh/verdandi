"""Catalog sweep for the desktop app (D66 Phase 2, Batch 2).

Wraps `extract.run_catalog` (D26): ONE cheap call per file listing the
documents inside ("Invoice p3, HAWB p11"). Advisory only — pinning is the
source of truth and extraction is never gated on it.
"""

from __future__ import annotations

import json
from pathlib import Path


def run(run_dir: Path, *, model: str, files: set[str] | None = None) -> int:
    import extract as extract_mod  # noqa: PLC0415 — engine import kept lazy

    return extract_mod.run_catalog(
        Path(run_dir) / "raw", Path(run_dir) / "catalog",
        model=model, files_filter=files)


def cards(run_dir: Path) -> list[dict]:
    """Parsed catalog cards for the UI (one row per staged file)."""
    out: list[dict] = []
    catalog_dir = Path(run_dir) / "catalog"
    if not catalog_dir.is_dir():
        return out
    for path in sorted(catalog_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"status": "failed", "error": "unreadable catalog output"}
        out.append({
            "stem": path.stem,
            "status": data.get("status", "ok"),
            "error": data.get("error"),
            "documents": data.get("documents") or [],
        })
    return out
