"""Workspace layout for the desktop app (D66).

Everything a job needs lives under one run directory, mirroring the CLI's
self-contained-run convention (D18):

    <workspace>/runs/<YYYY-MM-DD>/<N>/
        job.json      job metadata (state, timings, options)
        inbox/        staged input PDFs (copied — runs are self-contained)
        raw/          capture output (text_layer.run_batch)
        json/         extraction output (extract.run_batch) + flags.csv

The workspace defaults to `~/.verdandi` (override with VERDANDI_WORKSPACE).
No core module reads this layout — it is desktop-only.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

DEFAULT_WORKSPACE = Path.home() / ".verdandi"
MAX_STEM = 80


def workspace_root() -> Path:
    env = os.environ.get("VERDANDI_WORKSPACE")
    return Path(env).expanduser() if env else DEFAULT_WORKSPACE


def allocate_run(root: Path, today: dt.date | None = None) -> Path:
    """Create and return a fresh run dir under <root>/runs/<date>/<n>/."""
    day = root / "runs" / (today or dt.date.today()).isoformat()
    day.mkdir(parents=True, exist_ok=True)
    n = 1
    while (day / str(n)).exists():
        n += 1
    run = day / str(n)
    run.mkdir()
    return run


def parse_pages(spec: str | None) -> list[int] | None:
    """Parse a pin spec like "1-3,5" into [1, 2, 3, 5] (None when empty)."""
    if not spec or not spec.strip():
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                start, end = int(lo), int(hi)
            except ValueError:
                raise ValueError(f"bad page range: {part!r}")
            if start < 1 or end < start:
                raise ValueError(f"bad page range: {part!r}")
            pages.extend(range(start, end + 1))
        else:
            try:
                page = int(part)
            except ValueError:
                raise ValueError(f"bad page number: {part!r}")
            if page < 1:
                raise ValueError(f"bad page number: {part!r}")
            pages.append(page)
    return sorted(set(pages)) or None


def expand_pdf_paths(paths: list[str]) -> list[Path]:
    """Resolve input paths to PDF files: directories are searched recursively.

    Raises ValueError with a human-readable message when a path is missing or
    nothing PDF-like is found. Order is preserved, duplicates dropped.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            raise ValueError(f"path does not exist: {raw}")
        candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in candidates:
            if f.is_file() and f.suffix.lower() == ".pdf":
                resolved = f.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
    if not found:
        raise ValueError("no PDF files found in the selected paths")
    return found


def stage_inputs(files: list[Path], inbox: Path) -> list[str]:
    """Copy the inputs into <run>/inbox (self-contained runs) and return stems.

    Duplicate file names get a numeric suffix so no input is ever overwritten
    (the anti-mix-up rule the CLI learned the hard way, 2026-09-05).
    """
    inbox.mkdir(parents=True, exist_ok=True)
    stems: list[str] = []
    used: set[str] = set()
    for f in files:
        stem = f.stem[:MAX_STEM] or "document"
        candidate, i = stem, 2
        while candidate in used:
            candidate = f"{stem}_{i}"
            i += 1
        used.add(candidate)
        shutil.copy2(f, inbox / f"{candidate}.pdf")
        stems.append(candidate)
    return stems
