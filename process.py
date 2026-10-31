"""Run management CLI (process.py).

Every corpus/batch pass is a RUN: out/runs/<YYYY-MM-DD>/<N>/ (auto-incremented
per day) with out/latest symlinked to it; all stage artifacts (raw/, json/,
logs/, flags.csv) live inside the run. This module owns run creation
(`--new-run`, used by the Makefile and the UI), logging, runtime directory
creation, and the config-contract smoke load. The pipeline itself runs through
text_layer.py / extract.py (Makefile `ocr-corpus`/`extract-corpus` or the UI).

Usage:
  python process.py --new-run   # create out/runs/<today>/<N>/, point out/latest at it
  python process.py [--watch]   # status pass (--watch polling lands in Stage 7)
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path

import config
import model_catalog

logger = logging.getLogger("process")

REPO_ROOT = Path(__file__).resolve().parent

# Runtime-created directories. Everything under here is gitignored (see .gitignore).
RUNTIME_DIRS = [
    "inbox",
    "out",
    "out/runs",
]

RUN_DATE_FMT = "%Y-%m-%d"
RUN_SUBDIRS = ("raw", "json", "logs")


def ensure_dirs() -> None:
    """Create runtime directories if missing (inbox/, out/, ...). Never committed."""
    for rel in RUNTIME_DIRS:
        (REPO_ROOT / rel).mkdir(parents=True, exist_ok=True)


def new_run(runs_dir: str = "out/runs", latest_link: str = "out/latest") -> Path:
    """Create <runs_dir>/<YYYY-MM-DD>/<N>/ (per-day auto-increment) with subdirs.

    Points <latest_link> at the new run (relative symlink). Returns the run dir.
    """
    day_dir = REPO_ROOT / runs_dir / datetime.date.today().strftime(RUN_DATE_FMT)
    day_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (day_dir / str(n)).exists():
        n += 1
    run_dir = day_dir / str(n)
    for sub in RUN_SUBDIRS:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    latest = REPO_ROOT / latest_link
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(run_dir.relative_to(REPO_ROOT / "out"), target_is_directory=True)
    return run_dir


def setup_logging(verbose: bool, run_dir: Path | None = None) -> None:
    """Log to stdout and <run>/logs/process.log (fallback out/process.log)."""
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(level)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)

    log_dir = run_dir / "logs" if run_dir else REPO_ROOT / "out"
    (log_dir).mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "process.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def process_file(path: Path) -> None:
    """Per-file hook for the future inbox loop (Stage 7) — a no-op today; the
    pipeline runs via text_layer.py / extract.py."""
    logger.debug("process_file: %s (inbox automation lands in Stage 7)", path.name)


def process_inbox(inbox: Path) -> None:
    """Inbox scan for the future --watch loop (Stage 7) — reports files only."""
    files = sorted(inbox.glob("*.pdf"))
    if not files:
        logger.info("inbox is empty: no files to process")
        return
    for path in files:
        logger.info("processing %s", path.name)
        process_file(path)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="process.py",
        description="Broker invoice extraction pipeline (see architecture.md, roadmap.md)",
    )
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="create out/runs/<YYYY-MM-DD>/<N>/ and point out/latest at it, then exit",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="poll inbox/ for new files (not yet implemented — lands in Stage 7)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_dirs()

    if args.new_run:
        run_dir = new_run()
        setup_logging(args.verbose, run_dir)
        logger.info("run created: %s (out/latest -> %s)", run_dir, run_dir.name)
        print(run_dir)
        return 0

    run_dir = None
    latest = REPO_ROOT / "out" / "latest"
    if latest.is_symlink():
        run_dir = latest.resolve()
    setup_logging(args.verbose, run_dir)

    if args.watch:
        logger.warning("--watch is not implemented yet (Stage 7): running a single status pass")

    cfg = config.load_all()
    logger.info(
        "config loaded: primary=%s mode=%s documents=%d thresholds=%d",
        cfg["models"]["primary_model"],
        cfg["models"]["primary_mode"],
        len(cfg["documents"]["documents"]),
        len(cfg["thresholds"]["thresholds"]),
    )
    known = model_catalog.cached_catalog_ids(cfg["models"]["endpoint"])
    if known and cfg["models"]["primary_model"] not in known:
        logger.warning("primary_model %r is not in the cached catalog snapshot "
                       "(removed/deprecated?) — run `make models`",
                       cfg["models"]["primary_model"])
    if not config.opencode_api_key():
        logger.warning("OPENCODE_API_KEY not set — extraction (extract.py / the UI) needs it")

    process_inbox(REPO_ROOT / "inbox")
    logger.info("run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
