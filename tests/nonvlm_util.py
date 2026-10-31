"""Shared plumbing for the non-VLM subsystem tests (2026-09-29 audit; D63).

Builds the synthetic fixture PDFs with the cross-platform Python generator
(tests/fixtures/pdfbuild.py — no Swift needed) and runs the capture engine +
D16 quality gate on them. Callers gate Apple-only assertions on
`swift_available()`.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def swift_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("swiftc") is not None


def font_calibrated() -> bool:
    """True when fixtures render with the calibrated macOS/Windows fonts.

    pdfbuild picks the first available system font (Helvetica/Arial on
    macOS/Windows, DejaVu on Linux). The engine quality floors were
    calibrated against Helvetica/Arial glyph metrics; on Linux the rendered
    fixture differs and those assertions do not transfer — they self-skip.
    """
    sys.path.insert(0, str(REPO / "tests" / "fixtures"))
    try:
        import pdfbuild
    except ImportError:  # pragma: no cover — Pillow missing
        return False
    return pdfbuild.font_family() in ("Helvetica.ttc", "Arial.ttf", "arial.ttf")


def build_fixtures(dest: Path) -> dict[str, Path]:
    """Generate every synthetic fixture into `dest`; {name: pdf path}."""
    sys.path.insert(0, str(REPO / "tests" / "fixtures"))
    import pdfbuild

    if not pdfbuild.available():
        raise RuntimeError("fixture generation needs Pillow "
                           "(pip install -r requirements-crossplatform.txt)")
    return pdfbuild.build_fixtures(dest)


def process(pdf: Path, out_dir: Path, *, engine: str | None = None) -> dict:
    """Capture engine + quality gate, in place (mirrors text_layer.run_batch).

    `engine` = None/auto (Apple Vision on this host when available), or an
    explicit name for cross-engine parity runs.
    """
    sys.path.insert(0, str(REPO))
    import config
    import text_layer

    report = text_layer.ocr_pdf(pdf, out_dir, engine=engine)
    report = text_layer.correct_orientation(out_dir, report)  # D64
    report = text_layer.apply_quality_gate(report, out_dir, config.load_all())
    (out_dir / "report.json").write_text(json.dumps(report, indent=2),
                                         encoding="utf-8")
    return report


def process_all(fixtures: dict[str, Path], dest: Path,
                *, engine: str | None = None) -> dict[str, dict]:
    """Process every fixture once; {name: report} (raws under dest/<name>/)."""
    out: dict[str, dict] = {}
    for name, pdf in fixtures.items():
        out[name] = process(pdf, dest / name, engine=engine)
    return out


def page_text(out_dir: Path, page: int = 1) -> str:
    path = out_dir / f"page{page:03d}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def bbox_rows(out_dir: Path, page: int = 1) -> list[dict]:
    """Parsed bbox rows: {lineno, geo (str), confidence, text}."""
    path = out_dir / f"page{page:03d}.bbox.txt"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        rows.append({"lineno": int(parts[0]), "geo": parts[1].strip(),
                     "confidence": float(parts[2] or 0), "text": parts[3]})
    return rows


def marker_order(text: str) -> str:
    """'upright' | 'upside-down' | 'unknown' from TOP/BOTTOM marker positions."""
    top = text.find("TOP MARKER")
    bottom = text.find("BOTTOM MARKER")
    if top < 0 or bottom < 0:
        return "unknown"
    return "upright" if top < bottom else "upside-down"
