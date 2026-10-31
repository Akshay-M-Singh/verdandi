"""Document-orientation classifier + corrector tests (D64) — offline.

Covers the geometry transform, the PP-LCNet classifier on synthetic pages,
and the `text_layer.correct_orientation` pass (render + text-channel
rotation). Skipped when onnxruntime/Pillow are absent (the pipeline then
keeps the D61 no-rotation behavior).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engines import orientation
import text_layer


def _synthetic_page() -> "object":
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1224, 1584), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26)
    except Exception:
        font = ImageFont.load_default()
    lines = ["TAX INVOICE", "Invoice No. INV-001", "Date: 21-Feb-2025",
             "Total: 7,500.00", "Bank: Example Bank",
             "Terms and conditions apply to this invoice"]
    for i, line in enumerate(lines):
        d.text((70, 80 + i * 60), line, fill="black", font=font)
    return img


class TestGeometry(unittest.TestCase):
    def test_rotate_point_corners(self):
        # bottom-left origin, CCW image rotation
        self.assertEqual(orientation.rotate_point(0, 0, 90), (1.0, 0.0))
        self.assertEqual(orientation.rotate_point(0, 1, 90), (0.0, 0.0))
        self.assertEqual(orientation.rotate_point(0, 1, 180), (1.0, 0.0))
        self.assertEqual(orientation.rotate_point(1, 0, 270), (0.0, 0.0))
        self.assertEqual(orientation.rotate_point(0.3, 0.7, 0), (0.3, 0.7))

    def test_rotate_bbox_roundtrip(self):
        box = (0.05, 0.90, 0.60, 0.93)
        for label in (90, 180, 270):
            back = orientation.rotate_bbox(*orientation.rotate_bbox(*box, label),
                                           (360 - label) % 360)
            for a, b in zip(box, back):
                self.assertAlmostEqual(a, b, places=6)

    def test_rotate_bbox_keeps_bounds(self):
        x0, y0, x1, y1 = orientation.rotate_bbox(0.1, 0.2, 0.4, 0.6, 90)
        self.assertTrue(0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1)


@unittest.skipUnless(orientation.available(),
                     "onnxruntime + Pillow required for the D64 classifier")
class TestClassifier(unittest.TestCase):
    def test_synthetic_rotations(self):
        img = _synthetic_page()
        for deg, want in ((0, 0), (90, 270), (180, 180), (270, 90)):
            got, margin = orientation.classify(img.rotate(deg, expand=True)
                                               if deg else img)
            self.assertEqual(got, want, f"rotation {deg}")
            self.assertGreater(margin, orientation.MARGIN_THRESHOLD)


@unittest.skipUnless(orientation.available(),
                     "onnxruntime + Pillow required for the D64 corrector")
class TestCorrector(unittest.TestCase):
    def _raw(self, tmp: str, label: int) -> tuple[Path, dict]:
        # render = upright rotated by the INVERSE of the expected correction
        img = _synthetic_page().rotate((360 - label) % 360, expand=True)
        raw = Path(tmp)
        img.save(raw / "page001.render-ocr-input.png")
        # fragments in the RENDER frame (inverse of the correction)
        upright = {"top": (0.05, 0.90, 0.60, 0.93),
                   "bottom": (0.05, 0.05, 0.60, 0.08)}
        seg_rows, bbox_rows = [], []
        for i, key in enumerate(("top", "bottom"), start=1):
            rx0, ry0, rx1, ry1 = orientation.rotate_bbox(
                *upright[key], (360 - label) % 360)
            text = "TOP MARKER ALPHA" if key == "top" else "BOTTOM MARKER OMEGA"
            seg_rows.append(f"{i}|{rx0:.4f},{ry0:.4f},{rx1:.4f},{ry1:.4f}|0.90|{text}")
            bbox_rows.append(f"{i}|{rx0:.4f},{ry0:.4f},{rx1:.4f},{ry1:.4f}|0.90|{text}")
        (raw / "page001.segments.txt").write_text("\n".join(seg_rows), encoding="utf-8")
        (raw / "page001.bbox.txt").write_text("\n".join(bbox_rows), encoding="utf-8")
        (raw / "page001.txt").write_text("TOP MARKER ALPHA\nBOTTOM MARKER OMEGA",
                                         encoding="utf-8")
        report = {"pages_report": [{"page": 1, "source": "ocr", "lines": 2,
                                    "words": 5, "orientation": "unresolved",
                                    "orientation_confidence": "sideways-kept-original"}]}
        return raw, report

    def test_corrects_render_and_text_channel(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            raw, report = self._raw(tmp, 270)
            before = Image.open(raw / "page001.render-ocr-input.png").size
            text_layer.correct_orientation(raw, report)
            page = report["pages_report"][0]
            self.assertEqual(page["orientation"], "rotated-270")
            self.assertTrue(str(page["orientation_confidence"]).startswith("doc-ori("))
            after = Image.open(raw / "page001.render-ocr-input.png").size
            self.assertEqual(after, (before[1], before[0]))  # rotated
            txt = (raw / "page001.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(txt[0], "TOP MARKER ALPHA")     # upright order
            self.assertEqual(txt[1], "BOTTOM MARKER OMEGA")
            top = (raw / "page001.bbox.txt").read_text().splitlines()[0]
            geo = [float(v) for v in top.split("|")[1].split(",")]
            self.assertAlmostEqual(geo[1], 0.90, places=2)   # back at the top

    def test_upright_page_is_marked_but_not_rotated(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            raw, report = self._raw(tmp, 0)
            before = Image.open(raw / "page001.render-ocr-input.png").size
            text_layer.correct_orientation(raw, report)
            page = report["pages_report"][0]
            self.assertEqual(page["orientation"], "upright")
            self.assertEqual(Image.open(raw / "page001.render-ocr-input.png").size,
                             before)


if __name__ == "__main__":
    unittest.main()
