"""Human-readable messages for every technical code the pipeline emits (2026-10-05).

Single source of truth for user-facing wording: the CLI prints it, the API
returns it alongside flag rows, the Excel export uses it for cell notes and
sheet labels, and the desktop UI renders it. Technical codes stay in the data
(audit, review keys, flags.csv) — they never lead a sentence.

Voice: friendly-professional, sentence case, no jargon, no "(s)" plurals.
A message is {title, body, hint}; `hint` says what to do next.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# flag reasons (verify.py / extract.py / text_layer.py)
# ---------------------------------------------------------------------------

REASONS: dict[str, dict[str, str]] = {
    "verification_grounding": {
        "title": "Value not found on the page",
        "body": "The model read {value}, but the page text doesn't clearly show "
                "it near the spot the model pointed at.",
        "hint": "Check the evidence and confirm the value if it's correct, or "
                "type the right one.",
    },
    "verification_arithmetic": {
        "title": "The numbers don't add up",
        "body": "A total on this document doesn't match the values it's made of.",
        "hint": "Check the amounts and correct the wrong one.",
    },
    "verification_format": {
        "title": "This doesn't look like the expected format",
        "body": "The extracted value isn't shaped like the field expects — for "
                "example a date or an amount.",
        "hint": "Compare it with the document and correct it if needed.",
    },
    "verification_disagreement": {
        "title": "A second read disagreed",
        "body": "The model read this value again with a different approach and "
                "got something else.",
        "hint": "Compare both readings with the document and confirm the right one.",
    },
    "missing_value": {
        "title": "Nothing was extracted",
        "body": "This document should show {field}, but no value was found.",
        "hint": "Check the document — if the value is there, type it in; if not, "
                "confirm it's genuinely absent.",
    },
    "line_item_issue": {
        "title": "A line item doesn't add up",
        "body": "One of the table rows doesn't reconcile with its own values, or "
                "has no amount.",
        "hint": "Open the row and compare it with the printed line.",
    },
    "no_document_match": {
        "title": "This document wasn't found",
        "body": "Nothing in this file matched the {template} profile, so it "
                "wasn't extracted.",
        "hint": "If the document is in the file, pin its pages and extract again; "
                "otherwise confirm it isn't needed.",
    },
    "below_threshold": {
        "title": "Low-confidence read",
        "body": "The model found this document, but its confidence is below the "
                "level you set.",
        "hint": "Check that the selection is right before trusting the values.",
    },
    "selection_conflict": {
        "title": "The selected pages don't match the document",
        "body": "The pages the model chose differ from the pages you pinned.",
        "hint": "Review the pinned pages and extract again if the wrong pages "
                "were used.",
    },
    "document_quality": {
        "title": "Page skipped — not readable",
        "body": "This page was left out of extraction because it looked blank or "
                "illegible.",
        "hint": "If the page matters, check the scan quality and try again.",
    },
    "invalid_source_region": {
        "title": "The page location was unusable",
        "body": "The model pointed at an invalid spot on the page, so the "
                "location was dropped. The value itself is kept.",
        "hint": "Treat the value as unverified and check it against the document.",
    },
    "page_overlap": {
        "title": "Two documents share pages",
        "body": "Two extracted documents claim the same page — they may be "
                "duplicates, or the pages may be split wrongly.",
        "hint": "Check both documents and confirm which one is correct.",
    },
    "duplicate_occurrence": {
        "title": "Possible duplicate document",
        "body": "This file contains another document with the same details.",
        "hint": "Confirm whether it's a true duplicate or two different documents.",
    },
    "missing_expected_document": {
        "title": "Expected document is missing",
        "body": "This workflow expects a {template} in every file, but it wasn't "
                "found here.",
        "hint": "Attach the missing document or remove it from the workflow's "
                "Expected list.",
    },
    "content_mismatch": {
        "title": "Value may belong to another file",
        "body": "This document's identifier wasn't found anywhere in this file's "
                "text.",
        "hint": "Check that the files weren't mixed up before trusting the values.",
    },
    "unbound_document": {
        "title": "Document doesn't match the others",
        "body": "This {template} matches none of the values on the other side of "
                "the check — it may not belong to this shipment.",
        "hint": "Confirm whether this document belongs with the rest, or flag it "
                "as an issue.",
    },
    "cross_document_mismatch": {
        "title": "Two documents disagree",
        "body": "The check {rule} failed: {left} says {left_value}, while {right} "
                "says {right_value}.",
        "hint": "Open both sides and confirm which document is right — or mark "
                "the pair as an issue.",
    },
}

# ---------------------------------------------------------------------------
# verification levels (aggregate.py)
# ---------------------------------------------------------------------------

LEVELS: dict[str, str] = {
    "accept": "Checked",
    "review_human": "Needs review",
    "review_secondary": "Second opinion differs",
}

# ---------------------------------------------------------------------------
# export cell statuses (export.py)
# ---------------------------------------------------------------------------

STATUSES: dict[str, dict[str, str]] = {
    "ok": {"title": "OK", "body": "Nothing to review."},
    "pending": {
        "title": "Needs review",
        "body": "Not reviewed yet — open Review in Verdandi to confirm or "
                "correct this.",
    },
    "issue": {
        "title": "Issue",
        "body": "Reviewed and marked as a problem.",
    },
    "mismatch": {
        "title": "Disagreement",
        "body": "Two documents disagree here.",
    },
    "not_checked": {
        "title": "Not checked",
        "body": "The check couldn't run because a document or value was missing.",
    },
}

# ---------------------------------------------------------------------------
# config warnings (config.py) — {code: {title, body, hint}} with placeholders
# ---------------------------------------------------------------------------

WARNINGS: dict[str, dict[str, str]] = {
    "workflow_doc_missing": {
        "title": "A workflow document was removed",
        "body": "The workflow {workflow} includes {template}, which no longer "
                "exists — it will be skipped in runs.",
        "hint": "Recreate the template or remove it from the workflow's "
                "Documents list.",
    },
    "workflow_doc_fieldless": {
        "title": "A workflow document has no fields",
        "body": "{template} has no extraction fields, so workflow runs skip it.",
        "hint": "Add at least one field to the template in Templates.",
    },
    "expected_missing": {
        "title": "An expected document was removed",
        "body": "The workflow expects {template}, which no longer exists — runs "
                "will flag it as a missing document.",
        "hint": "Recreate the template or remove it from the Expected list.",
    },
    "cross_template_missing": {
        "title": "A cross-check document was removed",
        "body": "The check {rule} uses {template}, which no longer exists — the "
                "check will be skipped.",
        "hint": "Recreate the template or update the check.",
    },
    "cross_template_not_in_workflow": {
        "title": "A cross-check document isn't in the workflow",
        "body": "The check {rule} uses {template}, which isn't in the workflow's "
                "Documents list — the check will be skipped.",
        "hint": "Add the document to Documents so it gets extracted.",
    },
    "cross_template_fieldless": {
        "title": "A cross-check document has no fields",
        "body": "{template} has no extraction fields, so the check {rule} will "
                "be skipped.",
        "hint": "Add fields to the template in Templates.",
    },
    "cross_field_missing": {
        "title": "A cross-check field was removed",
        "body": "{template} no longer has a {field} field, so the check {rule} "
                "will be skipped.",
        "hint": "Update the check to use a field the template still has.",
    },
    "cross_kinds_incompatible": {
        "title": "The two fields can't be compared",
        "body": "{left_field} is a {left_kind} and {right_field} is a "
                "{right_kind} — the check {rule} will be skipped.",
        "hint": "Compare fields of the same kind: dates with dates, amounts with "
                "amounts, text with text.",
    },
    "export_workflow_missing": {
        "title": "The linked workflow was removed",
        "body": "This export links to the workflow {workflow}, which no longer "
                "exists — the export still runs, but check columns may be empty.",
        "hint": "Link the export to an existing workflow.",
    },
    "export_line_item_template_missing": {
        "title": "Line-item document was removed",
        "body": "Line items come from {template}, which no longer exists — those "
                "rows will be empty.",
        "hint": "Pick an existing document for line items.",
    },
    "column_template_missing": {
        "title": "A column's document was removed",
        "body": "The {header} column reads from {template}, which no longer "
                "exists — it will be empty.",
        "hint": "Pick a different source for this column.",
    },
    "column_field_missing": {
        "title": "A column's field was removed",
        "body": "{template} no longer has a {field} field — the {header} column "
                "will be empty.",
        "hint": "Pick another field or re-add it in Templates.",
    },
    "column_line_item_missing": {
        "title": "A column's line-item value was removed",
        "body": "{template} no longer has a {column} column — the {header} "
                "column will be empty.",
        "hint": "Pick another line-item column or update the template.",
    },
    "column_cross_check_missing": {
        "title": "A check isn't in the linked workflow",
        "body": "The check {rule} isn't part of the linked workflow — this "
                "column will show NOT CHECKED.",
        "hint": "Add the check to the workflow or link the right workflow.",
    },
    "column_source_gone": {
        "title": "A column's source isn't available",
        "body": "The {header} column's source no longer exists — it will be empty.",
        "hint": "Pick a different source for this column.",
    },
    "column_line_item_not_available": {
        "title": "Line-item column isn't available",
        "body": "The {column} column isn't declared by the line-item document — "
                "it will be empty.",
        "hint": "Set the export's rows to line items and pick a document that "
                "declares this column.",
    },
    "column_no_source": {
        "title": "No source selected",
        "body": "This column doesn't have a data source yet.",
        "hint": "Choose what this column should show.",
    },
    "unknown_key": {
        "title": "Unrecognized setting",
        "body": "{keys} isn't a known setting here and was ignored.",
        "hint": "Remove it, or check the spelling against the editor.",
    },
    "template_in_use": {
        "title": "This template is used by a workflow",
        "body": "Deleting it leaves {workflows} without this document — runs "
                "will flag it as missing.",
        "hint": "Update the workflow first if you want a clean removal.",
    },
}

# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_DEFAULTS = {
    "value": "this value", "field": "this field", "template": "this document",
    "rule": "this check", "left": "the first document",
    "right": "the second document", "left_value": "a different value",
    "right_value": "a different value", "workflow": "this workflow",
    "header": "this", "column": "this", "keys": "an entry",
    "left_field": "the first field", "right_field": "the second field",
    "left_kind": "text", "right_kind": "text",
}


class Warning(dict):
    """A structured config warning that still behaves like its old string.

    `str(w)` is the plain one-line text (CLI/logs), and `"needle" in w`
    matches the text, so legacy substring checks keep working.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return format_warning(self)

    def __contains__(self, item: object) -> bool:
        return dict.__contains__(self, item) or str(item) in str(self)


