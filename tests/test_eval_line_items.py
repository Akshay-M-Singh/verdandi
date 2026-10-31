"""D47 line-item truth metric tests — offline, no paid calls."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import eval as eval_mod

REPO = Path(__file__).resolve().parent.parent
_CASES_PATH = REPO / "tests" / "fixtures" / "verification" / "corpus_cases.json"
try:
    _CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    _CASES = None
# The corpus truth file quotes the local invoices — local-only, never
# committed (owner rule 2026-10-01). The test self-skips without it.
TRUTH_AVAILABLE = eval_mod.DEFAULT_LINE_ITEM_TRUTH.exists() and _CASES is not None


class TestCompareLineItems(unittest.TestCase):
    def test_rows_and_amounts_ok(self):
        t = {"rows": 2, "amounts": ["1200.00", "800.00"]}
        out = eval_mod.compare_line_items(
            [{"amount": "1200.00"}, {"amount": "800.00"}], t)
        self.assertTrue(out["rows_ok"])
        self.assertEqual(out["amounts_matched"], 2)
        self.assertEqual(out["amounts_truth"], 2)

    def test_missing_row(self):
        t = {"rows": 2, "amounts": ["1200.00", "800.00"]}
        out = eval_mod.compare_line_items([{"amount": "1200.00"}], t)
        self.assertFalse(out["rows_ok"])
        self.assertEqual(out["rows_got"], 1)
        self.assertEqual(out["amounts_matched"], 1)

    def test_wrong_amount(self):
        t = {"rows": 1, "amounts": ["1500.00"]}
        out = eval_mod.compare_line_items([{"amount": "1400.00"}], t)
        self.assertEqual(out["amounts_matched"], 0)

    def test_custom_amount_column(self):
        cols = [{"name": "desc", "type": "str", "role": "description"},
                {"name": "gross", "type": "amount", "role": "amount"}]
        t = {"rows": 1, "amounts": ["118.00"]}
        out = eval_mod.compare_line_items([{"desc": "x", "gross": "118.00"}], t,
                                          columns=cols)
        self.assertEqual(out["amounts_matched"], 1)

    def test_no_truth_entry(self):
        self.assertIsNone(eval_mod.compare_line_items([], None))


@unittest.skipUnless(TRUTH_AVAILABLE, "local corpus truth not on disk")
class TestLineItemTruthFile(unittest.TestCase):
    def test_real_file_loads_and_covers_the_corpus(self):
        truth = eval_mod.load_line_item_truth(eval_mod.DEFAULT_LINE_ITEM_TRUTH)
        self.assertEqual(len(truth), 12)  # 12 corpus files
        self.assertEqual(sum(t["rows"] for t in truth.values()), 15)  # body rows
        # norm_key'd keys (the eval join); spot-check the known hard row
        expected = _CASES["sample_c_1"]["line_item_amount"]
        self.assertEqual(truth["samplec1"]["amounts"], [expected])


if __name__ == "__main__":
    unittest.main()
