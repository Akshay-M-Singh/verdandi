"""Benchmark containerization guards (benchmark/README.md).

The benchmark must not touch Verdandi: no core module may import it, the
shipped profile file must stay free of the benchmark profile, benchmark data
must stay gitignored, and the canonical schema must match both the benchmark
profile and the synthetic truth. Also unit-tests the scorer's matching logic.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmark"

_CORE_DIRS = ("engines", "prompts", "ui", "tools")
_CORE_FILES = ("config.py", "extract.py", "compare.py", "verify.py", "validate.py",
               "text_layer.py", "aggregate.py", "grounding.py", "containment.py",
               "process.py", "eval.py", "eval_verification.py", "export.py",
               "model_catalog.py", "llm_client.py", "bench.py")


def _benchmark_importers() -> list[str]:
    offenders: list[str] = []
    paths = [ROOT / name for name in _CORE_FILES]
    for directory in _CORE_DIRS:
        paths.extend(sorted((ROOT / directory).rglob("*.py")))
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import benchmark", "from benchmark")):
                offenders.append(str(path.relative_to(ROOT)))
                break
    return offenders


def _load_scorer():
    spec = importlib.util.spec_from_file_location(
        "benchmark_score", BENCH / "score.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_score"] = module
    spec.loader.exec_module(module)
    return module


class TestBenchmarkIsolation(unittest.TestCase):
    def test_no_core_module_imports_benchmark(self):
        self.assertEqual(_benchmark_importers(), [])

    def test_owner_profile_file_has_no_benchmark_profile(self):
        text = (ROOT / "rules" / "documents.yaml").read_text(encoding="utf-8")
        self.assertNotIn("benchmark_document", text)
        self.assertNotIn("benchmark/", text)

    def test_benchmark_data_is_gitignored(self):
        ignore = (BENCH / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/", ignore)

    def test_benchmark_requirements_are_separate(self):
        self.assertTrue((BENCH / "requirements-benchmark.txt").exists())
        pipeline = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("benchmark", pipeline.lower())

    def test_schema_matches_benchmark_profile(self):
        schema = yaml.safe_load((BENCH / "schema.yaml").read_text(encoding="utf-8"))
        profile = yaml.safe_load(
            (BENCH / "rules_documents.yaml").read_text(encoding="utf-8"))
        extraction = profile["documents"][0]["extraction"]
        schema_fields = list(schema["fields"])
        profile_fields = [f["name"] for f in extraction["fields"]]
        self.assertEqual(schema_fields, profile_fields)
        schema_columns = list(schema["line_items"]["columns"])
        profile_columns = [c["name"] for c in extraction["line_items_columns"]]
        self.assertEqual(schema_columns, profile_columns)


class TestSyntheticTruth(unittest.TestCase):
    def test_truth_documents_are_canonical(self):
        truth_dir = BENCH / "synthetic" / "truth"
        docs = sorted(truth_dir.glob("*.json"))
        self.assertGreaterEqual(len(docs), 9)
        schema = yaml.safe_load((BENCH / "schema.yaml").read_text(encoding="utf-8"))
        canonical = set(schema["fields"])
        for path in docs:
            truth = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(truth["stem"], path.stem)
            self.assertEqual(set(truth["fields"]), canonical, path.name)
            self.assertIn(truth["axes"]["language"], ("en", "hi-en", "id"))
            for row in truth.get("line_items") or []:
                self.assertIn("amount", row, path.name)
                self.assertTrue(row["amount"], path.name)

    def test_generated_pdfs_match_truth_when_present(self):
        pdf_dir = BENCH / "data" / "pdfs"
        if not pdf_dir.exists():
            self.skipTest("synthetic PDFs not generated in this checkout")
        truth_stems = {p.stem for p in (BENCH / "synthetic" / "truth").glob("*.json")}
        pdf_stems = {p.stem for p in pdf_dir.glob("syn_*.pdf")}
        self.assertEqual(truth_stems, pdf_stems)


class TestScorerLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scorer = _load_scorer()

    def test_field_semantics(self):
        s = self.scorer
        self.assertTrue(s._field_correct("total_amount", "1,45,679.00", "145679.00"))
        self.assertTrue(s._field_correct("invoice_date", "1999-01-18", "18-Jan-1999"))
        self.assertTrue(s._field_correct("vendor_name", "Bailey Group", "BAILEY GROUP PVT LTD"))
        self.assertTrue(s._field_correct("currency", "USD", "usd"))
        self.assertFalse(s._field_correct("total_amount", "10.00", "20.00"))
        self.assertFalse(s._field_correct("invoice_date", None, "1999-01-18"))
        self.assertFalse(s._field_correct("invoice_number", "AB-1", "AB2"))

    def test_line_item_matching(self):
        s = self.scorer
        truth = [{"description": "Freight", "amount": "100.00"},
                 {"description": "Insurance", "amount": "50.00"}]
        preds = [{"description": "Freight charges", "amount": "100.00"},
                 {"description": "Extra", "amount": "9.00"}]
        matches, unmatched_t, unmatched_p = s._match_items(truth, preds)
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(unmatched_t), 1)
        self.assertEqual(len(unmatched_p), 1)
        self.assertEqual(matches[0][0]["description"], "Freight")
        self.assertEqual(matches[0][1]["description"], "Freight charges")

    def test_scoring_end_to_end(self):
        s = self.scorer
        truths = {"doc": {"truth": {
            "stem": "doc",
            "axes": {"document_type": "invoice", "quality": "clean"},
            "fields": {"invoice_number": "A1", "total_amount": "10.00",
                       "invoice_date": None, "vendor_name": "V", "buyer_name": None,
                       "currency": "USD", "subtotal": None, "tax_total": None},
            "line_items": [{"description": "X", "amount": "10.00"}]}}}
        preds = {"doc": {"fields": {"invoice_number": "A1", "total_amount": "10.00",
                                    "vendor_name": "Wrong"},
                         "line_items": [{"description": "X", "amount": "10.00"}],
                         "found": True}}
        result = s.score(preds, truths)
        self.assertEqual(result["fields"]["invoice_number"]["accuracy"], 100.0)
        self.assertEqual(result["fields"]["vendor_name"]["accuracy"], 0.0)
        self.assertEqual(result["line_items"]["tp"], 1)
        self.assertEqual(result["line_items"]["precision"], 100.0)


if __name__ == "__main__":
    unittest.main()
