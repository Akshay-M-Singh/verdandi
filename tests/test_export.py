"""Excel export engine tests (D54) — offline, synthetic run dirs, no LLM.

The synthetic run mirrors the real artifact shapes: multi-document JSON with
`documents[]` entries + `_meta.verification.cross_checks`, flags.csv,
review.json and manifest.json. Workbook assertions need openpyxl (UI dep) and
skip when it is absent; row assembly/status tests are dependency-free.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import export
from ui import lib as L

HAVE_OPENPYXL = importlib.util.find_spec("openpyxl") is not None

CFG = {
    "documents": {"documents": [
        {"id": "broker_invoice", "label": "Broker", "description": "d",
         "signals": {},
         "extraction": {
             "fields": [
                 {"name": "invoice_number", "type": "str", "description": "d"},
                 {"name": "invoice_date", "type": "date", "description": "d"},
                 {"name": "boe_number", "type": "str", "description": "d"},
                 {"name": "total_invoice_amount", "type": "amount", "description": "d"},
             ],
             "line_items": True,
             "line_items_columns": [
                 {"name": "description", "type": "str", "role": "description"},
                 {"name": "amount", "type": "amount", "role": "amount"},
             ],
         }},
        {"id": "bill_of_entry", "label": "BOE", "description": "d",
         "signals": {},
         "extraction": {"fields": [
             {"name": "boe_number", "type": "str", "description": "d"},
             {"name": "boe_date", "type": "date", "description": "d"},
         ], "line_items": False}},
    ]},
}

PROFILE = {
    "id": "t", "label": "T", "grain": "line_item",
    "line_item_template": "broker_invoice",
    "columns": [
        {"header": "File", "source": {"meta": "original"}},
        {"header": "Invoice", "source": {"template": "broker_invoice", "field": "invoice_number"}},
        {"header": "BOE", "source": {"template": "bill_of_entry", "field": "boe_number"}},
        {"header": "Item", "source": {"line_item": "index"}},
        {"header": "Desc", "source": {"line_item": "description"}},
        {"header": "Amount", "source": {"line_item": "amount"}},
        {"header": "Check", "source": {"cross_check": "r1"}},
        {"header": "Validation", "source": {"meta": "validation_status"}},
    ],
    "options": {},
}

FILE_GRAIN_PROFILE = {
    "id": "f", "label": "F", "grain": "file",
    "columns": [
        {"header": "File", "source": {"meta": "original"}},
        {"header": "Invoice", "source": {"template": "broker_invoice", "field": "invoice_number"}},
        {"header": "Pending", "source": {"meta": "flags_pending"}},
    ],
    "options": {},
}

FLAG_HEADERS = ["file", "template", "field", "value", "confidence", "threshold",
                "reason", "sources", "detail", "entry"]


def _entry(tid, fields, items=None, found=True):
    return {"template_id": tid, "found": found,
            "selected_document": {"name": f"doc-{tid}", "document_pages": [1],
                                  "reason": "seen", "confidence": 0.99},
            "header_fields": [{"name": n, "value": v, "confidence": 0.99,
                               "source": "page 1 (render image)"}
                              for n, v in fields.items()],
            "line_items": items or [], "notes": []}


def _make_single_run(tmp: Path) -> Path:
    """One file, single-document JSON shape (no documents[] entries)."""
    run = tmp / "single"
    (run / "json").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"files": [
        {"stem": "s1", "original": "single.pdf", "sha": "ccc"}]}),
        encoding="utf-8")
    (run / "json" / "s1.json").write_text(json.dumps({
        "status": "extracted", "_meta": {"document": "broker_invoice"},
        "selected_document": {"name": "Broker Co", "document_pages": [1],
                              "reason": "seen", "confidence": 0.99},
        "header_fields": [{"name": "invoice_number", "value": "SINV-1",
                           "confidence": 0.99,
                           "source": "page 1 (render image)"}],
        "line_items": [{"description": "Row", "amount": "10.00"}],
        "notes": [],
    }), encoding="utf-8")
    (run / "flags.csv").write_text(",".join(FLAG_HEADERS) + "\n",
                                   encoding="utf-8")
    return run


def _make_run(tmp: Path) -> Path:
    """Two files: a1 (2 items, cross-check skipped + pending flag) and b2
    (1 item, cross-check MISMATCH + reviewed-as-issue flag)."""
    run = tmp / "run"
    (run / "json").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"files": [
        {"stem": "a1", "original": "alpha.pdf", "sha": "aaa"},
        {"stem": "b2", "original": "beta.pdf", "sha": "bbb"},
    ]}), encoding="utf-8")

    a1 = {"status": "extracted_all", "documents": [
        _entry("broker_invoice",
               {"invoice_number": "INV-1", "invoice_date": "01/02/2025",
                "total_invoice_amount": "100.00"},
               [{"description": "Row A", "amount": "60.00"},
                {"description": "Row B", "amount": "40.00"}]),
        _entry("bill_of_entry", {"boe_number": "BOE-9", "boe_date": "2025-02-01"}),
    ], "_meta": {"document": "broker_invoice", "model": "test-model",
                 "prompt": {"version": "v9"},
                 "verification": {"cross_checks": {"r1": {
                     "label": "BOE check",
                     "left": {"template": "broker_invoice", "field": "boe_number"},
                     "right": {"template": "bill_of_entry", "field": "boe_number"},
                     "evaluable": False, "passed": None, "skip": "no_value",
                     "pairs": []}}}}}
    b2 = {"status": "extracted_all", "documents": [
        _entry("broker_invoice",
               {"invoice_number": "INV-2", "invoice_date": "02/02/2025",
                "boe_number": "BOE-7", "total_invoice_amount": "30.00"},
               [{"description": "Row C", "amount": "30.00"}]),
        _entry("bill_of_entry", {"boe_number": "BOE-8", "boe_date": "2025-02-02"}),
    ], "_meta": {"document": "broker_invoice", "model": "test-model",
                 "prompt": {"version": "v9"},
                 "verification": {"cross_checks": {"r1": {
                     "label": "BOE check",
                     "left": {"template": "broker_invoice", "field": "boe_number"},
                     "right": {"template": "bill_of_entry", "field": "boe_number"},
                     "evaluable": True, "passed": False, "skip": None,
                     "pairs": [{"left": "BOE-7", "right": "BOE-8", "equal": False}]}}}}}
    (run / "json" / "a1.json").write_text(json.dumps(a1), encoding="utf-8")
    (run / "json" / "b2.json").write_text(json.dumps(b2), encoding="utf-8")

    flag_a = {"file": "a1", "template": "bill_of_entry", "field": "boe_number",
              "value": "BOE-9", "confidence": "0.9", "threshold": "0.95",
              "reason": "verification_grounding", "sources": "vision",
              "detail": "value NOT found near region", "entry": ""}
    flag_b = {"file": "b2", "template": "bill_of_entry", "field": "boe_number",
              "value": "BOE-8", "confidence": "0.9", "threshold": "0.95",
              "reason": "verification_grounding", "sources": "vision",
              "detail": "value NOT found near region", "entry": ""}
    with (run / "flags.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FLAG_HEADERS)
        w.writeheader()
        w.writerows([flag_a, flag_b])
    (run / "review.json").write_text(json.dumps({
        export.flag_key(flag_b): {"status": "issue", "note": "confirmed wrong BOE",
                                  "reviewed_at": "2026-09-28T00:00:00+00:00"},
    }), encoding="utf-8")
    return run


class TestExportRows(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run = _make_run(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_line_item_grain_expands_and_repeats_headers(self):
        rows = export.build_rows(self.run, PROFILE, CFG)
        self.assertEqual(len(rows), 3)  # a1 has 2 items, b2 has 1
        a = [r for r in rows if r.original == "alpha.pdf"]
        self.assertEqual([c.value for c in a[0].cells], [
            "alpha.pdf", "INV-1", "BOE-9", 1, "Row A", 60.0, "NOT CHECKED",
            a[0].validation])
        self.assertEqual([c.value for c in a[1].cells][3:6], [2, "Row B", 40.0])

    def test_typing(self):
        rows = export.build_rows(self.run, PROFILE, CFG)
        a = rows[0]
        self.assertEqual(a.cells[5].type, "number")
        self.assertIsInstance(a.cells[5].value, float)

    def test_pending_and_issue_statuses(self):
        rows = export.build_rows(self.run, PROFILE, CFG)
        a = rows[0]   # a1: BOE flagged, unreviewed
        b = rows[-1]  # b2: BOE flagged, reviewed as issue (and a mismatch)
        self.assertEqual(a.cells[2].status, "pending")
        # 2026-10-05: comments are reviewer sentences, never raw codes.
        self.assertIn("Value not found on the page", a.cells[2].comment)
        self.assertNotIn("[verification_", a.cells[2].comment)
        # mismatch outranks issue (both are red); the review note is kept.
        self.assertEqual(b.cells[2].status, "mismatch")
        self.assertIn("confirmed wrong BOE", b.cells[2].comment)

    def test_cross_check_states(self):
        rows = export.build_rows(self.run, PROFILE, CFG)
        a = rows[0]
        b = rows[-1]
        self.assertEqual(a.cells[6].value, "NOT CHECKED")
        self.assertEqual(a.cells[6].status, "not_checked")
        self.assertEqual(b.cells[6].value, "FAIL")
        self.assertEqual(b.cells[6].status, "mismatch")
        # the mismatching BOE cells on both sides are red
        self.assertEqual(b.cells[2].status, "mismatch")

    def test_single_document_run_binds_template_columns(self):
        # 2026-10-02 fix: template columns were silently blank for every
        # single-document run (the app's primary shape) — only documents[]
        # entries were consulted.
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_single_run(Path(tmp))
            rows = export.build_rows(run, FILE_GRAIN_PROFILE, CFG)
            self.assertEqual(rows[0].cells[1].raw, "SINV-1")
            self.assertEqual(rows[0].cells[1].status, "ok")

    def test_single_document_foreign_template_is_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_single_run(Path(tmp))
            prof = {
                "id": "x", "label": "X", "grain": "file", "options": {},
                "columns": [{"header": "BOE", "source": {
                    "template": "bill_of_entry", "field": "boe_number"}}],
            }
            rows = export.build_rows(run, prof, CFG)
            self.assertIsNone(rows[0].cells[0].raw)

    def test_legacy_single_document_run_without_meta_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_single_run(Path(tmp))
            data = json.loads((run / "json" / "s1.json").read_text())
            data.pop("_meta")
            (run / "json" / "s1.json").write_text(json.dumps(data),
                                                  encoding="utf-8")
            rows = export.build_rows(run, FILE_GRAIN_PROFILE, CFG)
            self.assertEqual(rows[0].cells[1].raw, "SINV-1")

    def test_file_grain(self):
        rows = export.build_rows(self.run, FILE_GRAIN_PROFILE, CFG)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].cells[0].value, "alpha.pdf")
        self.assertEqual(rows[0].cells[2].value, 1)  # one pending flag

    def test_require_reviewed_gate(self):
        profile = {**PROFILE, "options": {"require_reviewed": True}}
        with self.assertRaises(export.ExportError):
            export.build_rows(self.run, profile, CFG)

    def test_flags_do_not_leak_across_occurrences(self):
        # D57: a flag on documents[1] must not colour the exported (first)
        # occurrence's cell; the Issues sheet owns it.
        jp = self.run / "json" / "a1.json"
        data = json.loads(jp.read_text(encoding="utf-8"))
        data["documents"].insert(1, _entry(
            "broker_invoice",
            {"invoice_number": "INV-1B", "total_invoice_amount": "10.00"},
            [{"description": "Row A2", "amount": "10.00"}]))
        jp.write_text(json.dumps(data), encoding="utf-8")
        with (self.run / "flags.csv").open("a", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FLAG_HEADERS).writerow({
                "file": "a1", "template": "broker_invoice", "field": "invoice_number",
                "value": "INV-1B", "confidence": "0.9", "threshold": "0.8",
                "reason": "verification_grounding", "sources": "vision",
                "detail": "second occurrence", "entry": "1"})
        rows = export.build_rows(self.run, FILE_GRAIN_PROFILE, CFG)
        a1 = next(r for r in rows if r.original == "alpha.pdf")
        self.assertEqual(a1.cells[1].value, "INV-1")   # first occurrence exported
        self.assertEqual(a1.cells[1].status, "ok")     # not coloured by entry 1
        self.assertEqual(a1.flags_total, 2)            # but still counted

    def test_unbound_document_red_until_confirmed(self):
        # D59: a document matching no cross-check candidate is red, not amber;
        # confirming it clears the colour.
        with (self.run / "flags.csv").open("a", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FLAG_HEADERS).writerow({
                "file": "b2", "template": "broker_invoice",
                "field": "invoice_number", "value": "INV-2", "confidence": "0.9",
                "threshold": "", "reason": "unbound_document", "sources": "vision",
                "detail": "matches none of the other side", "entry": "0"})
        rows = export.build_rows(self.run, FILE_GRAIN_PROFILE, CFG)
        b = next(r for r in rows if r.original == "beta.pdf")
        self.assertEqual(b.cells[1].status, "mismatch")
        self.assertIn("doesn't match the others", b.cells[1].comment)
        self.assertIn("not confirmed yet", b.cells[1].comment)
        row = {"file": "b2", "template": "broker_invoice",
               "field": "invoice_number", "reason": "unbound_document",
               "detail": "matches none of the other side", "entry": "0"}
        (self.run / "review.json").write_text(json.dumps({
            **json.loads((self.run / "review.json").read_text(encoding="utf-8")),
            export.flag_key(row): {"status": "confirmed", "note": "checked, fine"},
        }), encoding="utf-8")
        rows = export.build_rows(self.run, FILE_GRAIN_PROFILE, CFG)
        b = next(r for r in rows if r.original == "beta.pdf")
        self.assertEqual(b.cells[1].status, "ok")
        self.assertIn("confirmed", b.cells[1].comment)

    def test_missing_value_flag_colours_the_cell(self):
        # D55: a missing_value flag on a null field must show as pending.
        jp = self.run / "json" / "a1.json"
        data = json.loads(jp.read_text(encoding="utf-8"))
        for f in data["documents"][0]["header_fields"]:
            if f["name"] == "total_invoice_amount":
                f["value"] = None
        jp.write_text(json.dumps(data), encoding="utf-8")
        with (self.run / "flags.csv").open("a", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FLAG_HEADERS).writerow({
                "file": "a1", "template": "broker_invoice",
                "field": "total_invoice_amount", "value": "", "confidence": "0.0",
                "threshold": "0.95", "reason": "missing_value",
                "sources": "profile", "detail": "declared field has no value",
                "entry": ""})
        profile = {**PROFILE, "columns": [
            {"header": "File", "source": {"meta": "original"}},
            {"header": "Total", "source": {"template": "broker_invoice",
                                           "field": "total_invoice_amount"}},
            {"header": "Validation", "source": {"meta": "validation_status"}},
        ]}
        rows = export.build_rows(self.run, profile, CFG)
        cell = rows[0].cells[1]
        self.assertIsNone(cell.value)
        self.assertEqual(cell.status, "pending")
        self.assertIn("Nothing was extracted", cell.comment)
        # The Validation cell mirrors the worst status (amber) too.
        self.assertEqual(rows[0].cells[2].status, "pending")


@unittest.skipUnless(HAVE_OPENPYXL, "openpyxl not installed (UI deps optional)")
class TestWorkbook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run = _make_run(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_workbook_structure(self):
        from openpyxl import load_workbook
        path = export.export_run(self.run, "t", self.run / "out.xlsx",
                                 cfg={"exports": {"exports": [PROFILE]}, **CFG})
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["Data", "Issues", "Meta"])
        ws = wb["Data"]
        self.assertEqual(ws.max_row, 4)  # header + 3 rows
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertTrue(ws.auto_filter.ref)
        # amber pending on a1's BOE, red issue+mismatch on b2's BOE
        self.assertEqual(ws.cell(row=2, column=3).fill.fgColor.rgb, "00FFF2CC")
        self.assertEqual(ws.cell(row=4, column=3).fill.fgColor.rgb, "00FFC7CE")
        self.assertIsNotNone(ws.cell(row=2, column=3).comment)
        # grey not-checked cross-check column
        self.assertEqual(ws.cell(row=2, column=7).fill.fgColor.rgb, "00E7E6E6")
        self.assertEqual(ws.cell(row=2, column=7).value, "NOT CHECKED")
        # validation summary populated (plain phrase, correct plurals)
        self.assertIn("need review", ws.cell(row=2, column=8).value)
        issues = wb["Issues"]
        # 2 flags + 2 cross-check rows; D57 entry column present
        self.assertEqual(issues.max_row - 1, 4)
        self.assertEqual(issues.cell(row=1, column=3).value, "Entry")
        self.assertEqual(issues.cell(row=2, column=4).value, "bill_of_entry")
        meta = wb["Meta"]
        values = {r[0].value: r[1].value for r in meta.iter_rows(min_row=2)}
        self.assertEqual(values["files"], 2)
        self.assertEqual(values["data rows"], 3)
        self.assertEqual(values["flags total"], 2)
        self.assertEqual(values["cross-check mismatches"], 1)
        self.assertEqual(values["cross-checks not evaluated"], 1)

    def test_meta_run_name_from_job_json(self):
        """Desktop runs carry a user-editable name in job.json (D66)."""
        (self.run / "job.json").write_text(
            json.dumps({"id": "abc", "name": "Forwarder July"}),
            encoding="utf-8")
        self.assertEqual(export.load_run(self.run)["name"], "Forwarder July")
        from openpyxl import load_workbook

        path = export.export_run(self.run, "t", self.run / "out.xlsx",
                                 cfg={"exports": {"exports": [PROFILE]}, **CFG})
        meta = load_workbook(path)["Meta"]
        values = {r[0].value: r[1].value for r in meta.iter_rows(min_row=2)}
        self.assertEqual(values["run name"], "Forwarder July")

    def test_meta_run_name_blank_without_job_json(self):
        self.assertEqual(export.load_run(self.run)["name"], "")


class TestFlagKeyContract(unittest.TestCase):
    def test_flag_key_matches_ui_lib(self):
        row = {"file": "a", "template": "t", "field": "f",
               "reason": "r", "detail": "d"}
        self.assertEqual(export.flag_key(row), L.flag_key(row))


if __name__ == "__main__":
    unittest.main()
