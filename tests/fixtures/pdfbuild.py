"""Cross-platform synthetic fixture generator (D63) — no Swift required.

Hand-built PDFs (stdlib + Pillow) covering the whole non-VLM audit surface:
routing (embedded/OCR/empty), blank detection, quality floors, orientation,
image decode paths and embedded-text geometry. The same fixture names the
Swift generator used, so the harness suite runs on any OS.

Image codecs on purpose:
- DCTDecode (JPEG) for real-scan-like pages — ImageIO/PDFium decode them.
- one FlateDecode + INDIRECT ICCBased colorspace page (`photo_flate_icc`) —
  the F-R2/D62 decode path (an indirect colorspace ARRAY, exactly what
  CGPDFContext writes and the old decoder rejected).

Requires Pillow. `available()` gates the tests.
"""

from __future__ import annotations

import io
import zlib
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:  # pragma: no cover - env without UI deps
    HAVE_PIL = False


def available() -> bool:
    return HAVE_PIL


# --- images -----------------------------------------------------------------

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",                       # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",              # macOS
    "C:/Windows/Fonts/arial.ttf",                                # Windows
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",           # Linux (CI)
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
# Cyrillic-capable fonts (the trash-band fixture needs real non-Latin text).
_CYRILLIC_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def font_family() -> str:
    """Which system font fixtures will render with (first available)."""
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return Path(path).name
    return "default"


def _truetype(candidates: tuple[str, ...], size: int):
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _font(size: int):
    return _truetype(_FONT_CANDIDATES, size)


def _jpeg(image: "Image.Image", quality: int = 85) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _scan_image(title: str, w: int = 1700, h: int = 2200,
                size: int = 26, step: int = 70) -> "Image.Image":
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    lines = [f"{title} TOP MARKER ALPHA",
             f"Invoice No. {title.upper()}-001 dated 21-Feb-2025",
             "Bill of Entry No. 1234567 dated 30-Apr-2025",
             "Total Invoice Value INR 7,500.00",
             "Freight charges as per the attached statement",
             "Bank: Example Bank, Mumbai 400001",
             "GSTIN 29AAAAA0000A1Z5 PAN AAAAA0000A",
             "The quick brown fox jumps over the lazy dog",
             f"{title} BOTTOM MARKER OMEGA"]
    y = 60
    for i, line in enumerate(lines):
        d.text((60, y), line, fill="black",
               font=_font(size if i not in (0, len(lines) - 1) else size + 6))
        y += step
    return img


# --- PDF writing ------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _text_ops(lines: list[str], x: float = 50, y: float = 700,
              size: float = 12, leading: float = 30,
              render_mode: int | None = None) -> str:
    """A simple text block (Type1 Helvetica) for digital/hybrid fixtures."""
    ops = ["BT"]
    if render_mode is not None:
        ops.append(f"{render_mode} Tr")
    ops.append(f"/F1 {size} Tf")
    ops.append(f"1 0 0 1 {x} {y} Tm")
    for line in lines:
        ops.append(f"({_esc(line)}) Tj")
        ops.append(f"0 -{leading} Td")
    ops.append("ET")
    return "\n".join(ops)


def _obj(no: int, body: bytes) -> bytes:
    return f"{no} 0 obj\n".encode() + body + b"\nendobj\n"


def _stream_obj(no: int, dict_str: str, data: bytes) -> bytes:
    return _obj(no, f"<< {dict_str} /Length {len(data)} >>\nstream\n".encode()
                + data + b"\nendstream")


def _icc_profile_bytes() -> bytes:
    """A valid sRGB ICC profile when littlecms is available, else a stub.

    The Apple harness decoder only reads /N (components); PDFium parses the
    profile for rendering, so a real profile keeps both engines honest.
    """
    try:
        from PIL import ImageCms
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:  # pragma: no cover - fallback stub
        return b"stub-icc"


