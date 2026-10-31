"""Verification stack orchestration — replaces the locator+crosscheck path
(Phase 3, 2026-09-02; owner decision: a SANITY CHECKER, not a second extractor).

Per file, after the primary vision extraction: every extracted field runs the
offline checks (Check 1 grounding — region existence in the text channel;
Check 2 arithmetic — the profile's config-declared invariants; Check 3 format
— value_type table) and is aggregated into a FieldVerdict
(accept | review_secondary | review_human). The gated Check 4 (disagreement)
lives in extract.py run_batch — it needs a second model call and runs only on
suspicion, capped per file (cost discipline, D24).

Also carries the selection guards that survive the pivot (previously inside
compare.crosscheck): flag_no_document_match, sub-min_score matched selections,
pin-drift (D25/D26 hard flag), and per-field below_threshold review rows.

Also runs the cross-document validation rules (D32): workflow-declared
equalities between a field on one document entry and a field on another entry
of the SAME file (check_cross_document) — the per-shipment pertinence check.

Output contract unchanged: flag rows in compare.FLAG_COLUMNS shape; dual-
anchored sources where the text channel was consulted; per-field audit in
data['_meta']['verification'] (every check result — explainability).
"""

from __future__ import annotations

import logging

import aggregate
import config
import containment
import grounding
from compare import (_field_source, amount_components, field_threshold,
                     norm_amount_components, profile_field_kinds,
                     profile_min_score, selected_name, values_equal)
from validate import (check_arithmetic, check_format, check_row_arithmetic,
                      profile_arithmetic_rules, value_type_for)

logger = logging.getLogger("verify")


def _flag(stem: str, field: dict, name: str, threshold, reason: str, sources: str,
          detail: str) -> dict:
    return {
        "file": stem, "field": name, "value": field.get("value"),
        "confidence": field.get("confidence") or 0.0, "threshold": threshold,
        "reason": reason, "sources": sources, "detail": detail,
    }


def _declared_fields(cfg: dict, doc_id: str | None) -> list[str]:
    """The fields a found document is expected to carry (D55): the profile's
    declared fields, or the generic minimal schema for fieldless profiles."""
    names = ([f["name"] for f in config.profile_fields(cfg["documents"], doc_id)]
             if doc_id else [])
    return names or list(config.GENERIC_SCHEMA_FIELDS)


def _missing_value_row(stem: str, field: dict, name: str, cfg: dict,
                       doc_id: str | None, template: str) -> dict:
    """Review row for a declared field with no value on a found document
    (D55). Reason `missing_value`; eval_verification ignores it (completeness
    signal, not a value disagreement)."""
    return {**_flag(stem, {"value": None, "confidence": field.get("confidence") or 0.0},
                    name, field_threshold(cfg, doc_id, name), "missing_value",
                    f"profile: {template or doc_id or ''}.{name} expects a value",
                    "declared field has no value on a found document — missing or "
                    "unreadable (review)"),
            "template": template}


def verify_field(stem: str, raw_dir, field: dict, by_name: dict, line_items: list,
                 rules: list, cfg: dict, doc_id: str) -> dict:
    """Run the offline checks for ONE extracted field.

    Returns {'value', 'checks', 'score', 'level', 'hard_fail', 'flagged'}.
    A missing value yields an all-skip record (nothing to verify).
    """
    name = field["name"]
    value = field.get("value")
    skipped = {"value": value, "checks": [], "score": 1.0, "level": "accept",
               "hard_fail": [], "flagged": False, "skipped": True}
    if value in (None, ""):
        return skipped
    vcfg = cfg["thresholds"]["verification"]
    vtype = value_type_for(cfg, doc_id, name)
    kind = grounding.KIND_BY_TYPE.get(vtype, "code")
    checks = [check_format(name, value, vtype)]

    region = field.get("source_region") or {}
    if region.get("bbox") is not None:
        g = grounding.check_grounding(
            stem, region.get("page", 1), value, region["bbox"], raw_dir,
            margin=vcfg["grounding_margin_rel"],
            search_margin=vcfg["grounding_search_rel"],
            loose_score=vcfg["grounding_loose_score"],
            ngram_k=vcfg["containment_ngram"], kind=kind)
        if g is not None:
            checks.append(g)

    if rules:
        fields = {n: {"value": f.get("value")} for n, f in by_name.items()}
        amount_col = config.line_item_column(cfg["documents"], doc_id, "amount") \
            if doc_id else None
        checks.extend(r for r in check_arithmetic(
            fields, line_items, rules,
            amount_col=(amount_col or {}).get("name", "amount"))
            if r.get("field") == name)

    verdict = aggregate.aggregate(
        name, checks,
        weights=vcfg["check_weights"], hard_fail=tuple(vcfg["hard_fail_checks"]),
        accept=vcfg["accept_score"], review=vcfg["review_score"])
    return {"value": value, "checks": checks, "score": verdict.score,
            "level": verdict.level, "hard_fail": verdict.hard_fail,
            "flagged": verdict.flagged, "skipped": False}


