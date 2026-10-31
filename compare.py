"""Shared value normalization + selection helpers (2026-09-02).

The D24 label-locator crosscheck lived here; the deterministic verification
stack (verify.py — grounding/arithmetic/format + gated disagreement)
replaced it (2026-09-02, owner decision: a sanity checker, not a second
extractor). This module keeps the SHARED definitions the stack, the eval
harness and the flag contract all depend on: normalization helpers
(norm_key/norm_amount/norm_date, values_equal), field-kind + threshold
resolution (profile_field_kinds, field_threshold, profile_min_score),
FLAG_COLUMNS and _field_source (dual-anchored sources).
"""

from __future__ import annotations

import datetime
import re

# --- normalization (shared with eval.py) ------------------------------------

_ALNUM = re.compile(r"[^a-z0-9]+")
_LEGAL = re.compile(r"(pvt|private|ltd|limited|inc|incorporated|corp|corporation)\b")
_PUNCT = re.compile(r"[^a-z0-9 ]+")
# D58: day-first formats only (the corpus is Indian logistics; dotted and
# 2-digit years are common on printed BOEs). %y and %Y reject each other's
# widths, so no ambiguity; ISO stays last (no dd-first pattern matches it).
_DATE_FMTS = (
    "%d.%m.%y", "%d.%m.%Y",
    "%d/%m/%y", "%d/%m/%Y",
    "%d-%m-%y", "%d-%m-%Y",
    "%d-%b-%y", "%d-%b-%Y",
    "%d %b %y", "%d %b %Y",
    "%d %B %Y", "%d-%B-%Y",
    "%Y-%m-%d",
)

# Field kinds drive both eval comparison and D32 cross-document equality.
# D49: static kinds cover ONLY the generic minimal schema — every other
# field's kind comes from its profile declaration (type -> kind below).
FIELD_KINDS = {
    "document_title": "str", "document_date": "date",
    "document_reference": "str", "document_amount": "amount",
}


def norm_key(s: str) -> str:
    return _ALNUM.sub("", s.lower())


def norm_name(s) -> list[str]:
    if not isinstance(s, str):
        # Object-typed model values (schema permits dict) are not comparable
        # as names — treat as no-agreement, never crash (2026-09-30).
        return []
    return _PUNCT.sub(" ", _LEGAL.sub(" ", s.lower())).split()


def norm_amount(s) -> float | None:
    if s is None:
        return None
    t = str(s).replace(",", "").replace("₹", "").replace("INR", "")
    t = re.sub(r"[^0-9.\-]", "", t)
    try:
        return round(float(t), 2)
    except ValueError:
        return None


# Amount-like tokens: two decimal places, not embedded in a longer dotted
# sequence (so a date like 04.07.2024 is NOT read as 04.07). Rates/percentages
# have no decimal form and are ignored.
_AMOUNT_TOKEN = re.compile(r"(?<![\d.])([-+]?\d[\d,]*\.\d{2})(?![\d.])")


def amount_components(value) -> list[str]:
    """The amount-like component strings of a cell (D47/D48) — rate
    expressions dropped, so "CGST 9.00% 270.00; SGST 9.00% 270.00" yields
    ["270.00", "270.00"]. [] when the cell has no decimal-form amount."""
    if value is None:
        return []
    cleaned = re.sub(r"\d+(?:\.\d+)?\s*%", " ", str(value))
    return _AMOUNT_TOKEN.findall(cleaned)


def norm_amount_components(value) -> float | None:
    """Amount-cell parser that tolerates split/compound cells (D47).

    One decimal amount -> that amount; several -> their sum
    ("CGST 270.00; SGST 270.00" -> 540.00, "315.00 + 315.00" -> 630.00).
    Rates/percentages are ignored ("9% 270.00" -> 270.00). No decimal-form
    token -> `norm_amount` fallback (single "990", comma-grouped "1,73,140",
    or a 4-digit integer like "5500").
    """
    if value is None:
        return None
    cleaned = re.sub(r"\d+(?:\.\d+)?\s*%", " ", str(value))
    tokens = _AMOUNT_TOKEN.findall(cleaned)
    if not tokens:
        return norm_amount(cleaned)
    try:
        return round(sum(float(t.replace(",", "")) for t in tokens), 2)
    except ValueError:
        return norm_amount(cleaned)


