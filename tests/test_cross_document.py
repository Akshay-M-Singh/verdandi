"""Cross-document validation rule executor tests (2026-09-07, D32; D52).

Pure offline tests — no paid calls, no raw text channel (the cross rule only
compares extracted values across entries; grounding of each value is the
per-field stack's business). Exercises check_cross_document against hand-built
rule dicts over the remaining profiles and against rules loaded from a TEMP
workflow (the shipped BOE demo workflow was removed 2026-09-28 together with
the bill_of_entry template).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config
from verify import check_cross_document

# A synthetic chain over the remaining profiles. Built inline so the tests
# are independent of user-editable rule ids in workflows.yaml.
CHAIN_RULES = [
    {"id": "hawb_binds_invoice",
     "left": {"template": "broker_invoice", "field": "invoice_number"},
     "right": {"template": "hawb_doc", "field": "hawb_number"}},
    {"id": "supplier_binds_invoice",
     "left": {"template": "broker_invoice", "field": "invoice_number"},
     "right": {"template": "supplier_invoice", "field": "invoice_number"}},
    {"id": "date_binds_hawb",
     "left": {"template": "broker_invoice", "field": "invoice_date"},
     "right": {"template": "hawb_doc", "field": "hawb_date"}},
]

WF_YAML = """workflows:
- id: synthetic_chain
  label: Synthetic Chain
  documents: [broker_invoice, hawb_doc, supplier_invoice]
  cross_checks:
  - id: hawb_binds_invoice
    left: {template: broker_invoice, field: invoice_number}
    right: {template: hawb_doc, field: hawb_number}
  - id: supplier_binds_invoice
    left: {template: broker_invoice, field: invoice_number}
    right: {template: supplier_invoice, field: invoice_number}
  - id: date_binds_hawb
    left: {template: broker_invoice, field: invoice_date}
    right: {template: hawb_doc, field: hawb_date}
