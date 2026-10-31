"""Capture-engine seam (D63): Apple Vision preferred, cross-platform fallback.

The capture stage (routing probe, embedded text + geometry, OCR with boxes,
renders for the vision pass, image stats) is engine-pluggable. Every engine
produces the SAME per-page artifacts + report.json contract, so the quality
gate, grounding, verification, eval and UI are engine-agnostic.

- `apple_vision` — the Swift harness (swift/ocr.swift); macOS with swiftc.
- `crossplatform` — pypdfium2 + RapidOCR (PaddleOCR models on ONNX); any OS.

Selection: `auto` (default) prefers Apple Vision when the platform can run it,
else the cross-platform engine; `VERDANDI_OCR_ENGINE` or the CLI `--engine`
override. The chosen engine is recorded in report.json `engine` (provenance).
"""

from __future__ import annotations

import functools
import os
import platform
import shutil
import subprocess
from pathlib import Path

ENGINES = ("apple_vision", "crossplatform")
ENV_VAR = "VERDANDI_OCR_ENGINE"

# The Swift harness source. Packaged sidecars bundle it (desktop/build_sidecar.py);
# when it is absent the Apple engine cannot run and `auto` must fall back.
SWIFT_SOURCE = Path(__file__).resolve().parent.parent / "swift" / "ocr.swift"


class EngineError(RuntimeError):
    """No usable capture engine for the requested configuration."""


@functools.lru_cache(maxsize=None)
def _swiftc_available() -> bool:
    """Is a WORKING Swift compiler present (not just the /usr/bin shim)?

    `/usr/bin/swiftc` exists on every modern macOS even without the Xcode
    command line tools — invoking it pops the system "install developer
    tools" dialog and fails. `xcrun` is a real binary that fails cleanly
    instead, so resolve swiftc through it (cached: one probe per process).
    """
    xcrun = shutil.which("xcrun")
    if xcrun:
        try:
            proc = subprocess.run([xcrun, "--find", "swiftc"],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and bool(proc.stdout.strip())
    return shutil.which("swiftc") is not None


def swift_available() -> bool:
    """Cheap capability probe: macOS + a WORKING Swift compiler + the source.

    The source check matters for packaged builds: a sidecar without
    `swift/ocr.swift` on a Mac that HAS Xcode must not pick Apple Vision and
    then fail compiling a missing file — it falls back to the cross-platform
    engine instead. A compile failure WITH the source present still surfaces
    as a clear error (broken dev machine stays visible).
    """
    return (platform.system() == "Darwin"
            and _swiftc_available()
            and SWIFT_SOURCE.exists())


def detect_engine() -> str:
    """`auto` resolution: Apple Vision when this host can run it, else the
    cross-platform engine."""
    return "apple_vision" if swift_available() else "crossplatform"


def resolve_engine(name: str | None = None) -> str:
    """Explicit name > VERDANDI_OCR_ENGINE > auto-detection."""
    choice = (name or os.environ.get(ENV_VAR) or "auto").strip().lower()
    if choice == "auto":
        return detect_engine()
    if choice not in ENGINES:
        raise EngineError(
            f"unknown engine {choice!r} — choose auto|{'|'.join(ENGINES)}")
    return choice


def get_engine(name: str | None = None):
    """Instantiate the resolved engine (constructors are cheap; heavy
    resources load on first process())."""
    resolved = resolve_engine(name)
    if resolved == "apple_vision":
        from engines.apple_vision import AppleVisionEngine
        return AppleVisionEngine()
    from engines.crossplatform import CrossPlatformEngine
    return CrossPlatformEngine()
