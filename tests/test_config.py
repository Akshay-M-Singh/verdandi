"""Config-contract tests — the real YAML files are the contract (`make check`).

Tests load the REAL config files plus temp-file error paths: missing required
keys, wrong types, unknown keys (warn), malformed YAML, missing files, and
.env semantics (precedence, placeholder, missing file).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import config


def _write(tmpdir: str, name: str, content: str) -> str:
    path = Path(tmpdir) / name
    path.write_text(content, encoding="utf-8")
    return str(path)


class TestRealConfigs(unittest.TestCase):
    """The actual shipped YAML files must always parse (they are the contract)."""

    def test_models_loads(self):
        cfg = config.load_models()
        self.assertIsInstance(cfg["primary_model"], str)
        self.assertTrue(cfg["primary_model"].strip())
        # 2026-08-29 (swap #4): luna is primary again, via the Responses API
        # (its chat/completions route is provider-broken — see llm_client).
        self.assertEqual(cfg["primary_model"], "gpt-5.6-luna")
        self.assertEqual(cfg["primary_mode"], "vision")  # D24 vision-primary
        self.assertEqual(cfg["api_style"], "responses")
        self.assertTrue(cfg["endpoint"].startswith("https://"))
        self.assertFalse(cfg["endpoint"].endswith("/"))

    def test_models_api_style_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\napi_style: octopus\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\n")
            self.assertEqual(config.load_models(Path(path))["api_style"], "chat_completions")  # default

    def test_models_image_url_form_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\nimage_url_form: octopus\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\n")
            self.assertEqual(config.load_models(Path(path))["image_url_form"], "flat")  # default

    def test_models_vision_verified_contract(self):
        # D35: the probed-model allowlist the UI picker trusts.
        m = config.load_models()
        self.assertIsInstance(m["vision_verified"], list)
        self.assertIn("gpt-5.6-luna", m["vision_verified"])
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\nvision_verified: nope\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\nvision_verified: [a, '']\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\n")
            self.assertEqual(config.load_models(Path(path))["vision_verified"], [])  # default

    def test_models_model_notes_contract(self):
        # D43: short measured hints for the picker — documentation only.
        m = config.load_models()
        self.assertIsInstance(m["model_notes"], dict)
        self.assertIn("gpt-5.6-luna", m["model_notes"])
        for note in m["model_notes"].values():
            self.assertIsInstance(note, str)
            self.assertTrue(note.strip())
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\nmodel_notes: nope\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\nmodel_notes: {m: ''}\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "primary_model: x\n")
            self.assertEqual(config.load_models(Path(path))["model_notes"], {})  # default

    def test_line_items_columns_contract(self):
        # D48/D49: declared columns, generic roles, legacy alias, error paths.
        docs = config.load_documents()
        broker = next(d for d in docs["documents"] if d["id"] == "broker_invoice")
        cols = broker["extraction"]["line_items_columns"]
        self.assertEqual([c["name"] for c in cols],
                         ["description", "hsn_sac", "taxable", "tax", "total"])
        self.assertEqual(config.line_item_column(docs, "broker_invoice", "amount")["name"],
                         "total")
        # The legacy `total` role reads as `amount`; taxable/tax are plain
        # columns now (the row math is template data).
        roles = {c["name"]: c.get("role") for c in cols}
        self.assertEqual(roles, {"description": "description", "hsn_sac": None,
                                 "taxable": None, "tax": None, "total": "amount"})
        self.assertEqual(config.profile_row_arithmetic(docs, "broker_invoice"),
                         [{"rule": "sum_equals", "fields": ["taxable", "tax"],
                           "target": "total", "tolerance": 0.02, "missing": "zero"}])
        # A profile without declared columns falls back to the generic pair.
        self.assertEqual([c["name"] for c in config.line_item_columns(docs, "hawb_doc")],
                         ["description", "amount"])

        def docs_yaml(block: str) -> str:
            return ("documents:\n- id: t\n  label: T\n  description: d\n"
                    "  extraction:\n    line_items: true\n" + block)

        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns: []\n"))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns:\n"
                "    - name: a\n      description: d\n      role: description\n"))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))  # missing the amount role
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns:\n"
                "    - name: a\n      description: d\n      role: description\n"
                "    - name: b\n      description: d\n      role: amount\n"
                "    - name: c\n      description: d\n      role: amount\n"))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))  # duplicate role
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns:\n"
                "    - name: a\n      description: d\n      role: description\n"
                "    - name: c\n      description: d\n      role: amount\n"
                "      type: octopus\n"))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))  # bad column type

    def test_legacy_role_alias_and_row_rules_contract(self):
        # D49: `total` reads as `amount`; taxable/tax roles are dropped;
        # row_arithmetic is validated against the declared columns.
        def docs_yaml(block: str) -> str:
            return ("documents:\n- id: t\n  label: T\n  description: d\n"
                    "  signals:\n    keywords: []\n"
                    "  extraction:\n    line_items: true\n"
                    "    fields:\n    - name: x\n      type: str\n"
                    "      description: d\n" + block)

        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns:\n"
                "    - name: description\n      description: d\n      role: description\n"
                "    - name: taxable\n      description: d\n      role: taxable\n"
                "    - name: total\n      description: d\n      role: total\n"))
            docs = config.load_documents(Path(path))
            cols = docs["documents"][0]["extraction"]["line_items_columns"]
            # `taxable` keeps its data (role dropped); `total` reads as `amount`.
            self.assertEqual({c["name"]: c.get("role") for c in cols},
                             {"description": "description", "taxable": None,
                              "total": "amount"})
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns:\n"
                "    - name: description\n      description: d\n      role: description\n"
                "    - name: net\n      description: d\n      role: amount\n"
                "    - name: vat\n      description: d\n"
                "    row_arithmetic:\n"
                "    - rule: sum_equals\n      fields:\n      - net\n      - vat\n"
                "      target: bogus\n      missing: zero\n"))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))  # undeclared rule column
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", docs_yaml(
                "    line_items_columns:\n"
                "    - name: description\n      description: d\n      role: description\n"
                "    - name: net\n      description: d\n      role: amount\n"
                "    row_arithmetic:\n"
                "    - rule: line_items_sum_to\n      target: net\n"))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))  # only sum_equals per row

    def test_thresholds_loads_with_required_fields(self):
        cfg = config.load_thresholds()
        thresholds = cfg["thresholds"]
        for field in config.REQUIRED_THRESHOLDS:
            self.assertIn(field, thresholds)
            self.assertTrue(0.0 <= thresholds[field] <= 1.0)

    def test_thresholds_accepts_profile_declared_names(self):
        # D28: profile-declared field names never warn (dynamic whitelist).
        profiled = set(config.profile_field_names())
        self.assertTrue({"invoice_number", "boe_number", "total_invoice_amount"} <= profiled)

    def test_quality_block_loads(self):
        quality = config.load_thresholds()["quality"]
        for key in config.QUALITY_KEYS:
            self.assertIn(key, quality)
        self.assertIsInstance(quality["min_lines_per_page"], int)
        self.assertIsInstance(quality["min_words_per_page"], int)
        for key in ("min_latin_ratio", "min_text_area_ratio",
                    "max_dark_pixel_ratio", "review_band"):
            self.assertTrue(0.0 <= quality[key] <= 1.0)
        self.assertTrue(0.0 <= quality["max_pixel_std_dev"] <= 255.0)
        self.assertIsInstance(quality["quality_probe"], bool)
        self.assertFalse(quality["quality_probe"])  # default off (cost + determinism)

    def test_verification_block_loads(self):
        cfg = config.load_thresholds()
        v = cfg["verification"]
        # 2026-09-02: the verification stack replaced the rules locator.
        self.assertNotIn("locator_fields", cfg)
        for key in ("grounding_margin_rel", "grounding_search_rel",
                    "grounding_loose_score", "check_weights", "accept_score",
                    "review_score", "hard_fail_checks", "recheck_max_calls"):
            self.assertIn(key, v)
        self.assertAlmostEqual(sum(v["check_weights"].values()), 1.0, places=2)

    def test_models_verification_contract(self):
        m = config.load_models()
        self.assertIn("verification_model", m)
        self.assertIn("deepseek-v4-flash-vision-exp", m.get("model_overrides", {}))
        over = m["model_overrides"]["deepseek-v4-flash-vision-exp"]
        self.assertEqual(over["image_url_form"], "nested")

    def test_documents_loads(self):
        docs = config.load_documents()["documents"]
        # D29: one shipped profile (broker_invoice) — the D26-era
        # supplier_invoice demo was never re-seeded.
        self.assertGreaterEqual(len(docs), 1)
        broker = next(d for d in docs if d["id"] == "broker_invoice")
        # D29: user keywords, NO broker registry / source key.
        self.assertNotIn("source", broker["signals"])
        self.assertTrue(broker["signals"]["keywords"])
        fields = broker["extraction"]["fields"]
        names = {f["name"] for f in fields}
        self.assertTrue({"invoice_number", "invoice_date", "boe_number",
                         "boe_date", "total_invoice_amount"} <= names)
        for f in fields:
            self.assertIn(f["type"], config.FIELD_TYPES)
        # D28: self-contained fields (threshold + labels inline).
        boe = next(f for f in fields if f["name"] == "boe_number")
        self.assertTrue(0.0 <= boe["threshold"] <= 1.0)
        self.assertTrue(boe["labels"])
        self.assertTrue(broker["extraction"]["line_items"])

    def test_documents_fields_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml",
                          "documents:\n  - id: x\n    label: X\n    description: d\n"
                          "    extraction:\n      fields:\n"
                          "        - name: total\n          type: octopus\n")
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml",
                          "documents:\n  - id: x\n    label: X\n    description: d\n"
                          "    extraction:\n      fields:\n"
                          "        - name: total\n          type: amount\n"
                          "        - name: total\n          type: str\n")
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml",
                          "documents:\n  - id: x\n    label: X\n    description: d\n"
                          "    extraction:\n      fields:\n"
                          "        - name: total\n          type: amount\n"
                          "          threshold: 1.5\n")
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))

    def test_documents_signals_legacy_keys_rejected(self):
        # D29: the broker-era signal keys are gone — warnings, not errors for
        # forward compat, but the contract ignores them.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml",
                          "documents:\n  - id: x\n    label: X\n    description: d\n"
                          "    signals:\n      source: brokers.yaml\n"
                          "    extraction:\n      schema: generic\n")
            with self.assertLogs("config", level="WARNING") as logs:
                docs = config.load_documents(Path(path))
            self.assertTrue(any("source" in msg for msg in logs.output))

    def test_documents_id_required_and_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml",
                          "documents:\n  - label: X\n    description: d\n"
                          "    extraction:\n      schema: generic\n")
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml",
                          "documents:\n" +
                          "\n".join("  - id: {0}\n    label: {0}\n    description: d\n"
                                    "    extraction:\n      schema: generic\n".format(i)
                                    for i in ("a", "a")))
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))

    def test_documents_empty_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "documents.yaml", "documents: []\n")
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))

    def test_profile_helpers(self):
        # D28 field-level helpers used by crosscheck/validate thresholds.
        cfg = config.load_all()
        field = config.profile_field_def(cfg["documents"], "broker_invoice", "boe_number")
        self.assertEqual(field["type"], "str")
        self.assertEqual(config.profile_field_def(cfg["documents"], "broker_invoice", "nope"), None)
        self.assertEqual(config.profile_min_score(cfg["documents"], "broker_invoice"), 0.5)
        self.assertEqual(config.profile_min_score(cfg["documents"], "nope"), 0.5)
        self.assertEqual(config.FIELD_TYPE_DEFAULTS["amount"], 0.95)


def _valid_thresholds_yaml() -> str:
    if config.REQUIRED_THRESHOLDS:
        lines = ["thresholds:"]
        lines += [f"  {k}: 0.9" for k in config.REQUIRED_THRESHOLDS]
    else:
        lines = ["thresholds: {}"]
    lines += [
        "quality:",
        "  min_lines_per_page: 5",
        "  min_words_per_page: 40",
        "  min_latin_ratio: 0.7",
        "  min_text_area_ratio: 0.02",
        "  max_pixel_std_dev: 12.0",
        "  max_dark_pixel_ratio: 0.05",
        "  review_band: 0.15",
        "  quality_probe: false",
        "verification:",
        "  grounding_margin_rel: 0.035",
        "  grounding_search_rel: 0.12",
        "  grounding_loose_score: 0.6",
        "  containment_ngram: 3",
        "  check_weights:",
        "    grounding: 0.25",
        "    arithmetic: 0.25",
        "    format: 0.15",
        "    disagreement: 0.35",
        "  accept_score: 0.8",
        "  review_score: 0.5",
        "  hard_fail_checks: [arithmetic, disagreement]",
        "  recheck_max_calls: 1",
    ]
    return "\n".join(lines) + "\n"


class TestValidationErrors(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load_models(Path("/nonexistent/models.yaml"))

    def test_malformed_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", "brokers: [unclosed")
            with self.assertRaises(config.ConfigError):
                config.load_documents(Path(path))

    def test_missing_required_key_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "models.yaml", "endpoint: https://x\n")
            with self.assertRaises(config.ConfigError):
                config.load_models(Path(path))

    def test_missing_documents_file(self):
        with self.assertRaises(config.ConfigError):
            config.load_documents(Path("/nonexistent/documents.yaml"))

    def test_wrong_type_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "thresholds.yaml", "thresholds:\n  total: \"high\"\n")
            with self.assertRaises(config.ConfigError):
                config.load_thresholds(Path(path))

    def test_threshold_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "thresholds.yaml", "thresholds:\n  total: 1.5\n")
            with self.assertRaises(config.ConfigError):
                config.load_thresholds(Path(path))

    def test_missing_quality_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "thresholds.yaml", "thresholds:\n  total: 0.9\n")
            with self.assertRaises(config.ConfigError):
                config.load_thresholds(Path(path))

    def test_verification_weights_missing_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "thresholds.yaml",
                          _valid_thresholds_yaml().replace("    format: 0.15\n",
                                                           ""))
            with self.assertRaises(config.ConfigError):
                config.load_thresholds(Path(path))

    def test_verification_review_above_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "thresholds.yaml",
                          _valid_thresholds_yaml().replace("  review_score: 0.5",
                                                           "  review_score: 0.9"))
            with self.assertRaises(config.ConfigError):
                config.load_thresholds(Path(path))


class TestWarnings(unittest.TestCase):
    def test_unknown_threshold_key_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = _valid_thresholds_yaml().replace("quality:\n", "typo_field: 0.9\nquality:\n")
            path = _write(tmp, "thresholds.yaml", content)
            with self.assertLogs("config", level="WARNING") as logs:
                config.load_thresholds(Path(path))
            self.assertTrue(any("unknown key 'typo_field'" in msg for msg in logs.output))

    def test_unknown_quality_key_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = _valid_thresholds_yaml().replace("review_band: 0.15", "review_band: 0.15\n  typo_floor: 0.1")
            path = _write(tmp, "thresholds.yaml", content)
            with self.assertLogs("config", level="WARNING") as logs:
                config.load_thresholds(Path(path))
            self.assertTrue(any("unknown key 'typo_floor'" in msg for msg in logs.output))


class TestDotenv(unittest.TestCase):
    def test_placeholder_env_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, ".env", "OPENCODE_API_KEY=test-placeholder-key\n")
            loaded = config.load_dotenv(Path(path))
            self.assertEqual(loaded["OPENCODE_API_KEY"], "test-placeholder-key")
            self.assertEqual(os.environ["OPENCODE_API_KEY"], "test-placeholder-key")
            os.environ.pop("OPENCODE_API_KEY")

    def test_existing_env_wins_over_file(self):
        os.environ["OPENCODE_API_KEY"] = "from-env"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write(tmp, ".env", "OPENCODE_API_KEY=from-file\n")
                config.load_dotenv(Path(path))
                self.assertEqual(os.environ["OPENCODE_API_KEY"], "from-env")
        finally:
            os.environ.pop("OPENCODE_API_KEY")

    def test_missing_env_file_warns_not_fails(self):
        with self.assertLogs("config", level="WARNING") as logs:
            loaded = config.load_dotenv(Path("/nonexistent/.env"))
        self.assertEqual(loaded, {})
        self.assertTrue(any("not found" in msg for msg in logs.output))


if __name__ == "__main__":
    unittest.main()

class TestWorkflows(unittest.TestCase):
    """Bulk-workflow contract (2026-09-05): named document bundles."""

    def _write(self, tmp, content):
        path = Path(tmp) / "workflows.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_workflows_load_real(self):
        # D53: structural — the shipped workflows are OWNER DATA (templates
        # and workflows are editable/deletable); assert the contract, not ids.
        cfg = config.load_workflows()
        self.assertIsInstance(cfg["warnings"], list)
        self.assertTrue(cfg["workflows"])
        for wf in cfg["workflows"]:
            self.assertTrue(wf.get("id"))
            self.assertTrue(wf.get("label"))
            self.assertTrue(wf.get("documents"))

    def test_unknown_profile_warns_but_loads(self):
        # D52: a deleted template is a warning, never a crash.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "workflows:\n"
                                  "  - id: w\n"
                                  "    label: W\n"
                                  "    documents: [broker_invoice, not_a_profile]\n")
            res = config.load_workflows(path)
            self.assertTrue(any("not_a_profile" in w for w in res["warnings"]))
            self.assertEqual(res["workflows"][0]["documents"],
                             ["broker_invoice", "not_a_profile"])

    def test_duplicate_documents_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "workflows:\n"
                                  "  - id: w\n"
                                  "    label: W\n"
                                  "    documents: [broker_invoice, broker_invoice]\n")
            with self.assertRaises(config.ConfigError):
                config.load_workflows(path)

    def test_duplicate_workflow_ids_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "workflows:\n"
                                  "  - id: w\n    label: A\n    documents: [broker_invoice]\n"
                                  "  - id: w\n    label: B\n    documents: [broker_invoice]\n")
            with self.assertRaises(config.ConfigError):
                config.load_workflows(path)

    def test_expected_order_preserved_and_may_exceed_documents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "workflows:\n"
                                  "  - id: w\n"
                                  "    label: W\n"
                                  "    documents: [broker_invoice]\n"
                                  "    expected: [hawb_doc, supplier_invoice, broker_invoice]\n")
            wf = config.load_workflows(path)["workflows"][0]
            self.assertEqual(wf["expected"], ["hawb_doc", "supplier_invoice", "broker_invoice"])


class TestCrossChecks(unittest.TestCase):
    """Cross-document validation rule contract (2026-09-07, D32; D52 policy:
    dangling references WARN, structural errors raise).

    D53: fully hermetic — synthetic profiles in a temp documents.yaml, so the
    contract tests never depend on the owner's editable/deletable templates.
    """

    DOCS = """documents:
