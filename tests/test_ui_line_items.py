"""UI helper regression tests (D50) — offline, no browser, no runtime UI.

`ui.lib.find_line_item`: a `line_item_<n>` flag resolves to the row's
amount-role value + region (single- and multi-document runs). The desktop
engine reuses this helper for its Review/Results evidence panes.

(The former `ui.app` draft round-trip tests retired with the Streamlit UI —
D67; the desktop templates service keeps profile dicts verbatim, and
TestTemplatesService asserts anchor/column/rule preservation.)
"""

from __future__ import annotations

import unittest

from ui import lib as L

class TestFindLineItem(unittest.TestCase):
    def _single(self):
        return {
            "line_items": [{"description": "A", "total": "5000.00",
                            "confidence": 0.9,
                            "source_region": {"page": 1, "bbox": [0, 0, 1, 1]}}],
            "_meta": {"document": "broker_invoice"},
        }

    def test_single_document_resolves_amount_role(self):
        f = L.find_line_item(self._single(), "line_item_1")
        self.assertEqual(f["value"], "5000.00")  # amount role = column `total`
        self.assertEqual(f["_kind"], "amount")
        self.assertEqual(f["source_region"]["page"], 1)
        self.assertEqual(f["_template"], "broker_invoice")

    def test_multi_document_template_filter(self):
        data = {"documents": [
            {"template_id": "hawb_doc", "found": True, "line_items": []},
            {"template_id": "broker_invoice", "found": True, "line_items": [
                {"description": "A", "total": "118.00",
                 "source_region": {"page": 2, "bbox": [0.1, 0.1, 0.2, 0.2]}}]},
        ]}
        f = L.find_line_item(data, "line_item_1", "broker_invoice")
        self.assertEqual(f["value"], "118.00")
        self.assertEqual(f["source_region"]["page"], 2)

    def test_out_of_range_and_other_names(self):
        data = self._single()
        self.assertIsNone(L.find_line_item(data, "line_item_9"))
        self.assertIsNone(L.find_line_item(data, "invoice_number"))
        self.assertIsNone(L.find_line_item(data, ""))


if __name__ == "__main__":
    unittest.main()
