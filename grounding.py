"""Check 1 — Grounding: the claimed value exists in the OCR text near the
region the vision model says it read it from (2026-09-02, verification stack).

An EXISTENCE check, not an extraction: no label matching, no value
re-derivation. Region pull over the raw text channel (`segments.txt`
fragment geometry on OCR pages; real per-line PDFKit geometry on embedded
pages since 2026-09-02), joined in reading order, then STRICT containment
(containment.exact_containment — normalized-token equality per value kind).

Coordinate conventions (verified against live data 2026-09-02):
  - model source_region bbox: normalized 0-1, PAGE-relative, TOP-LEFT origin
    (models read the render pixels); a claimed reference box sits ~0.008
    above the OCR line's flipped box (measured).
  - OCR span bboxes (segments/bbox): normalized 0-1, BOTTOM-LEFT origin
    (Apple Vision). flip: y_tl = 1 - y_bl, y_bl = 1 - y_tl.
  - margin is relative (grounding_margin_rel, default 0.035) and covers the
    model-vs-OCR box offset.

Failure semantics (deliberate): a missing source_region SKIPS the check
(excluded from aggregation, logged — not a failure). A value the text
channel cannot confirm as a normalized token FAILS with an informational
n-gram proximity in the detail — the flag truthfully says "could not
confirm", review decides.

Two location outputs (2026-09-22, D36) — the boxes the reviewer sees:
  - a passing check carries `match` (the span-level fragment + line bbox that
    confirmed the value) into the per-field audit;
  - `locate_value` answers the display question page-wide for EVERY field
    (even when grounding fails/skips): exact span matches and partial
    line-level matches, ranked by label hit / proximity to the model's claim /
    context. Display-only — it never feeds flags, scores or confidence.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import containment
from compare import norm_key

CHECK_NAME = "grounding"

# locate_value: partial candidates need at least this much of the value's
# character n-grams in the line (display-only heuristic; OCR truncation and
# spaced digits are normal on scans).
PARTIAL_MIN_RATIO = 0.5
PARTIAL_MIN_TOKEN_LEN = 5

# Kinds used for normalized-token equality per field (mirror the profile
# types: str/date/amount -> code/date/amount comparators).
KIND_BY_TYPE = {"amount": "amount", "date": "date", "currency": "code",
                "str": "code", "name": "str", "object": "str"}


def _parse_spans_text(text: str) -> tuple[dict, ...]:
    """Parse a geometry file (`lineno|geometry|confidence|text`) into span dicts
    (normalized, bottom-left origin). Same row format for segments and bbox."""
    spans: list[dict] = []
    for line in text.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4 or not parts[1].strip():
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in parts[1].strip().split(","))
            lineno = int(parts[0])
        except ValueError:
            continue
        spans.append({"text": parts[3], "x0": x0, "y0": y0,
                      "x1": x1, "y1": y1, "lineno": lineno})
    return tuple(spans)


@lru_cache(maxsize=512)
def _spans_cached(page_dir_str: str, page: int, stamp: float) -> tuple[dict, ...]:
    """Cached span load keyed by (page dir, page, geometry-file mtime).

    Capture files are immutable after a run; the mtime stamp invalidates the
    cache if a page is ever re-captured in place. UI reruns hit this cache, so
    the locate search over every page is cheap.
    """
    page_dir = Path(page_dir_str)
    segments = page_dir / f"page{page:03d}.segments.txt"
    if segments.exists():
        return _parse_spans_text(segments.read_text(encoding="utf-8"))
    bbox = page_dir / f"page{page:03d}.bbox.txt"
    if bbox.exists():
        return _parse_spans_text(bbox.read_text(encoding="utf-8"))
    return ()


def _load_spans(raw_dir: Path, stem: str, page: int) -> list[dict]:
    """OCR-level spans for one page: segments.txt (fragment bboxes preferred),
    else pageNNN.bbox.txt merged lines. Normalized, bottom-left origin."""
    page_dir = raw_dir / stem
    stamp = 0.0
    for name in (f"page{page:03d}.segments.txt", f"page{page:03d}.bbox.txt"):
        p = page_dir / name
        if p.exists():
            try:
                stamp = max(stamp, p.stat().st_mtime)
            except OSError:
                pass
    return list(_spans_cached(str(page_dir), page, stamp))


def _region_spans(spans: list[dict], bbox: list[float], margin: float) -> list[dict]:
    """Spans whose box intersects the expanded region (model coords, tl origin)."""
    if len(bbox) != 4:
        return []
    x0, y0_tl, x1, y1_tl = (float(v) for v in bbox)
    # Flip the model's top-left y to the bottom-left convention of the spans.
    y0, y1 = 1.0 - y1_tl, 1.0 - y0_tl
    rx0, ry0 = x0 - margin, y0 - margin
    rx1, ry1 = x1 + margin, y1 + margin
    out = []
    for s in spans:
        if (s["x1"] >= rx0 and s["x0"] <= rx1
                and s["y1"] >= ry0 and s["y0"] <= ry1):
            out.append(s)
    return out


def _reading_order(spans: list[dict]) -> str:
    by_line: dict[int, list[str]] = {}
    for s in spans:
        by_line.setdefault(s["lineno"], []).append(s)
    pieces = []
    for lineno in sorted(by_line):
        for s in sorted(by_line[lineno], key=lambda s: s["x0"]):
            pieces.append(s["text"])
    return " ".join(pieces)


def _tl(bbox_bl) -> list[float]:
    """Bottom-left span bbox -> top-left render bbox (the UI convention)."""
    x0, y0, x1, y1 = bbox_bl
    return [x0, 1.0 - y1, x1, 1.0 - y0]


def _line_info(spans: list[dict], lineno: int) -> tuple[str, list[float]]:
    """(text, bottom-left bbox) of one line = union of its spans, x-sorted."""
    ss = [s for s in spans if s["lineno"] == lineno]
    x0 = min(s["x0"] for s in ss)
    x1 = max(s["x1"] for s in ss)
    y0 = min(s["y0"] for s in ss)
    y1 = max(s["y1"] for s in ss)
    text = " ".join(s["text"] for s in sorted(ss, key=lambda s: s["x0"]))
    return text, [x0, y0, x1, y1]


def _value_offset(value, text: str) -> float:
    """Approximate x fraction (0-1) of the value inside the text.

    Wide OCR fragments are line-sized (several label/value pairs come back as
    ONE span), so the crop window needs to centre on the value's character
    position within the fragment.
    """
    v = "".join(ch.lower() for ch in str(value) if ch.isalnum())
    t = "".join(ch.lower() for ch in text if ch.isalnum())
    if not v or not t:
        return 0.5
    idx = t.find(v)
    if idx < 0:
        token = max(str(value).split(), key=len, default="")
        tok = "".join(ch.lower() for ch in token if ch.isalnum())
        idx = t.find(tok) if tok else -1
    if idx < 0:
        return 0.5
    return min(1.0, max(0.0, (idx + len(v) / 2) / len(t)))


def _page_numbers(raw_dir: Path, stem: str) -> list[int]:
    """Pages with text-channel geometry under raw_dir/stem."""
    page_dir = raw_dir / stem
    pages: set[int] = set()
    for pattern in ("page[0-9][0-9][0-9].txt", "page[0-9][0-9][0-9].segments.txt",
                    "page[0-9][0-9][0-9].bbox.txt"):
        for p in page_dir.glob(pattern):
            try:
                pages.add(int(p.name[4:7]))
            except ValueError:
                continue
    return sorted(pages)


def locate_value(raw_dir: Path, stem: str, value, kind: str = "code", *,
                 claim: dict | None = None, labels: list[str] | None = None,
                 pages: list[int] | None = None) -> list[dict]:
    """DISPLAY-ONLY: where is `value` actually printed in the text channel?

    Returns candidates ranked best-first; each:
      {page, level: "exact"|"partial", fragment_bbox, line_bbox, text, lineno,
       score, value_offset}
    `fragment_bbox`/`line_bbox` are normalized TOP-LEFT (render coordinates);
    the text channel stores bottom-left, flipped here for the UI.

    This never feeds verification (no flags, no confidence): it answers the
    reviewer's question "where on the paper is it?" — including when the
    model's `source_region` is wrong (measured on the corpus: median 6% page
    offset, wrong-label/row/page cases). `exact` = a span token equals the
    value (`containment.exact_containment`); `partial` = the line carries
    enough of the value's n-grams or a long value token (OCR-spaced digits,
    truncated multi-token values). Ranking: exact > partial, then a line
    containing one of the field's profile `labels`, then proximity to the
    model's claimed region, then the longest line (most context).
    """
    if value in (None, ""):
        return []
    claim = claim or {}
    claim_page = int(claim.get("page") or 0)
    claim_bbox = claim.get("bbox") if isinstance(claim.get("bbox"), list) else None
    page_list = sorted({int(p) for p in pages}) if pages else _page_numbers(raw_dir, stem)
    candidates: list[dict] = []
    for page in page_list:
        spans = _load_spans(raw_dir, stem, page)
        if not spans:
            continue
        by_line: dict[int, list[dict]] = {}
        for s in spans:
            by_line.setdefault(s["lineno"], []).append(s)
        exact = [s for s in spans
                 if containment.exact_containment(str(value), s["text"], kind)]
        if exact:
            for s in exact:
                text, line_bl = _line_info(spans, s["lineno"])
                candidates.append({
                    "page": page, "level": "exact",
                    "fragment_bbox": _tl([s["x0"], s["y0"], s["x1"], s["y1"]]),
                    "line_bbox": _tl(line_bl), "text": text, "lineno": s["lineno"],
                    "score": 1.0, "value_offset": _value_offset(value, s["text"]),
                })
            continue
        for lineno, ss in by_line.items():
            text, line_bl = _line_info(spans, lineno)
            ratio = containment.ngram_ratio(str(value), text, 3)
            token_hit = any(
                len(w) >= PARTIAL_MIN_TOKEN_LEN and norm_key(w)
                and norm_key(w) in norm_key(text)
                for w in str(value).split())
            if ratio < PARTIAL_MIN_RATIO and not token_hit:
                continue
            best = max(ss, key=lambda s: containment.ngram_ratio(str(value), s["text"], 3))
            candidates.append({
                "page": page, "level": "partial",
                "fragment_bbox": _tl([best["x0"], best["y0"], best["x1"], best["y1"]]),
                "line_bbox": _tl(line_bl), "text": text, "lineno": lineno,
                "score": round(ratio, 3), "value_offset": _value_offset(value, text),
            })
    if not candidates:
        return []
    label_keys = [norm_key(str(l)) for l in (labels or []) if str(l).strip()]

    def _label_hit(c: dict) -> bool:
        return any(k and k in norm_key(c["text"]) for k in label_keys)

    def _claim_dist(c: dict) -> float:
        if not claim_bbox or claim_page != c["page"]:
            return float("inf")
        cx = (c["fragment_bbox"][0] + c["fragment_bbox"][2]) / 2
        cy = (c["fragment_bbox"][1] + c["fragment_bbox"][3]) / 2
        qx = (claim_bbox[0] + claim_bbox[2]) / 2
        qy = (claim_bbox[1] + claim_bbox[3]) / 2
        return ((cx - qx) ** 2 + (cy - qy) ** 2) ** 0.5

    def rank(c: dict):
        d = _claim_dist(c)
        # Page preference: a STRONG candidate on the page the model claimed is
        # where the model actually read it (e.g. the waybill's spaced number
        # 'WAYBILL 45 7313 6702' on p10) — prefer it over an exact occurrence
        # on another page (the BOE on p13 merely REFERENCES the same number).
        # When the claimed page has nothing (a hallucinated region), the best
        # candidate anywhere wins (the boe_date p13 case).
        on_claim = bool(claim_page) and c["page"] == claim_page
        strong = c["level"] == "exact" or c["score"] >= 0.7
        return (0 if (on_claim and strong) else 1,
                0 if c["level"] == "exact" else 1,
                0 if _label_hit(c) else 1,
                0 if d <= 0.12 else 1,
                d if d != float("inf") else 9.0,
                -(c["line_bbox"][2] - c["line_bbox"][0]))

    return sorted(candidates, key=rank)


def _row_spans(spans: list[dict], bbox: list[float], margin: float) -> list[dict]:
    """Spans in the region's ROW band: same y range, ANY x.

    Measured on the corpus (2026-09-02): every imprecise vision region is
    correct in y but off in x (deepseek/qwen anchored boxes 0.2-0.5 off
    horizontally). The loose search band therefore extends the ROW, not the
    box — column-agnostic, row-honest.
    """
    x0, y0_tl, x1, y1_tl = (float(v) for v in bbox[:4])
    y0, y1 = 1.0 - y1_tl, 1.0 - y0_tl
    ry0, ry1 = y0 - margin, y1 + margin
    return [s for s in spans if s["y1"] >= ry0 and s["y0"] <= ry1]


def check_grounding(stem: str, page: int, value, bbox, raw_dir: Path,
                    margin: float = 0.035, search_margin: float = 0.12,
                    loose_score: float = 0.6, ngram_k: int = 3,
                    kind: str = "code") -> dict | None:
    """Grounding check for one field value. None = skipped (no region).

    Two search bands (measured on the corpus 2026-09-02): a TIGHT band
    (margin, ~0.035) is a strong confirmation (score 1.0); a LOOSE band
    (search_margin, config 0.12) absorbs the region imprecision of some vision
    models (deepseek's regions ran ~2-3% off) and scores `loose_score`
    (config 0.6: value exists in the general locality — weaker, still a pass).
    Nothing found at either band (or region text that cannot confirm the
    value) -> fail (score 0.0).

    Returns a check-result dict: {check, passed, score, detail}.
    """
    if not bbox or value is None:
        return None
    spans = _load_spans(raw_dir, stem, page)
    # Band 1: tight box (strong confirmation, 1.0). Band 2: the region's ROW
    # (y-band, any x — absorbs the measured x-offset of imprecise regions,
    # loose_score = weak confirmation; measured tradeoff in eval-verify:
    #   loose_score 0.7 -> 4 fewer review flags, but the synthetic field-swap
    #   case slips through (both cells pass at 0.7 >= accept after aggregation)
    #   loose_score 0.6 -> swap caught (flagged), x-offset correct values
    #   re-flagged at review_secondary. Owner picks per verification posture).
    candidates = {
        (margin, 1.0): [_region_spans(spans, bbox, margin)],
        (search_margin, loose_score): [_region_spans(spans, bbox, search_margin),
                                       _row_spans(spans, bbox, search_margin)],
    }
    for (band, score), region_sets in candidates.items():
        for region in region_sets:
            if not region:
                continue
            region_text = _reading_order(region)
            if containment.exact_containment(str(value), region_text, kind):
                label = "at region" if band == margin else "near region (loose/row band)"
                excerpt = " ".join(region_text.split()[:8])
                return {
                    "check": CHECK_NAME, "passed": True, "score": score,
                    "detail": f"value found {label} (page {page}): '{excerpt}'",
                    "match": _match_record(spans, region, value, kind, page,
                                           "tight" if band == margin else "loose"),
                }
    # Failure detail: reuse the tight band's text for evidence.
    region = _region_spans(spans, bbox, margin)
    region_text = _reading_order(region) if region else ""
    proximity = containment.ngram_ratio(str(value), region_text, ngram_k) if region_text else 0.0
    excerpt = " ".join(region_text.split()[:8]) or "no text found near the claimed region"
    return {
        "check": CHECK_NAME, "passed": False, "score": 0.0,
        "detail": (f"value '{value}' NOT found near region (page {page}); "
                   f"nearest text '{excerpt}' (n-gram proximity {proximity:.2f})"),
        "match": None,
    }


def _match_record(spans: list[dict], region: list[dict], value, kind: str,
                  page: int, band: str) -> dict | None:
    """The span-level location that confirmed `value` (audit + UI evidence).

    Returns {page, fragment_bbox, line_bbox, text, band} with TOP-LEFT
    normalized bboxes, or None when no single span carries the token (the
    value matched only in the joined region text — shouldn't happen for
    single-token values).
    """
    matched = next((s for s in region
                    if containment.exact_containment(str(value), s["text"], kind)), None)
    if matched is None:
        return None
    text, line_bl = _line_info(spans, matched["lineno"])
    return {
        "page": page,
        "fragment_bbox": _tl([matched["x0"], matched["y0"], matched["x1"], matched["y1"]]),
        "line_bbox": _tl(line_bl),
        "text": text,
        "lineno": matched["lineno"],
        "band": band,
    }