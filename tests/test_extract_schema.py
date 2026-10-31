"""Extraction schema robustness tests (D43) — offline, no paid calls.

Two failure classes from the 2026-09-25 qwen3.8-flash run:
- the model returned a top-level JSON ARRAY -> validate_schema crashed with
  "'list' object has no attribute 'get'" and skipped the retry (fixed: a
  non-object response is a normal retryable problem everywhere);
- the model returned out-of-range source_regions on every attempt -> the file
  failed although the VALUES were fine (fixed: on the last attempt, bad boxes
  are dropped, values kept, an invalid_source_region flag is emitted).
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import extract
import llm_client

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class TestNonObjectResponses(unittest.TestCase):
    def test_validate_schema_list_is_a_problem(self):
        problems = extract.validate_schema(["not", "an", "object"], {"a"})
        self.assertEqual(len(problems), 1)
        self.assertIn("not a JSON object", problems[0])

    def test_multi_problems_list_is_a_problem(self):
        problems = extract._multi_problems([], {"t": {"a"}})
        self.assertEqual(len(problems), 1)
        self.assertIn("not a JSON object", problems[0])


class TestBadRegionDegradation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        raw = Path(self._tmp.name) / "raw" / "doc1"
        raw.mkdir(parents=True)
        (raw / "page001.render-vision.png").write_bytes(PNG_1PX)
        self.raw = Path(self._tmp.name) / "raw"
        self.cfg = config.load_all()

    def tearDown(self):
        self._tmp.cleanup()

    def _fields(self, *, confidence=0.9, bbox=None, drop_last=False):
        fields = [{"name": f["name"], "value": "V", "confidence": confidence,
                   "source": "page 1 (render image)",
                   "source_region": {"page": 1, "bbox": bbox or [1.5, 0.0, 0.2, 0.2]}}
                  for f in extract.profile_fields(self.cfg, "broker_invoice")]
        return fields[:-1] if drop_last else fields

    def _reply(self, fields, line_items=None):
        return json.dumps({
            "selected_document": {"name": "X", "document_pages": [1],
                                  "reason": "r", "confidence": 0.9},
            "header_fields": fields, "line_items": line_items or [], "notes": [],
            "status": "extracted",
        })

    def test_bad_regions_only_are_dropped_and_values_kept(self):
        fields = self._fields()
        with mock.patch.object(llm_client, "chat_vision_pages",
                               return_value=self._reply(fields)):
            data = extract.extract_file("doc1", self.raw, doc_id="broker_invoice")
        self.assertEqual(data["status"], "extracted")
        dropped = data["_meta"]["dropped_regions"]
        self.assertEqual(len(dropped), len(fields))
        for f in data["header_fields"]:
            self.assertIsNone(f["source_region"])  # box dropped
            self.assertEqual(f["value"], "V")      # value + confidence kept
            self.assertEqual(f["confidence"], 0.9)

    def test_hard_problem_still_fails(self):
        # bad boxes PLUS an impossible confidence -> no degradation
        fields = self._fields(confidence=5)
        with mock.patch.object(llm_client, "chat_vision_pages",
                               return_value=self._reply(fields)):
            with self.assertRaises(extract.SchemaError):
                extract.extract_file("doc1", self.raw, doc_id="broker_invoice")

    def test_valid_regions_are_untouched(self):
        fields = self._fields(bbox=[0.1, 0.1, 0.2, 0.2])
        with mock.patch.object(llm_client, "chat_vision_pages",
                               return_value=self._reply(fields)):
            data = extract.extract_file("doc1", self.raw, doc_id="broker_invoice")
        self.assertNotIn("dropped_regions", data["_meta"])
        self.assertIsNotNone(data["header_fields"][0]["source_region"])

    def test_bad_row_regions_only_are_dropped(self):
        # D48: header regions valid, the ROW region malformed -> the row keeps
        # its values, the row box is dropped and recorded.
        fields = self._fields(bbox=[0.1, 0.1, 0.2, 0.2])
        rows = [{"description": "HANDLING CHARGES", "taxable": "3500.00",
                 "tax": "630.00", "total": "5000.00", "confidence": 0.9,
                 "source": "page 1 (render image)",
                 "source_region": {"page": 1, "bbox": [5.0, 0.0, 0.1, 0.1]}}]
        with mock.patch.object(llm_client, "chat_vision_pages",
                               return_value=self._reply(fields, rows)):
            data = extract.extract_file("doc1", self.raw, doc_id="broker_invoice")
        self.assertEqual(data["_meta"]["dropped_regions"],
                         [{"template": None, "name": "line_item_1"}])
        self.assertIsNone(data["line_items"][0]["source_region"])
        self.assertEqual(data["line_items"][0]["total"], "5000.00")

    def test_row_with_unknown_column_still_fails(self):
        # Strict column validation (D48): an undeclared row key is a hard problem.
        fields = self._fields(bbox=[0.1, 0.1, 0.2, 0.2])
        rows = [{"description": "X", "bogus": "1", "total": "1.00",
                 "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.2, 0.2]}}]
        with mock.patch.object(llm_client, "chat_vision_pages",
                               return_value=self._reply(fields, rows)):
            with self.assertRaises(extract.SchemaError):
                extract.extract_file("doc1", self.raw, doc_id="broker_invoice")

    def test_recheck_list_response_retries_cleanly(self):
        with mock.patch.object(llm_client, "chat_vision_pages", return_value="[1, 2]"):
            with self.assertRaises(extract.SchemaError) as ctx:
                extract.recheck_fields(
                    "doc1", self.raw,
                    field_defs=[{"name": "invoice_number", "type": "str",
                                 "description": "x"}],
                    pages=[1], model="m", doc_id="broker_invoice", retries=1)
        self.assertIn("not a JSON object", str(ctx.exception))


class TestDroppedRegionFlags(unittest.TestCase):
    def test_flag_row_keeps_value_and_carries_threshold(self):
        cfg = config.load_all()
        data = {
            "header_fields": [
                {"name": "invoice_number", "value": "INV-001",
                 "confidence": 0.97, "source": "page 1 (render image)",
                 "source_region": None},
            ],
            "_meta": {"dropped_regions": [{"template": None,
                                           "name": "invoice_number"}]},
        }
        rows = extract._dropped_region_flags(data, cfg, "stem1", "broker_invoice")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["reason"], "invalid_source_region")
        self.assertEqual(row["field"], "invoice_number")
        self.assertEqual(row["value"], "INV-001")
        self.assertEqual(row["confidence"], 0.97)
        self.assertIsNotNone(row["threshold"])
        self.assertIn("vision:", row["sources"])

    def test_no_dropped_regions_no_rows(self):
        self.assertEqual(
            extract._dropped_region_flags({"_meta": {}}, {}, "s", "d"), [])

    def test_row_flag_uses_the_total_column(self):
        # D48: a dropped ROW region flags with the row's total-role value.
        cfg = config.load_all()
        data = {
            "line_items": [
                {"description": "HANDLING CHARGES", "total": "5000.00",
                 "confidence": 0.91, "source": "page 1 (render image)",
                 "source_region": None},
            ],
            "_meta": {"dropped_regions": [{"template": None,
                                           "name": "line_item_1"}]},
        }
        rows = extract._dropped_region_flags(data, cfg, "stem1", "broker_invoice")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["reason"], "invalid_source_region")
        self.assertEqual(row["field"], "line_item_1")
        self.assertEqual(row["value"], "5000.00")
        self.assertEqual(row["confidence"], 0.91)
        self.assertIsNone(row["threshold"])
        self.assertIn("row", row["detail"])


class TestDropBadRegionsMulti(unittest.TestCase):
    def test_multi_entries(self):
        data = {"documents": [
            {"template_id": "a", "header_fields": [
                {"name": "x", "value": "1", "source_region": {"page": 1, "bbox": [2, 0, 0.1, 0.1]}},
                {"name": "y", "value": "2", "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.2, 0.2]}},
            ], "line_items": [
                {"description": "r1", "total": "1.00", "source_region": {"page": 1, "bbox": [3, 0, 0.1, 0.1]}},
            ]},
            {"template_id": "b", "header_fields": [
                {"name": "z", "value": "3", "source_region": "nonsense"},
            ], "line_items": [
                {"description": "r2", "total": "2.00", "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.2, 0.2]}},
            ]},
        ]}
        dropped = extract._drop_bad_regions(data, multi=True)
        self.assertEqual(dropped, [{"template": "a", "name": "x", "entry": 0},
                                   {"template": "a", "name": "line_item_1", "entry": 0},
                                   {"template": "b", "name": "z", "entry": 1}])
        self.assertIsNone(data["documents"][0]["header_fields"][0]["source_region"])
        self.assertIsNotNone(data["documents"][0]["header_fields"][1]["source_region"])
        self.assertIsNone(data["documents"][0]["line_items"][0]["source_region"])
        self.assertIsNotNone(data["documents"][1]["line_items"][0]["source_region"])


if __name__ == "__main__":
    unittest.main()
