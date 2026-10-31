"""Apple Vision capture engine — the Swift harness (swift/ocr.swift).

One compiled subprocess per PDF. Behavior is frozen (D15/D16/D20/D21/D24/
D61/D62 all live in the Swift source); this module only wraps the invocation
and the report provenance.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import engines

logger = logging.getLogger("engines.apple_vision")

REPO_ROOT = Path(__file__).resolve().parent.parent
SWIFT_DIR = REPO_ROOT / "swift"
ENV_BIN_CACHE = "VERDANDI_SWIFT_CACHE"


def _binary_path() -> Path:
    """Where the compiled harness lives.

    `VERDANDI_SWIFT_CACHE` wins; packaged builds use a stable user cache
    (`~/.verdandi/cache/swift/ocr`) because the bundle's `_MEIPASS` is
    wiped between launches (a recompile per launch otherwise); dev checkouts
    keep the mtime-cached `swift/.build/ocr`.
    """
    env = os.environ.get(ENV_BIN_CACHE)
    if env:
        return Path(env).expanduser() / "ocr"
    if getattr(sys, "frozen", False):
        return Path.home() / ".verdandi" / "cache" / "swift" / "ocr"
    return SWIFT_DIR / ".build" / "ocr"


BINARY = _binary_path()

DEFAULT_TIMEOUT_S = 120  # per-file; one bad file must never hang the batch

# Exit codes of the Swift harness (documented in swift/ocr.swift).
EXIT_OK = 0
EXIT_OPEN_FAIL = 2
EXIT_PARTIAL = 3
EXIT_USAGE = 64


def _source_digest(source: Path) -> str:
    import hashlib

    return hashlib.sha256(source.read_bytes()).hexdigest()


def ensure_binary() -> Path:
    """Compile swift/ocr.swift on demand (swiftc -O) when the binary is
    missing OR was built from a different source revision.

    The stamp is a content hash, not an mtime: PyInstaller onefile does NOT
    preserve mtimes (extracted files always look brand-new), so an mtime
    check recompiled once per app launch in packaged builds. A source edit
    must never keep using a stale binary (2026-09-22: the geometry fix was
    silently ignored by the cached build)."""
    source = engines.SWIFT_SOURCE
    if not source.exists():
        raise RuntimeError(
            f"Swift harness source missing: {source} — this build cannot use "
            "the Apple Vision engine; use --engine crossplatform"
        )
    digest = _source_digest(source)
    stamp = BINARY.parent / (BINARY.name + ".sha256")
    if BINARY.exists() and stamp.exists():
        try:
            if stamp.read_text(encoding="utf-8").strip() == digest:
                return BINARY
        except OSError:
            pass
    logger.info("compiling %s (swiftc -O) — source changed or binary missing",
                BINARY)
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    # Compile to a temp path + atomic replace: a killed compile must never
    # leave a half-written binary that the hash stamp would bless.
    tmp = BINARY.parent / (BINARY.name + ".tmp")
    proc = subprocess.run(
        ["swiftc", "-O", str(source), "-o", str(tmp)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"swiftc failed (exit {proc.returncode}): {proc.stderr[-2000:]}"
        )
    os.replace(tmp, BINARY)
    stamp.write_text(digest + "\n", encoding="utf-8")
    return BINARY


class AppleVisionEngine:
    """macOS capture engine: PDFKit text/renders + Apple Vision OCR."""

    name = "apple_vision"

    def available(self) -> bool:
        return engines.swift_available()

    def describe(self) -> dict:
        return {"name": self.name, "platform": "swift",
                "binary": str(BINARY)}

    def process(
        self,
        pdf_path: Path,
        out_dir: Path,
        *,
        custom_words: list[str] | None = None,
        ocr_only: bool = False,
        dump_embedded: bool = False,
        vision_cap: int = 0,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict:
        """Run the Swift harness on one PDF, isolated from all other files.

        Returns the report dict on success (exit 0 or 3 — partial = some pages
        failed but the report is still readable). Raises OSError on timeout,
        and RuntimeError on open-failure/usage errors where no usable report
        exists.
        """
        binary = ensure_binary()
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(binary), "process", str(pdf_path), str(out_dir)]
        if custom_words:
            cmd += ["--custom-words", json.dumps(custom_words)]
        if ocr_only:
            cmd += ["--ocr-only"]
        if dump_embedded:
            cmd += ["--dump-embedded"]
        if vision_cap > 0:
            cmd += ["--vision-cap", str(vision_cap)]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise OSError(
                f"ocr timed out after {timeout_s:.0f}s on {pdf_path.name} "
                f"(stderr tail: {(exc.stderr or b'')[-300:].decode(errors='replace')})"
            ) from None

        report_path = out_dir / "report.json"
        if proc.returncode in (EXIT_OK, EXIT_PARTIAL) and report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report.setdefault("engine", self.describe())
            return report

        if proc.returncode == EXIT_OPEN_FAIL:
            raise RuntimeError(
                f"cannot open PDF (corrupt or unsupported): {pdf_path.name}")
        if proc.returncode == EXIT_USAGE:
            raise RuntimeError(
                f"ocr usage error on {pdf_path.name}: {proc.stderr[-500:]}")
        raise RuntimeError(
            f"ocr failed (exit {proc.returncode}) on {pdf_path.name}: "
            f"{proc.stderr[-500:]}"
        )
