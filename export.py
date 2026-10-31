"""Excel export engine (D54) — local, deterministic, no LLM, no network.

Turns a run's reviewed artifacts (`json/` + `flags.csv` + `review.json` +
`manifest.json`) into a typed, colour-coded workbook per an export profile
(`rules/exports.yaml`). Read-only over the run; openpyxl is imported LAZILY
(UI dependency — the pipeline venv stays pyyaml+stdlib, D14).

CLI:
    python export.py <run_dir> --export <profile_id> [--out PATH] [--list]

Workbook layout: `Data` (the configured columns; grain `line_item` expands one
row per line item of `line_item_template`, header fields repeated), `Issues`
(every flag row + cross-check outcome + multiplicity notes, with review
state), `Meta` (provenance, counts, colour legend) and an optional
`Line items` long-format sheet.

Colour semantics (D54, owner decision):
  amber = flagged & unreviewed · red = reviewed-as-issue OR cross-document
  mismatch · grey = cross-check NOT evaluated (missing value/template) ·
  clean cells stay uncoloured. Every coloured cell carries a comment with the
  reason + detail.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import config
import messages
from compare import norm_amount_components, norm_date

REPO_ROOT = Path(__file__).resolve().parent

FILL_PENDING = "FFF2CC"
FILL_ISSUE = "FFC7CE"
FILL_NOT_CHECKED = "E7E6E6"
HEADER_FILL = "1F3864"
HEADER_FONT = "FFFFFF"
STATUS_ORDER = {"ok": 0, "not_checked": 1, "pending": 2, "issue": 3, "mismatch": 4}
STATUS_LABEL = {"ok": "OK", "not_checked": "not checked", "pending": "pending",
                "issue": "issue", "mismatch": "mismatch"}
FILLS = {"pending": FILL_PENDING, "issue": FILL_ISSUE,
         "mismatch": FILL_ISSUE, "not_checked": FILL_NOT_CHECKED}


class ExportError(ValueError):
    """Export cannot proceed (unknown profile, unreviewed gate, bad run)."""


@dataclass
class Cell:
    value: object = None          # typed value written to Excel
    raw: object = None            # value as stored (printed/corrected)
    status: str = "ok"            # ok|not_checked|pending|issue|mismatch
    comment: str | None = None
    type: str = "text"            # text|number|date


@dataclass
class Row:
    stem: str
    original: str
    cells: list[Cell] = field(default_factory=list)
    flags_total: int = 0
    flags_pending: int = 0
    reviewed: int = 0
    validation: str = "OK"


def flag_key(row: dict) -> str:
    """The review-state key — IDENTICAL to ui.lib.flag_key (a test asserts
    equality): file|entry|template|field|reason|detail (D57)."""
    entry = row.get("entry")
    entry = "" if entry is None else entry
    return (f"{row.get('file')}|{entry}|{row.get('template') or ''}|"
            f"{row.get('field')}|{row.get('reason')}|{row.get('detail')}")


def legacy_flag_key(row: dict) -> str:
    """Pre-D57 key format — old review.json entries keep matching."""
    return (f"{row.get('file')}|{row.get('template') or ''}|{row.get('field')}|"
            f"{row.get('reason')}|{row.get('detail')}")


def review_state(review: dict, row: dict) -> dict:
    """Review entry for a flag row (D57 key first, legacy fallback)."""
    if not review:
        return {}
    return review.get(flag_key(row)) or review.get(legacy_flag_key(row)) or {}


# ---------------------------------------------------------------------------
# run loading
# ---------------------------------------------------------------------------

def load_run(run_dir: Path) -> dict:
    """Read a run's artifacts into a plain structure (no openpyxl needed)."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise ExportError(f"not a run directory: {run_dir}")
    manifest: dict[str, str] = {}
    mp = run_dir / "manifest.json"
    if mp.exists():
        try:
            for e in (json.loads(mp.read_text(encoding="utf-8")).get("files") or []):
                manifest[e.get("stem")] = e.get("original")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    review: dict = {}
    rp = run_dir / "review.json"
    if rp.exists():
        try:
            review = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review = {}
    flags_by_file: dict[str, list[dict]] = defaultdict(list)
    fp = run_dir / "flags.csv"
    if fp.exists():
        with fp.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                flags_by_file[row.get("file", "")].append(row)
    files: list[dict] = []
    for jp in sorted((run_dir / "json").glob("*.json")):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("status") == "failed":
            continue
        files.append({"stem": jp.stem,
                      "original": manifest.get(jp.stem) or jp.stem,
                      "data": data,
                      "flags": flags_by_file.get(jp.stem, []),
                      "review": review})
    meta = (files[0]["data"].get("_meta") or {}) if files else {}
    # Desktop runs persist a job.json with the user-editable name (D66);
    # CLI runs have none — the Meta row stays blank there.
    name = ""
    job_file = run_dir / "job.json"
    if job_file.exists():
        try:
            name = (json.loads(job_file.read_text(encoding="utf-8"))
                    .get("name") or "")
        except (OSError, json.JSONDecodeError):
            name = ""
    return {"run_dir": run_dir, "files": files, "model": meta.get("model"),
            "prompt": meta.get("prompt"), "name": name}