def format_warning(w: dict) -> str:
    """One-line plain text for a structured warning (CLI/logs/legacy joins)."""
    title, body, where = w.get("title") or "", w.get("body") or "", w.get("where") or ""
    text = title if not body else f"{title} — {body}"
    return f"{text} ({where})" if where else text


def _fill(template: str, values: dict[str, Any]) -> str:
    data = {k: (values.get(k) if values.get(k) not in (None, "") else v)
            for k, v in _PLACEHOLDER_DEFAULTS.items()}
    try:
        return template.format(**data)
    except (KeyError, IndexError):
        return template


def _quoted(value: Any) -> str:
    text = str(value).strip()
    return f"“{text}”" if text else _PLACEHOLDER_DEFAULTS["value"]


def pretty_name(value: Any) -> str:
    """'boe_date' → 'boe date' (for placeholder substitution)."""
    return str(value or "").replace("_", " ").strip()


def pretty_rule(rule: str) -> str:
    """'a.b__c.d' → 'a · b = c · d' for human sentences."""
    return " = ".join(
        " · ".join(part.replace("_", " ") for part in side.split("."))
        for side in str(rule).split("__"))


def reason_message(code: str | None, **values: Any) -> dict[str, str]:
    """{title, body, hint} for a flag reason code; never raises."""
    rec = REASONS.get(code or "")
    if rec is None:
        title = (code or "Review needed").replace("_", " ").capitalize()
        return {"title": title, "body": "", "hint": ""}
    if values.get("value") not in (None, ""):
        values = {**values, "value": _quoted(values["value"])}
    return {k: _fill(v, values) for k, v in rec.items()}