- id: alpha
  label: A
  description: d
  signals: {keywords: []}
  extraction:
    fields:
    - {name: ref, type: str, description: d}
    - {name: date, type: date, description: d}
    - {name: amount, type: amount, description: d}
- id: beta
  label: B
  description: d
  signals: {keywords: []}
  extraction:
    fields:
    - {name: ref, type: str, description: d}
    - {name: amount, type: amount, description: d}
- id: gamma
  label: G
  description: d
  signals: {keywords: []}
  extraction:
    fields:
    - {name: ref, type: str, description: d}
- id: generic_doc
  label: GD
  description: d
  signals: {keywords: []}
  extraction:
    schema: generic
"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "documents.yaml").write_text(self.DOCS, encoding="utf-8")
        self._orig_docs = config.DOCUMENTS_PATH
        config.DOCUMENTS_PATH = self.tmp / "documents.yaml"

    def tearDown(self):
        config.DOCUMENTS_PATH = self._orig_docs
        self._tmp.cleanup()

    def _write(self, content: str) -> Path:
        path = self.tmp / "workflows.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_rule_loads(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha, beta]\n"
            "    cross_checks:\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: ref}\n"
            "      right: {template: beta, field: ref}\n")
        res = config.load_workflows(path)
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["workflows"][0]["cross_checks"][0]["id"], "r1")

    def test_template_not_in_documents_warns(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha]\n"
            "    cross_checks:\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: ref}\n"
            "      right: {template: beta, field: ref}\n")
        res = config.load_workflows(path)
        self.assertTrue(any(w.get("code") == "cross_template_not_in_workflow"
                            for w in res["warnings"]))
        # The rule stays declared (the UI shows it), it is just skipped.
        self.assertEqual(res["workflows"][0]["cross_checks"][0]["id"], "r1")

    def test_unknown_template_warns(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha, not_a_profile]\n")
        res = config.load_workflows(path)
        self.assertTrue(any(w.get("code") == "workflow_doc_missing"
                            and "not_a_profile" in str(w)
                            for w in res["warnings"]))
        self.assertEqual(res["workflows"][0]["documents"],
                         ["alpha", "not_a_profile"])

    def test_unknown_field_warns(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha, beta]\n"
            "    cross_checks:\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: nope}\n"
            "      right: {template: beta, field: ref}\n")
        res = config.load_workflows(path)
        self.assertTrue(any(w.get("code") == "cross_field_missing"
                            for w in res["warnings"]))

    def test_fieldless_workflow_member_warns(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [generic_doc, alpha]\n")
        res = config.load_workflows(path)
        self.assertTrue(any(w.get("code") == "workflow_doc_fieldless"
                            for w in res["warnings"]))

    def test_incompatible_kinds_warns(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha, beta]\n"
            "    cross_checks:\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: ref}\n"
            "      right: {template: beta, field: amount}\n")
        res = config.load_workflows(path)
        self.assertTrue(any(w.get("code") == "cross_kinds_incompatible"
                            for w in res["warnings"]))

    def test_same_template_both_sides_rejected(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha]\n"
            "    cross_checks:\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: ref}\n"
            "      right: {template: alpha, field: date}\n")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_workflows(path)
        self.assertIn("DIFFERENT templates", str(ctx.exception))

    def test_duplicate_rule_id_rejected(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha, beta]\n"
            "    cross_checks:\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: ref}\n"
            "      right: {template: beta, field: ref}\n"
            "    - id: r1\n"
            "      left: {template: alpha, field: date}\n"
            "      right: {template: beta, field: ref}\n")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_workflows(path)
        self.assertIn("duplicate cross-check id", str(ctx.exception))

    def test_missing_rule_id_rejected(self):
        path = self._write(
            "workflows:\n"
            "  - id: w\n    label: W\n"
            "    documents: [alpha, beta]\n"
            "    cross_checks:\n"
            "    - left: {template: alpha, field: ref}\n"
            "      right: {template: beta, field: ref}\n")
        with self.assertRaises(config.ConfigError):
            config.load_workflows(path)

    def test_cross_kinds_compatible(self):
        self.assertTrue(config.cross_kinds_compatible("str", "str"))
        self.assertTrue(config.cross_kinds_compatible("str", "name"))
        self.assertTrue(config.cross_kinds_compatible("name", "str"))
        self.assertTrue(config.cross_kinds_compatible("date", "date"))
        self.assertTrue(config.cross_kinds_compatible("amount", "amount"))
        self.assertFalse(config.cross_kinds_compatible("date", "str"))
        self.assertFalse(config.cross_kinds_compatible("amount", "date"))
        self.assertFalse(config.cross_kinds_compatible("object", "str"))
        self.assertFalse(config.cross_kinds_compatible("str", "object"))


