"""Non-VLM harness tests (D63): routing / blank detector / quality gate /
orientation on synthetic adversarial PDFs — engine-parameterized.

The engine under test comes from VERDANDI_TEST_ENGINE (default `auto`: Apple
Vision on macOS with swiftc, else the cross-platform engine). CI runs the
same suite on Windows/Linux with `crossplatform`. The TOP/BOTTOM marker order
in the OCR text is the orientation ground truth. Known gaps are
`expectedFailure` with a finding id; Apple-only specifics (normalized-render
labels, raw-image dumps, Vision's framed-page failure) are gated separately.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engines  # noqa: E402
import nonvlm_util as U  # noqa: E402

ENGINE = os.environ.get("VERDANDI_TEST_ENGINE", "auto")
_ENGINE_NAME = "auto"  # resolved in setUpModule

_FIXTURES: dict[str, Path] = {}
_RAW: Path = Path()
_REPORTS: dict[str, dict] = {}
_TMP: tempfile.TemporaryDirectory | None = None


def _engine_ready(name: str) -> bool:
    try:
        resolved = engines.resolve_engine(name)
    except engines.EngineError:
        return False
    if resolved == "apple_vision":
        return U.swift_available()
    return engines.get_engine("crossplatform").available()


def _apple() -> bool:
    return engines.resolve_engine(ENGINE) == "apple_vision"


# Fixtures render with the first available system font (Helvetica/Arial on
# macOS/Windows, DejaVu on Linux). The quality floors below were calibrated
# against the former; on Linux the glyph metrics differ and these assertions
# do not transfer (see follow-up: bundle a fixture font).
_FONT_OK = U.font_calibrated()
_FONT_SKIP = "fixture font differs from the calibrated macOS/Windows fonts"


def setUpModule() -> None:
    global _TMP, _FIXTURES, _RAW, _REPORTS, _ENGINE_NAME
    _ENGINE_NAME = engines.resolve_engine(ENGINE)
    if not _engine_ready(ENGINE):
        return
    _TMP = tempfile.TemporaryDirectory()
    root = Path(_TMP.name)
    _FIXTURES = U.build_fixtures(root / "pdfs")
    _RAW = root / "raw"
    _REPORTS = U.process_all(_FIXTURES, _RAW, engine=ENGINE)


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


@unittest.skipUnless(_engine_ready(ENGINE), f"capture engine {ENGINE!r} unavailable")
class HarnessCase(unittest.TestCase):
    def page(self, name: str) -> dict:
        return _REPORTS[name]["pages_report"][0]

    def text(self, name: str) -> str:
        return U.page_text(_RAW / name)


class TestRouting(HarnessCase):
    def test_clean_digital_is_embedded(self):
        self.assertEqual(self.page("digital")["source"], "embedded")

    def test_digital_over_background_image_is_embedded(self):
        # D15: a decorative full-page image must not force OCR.
        self.assertEqual(self.page("digital_bg")["source"], "embedded")

    def test_invisible_layer_scan_routes_to_ocr(self):
        self.assertEqual(self.page("scan_invisible_layer")["source"], "ocr")

    def test_short_digital_page_routes_to_ocr(self):
        # 27 chars < the 100-char embedded floor -> OCR of the render.
        self.assertEqual(self.page("digital_short")["source"], "ocr")

    def test_blank_page_is_empty(self):
        self.assertEqual(self.page("blank")["source"], "empty")

    @unittest.expectedFailure  # finding F-R1: visible junk layer over a scan
    def test_visible_junk_layer_over_scan_routes_to_ocr(self):
        # The scan underneath is the document; a visible vendor text layer is
        # not authoritative. Currently the junk layer passes the floors and
        # wins (embedded), so the text channel is junk.
        self.assertEqual(self.page("scan_visible_junk")["source"], "ocr")


class TestBlankDetector(HarnessCase):
    def test_truly_blank_page(self):
        self.assertEqual(self.page("blank")["quality"], "blank")

    def test_white_image_scan(self):
        self.assertEqual(self.page("white_scan")["quality"], "blank")

    def test_near_white_speckle_still_blank(self):
        self.assertEqual(self.page("light_gray_scan")["quality"], "blank")

    def test_noisy_non_blank_page_is_poor_not_blank(self):
        p = self.page("noisy_scan")
        self.assertEqual(p["quality"], "poor")
        self.assertIn("unreadable", p["quality_reason"])

    def test_dark_inverted_scan_is_read_but_floor_flagged(self):
        # KNOWN TIGHTNESS (finding F-Q2): 8 readable lines / 39 words miss the
        # 45-word floor and the 0.02 area floor -> excluded from the text
        # channel (still sent to the VLM since D62).
        p = self.page("dark_scan")
        self.assertEqual(p["quality"], "poor")
        self.assertIn("DARK TOP MARKER", self.text("dark_scan"))


class TestVisionChannelFilter(HarnessCase):
    """D62: only blank pages are dropped from the vision channel — poor pages
    (OCR failure on real ink) are sent to the model."""

    def _excluded(self, name: str) -> set:
        import extract
        return extract._excluded_pages(_RAW / name)

    def test_blank_pages_are_excluded(self):
        self.assertEqual(self._excluded("blank"), {1})
        self.assertEqual(self._excluded("white_scan"), {1})

    @unittest.skipUnless(_FONT_OK, _FONT_SKIP)
    def test_poor_pages_are_kept(self):
        self.assertEqual(self.page("framed_scan")["quality"], "poor")
        self.assertEqual(self._excluded("framed_scan"), set())
        self.assertEqual(self._excluded("dark_scan"), set())


class TestQualityGate(HarnessCase):
    @unittest.skipUnless(_FONT_OK, _FONT_SKIP)
    def test_dctdecode_scan_is_good_and_readable(self):
        p = self.page("jpeg_photo_fit")
        self.assertEqual(p["quality"], "good")
        self.assertIn("JPHOTO TOP MARKER ALPHA", self.text("jpeg_photo_fit"))

    @unittest.skipUnless(_apple(), "Apple Vision render specifics")
    def test_dctdecode_scan_uses_normalized_render_and_raw_dump(self):
        p = self.page("jpeg_photo_fit")
        self.assertEqual(p["render"], "normalized-images")
        self.assertTrue((_RAW / "jpeg_photo_fit" / "page001.image.png").exists())

    @unittest.skipUnless(_FONT_OK, _FONT_SKIP)
    def test_framed_scan_is_poor(self):
        # Small print inside a thick frame misses the floors (Apple Vision
        # additionally returns 0 observations — finding F-Q3).
        self.assertEqual(self.page("framed_scan")["quality"], "poor")

    @unittest.skipUnless(_apple(), "Apple Vision render specifics")
    def test_framed_page_vision_failure(self):
        # Finding F-Q3: Vision versions differ (0 lines on the calibration
        # macOS; a few spurious lines on newer ones) — the page must still
        # stay `poor` and kept out of nothing (D62: poor pages reach the VLM).
        self.assertLessEqual(self.page("framed_scan")["lines"], 5)

    @unittest.skipUnless(_FONT_OK, _FONT_SKIP)
    def test_numeric_table_page_is_good(self):
        # D62: digits are script-neutral — a readable numeric sheet passes.
        self.assertEqual(self.page("numeric_table_scan")["quality"], "good")

    def test_sparse_scan_is_poor(self):
        self.assertEqual(self.page("sparse_scan")["quality"], "poor")

    def test_cyrillic_scan_is_poor(self):
        self.assertEqual(self.page("cyrillic_scan")["quality"], "poor")

    @unittest.skipUnless(_apple(), "Vision reads Cyrillic letters -> script floor")
    def test_cyrillic_scan_poor_by_script_purity(self):
        self.assertIn("latin_ratio", self.page("cyrillic_scan")["quality_reason"])

    def test_flate_icc_scan_is_readable(self):
        # D62 (F-R2): FlateDecode + indirect/ICCBased ColorSpace decodes and
        # renders; both engines read the page.
        p = self.page("photo_flate_icc")
        self.assertEqual(p["quality"], "good")
        self.assertIn("PHOTO TOP MARKER ALPHA", self.text("photo_flate_icc"))

    @unittest.skipUnless(_apple(), "Swift decoder specifics (F-R2)")
    def test_flate_icc_decodes_with_normalized_render(self):
        p = self.page("photo_flate_icc")
        self.assertEqual(p["signals"]["image_decode_failures"], 0)
        self.assertEqual(p["render"], "normalized-images")
        self.assertTrue((_RAW / "photo_flate_icc" / "page001.image.png").exists())


class TestOrientation(HarnessCase):
    """D64 contract: the local 4-class doc-ori classifier decides orientation
    (0/90/180/270) and confident pages are rotated upright — render + OCR
    spans + dumps together. Uncertain pages keep their frame (D61 fallback,
    `unresolved`). This replaces the D22 geometry A/B, which could not tell
    upright from upside-down (Vision reads all orientations equally well),
    and closes the pure-180 limitation (F-O2)."""

    def _corrected(self, name: str, label: int) -> None:
        p = self.page(name)
        self.assertEqual(p["orientation"], f"rotated-{label}")
        self.assertTrue(str(p["orientation_confidence"]).startswith("doc-ori("))
        self.assertEqual(U.marker_order(self.text(name)), "upright")

    def test_sideways_print_corrected(self):
        self._corrected("print_rot90", 270)

    def test_sideways_print_opposite_direction_corrected(self):
        self._corrected("print_rot270", 90)

    def test_pure_180_is_now_corrected(self):
        # F-O2 closed: the classifier sees 180° directly.
        self._corrected("print_rot180", 180)

    def test_sideways_sparse_corrected(self):
        self._corrected("sideways_sparse", 270)

    def test_merged_sideways_lines_are_corrected(self):
        # The sideways text merges into one wide OCR line (the old F-O3 trap
        # for the geometry gate); the classifier does not depend on geometry,
        # so the page is corrected and re-assembled upright.
        self._corrected("print_rot90", 270)
        self.assertGreaterEqual(self.page("print_rot90")["lines"], 2)

    def test_two_pages_of_one_file_get_the_same_decision(self):
        rep = _REPORTS["print_rot90_pair"]
        first, second = rep["pages_report"][0], rep["pages_report"][1]
        self.assertEqual(first["orientation"], second["orientation"])
        self.assertEqual(first["orientation_confidence"],
                         second["orientation_confidence"])

    def test_vertical_strip_does_not_rotate_a_upright_page(self):
        p = self.page("mixed_vertical")
        self.assertEqual(p["orientation"], "upright")
        conf = str(p["orientation_confidence"])
        if not conf.startswith("doc-ori("):
            # The D64 classifier's margin is float-backend dependent; when it
            # does not engage the D61 fallback must keep the page upright.
            self.skipTest(f"D64 classifier did not engage on this backend ({conf})")
        self.assertTrue(conf.startswith("doc-ori("))


def _both_engines() -> bool:
    return _apple() and engines.get_engine("crossplatform").available()


@unittest.skipUnless(_both_engines(), "both engines required for parity")
class TestEngineParity(unittest.TestCase):
    """The two engines agree on routing / quality / orientation across the
    fixture suite, except the documented OCR-quality-only diffs (Vision reads
    upside-down text better; Paddle reads the invisible-layer scan slightly
    better). A NEW divergence fails this test loudly."""

    KNOWN_DIFFS = {
        ("scan_invisible_layer", "quality"),
        # PDFKit's /Rotate 90 branch is 180° off (pre-existing D20 caveat, no
        # corpus file uses /Rotate 90/180): the Apple render stays as-is (the
        # classifier is uncertain on the sparse upside-down page), the cross
        # engine renders /Rotate correctly and the corrector rotates it.
        ("digital_rotations", "orientation"),
    }

    def test_fixture_sweep_parity(self):
        root = Path(_TMP.name) / "parity"
        cross = U.process_all(_FIXTURES, root, engine="crossplatform")
        diffs = set()
        for name in _FIXTURES:
            a = _REPORTS[name]["pages_report"][0]
            c = cross[name]["pages_report"][0]
            for key in ("source", "quality", "orientation"):
                if a.get(key) != c.get(key):
                    diffs.add((name, key))
        # KNOWN_DIFFS are ALLOWED differences: a vanished one is fine (engine
        # versions converged), a NEW one still fails loudly.
        self.assertTrue(diffs <= self.KNOWN_DIFFS,
                        f"new engine divergences: {sorted(diffs - self.KNOWN_DIFFS)}")


if __name__ == "__main__":
    unittest.main()
