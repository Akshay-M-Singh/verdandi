"""Universal output-format helpers (2026-10-06): currency normalization,
locale-aware amount readings, the canonical money form.

These are dataset-independent by design — one place that understands the
world's number/currency conventions, shared by grounding, validation,
cross-checks and export.
"""

from __future__ import annotations

import unittest

import config
import grounding
from compare import (
    amount_readings,
    canonical_amount_ok,
    currency_ambiguous,
    currency_eq,
    norm_currency,
)


class TestCurrency(unittest.TestCase):
    def test_iso_codes_pass_through(self):
        self.assertEqual(norm_currency("USD"), "USD")
        self.assertEqual(norm_currency("inr"), "INR")
        self.assertEqual(norm_currency("Idr"), "IDR")
        self.assertEqual(norm_currency(" EUR "), "EUR")

    def test_unambiguous_aliases(self):
        cases = (("₹", "INR"), ("Rs.", "INR"), ("Rs", "INR"), ("Rp", "IDR"),
                 ("€", "EUR"), ("£", "GBP"), ("U.S. $", "USD"), ("US$", "USD"),
                 ("S$", "SGD"), ("A$", "AUD"), ("C$", "CAD"), ("R$", "BRL"),
                 ("₩", "KRW"), ("฿", "THB"), ("₪", "ILS"), ("¥", None))
        for raw, code in cases:
            self.assertEqual(norm_currency(raw), code, raw)

    def test_ambiguous_symbols_never_guessed(self):
        for raw in ("$", "¥", "kr"):
            self.assertIsNone(norm_currency(raw), raw)
            self.assertTrue(currency_ambiguous(raw), raw)

    def test_unknown_values_are_none(self):
        for raw in ("XYZ", "dollars", "", None, "123"):
            self.assertIsNone(norm_currency(raw), raw)

    def test_currency_eq(self):
        self.assertTrue(currency_eq("U.S. $", "USD"))
        self.assertTrue(currency_eq("₹", "INR"))
        self.assertTrue(currency_eq("USD", "usd"))
        self.assertFalse(currency_eq("USD", "GBP"))
        # unresolvable forms fall back to plain text comparison
        self.assertTrue(currency_eq("$", "$"))
        self.assertFalse(currency_eq("$", "USD"))


class TestAmountReadings(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(amount_readings("1234.56"), {1234.56})
        self.assertEqual(amount_readings("50000"), {50000.0})
        self.assertEqual(amount_readings("-12.5"), {-12.5})

    def test_comma_grouping(self):
        self.assertEqual(amount_readings("1,234.56"), {1234.56})
        self.assertEqual(amount_readings("1,73,140.00"), {173140.0})

    def test_dot_grouping(self):
        self.assertEqual(amount_readings("1.234,56"), {1234.56})
        self.assertEqual(amount_readings("1.234.567"), {1234567.0})

    def test_single_three_digit_group_is_ambiguous(self):
        self.assertEqual(amount_readings("50.000"), {50.0, 50000.0})
        self.assertEqual(amount_readings("50,000"), {50.0, 50000.0})

    def test_leading_zero_is_never_grouping(self):
        self.assertEqual(amount_readings("0.125"), {0.12})

    def test_unparseable(self):
        self.assertEqual(amount_readings("abc"), set())
        self.assertEqual(amount_readings(None), set())

    def test_canonical_form(self):
        for good in ("1234.56", "50000", "0.5", "-12.5", "50000.00"):
            self.assertTrue(canonical_amount_ok(good), good)
        for bad in ("50.000", "1,234.56", "₹1,234", "1234.567", "1 234",
                    "1234,56", ""):
            self.assertFalse(canonical_amount_ok(bad), bad)


class TestCurrencyTypeContract(unittest.TestCase):
    def test_registered_in_config(self):
        self.assertIn("currency", config.FIELD_TYPES)
        self.assertEqual(config.FIELD_TYPE_DEFAULTS["currency"], 0.9)

    def test_cross_kind_compatibility(self):
        self.assertTrue(config.cross_kinds_compatible("currency", "currency"))
        self.assertFalse(config.cross_kinds_compatible("currency", "str"))
        self.assertFalse(config.cross_kinds_compatible("currency", "amount"))

    def test_grounding_kind(self):
        self.assertEqual(grounding.KIND_BY_TYPE["currency"], "code")


if __name__ == "__main__":
    unittest.main()
