"""Minimal PDF writer for the benchmark synthetic generator — stdlib only.

Deliberately standalone (benchmark isolation): no imports from the Verdandi
pipeline or the test-fixture builders. Supports text pages (base-14 fonts) and
full-page JPEG image pages, multi-page documents.
"""

from __future__ import annotations

import io
from pathlib import Path

PAGE_W, PAGE_H = 595.0, 842.0  # A4 pt

FONTS = {
    "F1": "Helvetica",
    "F2": "Helvetica-Bold",
    "F3": "Courier",
    "F4": "Courier-Bold",
}


def _esc(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class TextPage:
    """Accumulates PDF content-stream ops on an A4 canvas (pt, origin bottom-left)."""

    def __init__(self, w: float = PAGE_W, h: float = PAGE_H):
        self.w, self.h = w, h
        self._ops: list[str] = []

    def text(self, x: float, y: float, s: str, size: float = 10,
             font: str = "F1", color: tuple[float, float, float] | None = None) -> None:
        if color is not None:
            self._ops.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg")
        self._ops.append(
            f"BT /{font} {size:.1f} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({_esc(s)}) Tj ET")
        if color is not None:
            self._ops.append("0 0 0 rg")

    def text_right(self, x_right: float, y: float, s: str, size: float = 10,
                   font: str = "F3",
                   color: tuple[float, float, float] | None = None) -> None:
        width = len(s) * size * (0.6 if font.startswith("F3") or font.startswith("F4") else 0.5)
        self.text(x_right - width, y, s, size, font, color)

    def line(self, x1: float, y1: float, x2: float, y2: float,
             width: float = 0.6) -> None:
        self._ops.append(f"{width} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def rect(self, x: float, y: float, w: float, h: float,
             gray: float | None = None, stroke: bool = False) -> None:
        if gray is not None:
            self._ops.append(f"{gray:.3f} g {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f 0 g")
        if stroke:
            self._ops.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re S")

    def content(self) -> str:
        return "\n".join(self._ops)


def _obj(no: int, body: bytes) -> bytes:
    return f"{no} 0 obj\n".encode() + body + b"\nendobj\n"


def _stream_obj(no: int, dict_str: str, data: bytes) -> bytes:
    return _obj(no, f"<< {dict_str} /Length {len(data)} >>\nstream\n".encode()
                + data + b"\nendstream")


def write_pdf(path: Path, pages: list[dict]) -> None:
    """Write a minimal PDF.

    Page keys:
      text: TextPage          (content stream; base-14 fonts are implicit)
      jpeg + jw + jh: bytes   (full-page DCTDecode image drawn to the page box)
    When both are present the image draws first, text on top.
    """
    objs: dict[int, bytes] = {}
    next_no = 3
    kids: list[str] = []
    for page in pages:
        xobjs: list[str] = []
        if page.get("jpeg"):
            n = next_no
            next_no += 1
            objs[n] = _stream_obj(
                n, f"/Type /XObject /Subtype /Image /Width {page['jw']} "
                   f"/Height {page['jh']} /ColorSpace /DeviceRGB "
                   f"/BitsPerComponent 8 /Filter /DCTDecode", page["jpeg"])
            xobjs.append(f"/Im0 {n} 0 R")
        parts: list[str] = []
        if xobjs:
            sw, sh = float(page["jw"]), float(page["jh"])
            scale = min(PAGE_W / sw, PAGE_H / sh)
            dw, dh = sw * scale, sh * scale
            ox, oy = (PAGE_W - dw) / 2.0, (PAGE_H - dh) / 2.0
            parts.append(f"q {dw:.1f} 0 0 {dh:.1f} {ox:.1f} {oy:.1f} cm /Im0 Do Q")
        text = page.get("text")
        if text is not None:
            parts.append(text.content())
        content = "\n".join(parts).encode()
        content_no = next_no
        next_no += 1
        objs[content_no] = _stream_obj(content_no, "", content)
        page_no = next_no
        next_no += 1
        res = ["/Resources <<"]
        if xobjs:
            res.append(f"/XObject << {' '.join(xobjs)} >>")
        font_defs = " ".join(f"/{name} << /Type /Font /Subtype /Type1 /BaseFont /{base} >>"
                             for name, base in FONTS.items())
        res.append(f"/Font << {font_defs} >>")
        res.append(">>")
        objs[page_no] = _obj(
            page_no, f"<< /Type /Page /Parent 2 0 R "
                     f"/MediaBox [0 0 595 842] {' '.join(res)} "
                     f"/Contents {content_no} 0 R >>".encode())
        kids.append(f"{page_no} 0 R")

    max_no = next_no - 1
    objs[1] = _obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    objs[2] = _obj(2, f"<< /Type /Pages /Kids [{' '.join(kids)}] "
                      f"/Count {len(pages)} >>".encode())

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for no in range(1, max_no + 1):
        offsets[no] = out.tell()
        out.write(objs[no])
    xref = out.tell()
    out.write(f"xref\n0 {max_no + 1}\n0000000000 65535 f \n".encode())
    for no in range(1, max_no + 1):
        out.write(f"{offsets[no]:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {max_no + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out.getvalue())
