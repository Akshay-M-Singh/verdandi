"""Text acquisition — embedded-text routing + OCR (engine-pluggable, D63).

Stage 1 implementation. The capture engine is selected per host (Apple Vision
on macOS with swiftc, else the cross-platform pypdfium2 + RapidOCR engine —
`engines/`); both produce the same artifacts, so everything below this module
is engine-agnostic. For each PDF:

  1. Per-page probe (Swift CGPDF scan, inside the harness): embedded-text
     quality (chars, alpha ratio, word count), full-page image coverage,
     invisible-text mode, image codecs. Hybrid pages (OCR text layer over a
     scan, invisible text, or text under a full-page image) are routed to OCR
     (architecture.md D15).
  2. Embedded pages: PDFKit text extraction (handles CID/ToUnicode).
  3. Scanned/hybrid pages: Apple Vision OCR via `swift/ocr.swift` (3x render,
     `.accurate`, en-US, language correction, customWords from the document
     profiles' `keywords` in `rules/documents.yaml` — D29).
  4. Output per file under `<outdir>/<stem>/`: `pageNNN.txt`,
     `pageNNN.bbox.txt`, `report.json`), plus a combined text with `[PAGE N]` markers.

Vision-primary (D24, 2026-08-19): the harness ALWAYS writes a per-page vision
input — `pageNNN.render-ocr-input.png` (page render; embedded pages included)
+ `pageNNN.image.png` (raw embedded photo for pure-image pages) — because the
primary extraction pass reads IMAGES, not text. The text channel here (embedded
routing + OCR) feeds ONLY the deterministic verification stack (grounding /
arithmetic / format / gated disagreement — D30), never an LLM. No
`--dump-renders` flag anymore: the dumps are unconditional.

Reference format (source pointers for downstream stages): a value's source is
"page N, line M" — line M (1-based) of `<outdir>/<stem>/pageNNN.txt`.
The same line's geometry lives in `pageNNN.bbox.txt` row M:
`lineno|x0,y0,x1,y1|confidence|text` (normalized Vision coordinates,
origin bottom-left; empty geometry field for embedded lines).

Run modes:
  python text_layer.py <in_dir> <out_dir> [options]     # corpus/batch run

See roadmap.md Stage 1 and architecture.md D2/D3/D15.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import config
import engines
import llm_client

logger = logging.getLogger("text_layer")

REPO_ROOT = Path(__file__).resolve().parent

DEFAULT_TIMEOUT_S = 120  # per-file; one bad file must never hang the batch


def custom_words_from_profiles() -> list[str]:
    """Custom OCR words from the document profiles' user keywords (D29, hot-loaded).

    The keywords are the tokens Vision most often mangles (all-caps multi-word
    names). Fed as-is plus their space-split pieces (>=4 chars) so both whole
    phrases and fragments match. No broker registry anymore (D29).
    """
    cfg = config.load_all()
    words: set[str] = set()
    for doc in cfg["documents"]["documents"]:
        for kw in (doc.get("signals") or {}).get("keywords", []):
            words.add(kw)
            words.update(w for w in kw.replace("/", " ").split() if len(w) >= 4)
    return sorted(words)


def ocr_pdf(
    pdf_path: Path,
    out_dir: Path,
    *,
    engine: str | None = None,
    custom_words: list[str] | None = None,
    ocr_only: bool = False,
    dump_embedded: bool = False,
    vision_cap: int = 0,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Run the selected capture engine on one PDF (D63).

    `engine` = None/auto (Apple Vision when the host can run it, else the
    cross-platform engine) or an explicit name; VERDANDI_OCR_ENGINE overrides
    the auto default. Every engine produces the same artifacts + report
    contract (the report carries an `engine` provenance block).

    Vision inputs (pageNNN.render-ocr-input.png / image.png) are written
    unconditionally (D24 vision-primary) — no flag needed.
    """
    return engines.get_engine(engine).process(
        pdf_path, out_dir,
        custom_words=custom_words, ocr_only=ocr_only,
        dump_embedded=dump_embedded, vision_cap=vision_cap,
        timeout_s=timeout_s,
    )


