"""Text-channel location tests (D36) — offline, deterministic.

`grounding.locate_value` answers the DISPLAY question "where is this value
printed?" (never verification): exact span matches, partial line matches
(OCR-spaced digits / truncated multi-token values), page preference for the
model's claimed page, label ranking, and the persisted grounding `match`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import grounding

PAGE1 = [
    "1|0.10,0.80,0.30,0.82|0.9|BOE: 1234567",
    "2|0.10,0.70,0.60,0.72|0.9|Invoice Number: INV-0001",
    "3|0.10,0.60,0.40,0.62|0.9|WAYBILL 99 8877 6655",
    "4|0.10,0.50,0.50,0.52|0.9|PAN # AAAAA0000A",
]
PAGE2 = [
    "1|0.10,0.90,0.60,0.92|0.9|BOE No 7654321 BOE Date 21/02/2025 CAV Value 99999.00",
    "2|0.10,0.80,0.30,0.82|0.9|7654321 21/02/2025",
    "3|0.10,0.70,0.60,0.72|0.9|Reference: 9988776655 attached",
    "4|0.10,0.60,0.60,0.62|0.9|TOTAL 50.000",
]


def _make_raw(tmp: str, pages: dict[int, list[str]], stem: str = "doc1") -> Path:
    raw = Path(tmp) / "raw"
    d = raw / stem
    d.mkdir(parents=True, exist_ok=True)
    for page, rows in pages.items():
        (d / f"page{page:03d}.segments.txt").write_text(
            "\n".join(rows) + "\n", encoding="utf-8")
    return raw


class TestLocateValue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = _make_raw(self._tmp.name, {1: PAGE1, 2: PAGE2})

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_span(self):
        hits = grounding.locate_value(self.raw, "doc1", "1234567", "code")
        self.assertTrue(hits)
        top = hits[0]
        self.assertEqual((top["page"], top["level"]), (1, "exact"))
        self.assertEqual(top["text"], "BOE: 1234567")
        # top-left flip: bl [0.10,0.80,0.30,0.82] -> tl [0.10,0.18,0.30,0.20]
        self.assertAlmostEqual(top["fragment_bbox"][1], 0.18, places=3)
        self.assertAlmostEqual(top["fragment_bbox"][3], 0.20, places=3)

    def test_spaced_digits_are_exact_since_d62(self):
        # The text channel prints the number with spaces; D62 reconstructs
        # adjacent digit tokens, so this is now an EXACT match (was partial).
        hits = grounding.locate_value(self.raw, "doc1", "9988776655", "code")
        top = next(h for h in hits if h["page"] == 1)
        self.assertEqual(top["level"], "exact")
        self.assertIn("99 8877 6655", top["text"])

    def test_grouped_amount_locates_the_canonical_value(self):
        # 2026-10-06: the model outputs the numeric VALUE; the printed token
        # uses dot grouping — the text channel still locates it.
        hits = grounding.locate_value(self.raw, "doc1", "50000", "amount")
        top = next(h for h in hits if h["page"] == 2)
        self.assertEqual(top["level"], "exact")
        self.assertIn("50.000", top["text"])

    def test_claim_page_preferred_over_exact_elsewhere(self):
        # p1 has the spaced occurrence (where the model read it); p2 has an
        # exact reference. Both are exact since D62 — the claimed page wins
        # (the p2 line is a different document).
        claim = {"page": 1, "bbox": [0.10, 0.36, 0.45, 0.44]}
        hits = grounding.locate_value(self.raw, "doc1", "9988776655", "code", claim=claim)
        self.assertEqual(hits[0]["page"], 1)
        self.assertEqual(hits[0]["level"], "exact")

    def test_wrong_page_jump_and_label_rank(self):
        # Nothing on the claimed page -> the best candidate anywhere; the line
        # carrying a profile label ("BOE Date") beats the bare footer.
        claim = {"page": 1, "bbox": [0.70, 0.05, 0.80, 0.06]}
        hits = grounding.locate_value(self.raw, "doc1", "21/02/2025", "date",
                                      claim=claim, labels=["BOE Date", "BE Date"])
        self.assertEqual(hits[0]["page"], 2)
        self.assertIn("BOE Date", hits[0]["text"])
        self.assertNotEqual(hits[0]["lineno"], 2)  # not the '7654321 21/02/2025' footer

    def test_label_ranking_prefers_named_row(self):
        # Both p2 lines contain the date; the label hit must rank first.
        hits = grounding.locate_value(self.raw, "doc1", "21/02/2025", "date",
                                      labels=["BOE Date"])
        self.assertIn("BOE Date", hits[0]["text"])

    def test_multi_token_truncated_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = _make_raw(tmp, {1: ["1|0.05,0.85,0.30,0.88|0.9|JEBEL ALI FREE ZONE"]})
            hits = grounding.locate_value(
                raw, "doc1", "JEBEL ALI FREE ZONE, UNITED ARAB EMIRATES", "str")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["level"], "partial")

    def test_no_match_returns_empty(self):
        self.assertEqual(grounding.locate_value(self.raw, "doc1", "ZZZZZZZ", "code"), [])

    def test_value_offset_within_wide_fragment(self):
        # '21/02/2025' sits mid-fragment; the last token sits at the end.
        hits = grounding.locate_value(self.raw, "doc1", "21/02/2025", "date")
        top = hits[0]
        self.assertGreater(top["value_offset"], 0.3)
        self.assertLess(top["value_offset"], 0.8)
        off_end = grounding._value_offset("99999.00", top["text"])
        self.assertGreater(off_end, 0.7)

    def test_page_scan_uses_all_pages(self):
        hits = grounding.locate_value(self.raw, "doc1", "1234567", "code", pages=[2])
        self.assertEqual(hits, [])


class TestGroundingMatch(unittest.TestCase):
    """The persisted span-level match (audit + UI fallback)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = _make_raw(self._tmp.name, {1: PAGE1})

    def tearDown(self):
        self._tmp.cleanup()

    def test_pass_carries_match(self):
        g = grounding.check_grounding(
            "doc1", 1, "1234567", [0.10, 0.18, 0.30, 0.20], self.raw,
            margin=0.035, search_margin=0.12, loose_score=0.6, kind="code")
        self.assertIsNotNone(g)
        self.assertTrue(g["passed"])
        match = g["match"]
        self.assertEqual(match["page"], 1)
        self.assertEqual(match["text"], "BOE: 1234567")
        self.assertEqual(match["band"], "tight")
        self.assertEqual(match["lineno"], 1)
        self.assertAlmostEqual(match["fragment_bbox"][1], 0.18, places=3)

    def test_failure_has_no_match(self):
        g = grounding.check_grounding(
            "doc1", 1, "ACME-12", [0.5, 0.28, 0.6, 0.3], self.raw,
            margin=0.035, search_margin=0.12, loose_score=0.6, kind="code")
        self.assertIsNotNone(g)
        self.assertFalse(g["passed"])
        self.assertIsNone(g["match"])

    def test_skip_returns_none(self):
        self.assertIsNone(grounding.check_grounding("doc1", 1, "x", None, self.raw))


if __name__ == "__main__":
    unittest.main()
