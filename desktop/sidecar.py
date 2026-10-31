"""Desktop sidecar entry point (D66).

Two uses:
- development:  `python -m desktop.sidecar`            (repo root on path)
- release:      PyInstaller entry script (CI builds one binary per OS)

Binds 127.0.0.1 only. The Tauri shell starts this binary and probes
/health before showing the UI.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "extract.py").exists() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_PORT = 8787


def _configure_service_logging() -> Path:
    """Route service logs to <workspace>/logs/engine.log (rotated ~5 MB).

    Packaged apps have no console, and a windowed Windows build may have no
    usable stdio at all — the file is the durable channel (the shell also
    redirects the engine's stdout/stderr there). uvicorn is told not to
    install its own handlers (`log_config=None`) so everything flows through
    the root logger, and access logs are OFF: the UI polls /health every few
    seconds and the access lines would fill the file with noise.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    from desktop.engine_service.workspace import workspace_root

    path = workspace_root() / "logs" / "engine.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                       backupCount=1, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # Dev terminals keep readable console output; a redirected/absent stdio
    # (packaged app) goes to the file only.
    if sys.stdout is not None and sys.stdout.isatty():
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verdandi desktop engine service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("VERDANDI_SIDECAR_PORT", DEFAULT_PORT)))
    ap.add_argument("--reload", action="store_true",
                    help="dev only: restart the engine when Python files change "
                         "(run from the repo root)")
    ap.add_argument("--run-job", metavar="JOB_JSON",
                    help="worker mode (internal): run one job from its job.json "
                         "and exit — spawned by the service for cancellability")
    args = ap.parse_args(argv)

    if args.run_job:
        import json

        from desktop.engine_service import settings, userconfig
        from desktop.engine_service.jobs import Job
        from desktop.engine_service.runner import run_job

        data = json.loads(Path(args.run_job).read_text(encoding="utf-8"))
        job = Job.from_dict(data)
        # Worker stdout/stderr is captured to <run>/logs/worker.log — make the
        # engine's own INFO lines part of it (the View log dialog reads it).
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
        logging.basicConfig(level=logging.INFO, format=fmt)
        if sys.stderr is None:
            # Windowed builds can have no usable stdio even though the parent
            # opened worker.log and passed it as a handle — write the file
            # directly so the worker log is never silently empty.
            handler = logging.FileHandler(
                Path(job.run_dir) / "logs" / "worker.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter(fmt))
            logging.getLogger().addHandler(handler)
        userconfig.bootstrap()
        settings.apply_key()
        run_job(job)
        return 0

    # A dead launcher (crashed window / closed terminal / Task Manager) must
    # never orphan the engine on its port — create_app() checks this env in
    # every process it runs in, including uvicorn's reload children.
    os.environ["VERDANDI_WATCH_PARENT"] = "1"

    import uvicorn

    from desktop.engine_service.app import create_app, watch_parent

    log_path = _configure_service_logging()

    watch_parent()  # this process (the reloader in --reload mode)

    logging.getLogger("verdandi.sidecar").info(
        "engine on http://%s:%s (log: %s)", args.host, args.port, log_path)

    if args.reload:
        # Dev only: uvicorn keeps its own console logging — reload children
        # don't inherit our handlers anyway, and access logs are welcome in a
        # terminal. Import-string + factory so the reloader re-imports cleanly.
        uvicorn.run("desktop.engine_service.app:create_app", factory=True,
                    reload=True, reload_dirs=[str(_REPO_ROOT)],
                    host=args.host, port=args.port, log_level="info")
        return 0

    uvicorn.run(create_app(), host=args.host, port=args.port,
                log_level="info", access_log=False, log_config=None)
    return 0


def _log_fatal() -> None:
    """Best-effort crash log for windowed builds (no stdio, no dialogs)."""
    try:
        import traceback

        from desktop.engine_service.workspace import workspace_root

        path = workspace_root() / "logs" / "engine.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
    except Exception:  # noqa: BLE001 — logging must never mask the exit
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — includes KeyboardInterrupt
        if getattr(sys, "frozen", False):
            # PyInstaller windowed builds pop a modal fatal-error dialog for
            # unhandled exceptions; the shell watchdog would respawn straight
            # into a dialog storm. Log and exit quietly instead.
            _log_fatal()
            sys.exit(1)
        raise