def level_label(code: str | None) -> str:
    return LEVELS.get(code or "", "Needs review" if code else "")


def status_message(code: str | None) -> dict[str, str]:
    return STATUSES.get(code or "", STATUSES["pending"])


def warning_message(code: str, *, where: str = "", **values: Any) -> Warning:
    """{code, title, body, hint, where} for a config warning; never raises."""
    rec = WARNINGS.get(code)
    if rec is None:
        rec = {"title": "Something needs attention", "body": code, "hint": ""}
    out = {k: _fill(v, values) for k, v in rec.items()}
    out["code"] = code
    out["where"] = where
    return Warning(out)


def warning_text(code: str, *, where: str = "", **values: Any) -> str:
    """One-line plain-text form for the CLI and logs."""
    return format_warning(warning_message(code, where=where, **values))


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{n} {singular}" if n == 1 else f"{n} {plural_form or singular + 's'}"


def count_phrase(counts: dict[str, int]) -> str:
    """Plain phrase for status counts, e.g. '2 items need review · 1 disagreement'."""
    parts: list[str] = []
    if counts.get("pending"):
        n = counts["pending"]
        parts.append(f"{plural(n, 'item')} need{'s' if n == 1 else ''} review")
    if counts.get("issue"):
        parts.append(plural(counts["issue"], "issue"))
    if counts.get("mismatch"):
        parts.append(plural(counts["mismatch"], "disagreement"))
    if counts.get("not_checked"):
        parts.append(f"{plural(counts['not_checked'], 'check')} not run")
    return " · ".join(parts) if parts else "OK"