def export_profile(cfg: dict, export_id: str) -> dict:
    for exp in (cfg.get("exports", {}).get("exports") or []):
        if exp.get("id") == export_id:
            return exp
    raise ExportError(f"unknown export profile {export_id!r} — rules/exports.yaml")


# ---------------------------------------------------------------------------
# cell resolution
# ---------------------------------------------------------------------------

def _found_entries(data: dict, template: str) -> list[dict]:
    entries = [e for e in data.get("documents", []) if e.get("found")
               and e.get("template_id") == template]
    if entries:
        return entries
    if data.get("documents"):
        return []  # multi-document run: this template is genuinely absent
    # SINGLE-DOCUMENT run (2026-10-02 fix): the file carries exactly one
    # document; bind it when the run's own template matches (legacy runs
    # without _meta.document are accepted). Previously template columns were
    # silently blank for every single-document export.
    doc = (data.get("_meta") or {}).get("document")
    if doc in (None, "", template) and \
            data.get("status") != "flag_no_document_match":
        return [data]
    return []


def _first_entry(data: dict, template: str) -> dict | None:
    entries = _found_entries(data, template)
    return entries[0] if entries else None


def _entry_of(data: dict, entry: dict | None) -> int | None:
    """documents[] index of an entry object (None for single-document JSON)."""
    if entry is None:
        return None
    for i, d in enumerate(data.get("documents", [])):
        if d is entry:
            return i
    return None


def _flags_for(flags: list[dict], entry_idx: int | None) -> list[dict]:
    """D57: keep only flags anchored on the exported occurrence. Rows without
    an `entry` are file-level (selection/completeness) and always kept."""
    if entry_idx is None:
        return flags
    return [f for f in flags
            if f.get("entry") in (None, "") or str(f.get("entry")) == str(entry_idx)]


def _field_value(entry: dict | None, name: str):
    if entry is None:
        return None
    for f in entry.get("header_fields", []):
        if f.get("name") == name:
            return f.get("value")
    return None


def _cross_outcomes(data: dict) -> dict[tuple[str, str], list[tuple]]:
    """(template, field) -> [(rule_id, passed, skip, sentence)] from the audit."""
    out: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    audit = ((data.get("_meta") or {}).get("verification") or {}).get("cross_checks") or {}
    for rid, rec in audit.items():
        if not isinstance(rec, dict):
            continue
        sentence = _cross_sentence(rid, rec)
        for side in ("left", "right"):
            s = rec.get(side) or {}
            t, f = s.get("template"), s.get("field")
            if t and f:
                out[(t, f)].append((rid, rec.get("passed"), rec.get("skip"),
                                    sentence))
    return out


