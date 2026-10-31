"""Verification stack unit tests (D30, 2026-09-02).

Covers: containment (plain-string, kind-aware), grounding bands + coordinate
flip + skip semantics, arithmetic rules (incl. a source rounding quirk),
format validators, aggregation (weights/hard-fail/floors), and the config
contract for the verification block + profile arithmetic rules.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import aggregate
import containment
import config
import grounding
from validate import check_arithmetic, check_format, profile_arithmetic_rules

REPO = Path(__file__).resolve().parent.parent
# Corpus-coupled tests read a STABLE capture, not out/latest (which the UI
# workflow runs keep re-pointing at fresh in-progress runs). The 2026-09-03/5
# capture is the canonical cap-1536 full corpus (all 12 files, embedded
# geometry). BOTH the capture and the case values (which quote the local
# invoices) are local-only and NEVER committed — the tests self-skip without
# them (owner rule 2026-10-01).
CORPUS_RAW = REPO / "out" / "runs" / "2026-09-03" / "5" / "raw"
CASES_PATH = REPO / "tests" / "fixtures" / "verification" / "corpus_cases.json"
try:
    CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    CASES = None
CORPUS_AVAILABLE = CORPUS_RAW.is_dir() and CASES is not None


class TestContainment(unittest.TestCase):
    def test_exact_code_token(self):
        self.assertTrue(containment.exact_containment(
            "INV-001", "Invoice No. : INV-001 Date ...", "code"))

    def test_code_drift_fails(self):
        # A claimed reference missing a printed leading character: strict
        # matching must NOT pass it.
        self.assertFalse(containment.exact_containment(
            "ACME-12", "Vessel No. XACME-12 Tax", "code"))

    def test_amount_comma_format(self):
        self.assertTrue(containment.exact_containment(
            "7777.00", "Total 7,777.00", "amount"))
        self.assertFalse(containment.exact_containment(
            "1234.99", "Total 1,234.00", "amount"))

    def test_date_normalized(self):
        self.assertTrue(containment.exact_containment(
            "26/06/2025", "INVOICE DATE: 26/06/2025", "date"))

    def test_date_drift_fails(self):
        # A one-day date drift: strict matching must flag it.
        self.assertFalse(containment.exact_containment(
            "18/06/2025", "ISSUE DATE: 16/06/2025", "date"))

    def test_ngram_proximity_evidence(self):
        # OCR noise ("12-Mar-20:24" vs claimed "12-Mar-2024") is close by
        # n-gram but must NOT pass the strict check.
        self.assertFalse(containment.exact_containment(
            "12-Mar-2024", "Invoice Date : 12-Mar-20:24", "date"))
        self.assertGreater(containment.ngram_ratio(
            "12-Mar-2024", "Invoice Date : 12-Mar-20:24"), 0.6)

    def test_garbage_prose_fails(self):
        self.assertFalse(containment.exact_containment(
            "TWO WORDS", "BE Number : 1234567", "code"))


@unittest.skipUnless(CORPUS_AVAILABLE, "local corpus capture + cases not on disk")
class TestGrounding(unittest.TestCase):
    RAW = CORPUS_RAW

    def test_embedded_geometry_and_flip(self):
        # An embedded corpus page: the harness added real per-line geometry
        # (model regions are top-left; spans bottom-left).
        case = CASES["sample_b_1"]
        g = grounding.check_grounding(
            "sample_b_1", 1, case["invoice_number"], case["invoice_bbox"],
            self.RAW, margin=0.035, search_margin=0.12, loose_score=0.6, kind="code")
        self.assertIsNotNone(g)
        self.assertTrue(g["passed"])
        self.assertEqual(g["score"], 1.0)

    def test_region_imprecision_row_band(self):
        # A total region x-off by ~0.2; the row band finds the value (amount
        # kind): weak confirmation at loose_score.
        case = CASES["classic_3"]
        g = grounding.check_grounding(
            "classic_3", 1, case["total"], case["total_bbox"],
            self.RAW, margin=0.035, search_margin=0.12, loose_score=0.6, kind="amount")
        self.assertIsNotNone(g)
        self.assertTrue(g["passed"])
        self.assertEqual(g["score"], 0.6)

    def test_ocr_garble_unconfirmable(self):
        # A date the OCR channel misreads (a noise character instead of the
        # year's last digits) — no exact token exists: grounding must fail.
        case = CASES["classic_2"]
        g = grounding.check_grounding(
            "classic_2", 1, case["date"], case["date_bbox"],
            self.RAW, margin=0.035, search_margin=0.12, loose_score=0.6, kind="date")
        self.assertIsNotNone(g)
        self.assertFalse(g["passed"])
        self.assertIn("n-gram proximity", g["detail"])

    def test_missing_region_skips(self):
        g = grounding.check_grounding(
            "classic_2", 1, CASES["classic_2"]["boe_number"], None, self.RAW)
        self.assertIsNone(g)

    def test_wrong_value_not_confirmed(self):
        # A claimed value absent from the region (one-char drift class).
        case = CASES["sample_a_1"]
        g = grounding.check_grounding(
            "sample_a_1", 1, case["drifted_number"], case["drift_bbox"],
            self.RAW, margin=0.035, search_margin=0.12, loose_score=0.6, kind="code")
        self.assertIsNotNone(g)
        self.assertFalse(g["passed"])

    def test_verify_file_persists_grounding_match(self):
        # D36: a passing grounding check stores the span-level match in the
        # per-field audit (the UI's stored fallback for the located box).
        import verify as verify_mod
        case = CASES["sample_b_1"]
        data = {
            "header_fields": [{
                "name": "invoice_number", "value": case["invoice_number"],
                "confidence": 0.99, "source": "page 1 (render image)",
                "source_region": {"page": 1, "bbox": case["invoice_bbox"]},
            }],
            "line_items": [], "selected_document": {}, "_meta": {},
        }
        cfg = config.load_all()
        verify_mod.verify_file("sample_b_1", data, self.RAW, cfg, "broker_invoice")
        checks = data["header_fields"][0]["verification"]["checks"]
        g = next(c for c in checks if c["check"] == "grounding")
        self.assertTrue(g["passed"])
        self.assertEqual(g["match"]["page"], 1)
        self.assertIn(case["invoice_number"], g["match"]["text"])
        self.assertIn("fragment_bbox", g["match"])


class TestArithmetic(unittest.TestCase):
    def test_line_items_sum_pass(self):
        # D49: the summed column is the profile's amount role (here `total`).
        fields = {"total_invoice_amount": {"value": "1234.00"}}
        line_items = [{"total": "1200.00"}, {"total": "34.00"}]
        rules = [{"rule": "line_items_sum_to", "target": "total_invoice_amount",
                  "tolerance": 0.02}]
        res = check_arithmetic(fields, line_items, rules, amount_col="total")
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["passed"])
        self.assertEqual(res[0]["field"], "total_invoice_amount")

    def test_line_items_sum_default_amount_column(self):
        fields = {"total_invoice_amount": {"value": "7500.00"}}
        line_items = [{"amount": "5000.00"}, {"amount": "2500.00"}]
        rules = [{"rule": "line_items_sum_to", "target": "total_invoice_amount",
                  "tolerance": 0.02}]
        res = check_arithmetic(fields, line_items, rules)  # default `amount`
        self.assertTrue(res[0]["passed"])

    def test_line_items_quirk_fails_all(self):
        # A printed total differing from the item sum beyond tolerance.
        fields = {"total_invoice_amount": {"value": "1234.00"}}
        line_items = [{"total": "1200.00"}, {"total": "8.00"}]
        rules = [{"rule": "line_items_sum_to", "target": "total_invoice_amount",
                  "tolerance": 0.02}]
        res = check_arithmetic(fields, line_items, rules, amount_col="total")
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0]["passed"])

    def test_sum_equals(self):
        fields = {"a": {"value": "4000.00"}, "b": {"value": "800.00"},
                  "total": {"value": "4800.00"}}
        rules = [{"rule": "sum_equals", "fields": ["a", "b"], "target": "total",
                  "tolerance": 0.02}]
        res = check_arithmetic(fields, [], rules)
        self.assertEqual(len(res), 3)
        self.assertTrue(all(r["passed"] for r in res))

    def test_not_evaluable_skipped(self):
        res = check_arithmetic({"x": {"value": "1.00"}}, [], [])
        self.assertEqual(res, [])


class TestFormat(unittest.TestCase):
    def test_amount(self):
        self.assertTrue(check_format("total", "1234.56", "amount")["passed"])
        self.assertFalse(check_format("total", "five hundred", "amount")["passed"])

    def test_amount_must_be_canonical(self):
        # 2026-10-06: the model outputs the VALUE, not the printed text —
        # grouped/symbol amounts flag for review.
        for bad in ("50.000", "1,234.56", "1.234,56", "₹1,234", "1234.567"):
            res = check_format("total", bad, "amount")
            self.assertFalse(res["passed"], bad)
            self.assertIn("plain decimal", res["detail"], bad)
        self.assertTrue(check_format("total", "50000", "amount")["passed"])

    def test_currency(self):
        for good in ("USD", "inr", "₹", "U.S. $", "Rp"):
            self.assertTrue(check_format("cur", good, "currency")["passed"], good)
        ambiguous = check_format("cur", "$", "currency")
        self.assertFalse(ambiguous["passed"])
        self.assertIn("ambiguous", ambiguous["detail"])
        unknown = check_format("cur", "XYZ", "currency")
        self.assertFalse(unknown["passed"])
        self.assertIn("ISO 4217", unknown["detail"])

    def test_date_plausible(self):
        self.assertTrue(check_format("d", "26/06/2025", "date")["passed"])
        self.assertTrue(check_format("d", "30-Jun-2025", "date")["passed"])
        self.assertFalse(check_format("d", "32/13/2025", "date")["passed"])
        self.assertFalse(check_format("d", "01/01/1899", "date")["passed"])

    def test_code_str(self):
        self.assertTrue(check_format("inv", "INV-001", "str")["passed"])
        self.assertFalse(check_format("inv", "TWO WORDS", "str")["passed"])

    def test_unknown_type_passes(self):
        self.assertTrue(check_format("x", "anything", "object")["passed"])


class TestAggregate(unittest.TestCase):
    def test_clean_accept(self):
        v = aggregate.aggregate("f", [
            {"check": "grounding", "passed": True, "score": 1.0},
            {"check": "format", "passed": True, "score": 1.0},
        ])
        self.assertEqual(v.level, "accept")
        self.assertFalse(v.flagged)

    def test_grounding_fail_pulls_down(self):
        v = aggregate.aggregate("f", [
            {"check": "grounding", "passed": False, "score": 0.0},
            {"check": "format", "passed": True, "score": 1.0},
        ])
        self.assertIn(v.level, ("review_secondary", "review_human"))

    def test_arithmetic_hard_fail(self):
        v = aggregate.aggregate("total", [
            {"check": "grounding", "passed": True, "score": 1.0},
            {"check": "format", "passed": True, "score": 1.0},
            {"check": "arithmetic", "passed": False, "score": 0.0},
        ])
        self.assertEqual(v.level, "review_human")
        self.assertEqual(v.hard_fail, ["arithmetic"])

    def test_no_checks_pass_silently(self):
        v = aggregate.aggregate("f", [])
        self.assertEqual(v.level, "accept")

    def test_disagreement_hard_fail(self):
        v = aggregate.aggregate("f", [
            {"check": "disagreement", "passed": False, "score": 0.0},
        ])
        self.assertEqual(v.level, "review_human")


class TestVerificationConfig(unittest.TestCase):
    def test_verification_block_contract(self):
        cfg = config.load_all()
        v = cfg["thresholds"]["verification"]
        for key in ("grounding_margin_rel", "grounding_search_rel",
                    "grounding_loose_score", "check_weights", "accept_score",
                    "review_score", "hard_fail_checks", "recheck_max_calls"):
            self.assertIn(key, v)
        self.assertAlmostEqual(sum(v["check_weights"].values()), 1.0, places=2)

    def test_profile_arithmetic_rules(self):
        cfg = config.load_all()
        rules = profile_arithmetic_rules(cfg, "broker_invoice")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["rule"], "line_items_sum_to")
        self.assertEqual(rules[0]["target"], "total_invoice_amount")


if __name__ == "__main__":
    unittest.main()

class TestMultiDocumentVerify(unittest.TestCase):
    """Multi-document extraction verification (2026-09-05)."""

    def setUp(self):
        import config as cfg_mod
        self.cfg = cfg_mod.load_all()
        self.raw = CORPUS_RAW

    def _entry(self, tid, pages, fields, found=True):
        return {
            "template_id": tid, "found": found,
            "selected_document": {"name": f"doc-{tid}", "document_pages": pages,
                                  "reason": "seen", "confidence": 0.99},
            "header_fields": [{"name": n, "value": v, "confidence": 0.99,
                               "source": "page 1 (render image)"}
                              for n, v in fields.items()],
            "line_items": [], "notes": [],
        }

    def test_duplicate_occurrence_flagged(self):
        from verify import check_duplicates
        data = {"documents": [
            self._entry("hawb_doc", [1, 2], {"hawb_number": "123"}),
            self._entry("hawb_doc", [2, 3], {"hawb_number": "123"}),  # shared p2, same number
        ]}
        rows = check_duplicates(data, self.cfg, "f")
        self.assertEqual([r["reason"] for r in rows], ["duplicate_occurrence"])

    def test_page_overlap_conflict_flagged(self):
        from verify import check_duplicates
        data = {"documents": [
            self._entry("hawb_doc", [1, 2], {"hawb_number": "123"}),
            self._entry("hawb_doc", [2, 3], {"hawb_number": "456"}),  # shared p2, DIFFERENT numbers
        ]}
        rows = check_duplicates(data, self.cfg, "f")
        self.assertEqual([r["reason"] for r in rows], ["page_overlap"])

    def test_non_overlapping_not_flagged(self):
        from verify import check_duplicates
        data = {"documents": [
            self._entry("hawb_doc", [1], {"hawb_number": "123"}),
            self._entry("hawb_doc", [4], {"hawb_number": "456"}),  # distinct pages = two AWBs
        ]}
        rows = check_duplicates(data, self.cfg, "f")
        self.assertEqual(rows, [])

    def test_duplicate_flag_carries_entry_index(self):
        # D57: the later occurrence (documents[1]) is the flagged one.
        from verify import check_duplicates
        data = {"documents": [
            self._entry("hawb_doc", [1, 2], {"hawb_number": "123"}),
            self._entry("hawb_doc", [2, 3], {"hawb_number": "123"}),
        ]}
        rows = check_duplicates(data, self.cfg, "f")
        self.assertEqual(rows[0]["entry"], 1)
        self.assertIn("documents[0]", rows[0]["detail"])
        self.assertIn("documents[1]", rows[0]["detail"])

    def test_verify_multidoc_rows_carry_entry_index(self):
        # D57: every per-entry row is occurrence-attributed.
        from verify import verify_multidoc
        data = {"documents": [
            self._entry("hawb_doc", [], {}, found=False),
            self._entry("broker_invoice", [], {}, found=False),
        ]}
        rows, _ = verify_multidoc("f", data, self.raw, self.cfg, ["hawb_doc",
                                                                  "broker_invoice"])
        self.assertTrue(rows)
        self.assertEqual({r["entry"] for r in rows}, {0, 1})

    def test_missing_entry_has_guard_row(self):
        from verify import verify_multidoc
        data = {"documents": [
            self._entry("hawb_doc", [], {}, found=False),
        ]}
        rows, rec = verify_multidoc("f", data, self.raw, self.cfg, ["hawb_doc"])
        reasons = [r["reason"] for r in rows]
        self.assertIn("no_document_match", reasons)
        self.assertEqual(rows[0]["template"], "hawb_doc")
        self.assertFalse(rec["documents"]["hawb_doc"]["found"])

    def test_entry_arithmetic_uses_own_template(self):
        from verify import verify_document_entry
        entry = self._entry("broker_invoice", [1],
                            {"total_invoice_amount": "1234.00"})
        entry["line_items"] = [{"total": "1200.00"}, {"total": "8.00"}]
        rows, rec = verify_document_entry("f", entry, self.raw, self.cfg)
        levels = {n: r["level"] for n, r in rec["fields"].items()}
        self.assertEqual(levels["total_invoice_amount"], "review_human")

    def test_uncertain_duplicate_flagged(self):
        from verify import verify_document_entry
        entry = self._entry("hawb_doc", [1], {"hawb_number": "123"})
        entry["uncertain_duplicate"] = True
        rows, _ = verify_document_entry("f", entry, self.raw, self.cfg)
        self.assertIn("duplicate_uncertain", [r["reason"] for r in rows])


@unittest.skipUnless(CORPUS_AVAILABLE, "local corpus capture + cases not on disk")
class TestContentBinding(unittest.TestCase):
    """Anti-cross-file-mix-up guard (2026-09-05): the primary identifier of a
    found document must exist somewhere in its OWN file's text channel."""

    def setUp(self):
        import config as cfg_mod
        self.cfg = cfg_mod.load_all()

    def test_present_value_passes(self):
        from verify import check_content_binding
        entry = {"template_id": "broker_invoice", "found": True,
                 "selected_document": {"name": "X", "document_pages": [1],
                                       "reason": "r", "confidence": 0.9},
                 "header_fields": [{"name": "invoice_number",
                                    "value": CASES["sample_b_1"]["invoice_number"],
                                    "confidence": 0.99,
                                    "source": "page 1 (render image)"}]}
        rows = check_content_binding("sample_b_1", entry,
                                     CORPUS_RAW, self.cfg)
        self.assertEqual(rows, [])

    def test_foreign_value_flagged(self):
        from verify import check_content_binding
        # A number that is NOT in the file's text channel at all.
        entry = {"template_id": "broker_invoice", "found": True,
                 "selected_document": {"name": "X", "document_pages": [1],
                                       "reason": "r", "confidence": 0.9},
                 "header_fields": [{"name": "invoice_number",
                                    "value": "SOME-STRANGE-12345",
                                    "confidence": 0.99,
                                    "source": "page 1 (render image)"}]}
        rows = check_content_binding("sample_b_1", entry,
                                     CORPUS_RAW, self.cfg)
        self.assertEqual([r["reason"] for r in rows], ["content_mismatch"])

    def test_skipped_when_not_found(self):
        from verify import check_content_binding
        entry = {"template_id": "hawb_doc", "found": False, "header_fields": []}
        rows = check_content_binding("sample_b_1", entry,
                                     CORPUS_RAW, self.cfg)
        self.assertEqual(rows, [])