class TestRealExports(unittest.TestCase):
    def test_real_file_loads_structurally(self):
        # D53: no id assertions — exports.yaml is owner data.
        res = config.load_exports()
        self.assertIsInstance(res["exports"], list)
        self.assertIsInstance(res["warnings"], list)


class TestExports(unittest.TestCase):
    """Excel export profile contract (D54) — hermetic temp configs."""

    DOCS = """documents:
- id: alpha
  label: A
  description: d
  signals: {keywords: []}
  extraction:
    fields:
    - {name: ref, type: str, description: d}
    - {name: amt, type: amount, description: d}
    line_items: true
    line_items_columns:
    - {name: description, description: d, type: str, role: description}
    - {name: amount, description: d, type: amount, role: amount}
- id: beta
  label: B
  description: d
  signals: {keywords: []}
  extraction:
    fields:
    - {name: ref, type: str, description: d}
"""
    WFS = """workflows:
- id: w
  label: W
  documents: [alpha, beta]
  cross_checks:
  - id: r1
    left: {template: alpha, field: ref}
    right: {template: beta, field: ref}
"""
    COL = "{header: Ref, source: {template: alpha, field: ref}}"

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "documents.yaml").write_text(self.DOCS, encoding="utf-8")
        (self.tmp / "workflows.yaml").write_text(self.WFS, encoding="utf-8")
        self._orig = (config.DOCUMENTS_PATH, config.WORKFLOWS_PATH, config.EXPORTS_PATH)
        config.DOCUMENTS_PATH = self.tmp / "documents.yaml"
        config.WORKFLOWS_PATH = self.tmp / "workflows.yaml"
        config.EXPORTS_PATH = self.tmp / "exports.yaml"

    def tearDown(self):
        (config.DOCUMENTS_PATH, config.WORKFLOWS_PATH, config.EXPORTS_PATH) = self._orig
        self._tmp.cleanup()

    def _load(self, body: str) -> dict:
        (self.tmp / "exports.yaml").write_text(body, encoding="utf-8")
        return config.load_exports()

    def _profile(self, columns: str, extra: str = "") -> str:
        return ("exports:\n- id: e\n  label: E\n  grain: file\n"
                + extra + "  columns:\n" + columns)

    def test_empty_exports_allowed(self):
        res = self._load("exports: []\n")
        self.assertEqual(res["exports"], [])

    def test_valid_profile_loads(self):
        res = self._load(self._profile(f"  - {self.COL}\n"))
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["exports"][0]["id"], "e")
        self.assertEqual(res["exports"][0]["grain"], "file")

    def test_structural_errors_raise(self):
        bad = [
            "exports:\n- label: E\n  columns:\n  - {header: H, source: {meta: original}}\n",
            "exports:\n- id: e\n  columns:\n  - {header: H, source: {meta: original}}\n",
            "exports:\n- id: e\n  label: E\n  grain: sideways\n  columns:\n  - {header: H, source: {meta: original}}\n",
            "exports:\n- id: e\n  label: E\n  columns: []\n",
            self._profile("  - {header: H, source: {meta: nope}}\n"),
            self._profile("  - {header: H, source: {template: alpha, field: ref}, type: octopus}\n"),
            self._profile("  - {header: H, source: {line_item: amount}}\n"),  # grain file
            "exports:\n- id: e\n  label: E\n  grain: line_item\n  columns:\n"
            "  - {header: H, source: {meta: original}}\n",  # missing line_item_template
        ]
        for body in bad:
            with self.subTest(body=body[:60]):
                with self.assertRaises(config.ConfigError):
                    self._load(body)

    def test_dangling_references_warn(self):
        cases = [
            (self._profile("  - {header: H, source: {template: ghost, field: ref}}\n"),
             "column_template_missing"),
            (self._profile("  - {header: H, source: {template: alpha, field: nope}}\n"),
             "column_field_missing"),
            (self._profile(f"  - {self.COL}\n", extra="  workflow: ghost_wf\n"),
             "export_workflow_missing"),
            ("exports:\n- id: e\n  label: E\n  grain: line_item\n"
             "  line_item_template: alpha\n  columns:\n"
             "  - {header: H, source: {line_item: nope}}\n",
             "column_line_item_missing"),
            (self._profile(f"  - {self.COL}\n",
                           extra="  workflow: w\n").replace(
                "- {header: Ref, source: {template: alpha, field: ref}}",
                "- {header: Chk, source: {cross_check: ghost_rule}}"),
             "column_cross_check_missing"),
        ]
        for body, code in cases:
            with self.subTest(code=code):
                res = self._load(body)
                self.assertTrue(any(w.get("code") == code for w in res["warnings"]),
                                res["warnings"])

    def test_duplicate_ids_raise(self):
        body = ("exports:\n"
                "- id: e\n  label: E\n  columns:\n"
                "  - {header: H, source: {meta: original}}\n"
                "- id: e\n  label: E2\n  columns:\n"
                "  - {header: H, source: {meta: original}}\n")
        with self.assertRaises(config.ConfigError):
            self._load(body)


