"""Review evidence + flag enrichment for the desktop app (D66 Phase 1).

Read-only over a run directory. Reuses the Streamlit UI's pure helpers
(`ui.lib` — flag keys, review state, field lookup) so review semantics can
never diverge between the two interfaces. Crops come from the captured page
renders; if the capture did not run (or a page render is missing) the flag is
returned without evidence instead of failing.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import messages

_PAGE_CANDIDATES = (
    "page{page:03d}.render-vision.png",
    "page{page:03d}.render-ocr-input.png",
    "page{page:03d}.image.png",
)


def _ui_lib():
    from ui import lib as ui_lib  # noqa: PLC0415 — engine path set by the entry

    return ui_lib


def load_flags_enriched(run_dir: Path) -> list[dict]:
    """flags.csv rows + review state + located evidence (D36 semantics)."""
    lib = _ui_lib()
    rows = lib.load_flags(run_dir)
    review = lib.load_review(run_dir)
    docs: dict[str, dict | None] = {}
    out: list[dict] = []
    for row in rows:
        enriched = dict(row)
        enriched["review"] = lib.review_state(review, row) or None
        # Human wording travels with the row (messages.py is the one source).
        code = row.get("reason")
        msg = messages.reason_message(
            code, value=row.get("value"),
            field=messages.pretty_name(row.get("field")),
            template=messages.pretty_name(row.get("template")),
            rule=(messages.pretty_rule(row["rule"]) if row.get("rule") else None))
        if code == "cross_document_mismatch":
            # Flag-level help is generic; the panel above shows both sides.
            msg = {**msg,
                   "body": "This value disagrees with the same field on "
                           "another document in the file — compare both sides "
                           "in the cross-check panel."}
        enriched["reason_label"] = msg["title"]
        enriched["reason_help"] = msg["body"]
        enriched["reason_hint"] = msg["hint"]
        evidence = _evidence_for(run_dir, row, docs)
        if evidence:
            enriched["evidence"] = evidence
        out.append(enriched)
    return out


def _doc(run_dir: Path, stem: str, cache: dict) -> dict | None:
    if stem not in cache:
        try:
            cache[stem] = json.loads(
                (run_dir / "json" / f"{stem}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache[stem] = None
    return cache[stem]


def _entry(data: dict, row: dict) -> dict:
    """The documents[] entry a flagged field lives in (D57), else the doc."""
    entries = data.get("documents") or []
    idx = _ui_lib().entry_index(row.get("entry"))
    if entries and idx is not None and 0 <= idx < len(entries):
        return entries[idx]
    template = row.get("template")
    if entries and template:
        for e in entries:
            if e.get("template_id") == template:
                return e
    return data


def _field_record(data: dict, row: dict) -> dict | None:
    field = (row.get("field") or "").strip()
    if not field:
        return None
    entry = _entry(data, row)
    if field.startswith("line_item_"):
        try:
            idx = int(field.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            return None
        items = entry.get("line_items") or []
        return items[idx] if 0 <= idx < len(items) else None
    for rec in entry.get("header_fields") or []:
        if rec.get("name") == field:
            return rec
    return None


def _evidence_for(run_dir: Path, row: dict, cache: dict) -> dict | None:
    data = _doc(run_dir, str(row.get("file") or ""), cache)
    if not data:
        return None
    rec = _field_record(data, row)
    if not rec:
        return None
    verification = rec.get("verification") or {}
    match = None
    for check in verification.get("checks") or []:
        if check.get("check") == "grounding" and check.get("match"):
            match = check["match"]
    region = rec.get("source_region") or {}
    page = (match or {}).get("page") or region.get("page")
    bbox = (match or {}).get("line_bbox") or region.get("bbox")
    if not page:
        return None
    return {
        "page": int(page),
        "bbox": bbox,
        "text": (match or {}).get("text"),
        "claimed_bbox": region.get("bbox"),
        "claimed_page": region.get("page"),
        "value": rec.get("value"),
    }


def page_image_path(run_dir: Path, stem: str, page: int) -> Path | None:
    for pattern in _PAGE_CANDIDATES:
        candidate = run_dir / "raw" / stem / pattern.format(page=page)
        if candidate.exists():
            return candidate
    return None


def _render(run_dir: Path, stem: str, page: int, box: tuple | None,
            max_width: int) -> bytes | None:
    path = page_image_path(run_dir, stem, page)
    if path is None:
        return None
    from PIL import Image  # noqa: PLC0415 — optional capture dependency

    image = Image.open(path).convert("RGB")
    width, height = image.size
    if box is not None:
        x0, y0, x1, y1 = box
        pad = 0.03
        x0 = max(0.0, min(x0, x1) - pad)
        y0 = max(0.0, min(y0, y1) - pad)
        x1 = min(1.0, max(x0, x1) + pad)
        y1 = min(1.0, max(y0, y1) + pad)
        image = image.crop((int(x0 * width), int(y0 * height),
                            int(x1 * width), int(y1 * height)))
    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, max(1, int(image.height * ratio))))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def crop_png(run_dir: Path, stem: str, page: int,
             bbox: tuple | None, max_width: int = 1400) -> bytes | None:
    """Evidence crop (whole page when bbox is None) as PNG bytes."""
    return _render(run_dir, stem, page, bbox, max_width)


def flag_counts(run_dir: Path) -> dict:
    """Light flag/review counters + per-reason counts for the run metrics."""
    lib = _ui_lib()
    rows = lib.load_flags(run_dir)
    review = lib.load_review(run_dir)
    pending = confirmed = issues = 0
    reasons: dict[str, int] = {}
    for row in rows:
        state = lib.review_state(review, row)
        status = state.get("status")
        if status == "confirmed":
            confirmed += 1
        elif status == "issue":
            issues += 1
        else:
            pending += 1
        code = row.get("reason") or "flag"
        reasons[code] = reasons.get(code, 0) + 1
    return {"flags": len(rows), "pending": pending,
            "confirmed": confirmed, "issues": issues, "reasons": reasons}


def recompute_flags(run_dir: Path) -> int:
    """Rebuild flags.csv from the stored JSONs (local, free) — D39 freshness."""
    return _ui_lib().recompute_flags(Path(run_dir) / "raw",
                                     Path(run_dir) / "json")


def apply_all_reviews(run_dir: Path) -> dict:
    """Re-apply every review.json entry to the run's JSONs (after retries)."""
    lib = _ui_lib()
    review = lib.load_review(run_dir)
    applied = 0
    stems = sorted(p.stem for p in (Path(run_dir) / "json").glob("*.json"))
    for stem in stems:
        applied += lib.apply_reviews(Path(run_dir), stem, review)
    return {"stems": len(stems), "entries": applied}


def apply_review(run_dir: Path, row: dict, status: str, note: str = "",
                 value: str | None = None) -> dict:
    """Write a review outcome (review.json + the run's JSON) — D39 semantics.

    Exactly the Streamlit flow: update review.json under the D57 key, then
    mirror it into the final JSON via ui.lib.apply_review.
    """
    lib = _ui_lib()
    if status not in ("confirmed", "issue"):
        raise ValueError("status must be 'confirmed' or 'issue'")
    key = lib.flag_key(row)
    review = lib.load_review(run_dir)
    entry: dict = {"status": status, "note": note}
    if status == "confirmed" and value not in (None, ""):
        entry["value"] = value
    review[key] = entry
    lib.save_review(run_dir, review)
    message = lib.apply_review(run_dir, row, status, note, value=value)
    return {"message": message, "key": key, "entry": entry}