def reason_summary(counts: dict[str, int], *, limit: int = 3) -> str:
    """Plain sentence for per-reason flag counts (run inspector).

    e.g. '8 items need review — 2 documents disagree, 3 values missing, …'
    """
    total = sum(counts.values())
    if not total:
        return "Nothing needs review"
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    bits = []
    for code, n in ranked[:limit]:
        label = {
            "cross_document_mismatch": "documents disagree",
            "unbound_document": "documents don't match",
            "missing_value": "values missing",
            "line_item_issue": "line items don't add up",
            "verification_grounding": "values not found on the page",
            "verification_arithmetic": "totals don't add up",
            "verification_format": "format problems",
            "verification_disagreement": "second reads disagreed",
            "no_document_match": "documents not found",
            "missing_expected_document": "expected documents missing",
            "document_quality": "pages skipped",
            "duplicate_occurrence": "possible duplicates",
            "page_overlap": "documents share pages",
            "content_mismatch": "files may be mixed up",
            "below_threshold": "low-confidence reads",
            "selection_conflict": "selection conflicts",
            "invalid_source_region": "bad page locations",
        }.get(code, (REASONS.get(code) or {}).get("title", code).lower())
        bits.append(f"{n} {label}")
    more = total - sum(n for _, n in ranked[:limit])
    tail = f", +{more} more" if more > 0 else ""
    return f"{plural(total, 'item')} need review — " + ", ".join(bits) + tail