def _cross_sentence(rid: str, rec: dict) -> str:
    """Plain reviewer sentence for one cross-check outcome (no codes)."""
    label = rec.get("label") or rid
    if rec.get("skip"):
        reason = {"no_value": "one side has no value",
                  "template_not_found": "a document is missing"}.get(
                      rec["skip"], "it couldn't be evaluated")
        return f"Check “{label}” wasn't run — {reason}."
    pairs = [p for p in (rec.get("pairs") or []) if isinstance(p, dict)]
    left = _humanize(rec.get("left", {}).get("template"))
    right = _humanize(rec.get("right", {}).get("template"))
    if rec.get("passed") is False:
        if pairs:
            p = pairs[0]
            msg = messages.reason_message(
                "cross_document_mismatch", rule=f"“{label}”", left=left,
                right=right, left_value=p.get("left"), right_value=p.get("right"))
            return f"{msg['title']} — {msg['body']}"
        return f"Check “{label}” failed — the two documents don't agree."
    return f"Check “{label}” passed — both documents agree."


def _humanize(name: str | None) -> str:
    return str(name or "").replace("_", " ").strip().title()


def _page_ref(frow: dict) -> str | None:
    """'page 8' extracted from a flag row's sources, when present."""
    import re

    match = re.search(r"page\s+(\d+)", str(frow.get("sources") or ""))
    return f"page {match.group(1)}" if match else None


def _review_state(review: dict, frow: dict) -> tuple[str, str | None, str | None]:
    """(status, note, reviewed_at) for one flag row; pending when absent."""
    r = review_state(review, frow)
    return (r.get("status") or "pending", r.get("note"), r.get("reviewed_at"))


def _field_cell_status(flags: list[dict], review: dict,
                       cross: list[tuple]) -> tuple[str, str | None]:
    status = "ok"
    notes: list[str] = []

    def worst(new: str) -> None:
        nonlocal status
        if STATUS_ORDER[new] > STATUS_ORDER[status]:
            status = new

    for frow in flags:
        st, note, _ = _review_state(review, frow)
        reason = frow.get("reason") or "flag"
        msg = messages.reason_message(
            reason, value=frow.get("value"), field=frow.get("field"),
            template=_humanize(frow.get("template")), rule=frow.get("rule"))
        base = msg["title"] + (f" — {msg['body']}" if msg["body"] else "")
        page = _page_ref(frow)
        if page:
            base += f" (see {page})"
        if st == "issue":
            worst("issue")
            notes.append(f"Marked as an issue — {note or msg['hint'] or base}")
        elif st == "confirmed":
            notes.append("Reviewed and confirmed"
                         + (f" — {note}" if note else ""))
        elif reason == "unbound_document":
            # D59: a document matching no cross-check candidate is red until
            # the reviewer confirms it (pertinence problem, not a to-do).
            worst("mismatch")
            notes.append(f"{base} — not confirmed yet")
        else:
            worst("pending")
            notes.append(f"{base} {msg['hint']}".rstrip())
    for _rid, passed, skip, sentence in cross:
        if passed is False:
            worst("mismatch")
            notes.append(sentence)
        elif skip:
            worst("not_checked")
            notes.append(sentence)
    return status, "; ".join(notes) or None


def _coerce(value, col_type: str, *, field_type: str | None = None,
            date_format: str = "dd/mm/yyyy") -> tuple[object, str, str | None]:
    """(typed_value, type, note) — amount->number, date->Excel date, else text."""
    if value in (None, ""):
        return None, "text", None
    kind = col_type
    if kind == "auto":
        kind = {"amount": "number", "date": "date"}.get(field_type or "", "text")
    if kind == "number":
        num = norm_amount_components(value)
        if num is None:
            return str(value), "text", "Couldn't read this as a number — kept as text."
        return num, "number", None
    if kind == "date":
        iso = norm_date(value)
        if iso is None:
            return str(value), "text", "Couldn't read this as a date — kept as text."
        return date.fromisoformat(iso), "date", None
    return str(value), "text", None


