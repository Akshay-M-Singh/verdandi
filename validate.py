"""Deterministic arithmetic + format checks, no AI (architecture.md D8/D30).

The VERIFICATION STACK checks (D30, 2026-09-02) — data-driven and
profile-scoped:
- `profile_arithmetic_rules` reads `extraction.arithmetic` from
  rules/documents.yaml (rules are data, D10); `check_arithmetic` evaluates
  `line_items_sum_to` / `sum_equals` (a failed rule implicates ALL involved
  fields — never pick a culprit);
- `check_format` is a per-value_type validator table: fail ONLY when a
  validator exists AND fails (unknown types pass by default).
These feed `verify.py`'s per-field verdicts; they never re-score confidence
(D7/D30 — confidence stays the primary model's). The legacy D8
`validate_output` path was removed 2026-09-21 with the fixed_v1 schema.

Known source-side quirk that must be tolerated, not "fixed": a document may
print a total that differs from its own line-item sum by a rounding amount —
the tolerance absorbs it; the field is flagged, never corrected (D8 note).
"""

from __future__ import annotations

from compare import (amount_components, canonical_amount_ok, currency_ambiguous,
                     norm_amount, norm_amount_components, norm_currency, norm_date)

ARITH_TOLERANCE = 0.02       # D8 rounding tolerance (~₹0.01–0.02)

# --- Check 2 (arithmetic) + Check 3 (format) — verification stack, 2026-09-02 ---
# Additive helpers: profile-config-driven rules (data, not code — D10) and a
# per-value_type validator table. Rule kinds are validated in config.py
# (ARITHMETIC_RULE_KINDS there — the source of truth).

DATE_YEAR_MIN, DATE_YEAR_MAX = 2000, 2099  # plausible-print range guard


def profile_arithmetic_rules(cfg: dict, doc_id: str) -> list[dict]:
    """Arithmetic invariants declared by the document profile (extraction.arithmetic)."""
    for d in cfg.get("documents", {}).get("documents", []):
        if d.get("id") != doc_id:
            continue
        return list(d.get("extraction", {}).get("arithmetic", []) or [])
    return []


def check_arithmetic(header_fields: dict[str, dict], line_items: list[dict],
                     rules: list[dict], amount_col: str = "amount") -> list[dict]:
    """Evaluate the profile's arithmetic rules; results per INVOLVED field.

    A failed rule implicates every field it involves — it cannot say which
    one is wrong, and the aggregation must NOT guess (hard-fail on all).
    `amount_col` (D49): the line-item column carrying the row's amount — the
    column `line_items_sum_to` sums (defaults to the generic `amount` role
    name; callers pass the profile's resolved column).
    """
    results: list[dict] = []

    def add(field: str, rule_id: str, passed: bool, detail: str) -> None:
        results.append({
            "check": "arithmetic", "passed": passed,
            "score": 1.0 if passed else 0.0,
            "detail": f"rule '{rule_id}': {detail}",
        })
        results[-1]["field"] = field

    for rule in rules:
        rid = rule.get("rule")
        tol = float(rule.get("tolerance", ARITH_TOLERANCE))
        if rid == "line_items_sum_to":
            target = rule.get("target")
            f = header_fields.get(target)
            totals = [norm_amount_components(li.get(amount_col))
                      for li in line_items if isinstance(li, dict)]
            if f is None or f.get("value") in (None, "") or not totals:
                continue  # not evaluable -> not applicable, no result
            if not all(t is not None for t in totals):
                continue
            s, c = round(sum(t for t in totals), 2), norm_amount(f["value"])
            if c is None:
                continue
            ok = abs(s - c) <= tol
            detail = f"line-item sum {s:.2f} vs {target} {c:.2f} (tol {tol:.2f})"
            add(target, rid, ok, detail)
        elif rid == "sum_equals":
            fields, target = rule.get("fields", []), rule.get("target")
            vals = [norm_amount(header_fields.get(n, {}).get("value"))
                    for n in fields + [target]]
            if any(v is None for v in vals):
                continue
            ok = abs(sum(vals[:-1]) - vals[-1]) <= tol
            detail = (f"{'+'.join(fields)} = {sum(vals[:-1]):.2f} vs "
                      f"{target} {vals[-1]:.2f} (tol {tol:.2f})")
            for n in fields + [target]:
                add(n, rid, ok, detail)
    return results


def value_type_for(cfg: dict, doc_id: str, name: str) -> str:
    """Profile field type (str|date|amount|name|object); legacy fallbacks."""
    for d in cfg.get("documents", {}).get("documents", []):
        if d.get("id") != doc_id:
            continue
        for f in d.get("extraction", {}).get("fields", []):
            if f.get("name") == name:
                return f.get("type", "str")
    # D49: only the generic minimal schema has static fallbacks — every other
    # field type comes from its profile declaration.
    type_map = {"document_title": "str", "document_date": "date",
                "document_reference": "str", "document_amount": "amount"}
    return type_map.get(name, "str")