def _rotate_geometry_row(geo: str, rotation: int) -> str:
    from engines import orientation
    try:
        x0, y0, x1, y1 = (float(v) for v in geo.split(","))
    except ValueError:
        return geo
    nx0, ny0, nx1, ny1 = orientation.rotate_bbox(x0, y0, x1, y1, rotation)
    return f"{nx0:.4f},{ny0:.4f},{nx1:.4f},{ny1:.4f}"


def _rotate_text_channel(raw_dir: Path, page_no: int, rotation: int) -> None:
    """Rotate the page's text-channel geometry by `rotation` (CCW) and rebuild
    the line files. OCR pages carry segments.txt (fragments) — re-assembled so
    line order/labels follow the upright frame; embedded pages rotate their
    per-line bbox rows and keep the text order (content order is stable)."""
    from engines.crossplatform import assemble_lines
    from engines import orientation

    txt_path = raw_dir / f"page{page_no:03d}.txt"
    bbox_path = raw_dir / f"page{page_no:03d}.bbox.txt"
    seg_path = raw_dir / f"page{page_no:03d}.segments.txt"

    if seg_path.exists():
        fragments = []
        for row in seg_path.read_text(encoding="utf-8").splitlines():
            parts = row.split("|", 3)
            if len(parts) != 4 or not parts[1].strip():
                continue
            x0, y0, x1, y1 = (float(v) for v in parts[1].split(","))
            nx0, ny0, nx1, ny1 = orientation.rotate_bbox(x0, y0, x1, y1, rotation)
            fragments.append({"text": parts[3], "conf": float(parts[2] or 0),
                              "x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1})
        assembled = assemble_lines(fragments)
        txt_path.write_text("\n".join(l["text"] for l in assembled["lines"]),
                            encoding="utf-8")
        bbox_path.write_text("\n".join(
            "{lineno}|{geo}|{conf:.2f}|{text}".format(
                lineno=i, geo="{:.4f},{:.4f},{:.4f},{:.4f}".format(
                    l["x0"], l["y0"], l["x1"], l["y1"]),
                conf=l["conf"], text=l["text"])
            for i, l in enumerate(assembled["lines"], start=1)), encoding="utf-8")
        seg_path.write_text("\n".join(
            "{}|{:.4f},{:.4f},{:.4f},{:.4f}|{:.2f}|{}".format(
                lineno, f["x0"], f["y0"], f["x1"], f["y1"], f["conf"], f["text"])
            for lineno, group in enumerate(assembled["groups"], start=1)
            for f in (fragments[i] for i in group)), encoding="utf-8")
        return

    # Embedded pages: rotate the per-line boxes, keep the text lines.
    if bbox_path.exists():
        rows = []
        for row in bbox_path.read_text(encoding="utf-8").splitlines():
            parts = row.split("|", 3)
            if len(parts) != 4:
                continue
            geo = _rotate_geometry_row(parts[1].strip(), rotation) \
                if parts[1].strip() else ""
            rows.append(f"{parts[0]}|{geo}|{parts[2]}|{parts[3]}")
        bbox_path.write_text("\n".join(rows), encoding="utf-8")


def correct_orientation(raw_dir: Path, report: dict,
                        threshold: float | None = None) -> dict:
    """D64: rotate pages the local doc-ori classifier is confident about.

    Runs after the capture engine and BEFORE the quality gate. The render
    artifacts (OCR input / vision input / raw photo) and the text-channel
    geometry rotate together, so the VLM render, the OCR spans and the
    reviewer crops share one upright frame. Below the margin threshold the
    page keeps its frame (`unresolved`, D61 fallback). No-op when the
    classifier is unavailable (missing onnxruntime/Pillow) — the pipeline
    then behaves exactly as before.
    """
    from engines import orientation
    if not orientation.available():
        return report
    from PIL import Image
    if threshold is None:
        threshold = orientation.MARGIN_THRESHOLD
    for page in report.get("pages_report", []):
        page_no = page.get("page")
        if page.get("source") not in ("ocr", "embedded") or not page_no:
            continue
        if str(page.get("orientation") or "").startswith("rotated-"):
            continue  # the engine already pre-rotated this page (D64 path)
        render = raw_dir / f"page{page_no:03d}.render-ocr-input.png"
        if not render.exists():
            continue
        try:
            label, margin = orientation.classify(Image.open(render))
        except Exception as exc:  # a classifier hiccup must never fail the file
            logger.warning("page %d: doc-ori classifier failed: %s", page_no, exc)
            continue
        if margin < threshold:
            continue  # not decisive — keep the engine's frame (D61)
        if label == 0:
            page["orientation"] = "upright"
            page["orientation_confidence"] = f"doc-ori({margin:.2f})"
            continue
        for name in (f"page{page_no:03d}.render-ocr-input.png",
                     f"page{page_no:03d}.render-vision.png",
                     f"page{page_no:03d}.image.png"):
            path = raw_dir / name
            if path.exists():
                orientation.correct(Image.open(path), label).save(path)
        _rotate_text_channel(raw_dir, page_no, label)
        page["orientation"] = f"rotated-{label}"
        page["orientation_confidence"] = f"doc-ori({margin:.2f})"
        page["render_note"] = (f"orientation corrected: rotated-{label} "
                               f"(doc-ori classifier, margin {margin:.2f}, D64)")
        txt = raw_dir / f"page{page_no:03d}.txt"
        if txt.exists():
            lines = [l for l in txt.read_text(encoding="utf-8").splitlines()
                     if l.strip()]
            page["lines"] = len(lines)
            page["words"] = sum(len(l.split()) for l in lines)
    return report


def combined_text(out_dir: Path, exclude: set[int] | None = None) -> str:
    """Join pageNNN.txt files into one text with [PAGE N] markers.

    Marker placement follows the pipeline contract (D9): every page boundary is
    marked so downstream selection can address pages and source pointers can
    say "page N, line M". Line numbers in pageNNN.txt are 1-based and stable.

    `exclude` (D16 quality gate): 1-based page numbers dropped entirely from
    the combined text — the exclusions are still recorded in report.json and
    flagged, never silent. (combined.txt is a human-readable debug dump since
    2026-09-02 — the extraction pass reads renders, the validation stack reads
    per-page segments/bbox; nothing in the pipeline consumes it.)
    """
    exclude = exclude or set()
    blocks: list[str] = []
    for page_file in sorted(out_dir.glob("page[0-9][0-9][0-9].txt"),
                            key=lambda p: int(p.stem[4:])):
        page_no = int(page_file.stem[4:])
        if page_no in exclude:
            continue
        text = page_file.read_text(encoding="utf-8").rstrip("\n")
        blocks.append(f"[PAGE {page_no}]" + (f"\n{text}" if text else ""))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Stage 1b — document-quality gate (architecture.md D16)
# ---------------------------------------------------------------------------
# Verdicts: good | poor | blank. Cascade order (evaluated in code):
#   blank    — 0 OCR lines and the page is visually blank (pixel stats from the
#              Swift harness confirm it; a 0-line page with real image content
#              is "poor" — unreadable, not blank)
#   poor     — any automatic floor failed (lines/words/latin/text-area, or a
#              sustained band of garbage lines)
#   good     — everything else. Embedded-sourced pages are always good: D15's
#              routing gate already certified their quality (chars >= 100,
#              alpha/words floors, no full-page image) and they carry no OCR
#              bbox geometry for the text-area floor — re-gating would
#              re-litigate routing and FP on number-heavy embedded tables.
# Excluded pages are dropped from combined_text(), recorded in report.json
# (per-page quality + reason, quality_gate.excluded_pages), and the file carries
# a document_quality flag record for Stage 6 to lift into <run>/flags.csv
# (per-run flags — master-CSV decision amended 2026-08-09).
# Borderline pages (within review_band of a floor) are probe candidates: one
# flash-model call ("is this readable English invoice text? rate 0-1") tilts the
# verdict — only when quality.quality_probe is true (default false, D16).

LINE_CATS = ("clear", "noisy", "trash", "non-text")
LINE_BAND_SIZE = 5          # rolling window length for the garbage-line band
LINE_BAND_TRASH_FLOOR = 0.8  # worst 5-line window >80% trash lines -> poor
PROBE_READABLE = 0.6        # probe score >= this rescues a borderline poor page
PROBE_UNREADABLE = 0.4      # probe score <= this condemns a borderline good page

REASON_FLOORS = "below_quality_floors"
REASON_BLANK = "blank"


def _is_latin(ch: str) -> bool:
    return ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _script_ratio(text: str) -> float:
    """Latin share of the LETTERS (D62: digits are script-neutral).

    The old ratio divided by alphanumerics, so a perfectly readable numeric
    table row (digits only) scored 0.0 and was classified as trash — real
    numeric continuation sheets were dropped as "poor". Digits now neither
    help nor hurt; a text with no letters at all is neutral (1.0).
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if _is_latin(c)) / len(letters)


def line_category(line: str, min_latin: float) -> str:
    """Per-line quality bucket: clear | noisy | trash | non-text (D16).

    Buckets are script-purity based over the line's LETTERS (digits are
    neutral, D62): non-text = nothing alphanumeric (divider/stray-punctuation
    lines), trash = less than half of min_latin_ratio Latin
    (Cyrillic/gibberish-dominant), noisy = below min_latin_ratio, clear = at
    or above it.
    """
    if not [c for c in line.strip() if c.isalnum()]:
        return "non-text"
    ratio = _script_ratio(line)
    if ratio >= min_latin:
        return "clear"
    if ratio >= min_latin / 2:
        return "noisy"
    return "trash"


def page_line_quality(lines: list[str], min_latin: float) -> dict:
    """Per-line categories + rolling 5-line garbage band for one page.

    Returns {'counts': {cat: n}, 'worst_band_trash': 0-1}. The worst 5-line
    window's trash share is the page's sustained-garbage signal; a page where
    even its best window is mostly trash cannot be trusted, no matter the word
    count (e.g. a table of Cyrillic-contaminated rows).
    """
    cats = [line_category(l, min_latin) for l in lines]
    counts = {c: cats.count(c) for c in LINE_CATS}
    content = [c for c in cats if c != "non-text"]
    worst = 0.0
    if len(content) >= LINE_BAND_SIZE:
        for i in range(len(content) - LINE_BAND_SIZE + 1):
            band = content[i:i + LINE_BAND_SIZE]
            worst = max(worst, band.count("trash") / LINE_BAND_SIZE)
    return {"counts": counts, "worst_band_trash": worst}


def page_text_stats(text: str) -> dict:
    """Script-purity stats over the page text (D16 min_latin_ratio floor;
    D62: letters only — digits are neutral)."""
    return {"latin_ratio": _script_ratio(text)}


def text_area_ratio(bbox_path: Path) -> float:
    """Sum of line-bbox areas / page area (0-1, normalized Vision coords).

    Row format: lineno|x0,y0,x1,y1|confidence|text — geometry is empty for
    embedded lines, so this is meaningful for OCR pages only.
    """
    try:
        rows = bbox_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    total = 0.0
    for row in rows:
        parts = row.split("|", 3)
        if len(parts) < 2:
            continue
        geo = parts[1].strip()
        if not geo:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in geo.split(","))
        except ValueError:
            continue
        total += max(0.0, (x1 - x0) * (y1 - y0))
    return min(total, 1.0)


def _in_review_band(signal: float, floor: float, band: float) -> bool:
    """Within review_band x floor of a floor (either side) — probe candidate."""
    return floor > 0 and abs(signal - floor) <= band * floor


def page_verdict(
    page: dict,
    lines: list[str],
    bbox_path: Path,
    q: dict,
) -> dict:
    """One page through the D16 cascade.

    Returns {'quality', 'reason', 'detail', 'borderline'}; reason is one of
    REASON_* (machine-readable), detail is a human-readable string explaining
    the verdict. `borderline` marks pages inside the review_band of a floor —
    the probe hook (only consulted when quality_probe is enabled).
    """
    if page.get("source") == "embedded":
        # D15 already certified embedded pages (chars >= 100, alpha/words floors,
        # no full-page image); they have no OCR bbox geometry either, so D16
        # floors (tuned for OCR output) do not apply. Re-gating them here would
        # re-litigate routing and FP on number-heavy embedded tables.
        return {
            "quality": "good", "reason": None,
            "detail": "embedded text page (D15-certified); floors not applied",
            "borderline": False,
        }

    text = "\n".join(lines)
    words = len(text.split())
    stats = page_text_stats(text)
    area = text_area_ratio(bbox_path)
    line_q = page_line_quality(lines, q["min_latin_ratio"])
    image = page.get("image_stats") or {}
    has_image = bool(image)
    pixels_blank = (
        not has_image
        or (
            image.get("std_dev", 0.0) < q["max_pixel_std_dev"]
            and image.get("dark_ratio", 0.0) < q["max_dark_pixel_ratio"]
        )
    )

    if not lines:
        if pixels_blank:
            evidence = ("pixels confirm blank" if has_image else "no text, no image")
            return {
                "quality": "blank", "reason": REASON_BLANK,
                "detail": f"0 lines, {evidence}", "borderline": False,
            }
        return {
            "quality": "poor", "reason": REASON_FLOORS,
            "detail": "0 OCR lines from a non-blank image (unreadable scan)",
            "borderline": False,
        }

    failures = []
    if len(lines) < q["min_lines_per_page"]:
        failures.append(
            f"{len(lines)} lines < min_lines_per_page {q['min_lines_per_page']}"
        )
    if words < q["min_words_per_page"]:
        failures.append(f"{words} words < min_words_per_page {q['min_words_per_page']}")
    if stats["latin_ratio"] < q["min_latin_ratio"]:
        failures.append(
            f"latin_ratio {stats['latin_ratio']:.2f} < min_latin_ratio {q['min_latin_ratio']}"
        )
    if area < q["min_text_area_ratio"]:
        failures.append(
            f"text_area_ratio {area:.3f} < min_text_area_ratio {q['min_text_area_ratio']}"
        )
    if line_q["worst_band_trash"] > LINE_BAND_TRASH_FLOOR:
        failures.append(
            f"worst {LINE_BAND_SIZE}-line band {line_q['worst_band_trash']:.0%} trash lines"
        )

    band = q["review_band"]
    borderline = any((
        _in_review_band(len(lines), q["min_lines_per_page"], band),
        _in_review_band(words, q["min_words_per_page"], band),
        _in_review_band(stats["latin_ratio"], q["min_latin_ratio"], band),
        _in_review_band(area, q["min_text_area_ratio"], band),
    ))
    if failures:
        return {
            "quality": "poor", "reason": REASON_FLOORS,
            "detail": "; ".join(failures), "borderline": borderline,
        }
    return {"quality": "good", "reason": None, "detail": "passes all floors",
            "borderline": borderline}


def probe_page_text(text: str, model_name: str, api_key: str) -> float | None:
    """One bulk-model call rating page readability 0-1 (D16 probe, config-flagged).

    Goes through the shared llm_client (opencode Zen gateway, D4). Returns a
    clamped score or None on any failure (gateway error, unparseable answer) —
    a failed probe never blocks the batch.
    """
    if not text.strip():
        return None
    prompt = (
        "You are rating the quality of OCR'd text from a freight broker invoice "
        "page. Reply with exactly one number between 0 and 1: how readable is "
        "this as English invoice text? 0 = garbage (handwriting, gibberish, "
        "wrong script, random fragments); 1 = clean readable English invoice "
        "text.\n\nPAGE TEXT:\n" + text[:4000]
    )
    try:
        response = llm_client.chat(
            model_name, prompt, api_key=api_key, max_tokens=16, temperature=0.0
        )
        score = float(response.strip())
        return min(1.0, max(0.0, score))
    except Exception as exc:
        logger.warning("quality probe failed: %s", exc)
        return None


def _apply_probe_tilt(verdict: dict, score: float | None) -> dict:
    """Tilt a borderline verdict by the probe score (D16)."""
    if score is None:
        return verdict
    tilted = False
    if score >= PROBE_READABLE and verdict["quality"] == "poor":
        verdict["quality"] = "good"
        verdict["reason"] = None
        verdict["detail"] = f"{verdict['detail']}; probe rated {score:.2f} -> good"
        tilted = True
    elif score <= PROBE_UNREADABLE and verdict["quality"] == "good":
        verdict["quality"] = "poor"
        verdict["reason"] = REASON_FLOORS
        verdict["detail"] = f"probe rated {score:.2f} (unreadable); {verdict['detail']}"
        tilted = True
    verdict["probe"] = {"score": score, "tilted": tilted}
    return verdict


def apply_quality_gate(report: dict, out_dir: Path, cfg: dict) -> dict:
    """Post-process a harness report: per-page verdicts + file-level gate record.

    Enriches report.json in place: each page gains `quality`, `quality_reason`
    and `quality_signals`; the file gains `quality_gate` (excluded_pages with
    reasons, document_quality flag record, probe summary).
    """
    q = cfg["thresholds"]["quality"]
    pages = report.get("pages_report", [])
    # D29: document profiles carry no per-page ignore rules — the universal
    # floors are the mechanism (ignore_pages died with brokers.yaml).

    excluded: list[dict] = []
    probe_consulted = 0
    probe_model = cfg["models"]["primary_model"]
    api_key = config.opencode_api_key() if q["quality_probe"] else None

    for page in pages:
        page_no = page["page"]
        txt_path = out_dir / f"page{page_no:03d}.txt"
        bbox_path = out_dir / f"page{page_no:03d}.bbox.txt"
        lines = []
        if txt_path.exists():
            lines = [l for l in txt_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        verdict = page_verdict(page, lines, bbox_path, q)

        if q["quality_probe"] and verdict["borderline"] and api_key:
            verdict = _apply_probe_tilt(
                verdict, probe_page_text("\n".join(lines), probe_model, api_key)
            )
            probe_consulted += 1
        elif q["quality_probe"] and verdict["borderline"]:
            logger.warning(
                "page %d borderline but OPENCODE_API_KEY not set — probe skipped (config quality_probe is on)",
                page_no,
            )
            verdict["probe"] = {"score": None, "tilted": False}
        else:
            verdict["probe"] = {"score": None, "tilted": False}

        page["quality"] = verdict["quality"]
        page["quality_reason"] = verdict["detail"]
        line_q = page_line_quality(lines, q["min_latin_ratio"])
        page["quality_signals"] = {
            "lines": len(lines),
            "words": len("\n".join(lines).split()),
            "latin_ratio": page_text_stats("\n".join(lines))["latin_ratio"],
            "text_area_ratio": text_area_ratio(bbox_path),
            "line_categories": line_q["counts"],
            "worst_band_trash": line_q["worst_band_trash"],
        }
        if verdict["quality"] in ("poor", "blank"):
            excluded.append({
                "page": page_no,
                "quality": verdict["quality"],
                "reason": verdict["reason"],
                "detail": verdict["detail"],
            })

    gate = {
        "broker": None,  # D29: no broker registry — selection is vision-led
        "excluded_pages": excluded,
        "document_quality": None,
        "probe": {"enabled": q["quality_probe"], "consulted": probe_consulted},
    }
    if excluded:
        reasons = ", ".join(sorted({e["reason"] for e in excluded}))
        pages_s = sorted(e["page"] for e in excluded)
        gate["document_quality"] = {
            "field": "document_quality",
            "value": "illegible/blank pages excluded",
            "reason": f"{len(excluded)} pages excluded ({reasons}): {pages_s}",
            "pages": pages_s,
        }
    report["quality_gate"] = gate
    return report


def run_batch(in_dir: Path, out_dir: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S,
              engine: str | None = None,
              ocr_only: bool = False,
              dump_embedded: bool = False,
              vision_cap: int = 0,
              files: set[str] | None = None) -> int:
    """Process every *.pdf in in_dir into out_dir/<stem>/, isolating failures.

    After each file's harness report lands, the D16 quality gate post-processes
    it (per-page verdicts, exclusions, document_quality flag), the enriched
    report.json is written back, and combined.txt (human-readable debug dump —
    nothing in the pipeline reads it since 2026-09-02) drops excluded pages.

    Vision inputs (D24): the harness writes pageNNN.render-ocr-input.png (the
    image the OCR harness reads) and pageNNN.image.png (the raw embedded photo,
    D21) for every page, unconditionally — the primary extraction pass reads
    them. The text channel feeds the verification stack (grounding).
    """
    cfg = config.load_all()
    resolved_engine = engines.resolve_engine(engine)
    logger.info("capture engine: %s", resolved_engine)
    pdfs = sorted(in_dir.glob("*.pdf"))
    if files is not None:
        pdfs = [p for p in pdfs if p.stem in files]
    if not pdfs:
        logger.info("no PDFs in %s (filtered %r)", in_dir, files)
        return 0
    ok = failed = 0
    reports: list[dict] = []
    for pdf in pdfs:
        dest = out_dir / pdf.stem
        try:
            report = ocr_pdf(pdf, dest, engine=resolved_engine, timeout_s=timeout_s,
                             ocr_only=ocr_only,
                             dump_embedded=dump_embedded,
                             vision_cap=vision_cap)
            report = correct_orientation(dest, report)  # D64 (before the gate)
            report = apply_quality_gate(report, dest, cfg)
            dest.joinpath("report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            excluded = {e["page"] for e in report.get("quality_gate", {}).get("excluded_pages", [])}
            combined = combined_text(dest, exclude=excluded)
            dest.joinpath("combined.txt").write_text(combined + "\n", encoding="utf-8")
            pages = report.get("pages_report", [])
            sources = {p.get("source") for p in pages}
            bad = [e["page"] for e in report.get("quality_gate", {}).get("excluded_pages", [])]
            logger.info(
                "%s: %d pages %s (ocr %.0f ms), excluded %s",
                pdf.name, report.get("pages", 0), sorted(sources),
                report.get("total_ocr_ms", 0), bad or "[]",
            )
            reports.append(report)
            ok += 1
        except (OSError, RuntimeError) as exc:
            logger.error("%s: %s", pdf.name, exc)
            failed += 1
    logger.info("batch done: %d ok, %d failed", ok, failed)
    return 0 if failed == 0 else 1


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="text_layer.py",
        description="Stage 1: per-file text acquisition (embedded routing + OCR) "
                    "with the D16 document-quality gate.",
    )
    parser.add_argument("in_dir", nargs="?", help="directory of PDFs to process")
    parser.add_argument("out_dir", nargs="?", help="output directory (per-file subdirs created)")
    parser.add_argument("--engine", default="auto",
                        choices=("auto",) + engines.ENGINES,
                        help="capture engine (D63): auto prefers Apple Vision on "
                             "macOS, else the cross-platform engine; "
                             f"VERDANDI_OCR_ENGINE overrides the auto default")
    parser.add_argument("--custom-words", action="store_true",
                        help="feed customWords from the document profiles' keywords "
                             "(rules/documents.yaml) into OCR")
    parser.add_argument("--ocr-only", action="store_true",
                        help="force OCR on every page (skip embedded-text routing)")
    parser.add_argument("--dump-embedded", action="store_true",
                        help="write pageNNN.embedded.txt = PDFKit text-layer extraction for every page")
    parser.add_argument("--files", default=None, metavar="STEM1,STEM2",
                        help="process ONLY these inbox file stems (2026-09-05: the "
                             "UI scopes a run to the just-uploaded files instead of "
                             "re-capturing accumulated inbox/)")
    parser.add_argument("--vision-cap", type=int, default=0, metavar="PIXELS",
                        help="D30 cost A/B: also write pageNNN.render-vision.png — the "
                             "page render downscaled to this long edge, for the vision "
                             "pass ONLY (OCR/grounding keep the full-res render). "
                             "0 = off (legacy behavior)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"per-file timeout in seconds (default {DEFAULT_TIMEOUT_S})")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.in_dir or not args.out_dir:
        parse_args(["--help"])
        return 64
    custom_words = custom_words_from_profiles() if args.custom_words else None
    if args.custom_words:
        logger.info("custom words from document profiles: %d", len(custom_words))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files_filter = None
    if args.files:
        files_filter = {s.strip() for s in args.files.split(",") if s.strip()}
    code = run_batch(Path(args.in_dir), out_dir, timeout_s=args.timeout,
                     engine=args.engine,
                     ocr_only=args.ocr_only,
                     dump_embedded=args.dump_embedded,
                     vision_cap=args.vision_cap,
                     files=files_filter)
    if custom_words:
        out_dir.joinpath("custom_words.json").write_text(
            json.dumps(custom_words, indent=1), encoding="utf-8"
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