def _column_field_type(cfg: dict, col: dict) -> str | None:
    src = col.get("source") or {}
    if "template" in src:
        docs = cfg.get("documents", {})
        field_def = config.profile_field_def(docs, src["template"], src.get("field") or "")
        return (field_def or {}).get("type")
    if "line_item" in src:
        return None  # resolved by the caller against declared columns
    return None


def build_rows(run_dir: Path, profile: dict, cfg: dict | None = None) -> list[Row]:
    """Assemble the Data-sheet rows (typed cells + per-cell status/comments)."""
    cfg = cfg or config.load_all()
    run = load_run(run_dir)
    options = profile.get("options") or {}
    date_format = options.get("date_format") or "dd/mm/yyyy"
    grain = profile.get("grain", "file")
    grain_tpl = profile.get("line_item_template")
    columns = profile.get("columns") or []
    li_cols = config.line_item_columns(cfg["documents"], grain_tpl) if grain_tpl else []
    li_by_name = {c["name"]: c for c in li_cols}
    amount_name = next((c["name"] for c in li_cols if c.get("role") == "amount"), "amount")

    rows: list[Row] = []
    for frec in run["files"]:
        data, flags, review = frec["data"], frec["flags"], frec["review"]
        cross_by_field = _cross_outcomes(data)
        flags_by_field: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for frow in flags:
            flags_by_field[(frow.get("template") or "", frow.get("field") or "")].append(frow)

        if grain == "line_item":
            entry = _first_entry(data, grain_tpl)
            grain_entry_idx = _entry_of(data, entry)
            items = (entry.get("line_items") or []) if entry else []
            item_rows: list[dict | None] = list(items) or [None]
        else:
            grain_entry_idx = None
            item_rows = [None]

        for idx, item in enumerate(item_rows, start=1):
            cells: list[Cell] = []
            for col in columns:
                src = col.get("source") or {}
                col_type = col.get("type", "auto")
                if "meta" in src:
                    cell = _meta_cell(src["meta"], frec, flags, review, data)
                elif "template" in src:
                    tpl, fname = src["template"], src.get("field") or ""
                    col_entry = _first_entry(data, tpl)
                    value = _field_value(col_entry, fname)
                    status, comment = _field_cell_status(
                        _flags_for(flags_by_field.get((tpl, fname), []),
                                   _entry_of(data, col_entry)), review,
                        cross_by_field.get((tpl, fname), []))
                    typed, typ, note = _coerce(value, col_type,
                                               field_type=_column_field_type(cfg, col))
                    comment = "; ".join(x for x in (comment, note) if x) or None
                    cell = Cell(typed, value, status, comment, typ)
                elif "line_item" in src:
                    key = src["line_item"]
                    status, comment = _field_cell_status(
                        _flags_for(flags_by_field.get((grain_tpl, f"line_item_{idx}"), []),
                                   grain_entry_idx), review, [])
                    if item is None:
                        cell = Cell(None, None, status, comment, "text")
                    elif key == "index":
                        cell = Cell(idx, idx, status, comment, "number")
                    else:
                        name = amount_name if key == "amount" else key
                        value = item.get(name)
                        declared = li_by_name.get(name) or {}
                        ftype = declared.get("type")
                        typed, typ, note = _coerce(value, col_type, field_type=ftype)
                        cell = Cell(typed, value, status,
                                    "; ".join(x for x in (comment, note) if x) or None, typ)
                else:  # cross_check
                    rid = src["cross_check"]
                    audit = ((data.get("_meta") or {}).get("verification") or {}
                             ).get("cross_checks") or {}
                    rec = audit.get(rid) or {}
                    if not rec:
                        value, status = "NOT CHECKED", "not_checked"
                        detail = ("This check isn't in this run — it may have "
                                  "been added after the run.")
                    elif rec.get("passed") is False:
                        value, status = "FAIL", "mismatch"
                        detail = _cross_sentence(rid, rec)
                    elif rec.get("skip"):
                        value, status = "NOT CHECKED", "not_checked"
                        detail = _cross_sentence(rid, rec)
                    else:
                        value, status = "PASS", "ok"
                        detail = _cross_sentence(rid, rec)
                    cell = Cell(value, value, status, detail, "text")
                cells.append(cell)

            pending = sum(1 for frow in flags if _review_state(review, frow)[0] == "pending")
            reviewed = len(flags) - pending
            validation = _validation_summary(cells, pending)
            row = Row(stem=frec["stem"], original=frec["original"], cells=cells,
                      flags_total=len(flags), flags_pending=pending, reviewed=reviewed,
                      validation=validation)
            # The Validation meta cell mirrors the row's worst status.
            for col, cell in zip(columns, row.cells):
                if (col.get("source") or {}).get("meta") == "validation_status":
                    cell.value = cell.raw = row.validation
                    cell.status = max((c.status for c in row.cells),
                                      key=lambda s: STATUS_ORDER[s])
                    cell.comment = _validation_comment(row)
            rows.append(row)

    if options.get("require_reviewed"):
        pending = sum(r.flags_pending for r in rows)
        if pending:
            raise ExportError(f"{pending} flag(s) are still unreviewed — "
                              f"review them or set options.require_reviewed: false")
    return rows


