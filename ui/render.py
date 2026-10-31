"""Evidence rendering — Pillow crops + the extraction field-overlay (Streamlit UI).

The reviewer's two questions: "where is this value on the paper?" and "is the
model's claimed region right?" (D36, 2026-09-22).

- Location: `evidence_view` crops the text-channel location when one exists
  (`grounding.locate_value` / the persisted grounding `match`), with a
  readable context window instead of a tight box; the model's `source_region`
  is the fallback (labeled unconfirmed) and a muted outline when it differs.
- Overlay: rectangles keep the confidence color code (green = at/above the
  config threshold, amber = near, red = capped, grey = no region); the label
  says whether the box is the text-confirmed location, a partial text match,
  or the model's unconfirmed claim.

Crops use the full-resolution page render (`pageNNN.render-ocr-input.png`,
falling back to `image.png`); the model reads the capped `render-vision.png`,
a pure downscale, so normalized coordinates map 1:1.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CLAIM_COLOR = (150, 150, 150)   # muted grey — the model's unconfirmed claim
CLAIM_DIFF_MIN = 0.025          # page fraction: when the claim differs this much, draw both

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def page_image(raw_dir: Path, stem: str, page: int) -> Image.Image | None:
    """Page render for evidence views (vision input order, D21/D24)."""
    for name in (
        f"page{page:03d}.render-ocr-input.png",
        f"page{page:03d}.image.png",
        f"page{page:03d}.render-images-only.png",
    ):
        p = raw_dir / stem / name
        if p.exists():
            try:
                return Image.open(p).convert("RGB")
            except OSError:
                continue
    return None


def threshold_color(confidence: float | None, threshold: float | None) -> tuple[int, int, int]:
    if confidence is None:
        return (120, 120, 120)
    if threshold is not None and confidence >= threshold:
        return (34, 160, 60)
    if confidence <= 0.3:  # D8 confidence cap (validation)
        return (200, 40, 40)
    return (230, 160, 30)


def _px_rect(rect: list[float], size: tuple[int, int]) -> list[float]:
    w, h = size
    return [rect[0] * w, rect[1] * h, rect[2] * w, rect[3] * h]


def _claim_differs(claim_bbox: list[float], located_bbox: list[float]) -> bool:
    """True when the model's claim and the located box are materially apart."""
    if not (isinstance(claim_bbox, list) and len(claim_bbox) == 4
            and isinstance(located_bbox, list) and len(located_bbox) == 4):
        return False
    cx1 = (claim_bbox[0] + claim_bbox[2]) / 2
    cy1 = (claim_bbox[1] + claim_bbox[3]) / 2
    cx2 = (located_bbox[0] + located_bbox[2]) / 2
    cy2 = (located_bbox[1] + located_bbox[3]) / 2
    return abs(cx1 - cx2) > CLAIM_DIFF_MIN or abs(cy1 - cy2) > CLAIM_DIFF_MIN


def draw_overlay(img: Image.Image, fields: list[dict], thresholds: dict, page: int,
                 ) -> tuple[Image.Image, list[str]]:
    """Draw every field anchored to `page` as a labeled rectangle.

    A field's `_location` (set by the UI from `lib.locate_field`) wins over its
    raw `source_region`: the text-confirmed/partial box is drawn solid (label
    ✓text / ~text), and the model's claim is drawn as a muted outline when it
    differs. Without a location the claim is drawn as before (label 'claim').

    Returns (image copy, fields without any anchor on this page).
    """
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    font, small = _font(15), _font(12)
    missing: list[str] = []
    for field in fields:
        region = field.get("source_region") if isinstance(field.get("source_region"), dict) else None
        location = field.get("_location") if isinstance(field.get("_location"), dict) else None
        claim_on_page = bool(region and region.get("page") == page
                             and isinstance(region.get("bbox"), list))
        loc_on_page = bool(location and location.get("page") == page
                           and isinstance(location.get("fragment_bbox"), list))
        if not claim_on_page and not loc_on_page:
            missing.append(field.get("name", "?"))
            continue
        w, h = canvas.size
        color = threshold_color(field.get("confidence"), thresholds.get(field.get("name")))
        label = f"{field.get('name')}: {field.get('value')}"
        if loc_on_page:
            frag = location["fragment_bbox"]
            x0, y0, x1, y1 = _px_rect(frag, (w, h))
            draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
            tag = "✓text" if location.get("level") == "exact" else "~text"
            if claim_on_page and _claim_differs(region["bbox"], frag):
                cx0, cy0, cx1, cy1 = _px_rect(region["bbox"], (w, h))
                draw.rectangle([cx0, cy0, cx1, cy1], outline=CLAIM_COLOR, width=2)
                tag += " · claim ⚠"
            label = f"{label} [{tag}]"
        else:
            x0, y0, x1, y1 = _px_rect(region["bbox"], (w, h))
            draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
            label = f"{label} [claim]"
        label = label[:72]
        text_w = draw.textlength(label, font=font) + 8
        text_y = max(0, y0 - 20)
        draw.rectangle([x0, text_y, x0 + text_w, text_y + 18], fill=color)
        draw.text((x0 + 4, text_y + 1), label, fill=(255, 255, 255), font=small)
    return canvas, sorted(set(missing))