class TestWorkflowResolution(unittest.TestCase):
    """D52: run-time resolution skips missing/fieldless members, keeps
    `expected` raw (missing_expected rows fire), errors only when NOTHING in
    the workflow is usable."""

    CFG = {
        "documents": {"documents": [
            {"id": "a", "label": "A", "description": "d", "signals": {},
             "extraction": {"fields": [{"name": "x", "type": "str",
                                        "description": "x"}]}},
            {"id": "generic", "label": "G", "description": "d", "signals": {},
             "extraction": {"schema": "generic"}},
        ]},
        "workflows": {"workflows": [
            {"id": "w", "documents": ["a", "ghost", "generic"],
             "expected": ["ghost", "a"], "cross_checks": [{"id": "r"}]},
            {"id": "empty", "documents": ["ghost"], "expected": ["ghost"]},
        ]},
    }

    def test_skips_missing_and_fieldless_keeps_expected_raw(self):
        import extract
        doc_ids, expected, rules = extract._resolve_workflow(self.CFG, "w")
        self.assertEqual(doc_ids, ["a"])
        self.assertEqual(expected, ["ghost", "a"])
        self.assertEqual(rules, [{"id": "r"}])

    def test_all_missing_is_an_error(self):
        import extract
        with self.assertRaises(ValueError):
            extract._resolve_workflow(self.CFG, "empty")