def verify_file(stem: str, data: dict, raw_dir, cfg: dict, doc_id: str,
                pinned: list[int] | None = None) -> tuple[list[dict], dict]:
    """Run the verification stack + selection guards for one extraction.

    Returns (flag_rows, verification_record). Mutates the per-field
    `verification` audit tag and `_meta.verification` (never values or
    confidence — confidence stays the primary model's; D7/D24 semantics).
    """
    fields = data.setdefault("header_fields", [])
    by_name = {f["name"]: f for f in fields}
    rules = profile_arithmetic_rules(cfg, doc_id)
    rows: list[dict] = []
    record: dict[str, object] = {"fields": {}}

    failed_by_check = {}
    for f in fields:
        verdict = verify_field(stem, raw_dir, f, by_name, data.get("line_items", []),
                               rules, cfg, doc_id)
        name = f["name"]
        f["verification"] = {
            "score": verdict["score"], "level": verdict["level"],
            "hard_fail": verdict.get("hard_fail", []),
            "checks": [{"check": c["check"], "passed": c["passed"],
                        "score": c["score"], "detail": c["detail"],
                        "match": c.get("match")}
                       for c in verdict["checks"]],
        }
        record["fields"][name] = {"value": f.get("value"), **f["verification"]}
        threshold = field_threshold(cfg, doc_id, name)
        for c in verdict["checks"]:
            if c["passed"]:
                continue
            failed_by_check[c["check"]] = failed_by_check.get(c["check"], 0) + 1
            reason = f"verification_{c['check']}"
            sources = f"vision: {_field_source(f)}"
            if c["check"] == "grounding":
                # Dual-anchored: the region the model claimed + the text channel.
                text_anchor = c["detail"].split("): ", 1)[-1] if "): " in c["detail"] \
                    else c["detail"]
                sources += f"; text channel: {text_anchor}"
            rows.append(_flag(stem, f, name, threshold, reason, sources, c["detail"]))

    rows.extend(selection_guards(data, cfg, stem, pinned, by_name))
    columns = config.line_item_columns(cfg["documents"], doc_id) if doc_id else None
    row_rules = config.profile_row_arithmetic(cfg["documents"], doc_id) if doc_id else []
    rows.extend(check_line_items(stem, data.get("line_items", []),
                                 template=str(doc_id or ""), columns=columns,
                                 row_rules=row_rules))
    rows.extend(check_line_item_grounding(stem, raw_dir, data.get("line_items", []),
                                          cfg, doc_id, columns=columns,
                                          template=str(doc_id or "")))
    if data.get("status") not in ("flag_no_document_match", "failed"):
        single_entry = {
            "template_id": (data.get("_meta") or {}).get("document") or "",
            "found": True,
            "selected_document": data.get("selected_document"),
            "header_fields": fields,
        }
        rows.extend(check_content_binding(stem, single_entry, raw_dir, cfg))
    record["summary"] = {"failed_by_check": failed_by_check,
                         "flag_rows": len(rows)}
    data.setdefault("_meta", {})["verification"] = record
    return rows, record


