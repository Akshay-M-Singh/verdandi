"""Deterministic synthetic benchmark documents (9 docs) + ground truth.

Runbook (benchmark/README.md): `python benchmark/synthetic/generate.py`.
PDFs go to benchmark/data/pdfs/ (gitignored); truth goes to
benchmark/synthetic/truth/ (committed). Fixed seeds -> stable content; the
frozen manifest in benchmark/frozen/ hashes the generated PDFs.

The nine documents cover the axes the public sources do not: clean digital
text-layer PDFs, deliberate rotation, degradation, handwriting-style, a
Hindi+English bilingual page and a two-page multi-document file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdfwrite  # noqa: E402  (sibling module, standalone by design)

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
PDF_DIR = BENCH / "data" / "pdfs"
TRUTH_DIR = HERE / "truth"

CANON_FIELDS = ("invoice_number", "invoice_date", "vendor_name", "buyer_name",
                "currency", "subtotal", "tax_total", "total_amount")

VENDORS = [
    ("ORION GLOBAL LOGISTICS PVT. LTD.", "Plot 12, Sector 9, Navi Mumbai 400703, India"),
    ("NOVA FREIGHT SOLUTIONS PVT. LTD.", "42 Transport Nagar, Chennai 600032, India"),
    ("BLUE HORIZON CARGO SERVICES", "18 Harbor Road, Kochi 682003, India"),
    ("MERIDIAN SUPPLY CHAIN PVT. LTD.", "7th Floor, Trade Centre, Ahmedabad 380015, India"),
]
BUYERS = [
    ("GREENFIELD ENERGY EQUIPMENT LIMITED", "P.O. Box 145, Bengaluru 560001, India"),
    ("STERLING MARKETING COMPANY", "22 MG Road, Pune 411001, India"),
    ("COASTAL POWER PROJECTS LIMITED", "5 Industrial Estate, Surat 395003, India"),
]
ITEMS = ["Freight forwarding charges", "Customs documentation fee",
         "Terminal handling charges", "Warehouse storage charges",
         "Local transportation", "Cargo insurance",
         "Bill of entry charges", "Agency commission"]

_FONT_CANDIDATES = {
    "sans": ("/System/Library/Fonts/Supplemental/Arial.ttf",
             "/System/Library/Fonts/Helvetica.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    "sans_bold": ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    "hand": ("/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
             "/System/Library/Fonts/Supplemental/Brush Script.ttf"),
    "devanagari": ("/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
                   "/System/Library/Fonts/Kohinoor.ttc",
                   "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"),
}
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _FONT_CACHE:
        for path in _FONT_CANDIDATES[kind]:
            if Path(path).exists():
                _FONT_CACHE[key] = ImageFont.truetype(path, size)
                break
        else:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


# --- data -------------------------------------------------------------------

def money(value: Decimal, style: str = "plain") -> str:
    quant = value.quantize(Decimal("0.01"))
    whole, frac = f"{abs(quant):.2f}".split(".")
    if style == "indian" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    else:
        whole = f"{int(whole):,}"
    return f"{whole}.{frac}"


def build_data(seed: int, style: str) -> dict:
    rng = random.Random(seed)
    vendor, vendor_addr = rng.choice(VENDORS)
    buyer, buyer_addr = rng.choice(BUYERS)
    n = rng.randint(3, 5)
    lines: list[dict] = []
    subtotal = Decimal("0")
    for desc in rng.sample(ITEMS, n):
        qty = Decimal(rng.choice(["1", "1", "2", "3", "10"]))
        rate = (Decimal(rng.randrange(25000, 1200000)) / 100).quantize(Decimal("0.01"))
        amount = (qty * rate).quantize(Decimal("0.01"))
        subtotal += amount
        lines.append({"description": desc, "quantity": str(qty),
                      "unit_price": f"{rate:.2f}", "amount": f"{amount:.2f}"})
    tax_rate = Decimal("0.05") if style == "usd" else Decimal("0.18")
    tax_total = (subtotal * tax_rate).quantize(Decimal("0.01"))
    total = (subtotal + tax_total).quantize(Decimal("0.01"))
    date = dt.date(2025, rng.randint(4, 9), rng.randint(1, 28))
    if style == "usd":
        number = f"RCPT-{rng.randrange(10000, 99999)}"
        currency = "USD"
    elif style == "dotted":
        number = f"NF-2025-{rng.randrange(1000, 9999)}"
        currency = "INR"
    else:
        number = f"INV/25-26/{rng.randrange(100, 999)}"
        currency = "INR"
    return {
        "vendor": vendor, "vendor_address": vendor_addr,
        "buyer": buyer, "buyer_address": buyer_addr,
        "invoice_number": number, "date": date,
        "currency": currency, "style": style,
        "subtotal": subtotal, "tax_total": tax_total, "total": total,
        "line_items": lines,
    }


def _date_display(date: dt.date, style: str) -> str:
    if style in ("dotted", "image_b"):
        return date.strftime("%d.%m.%Y")
    if style == "usd":
        return date.strftime("%d/%m/%Y")
    return date.strftime("%d-%m-%Y")


def truth_doc(stem: str, seed: int, data: dict, axes: dict,
              verification: str = "generated") -> dict:
    # Currency is scored ONLY where the document prints it: the compact
    # receipt template prints "USD"; the other templates print bare amounts
    # (no symbol/code), so their currency cell is unscoreable (null).
    printed_currency = data["currency"] if data["style"] == "usd" else None
    axes = {**axes, "intended_currency": data["currency"]}
    return {
        "stem": stem,
        "source": {"kind": "synthetic",
                   "generator": "benchmark/synthetic/generate.py", "seed": seed},
        "axes": axes,
        "fields": {
            "invoice_number": data["invoice_number"],
            "invoice_date": data["date"].isoformat(),
            "vendor_name": data["vendor"],
            "buyer_name": data["buyer"],
            "currency": printed_currency,
            "subtotal": f"{data['subtotal']:.2f}",
            "tax_total": f"{data['tax_total']:.2f}",
            "total_amount": f"{data['total']:.2f}",
        },
        "line_items": list(data["line_items"]),
        "verification": {"status": verification, "by": "generator", "notes": ""},
    }


# --- image rendering --------------------------------------------------------

IMG_W, IMG_H = 1400, 1900


def render_image(data: dict, variant: str) -> Image.Image:
    img = Image.new("RGB", (IMG_W, IMG_H), "white")
    d = ImageDraw.Draw(img)
    inr = data["style"] != "usd"
    grouping = "indian" if inr else "plain"
    d.text((80, 80), data["vendor"], fill="black", font=font("sans_bold", 46))
    d.text((80, 140), data["vendor_address"], fill="#333333", font=font("sans", 26))
    title = "TAX INVOICE" if variant == "a" else ("INVOICE" if variant == "b" else "RECEIPT")
    d.text((IMG_W - 80 - d.textlength(title, font=font("sans_bold", 46)), 200),
           title, fill="black", font=font("sans_bold", 46))
    y = 270
    meta = [f"Invoice No: {data['invoice_number']}",
            f"Invoice Date: {_date_display(data['date'], variant)}",
            f"Bill To: {data['buyer']}"]
    for line in meta:
        d.text((80, y), line, fill="black", font=font("sans", 30))
        y += 46
    y += 30
    cols = (80, 760, 950, 1100) if variant != "c" else (80, 700, 960, 1100)
    header = ["Description", "Qty", "Rate", "Amount"]
    if variant == "a":
        d.rectangle((60, y - 10, IMG_W - 60, y + 44), fill="#e8e8e8")
    for x, label in zip(cols, header):
        if label == "Description":
            d.text((x, y), label, fill="black", font=font("sans_bold", 30))
        else:
            d.text((x + 220 - d.textlength(label, font=font("sans_bold", 30)), y),
                   label, fill="black", font=font("sans_bold", 30))
    y += 60
    for row in data["line_items"]:
        d.text((cols[0], y), row["description"], fill="black", font=font("sans", 28))
        for x, key in ((cols[1], "quantity"), (cols[2], "unit_price"), (cols[3], "amount")):
            text = row[key] if key != "amount" else money(Decimal(row[key]), grouping)
            if key == "unit_price":
                text = money(Decimal(row[key]), grouping)
            d.text((x + 220 - d.textlength(text, font=font("sans", 28)), y),
                   text, fill="black", font=font("sans", 28))
        if variant == "a":
            d.line((60, y + 42, IMG_W - 60, y + 42), fill="#bbbbbb", width=2)
        y += 54
    y += 30
    def right_amount(label: str, value: str, bold: bool = False, dy: int = 0) -> None:
        f = font("sans_bold", 30 if bold else 28)
        d.text((1000, y + dy), label, fill="black", font=f)
        d.text((IMG_W - 80 - d.textlength(value, font=f), y + dy), value,
               fill="black", font=f)
    right_amount("Subtotal:", money(data["subtotal"], grouping))
    if variant == "a":
        half = (data["tax_total"] / 2).quantize(Decimal("0.01"))
        right_amount("CGST @9%:", money(half, grouping), dy=40)
        right_amount("SGST @9%:", money(half, grouping), dy=80)
        right_amount("Tax Total:", money(data["tax_total"], grouping), dy=120)
        y += 160
    else:
        right_amount("Tax:", money(data["tax_total"], grouping), dy=40)
        y += 80
    d.line((950, y + 20, IMG_W - 80, y + 20), fill="black", width=2)
    right_amount("Total:", money(data["total"], grouping), bold=True, dy=40)
    d.text((80, IMG_H - 120), "Payment terms: 30 days. Bank: Example Bank, Mumbai.",
           fill="#444444", font=font("sans", 24))
    return img


def render_handwriting(data: dict, seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (IMG_W, IMG_H), "#fdfdf7")
    d = ImageDraw.Draw(img)
    ink = (10, 20, 120)
    f_title = font("hand", 56)
    f_body = font("hand", 40)
    y = 90 + rng.randint(-6, 6)
    d.text((90 + rng.randint(-4, 4), y), data["vendor"], fill=ink, font=f_title)
    y += 90
    d.text((90, y + rng.randint(-5, 5)), "TAX INVOICE", fill=ink, font=f_title)
    y += 100
    for line in (f"Invoice No: {data['invoice_number']}",
                 f"Date: {_date_display(data['date'], 'a')}",
                 f"Bill To: {data['buyer']}"):
        d.text((90 + rng.randint(-3, 3), y), line, fill=ink, font=f_body)
        y += 62
    y += 30
    d.line((80, y, IMG_W - 80, y + rng.randint(-4, 4)), fill=ink, width=3)
    y += 30
    for row in data["line_items"]:
        text = f"{row['description']}"
        amount = money(Decimal(row["amount"]), "indian")
        d.text((90 + rng.randint(-3, 3), y), text, fill=ink, font=f_body)
        d.text((IMG_W - 90 - d.textlength(amount, font=f_body), y + rng.randint(-4, 4)),
               amount, fill=ink, font=f_body)
        y += 66
    y += 20
    d.line((700, y, IMG_W - 80, y + rng.randint(-3, 3)), fill=ink, width=3)
    y += 30
    for label, value in (("Subtotal:", data["subtotal"]), ("Tax:", data["tax_total"]),
                         ("Total:", data["total"])):
        text = money(value, "indian")
        d.text((760, y + rng.randint(-4, 4)), label, fill=ink, font=f_body)
        d.text((IMG_W - 90 - d.textlength(text, font=f_body), y + rng.randint(-4, 4)),
               text, fill=ink, font=f_body)
        y += 66
    return img


def render_hindi(data: dict, seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (IMG_W, IMG_H), "white")
    d = ImageDraw.Draw(img)
    f_hi = font("devanagari", 44)
    f_hi_t = font("devanagari", 52)
    f_en = font("sans", 30)
    f_en_b = font("sans_bold", 40)
    d.text((80, 80), "कर बीजक / TAX INVOICE", fill="black", font=f_hi_t)
    d.text((80, 170), f"बेचने वाला: {data['vendor']}", fill="black", font=f_hi)
    d.text((80, 240), f"खरीदने वाला: {data['buyer']}", fill="black", font=f_hi)
    d.text((80, 300), f"तारीख: {_date_display(data['date'], 'dotted')}", fill="black", font=f_hi)
    d.text((700, 300), f"Invoice No: {data['invoice_number']}", fill="black", font=f_en)
    y = 420
    d.rectangle((60, y - 12, IMG_W - 60, y + 46), fill="#eeeeee")
    d.text((80, y), "विवरण / Description", fill="black", font=f_hi)
    d.text((IMG_W - 80 - d.textlength("रकम / Amount", font=f_hi), y),
           "रकम / Amount", fill="black", font=f_hi)
    y += 70
    for row in data["line_items"]:
        amount = money(Decimal(row["amount"]), "indian")
        d.text((80, y), row["description"], fill="black", font=f_en)
        d.text((IMG_W - 80 - d.textlength(amount, font=f_en), y), amount,
               fill="black", font=f_en)
        y += 56
    y += 40
    for label, value, hi in (("जोड़ / Subtotal", data["subtotal"], True),
                             ("कर / Tax", data["tax_total"], True),
                             ("कुल / Total", data["total"], False)):
        text = money(value, "indian")
        f = f_hi if hi else f_hi_t
        d.text((720, y), label, fill="black", font=f_hi)
        d.text((IMG_W - 80 - d.textlength(text, font=f_en_b), y), text,
               fill="black", font=f_en_b)
        y += 70
    return img


# --- digital PDF templates --------------------------------------------------

def pdf_template_a(path: Path, data: dict) -> None:
    pdfwrite.write_pdf(path, [{"text": _template_a_page(data)}])


def pdf_template_b(path: Path, data: dict) -> None:
    p = pdfwrite.TextPage()
    p.text(50, 800, "INVOICE", 16, "F2")
    p.text(430, 800, data["vendor"], 10, "F2")
    p.text(430, 786, data["vendor_address"], 7)
    p.text(50, 760, f"Invoice #: {data['invoice_number']}", 9)
    p.text(50, 747, f"Dated {_date_display(data['date'], 'dotted')}", 9)
    p.text(50, 722, f"To: {data['buyer']}", 9)
    p.text(50, 709, data["buyer_address"], 8)
    y = 670
    p.text(50, y, "Description", 9, "F2")
    p.text_right(350, y, "Qty", 9, "F2")
    p.text_right(450, y, "Rate", 9, "F2")
    p.text_right(555, y, "Amount", 9, "F2")
    y -= 6
    p.line(50, y, 555, y, 0.5)
    y -= 18
    for row in data["line_items"]:
        p.text(50, y, row["description"], 8.5)
        p.text_right(350, y, row["quantity"], 8.5)
        p.text_right(450, y, money(Decimal(row["unit_price"]), "indian"), 8.5)
        p.text_right(555, y, money(Decimal(row["amount"]), "indian"), 8.5)
        y -= 22
    y -= 10
    p.text_right(430, y, "Subtotal", 9)
    p.text_right(555, y, money(data["subtotal"], "indian"), 9, "F3")
    y -= 14
    p.text_right(430, y, "Tax", 9)
    p.text_right(555, y, money(data["tax_total"], "indian"), 9, "F3")
    y -= 6
    p.line(400, y, 555, y, 0.8)
    y -= 16
    p.text_right(430, y, "Total", 10, "F2")
    p.text_right(555, y, money(data["total"], "indian"), 10, "F4")
    pdfwrite.write_pdf(path, [{"text": p}])


def pdf_template_c(path: Path, data: dict) -> None:
    p = pdfwrite.TextPage()
    p.text(50, 795, data["vendor"], 13, "F2")
    p.text(50, 780, data["vendor_address"], 8)
    p.text(50, 750, "RECEIPT", 14, "F2")
    p.text(50, 730, f"Receipt No: {data['invoice_number']}", 9)
    p.text(50, 717, f"Date: {_date_display(data['date'], 'usd')}", 9)
    p.text(50, 704, f"Customer: {data['buyer']}", 9)
    y = 660
    p.line(50, y + 10, 555, y + 10, 0.5)
    for row in data["line_items"]:
        left = f"{row['description']}"
        right = money(Decimal(row["amount"]), "plain")
        p.text(50, y, left, 8.5)
        p.text_right(555, y, f"USD {right}", 8.5, "F3")
        y -= 22
    p.line(50, y + 10, 555, y + 10, 0.5)
    y -= 6
    p.text_right(430, y, "Subtotal", 9)
    p.text_right(555, y, f"USD {money(data['subtotal'], 'plain')}", 9, "F3")
    y -= 14
    p.text_right(430, y, "Tax 5%", 9)
    p.text_right(555, y, f"USD {money(data['tax_total'], 'plain')}", 9, "F3")
    y -= 16
    p.text_right(430, y, "Total", 10, "F2")
    p.text_right(555, y, f"USD {money(data['total'], 'plain')}", 10, "F4")
    pdfwrite.write_pdf(path, [{"text": p}])


def pdf_multidoc(path: Path, data: dict) -> None:
    p1 = _template_a_page(data)
    p2 = pdfwrite.TextPage()
    p2.text(50, 795, "ANNEXURE - SHIPMENT DETAILS", 13, "F2")
    p2.text(50, 765, f"Invoice reference: {data['invoice_number']}", 9)
    p2.text(50, 750, "Container No: TEMU7788123 / Seal No: SL-99231", 9)
    p2.text(50, 735, "Vessel: MV EXAMPLE STAR / Voyage 118E", 9)
    p2.text(50, 720, "Port of loading: Nhava Sheva (INNSA) / Port of discharge: Hamburg (DEHAM)", 9)
    p2.text(50, 705, "This annexure is a supporting page and carries no charge amounts.", 9)
    pdfwrite.write_pdf(path, [{"text": p1}, {"text": p2}])


def _template_a_page(data: dict) -> pdfwrite.TextPage:
    p = pdfwrite.TextPage()
    p.text(50, 795, data["vendor"], 15, "F2")
    p.text(50, 779, data["vendor_address"], 8)
    p.text(430, 800, "TAX INVOICE", 14, "F2")
    p.text(50, 748, f"Invoice No: {data['invoice_number']}", 9)
    p.text(50, 735, f"Invoice Date: {_date_display(data['date'], 'a')}", 9)
    p.text(50, 722, f"Bill To: {data['buyer']}", 9)
    p.text(50, 709, data["buyer_address"], 8)
    y = 665
    cols = (50, 330, 420, 480)
    p.rect(45, y - 5, 510, 17, gray=0.88)
    p.text(cols[0], y, "Description", 9, "F2")
    p.text_right(cols[1] + 30, y, "Qty", 9, "F2")
    p.text_right(cols[2] + 55, y, "Rate", 9, "F2")
    p.text_right(cols[3] + 80, y, "Amount", 9, "F2")
    y -= 20
    for row in data["line_items"]:
        p.text(cols[0], y, row["description"], 8.5)
        p.text_right(cols[1] + 30, y, row["quantity"], 8.5)
        p.text_right(cols[2] + 55, y, money(Decimal(row["unit_price"]), "indian"), 8.5)
        p.text_right(cols[3] + 80, y, money(Decimal(row["amount"]), "indian"), 8.5)
        p.line(45, y - 7, 555, y - 7, 0.4)
        y -= 24
    y -= 8
    p.text_right(430, y, "Subtotal:", 9)
    p.text_right(555, y, money(data["subtotal"], "indian"), 9, "F3")
    y -= 14
    half = (data["tax_total"] / 2).quantize(Decimal("0.01"))
    p.text_right(430, y, "CGST @9%:", 9)
    p.text_right(555, y, money(half, "indian"), 9, "F3")
    y -= 14
    p.text_right(430, y, "SGST @9%:", 9)
    p.text_right(555, y, money(half, "indian"), 9, "F3")
    y -= 14
    p.text_right(430, y, "Tax Total:", 9)
    p.text_right(555, y, money(data["tax_total"], "indian"), 9, "F3")
    y -= 8
    p.line(400, y, 555, y, 0.8)
    y -= 16
    p.text_right(430, y, "Total:", 10, "F2")
    p.text_right(555, y, money(data["total"], "indian"), 10, "F4")
    return p


# --- degradation -------------------------------------------------------------

def _jpeg(image: Image.Image, quality: int = 78) -> bytes:
    buf = BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _low_quality(image: Image.Image) -> Image.Image:
    buf = BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=28)
    buf.seek(0)
    out = Image.open(buf).convert("RGB")
    out = out.rotate(0.9, resample=Image.BICUBIC, expand=False, fillcolor="white")
    out = out.filter(ImageFilter.GaussianBlur(0.8))
    noise = Image.effect_noise(out.size, 10).convert("RGB")
    return ImageChops.add(out, noise, scale=2.0, offset=-8)


def _degrade(image: Image.Image, kind: str) -> Image.Image:
    """Additional degradation transforms for the scaled synthetic leg."""
    if kind == "blur":
        return image.filter(ImageFilter.GaussianBlur(1.7))
    if kind == "noise":
        noise = Image.effect_noise(image.size, 22).convert("RGB")
        return ImageChops.add(image, noise, scale=2.0, offset=-12)
    if kind == "jpeg15":
        buf = BytesIO()
        image.convert("RGB").save(buf, "JPEG", quality=15)
        buf.seek(0)
        return Image.open(buf).convert("RGB")
    if kind == "low_contrast":
        out = ImageEnhance.Contrast(image).enhance(0.45)
        return ImageEnhance.Brightness(out).enhance(1.12)
    if kind == "skew":
        return image.rotate(2.4, resample=Image.BICUBIC, expand=False, fillcolor="white")
    raise ValueError(f"unknown degradation {kind!r}")


def _image_page(path: Path, image: Image.Image) -> None:
    pdfwrite.write_pdf(path, [{"jpeg": _jpeg(image), "jw": image.width, "jh": image.height}])


# --- the nine documents ------------------------------------------------------

def build_all(out_dir: Path = PDF_DIR, truth_dir: Path = TRUTH_DIR) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    made: list[dict] = []

    def emit(stem: str, seed: int, data: dict, axes: dict, pdf: Path,
             verification: str = "generated") -> None:
        truth = truth_doc(stem, seed, data, axes, verification)
        (truth_dir / f"{stem}.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        made.append({"stem": stem, "pdf": str(pdf), "axes": axes})

    a = build_data(101, "dashed")
    b = build_data(202, "dotted")
    c = build_data(303, "usd")

    p = out_dir / "syn_01_digital_ruled.pdf"
    pdf_template_a(p, a)
    emit("syn_01_digital_ruled", 101, a,
         {"document_type": "invoice", "quality": "clean", "capture": "digital",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "none"}, p)

    p = out_dir / "syn_02_digital_borderless.pdf"
    pdf_template_b(p, b)
    emit("syn_02_digital_borderless", 202, b,
         {"document_type": "invoice", "quality": "clean", "capture": "digital",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "none"}, p)

    p = out_dir / "syn_03_digital_compact.pdf"
    pdf_template_c(p, c)
    emit("syn_03_digital_compact", 303, c,
         {"document_type": "receipt", "quality": "clean", "capture": "digital",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "none"}, p)

    p = out_dir / "syn_04_scan_rot90.pdf"
    _image_page(p, render_image(a, "a").rotate(-90, expand=True, fillcolor="white"))
    emit("syn_04_scan_rot90", 404, a,
         {"document_type": "invoice", "quality": "clean", "capture": "scan",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "90"}, p)

    p = out_dir / "syn_05_scan_rot180.pdf"
    _image_page(p, render_image(b, "b").rotate(180, expand=True, fillcolor="white"))
    emit("syn_05_scan_rot180", 505, b,
         {"document_type": "invoice", "quality": "clean", "capture": "scan",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "180"}, p)

    p = out_dir / "syn_06_scan_lowq.pdf"
    _image_page(p, _low_quality(render_image(c, "c")))
    emit("syn_06_scan_lowq", 606, c,
         {"document_type": "receipt", "quality": "degraded", "capture": "scan",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "none"}, p)

    hand = build_data(707, "dashed")
    p = out_dir / "syn_07_handwriting.pdf"
    _image_page(p, render_handwriting(hand, 707))
    emit("syn_07_handwriting", 707, hand,
         {"document_type": "invoice", "quality": "handwriting", "capture": "photo",
          "language": "en", "pages": 1, "multi_document": False, "rotation": "none"}, p)

    hindi = build_data(808, "dotted")
    hindi["vendor"] = "ओरायन लॉजिस्टिक्स कंपनी"
    hindi["buyer"] = "ग्रीनफील्ड एनर्जी"
    p = out_dir / "syn_08_hindi_en.pdf"
    _image_page(p, render_hindi(hindi, 808))
    emit("syn_08_hindi_en", 808, hindi,
         {"document_type": "invoice", "quality": "clean", "capture": "scan",
          "language": "hi-en", "pages": 1, "multi_document": False, "rotation": "none"}, p)

    multi = build_data(909, "dashed")
    p = out_dir / "syn_09_multidoc.pdf"
    pdf_multidoc(p, multi)
    emit("syn_09_multidoc", 909, multi,
         {"document_type": "invoice", "quality": "clean", "capture": "digital",
          "language": "en", "pages": 2, "multi_document": True, "rotation": "none"}, p)

    # --- scaled leg (syn_10 .. syn_30): more layouts, transforms, languages ---

    def scan(stem: str, seed: int, style: str, variant: str, *,
             rotate: int | None = None, degrade: str | None = None,
             rotation: str = "none", quality: str = "clean",
             document_type: str = "invoice") -> None:
        data = build_data(seed, style)
        image = render_image(data, variant)
        if rotate is not None:
            image = image.rotate(rotate, expand=True, fillcolor="white")
        if degrade is not None:
            image = _degrade(image, degrade)
        path = out_dir / f"{stem}.pdf"
        _image_page(path, image)
        emit(stem, seed, data,
             {"document_type": document_type, "quality": quality, "capture": "scan",
              "language": "en", "pages": 1, "multi_document": False,
              "rotation": rotation}, path)

    def digital(stem: str, seed: int, style: str, renderer) -> None:
        data = build_data(seed, style)
        path = out_dir / f"{stem}.pdf"
        renderer(path, data)
        emit(stem, seed, data,
             {"document_type": "receipt" if style == "usd" else "invoice",
              "quality": "clean", "capture": "digital", "language": "en",
              "pages": 1, "multi_document": False, "rotation": "none"}, path)

    digital("syn_10_digital_ruled_b", 1010, "dashed", pdf_template_a)
    digital("syn_11_digital_borderless_b", 1111, "dotted", pdf_template_b)
    digital("syn_12_digital_compact_b", 1212, "usd", pdf_template_c)
    digital("syn_13_digital_ruled_c", 1313, "dashed", pdf_template_a)
    digital("syn_14_digital_borderless_c", 1414, "dotted", pdf_template_b)
    digital("syn_15_digital_compact_c", 1515, "usd", pdf_template_c)

    scan("syn_16_scan_rot270", 1616, "dashed", "a", rotate=90, rotation="270")
    scan("syn_17_scan_blur", 1717, "dotted", "b", degrade="blur", quality="degraded")
    scan("syn_18_scan_noise", 1818, "usd", "c", degrade="noise", quality="degraded",
         document_type="receipt")
    scan("syn_19_scan_jpeg15", 1919, "dashed", "a", degrade="jpeg15", quality="degraded")
    scan("syn_20_scan_lowcontrast", 2020, "dotted", "b", degrade="low_contrast",
         quality="degraded")
    scan("syn_21_scan_skew", 2121, "usd", "c", degrade="skew", quality="degraded",
         document_type="receipt")
    scan("syn_22_scan_rot90_b", 2222, "dashed", "a", rotate=-90, rotation="90")
    scan("syn_23_scan_rot180_b", 2323, "dotted", "b", rotate=180, rotation="180")

    for stem, seed in (("syn_24_handwriting_b", 2424),
                       ("syn_25_handwriting_c", 2525),
                       ("syn_26_handwriting_d", 2626)):
        hand = build_data(seed, "dashed")
        path = out_dir / f"{stem}.pdf"
        _image_page(path, render_handwriting(hand, seed))
        emit(stem, seed, hand,
             {"document_type": "invoice", "quality": "handwriting",
              "capture": "photo", "language": "en", "pages": 1,
              "multi_document": False, "rotation": "none"}, path)

    for stem, seed in (("syn_27_hindi_en_b", 2727), ("syn_28_hindi_en_c", 2828)):
        hindi = build_data(seed, "dotted")
        hindi["vendor"] = "ओरायन लॉजिस्टिक्स कंपनी"
        hindi["buyer"] = "ग्रीनफील्ड एनर्जी"
        path = out_dir / f"{stem}.pdf"
        _image_page(path, render_hindi(hindi, seed))
        emit(stem, seed, hindi,
             {"document_type": "invoice", "quality": "clean", "capture": "scan",
              "language": "hi-en", "pages": 1, "multi_document": False,
              "rotation": "none"}, path)

    for stem, seed in (("syn_29_multidoc_b", 2929), ("syn_30_multidoc_c", 3030)):
        data = build_data(seed, "dashed")
        path = out_dir / f"{stem}.pdf"
        pdf_multidoc(path, data)
        emit(stem, seed, data,
             {"document_type": "invoice", "quality": "clean", "capture": "digital",
              "language": "en", "pages": 2, "multi_document": True,
              "rotation": "none"}, path)

    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the synthetic benchmark leg")
    ap.add_argument("--out", default=str(PDF_DIR))
    ap.add_argument("--truth", default=str(TRUTH_DIR))
    args = ap.parse_args(argv)
    made = build_all(Path(args.out), Path(args.truth))
    for m in made:
        print(f"{m['stem']:26s} {Path(m['pdf']).stat().st_size:>8} B  {m['axes']}")
    print(f"{len(made)} synthetic documents written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
