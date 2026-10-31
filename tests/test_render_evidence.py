"""Evidence-rendering tests (D36) — Pillow only, no Streamlit, no paid calls.

Covers the crop context window + drawn markers, the page thumbnail, and the
overlay's located-vs-claim logic.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:  # Pillow is an optional capture dependency (requirements-crossplatform.txt)
    from PIL import Image
    import ui.render as R
except ImportError:  # pragma: no cover
    Image = None
    R = None


def _make_raw(tmp: str, width: int = 500, height: int = 700) -> Path:
    raw = Path(tmp) / "raw"
    d = raw / "doc1"
    d.mkdir(parents=True, exist_ok=True)
    for page in (1, 2):
        Image.new("RGB", (width, height), (250, 250, 250)).save(
            d / f"page{page:03d}.render-ocr-input.png")
    return raw


LOCATION = {
    "page": 1, "level": "exact",
    "fragment_bbox": [0.20, 0.30, 0.40, 0.34],
    "line_bbox": [0.20, 0.30, 0.70, 0.34],
    "text": "BOE: 1234567", "lineno": 27, "value_offset": 0.5, "source": "locate",
}
CLAIM = {"page": 1, "bbox": [0.60, 0.80, 0.70, 0.83]}


@unittest.skipUnless(R is not None, "UI deps (Pillow) not installed")
class TestContextCrop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = _make_raw(self._tmp.name)
        self.img = R.page_image(self.raw, "doc1", 1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_located_crop_has_context_and_marker(self):
        crop, note, marker = R.context_crop(self.img, LOCATION)
        self.assertEqual(note, "text-confirmed location")
        self.assertEqual(marker, LOCATION["fragment_bbox"])
        # context window is wider than the tight fragment (>= 30% page width)
        self.assertGreaterEqual(crop.width, int(0.30 * self.img.width))
        self.assertGreater(crop.width, int((0.40 - 0.20) * self.img.width))
        # the located box is drawn on the crop (scan for a non-background pixel)
        self.assertTrue(any(crop.getpixel((x, y)) != (250, 250, 250)
                            for x in range(0, crop.width, 3)
                            for y in range(0, crop.height, 3)))

    def test_partial_note(self):
        loc = {**LOCATION, "level": "partial"}
        _, note, _ = R.context_crop(self.img, loc)
        self.assertIn("likely location", note)

    def test_unconfirmed_uses_claim_with_context(self):
        crop, note, marker = R.context_crop(self.img, None, claim=CLAIM)
        self.assertIn("could not confirm", note)
        self.assertEqual(marker, CLAIM["bbox"])
        # 2x context: crop wider than the claim box alone
        self.assertGreater(crop.width, int((0.70 - 0.60) * self.img.width) * 2)
        # claim box drawn on the crop
        self.assertTrue(any(crop.getpixel((x, y)) != (250, 250, 250)
                            for x in range(0, crop.width, 3)
                            for y in range(0, crop.height, 3)))

    def test_no_region_at_all(self):
        crop, note, marker = R.context_crop(self.img, None, claim=None)
        self.assertEqual(crop.size, self.img.size)
        self.assertIsNone(marker)

    def test_wrong_page_location_draws_claim_only_on_claim_page(self):
        loc = {**LOCATION, "page": 2, "page_mismatch": True}
        crop, note, marker = R.context_crop(self.img, loc)
        # claim page differs -> the claim outline must not be drawn on this crop
        self.assertEqual(marker, LOCATION["fragment_bbox"])
        self.assertTrue(crop.size[0] > 0)


@unittest.skipUnless(R is not None, "UI deps (Pillow) not installed")
class TestEvidenceView(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = _make_raw(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_location_page_and_mismatch_note(self):
        field = {"value": "21/02/2025", "confidence": 0.99, "source_region": CLAIM}
        loc = {**LOCATION, "page": 2, "page_mismatch": True}
        crop, note, page, marker = R.evidence_view(self.raw, "doc1", field, loc)
        self.assertEqual(page, 2)  # located page wins over the claim's page
        self.assertIn("found on page 2; model claimed page 1", note)
        self.assertEqual(marker, LOCATION["fragment_bbox"])

    def test_unconfirmed_falls_back_to_claim_page(self):
        field = {"value": "x", "confidence": 0.9, "source_region": CLAIM}
        crop, note, page, marker = R.evidence_view(self.raw, "doc1", field, None)
        self.assertEqual(page, 1)
        self.assertIn("could not confirm", note)
        self.assertEqual(marker, CLAIM["bbox"])


@unittest.skipUnless(R is not None, "UI deps (Pillow) not installed")
class TestMarkPage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = _make_raw(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_thumbnail_capped_and_marked(self):
        thumb = R.mark_page(self.raw, "doc1", 1, [0.2, 0.3, 0.4, 0.34], max_width=380)
        self.assertIsNotNone(thumb)
        self.assertEqual(thumb.width, 380)
        # marker drawn in gold somewhere on the rect's top edge
        y = int(0.30 * thumb.height)
        xs = range(int(0.20 * thumb.width), int(0.40 * thumb.width))
        self.assertTrue(any(thumb.getpixel((x, y)) != (250, 250, 250) for x in xs))

    def test_no_rect_returns_none(self):
        self.assertIsNone(R.mark_page(self.raw, "doc1", 1, None))


@unittest.skipUnless(R is not None, "UI deps (Pillow) not installed")
class TestDrawOverlay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = _make_raw(self._tmp.name)
        self.img = R.page_image(self.raw, "doc1", 1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_located_field_drawn_on_its_page(self):
        fields = [{"name": "boe_number", "value": "1234567", "confidence": 0.99,
                   "source_region": CLAIM, "_location": LOCATION}]
        overlay, unanchored = R.draw_overlay(self.img, fields, {}, 1)
        self.assertEqual(unanchored, [])
        self.assertEqual(overlay.size, self.img.size)

    def test_field_on_other_page_is_unanchored(self):
        fields = [{"name": "invoice_number", "value": "X", "confidence": 0.9,
                   "source_region": {"page": 3, "bbox": [0.1, 0.1, 0.2, 0.2]}}]
        _, unanchored = R.draw_overlay(self.img, fields, {}, 1)
        self.assertEqual(unanchored, ["invoice_number"])

    def test_claim_only_field_drawn(self):
        fields = [{"name": "pan", "value": "AAAAA0000A", "confidence": 0.9,
                   "source_region": CLAIM}]
        overlay, unanchored = R.draw_overlay(self.img, fields, {}, 1)
        self.assertEqual(unanchored, [])
        # grey claim outline drawn near the claim box top edge
        px = overlay.getpixel((int(0.65 * self.img.width), int(0.80 * self.img.height)))
        self.assertNotEqual(px, (250, 250, 250))


if __name__ == "__main__":
    unittest.main()