class TestFieldAnchor(unittest.TestCase):
    """Structural field anchors (2026-09-07, D33): optional part/section/
    value_side on profile fields — page-agnostic location in boxy documents."""

    def _write(self, tmp, fields_yaml):
        path = Path(tmp) / "documents.yaml"
        path.write_text("documents:\n"
                        "  - id: x\n    label: X\n    description: d\n"
                        "    signals:\n      keywords: []\n      min_score: 0.5\n"
                        "    extraction:\n      fields:\n"
                        + fields_yaml, encoding="utf-8")
        return path

    def test_anchor_loads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "        - name: a\n          type: str\n"
                                     "          anchor:\n"
                                     "            part: \"1\"\n"
                                     "            section: A\n"
                                     "            value_side: right\n")
            f = config.load_documents(path)["documents"][0]["extraction"]["fields"][0]
            self.assertEqual(f["anchor"]["part"], "1")
            self.assertEqual(f["anchor"]["section"], "A")
            self.assertEqual(f["anchor"]["value_side"], "right")

    def test_anchor_optional_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "        - name: a\n          type: str\n"
                                     "          anchor:\n            part: \"2\"\n")
            f = config.load_documents(path)["documents"][0]["extraction"]["fields"][0]
            self.assertEqual(f["anchor"], {"part": "2"})

    def test_anchor_bad_value_side_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "        - name: a\n          type: str\n"
                                     "          anchor:\n            value_side: sideways\n")
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_documents(path)
            self.assertIn("value_side must be one of", str(ctx.exception))

    def test_anchor_empty_part_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "        - name: a\n          type: str\n"
                                     "          anchor:\n            part: \"\"\n")
            with self.assertRaises(config.ConfigError):
                config.load_documents(path)

    def test_anchor_unknown_key_warns(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "        - name: a\n          type: str\n"
                                     "          anchor:\n            page: 3\n")
            with self.assertLogs("config", level="WARNING") as logs:
                config.load_documents(path)
            self.assertTrue(any("unknown key 'page'" in m for m in logs.output))


