"""Non-VLM quality-gate + grounding unit tests (2026-09-29 audit) — pure
Python, hermetic, no Swift/LLM.

Covers the D16 cascade boundaries (blank vs poor, every floor at/around its
value, the embedded exemption), the script-purity semantics and the D30
grounding check's coordinate/format behaviour on hand-built text channels.
Known gaps found by the audit are marked `expectedFailure` with a comment —
they document the bug without breaking the suite.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import grounding
import text_layer

Q = {
    "min_lines_per_page": 7,
    "min_words_per_page": 45,
    "min_latin_ratio": 0.5,
    "min_text_area_ratio": 0.02,
    "max_pixel_std_dev": 12.0,
    "max_dark_pixel_ratio": 0.05,
    "review_band": 0.15,
}


def _page(source="ocr", std_dev=30.0, dark=0.2) -> dict:
    return {"source": source, "image_stats": {"std_dev": std_dev, "dark_ratio": dark}}


def _bbox_file(tmp: Path, rows: list[tuple[str, str]]) -> Path:
    path = tmp / "page001.bbox.txt"
    lines = [f"{i + 1}|{geo}|1.00|{text}" for i, (geo, text) in enumerate(rows)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _body(n: int = 8, words_per_line: int = 8) -> list[str]:
    return [" ".join(f"word{i}{j}" for j in range(words_per_line)) for i in range(n)]


class TestLineCategories(unittest.TestCase):
    def test_latin_is_clear(self):
        self.assertEqual(text_layer.line_category("Invoice No. 12345", 0.5), "clear")

    def test_cyrillic_is_trash(self):
        self.assertEqual(
            text_layer.line_category("СЧЕТ ФАКТУРА НОМЕР 12", 0.5), "trash")

    def test_mixed_script_is_noisy(self):
        # ratio 0.5 exactly -> clear (floor is inclusive); below -> noisy
        self.assertEqual(text_layer.line_category("AbcdЕЖЗИ", 0.5), "clear")
        self.assertEqual(text_layer.line_category("AbcЕЖЗИЙ", 0.5), "noisy")

    def test_digit_only_line_is_not_trash(self):
        # D62: digits are script-neutral — a numeric table row is clear.
        self.assertEqual(
            text_layer.line_category("1234 5678 9012 3456", 0.5), "clear")

    def test_numeric_page_latin_ratio_is_neutral(self):
        self.assertEqual(
            text_layer.page_text_stats("1234 5678 9012")["latin_ratio"], 1.0)

    def test_cyrillic_with_digits_is_still_trash(self):
        self.assertEqual(
            text_layer.line_category("СЧЕТ ФАКТУРА 1234", 0.5), "trash")


class TestPageVerdictFloors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_floors_pass(self):
        # 7 lines x 8 words = 56 words; line boxes sum to > 2% page area
        rows = [("0.05,{:.2f},0.55,{:.2f}".format(0.9 - i * 0.04, 0.93 - i * 0.04),
                 t) for i, t in enumerate(_body(7))]
        verdict = text_layer.page_verdict(_page(), _body(7), _bbox_file(self.tmp, rows), Q)
        self.assertEqual(verdict["quality"], "good")

    def test_one_line_below_the_floor_is_poor(self):
        rows = [("0.05,{:.2f},0.55,{:.2f}".format(0.9 - i * 0.04, 0.93 - i * 0.04),
                 t) for i, t in enumerate(_body(6))]
        verdict = text_layer.page_verdict(_page(), _body(6), _bbox_file(self.tmp, rows), Q)
        self.assertEqual(verdict["quality"], "poor")
        self.assertIn("min_lines_per_page", verdict["detail"])

    def test_blank_by_pixels(self):
        verdict = text_layer.page_verdict(_page(std_dev=3.0, dark=0.001), [],
                                          self.tmp / "missing.bbox.txt", Q)
        self.assertEqual(verdict["quality"], "blank")

    def test_no_lines_non_blank_is_poor_not_blank(self):
        verdict = text_layer.page_verdict(_page(), [], self.tmp / "missing.bbox.txt", Q)
        self.assertEqual(verdict["quality"], "poor")
        self.assertIn("unreadable", verdict["detail"])

    def test_no_lines_no_image_is_blank(self):
        verdict = text_layer.page_verdict({"source": "empty"}, [],
                                          self.tmp / "missing.bbox.txt", Q)
        self.assertEqual(verdict["quality"], "blank")

    def test_embedded_is_exempt(self):
        verdict = text_layer.page_verdict(_page(source="embedded"), [],
                                          self.tmp / "missing.bbox.txt", Q)
        self.assertEqual(verdict["quality"], "good")

    def test_sustained_trash_band_fails_even_with_words(self):
        lines = _body(8, 10)
        lines[0:5] = ["СЧЕТ ФАКТУРА НОМЕР " + str(i) for i in range(5)]
        rows = [("0.05,{:.2f},0.55,{:.2f}".format(0.9 - i * 0.04, 0.93 - i * 0.04),
                 t) for i, t in enumerate(lines)]
        verdict = text_layer.page_verdict(_page(), lines, _bbox_file(self.tmp, rows), Q)
        self.assertEqual(verdict["quality"], "poor")
        self.assertIn("trash lines", verdict["detail"])


# ---------------------------------------------------------------------------
# grounding (D30) — hermetic text channel
# ---------------------------------------------------------------------------

SEGMENTS = "\n".join([
    "1|0.1000,0.8000,0.5000,0.8300|1.00|Invoice No. ACME-123 dated 21-Feb-2025",
    "2|0.1000,0.7000,0.3000,0.7300|1.00|Total 1,234.00",
    "3|0.1000,0.6000,0.4000,0.6300|1.00|Split 2,000.00|2.00",
])


def _tl(x0, y0, x1, y1):
    return [x0, 1.0 - y1, x1, 1.0 - y0]


class TestGroundingCoordinates(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self._tmp.name)
        d = self.raw / "f"
        d.mkdir(parents=True)
        (d / "page001.segments.txt").write_text(SEGMENTS, encoding="utf-8")
        (d / "page001.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _check(self, value, bbox, kind="code", page=1):
        return grounding.check_grounding("f", page, value, bbox, self.raw,
                                         margin=0.035, search_margin=0.12,
                                         loose_score=0.6, ngram_k=3, kind=kind)

    def test_own_span_is_tight_pass(self):
        g = self._check("ACME-123", _tl(0.1, 0.8, 0.5, 0.83))
        self.assertTrue(g["passed"])
        self.assertEqual(g["score"], 1.0)
        self.assertEqual(g["match"]["band"], "tight")

    def test_small_shift_still_tight(self):
        g = self._check("ACME-123", _tl(0.13, 0.83, 0.53, 0.86))
        self.assertTrue(g["passed"])
        self.assertEqual(g["score"], 1.0)

    def test_medium_shift_is_loose_pass(self):
        g = self._check("ACME-123", _tl(0.18, 0.88, 0.58, 0.91))
        self.assertTrue(g["passed"])
        self.assertEqual(g["score"], 0.6)

    def test_quarter_page_shift_fails(self):
        g = self._check("ACME-123", _tl(0.35, 0.55, 0.75, 0.58))
        self.assertFalse(g["passed"])

    def test_wrong_page_fails(self):
        g = self._check("ACME-123", _tl(0.1, 0.8, 0.5, 0.83), page=2)
        self.assertFalse(g["passed"])

    def test_row_band_accepts_opposite_column_loosely(self):
        # Documented tradeoff: the loose band extends the ROW, any x.
        g = self._check("ACME-123", _tl(0.85, 0.8, 0.95, 0.83))
        self.assertTrue(g["passed"])
        self.assertEqual(g["score"], 0.6)

    def test_dotted_and_2digit_dates_ground(self):
        for value in ("21.02.2025", "21/02/25", "21-Feb-25"):
            g = self._check(value, _tl(0.1, 0.8, 0.5, 0.83), kind="date")
            self.assertTrue(g["passed"], value)

    def test_amount_normalizes(self):
        g = self._check("1234.00", _tl(0.1, 0.7, 0.3, 0.73), kind="amount")
        self.assertTrue(g["passed"])
        g = self._check("INR 1234.00", _tl(0.1, 0.7, 0.3, 0.73), kind="amount")
        self.assertTrue(g["passed"])

    def test_fused_pipe_token_grounds(self):
        g = self._check("2000.00", _tl(0.1, 0.6, 0.4, 0.63), kind="amount")
        self.assertTrue(g["passed"])

    def test_trailing_period_grounds(self):
        # D62 (F-G1): sentence-ending punctuation on the printed token.
        g = self._check("1,234.00.", _tl(0.1, 0.7, 0.3, 0.73), kind="amount")
        self.assertTrue(g["passed"])

    def test_quoted_value_grounds(self):
        seg = "1|0.1,0.8,0.5,0.83|1.00|Ref 'ACME-123' attached\n"
        d = self.raw / "q"
        d.mkdir()
        (d / "page001.segments.txt").write_text(seg, encoding="utf-8")
        g = grounding.check_grounding("q", 1, "ACME-123", _tl(0.1, 0.8, 0.5, 0.83),
                                      self.raw, margin=0.035, search_margin=0.12,
                                      loose_score=0.6, ngram_k=3, kind="code")
        self.assertTrue(g["passed"])

    def test_multi_token_value_grounds(self):
        # D62 (F-G2): values with spaces reconstruct from adjacent tokens.
        seg = "1|0.1,0.8,0.5,0.83|1.00|Invoice No. ACME CORP here\n"
        d = self.raw / "m"
        d.mkdir()
        (d / "page001.segments.txt").write_text(seg, encoding="utf-8")
        g = grounding.check_grounding("m", 1, "ACME CORP", _tl(0.1, 0.8, 0.5, 0.83),
                                      self.raw, margin=0.035, search_margin=0.12,
                                      loose_score=0.6, ngram_k=3, kind="str")
        self.assertTrue(g["passed"])

    def test_spaced_digits_ground(self):
        # D62 (F-G3): OCR splits digits; the reconstruction must confirm.
        seg = "1|0.1,0.8,0.5,0.83|1.00|WAYBILL 1 234 567 here\n"
        d = self.raw / "g"
        d.mkdir()
        (d / "page001.segments.txt").write_text(seg, encoding="utf-8")
        g = grounding.check_grounding("g", 1, "1234567", _tl(0.1, 0.8, 0.5, 0.83),
                                      self.raw, margin=0.035, search_margin=0.12,
                                      loose_score=0.6, ngram_k=3, kind="code")
        self.assertTrue(g["passed"])

    def test_short_digits_do_not_join(self):
        # Guard: "12" must not match a window "1 2".
        from containment import exact_containment
        self.assertFalse(exact_containment("12", "qty 1 2 pcs", "code"))

    def test_no_region_skips(self):
        self.assertIsNone(self._check("ACME-123", None))


if __name__ == "__main__":
    unittest.main()