class TestLineItemChecks(unittest.TestCase):
    """Row-level line-item sanity: amount completeness + template row rules."""

    COLS = [{"name": "description", "type": "str", "role": "description"},
            {"name": "taxable", "type": "amount"},
            {"name": "tax", "type": "amount"},
            {"name": "total", "type": "amount", "role": "amount"}]
    RULES = [{"rule": "sum_equals", "fields": ["taxable", "tax"],
              "target": "total", "tolerance": 0.02, "missing": "zero"}]

    def _li(self, **kw):
        row = {"description": "x", "taxable": None, "tax": None, "total": None}
        row.update({k: v for k, v in kw.items() if v is not None})
        return row

    def _check(self, items):
        from verify import check_line_items
        return check_line_items("f", items, columns=self.COLS, row_rules=self.RULES)

    def test_missing_amount_flagged(self):
        rows = self._check([self._li(taxable="100.00")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "line_item_issue")
        self.assertIn("NO AMOUNT", rows[0]["detail"])

    def test_consistent_with_tax_passes(self):
        self.assertEqual(self._check([self._li(
            taxable="4000.00", tax="800.00", total="4800.00")]), [])

    def test_tax_free_row_passes(self):
        # Tax legitimately absent + `missing: zero` -> taxable == total.
        self.assertEqual(self._check([self._li(
            taxable="4000.00", total="4000.00")]), [])

    def test_tax_free_row_mismatch_flagged(self):
        rows = self._check([self._li(taxable="4000.00", total="4800.00")])
        self.assertEqual(len(rows), 1)
        self.assertIn("sum_equals", rows[0]["detail"])

    def test_row_inconsistency_flagged(self):
        # A cross-row tax split class.
        rows = self._check([self._li(taxable="1200.00", tax="120.00",
                                     total="1400.00")])
        self.assertEqual(len(rows), 1)
        self.assertIn("inconsistent", rows[0]["detail"])

    def test_empty_list_passes(self):
        self.assertEqual(self._check([]), [])

    def test_composite_tax_reconciles(self):
        # D47: split tax cells sum their components.
        self.assertEqual(self._check([self._li(
            taxable="1000.00", tax="CGST 100.00; SGST 100.00",
            total="1200.00")]), [])
        self.assertEqual(self._check([self._li(
            taxable="3000.00", tax="150.00 + 150.00", total="3300.00")]), [])

    def test_declared_columns_and_rules(self):
        # D49: the checker follows the template's columns + row rules.
        from verify import check_line_items
        cols = [{"name": "desc", "type": "str", "role": "description"},
                {"name": "net", "type": "amount"},
                {"name": "vat", "type": "amount"},
                {"name": "gross", "type": "amount", "role": "amount"}]
        rules = [{"rule": "sum_equals", "fields": ["net", "vat"],
                  "target": "gross", "tolerance": 0.02}]
        rows = check_line_items(
            "f", [{"desc": "x", "net": "100.00", "vat": "18.00", "gross": "118.00"}],
            columns=cols, row_rules=rules)
        self.assertEqual(rows, [])
        rows = check_line_items(
            "f", [{"desc": "x", "net": "100.00", "vat": "18.00", "gross": "120.00"}],
            columns=cols, row_rules=rules)
        self.assertEqual(len(rows), 1)
        self.assertIn("net+vat", rows[0]["detail"])

    def test_no_rules_means_only_completeness(self):
        from verify import check_line_items
        rows = check_line_items("f", [{"description": "x", "amount": "10.00"}],
                                columns=[{"name": "description", "type": "str",
                                          "role": "description"},
                                         {"name": "amount", "type": "amount",
                                          "role": "amount"}])
        self.assertEqual(rows, [])


class TestRateAwareRowMath(unittest.TestCase):
    """D50: a bare-decimal rate is dropped only under the numeric identity."""

    def _v(self, cell, context):
        from validate import _addend_amount
        return _addend_amount({"tax": cell}, "tax", context, 0.02)

    def test_bare_decimal_rate_dropped(self):
        # 9% of taxable 3000 = 270 -> the two 9.00 components are rates.
        self.assertEqual(
            self._v("CGST 9.00 270.00; SGST 9.00 270.00", [3000.0, 3540.0]),
            540.0)

    def test_percent_rate_dropped(self):
        self.assertEqual(
            self._v("CGST 9.00% 270.00; SGST 9.00% 270.00", [3000.0, 3540.0]),
            540.0)

    def test_rate_without_identity_kept(self):
        # 9% of 2000 is 180, not 270 -> the 9.00 does NOT explain the sibling.
        self.assertEqual(self._v("9.00 270.00", [2000.0, 2270.0]), 279.0)

    def test_plain_split_amounts_kept(self):
        self.assertEqual(self._v("315.00 + 315.00", [3500.0, 4130.0]), 630.0)

    def test_single_component_unchanged(self):
        self.assertEqual(self._v("990.00", [5500.0, 6490.0]), 990.0)
        self.assertEqual(self._v("9.00", [3000.0, 3009.0]), 9.0)

    def test_rule_flags_when_identity_does_not_hold(self):
        from validate import check_row_arithmetic
        rule = {"rule": "sum_equals", "fields": ["taxable", "tax"],
                "target": "total", "tolerance": 0.02, "missing": "zero"}
        # The rate-looking 9.00 has no identity here -> kept -> mismatch.
        res = check_row_arithmetic(
            {"taxable": "2000.00", "tax": "9.00 270.00", "total": "2270.00"},
            [rule])
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0]["passed"])

    def test_rule_passes_with_bare_decimal_rate(self):
        from validate import check_row_arithmetic
        rule = {"rule": "sum_equals", "fields": ["taxable", "tax"],
                "target": "total", "tolerance": 0.02, "missing": "zero"}
        res = check_row_arithmetic(
            {"taxable": "3000.00",
             "tax": "CGST 9.00 270.00; SGST 9.00 270.00", "total": "3540.00"},
            [rule])
        self.assertTrue(res[0]["passed"])