def selection_guards(data: dict, cfg: dict, stem: str, pinned: list[int] | None,
                     by_name: dict | None = None) -> list[dict]:
    """Selection-level review rows (D25/D26; previously inside compare.crosscheck).

    flag_no_document_match; a MATCHED selection below the profile min_score;
    pin-drift (hard selection_conflict — never silent re-selection); and
    per-field below_threshold rows (D7/D24: thresholds trigger review).
    """
    doc_id = (data.get("_meta") or {}).get("document")
    rows: list[dict] = []
    by_name = by_name or {f["name"]: f for f in data.get("header_fields", [])}
    template = str(doc_id or "")

    sel = data.get("selected_document") or {}
    min_score = profile_min_score(cfg, doc_id)
    source = f"vision: {sel.get('reason', '')}"
    if data.get("status") == "flag_no_document_match":
        rows.append(_flag(stem, {"value": None, "confidence": sel.get("confidence") or 0.0},
                          "selection", min_score, "no_document_match", source,
                          "no document matches the target profile — never guess (D25)"))
    elif isinstance(sel.get("confidence"), (int, float)) and sel["confidence"] < min_score:
        rows.append(_flag(stem, {"value": selected_name(sel), "confidence": sel["confidence"]},
                          "selection", min_score, "below_threshold", source,
                          "matched selection below the profile min_score"))

    if pinned is not None:
        actual = sorted(sel.get("document_pages") or [])
        pin = sorted(pinned)
        if actual != pin:
            rows.append(_flag(
                stem, {"value": selected_name(sel), "confidence": sel.get("confidence") or 0.0},
                "document", min_score, "selection_conflict",
                source, f"pinned pages {pin}; extracted pages {actual}"))

    # D55: a declared field with no value on a FOUND document is a review flag
    # (missing or unreadable) — completeness, not a value disagreement. Not
    # emitted for a no-match file (that owns the single no_document_match row).
    target_found = (data.get("status") not in ("flag_no_document_match", "failed")
                    and selected_name(sel) is not None)
    if target_found:
        for name in _declared_fields(cfg, doc_id):
            f = by_name.get(name) or {"name": name, "value": None, "confidence": 0.0}
            if f.get("value") not in (None, ""):
                continue
            rows.append(_missing_value_row(stem, f, name, cfg, doc_id, template))

    for name, f in by_name.items():
        if f.get("value") in (None, ""):
            continue
        thr = field_threshold(cfg, doc_id, name)
        conf = f.get("confidence")
        if thr is not None and isinstance(conf, (int, float)) and conf < thr:
            rows.append(_flag(stem, f, name, thr, "below_threshold", _field_source(f),
                              "field confidence below the review threshold (D7/D24)"))
    return rows


# --- multi-document verification (2026-09-05) ------------------------------

def verify_document_entry(stem: str, entry: dict, raw_dir, cfg: dict,
                          pinned: list[int] | None = None) -> tuple[list[dict], dict]:
    """Verification for ONE document entry of a multi-document extraction.

    Mirrors verify_file's per-field logic (checks + verdicts + selection
    guards) but scoped to the entry's own template (its arithmetic rules and
    thresholds govern). Returns (flag_rows, per-entry verification record);
    entry['verification'] is mutated for the audit trail.
    """
    doc_id = entry.get("template_id") or ""
    fields = entry.setdefault("header_fields", [])
    by_name = {f["name"]: f for f in fields}
    rules = profile_arithmetic_rules(cfg, doc_id)
    rows: list[dict] = []
    record: dict[str, object] = {"template": doc_id, "found": bool(entry.get("found")),
                                 "fields": {}}
    failed_by_check: dict[str, int] = {}

    if not entry.get("found"):
        record["reason"] = entry.get("selected_document") or {}
        rows.extend(selection_guards_entry(stem, entry, cfg, doc_id, pinned))
        record["summary"] = {"failed_by_check": {}, "flag_rows": len(rows)}
        entry["verification"] = record
        return rows, record

    for f in fields:
        verdict = verify_field(stem, raw_dir, f, by_name, entry.get("line_items", []),
                               rules, cfg, doc_id)
        name = f["name"]
        f["verification"] = {
            "score": verdict["score"], "level": verdict["level"],
            "hard_fail": verdict.get("hard_fail", []),
            "checks": [{"check": c["check"], "passed": c["passed"],
                        "score": c["score"], "detail": c["detail"],
                        "match": c.get("match")}
                       for c in verdict["checks"]],
        }
        record["fields"][name] = f["verification"]
        threshold = field_threshold(cfg, doc_id, name)
        for c in verdict["checks"]:
            if c["passed"]:
                continue
            failed_by_check[c["check"]] = failed_by_check.get(c["check"], 0) + 1
            reason = f"verification_{c['check']}"
            sources = f"vision: {_field_source(f)}"
            if c["check"] == "grounding":
                text_anchor = c["detail"].split("): ", 1)[-1] if "): " in c["detail"] \
                    else c["detail"]
                sources += f"; text channel: {text_anchor}"
            row = _flag(stem, f, name, threshold, reason, sources, c["detail"])
            row["template"] = doc_id
            rows.append(row)

    # One row per entry, not per field — the ambiguity is document-level.
    if entry.get("uncertain_duplicate"):
        sel = entry.get("selected_document") or {}
        rows.append({**_flag(stem, {"value": selected_name(sel),
                                    "confidence": sel.get("confidence")},
                             "document", None, "duplicate_uncertain",
                             f"vision: {sel.get('reason', '')}",
                             "identical rows cannot be told apart — reviewed"),
                     "template": doc_id})

    rows.extend(selection_guards_entry(stem, entry, cfg, doc_id, pinned))
    columns = config.line_item_columns(cfg["documents"], doc_id) if doc_id else None
    row_rules = config.profile_row_arithmetic(cfg["documents"], doc_id) if doc_id else []
    rows.extend(check_line_items(stem, entry.get("line_items", []),
                                 template=doc_id, columns=columns,
                                 row_rules=row_rules))
    rows.extend(check_line_item_grounding(stem, raw_dir, entry.get("line_items", []),
                                          cfg, doc_id, columns=columns,
                                          template=doc_id))
    rows.extend(check_content_binding(stem, entry, raw_dir, cfg))
    record["summary"] = {"failed_by_check": failed_by_check, "flag_rows": len(rows)}
    entry["verification"] = record
    return rows, record