class TestStarterConfig(unittest.TestCase):
    """The bundled starter set (rules/starter/*) seeds every new install.

    Contract: it must always load — a new install must be able to extract
    immediately — and it must stay GENERIC (no maintainer templates).
    """

    STARTER = Path(__file__).resolve().parents[1] / "rules" / "starter"

    def test_starter_documents_loads_and_is_generic(self):
        docs = config.load_documents(self.STARTER / "documents.yaml")["documents"]
        ids = [d["id"] for d in docs]
        self.assertEqual(ids, ["any_document", "invoice_starter"])
        any_doc = next(d for d in docs if d["id"] == "any_document")
        self.assertNotIn("fields", any_doc["extraction"])
        self.assertEqual(any_doc["extraction"]["schema"], "generic")
        invoice = next(d for d in docs if d["id"] == "invoice_starter")
        names = [f["name"] for f in invoice["extraction"]["fields"]]
        self.assertEqual(names, ["invoice_number", "invoice_date",
                                 "vendor_name", "total_amount"])
        for f in invoice["extraction"]["fields"]:
            self.assertIn(f["type"], config.FIELD_TYPES)

    def test_starter_workflows_empty_is_valid(self):
        res = config.load_workflows(self.STARTER / "workflows.yaml")
        self.assertEqual(res, {"workflows": [], "warnings": []})

    def test_starter_exports_empty_is_valid(self):
        res = config.load_exports(self.STARTER / "exports.yaml")
        self.assertEqual(res["exports"], [])
        self.assertEqual(res["warnings"], [])

    def test_workflows_empty_list_allowed(self):
        # The starter state: a workflows file with an empty list must load
        # (mirrors load_exports' "empty is a valid starting state").
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflows.yaml"
            path.write_text("workflows: []\n", encoding="utf-8")
            res = config.load_workflows(path)
            self.assertEqual(res["workflows"], [])
            self.assertEqual(res["warnings"], [])