class TestLineItemGrounding(unittest.TestCase):
    """D48 Phase B: per-row grounding of amount cells against the row region."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name) / "raw" / "doc1"
        d.mkdir(parents=True)
        (d / "page001.segments.txt").write_text(
            "1|0.10,0.80,0.95,0.82|0.9|HANDLING CHARGES 996712 3,000.00 300.00 3,300.00\n",
            encoding="utf-8")
        self.raw = Path(self._tmp.name) / "raw"
        self.cfg = config.load_all()

    def tearDown(self):
        self._tmp.cleanup()

    def _row(self, **kw):
        row = {"description": "HANDLING CHARGES", "taxable": "3000.00",
               "tax": "300.00", "total": "3300.00", "confidence": 0.9,
               "source": "page 1 (render image)",
               "source_region": {"page": 1, "bbox": [0.0, 0.0, 1.0, 1.0]}}
        row.update(kw)
        return row

    def test_correct_cells_ground(self):
        from verify import check_line_item_grounding
        self.assertEqual(check_line_item_grounding(
            "doc1", self.raw, [self._row()], self.cfg, "broker_invoice"), [])

    def test_misread_tax_is_flagged(self):
        from verify import check_line_item_grounding
        rows = check_line_item_grounding(
            "doc1", self.raw, [self._row(tax="351.00")], self.cfg, "broker_invoice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "verification_grounding")
        self.assertEqual(rows[0]["field"], "line_item_1")
        self.assertIn("'tax' 351.00", rows[0]["detail"])

    def test_row_without_region_is_skipped(self):
        from verify import check_line_item_grounding
        row = self._row()
        row["source_region"] = None
        self.assertEqual(check_line_item_grounding(
            "doc1", self.raw, [row], self.cfg, "broker_invoice"), [])


class TestAmountComponents(unittest.TestCase):
    """D47: amount-cell parser — composite cells sum, rates/dates don't."""

    def _v(self, s):
        from compare import norm_amount_components
        return norm_amount_components(s)

    def test_single(self):
        self.assertEqual(self._v("990.00"), 990.0)
        self.assertEqual(self._v("1,23,456.00"), 123456.0)
        self.assertEqual(self._v("9876.54"), 9876.54)
        self.assertEqual(self._v("0.00"), 0.0)

    def test_composite_sums(self):
        self.assertEqual(self._v("CGST 315.00; SGST 315.00"), 630.0)
        self.assertEqual(self._v("315.00 + 315.00"), 630.0)
        self.assertEqual(self._v("CGST @9% 270.00 SGST @9% 270.00"), 540.0)
        self.assertEqual(self._v("5000.00 900.00"), 5900.0)

    def test_rates_ignored(self):
        self.assertEqual(self._v("9% 270.00"), 270.0)
        self.assertEqual(self._v("18% GST 990"), 990.0)

    def test_fallbacks(self):
        self.assertEqual(self._v("990"), 990.0)
        self.assertEqual(self._v("1,23,456"), 123456.0)
        self.assertIsNone(self._v(None))
        self.assertIsNone(self._v("04.07.2024"))  # date, not an amount