def _validation_summary(cells: list[Cell], pending: int) -> str:
    counts = {"pending": pending}
    for c in cells:
        if c.status != "ok":
            counts[c.status] = counts.get(c.status, 0) + 1
    return messages.count_phrase(counts)


def _validation_comment(row: Row) -> str:
    counts = {"pending": row.flags_pending}
    for c in row.cells:
        if c.status != "ok":
            counts[c.status] = counts.get(c.status, 0) + 1
    phrase = messages.count_phrase(counts)
    return f"{phrase} — every coloured cell has a note (hover it)."


def _meta_cell(name: str, frec: dict, flags: list[dict], review: dict,
               data: dict) -> Cell:
    if name == "original":
        return Cell(frec["original"], frec["original"], "ok", None, "text")
    if name == "stem":
        return Cell(frec["stem"], frec["stem"], "ok", None, "text")
    if name == "status":
        status = data.get("status") or ""
        return Cell(status, status, "ok", None, "text")
    pending = sum(1 for frow in flags if _review_state(review, frow)[0] == "pending")
    if name == "flags_total":
        return Cell(len(flags), len(flags), "ok", None, "number")
    if name == "flags_pending":
        return Cell(pending, pending, "ok", None, "number")
    if name == "reviewed":
        reviewed = len(flags) - pending
        return Cell(reviewed, reviewed, "ok", None, "number")
    if name == "validation_status":
        return Cell("", "", "ok", None, "text")  # filled after the row is built
    return Cell(None, None, "ok", None, "text")


# ---------------------------------------------------------------------------
# workbook
# ---------------------------------------------------------------------------

def _openpyxl():
    try:
        import openpyxl
        return openpyxl
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExportError(
            "openpyxl is required for Excel export — install the UI deps: "
            ".venv/bin/python -m pip install -r requirements-desktop.txt") from exc


def build_workbook(run_dir: Path, profile: dict, cfg: dict | None = None):
    """Build the openpyxl Workbook (Data + Issues + Meta [+ Line items])."""
    openpyxl = _openpyxl()
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill

    cfg = cfg or config.load_all()
    run = load_run(run_dir)
    rows = build_rows(run_dir, profile, cfg)
    options = profile.get("options") or {}
    date_format = options.get("date_format") or "dd/mm/yyyy"
    columns = profile.get("columns") or []

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT)
    for j, col in enumerate(columns, start=1):
        c = ws.cell(row=1, column=j, value=col.get("header") or f"Column {j}")
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(vertical="center")
    for i, row in enumerate(rows, start=2):
        for j, cell in enumerate(row.cells, start=1):
            xc = ws.cell(row=i, column=j, value=cell.value)
            if cell.type == "number":
                xc.number_format = "#,##0.00"
            elif cell.type == "date":
                xc.number_format = date_format
            fill = FILLS.get(cell.status)
            if fill:
                xc.fill = PatternFill("solid", fgColor=fill)
            if cell.comment:
                xc.comment = Comment(cell.comment, "Verdandi")
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{openpyxl.utils.get_column_letter(len(columns))}{len(rows) + 1}"
    for j, col in enumerate(columns, start=1):
        width = max(10, min(40, len(str(col.get("header") or "")) + 4))
        ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = width

    _issues_sheet(wb, run, openpyxl, PatternFill, Font)
    if options.get("include_line_items_sheet"):
        _line_items_sheet(wb, run, cfg, openpyxl, PatternFill, Font)
    _meta_sheet(wb, run, profile, rows, openpyxl, Font)
    return wb