def selection_guards_entry(stem: str, entry: dict, cfg: dict,
                           doc_id: str | None,
                           pinned: list[int] | None = None) -> list[dict]:
    """Selection-level guard rows for ONE document entry (multi-doc variant).

    `pinned` (D26): pin-drift with SUBSET semantics — a found entry's reported
    pages must lie within the pin. The single-document path uses equality
    because one document owns exactly the pinned pages; a pin in multi mode
    can cover several documents, so equality would false-positive on every
    entry.
    """
    rows: list[dict] = []
    template = str(doc_id or "")
    by_name = {f["name"]: f for f in entry.get("header_fields", [])}

    sel = entry.get("selected_document") or {}
    min_score = profile_min_score(cfg, doc_id or "")
    source = f"vision: {sel.get('reason', '')}"
    if not entry.get("found"):
        rows.append({**_flag(stem, {"value": None,
                                    "confidence": sel.get("confidence") or 0.0},
                             "selection", min_score, "no_document_match", source,
                             f"no document matched profile {template} — never guess (D25)"),
                     "template": template})
        return rows
    if isinstance(sel.get("confidence"), (int, float)) and sel["confidence"] < min_score:
        rows.append({**_flag(stem, {"value": selected_name(sel),
                                    "confidence": sel["confidence"]},
                             "selection", min_score, "below_threshold", source,
                             "matched selection below the profile min_score"),
                     "template": template})
    if pinned is not None:
        actual = sorted(sel.get("document_pages") or [])
        drift = sorted(set(actual) - set(pinned))
        if drift:
            rows.append({**_flag(
                stem, {"value": selected_name(sel),
                       "confidence": sel.get("confidence") or 0.0},
                "document", min_score, "selection_conflict", source,
                f"pinned pages {sorted(pinned)}; extracted pages {actual}"),
                "template": template})
    # D55: declared fields with no value on a FOUND entry get a review row.
    for name in _declared_fields(cfg, doc_id):
        f = by_name.get(name) or {"name": name, "value": None, "confidence": 0.0}
        if f.get("value") in (None, ""):
            rows.append(_missing_value_row(stem, f, name, cfg, doc_id, template))

    for name, f in by_name.items():
        if f.get("value") in (None, ""):
            continue
        thr = field_threshold(cfg, doc_id, name)
        conf = f.get("confidence")
        if thr is not None and isinstance(conf, (int, float)) and conf < thr:
            rows.append({**_flag(stem, f, name, thr, "below_threshold",
                                 _field_source(f),
                                 "field confidence below the review threshold"),
                         "template": template})
    return rows