def _pdf(path: Path, pages: list[dict]) -> None:
    """Write a minimal PDF. Page keys (all optional except w/h):
    rotate, jpeg + jw/jh (DCTDecode image), flate_icc + fw/fh (FlateDecode
    image with an INDIRECT [/ICCBased] ColorSpace), content, fonts.
    """
    objs: dict[int, bytes] = {}
    next_no = 3
    kids: list[str] = []
    for page in pages:
        xobjs: list[str] = []
        fonts: list[str] = []
        if page.get("jpeg"):
            n = next_no; next_no += 1
            objs[n] = _stream_obj(
                n, f"/Type /XObject /Subtype /Image /Width {page['jw']} "
                   f"/Height {page['jh']} /ColorSpace /DeviceRGB "
                   f"/BitsPerComponent 8 /Filter /DCTDecode", page["jpeg"])
            xobjs.append(f"/Im0 {n} 0 R")
        if page.get("flate_icc"):
            icc_no = next_no; next_no += 1
            cs_no = next_no; next_no += 1
            img_no = next_no; next_no += 1
            objs[icc_no] = _stream_obj(
                icc_no, "/N 3 /Alternate /DeviceRGB", _icc_profile_bytes())
            objs[cs_no] = _obj(cs_no, f"[/ICCBased {icc_no} 0 R]".encode())
            objs[img_no] = _stream_obj(
                img_no, f"/Type /XObject /Subtype /Image /Width {page['fw']} "
                        f"/Height {page['fh']} /ColorSpace {cs_no} 0 R "
                        f"/BitsPerComponent 8 /Filter /FlateDecode",
                zlib.compress(page["flate_icc"]))
            xobjs.append(f"/Im0 {img_no} 0 R")
        for fname, base in (page.get("fonts") or {}).items():
            n = next_no; next_no += 1
            objs[n] = _obj(n, f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} >>"
                           .encode())
            fonts.append(f"/{fname} {n} 0 R")
        content = page.get("content", "")
        content_no = next_no; next_no += 1
        objs[content_no] = _stream_obj(content_no, "", content.encode())
        page_no = next_no; next_no += 1
        res = ["/Resources <<"]
        if xobjs:
            res.append(f"/XObject << {' '.join(xobjs)} >>")
        if fonts:
            res.append(f"/Font << {' '.join(fonts)} >>")
        res.append(">>")
        rotate = f" /Rotate {page['rotate']}" if page.get("rotate") else ""
        objs[page_no] = _obj(
            page_no, f"<< /Type /Page /Parent 2 0 R "
                     f"/MediaBox [0 0 {page['w']} {page['h']}]{rotate} "
                     f"{' '.join(res)} /Contents {content_no} 0 R >>".encode())
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


_PAGE = {"w": 612, "h": 792}


def _page(**kw) -> dict:
    return {**_PAGE, **kw}


def _image_page(jpeg: bytes, jw: int, jh: int, cm: str = "612 0 0 792 0 0",
                **kw) -> dict:
    extra = kw.pop("content", "")
    content = f"q {cm} cm /Im0 Do Q"
    if extra:
        content += "\n" + extra
    return _page(content=content, jpeg=jpeg, jw=jw, jh=jh, **kw)


# --- the full fixture set ---------------------------------------------------

