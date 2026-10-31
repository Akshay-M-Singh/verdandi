"""Map raw benchmark annotations into canonical truth drafts.

  python benchmark/adapters/run_all.py                 # all present sources
  python benchmark/adapters/run_all.py --source fatura

Writes benchmark/data/truth/<stem>.json (canonical schema, see
benchmark/schema.yaml). Drafts start with verification.status = "mapped"
(human/generator source labels) or "machine-draft" (machine labels) and must
be owner-verified before score.py --freeze accepts them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH.parent))
from compare import norm_date  # noqa: E402  (one-way benchmark -> Verdandi)

RAW = BENCH / "data" / "raw"
TRUTH = BENCH / "data" / "truth"

_FIELDS = ("invoice_number", "invoice_date", "vendor_name", "buyer_name",
           "currency", "subtotal", "tax_total", "total_amount")

_CURRENCY_CODES = ("USD", "EUR", "GBP", "INR", "IDR", "SGD", "AED")
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


def _text(doc: dict, key: str) -> str:
    value = doc.get(key)
    return value.get("text", "") if isinstance(value, dict) else ""


def _last_amount(text: str) -> str | None:
    numbers = re.findall(r"-?\d[\d,]*\.?\d*", text or "")
    if not numbers:
        return None
    token = numbers[-1]
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", token):  # dot-grouped thousands (60.000)
        token = token.replace(".", "")
    else:
        token = token.replace(",", "")
    try:
        return f"{Decimal(token):.2f}"
    except InvalidOperation:
        return None


def _label_tail(text: str, labels: tuple[str, ...]) -> str | None:
    low = (text or "").lower()
    for label in labels:
        idx = low.find(label)
        if idx == -1:
            continue
        rest = text[idx + len(label):].lstrip(": #\t").strip()
        first = next((line.strip() for line in rest.splitlines() if line.strip()), None)
        if first:
            return first
    return None


def _currency(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        for code in _CURRENCY_CODES:
            if re.search(rf"(?<![A-Z]){code}(?![A-Z])", text):
                return code
    for text in texts:
        if not text:
            continue
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in text:
                return code
    return None


def _truth(stem: str, source: dict, axes: dict, fields: dict,
           line_items: list | None, status: str, notes: str = "") -> dict:
    return {
        "stem": stem,
        "source": source,
        "axes": axes,
        "fields": {name: fields.get(name) for name in _FIELDS},
        "line_items": line_items,
        "verification": {"status": status, "by": "adapter", "notes": notes},
    }


# --- FATURA (document-level fields; no line-item annotations) -----------------

def fatura_truth(stem: str, ann: dict) -> dict:
    date = _label_tail(_text(ann, "DATE"), ("invoice date", "date"))
    date_iso = norm_date(date) if date else None
    subtotal_t = _text(ann, "SUB_TOTAL")
    tax_t = _text(ann, "TAX")
    total_t = _text(ann, "TOTAL")
    fields = {
        "invoice_number": _label_tail(_text(ann, "NUMBER"),
                                      ("invoice number", "invoice id",
                                       "invoice #", "invoice no", "number")),
        "invoice_date": date_iso,
        "vendor_name": _text(ann, "SELLER_NAME").strip() or None,
        "buyer_name": _label_tail(_text(ann, "BILL_TO"), ("bill_to", "bill to")),
        "currency": _currency(subtotal_t, tax_t, total_t),
        "subtotal": _last_amount(subtotal_t) if subtotal_t else None,
        "tax_total": _last_amount(tax_t) if tax_t else None,
        "total_amount": _last_amount(total_t) if total_t else None,
    }
    axes = {"document_type": "invoice", "quality": "clean", "capture": "image",
            "language": "en", "pages": 1, "multi_document": False,
            "rotation": "none"}
    return _truth(stem, {"kind": "public", "dataset": "fatura2",
                         "license": "CC BY 4.0"}, axes, fields, None,
                  status="mapped")


# --- CORD (Indonesian receipt photos; document-level + line items) ------------

def _signed_amount(text: str) -> str | None:
    """Signed amount from a discount cell, ignoring any rate expression
    ("DISC ITEM -40.000% AMOUNT -2 600" -> -2600; "-67,000" -> -67000).
    Cells that carry only a percentage ("0%", "100%") return None."""
    if not text:
        return None
    cleaned = re.sub(r"-?\d+(?:\.\d+)?\s*%", " ", text)
    compact = re.sub(r"\s+", "", cleaned)
    matches = re.findall(r"(-?\d[\d,]*\.?\d*)", compact)
    if not matches:
        return None
    token = matches[-1]
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", token):
        token = token.replace(".", "")
    else:
        token = token.replace(",", "")
    try:
        return f"{Decimal(token):.2f}"
    except InvalidOperation:
        return None


def cord_truth(stem: str, gt: dict) -> dict:
    parse = gt.get("gt_parse") or {}
    sub = parse.get("sub_total") or {}
    total = parse.get("total") or {}
    rows: dict[tuple, dict] = {}
    order: list[tuple] = []
    for line in gt.get("valid_line") or []:
        category = line.get("category") or ""
        if not category.startswith("menu."):
            continue
        is_sub = category.startswith(("menu.sub_", "menu.sub."))
        key = (line.get("group_id"), line.get("sub_group_id", 0), int(is_sub))
        text = " ".join(w.get("text", "") for w in line.get("words") or []).strip()
        if not text:
            continue
        if key not in rows:
            rows[key] = {"description": None, "quantity": None,
                         "unit_price": None, "amount": None, "discount": None,
                         "itemsubtotal": None}
            order.append(key)
        row = rows[key]
        kind = category.split(".")[-1]
        if kind == "nm" and row["description"] is None:
            row["description"] = text
        elif kind == "cnt" and row["quantity"] is None:
            row["quantity"] = text.rstrip("Xx").strip() or None
        elif kind == "unitprice" and row["unit_price"] is None:
            row["unit_price"] = _last_amount(text.lstrip("@"))
        elif kind == "price" and row["amount"] is None:
            row["amount"] = _last_amount(text)
        elif kind == "itemsubtotal" and row["itemsubtotal"] is None:
            row["itemsubtotal"] = _last_amount(text)
        elif kind == "discountprice" and row["discount"] is None:
            row["discount"] = _signed_amount(text)
    line_items: list[dict] = []
    for key in order:
        row = rows[key]
        # Effective row amount: the printed itemsubtotal when present; else the
        # price net of an explicit signed discount; else the gross price.
        amount = row["itemsubtotal"] or row["amount"]
        if row["itemsubtotal"] is None and row["amount"] and row["discount"]:
            try:
                amount = f"{Decimal(row['amount']) + Decimal(row['discount']):.2f}"
            except InvalidOperation:
                amount = row["amount"]
        if amount is None:
            continue
        line_items.append({"description": row["description"], "quantity": row["quantity"],
                           "unit_price": row["unit_price"], "amount": amount})
    fields = {
        "invoice_number": None,
        "invoice_date": None,
        "vendor_name": None,
        "buyer_name": None,
        # Indonesian receipts print bare numbers (no symbol/code): currency is
        # not scoreable for CORD.
        "currency": None,
        "subtotal": _last_amount(str(sub.get("subtotal_price", ""))),
        "tax_total": _last_amount(str(sub.get("tax_price", ""))),
        "total_amount": _last_amount(str(total.get("total_price", ""))),
    }
    axes = {"document_type": "receipt", "quality": "messy", "capture": "photo",
            "language": "id", "pages": 1, "multi_document": False,
            "rotation": "none"}
    return _truth(stem, {"kind": "public", "dataset": "cord",
                         "license": "CC BY 4.0"}, axes, fields, line_items,
                  status="mapped")


# --- Kaggle "Invoices for Document AI" (Google Document AI parse) --------------

_KAGGLE_TYPES = {
    "invoice_id": "invoice_number",
    "invoice_date": "invoice_date",
    "supplier_name": "vendor_name",
    "receiver_name": "buyer_name",
    "customer_name": "buyer_name",
    "total_amount": "total_amount",
    "net_amount": "subtotal",
    "total_tax_amount": "tax_total",
}
_KAGGLE_ITEM_TYPES = {
    "description": "description",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "amount": "amount",
}


def kaggle_truth(stem: str, doc: dict) -> dict:
    entities = doc.get("entities") or doc.get("document", {}).get("entities")
    if not isinstance(entities, list):
        raise ValueError(
            "unrecognized Document AI JSON (no entities list) — inspect the "
            "annotation and finish the kaggle adapter before use")
    fields: dict[str, str | None] = {name: None for name in _FIELDS}
    items: list[dict] = []
    for ent in entities:
        etype = (ent.get("type") or "").split("/")[-1].strip().lower()
        text = (ent.get("mentionText") or "").strip()
        if etype in _KAGGLE_TYPES:
            target = _KAGGLE_TYPES[etype]
            value = norm_date(text) if target == "invoice_date" else (
                _last_amount(text) if target in ("total_amount", "subtotal", "tax_total") else text)
            if value and fields.get(target) is None:
                fields[target] = value
        elif (ent.get("type") or "").strip().lower() == "line_item":
            row = {"description": None, "quantity": None,
                   "unit_price": None, "amount": None}
            for prop in ent.get("properties") or []:
                ptype = (prop.get("type") or "").split("/")[-1].strip().lower()
                if ptype in _KAGGLE_ITEM_TYPES:
                    ptext = (prop.get("mentionText") or "").strip()
                    row[_KAGGLE_ITEM_TYPES[ptype]] = (
                        _last_amount(ptext) if ptype != "description" else ptext)
            items.append(row)
    fields["currency"] = None
    axes = {"document_type": "invoice", "quality": "scan", "capture": "scan",
            "language": "en", "pages": 1, "multi_document": False,
            "rotation": "none"}
    return _truth(stem, {"kind": "public", "dataset": "kaggle_docai",
                         "license": "CC BY 4.0",
                         "labels": "machine-generated — full review required"},
                  axes, fields, items, status="machine-draft",
                  notes="Document AI machine labels; verify every field")


# --- runner -------------------------------------------------------------------

SOURCES = {
    "fatura": (fatura_truth, "annotation.json"),
    "cord": (cord_truth, "annotation.json"),
    "kaggle": (kaggle_truth, "annotation.json"),
}


def draft_source(source: str) -> int:
    mapper, suffix = SOURCES[source]
    raw_dir = RAW / source
    made = 0
    for ann_path in sorted(raw_dir.glob(f"*.{suffix}")):
        stem = ann_path.name.removesuffix(f".{suffix}")
        try:
            payload = json.loads(ann_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{source}: unreadable annotation {ann_path.name}", file=sys.stderr)
            continue
        truth = mapper(stem, payload)
        out = TRUTH / f"{stem}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        made += 1
        print(f"{source}: {stem} -> truth draft")
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Map raw annotations to truth drafts")
    ap.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    args = ap.parse_args(argv)
    targets = list(SOURCES) if args.source == "all" else [args.source]
    total = sum(draft_source(name) for name in targets)
    print(f"{total} truth draft(s) in {TRUTH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
