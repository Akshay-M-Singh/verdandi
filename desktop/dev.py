#!/usr/bin/env python3
"""One-command dev loop (D66): engine (auto-reload) + interface, Ctrl-C stops both.

    make desktop-dev            # or: .venv/bin/python desktop/dev.py

Open http://localhost:5173 — interface changes hot-reload on save; engine
changes restart themselves via uvicorn's reloader. This is the desktop
equivalent of `make ui`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_PORT = 8787
UI_PORT = 5173


def _python() -> str:
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def _spawn(cmd: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=cwd, start_new_session=True)


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:  # pragma: no cover — Windows fallback
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


def _wait_engine(port: int, timeout: float = 60.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as res:
                return res.status == 200
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _port_in_use(port: int) -> bool:
    """True when something already listens on the port (IPv4 or IPv6)."""
    for host, family in (("127.0.0.1", socket.AF_INET),
                         ("::1", socket.AF_INET6)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("VERDANDI_SIDECAR_PORT", DEFAULT_PORT)))
    args = ap.parse_args(argv)

    npm = shutil.which("npm")
    if not npm:
        print("npm not found — install Node.js first", file=sys.stderr)
        return 1

    if _port_in_use(args.port):
        print(f"engine port {args.port} is already in use — another engine is "
              f"running (stop it, or pass --port)", file=sys.stderr)
        return 1
    if _port_in_use(UI_PORT):
        print(f"interface port {UI_PORT} is already in use — another dev "
              f"server is running (stop it first)", file=sys.stderr)
        return 1

    print(f"starting engine (auto-reload) on 127.0.0.1:{args.port} …", flush=True)
    engine = _spawn([_python(), "-m", "desktop.sidecar", "--reload", "--port",
                     str(args.port)], cwd=ROOT)
    ui = _spawn([npm, "run", "dev"], cwd=HERE)

    def _shutdown(*_sig) -> None:
        print("\nstopping engine + interface …", flush=True)
        _stop(ui)
        _stop(engine)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if _wait_engine(args.port):
        print(f"engine ready  → http://127.0.0.1:{args.port}", flush=True)
    else:
        print(f"engine not answering yet on :{args.port} — check its output above",
              file=sys.stderr)
    print(f"interface     → http://localhost:{UI_PORT}   (Ctrl-C stops everything)",
          flush=True)

    try:
        while True:
            for proc, name in ((engine, "engine"), (ui, "interface")):
                if proc.poll() is not None:
                    print(f"{name} exited with code {proc.returncode} — stopping "
                          f"the other", file=sys.stderr)
                    _stop(engine)
                    _stop(ui)
                    return proc.returncode or 0
            time.sleep(0.25)
    except SystemExit as exc:  # from the signal handler
        return int(exc.code or 0)


if __name__ == "__main__":
    sys.exit(main())