def check_duplicates(data: dict, cfg: dict, stem: str) -> list[dict]:
    """Deterministic duplicate/overlap backstop over a multi extraction.

    Two entries of the same template sharing any page AND equal on every
    common non-null field value (normalized per field kind — amounts/dates
    compare by value, not string spelling) -> duplicate_occurrence (review).
    Sharing a page with DIFFERENT values -> page_overlap (review). Never
    decides — the reviewer owns it.
    """
    rows: list[dict] = []
    docs = [(i, d) for i, d in enumerate(data.get("documents", [])) if d.get("found")]
    if len(docs) < 2:
        return rows
    by_template: dict[str, list[tuple[int, dict]]] = {}
    for i, d in docs:
        by_template.setdefault(d.get("template_id"), []).append((i, d))
    for template, group in by_template.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                ia, a = group[i]
                ib, b = group[j]
                pa = set((a.get("selected_document") or {}).get("document_pages") or [])
                pb = set((b.get("selected_document") or {}).get("document_pages") or [])
                if not pa & pb:
                    continue
                fa = {f["name"]: f.get("value") for f in a.get("header_fields", [])
                      if f.get("value") not in (None, "")}
                fb = {f["name"]: f.get("value") for f in b.get("header_fields", [])
                      if f.get("value") not in (None, "")}
                common = set(fa) & set(fb)
                kinds = profile_field_kinds(cfg, template)
                identical = bool(common) and all(
                    values_equal(fa[k], fb[k], kinds.get(k, "str")) for k in common)
                reason = "duplicate_occurrence" if identical else "page_overlap"
                detail = (f"{template}: documents[{ia}] (pages {sorted(pa)}) and "
                          f"documents[{ib}] (pages {sorted(pb)}) overlap with "
                          f"{'identical' if identical else 'DIFFERENT'} field values "
                          f"({', '.join(sorted(common)[:5]) or 'no common values'}) — review")
                rows.append({**_flag(stem, {"value": None, "confidence": None},
                                     "selection", None, reason, "", detail),
                             "template": template, "entry": ib})
    return rows


LINE_ITEM_TOLERANCE = 0.02  # row-level consistency bar (D8 semantics)


def check_line_items(stem: str, line_items: list[dict], template: str = "",
                     columns: list[dict] | None = None,
                     row_rules: list[dict] | None = None) -> list[dict]:
    """Row-level line-item sanity (2026-09-07; generic roles + template row
    rules since D49):

    1. COMPLETENESS: every line item MUST carry its `amount` role column —
       a row without an amount is unusable data.
    2. ROW RULES: the profile's `row_arithmetic` rules are evaluated per row
       (`validate.check_row_arithmetic`) — e.g. fields [taxable, tax] target
       total with `missing: zero` for tax-free rows. The rule vocabulary is
       template data; the checker knows no domain names.

    Flags: reason line_item_issue, field line_item_<n>, the row's source
    pointer. Never decides — the reviewer owns it.
    """
    cols = columns or list(config.DEFAULT_LINE_ITEM_COLUMNS)
    amount_name = next((c["name"] for c in cols if c.get("role") == "amount"),
                       "amount")
    rows: list[dict] = []
    for idx, li in enumerate(line_items, start=1):
        amount = norm_amount_components(li.get(amount_name))
        source = str(li.get("source") or "")
        conf = li.get("confidence")
        if amount is None:
            rows.append({**_flag(
                stem, {"value": li.get(amount_name), "confidence": conf},
                f"line_item_{idx}", None, "line_item_issue", source,
                f"line item {idx} has NO AMOUNT ('{amount_name}' missing/empty) "
                f"— row unusable"),
                "template": template})
            continue
        for result in check_row_arithmetic(li, row_rules or []):
            if result["passed"]:
                continue
            rows.append({**_flag(
                stem, {"value": li.get(amount_name), "confidence": conf},
                f"line_item_{idx}", None, "line_item_issue", source,
                f"line item {idx} amounts inconsistent: {result['detail']}"),
                "template": template})
    return rows


def check_line_item_grounding(stem: str, raw_dir, line_items: list[dict],
                              cfg: dict, doc_id: str | None,
                              columns: list[dict] | None = None,
                              template: str = "") -> list[dict]:
    """Per-row grounding (D48 Phase B): every claimed amount cell must be
    found in the text channel near the row's own `source_region`.

    Catches misread/misaligned cells (the claimed value is not printed at the
    claimed row) — the row-level counterpart of the header grounding check.
    Rows without a region, and cells without a value, are skipped (nothing to
    prove). One flag per row naming every ungrounded cell.
    """
    cols = columns if columns is not None else (
        config.line_item_columns(cfg["documents"], doc_id) if doc_id
        else list(config.DEFAULT_LINE_ITEM_COLUMNS))
    checkable = [c for c in cols if c.get("type") == "amount"]
    amount_name = next((c["name"] for c in cols if c.get("role") == "amount"), "amount")
    vcfg = cfg["thresholds"]["verification"]
    # Rows are wider than fields: a row may be printed across several lines
    # (amounts below the description) and models under-cover the row's height —
    # use a 2x search band (0.24). Attribution precision is the math check's
    # job; row grounding's job is "the claimed value is printed near the row".
    row_search = max(float(vcfg["grounding_search_rel"]), 0.24)
    rows: list[dict] = []
    for idx, li in enumerate(line_items or [], start=1):
        if not isinstance(li, dict):
            continue
        region = li.get("source_region") or {}
        if not isinstance(region, dict) or region.get("bbox") is None:
            continue
        misses: list[str] = []
        for c in checkable:
            value = li.get(c["name"])
            if value in (None, ""):
                continue
            # Composite cells ground component-wise (D48): "CGST 9.00% 270.00;
            # SGST 9.00% 270.00" is proven by its two 270.00 components.
            comps = amount_components(value) or [str(value)]
            bad = []
            for comp in comps:
                g = grounding.check_grounding(
                    stem, region.get("page", 1), comp, region["bbox"], raw_dir,
                    margin=vcfg["grounding_margin_rel"],
                    search_margin=row_search,
                    loose_score=vcfg["grounding_loose_score"],
                    ngram_k=vcfg["containment_ngram"], kind="amount")
                if g is not None and not g["passed"]:
                    bad.append(comp)
            if bad:
                misses.append(f"'{c['name']}' " + " / ".join(bad))
        if misses:
            rows.append({**_flag(
                stem, {"value": li.get(amount_name), "confidence": li.get("confidence")},
                f"line_item_{idx}", None, "verification_grounding",
                f"vision: {li.get('source') or ''} (row region page "
                f"{region.get('page')}, bbox {region.get('bbox')})",
                f"line item {idx}: " + "; ".join(misses)
                + " not found near the claimed row region"),
                "template": template})
    return rows