def norm_date(s) -> str | None:
    if not s:
        return None
    t = str(s).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def amount_eq(a, b) -> bool:
    x, y = norm_amount(a), norm_amount(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= 0.01


def date_eq(a, b) -> bool:
    x, y = norm_date(a), norm_date(b)
    return x is not None and x == y


# ---------------------------------------------------------------------------
# currency + locale-aware amount readings (2026-10-06, universal formatting)
#
# The world prints amounts three ways ("1,234.56", "1.234,56", "50.000"); a
# model that outputs the numeric VALUE (canonical, plain decimal) must still
# verify against the printed token, and a value that copied the printed form
# must be flaggable. These helpers are the ONE place that knows separators.
# ---------------------------------------------------------------------------

#: Symbols/text that map to exactly one ISO 4217 code. Ambiguous symbols
#: (bare "$", "¥", "kr") are deliberately ABSENT — callers must flag them.
_CURRENCY_ALIASES = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR",
    "rp": "IDR", "idr": "IDR", "rupiah": "IDR",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "₩": "KRW", "krw": "KRW",
    "us$": "USD", "u.s.$": "USD", "usd": "USD",
    "$us": "USD", "us dollar": "USD", "us dollars": "USD",
    "s$": "SGD", "sgd": "SGD",
    "a$": "AUD", "aud": "AUD",
    "c$": "CAD", "cad": "CAD",
    "hk$": "HKD", "hkd": "HKD",
    "nz$": "NZD", "nzd": "NZD",
    "r$": "BRL", "brl": "BRL",
    "₽": "RUB", "rub": "RUB",
    "฿": "THB", "thb": "THB",
    "₺": "TRY", "try": "TRY",
    "₪": "ILS", "ils": "ILS",
    "rm": "MYR", "myr": "MYR",
    "aed": "AED", "sar": "SAR",
    "cny": "CNY", "rmb": "CNY", "jpy": "JPY", "yen": "JPY",
    "chf": "CHF", "zar": "ZAR", "ngn": "NGN", "kes": "KES",
    "bdt": "BDT", "pkr": "PKR", "lkr": "LKR", "npr": "NPR",
    "php": "PHP", "vnd": "VND", "twd": "TWD",
}

#: Symbols whose meaning depends on the country — never silently guessed.
AMBIGUOUS_CURRENCY = {"$", "¥", "￥", "kr", "kč", "zł", "лв", "din", "r"}

#: Current ISO 4217 codes (the common active set). A code outside this list
#: is flagged for review, never accepted silently.
_ISO_CODES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC
CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF
GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF
KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU
MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR
PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP
STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU
UZS VED VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL
""".split())

#: Canonical money form the model is asked to output: plain decimal, no
#: separators/symbols, at most two decimals.
_CANONICAL_AMOUNT = re.compile(r"^-?\d+(\.\d{1,2})?$")

_GROUPED_COMMA = re.compile(r"^\d{1,3}(?:,\d{2,3})+(?:\.\d+)?$")
_GROUPED_DOT = re.compile(r"^\d{1,3}(?:\.\d{3})+(?:,\d+)?$")
_AMBIG_SINGLE = re.compile(r"^(\d+)([.,])(\d{3})$")


def norm_currency(value) -> str | None:
    """ISO 4217 code for a printed/claimed currency, or None when it can't be
    resolved without guessing (ambiguous symbols, unknown codes)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    key = raw.casefold().replace(" ", "")
    if key in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[key]
    upper = raw.upper()
    return upper if upper in _ISO_CODES else None


def currency_ambiguous(value) -> bool:
    """True when the value is a known multi-country symbol (bare "$", "¥"…)."""
    return str(value or "").strip().casefold() in AMBIGUOUS_CURRENCY


def currency_eq(a, b) -> bool:
    x, y = norm_currency(a), norm_currency(b)
    if x is None or y is None:
        return str_eq(a, b)  # unresolvable forms still compare textually
    return x == y


def canonical_amount_ok(value) -> bool:
    """True when the value is the canonical money form (plain decimal, ≤2dp)."""
    return bool(_CANONICAL_AMOUNT.match(str(value).strip()))