def context_crop(img: Image.Image, location: dict | None, *,
                 claim: dict | None = None) -> tuple[Image.Image, str, list[float] | None]:
    """Readable crop around the located (or claimed) region.

    - located: window around the matched fragment; a line-sized fragment
      centres on the value's character offset (`value_offset`); partial
      matches get the same treatment, labeled "likely".
    - no location: the model's claimed box expanded ~2x for context, labeled
      unconfirmed.
    Returns (crop, note, marker_rect) — marker_rect is the normalized region
    to highlight on the page thumbnail.
    """
    w, h = img.size
    if not location or not isinstance(location.get("fragment_bbox"), list):
        bbox = (claim or {}).get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            return img, "page render (no region reported)", None
        x0, y0, x1, y1 = bbox
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half_w = max((x1 - x0), 0.12) * 1.6
        half_h = max((y1 - y0), 0.03) * 3.0
        win = [cx - half_w, cy - half_h, cx + half_w, cy + half_h]
        note = "model-claimed region — text channel could not confirm"
        marker = [x0, y0, x1, y1]
    else:
        frag = location.get("fragment_bbox") or location.get("line_bbox")
        x0, y0, x1, y1 = frag
        fw, fh = x1 - x0, y1 - y0
        off = location.get("value_offset")
        if off is not None and fw > 0.35:
            cx = x0 + off * fw
            win_w = min(0.5, max(0.25, fw * 0.6))
        else:
            cx = (x0 + x1) / 2
            win_w = min(0.6, max(0.30, fw * 3))
        cy = (y0 + y1) / 2
        win_h = min(0.25, max(0.06, fh * 5))
        win = [cx - win_w / 2, cy - win_h / 2, cx + win_w / 2, cy + win_h / 2]
        note = ("text-confirmed location" if location.get("level") == "exact"
                else "likely location (partial text match)")
        marker = frag
    rx0, ry0, rx1, ry1 = (max(0.0, win[0]), max(0.0, win[1]),
                          min(1.0, win[2]), min(1.0, win[3]))
    box = (int(rx0 * w), int(ry0 * h), int(rx1 * w), int(ry1 * h))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return img, note, marker
    crop = img.crop(box).copy()
    # Draw the located region (and the differing claim) ON the crop, so the
    # reviewer never has to hunt for the value inside the context window.
    draw = ImageDraw.Draw(crop)
    if marker:
        mx0, my0, mx1, my1 = _px_rect(marker, (w, h))
        color = (212, 175, 55) if location else (230, 160, 30)
        draw.rectangle([mx0 - box[0], my0 - box[1], mx1 - box[0], my1 - box[1]],
                       outline=color, width=3)
    if location and claim and claim.get("page") == location.get("page"):
        claim_bbox = claim.get("bbox")
        if _claim_differs(claim_bbox, marker):
            cx0, cy0, cx1, cy1 = _px_rect(claim_bbox, (w, h))
            draw.rectangle([cx0 - box[0], cy0 - box[1], cx1 - box[0], cy1 - box[1]],
                           outline=CLAIM_COLOR, width=2)
    return crop, note, marker


def evidence_view(raw_dir: Path, stem: str, field: dict,
                  location: dict | None) -> tuple[Image.Image, str, int, list[float] | None]:
    """Review-evidence crop for one field: (crop, note, page, marker_rect).

    Uses the text-channel location when available (page-accurate, even when
    the model's claim was on another page); the model's claim otherwise.
    """
    claim = field.get("source_region") if isinstance(field.get("source_region"), dict) else None
    page = int((location or {}).get("page") or (claim or {}).get("page") or 1)
    img = page_image(raw_dir, stem, page)
    if img is None:
        return Image.new("RGB", (400, 120), (230, 230, 230)), "no render available", page, None
    crop, note, marker = context_crop(img, location, claim=claim)
    if location and location.get("page_mismatch"):
        claim_page = int((claim or {}).get("page") or 1)
        note += f" — found on page {page}; model claimed page {claim_page}"
    return crop, note, page, marker


def mark_page(raw_dir: Path, stem: str, page: int, rect: list[float] | None,
              max_width: int = 380) -> Image.Image | None:
    """Page thumbnail with `rect` (normalized) highlighted — context view."""
    if not rect:
        return None
    img = page_image(raw_dir, stem, page)
    if img is None:
        return None
    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    x0, y0, x1, y1 = _px_rect(rect, (w, h))
    draw.rectangle([x0, y0, x1, y1],
                   outline=(212, 175, 55), width=max(3, int(min(w, h) * 0.004)))
    if canvas.size[0] > max_width:
        canvas = canvas.resize((max_width, int(canvas.size[1] * max_width / canvas.size[0])),
                               Image.LANCZOS)
    return canvas