"""Message catalog contract (2026-10-05): every code has human wording, and
the primary text never leaks codes, paths or "(s)" plurals."""

from __future__ import annotations

import re
import unittest

import messages

# Every reason the engine can emit (verify.py / extract.py / text_layer.py).
ENGINE_REASONS = {
    "verification_grounding", "verification_arithmetic", "verification_format",
    "verification_disagreement", "missing_value", "line_item_issue",
    "no_document_match", "below_threshold", "selection_conflict",
    "document_quality", "invalid_source_region", "page_overlap",
    "duplicate_occurrence", "missing_expected_document", "content_mismatch",
    "unbound_document", "cross_document_mismatch",
}

# Every structured warning config.py can append.
CONFIG_WARNINGS = {
    "workflow_doc_missing", "workflow_doc_fieldless", "expected_missing",
    "cross_template_missing", "cross_template_not_in_workflow",
    "cross_template_fieldless", "cross_field_missing",
    "cross_kinds_incompatible", "export_workflow_missing",
    "export_line_item_template_missing", "column_template_missing",
    "column_field_missing", "column_line_item_missing",
    "column_cross_check_missing",
}

_SNAKE = re.compile(r"[a-z]+_[a-z]+")


class TestReasonCatalog(unittest.TestCase):
    def test_every_engine_reason_has_a_message(self):
        self.assertTrue(ENGINE_REASONS <= set(messages.REASONS))

    def test_titles_and_bodies_are_plain(self):
        for code in messages.REASONS:
            msg = messages.reason_message(code)  # placeholders filled
            with self.subTest(code=code):
                for field in ("title", "body", "hint"):
                    text = msg[field]
                    self.assertNotIn("(s)", text)
                    self.assertNotIn("rules/", text)
                    self.assertNotIn(".yaml", text)
                    self.assertNotIn("{", text)
                    self.assertIsNone(_SNAKE.search(text),
                                      f"{code}.{field} leaks a code: {text!r}")

    def test_value_is_quoted_and_optional(self):
        msg = messages.reason_message("verification_grounding", value="INV-77")
        self.assertIn("“INV-77”", msg["body"])
        bare = messages.reason_message("verification_grounding")
        self.assertIn("this value", bare["body"])

    def test_unknown_code_degrades_gracefully(self):
        msg = messages.reason_message("weird_new_code")
        self.assertTrue(msg["title"])
        self.assertNotIn("_", msg["title"])

    def test_level_labels(self):
        self.assertEqual(messages.level_label("accept"), "Checked")
        self.assertEqual(messages.level_label("review_human"), "Needs review")
        self.assertEqual(messages.level_label("review_secondary"),
                         "Second opinion differs")
        self.assertEqual(messages.level_label(None), "")


class TestWarningCatalog(unittest.TestCase):
    def test_every_config_warning_has_a_message(self):
        self.assertTrue(CONFIG_WARNINGS <= set(messages.WARNINGS))

    def test_warning_text_is_plain_and_carries_where(self):
        w = messages.warning_message(
            "column_field_missing", where="exports[0].columns[2]",
            header="Total", template="broker_invoice", field="total")
        self.assertIn("Total", w["body"])
        self.assertIn("broker_invoice", w["body"])
        text = str(w)
        self.assertIn("exports[0].columns[2]", text)
        self.assertNotIn("will be blank", text)  # old jargon is gone
        # Legacy substring checks still work on the structured warning.
        self.assertIn("Total", w)
        self.assertIn("column_field_missing", w.get("code", ""))

    def test_warning_text_helper(self):
        text = messages.warning_text("export_workflow_missing",
                                     workflow="ghost_wf")
        self.assertIn("ghost_wf", text)


class TestPhrases(unittest.TestCase):
    def test_plural(self):
        self.assertEqual(messages.plural(1, "item"), "1 item")
        self.assertEqual(messages.plural(2, "item"), "2 items")
        self.assertEqual(messages.plural(2, "check", "checks"), "2 checks")

    def test_count_phrase(self):
        self.assertEqual(
            messages.count_phrase({"pending": 1, "mismatch": 1}),
            "1 item needs review · 1 disagreement")
        self.assertEqual(
            messages.count_phrase({"pending": 2, "not_checked": 1}),
            "2 items need review · 1 check not run")
        self.assertEqual(messages.count_phrase({}), "OK")

    def test_reason_summary(self):
        text = messages.reason_summary(
            {"cross_document_mismatch": 2, "missing_value": 3,
             "line_item_issue": 3})
        self.assertIn("8 items need review", text)
        self.assertIn("2 documents disagree", text)
        self.assertIn("3 values missing", text)
        self.assertNotIn("_", text)
        self.assertEqual(messages.reason_summary({}), "Nothing needs review")

    def test_reason_summary_limits_and_counts_the_rest(self):
        text = messages.reason_summary(
            {"missing_value": 5, "line_item_issue": 4,
             "verification_grounding": 3, "document_quality": 2})
        self.assertIn("+2 more", text)


if __name__ == "__main__":
    unittest.main()