"""


def loaded_rules() -> tuple[dict, list[dict]]:
    """Rules as loaded from a workflow file (config path, not hand-built)."""
    cfg = config.load_all()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "workflows.yaml"
        path.write_text(WF_YAML, encoding="utf-8")
        wf = config.load_workflows(path)["workflows"][0]
    return cfg, wf["cross_checks"]


def entry(tid: str, fields: dict, found: bool = True, pages: list[int] | None = None) -> dict:
    return {
        "template_id": tid,
        "found": found,
        "selected_document": {"name": f"doc-{tid}",
                              "document_pages": pages or [1],
                              "reason": "seen", "confidence": 0.99},
        "header_fields": [{"name": n, "value": v, "confidence": 0.99,
                           "source": "page 1 (render image)"}
                          for n, v in fields.items()],
        "line_items": [], "notes": [],
    }


def chain_data(invoice="INV-1", hawb="INV-1", supplier="INV-1",
               invoice_date="30-Apr-2025", hawb_date="30/04/2025") -> dict:
    return {"documents": [
        entry("broker_invoice", {"invoice_number": invoice,
                                 "invoice_date": invoice_date}),
        entry("hawb_doc", {"hawb_number": hawb, "hawb_date": hawb_date}),
        entry("supplier_invoice", {"invoice_number": supplier}),
    ]}


def coher(self, data: dict, rules=None, cfg=None) -> tuple[list[dict], dict]:
    cfg = cfg or config.load_all()
    rules = rules if rules is not None else CHAIN_RULES
    return check_cross_document("f", data, cfg, rules)


class TestCrossDocumentBasics(unittest.TestCase):
    def test_rule_column_in_flag_contract(self):
        # 2026-10-04: flags.csv carries a `rule` column so the UI can group
        # both sides of a cross check (additive; blank for other reasons).
        from compare import FLAG_COLUMNS

        self.assertIn("rule", FLAG_COLUMNS)

    def test_all_match_passes(self):
        rows, rec = coher(self, chain_data())
        self.assertEqual(rows, [])
        for r in rec.values():
            self.assertTrue(r["evaluable"])
            self.assertTrue(r["passed"])

    def test_mismatch_flags_both_sides(self):
        rows, rec = coher(self, chain_data(supplier="OTHER"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["reason"] == "cross_document_mismatch" for r in rows))
        self.assertEqual({r["template"] for r in rows},
                         {"broker_invoice", "supplier_invoice"})
        self.assertEqual({r["field"] for r in rows}, {"invoice_number"})
        # The other two rules still pass (independent firing).
        self.assertTrue(rec["hawb_binds_invoice"]["passed"])
        self.assertTrue(rec["date_binds_hawb"]["passed"])

    def test_mismatch_rows_carry_rule_id(self):
        rows, _ = coher(self, chain_data(supplier="OTHER"))
        self.assertTrue(all(r.get("rule") == "supplier_binds_invoice"
                            for r in rows))

    def test_failure_flags_every_candidate(self):
        # Two supplier invoices, neither matches the broker reference -> one
        # row PER candidate entry (not just the first), so every occurrence is
        # reviewable.
        data = {"documents": [
            entry("broker_invoice", {"invoice_number": "INV-1"}),
            entry("supplier_invoice", {"invoice_number": "OTHER-1"}),
            entry("supplier_invoice", {"invoice_number": "OTHER-2"}),
        ]}
        rows, _rec = coher(self, data)
        mismatch = [r for r in rows if r["reason"] == "cross_document_mismatch"]
        self.assertEqual(
            sorted((r["template"], r["entry"]) for r in mismatch),
            [("broker_invoice", 0), ("supplier_invoice", 1),
             ("supplier_invoice", 2)])
        self.assertTrue(all(r["rule"] == "supplier_binds_invoice"
                            for r in mismatch))

    def test_single_candidate_detail_has_no_entry_jargon(self):
        # 1x1 mismatch: the detail names both sides plainly (no "entry N").
        rows, _ = coher(self, chain_data(supplier="OTHER"))
        row = next(r for r in rows if r["template"] == "broker_invoice")
        self.assertNotIn("entry", row["detail"])
        self.assertIn("supplier_invoice.invoice_number", row["detail"])

    def test_workflow_loaded_rules_still_run(self):
        # Rules loaded through the workflow config path execute identically.
        cfg, rules = loaded_rules()
        rows, rec = coher(self, chain_data(), rules, cfg)
        self.assertEqual(rows, [])
        self.assertTrue(all(r["evaluable"] and r["passed"] for r in rec.values()))

    def test_missing_side_is_silent(self):
        data = {"documents": [entry("broker_invoice", {"invoice_number": "INV-1"})]}
        rows, rec = coher(self, data)
        self.assertEqual(rows, [])
        self.assertEqual(rec["hawb_binds_invoice"]["skip"], "template_not_found")
        self.assertFalse(rec["hawb_binds_invoice"]["evaluable"])

    def test_no_value_is_silent(self):
        data = {"documents": [
            entry("broker_invoice", {"invoice_number": None, "invoice_date": None}),
            entry("hawb_doc", {"hawb_number": None, "hawb_date": None}),
            entry("supplier_invoice", {"invoice_number": None}),
        ]}
        rows, rec = coher(self, data)
        self.assertEqual(rows, [])
        self.assertEqual(rec["hawb_binds_invoice"]["skip"], "no_value")
        self.assertEqual(rec["supplier_binds_invoice"]["skip"], "no_value")

    def test_found_false_entries_ignored(self):
        data = {"documents": [
            entry("broker_invoice", {"invoice_number": "INV-1"}),
            entry("hawb_doc", {"hawb_number": "INV-1"}, found=False),
        ]}
        rows, rec = coher(self, data)
        self.assertEqual(rows, [])
        self.assertEqual(rec["hawb_binds_invoice"]["skip"], "template_not_found")

    def test_any_one_candidate_matches_passes(self):
        # Two commercial invoices in the file; one matches the broker's ref.
        data = {"documents": [
            entry("broker_invoice", {"invoice_number": "INV-1",
                                     "invoice_date": "30-Apr-2025"}),
            entry("hawb_doc", {"hawb_number": "INV-1", "hawb_date": "30/04/2025"}),
            entry("supplier_invoice", {"invoice_number": "OTHER"}),
            entry("supplier_invoice", {"invoice_number": "INV-1"}),
        ]}
        rows, rec = coher(self, data)
        self.assertTrue(rec["supplier_binds_invoice"]["passed"])
        # D59: pass-if-any keeps the rule passing, but the extra document that
        # matches nothing is surfaced for review.
        self.assertEqual([r["reason"] for r in rows], ["unbound_document"])
        self.assertEqual(rows[0]["entry"], 2)

    def test_flags_carry_entry_index(self):
        # D57: a flag anchors on its own documents[] occurrence; the audit
        # pairs keep both sides' entry indices.
        rows, rec = coher(self, chain_data(supplier="OTHER"))
        by_tpl = {r["template"]: r for r in rows}
        self.assertEqual(by_tpl["broker_invoice"]["entry"], 0)
        self.assertEqual(by_tpl["supplier_invoice"]["entry"], 2)
        pair = rec["supplier_binds_invoice"]["pairs"][0]
        self.assertEqual(pair["left_entry"], 0)
        self.assertEqual(pair["right_entry"], 2)

    def test_unbound_entry_flagged_when_rule_passes(self):
        # D59: two supplier invoices, one matches the broker's reference, the
        # other matches nothing — pass-if-ANY keeps the rule passing but the
        # extra document is flagged (it may not pertain to this shipment).
        data = {"documents": [
            entry("broker_invoice", {"invoice_number": "INV-1",
                                     "invoice_date": "30-Apr-2025"}),
            entry("hawb_doc", {"hawb_number": "INV-1", "hawb_date": "30/04/2025"}),
            entry("supplier_invoice", {"invoice_number": "OTHER"}),
            entry("supplier_invoice", {"invoice_number": "INV-1"}),
        ]}
        rows, rec = coher(self, data)
        self.assertTrue(rec["supplier_binds_invoice"]["passed"])
        unbound = [r for r in rows if r["reason"] == "unbound_document"]
        self.assertEqual(len(unbound), 1)
        self.assertEqual(unbound[0]["template"], "supplier_invoice")
        self.assertEqual(unbound[0]["entry"], 2)
        self.assertEqual(unbound[0]["rule"], "supplier_binds_invoice")
        self.assertIn("entry 2", unbound[0]["detail"])
        self.assertEqual(rec["supplier_binds_invoice"]["unbound"]["right"],
                         [{"entry": 2, "value": "OTHER",
                           "source": "page 1 (render image)"}])

    def test_unbound_empty_when_all_match(self):
        rows, rec = coher(self, chain_data())
        self.assertEqual(rows, [])
        self.assertEqual(rec["supplier_binds_invoice"]["unbound"],
                         {"left": [], "right": []})

    def test_no_unbound_rows_on_total_mismatch(self):
        # A failing rule already fires cross_document_mismatch for both sides;
        # D59 adds no rows (the audit still records what matched nothing).
        rows, rec = coher(self, chain_data(supplier="OTHER"))
        self.assertTrue(all(r["reason"] == "cross_document_mismatch" for r in rows))
        self.assertEqual(rec["supplier_binds_invoice"]["unbound"]["right"][0]["entry"],
                         2)

    def test_chain_fires_independently_on_broken_anchor(self):
        # A wrong broker reference surfaces every downstream mismatch at once.
        rows, rec = coher(self, chain_data(invoice="INV-9", hawb="OTHER-HAWB",
                                           supplier="OTHER-CI",
                                           hawb_date="01/01/2020"))
        self.assertEqual(len(rows), 6)
        failed = {rid for rid, r in rec.items() if r["evaluable"] and not r["passed"]}
        self.assertEqual(failed, {"hawb_binds_invoice", "supplier_binds_invoice",
                                  "date_binds_hawb"})


class TestCrossDocumentNormalization(unittest.TestCase):
    def test_amount_kind_normalized(self):
        cfg = config.load_all()
        rules = [{"id": "amt", "left": {"template": "broker_invoice",
                                        "field": "total_invoice_amount"},
                  "right": {"template": "supplier_invoice",
                            "field": "total_amount"}}]
        data = {"documents": [
            entry("broker_invoice", {"total_invoice_amount": "1,234.00"}),
            entry("supplier_invoice", {"total_amount": "1234.00"}),
        ]}
        rows, rec = coher(self, data, rules, cfg)
        self.assertEqual(rows, [])
        self.assertTrue(rec["amt"]["passed"])

    def test_amount_mismatch_flags(self):
        cfg = config.load_all()
        rules = [{"id": "amt", "left": {"template": "broker_invoice",
                                        "field": "total_invoice_amount"},
                  "right": {"template": "supplier_invoice",
                            "field": "total_amount"}}]
        data = {"documents": [
            entry("broker_invoice", {"total_invoice_amount": "1234.00"}),
            entry("supplier_invoice", {"total_amount": "1235.00"}),
        ]}
        rows, rec = coher(self, data, rules, cfg)
        self.assertEqual(len(rows), 2)
        self.assertFalse(rec["amt"]["passed"])

    def test_date_kind_normalized(self):
        cfg = config.load_all()
        rules = [{"id": "dt", "left": {"template": "broker_invoice",
                                       "field": "invoice_date"},
                  "right": {"template": "hawb_doc", "field": "hawb_date"}}]
        data = {"documents": [
            entry("broker_invoice", {"invoice_date": "30-Apr-2025"}),
            entry("hawb_doc", {"hawb_date": "30/04/2025"}),
        ]}
        rows, rec = coher(self, data, rules, cfg)
        self.assertEqual(rows, [])
        self.assertEqual(rec["dt"]["kind"], "date")
        self.assertTrue(rec["dt"]["passed"])

    def test_dotted_and_2digit_dates_normalized(self):
        # D58: dotted dates and 2-digit years (common on printed BOEs) now
        # normalize like the slash/dash forms.
        from compare import norm_date
        self.assertEqual(norm_date("04.07.2024"), "2024-07-04")
        self.assertEqual(norm_date("4.7.24"), "2024-07-04")
        self.assertEqual(norm_date("04/07/24"), "2024-07-04")
        self.assertEqual(norm_date("04-07-24"), "2024-07-04")
        self.assertEqual(norm_date("04-Jul-24"), "2024-07-04")
        self.assertEqual(norm_date("4 Jul 24"), "2024-07-04")
        self.assertEqual(norm_date("04 July 2024"), "2024-07-04")
        self.assertEqual(norm_date("04-July-2024"), "2024-07-04")
        self.assertEqual(norm_date("2024-07-04"), "2024-07-04")
        self.assertIsNone(norm_date("not a date"))

    def test_cross_check_dotted_dates_match(self):
        cfg = config.load_all()
        rules = [{"id": "dt", "left": {"template": "broker_invoice",
                                       "field": "invoice_date"},
                  "right": {"template": "hawb_doc", "field": "hawb_date"}}]
        data = {"documents": [
            entry("broker_invoice", {"invoice_date": "04.07.2024"}),
            entry("hawb_doc", {"hawb_date": "04-Jul-24"}),
        ]}
        rows, rec = coher(self, data, rules, cfg)
        self.assertEqual(rows, [])
        self.assertTrue(rec["dt"]["passed"])

    def test_code_kind_strict(self):
        cfg = config.load_all()
        rules = [{"id": "code", "left": {"template": "broker_invoice",
                                         "field": "boe_number"},
                  "right": {"template": "hawb_doc",
                            "field": "hawb_number"}}]
        data = {"documents": [
            entry("broker_invoice", {"boe_number": "1234567"}),
            entry("hawb_doc", {"hawb_number": "1234567 "}),
        ]}
        rows, rec = coher(self, data, rules, cfg)
        self.assertEqual(rows, [])
        self.assertTrue(rec["code"]["passed"])


if __name__ == "__main__":
    unittest.main()
