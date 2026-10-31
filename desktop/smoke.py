#!/usr/bin/env python3
"""Live engine smoke (D66): boot the sidecar on a free port, verify the API.

    .venv/bin/python desktop/smoke.py

Starts the real engine service (no paid calls — just the HTTP surface),
checks /health and /defaults against the shipped config, prints a summary,
and always tears the process down. Exit 0 = green, 1 = red.
"""

from __future__ import annotations

import json
import os
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


def _python() -> str:
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(port: int, path: str, timeout: float = 2.0):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def main(argv=None) -> int:
    port = _free_port()
    proc = subprocess.Popen(
        [_python(), "-m", "desktop.sidecar", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60.0
        health = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"engine exited early (code {proc.returncode})", file=sys.stderr)
                return 1
            try:
                health = _get(port, "/health")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        if not health or health.get("status") != "ok":
            print("engine did not answer /health in time", file=sys.stderr)
            return 1

        defaults = _get(port, "/defaults")
        profiles = defaults.get("profiles") or []
        if not profiles or not defaults.get("primary_model"):
            print(f"defaults incomplete: {defaults}", file=sys.stderr)
            return 1

        print(f"smoke ok — engine {health.get('version')} on :{port}; "
              f"{len(profiles)} profile(s), model {defaults['primary_model']}")
        return 0
    finally:
        if proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:  # pragma: no cover
                    proc.terminate()
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
