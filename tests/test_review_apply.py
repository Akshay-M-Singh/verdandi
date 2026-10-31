"""Review write-through tests (D39) — offline.

`apply_review` mirrors a Review-tab action into `json/<stem>.json`: per-field
`review` blocks (with `original_value` on a corrected confirm), a run-level
`_meta.review.entries` summary, and re-application after re-extraction.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ui.lib as L

SINGLE = {
    "header_fields": [
        {"name": "invoice_number", "value": "INV-001", "confidence": 0.9},
        {"name": "boe_number", "value": "1234567", "confidence": 0.8},
    ],
    "line_items": [], "status": "extracted",
    "_meta": {"document": "broker_invoice"},
}

MULTI = {
    "documents": [
        {"template_id": "broker_invoice", "found": True,
         "header_fields": [{"name": "boe_number", "value": "111", "confidence": 0.8}]},
        {"template_id": "bill_of_entry", "found": True,
         "header_fields": [{"name": "boe_number", "value": "111", "confidence": 0.9}]},
    ],
    "status": "extracted_all",
    "_meta": {"documents": ["broker_invoice", "bill_of_entry"]},
}


def _write_json(run: Path, stem: str, data: dict) -> Path:
    d = run / "json"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _field(data: dict, name: str, template: str | None = None) -> dict:
    return L.find_field(data, name, template)


class TestApplyReview(unittest.TestCase):
    def test_confirm_annotates_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            p = _write_json(run, "s1", SINGLE)
            msg = L.apply_review(
                run, {"file": "s1", "template": "broker_invoice",
                      "field": "boe_number", "reason": "verification_grounding"},
                "confirmed", "checked")
            data = json.loads(p.read_text(encoding="utf-8"))
            f = _field(data, "boe_number", "broker_invoice")
            self.assertEqual(f["review"]["status"], "confirmed")
            self.assertEqual(f["review"]["note"], "checked")
            self.assertNotIn("original_value", f["review"])
            self.assertEqual(f["value"], "1234567")  # untouched
            self.assertEqual(data["_meta"]["review"]["confirmed"], 1)
            self.assertEqual(data["_meta"]["review"]["issues"], 0)
            self.assertIn("confirmed", msg)

    def test_confirm_with_edit_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            p = _write_json(run, "s1", SINGLE)
            msg = L.apply_review(
                run, {"file": "s1", "template": "broker_invoice",
                      "field": "boe_number", "reason": "verification_grounding"},
                "confirmed", "fixed typo", value="1234567-A")
            data = json.loads(p.read_text(encoding="utf-8"))
            f = _field(data, "boe_number", "broker_invoice")
            self.assertEqual(f["value"], "1234567-A")
            self.assertEqual(f["review"]["original_value"], "1234567")
            self.assertIn("value corrected", msg)

    def test_confirm_without_change_has_no_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            _write_json(run, "s1", SINGLE)
            L.apply_review(
                run, {"file": "s1", "template": "broker_invoice",
                      "field": "boe_number", "reason": "verification_grounding"},
                "confirmed", "", value="1234567")
            data = json.loads((run / "json" / "s1.json").read_text(encoding="utf-8"))
            self.assertNotIn("original_value",
                             _field(data, "boe_number", "broker_invoice")["review"])

    def test_issue_marks_field_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            p = _write_json(run, "s1", SINGLE)
            L.apply_review(
                run, {"file": "s1", "template": "broker_invoice",
                      "field": "invoice_number", "reason": "verification_format"},
                "issue", "garbled")
            data = json.loads(p.read_text(encoding="utf-8"))
            f = _field(data, "invoice_number", "broker_invoice")
            self.assertEqual(f["review"]["status"], "issue")
            self.assertEqual(f["value"], "INV-001")  # never changed
            self.assertEqual(data["_meta"]["review"]["issues"], 1)
            entry = data["_meta"]["review"]["entries"][0]
            self.assertEqual(entry["reason"], "verification_format")

    def test_multi_doc_targets_the_right_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            p = _write_json(run, "m1", MULTI)
            L.apply_review(
                run, {"file": "m1", "template": "bill_of_entry",
                      "field": "boe_number", "reason": "cross_document_mismatch"},
                "issue", "wrong shipment")
            data = json.loads(p.read_text(encoding="utf-8"))
            broker = _field(data, "boe_number", "broker_invoice")
            boe = _field(data, "boe_number", "bill_of_entry")
            self.assertNotIn("review", broker)
            self.assertEqual(boe["review"]["status"], "issue")

    def test_non_field_row_lands_in_meta_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            p = _write_json(run, "s1", SINGLE)
            L.apply_review(
                run, {"file": "s1", "template": "broker_invoice",
                      "field": "selection", "reason": "no_document_match"},
                "issue", "wrong target")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertNotIn("review", _field(data, "invoice_number", "broker_invoice"))
            entries = data["_meta"]["review"]["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["field"], "selection")
            self.assertEqual(entries[0]["status"], "issue")

    def test_repeat_action_replaces_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            _write_json(run, "s1", SINGLE)
            row = {"file": "s1", "template": "broker_invoice",
                   "field": "boe_number", "reason": "verification_grounding"}
            L.apply_review(run, row, "issue", "first")
            L.apply_review(run, row, "confirmed", "second")
            data = json.loads((run / "json" / "s1.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["_meta"]["review"]["entries"]), 1)
            self.assertEqual(data["_meta"]["review"]["confirmed"], 1)
            self.assertEqual(data["_meta"]["review"]["issues"], 0)

    def test_missing_json_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            msg = L.apply_review(Path(tmp), {"file": "nope", "field": "x"},
                                 "confirmed", "")
            self.assertIn("no JSON", msg)

    def test_apply_reviews_survives_re_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            _write_json(run, "s1", SINGLE)
            row = {"file": "s1", "template": "broker_invoice",
                   "field": "boe_number", "reason": "verification_grounding",
                   "detail": "d"}
            L.save_review(run, {L.flag_key(row): {"status": "confirmed",
                                                  "note": "fixed",
                                                  "value": "FIXED"}})
            # re-extraction overwrites the JSON (review blocks gone)
            _write_json(run, "s1", SINGLE)
            n = L.apply_reviews(run, "s1")
            self.assertEqual(n, 1)
            data = json.loads((run / "json" / "s1.json").read_text(encoding="utf-8"))
            f = _field(data, "boe_number", "broker_invoice")
            self.assertEqual(f["value"], "FIXED")
            self.assertEqual(f["review"]["original_value"], "1234567")

    def test_entry_aware_targets_the_right_occurrence(self):
        # D57: two entries of the SAME template — the flag's entry index pins
        # the occurrence the review writes to.
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            data = {"documents": [
                {"template_id": "broker_invoice", "found": True,
                 "header_fields": [{"name": "boe_number", "value": "111"}]},
                {"template_id": "broker_invoice", "found": True,
                 "header_fields": [{"name": "boe_number", "value": "222"}]},
            ], "status": "extracted_all"}
            p = _write_json(run, "m2", data)
            L.apply_review(
                run, {"file": "m2", "template": "broker_invoice",
                      "field": "boe_number", "reason": "cross_document_mismatch",
                      "entry": "1"},
                "confirmed", "second occurrence", value="222-A")
            out = json.loads(p.read_text(encoding="utf-8"))
            first = out["documents"][0]["header_fields"][0]
            second = out["documents"][1]["header_fields"][0]
            self.assertNotIn("review", first)
            self.assertEqual(second["value"], "222-A")
            self.assertEqual(second["review"]["original_value"], "222")
            self.assertEqual(out["_meta"]["review"]["entries"][0]["entry"], 1)

    def test_flag_key_separates_occurrences(self):
        row0 = {"file": "f", "template": "t", "field": "x", "reason": "r",
                "detail": "d", "entry": 0}
        row1 = dict(row0, entry=1)
        self.assertNotEqual(L.flag_key(row0), L.flag_key(row1))
        self.assertNotEqual(L.flag_key(row0), L.legacy_flag_key(row0))
        review = {L.legacy_flag_key(row0): {"status": "issue"}}
        self.assertEqual(L.review_state(review, row0)["status"], "issue")

    def test_apply_reviews_reads_legacy_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            _write_json(run, "s1", SINGLE)
            row = {"file": "s1", "template": "broker_invoice",
                   "field": "boe_number", "reason": "verification_grounding",
                   "detail": "d"}
            L.save_review(run, {L.legacy_flag_key(row): {"status": "confirmed",
                                                         "note": "old key",
                                                         "value": "OLD-FIX"}})
            n = L.apply_reviews(run, "s1")
            self.assertEqual(n, 1)
            data = json.loads((run / "json" / "s1.json").read_text(encoding="utf-8"))
            self.assertEqual(_field(data, "boe_number", "broker_invoice")["value"],
                             "OLD-FIX")

    def test_apply_reviews_ignores_other_stems(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            _write_json(run, "s1", SINGLE)
            _write_json(run, "s2", SINGLE)
            L.save_review(run, {
                L.flag_key({"file": "s2", "template": "broker_invoice",
                            "field": "boe_number", "reason": "r", "detail": "d"}):
                    {"status": "issue", "note": ""},
            })
            self.assertEqual(L.apply_reviews(run, "s1"), 0)


if __name__ == "__main__":
    unittest.main()