def build_fixtures(dest: Path) -> dict[str, Path]:
    """Generate every fixture into `dest`; {name: path}."""
    dest.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}

    def make(name: str, pages: list[dict]) -> None:
        p = dest / f"{name}.pdf"
        _pdf(p, pages)
        made[name] = p

    # --- blank / pixel probes -------------------------------------------
    make("blank", [_page()])
    make("white_scan", [_image_page(_jpeg(Image.new("RGB", (1224, 1584), "white")),
                                    1224, 1584)])
    light = Image.new("RGB", (1224, 1584), (250, 250, 250))
    ld = ImageDraw.Draw(light)
    for i in range(400):  # deterministic near-white speckle
        ld.point(((i * 37) % 1224, (i * 91) % 1584), fill=(244, 244, 244))
    make("light_gray_scan", [_image_page(_jpeg(light), 1224, 1584)])
    noisy = Image.new("RGB", (1224, 1584), "white")
    nd = ImageDraw.Draw(noisy)
    for i in range(90000):
        nd.point(((i * 37) % 1224, (i * 91) % 1584), fill=(60, 60, 60))
    make("noisy_scan", [_image_page(_jpeg(noisy), 1224, 1584)])

    # --- OCR-readable scans with quality-floor probes -------------------
    def scan_bitmap(title: str, lines: list[str], w: int = 1224,
                    h: int = 1584, size: int = 26, step: int = 84) -> bytes:
        img = Image.new("RGB", (w, h), "white")
        d = ImageDraw.Draw(img)
        y = 60
        for i, line in enumerate(lines):
            d.text((70, y), line, fill="black",
                   font=_font(size if i not in (0, len(lines) - 1) else size + 6))
            y += step
        return _jpeg(img)

    make("sparse_scan", [_image_page(scan_bitmap("SPARSE", [
        "SPARSE TOP MARKER ALPHA", "Invoice No. SPARSE-001",
        "BOTTOM MARKER OMEGA", "Freight charges as per statement."]),
        1224, 1584)])

    def table_bitmap(title: str, rows: int, cells: int, w: int = 1224,
                     h: int = 1584) -> bytes:
        img = Image.new("RGB", (w, h), "white")
        d = ImageDraw.Draw(img)
        d.text((70, 40), f"{title} TOP MARKER ALPHA", fill="black",
               font=_font(30))
        y = 100
        for row in range(1, rows + 1):
            vals = "    ".join(str(1000 + row * 100 + c) for c in range(1, cells + 1))
            d.text((70, y), vals, fill="black", font=_font(26))
            y += 84
        d.text((70, h - 120), "BOTTOM MARKER OMEGA", fill="black", font=_font(30))
        return _jpeg(img)

    make("numeric_table_scan", [_image_page(table_bitmap("NUMERIC", 12, 4),
                                            1224, 1584)])
    make("numeric_sparse_scan", [_image_page(table_bitmap("NUMSPARSE", 8, 3),
                                             1224, 1584)])

    cyr = Image.new("RGB", (1224, 1584), "white")
    cd = ImageDraw.Draw(cyr)
    cyr_font = _truetype(_CYRILLIC_FONTS, 28)
    for row in range(12):
        cd.text((70, 60 + row * 84),
                f"СЧЕТ ФАКТУРА НОМЕР {row + 1} ОПЛАТА ПРОИЗВЕДЕНА ПОЛНОСТЬЮ",
                fill="black", font=cyr_font)
    make("cyrillic_scan", [_image_page(_jpeg(cyr), 1224, 1584)])

    dark = Image.new("RGB", (1224, 1584), (15, 15, 15))
    dd = ImageDraw.Draw(dark)
    dark_lines = ["DARK TOP MARKER ALPHA", "Invoice No. DARK-001",
                  "Invoice Date: 21-Feb-2025", "Bill of Entry No. 1234567",
                  "Total Invoice Value INR 7,500.00",
                  "The quick brown fox jumps over the lazy dog.",
                  "Freight charges as per the attached statement.",
                  "BOTTOM MARKER OMEGA"]
    for i, line in enumerate(dark_lines):
        dd.text((70, 60 + i * 90), line, fill="white",
                font=_font(30 if i in (0, len(dark_lines) - 1) else 26))
    make("dark_scan", [_image_page(_jpeg(dark), 1224, 1584)])

    # Small print inside a thick frame: reproduces the Apple Vision failure
    # (0 observations on a framed page — finding F-Q3).
    framed = Image.new("RGB", (1224, 1584), "white")
    fd = ImageDraw.Draw(framed)
    fd.rectangle([30, 30, 1194, 1554], outline="black", width=40)
    for row in range(8):
        fd.text((120, 200 + row * 60), f"FRAMED invoice line {row + 1} "
                f"value {(row + 1) * 111}.00", fill="black", font=_font(15))
    make("framed_scan", [_image_page(_jpeg(framed), 1224, 1584)])

    # --- digital routing fixtures ---------------------------------------
    digital_lines = [
        "DIGITAL TOP MARKER ALPHA",
        "Tax Invoice No. DIGITAL-001 dated 21-Feb-2025",
        "Bill of Entry No. 1234567 dated 30-Apr-2025",
        "Total Invoice Value INR 7,500.00 (Rupees Seven Thousand Five Hundred)",
        "The quick brown fox jumps over the lazy dog repeatedly for OCR volume.",
        "Bank: Example Bank, Mumbai 400001, Maharashtra, India",
        "GSTIN 29AAAAA0000A1Z5 PAN AAAAA0000A CIN U00000XX0000XXX000000",
        "BOTTOM MARKER OMEGA",
    ]
    make("digital", [_page(fonts={"F1": "Helvetica"},
                           content=_text_ops(digital_lines))])
    make("digital_short", [_page(fonts={"F1": "Helvetica"},
                                 content=_text_ops(["SHORT PAGE ONLY A FEW WORDS"]))])

    bg = _jpeg(_scan_image("DIGBG", w=612, h=792, size=11, step=30))
    make("digital_bg", [_page(jpeg=bg, jw=612, jh=792,
                              fonts={"F1": "Helvetica"},
                              content="q 612 0 0 792 0 0 cm /Im0 Do Q\n"
                                      + _text_ops(digital_lines, y=560))])

    invis_scan = _scan_image("INVIS", size=20, step=60)
    make("scan_invisible_layer", [_image_page(_jpeg(invis_scan), 1700, 2200,
                                              fonts={"F1": "Helvetica"},
                                              content=_text_ops(
                                                  digital_lines, render_mode=3))])
    junk_scan = _scan_image("JUNK", size=20, step=60)
    junk_lines = [f"the quick brown fox jumps over the lazy dog line {i} "
                  f"gibberish" for i in range(1, 7)]
    make("scan_visible_junk", [_image_page(_jpeg(junk_scan), 1700, 2200,
                                           fonts={"F1": "Helvetica"},
                                           content=_text_ops(junk_lines))])

    # --- print-rotation fixtures (image rotated inside the page) --------
    landscape = _scan_image("JROT", w=2200, h=1700, step=62)
    landscape_jpeg = _jpeg(landscape)
    cm = {"90": "0 792 -612 0 612 0",
          "180": "-612 0 0 -792 612 792",
          "270": "0 -792 612 0 0 792"}
    for delta, matrix in cm.items():
        make(f"print_rot{delta}",
             [_image_page(landscape_jpeg, 2200, 1700, cm=matrix)])
    make("print_rot90_pair",
         [_image_page(landscape_jpeg, 2200, 1700, cm=cm["90"])
          for _ in range(2)])

    side = _scan_image("SIDE", w=1584, h=1224, size=26, step=50)
    make("sideways_sparse", [_image_page(_jpeg(side), 1584, 1224, cm=cm["90"])])

    mixed = Image.new("RGB", (1224, 1584), "white")
    md = ImageDraw.Draw(mixed)
    for i in range(1, 11):
        md.text((70, 60 + i * 100),
                f"MIXED horizontal invoice line {i} with several words here",
                fill="black", font=_font(26))
    for j, ch in enumerate("VERTICALSTRIP"):
        md.text((1100, 80 + j * 60), ch, fill="black", font=_font(30))
    make("mixed_vertical", [_image_page(_jpeg(mixed), 1224, 1584)])

    # --- image placement / decode probes --------------------------------
    photo = _scan_image("PHOTO", size=24, step=64)
    raw_rgb = photo.convert("RGB").tobytes()
    make("photo_flate_icc", [_page(
        flate_icc=raw_rgb, fw=1700, fh=2200,
        content="q 612 0 0 792 0 0 cm /Im0 Do Q")])

    portrait = _scan_image("JPHOTO")
    make("jpeg_photo_fit", [_image_page(_jpeg(portrait), 1700, 2200)])

    # --- embedded geometry / rotation probes ----------------------------
    make("digital_rotations", [
        _page(rotate=rot, fonts={"F1": "Helvetica"},
              content=_text_ops([f"ROTPAGE{idx + 1} TOP MARKER ALPHA",
                                 "Tax Invoice No. ROT-001 dated 21-Feb-2025",
                                 "Bill of Entry No. 1234567 dated 30-Apr-2025",
                                 "Total Invoice Value INR 7,500.00",
                                 "BOTTOM MARKER OMEGA"]))
        for idx, rot in enumerate((90, 180, 270))
    ])

    col_lines: list[str] = []
    for row in range(1, 15):
        col_lines.append(f"LEFT-{row} charge row description text")
    make("multi_column", [_page(fonts={"F1": "Helvetica"}, content="\n".join(
        [_text_ops([f"LEFT-{row} charge row description text"], x=50,
                   y=700 - row * 42, size=11)
         + "\n"
         + _text_ops([f"RIGHT-{row} amount {row * 111}.00 value"], x=330,
                     y=700 - row * 42, size=11)
         for row in range(1, 15)]))])

    return made
