#!/usr/bin/env python3
"""Build the desktop engine sidecar (D66, PyInstaller) and place it where
Tauri expects the `externalBin`:

    src-tauri/binaries/engine-<target-triple>[.exe]

Run before `npm run tauri build` (CI does this automatically). For local
`tauri dev`, pass --dev-copy to also drop `engine[.exe]` next to the debug
binary, or simply run `.venv/bin/python -m desktop.sidecar` in a terminal —
the window connects to it over 127.0.0.1 (that is the fastest dev loop).

Usage:
    .venv/bin/python desktop/build_sidecar.py [--dev-copy]
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = HERE / "dist-sidecar"
WORK = HERE / ".pyinstaller"
BINARIES = HERE / "src-tauri" / "binaries"

# Heavy optional stacks that the engine imports lazily — PyInstaller cannot
# see them, so collect them explicitly (capture needs them to run at all).
# keyring is small but needs its backend modules AND its dist-info metadata
# (see the --copy-metadata below) — without both, a packaged app silently has
# no keychain backend and the gateway key never persists.
COLLECT_ALL = ("uvicorn", "fastapi", "pydantic", "pypdfium2", "rapidocr",
               "onnxruntime", "numpy", "pypdf", "PIL", "keyring")

# Shipped defaults + assets the frozen app reads from the bundle root:
# the user-config layer seeds from config/ + rules/ (D66 Batch 1) and the
# vision probe image lives in ui/static.
DATA_DIRS = ("config", "rules", "ui/static")

# Individual files: the Swift harness source (macOS; macs with Xcode compile
# it on first capture, cached under ~/.verdandi/cache/swift) and the D64
# orientation classifier (engines/orientation.py resolves it next to itself,
# i.e. <bundle>/engines/models/).
DATA_FILES = (("swift/ocr.swift", "swift"),
              ("engines/models/PP-LCNet_x1_0_doc_ori.onnx", "engines/models"))

# Engine modules imported lazily at runtime — a stale build venv would
# silently produce a bundle without them (no keychain / no Excel export).
REQUIRED_MODULES = ("keyring", "openpyxl")


def check_build_deps() -> None:
    """Fail fast when the build venv is missing runtime deps the engine needs."""
    import importlib.util

    missing = [name for name in REQUIRED_MODULES
               if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(
            f"missing build deps: {', '.join(missing)} — install with "
            f"`python -m pip install -r desktop/requirements-desktop.txt`")


def target_triple() -> str:
    """The Tauri target triple — from rustc when available, else inferred."""
    rustc = shutil.which("rustc")
    if rustc:
        try:
            out = subprocess.run([rustc, "-Vv"], capture_output=True, text=True,
                                 check=True).stdout
            for line in out.splitlines():
                if line.startswith("host:"):
                    return line.split()[1]
        except (OSError, subprocess.CalledProcessError):
            pass
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "darwin":
        return "aarch64-apple-darwin" if machine in ("arm64", "aarch64") else "x86_64-apple-darwin"
    if system == "windows":
        return "x86_64-pc-windows-msvc"
    return ("aarch64-unknown-linux-gnu" if machine in ("arm64", "aarch64")
            else "x86_64-unknown-linux-gnu")


DEV_LAUNCHER = """#!/bin/sh
# Verdandi dev sidecar launcher (D66) — runs the engine from source.
# Written by desktop/build_sidecar.py --dev-placeholder; never committed.
d="$(cd "$(dirname "$0")" && pwd)"
while [ "$d" != "/" ] && [ ! -f "$d/desktop/sidecar.py" ]; do d="$(dirname "$d")"; done
cd "$d" || exit 1
exec "$d/.venv/bin/python" -m desktop.sidecar
"""


def write_dev_placeholder() -> None:
    """Tiny launcher so `tauri dev` builds without a PyInstaller bundle.

    Tauri validates the `externalBin` resource even in dev; this script sits
    at the expected path and, when the shell spawns it, runs the engine from
    source (repo venv). Release builds overwrite it with the real binary.
    """
    triple = target_triple()
    BINARIES.mkdir(parents=True, exist_ok=True)
    dest = BINARIES / f"engine-{triple}"
    dest.write_text(DEV_LAUNCHER, encoding="utf-8")
    dest.chmod(0o755)
    dev_dir = HERE / "src-tauri" / "target" / "debug"
    dev_dir.mkdir(parents=True, exist_ok=True)
    beside = dev_dir / "engine"
    beside.write_text(DEV_LAUNCHER, encoding="utf-8")
    beside.chmod(0o755)
    print(f"dev launcher -> {dest}")
    print(f"dev launcher -> {beside}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev-copy", action="store_true",
                    help="also copy next to src-tauri/target/debug for `tauri dev`")
    ap.add_argument("--dev-placeholder", action="store_true",
                    help="write the source-run launcher instead of a PyInstaller "
                         "build (fast dev window loop, macOS/Linux)")
    args = ap.parse_args(argv)

    if args.dev_placeholder:
        write_dev_placeholder()
        return 0

    check_build_deps()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        # GUI subsystem on Windows: a console-subsystem exe pops a console
        # window when the Tauri shell (a GUI app) spawns it. macOS must NOT
        # use --windowed — it would produce a .app bundle instead of a plain
        # binary and break the externalBin layout.
        *(["--noconsole"] if os.name == "nt" else []),
        "--name", "engine",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
        "--paths", str(ROOT),
        *[arg for name in COLLECT_ALL for arg in ("--collect-all", name)],
        # keyring discovers its backends via importlib.metadata entry points:
        # without the dist-info in the bundle a packaged app silently has NO
        # keychain backend (the key then never persists).
        "--copy-metadata", "keyring",
        *[arg for rel in DATA_DIRS
          for arg in ("--add-data", f"{ROOT / rel}{os.pathsep}{rel}")],
        *[arg for rel, dest in DATA_FILES
          for arg in ("--add-data", f"{ROOT / rel}{os.pathsep}{dest}")],
        str(HERE / "sidecar.py"),
    ]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)

    suffix = ".exe" if os.name == "nt" else ""
    built = DIST / f"engine{suffix}"
    if not built.exists():
        raise SystemExit(f"PyInstaller did not produce {built}")

    triple = target_triple()
    BINARIES.mkdir(parents=True, exist_ok=True)
    dest = BINARIES / f"engine-{triple}{suffix}"
    shutil.copy2(built, dest)
    print(f"sidecar -> {dest}")

    if args.dev_copy:
        dev_dir = HERE / "src-tauri" / "target" / "debug"
        dev_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, dev_dir / f"engine{suffix}")
        print(f"sidecar -> {dev_dir / f'engine{suffix}'} (tauri dev)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