class TestMissingValueFlags(unittest.TestCase):
    """D55: a declared field with no value on a FOUND document is flagged."""

    CFG = {"documents": {"documents": [
        {"id": "alpha", "label": "A", "description": "d", "signals": {},
         "extraction": {"fields": [
             {"name": "ref", "type": "str", "description": "d"},
             {"name": "amt", "type": "amount", "description": "d"},
         ]}}]}}

    def test_single_found_flags_null_and_absent_fields(self):
        from verify import selection_guards
        data = {"status": "extracted",
                "selected_document": {"name": "ACME", "confidence": 0.99, "reason": "r"},
                "header_fields": [{"name": "ref", "value": "R-1", "confidence": 0.9}],
                "_meta": {"document": "alpha"}}
        rows = selection_guards(data, self.CFG, "s1", None,
                                {f["name"]: f for f in data["header_fields"]})
        missing = [r for r in rows if r["reason"] == "missing_value"]
        self.assertEqual([r["field"] for r in missing], ["amt"])  # absent row too
        self.assertIsNone(missing[0]["value"])
        self.assertEqual(missing[0]["template"], "alpha")
        self.assertIsNotNone(missing[0]["threshold"])

    def test_single_no_match_does_not_spam(self):
        from verify import selection_guards
        data = {"status": "flag_no_document_match",
                "selected_document": {"name": None, "confidence": 0.0, "reason": "none"},
                "header_fields": [], "_meta": {"document": "alpha"}}
        rows = selection_guards(data, self.CFG, "s2", None, {})
        self.assertEqual([r for r in rows if r["reason"] == "missing_value"], [])
        self.assertTrue(any(r["reason"] == "no_document_match" for r in rows))

    def test_multi_found_entry_flags_missing(self):
        from verify import selection_guards_entry
        found = {"template_id": "alpha", "found": True,
                 "selected_document": {"name": "ACME", "confidence": 0.99,
                                       "reason": "r"},
                 "header_fields": [{"name": "ref", "value": "R-1", "confidence": 0.9}]}
        rows = selection_guards_entry("s3", found, self.CFG, "alpha", None)
        missing = [r for r in rows if r["reason"] == "missing_value"]
        self.assertEqual([r["field"] for r in missing], ["amt"])
        self.assertEqual(missing[0]["template"], "alpha")

    def test_multi_not_found_entry_does_not_spam(self):
        from verify import selection_guards_entry
        not_found = {"template_id": "alpha", "found": False,
                     "selected_document": {"confidence": 0.0, "reason": "none"},
                     "header_fields": []}
        rows = selection_guards_entry("s3", not_found, self.CFG, "alpha", None)
        self.assertEqual([r for r in rows if r["reason"] == "missing_value"], [])