def _addend_amount(line_item: dict, name: str, context_values: list[float],
                   tol: float) -> float | None:
    """Rate-aware addend value for row rules (D50).

    A cell may carry a rate next to its money — e.g. the model drops the `%`
    sign ("CGST 9.00 270.00; SGST 9.00 270.00"). A component `r` (0 < r <= 100)
    is treated as a RATE and dropped ONLY when a self-validating numeric
    identity holds: some sibling component `v` in the same cell equals
    `base * r / 100` (base = the row's other rule values) within `tol`.
    Everything else is kept — a genuine small amount with no percentage
    identity stays and can still fail the rule. Single-component cells and
    bare integers go through `norm_amount_components` unchanged.
    """
    raw = line_item.get(name)
    if raw in (None, ""):
        return None
    comps = amount_components(raw)
    if len(comps) <= 1:
        return norm_amount_components(raw)
    try:
        vals = [float(c.replace(",", "")) for c in comps]
    except ValueError:
        return norm_amount_components(raw)
    kept: list[float] = []
    for i, r in enumerate(vals):
        siblings = vals[:i] + vals[i + 1:]
        is_rate = 0 < r <= 100 and any(
            abs(base * r / 100.0 - s) <= tol
            for base in context_values for s in siblings)
        if not is_rate:
            kept.append(r)
    if not kept:
        return norm_amount_components(raw)
    return round(sum(kept), 2)


def check_row_arithmetic(line_item: dict, rules: list[dict],
                         tolerance_default: float = ARITH_TOLERANCE) -> list[dict]:
    """Evaluate the profile's per-ROW arithmetic rules (D49/D50) for ONE row.

    Row rules are template data (`extraction.row_arithmetic`, `sum_equals`):
    the row's own values are checked, e.g. fields [taxable, tax] target total.
    Addend cells are rate-aware (`_addend_amount`): a bare-decimal rate that
    mathematically explains a sibling amount is dropped from the sum.
    `missing: zero` counts an absent component as 0 (tax-free rows pass);
    without it, any absent value makes the rule not evaluable (skip). An
    absent TARGET skips the rule (the completeness check owns missing
    amounts). Returns [{rule, passed, detail}] — the caller flags the row.
    """
    results: list[dict] = []
    for rule in rules or []:
        rid = rule.get("rule")
        if rid != "sum_equals":
            continue
        fields = list(rule.get("fields") or [])
        target = rule.get("target")
        tol = float(rule.get("tolerance", tolerance_default))
        target_val = norm_amount_components(line_item.get(target))
        if target_val is None:
            continue  # not evaluable — the amount-completeness check owns this
        # Context for rate detection: the row's OTHER rule values (never the
        # cell being cleaned — no self-reference).
        context = [norm_amount_components(line_item.get(n))
                   for n in fields + [target]]
        context = [v for v in context if v is not None]
        vals: list[float] = []
        evaluable = True
        for name in fields:
            v = _addend_amount(line_item, name, context, tol)
            if v is None:
                if rule.get("missing") == "zero":
                    v = 0.0
                else:
                    evaluable = False
                    break
            vals.append(v)
        if not evaluable:
            continue
        ok = abs(sum(vals) - target_val) <= tol
        detail = (f"rule '{rid}': {'+'.join(fields)} = {sum(vals):.2f} vs "
                  f"{target} {target_val:.2f} (tol {tol:.2f})")
        results.append({"rule": rid, "passed": ok, "detail": detail})
    return results


def check_format(name: str, value, vtype: str) -> dict:
    """Check 3 — value_type-driven format validator. Fail ONLY when a
    validator exists AND fails; unknown types pass by default."""
    v = str(value)
    detail = f"format '{vtype}'"
    if vtype == "amount":
        # 2026-10-06: the canonical money form only — a plain decimal number,
        # no separators/symbols, at most two decimals. A copied printed form
        # ("50.000", "1,234.56", "₹1,234") flags for review; the model is
        # asked to output the numeric value, not the printed text.
        ok = canonical_amount_ok(v)
        if not ok:
            detail = (f"amount {v.strip()!r} is not a plain decimal number "
                      f"(no separators or symbols; e.g. 1234.56)")
    elif vtype == "date":
        parsed = norm_date(v)
        ok = parsed is not None
        if parsed:
            try:
                year_ok = DATE_YEAR_MIN <= int(parsed[:4]) <= DATE_YEAR_MAX
            except (TypeError, ValueError):
                year_ok = False
            ok = ok and year_ok
    elif vtype == "currency":
        # 2026-10-06: a resolvable ISO 4217 code; ambiguous multi-country
        # symbols are flagged for review, never guessed.
        code = norm_currency(v)
        ok = code is not None
        if not ok:
            if currency_ambiguous(v):
                detail = (f"currency {v.strip()!r} is ambiguous — output the "
                          f"ISO 4217 code (e.g. USD)")
            else:
                detail = (f"currency {v.strip()!r} is not a recognized "
                          f"ISO 4217 code")
    elif vtype == "str":
        # Code-like tokens: no inner whitespace, sane length, some substance.
        stripped = v.strip()
        alnum = sum(ch.isalnum() for ch in stripped)
        ok = bool(stripped) and " " not in stripped and 3 <= alnum <= 40
    elif vtype == "name":
        ok = len(v.strip()) >= 3
    else:  # object and anything unknown: no validator -> pass by default
        ok = True
    return {"check": "format", "passed": ok,
            "score": 1.0 if ok else 0.0,
            "detail": (f"format check '{vtype}' {'passed' if ok else 'FAILED'} "
                       f"for {name}={v!r}" + (f" — {detail}" if not ok else ""))}