def _whole_file_text(raw_dir, stem: str) -> str:
    """All pages' text of one file (the anti-contamination scope: the primary
    identifier of a found document MUST exist SOMEWHERE in its OWN file)."""
    page_dir = raw_dir / stem
    pieces = []
    for txt in sorted(page_dir.glob("page[0-9][0-9][0-9].txt")):
        try:
            pieces.append(txt.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(pieces)


def check_content_binding(stem: str, entry: dict, raw_dir, cfg: dict) -> list[dict]:
    """Deterministic cross-file-mix-up guard (2026-09-05, bulk workflows).

    For a found document, its PRIMARY field (first profile-order field with a
    non-null value) must exist as a normalized token SOMEWHERE in the same
    file's text channel — check == existence, never extraction (D30 sanity
    discipline). A miss -> content_mismatch flag (route to review), because
    the claimed identifier is not witnessable in this file at all.
    """
    if not entry.get("found"):
        return []
    doc_id = entry.get("template_id") or ""
    by_name = {f["name"]: f for f in entry.get("header_fields", []) or []}
    primary_name = None
    for fd in cfg.get("documents", {}).get("documents", []):
        if fd.get("id") != doc_id:
            continue
        for fdef in fd.get("extraction", {}).get("fields", []):
            f = by_name.get(fdef.get("name"))
            if f and f.get("value") not in (None, ""):
                primary_name = fdef.get("name")
                break
    if primary_name is None:
        return []
    f = by_name[primary_name]
    value = str(f["value"])
    vtype = value_type_for(cfg, doc_id, primary_name)
    kind = grounding.KIND_BY_TYPE.get(vtype, "code")
    all_text = _whole_file_text(raw_dir, stem)
    if containment.exact_containment(value, all_text, kind):
        return []
    return [{**_flag(stem, f, primary_name, None, "content_mismatch",
                     _field_source(f),
                     f"primary value {value!r} not found ANYWHERE in this file's "
                     f"text channel — possible cross-file mix-up (review)"),
             "template": doc_id}]


def verify_multidoc(stem: str, data: dict, raw_dir, cfg: dict,
                    doc_ids: list[str], pinned: list[int] | None = None
                    ) -> tuple[list[dict], dict]:
    """Verification for multi-document extractions: per-entry stack + guards
    + the duplicate/overlap backstop. Recorded in _meta.verification.documents."""
    rows: list[dict] = []
    record: dict[str, object] = {"documents": {}, "summary": {}}
    for i, entry in enumerate(data.get("documents", [])):
        entry_rows, entry_record = verify_document_entry(stem, entry, raw_dir, cfg, pinned)
        for row in entry_rows:
            row.setdefault("entry", i)  # D57: occurrence attribution
        rows.extend(entry_rows)
        record["documents"][entry.get("template_id")] = entry_record
    rows.extend(check_duplicates(data, cfg, stem))
    record["summary"] = {"flag_rows": len(rows), "documents": len(data.get("documents", []))}
    data.setdefault("_meta", {})["verification"] = record
    return rows, record


# --- cross-document validation rules (2026-09-07, D32) -----------------------

CROSS_DOC_REASON = "cross_document_mismatch"
UNBOUND_REASON = "unbound_document"


def _cross_eq_kind(t1: str, t2: str) -> str:
    """Normalized-equality kind for a cross rule's field types (mirrors
    config.cross_kinds_compatible: amount/date must match exactly, str/name
    compare as strings — name equality when either side declares a name)."""
    if t1 == "amount" or t2 == "amount":
        return "amount"
    if t1 == "date" or t2 == "date":
        return "date"
    if t1 == "currency" or t2 == "currency":
        return "currency"
    if t1 == "name" or t2 == "name":
        return "name"
    return "str"


def _profile_field_type(cfg: dict, template: str, field: str) -> str:
    for d in cfg.get("documents", {}).get("documents", []):
        if d.get("id") != template:
            continue
        for f in d.get("extraction", {}).get("fields", []):
            if f.get("name") == field:
                return f.get("type", "str")
    return "str"


def _entry_values(entries: list[tuple[int, dict]], field: str) -> list[tuple[str, dict, int]]:
    """[(value_str, field_dict, entry_index)] for found entries carrying a
    non-null value for `field` — the candidates a cross rule compares.
    `entries` is [(documents index, entry)] (D57: flags are occurrence-aware)."""
    out: list[tuple[str, dict, int]] = []
    for idx, e in entries:
        for f in e.get("header_fields", []):
            if f.get("name") == field and f.get("value") not in (None, ""):
                out.append((str(f["value"]), f, idx))
                break
    return out


def _cross_flag_rows(stem: str, rule: dict, lt: str, lf: str,
                     lv: list[tuple[str, dict, int]], rt: str, rf: str,
                     rv: list[tuple[str, dict, int]]) -> list[dict]:
    """One dual-anchored flag row PER CANDIDATE ENTRY of a failing cross rule
    (each row anchors on its own value + source pointer; the detail names the
    other side's candidates + their sources, so a reviewer sees the pair).
    D57: rows carry the `entry` index of the anchored occurrence. The `rule`
    column (2026-10-04) lets the UI group both sides as one check."""
    rid = rule.get("id")
    label = rule.get("label") or rid

    def _other(tpl: str, field: str, vals: list[tuple[str, dict, int]],
               cap: int = 3) -> str:
        if len(vals) == 1:
            # Single candidate: plain language, no entry-index jargon.
            v, f, _i = vals[0]
            return f"{tpl}.{field}={v!r} ({_field_source(f) or 'no source'})"
        shown = [f"{v!r} (entry {i}, {_field_source(f) or 'no source'})"
                 for v, f, i in vals[:cap]]
        more = f" … and {len(vals) - cap} more" if len(vals) > cap else ""
        return f"{tpl}.{field} {shown}{more}"

    tail = " — attached documents may not pertain to the same shipment (review)"
    out: list[dict] = []
    for vals, tpl, field, otpl, ofield, ovals in (
            (lv, lt, lf, rt, rf, rv), (rv, rt, rf, lt, lf, lv)):
        for v, f, i in vals:
            out.append({
                **_flag(stem, f, field, None, CROSS_DOC_REASON, _field_source(f),
                        f"rule '{rid}' ({label}): {tpl}.{field}={v!r} does "
                        f"not match ANY {_other(otpl, ofield, ovals)}{tail}"),
                "template": tpl, "entry": i, "rule": rid,
            })
    return out


def _unbound_flag_rows(stem: str, rule: dict, lt: str, lf: str,
                       lv: list[tuple[str, dict, int]], rt: str, rf: str,
                       rv: list[tuple[str, dict, int]],
                       unbound: dict) -> list[dict]:
    """D59: one flag per entry whose value matches NO candidate on the other
    side — the case pass-if-ANY would otherwise hide (an attached document
    that does not pertain to the shipment). Only emitted when the rule itself
    passed (a total mismatch already fires `cross_document_mismatch`)."""
    rid = rule.get("id")
    label = rule.get("label") or rid

    def _describe(tpl: str, field: str,
                  vals: list[tuple[str, dict, int]], cap: int = 3) -> str:
        shown = [f"{v!r} (entry {i})" for v, _f, i in vals[:cap]]
        more = f" … and {len(vals) - cap} more" if len(vals) > cap else ""
        return f"{tpl}.{field} {shown}{more}"

    out: list[dict] = []
    sides = (("left", lt, lf, (rt, rf, rv), lv),
             ("right", rt, rf, (lt, lf, lv), rv))
    for side, tpl, field, other, vals in sides:
        unbound_entries = {c.get("entry") for c in unbound.get(side) or []}
        for v, f, i in vals:
            if i not in unbound_entries:
                continue
            out.append({
                **_flag(stem, f, field, None, UNBOUND_REASON, _field_source(f),
                        f"rule '{rid}' ({label}): {tpl}.{field}={v!r} (entry {i}) "
                        f"matches NONE of {_describe(*other)} — this document may "
                        f"not pertain to the same shipment (review)"),
                "template": tpl, "entry": i, "rule": rid,
            })
    return out


def check_cross_document(stem: str, data: dict, cfg: dict,
                         rules: list[dict]) -> tuple[list[dict], dict]:
    """Cross-document validation rules (D32): a declared equality between a
    field extracted on one document entry and a field on another entry of the
    SAME file ("BOE number on the broker invoice must equal the BOE number on
    the attached BOE") — the per-shipment pertinence guarantee.

    Semantics (owner decisions 2026-09-07):
      - found entries only; a rule is NOT evaluable when either side's
        template is absent (SILENT — the workflow `expected` completeness
        contract owns presence) or neither side carries a non-null value.
      - multiplicity: PASS when ANY left x right candidate pair is equal
        (kind-aware normalization — values_equal); a mismatch means NO pair
        matched, and one flag row is emitted PER SIDE (dual-anchored).
      - all rules fire INDEPENDENTLY — a wrong anchor surfaces the whole
        broken chain to review at once; the engine never guesses which side
        is right (never corrects values, D8/D30 discipline).

    Returns (flag_rows, audit_record); the caller merges the audit into
    _meta.verification.cross_checks.
    """
    rows: list[dict] = []
    record: dict[str, object] = {}
    # D57: keep the documents[] index so flags can be occurrence-attributed.
    entries_by_tpl: dict[str, list[tuple[int, dict]]] = {}
    for i, d in enumerate(data.get("documents", [])):
        if d.get("found"):
            entries_by_tpl.setdefault(d.get("template_id"), []).append((i, d))
    for rule in rules:
        rid = rule.get("id")
        left, right = rule.get("left") or {}, rule.get("right") or {}
        lt, lf = left.get("template"), left.get("field")
        rt, rf = right.get("template"), right.get("field")
        rec: dict[str, object] = {
            "label": rule.get("label") or rid,
            "left": {"template": lt, "field": lf},
            "right": {"template": rt, "field": rf},
            "evaluable": False, "passed": None, "skip": None, "pairs": [],
        }
        lv = _entry_values(entries_by_tpl.get(lt, []), lf)
        rv = _entry_values(entries_by_tpl.get(rt, []), rf)
        if not lv or not rv:
            rec["skip"] = ("template_not_found"
                           if not entries_by_tpl.get(lt) or not entries_by_tpl.get(rt)
                           else "no_value")
            record[rid] = rec
            continue
        kind = _cross_eq_kind(_profile_field_type(cfg, lt, lf),
                              _profile_field_type(cfg, rt, rf))
        pairs: list[dict] = []
        for lval, lfield, lentry in lv:
            for rval, rfield, rentry in rv:
                pairs.append({
                    "left": lval, "right": rval,
                    "left_entry": lentry, "right_entry": rentry,
                    "equal": values_equal(lval, rval, kind),
                    "left_source": _field_source(lfield),
                    "right_source": _field_source(rfield),
                })
        matched = any(p["equal"] for p in pairs)
        rec["evaluable"] = True
        rec["passed"] = matched
        rec["kind"] = kind
        rec["pairs"] = pairs
        # D59: candidates that match nothing on the other side. Emitted only
        # when the rule passed — a failing rule already flags every side.
        matched_left = {p["left_entry"] for p in pairs if p["equal"]}
        matched_right = {p["right_entry"] for p in pairs if p["equal"]}
        unbound = {
            "left": [{"entry": i, "value": v, "source": _field_source(f)}
                     for v, f, i in lv if i not in matched_left],
            "right": [{"entry": i, "value": v, "source": _field_source(f)}
                      for v, f, i in rv if i not in matched_right],
        }
        rec["unbound"] = unbound
        record[rid] = rec
        if not matched:
            rows.extend(_cross_flag_rows(stem, rule, lt, lf, lv, rt, rf, rv))
            continue
        if unbound["left"] or unbound["right"]:
            rows.extend(_unbound_flag_rows(stem, rule, lt, lf, lv, rt, rf, rv,
                                           unbound))
    return rows, record