class TestNormalizerTotality(unittest.TestCase):
    """2026-09-30: object-typed values must never crash comparisons."""

    def test_name_eq_non_string(self):
        from compare import name_eq, norm_name
        self.assertEqual(norm_name({"name": "ACME"}), [])
        self.assertFalse(name_eq({"name": "ACME"}, "ACME"))
        self.assertFalse(name_eq("ACME", ["ACME"]))

    def test_other_kinds_non_string(self):
        from compare import date_eq, amount_eq, str_eq
        for fn, a, b in ((amount_eq, {"amount": "10.00"}, "10.00"),
                         (date_eq, ["2025-01-01"], "2025-01-01"),
                         (str_eq, None, "x"),
                         (str_eq, {"a": 1}, {"a": 1})):
            self.assertIsInstance(fn(a, b), bool)


class TestSelectedName(unittest.TestCase):
    """D45: the selection key is `name`; legacy `broker` still reads."""

    def test_new_key(self):
        from compare import selected_name
        self.assertEqual(selected_name({"name": "ACME CORP"}), "ACME CORP")

    def test_legacy_key_compat(self):
        from compare import selected_name
        self.assertEqual(selected_name({"broker": "ACME CORP"}), "ACME CORP")

    def test_empty(self):
        from compare import selected_name
        self.assertIsNone(selected_name(None))
        self.assertIsNone(selected_name({}))