def _issues_sheet(wb, run: dict, openpyxl, PatternFill, Font) -> None:
    ws = wb.create_sheet("Issues")
    headers = ["File", "File ID", "Entry", "Document", "Field", "Check", "Value",
               "Confidence", "Threshold", "Review", "Note", "Reviewed at",
               "Details", "Code"]
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = Font(bold=True, color=HEADER_FONT)
        c.fill = PatternFill("solid", fgColor=HEADER_FILL)
    r = 2
    for frec in run["files"]:
        for frow in frec["flags"]:
            st, note, at = _review_state(frec["review"], frow)
            reason = frow.get("reason") or ""
            values = [frec["original"], frec["stem"], frow.get("entry", ""),
                      frow.get("template"), frow.get("field"),
                      messages.reason_message(reason, value=frow.get("value"),
                                              field=frow.get("field"),
                                              template=frow.get("template"))["title"],
                      frow.get("value"), frow.get("confidence"), frow.get("threshold"),
                      st, note, at, frow.get("detail"), reason]
            for j, v in enumerate(values, start=1):
                ws.cell(row=r, column=j, value=v)
            r += 1
        # cross-check outcomes (including NOT CHECKED)
        audit = ((frec["data"].get("_meta") or {}).get("verification") or {}
                 ).get("cross_checks") or {}
        for rid, rec in audit.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("passed") is False:
                verdict = "FAIL"
            elif rec.get("skip"):
                verdict = "NOT CHECKED"
            else:
                verdict = "PASS"
            label = rec.get("label") or rid
            values = [frec["original"], frec["stem"], "", "Cross-check", label,
                      "Cross-check", verdict, "", "", "", "", "",
                      _cross_sentence(rid, rec), rid]
            for j, v in enumerate(values, start=1):
                ws.cell(row=r, column=j, value=v)
            r += 1
        # multiplicity notes (first-found grain uses one entry per template)
        by_tpl = defaultdict(int)
        for e in frec["data"].get("documents", []) or []:
            if e.get("found"):
                by_tpl[e.get("template_id")] += 1
        for tpl, n in by_tpl.items():
            if n > 1:
                values = [frec["original"], frec["stem"], "", _humanize(tpl),
                          "Extra entries", "Possible duplicate document", n,
                          "", "", "", "", "",
                          f"This file has {messages.plural(n, 'document')} of this "
                          f"kind. The sheet uses the first; the others are listed "
                          f"here so nothing is hidden.", "multiple_entries"]
                for j, v in enumerate(values, start=1):
                    ws.cell(row=r, column=j, value=v)
                r += 1
    for j in range(1, len(headers) + 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = (
            52 if headers[j - 1] == "Details" else 16)


def _line_items_sheet(wb, run: dict, cfg: dict, openpyxl, PatternFill, Font) -> None:
    ws = wb.create_sheet("Line items")
    headers = ["File", "Stem", "Template", "Item #", "Column", "Value"]
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = Font(bold=True, color=HEADER_FONT)
        c.fill = PatternFill("solid", fgColor=HEADER_FILL)
    r = 2
    for frec in run["files"]:
        for entry in frec["data"].get("documents", []) or []:
            if not entry.get("found"):
                continue
            tpl = entry.get("template_id")
            for i, item in enumerate(entry.get("line_items") or [], start=1):
                for key in ("description", "amount", "total", "taxable", "tax"):
                    if key not in item:
                        continue
                    values = [frec["original"], frec["stem"], tpl, i, key, item.get(key)]
                    for j, v in enumerate(values, start=1):
                        ws.cell(row=r, column=j, value=v)
                    r += 1
    for j in range(1, len(headers) + 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = 18


def _meta_sheet(wb, run: dict, profile: dict, rows: list[Row],
                openpyxl, Font) -> None:
    ws = wb.create_sheet("Meta")
    ws.cell(row=1, column=1, value="Field").font = Font(bold=True)
    ws.cell(row=1, column=2, value="Value").font = Font(bold=True)
    # Counts are per FILE/rule (a grain:line_item sheet repeats them per row).
    total = sum(len(f["flags"]) for f in run["files"])
    pending = sum(1 for f in run["files"] for frow in f["flags"]
                  if _review_state(f["review"], frow)[0] == "pending")
    mismatches = not_checked = 0
    for frec in run["files"]:
        audit = ((frec["data"].get("_meta") or {}).get("verification") or {}
                 ).get("cross_checks") or {}
        for rec in audit.values():
            if not isinstance(rec, dict):
                continue
            if rec.get("passed") is False:
                mismatches += 1
            elif rec.get("skip"):
                not_checked += 1
    prompt = run.get("prompt") or {}
    entries = [
        ("run", str(run["run_dir"])),
        ("run name", run.get("name") or ""),
        ("export profile", f"{profile.get('id')} — {profile.get('label')}"),
        ("workflow", profile.get("workflow") or ""),
        ("grain", f"{profile.get('grain')}"
                  + (f" ({profile.get('line_item_template')})"
                     if profile.get("grain") == "line_item" else "")),
        ("model", run.get("model") or ""),
        ("prompt version", prompt.get("version") or ""),
        ("exported at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("files", len(run["files"])),
        ("data rows", len(rows)),
        ("flags total", total),
        ("flags pending review", pending),
        ("cross-check mismatches", mismatches),
        ("cross-checks not evaluated", not_checked),
        ("", ""),
        ("legend: amber", "Needs review — not confirmed yet"),
        ("legend: red", "An issue you marked, or two documents that disagree"),
        ("legend: grey", "A check that couldn't run (a document or value was "
                         "missing)"),
        ("", ""),
        ("how to read", "Hover any coloured cell to read why it's marked. "
                        "The Issues sheet lists every item with its details."),
    ]
    for i, (k, v) in enumerate(entries, start=2):
        ws.cell(row=i, column=1, value=k)
        ws.cell(row=i, column=2, value=v)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70


def export_run(run_dir: Path, export_id: str, out_path: Path | None = None,
               cfg: dict | None = None) -> Path:
    """Build and save the workbook; returns the written path."""
    cfg = cfg or config.load_all()
    profile = export_profile(cfg, export_id)
    wb = build_workbook(run_dir, profile, cfg)
    run_dir = Path(run_dir)
    if out_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = run_dir / "export" / f"{export_id}_{stamp}.xlsx"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Excel export engine (D54): turn a run's reviewed data "
                    "into a typed, colour-coded workbook per rules/exports.yaml.")
    ap.add_argument("run_dir", help="the run directory (contains json/ + flags.csv)")
    ap.add_argument("--export", dest="export_id", help="export profile id")
    ap.add_argument("--out", default=None, help="output .xlsx path "
                    "(default: <run>/export/<profile>_<timestamp>.xlsx)")
    ap.add_argument("--list", action="store_true", help="list export profiles and exit")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config.load_all()
    if args.list:
        for e in cfg.get("exports", {}).get("exports") or []:
            print(f"{e['id']}  —  {e.get('label')}  [{e.get('grain', 'file')}]")
        return 0
    if not args.export_id:
        print("--export <profile id> is required (or --list)", file=sys.stderr)
        return 2
    try:
        path = export_run(Path(args.run_dir), args.export_id,
                          Path(args.out) if args.out else None, cfg)
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
