"""Benchmark-ready eval join tests (D60) — offline, synthetic run dirs.

Covers the manifest-aware truth join, the multi-document scoring view
(`eval._scoring_view`, `eval_verification.read_run_values` + template-pinned
truth cells) and the run-info provenance header (mixed models/prompts).
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import eval as eval_mod
import eval_verification as ev

TRUTH = {
    "fields": ["invoice_number", "boe_number"],
    "quirk": set(),
    "docs": {
        "invoice_a": {
            "invoice_number": {"value": "INV-1", "page": 1,
                               "template": "broker_invoice"},
            "boe_number": {"value": "BOE-1", "page": 1,
                           "template": "bill_of_entry"},
        },
    },
}


def _multi_data() -> dict:
    return {
        "status": "extracted_all",
        "documents": [
            {"template_id": "broker_invoice", "found": True,
             "selected_document": {"name": "Broker Co", "document_pages": [1]},
             "header_fields": [{"name": "invoice_number", "value": "INV-1",
                                "confidence": 0.99,
                                "source": "page 1 (render image)"}],
             "line_items": [{"amount": "10.00"}]},
            {"template_id": "bill_of_entry", "found": True,
             "selected_document": {"name": "BOE", "document_pages": [2]},
             "header_fields": [{"name": "boe_number", "value": "BOE-2",
                                "confidence": 0.9,
                                "source": "page 2 (render image)"}],
             "line_items": []},
        ],
        "_meta": {"model": "test-model", "prompt": {"version": "v9"}},
    }


def _write_run(tmp: Path) -> Path:
    run = tmp / "r1"
    (run / "json").mkdir(parents=True)
    (run / "raw").mkdir()
    (run / "manifest.json").write_text(json.dumps({"files": [
        {"stem": "abc123", "original": "Invoice_A.pdf", "sha": "x"},
    ]}), encoding="utf-8")
    (run / "json" / "abc123.json").write_text(json.dumps(_multi_data()),
                                              encoding="utf-8")
    headers = ["file", "template", "field", "value", "confidence", "threshold",
               "reason", "sources", "detail", "entry"]
    with (run / "flags.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        w.writerow({"file": "abc123", "template": "bill_of_entry",
                    "field": "boe_number", "value": "BOE-2", "confidence": "0.9",
                    "threshold": "0.8", "reason": "verification_grounding",
                    "sources": "vision", "detail": "wrong read", "entry": "1"})
        # Same field name on the WRONG template — must not count for the
        # bill_of_entry truth cell (template pinning).
        w.writerow({"file": "abc123", "template": "broker_invoice",
                    "field": "boe_number", "value": "BOE-9", "confidence": "0.5",
                    "threshold": "0.8", "reason": "verification_grounding",
                    "sources": "vision", "detail": "other entry", "entry": "0"})
    return run


class TestResolveStemsManifest(unittest.TestCase):
    def test_manifest_maps_original_names_to_hash_stems(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            (raw / "abc123").mkdir(parents=True)
            (Path(tmp) / "manifest.json").write_text(json.dumps({"files": [
                {"stem": "abc123", "original": "Invoice_A.pdf"}]}),
                encoding="utf-8")
            truth = {"invoice_a": {}, "unrelated": {}}
            self.assertEqual(eval_mod.resolve_stems(truth, raw),
                             {"invoice_a": "abc123"})

    def test_corpus_dir_names_still_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            (raw / "invoice_a").mkdir(parents=True)
            self.assertEqual(eval_mod.resolve_stems({"invoice_a": {}}, raw),
                             {"invoice_a": "invoice_a"})


class TestScoringView(unittest.TestCase):
    def test_multi_doc_returns_first_matching_entry(self):
        view = eval_mod._scoring_view(_multi_data())
        self.assertEqual(view["header_fields"][0]["name"], "invoice_number")
        self.assertEqual(view["line_items"], [{"amount": "10.00"}])
        self.assertEqual(view["selected_document"]["name"], "Broker Co")
        self.assertEqual(view["_meta"]["document"], "broker_invoice")

    def test_single_document_passes_through(self):
        data = {"header_fields": [{"name": "x", "value": "1"}]}
        self.assertIs(eval_mod._scoring_view(data), data)

    def test_no_matching_entry_returns_none(self):
        data = {"documents": [{"template_id": "hawb_doc", "found": True,
                               "header_fields": []}]}
        self.assertIsNone(eval_mod._scoring_view(data))


class TestRunInfo(unittest.TestCase):
    def _json(self, d: Path, name: str, model: str, version: str) -> None:
        (d / name).write_text(json.dumps({"_meta": {
            "model": model, "prompt": {"version": version}}}), encoding="utf-8")

    def test_uniform_run_not_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._json(d, "a.json", "luna", "v9")
            self._json(d, "b.json", "luna", "v9")
            info = eval_mod.run_info(d)
            self.assertEqual(info["files"], 2)
            self.assertFalse(info["mixed"])

    def test_mixed_run_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._json(d, "a.json", "luna", "v9")
            self._json(d, "b.json", "deepseek", "v8")
            info = eval_mod.run_info(d)
            self.assertTrue(info["mixed"])
            self.assertEqual(info["models"], {"luna": 1, "deepseek": 1})


class TestMultiDocScoring(unittest.TestCase):
    def test_read_run_values_splits_entries_by_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            values = ev.read_run_values(run)
            self.assertEqual(set(values["abc123"]),
                             {"broker_invoice", "bill_of_entry"})
            self.assertEqual(values["abc123"]["bill_of_entry"]["boe_number"]["value"],
                             "BOE-2")

    def test_template_pinned_truth_cells_score_their_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            buckets = ev.score_run("r1", TRUTH, Path(tmp))
            self.assertEqual([c["field"] for c in buckets["TP"]], ["boe_number"])
            self.assertEqual([c["field"] for c in buckets["TN"]], ["invoice_number"])
            self.assertEqual(buckets["FN"], [])
            self.assertEqual(buckets["FP"], [])

    def test_view_fields_pins_template(self):
        by_tpl = {"a": {"x": {"value": "1"}}, "b": {"x": {"value": "2"}}}
        self.assertEqual(ev._view_fields(by_tpl, {"template": "b"})["x"]["value"], "2")
        self.assertIsNone(ev._view_fields(by_tpl, {"template": "missing"}))
        self.assertEqual(ev._view_fields({"": {"x": {"value": "1"}}}, {})["x"]["value"], "1")
        self.assertEqual(ev._view_fields(by_tpl, {})["x"]["value"], "1")


if __name__ == "__main__":
    unittest.main()