def amount_readings(value) -> set[float]:
    """Every plausible numeric reading of a printed amount token.

    "50.000" -> {50.0, 50000.0} (single 3-digit group is ambiguous);
    "1,234.56" -> {1234.56}; "1.234,56" -> {1234.56}; "1.234.567" -> {1234567.0};
    "1234.56" -> {1234.56}. Empty set when unparseable.
    """
    if value is None:
        return set()
    raw = str(value).strip()
    sign = -1 if raw.startswith("-") else 1
    text = re.sub(r"[^\d.,]", "", raw).strip(".,")
    if not text or not any(ch.isdigit() for ch in text):
        return set()
    out: set[float] = set()

    def add(number: str) -> None:
        try:
            out.add(round(sign * float(number), 2))
        except ValueError:
            pass

    if _CANONICAL_AMOUNT.match(text):
        add(text)
    if _GROUPED_COMMA.match(text):
        grouped = text.replace(",", "")
        if not grouped.startswith("0"):   # "0,125" is a decimal, not grouping
            add(grouped)
    if _GROUPED_DOT.match(text):
        grouped = text.replace(".", "").replace(",", ".")
        if not grouped.startswith("0"):   # "0.125" is a decimal, not grouping
            add(grouped)
    match = _AMBIG_SINGLE.match(text)
    if match:
        head, sep, tail = match.groups()
        add(f"{head}.{tail}")                 # decimal reading
        if not head.startswith("0"):          # grouping reading (no 0-leading)
            add(f"{head}{tail}")
    return out


def name_eq(a, b) -> bool:
    x, y = norm_name(a), norm_name(b)
    if not x or not y:
        return False
    # Prefix containment with a SINGLE window size (min of both, capped 3):
    # a short vendor name must match its longer printed legal form —
    # asymmetric windows (the old min(len(x),3) vs min(len(y),3)) never
    # compared the same slice. (Fixed 2026-08-29 — D29 vision-led selection.)
    n = min(len(x), len(y), 3)
    return x == y or x[:n] == y[:n]


def str_eq(a, b) -> bool:
    if a is None or b is None:
        return False
    return norm_key(str(a)) == norm_key(str(b))


def values_equal(a, b, kind: str) -> bool:
    """Normalized value equality for a field kind (D32 cross-document rules,
    gated-recheck agreement, duplicate detection)."""
    if kind == "amount":
        return amount_eq(a, b)
    if kind == "date":
        return date_eq(a, b)
    if kind == "name":
        return name_eq(a, b)
    if kind == "currency":
        return currency_eq(a, b)
    return str_eq(a, b)


def selected_name(selected_document: dict | None) -> str | None:
    """The name the model READ on the target document — the issuing
    organization's name as printed (v5 rule 1; generic, no registry).

    D45: the output key is `selected_document.name` (was `broker`); legacy
    `broker` is accepted on read so runs extracted before the rename keep
    working in the UI, eval and flag recomputation.
    """
    sel = selected_document or {}
    return sel.get("name") or sel.get("broker")


def profile_min_score(cfg: dict, doc_id: str | None) -> float:
    """The profile's selection-confidence bar (signals.min_score or default)."""
    import config as cfg_mod
    return cfg_mod.profile_min_score(cfg.get("documents", {}), doc_id or "")


def profile_field_kinds(cfg: dict, doc_id: str | None) -> dict[str, str]:
    """Field kinds for a document profile: static FIELD_KINDS merged with the
    profile's user-defined fields (D27 — type -> kind). Profile wins."""
    kinds = dict(FIELD_KINDS)
    if not doc_id:
        return kinds
    profile = next((d for d in cfg["documents"]["documents"] if d["id"] == doc_id), None)
    if not profile:
        return kinds
    type_kind = {"amount": "amount", "date": "date", "str": "str",
                 "name": "name", "object": "str"}
    for field in profile.get("extraction", {}).get("fields", []):
        kinds[field["name"]] = type_kind.get(field.get("type"), "str")
    return kinds


def field_threshold(cfg: dict, doc_id: str | None, name: str) -> float | None:
    """Confidence bar for a field — D28 resolution chain, NEVER silent:
    profile field's inline `threshold` -> thresholds.yaml -> type default."""
    import config as cfg_mod
    field = cfg_mod.profile_field_def(cfg.get("documents", {}), doc_id or "", name)
    if field and "threshold" in field:
        return float(field["threshold"])
    yaml_bar = cfg.get("thresholds", {}).get("thresholds", {}).get(name)
    if yaml_bar is not None:
        return float(yaml_bar)
    if field:
        return cfg_mod.FIELD_TYPE_DEFAULTS.get(field.get("type"), 0.8)
    return None


FLAG_COLUMNS = ("file", "template", "field", "value", "confidence", "threshold",
                "reason", "sources", "detail", "entry", "rule")


def _field_source(f: dict | None) -> str:
    if not f:
        return ""
    parts = [s for s in (f.get("source"),) if s]
    region = f.get("source_region")
    if isinstance(region, dict) and region.get("page"):
        parts.append(f"page {region['page']} (render image, bbox {region.get('bbox')})")
    return "; ".join(parts)
