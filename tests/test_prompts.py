"""Prompt package tests (D44) — golden fixtures + provenance, offline.

`tests/fixtures/prompts/` pins the EXACT (system, user) bytes for every
variant: text or assembly changes must be deliberate (update the fixtures,
bump PROMPT_VERSION, run the paid eval). The golden comparison is the
tripwire; the provenance tests prove every extraction records which prompt
revision produced it.
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
from prompts import compose, text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prompts"
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
RECHECK_FIELDS = {"invoice_number", "boe_date"}


def _golden(name: str, part: str) -> str:
    return (FIXTURES / f"{name}.{part}.txt").read_text(encoding="utf-8")


def _synthetic_cfg() -> dict:
    cfg = config.load_all()
    cfg["documents"]["documents"].append(json.loads(
        (FIXTURES / "synthetic_generic_profile.json").read_text(encoding="utf-8")))
    return cfg


class TestPromptGolden(unittest.TestCase):
    """Byte-identical assembly for every prompt variant."""

    def test_single_broker_invoice(self):
        system, user = compose.build_single_prompt(config.load_all(), "broker_invoice")
        self.assertEqual(system, _golden("single_broker_invoice", "system"))
        self.assertEqual(user, _golden("single_broker_invoice", "user"))

    def test_single_broker_invoice_pinned(self):
        system, user = compose.build_single_prompt(
            config.load_all(), "broker_invoice", pinned_pages=[1, 2])
        self.assertEqual(system, _golden("single_broker_invoice_pinned", "system"))
        self.assertEqual(user, _golden("single_broker_invoice_pinned", "user"))

    def test_multi_synthetic_pair(self):
        # D53: synthetic profiles — the multi prompt must not depend on the
        # owner's (editable, deletable) shipped templates.
        cfg = {"documents": {"documents": json.loads(
            (FIXTURES / "synthetic_multi_profiles.json").read_text(encoding="utf-8"))}}
        doc_ids = ["alpha_doc", "beta_doc"]
        line_items_map = {d: compose._profile(cfg, d)["extraction"].get("line_items", True)
                          for d in doc_ids}
        system, user = compose.build_multi_prompt(cfg, doc_ids, line_items_map)
        self.assertEqual(system, _golden("multi_synthetic_pair", "system"))
        self.assertEqual(user, _golden("multi_synthetic_pair", "user"))

    def test_generic_fieldless_profile(self):
        system, user = compose.build_single_prompt(_synthetic_cfg(), "generic_test")
        self.assertEqual(system, _golden("generic_test", "system"))
        self.assertEqual(user, _golden("generic_test", "user"))

    def test_catalog(self):
        system, user = compose.build_catalog_prompt()
        self.assertEqual(system, _golden("catalog", "system"))
        self.assertEqual(user, _golden("catalog", "user"))

    def test_recheck_broker_invoice(self):
        cfg = config.load_all()
        fdefs = [fd for fd in config.profile_fields(cfg["documents"], "broker_invoice")
                 if fd["name"] in RECHECK_FIELDS]
        system, user = compose.build_recheck_prompt(cfg, "broker_invoice", fdefs)
        self.assertEqual(system, _golden("recheck_broker_invoice", "system"))
        self.assertEqual(user, _golden("recheck_broker_invoice", "user"))


class TestGenericRouting(unittest.TestCase):
    def test_fieldless_profile_uses_generic_schema(self):
        system, user = compose.build_single_prompt(_synthetic_cfg(), "generic_test")
        self.assertEqual(system, text.GENERIC_SYSTEM_INSTRUCTIONS)
        self.assertIn('"document_title"', user)
        self.assertNotIn("Line items:", user)


class TestPromptProvenance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        raw = Path(self._tmp.name) / "raw" / "doc1"
        raw.mkdir(parents=True)
        (raw / "page001.render-vision.png").write_bytes(PNG_1PX)
        self.raw = Path(self._tmp.name) / "raw"

    def tearDown(self):
        self._tmp.cleanup()

    def test_fingerprint_shape_and_sensitivity(self):
        fp = compose.prompt_fingerprint("system", "user")
        self.assertEqual(fp["version"], text.PROMPT_VERSION)
        self.assertEqual(len(fp["sha256"]), 64)
        self.assertNotEqual(fp["sha256"], compose.prompt_fingerprint("system", "user2")["sha256"])

    def _extract_reply(self) -> str:
        cfg = config.load_all()
        fields = [{"name": f["name"], "value": "V", "confidence": 0.9,
                   "source": "page 1 (render image)",
                   "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.2, 0.2]}}
                  for f in config.profile_fields(cfg["documents"], "broker_invoice")]
        return json.dumps({
            "selected_document": {"name": "X", "document_pages": [1],
                                  "reason": "r", "confidence": 0.9},
            "header_fields": fields, "line_items": [], "notes": [],
            "status": "extracted",
        })

    def test_extraction_records_prompt_fingerprint(self):
        expected = compose.prompt_fingerprint(
            *compose.build_single_prompt(config.load_all(), "broker_invoice"))
        with mock.patch.object(llm_client, "chat_vision_pages",
                               return_value=self._extract_reply()):
            data = extract.extract_file("doc1", self.raw, doc_id="broker_invoice")
        self.assertEqual(data["_meta"]["prompt"], expected)

    def test_narrow_recheck_records_prompt_fingerprint(self):
        cfg = config.load_all()
        fdefs = [fd for fd in config.profile_fields(cfg["documents"], "broker_invoice")
                 if fd["name"] in RECHECK_FIELDS]
        reply = json.dumps({"header_fields": [
            {"name": "invoice_number", "value": "V", "confidence": 0.9,
             "source": "page 1 (render image)",
             "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.2, 0.2]}},
            {"name": "boe_date", "value": "W", "confidence": 0.9,
             "source": "page 1 (render image)",
             "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.2, 0.2]}},
        ]})
        with mock.patch.object(llm_client, "chat_vision_pages", return_value=reply):
            out = extract.recheck_fields("doc1", self.raw, field_defs=fdefs,
                                         pages=[1], model="m",
                                         doc_id="broker_invoice")
        self.assertEqual(out["prompt"]["version"], text.PROMPT_VERSION)
        self.assertEqual(out["prompt"]["sha256"],
                         compose.prompt_fingerprint(
                             *compose.build_recheck_prompt(cfg, "broker_invoice",
                                                           fdefs))["sha256"])


class TestCatalogPrompt(unittest.TestCase):
    """D46: the catalog sweep is domain-neutral and takes free-form types."""

    def test_catalog_prompt_is_domain_neutral(self):
        system, user = compose.build_catalog_prompt()
        for term in ("freight", "HAWB", "BOE", "packing_list", "supplier_bill",
                     "terminal_bill", "broker"):
            self.assertNotIn(term, system + user)

    def test_run_catalog_records_prompt_and_keeps_free_form_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw" / "doc1"
            raw.mkdir(parents=True)
            (raw / "page001.render-vision.png").write_bytes(PNG_1PX)
            reply = json.dumps({"documents": [
                {"type": "tax_invoice", "label": "ACME CORP — TAX INVOICE",
                 "pages": [1]}]})
            with mock.patch.object(llm_client, "chat_vision_pages", return_value=reply):
                rc = extract.run_catalog(Path(tmp) / "raw", Path(tmp) / "catalog",
                                         model="m")
            self.assertEqual(rc, 0)
            out = json.loads((Path(tmp) / "catalog" / "doc1.json").read_text())
            self.assertEqual(out["documents"][0]["type"], "tax_invoice")
            self.assertEqual(out["_meta"]["prompt"]["version"], text.PROMPT_VERSION)


if __name__ == "__main__":
    unittest.main()
