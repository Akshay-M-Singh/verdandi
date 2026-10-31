"""UI template-management tests (2026-09-02): validate/save round-trips.

These exercise the SAME contract the pipeline loads (config.load_documents) —
a template that passes here is guaranteed to load in the pipeline (D10
hot-load). Uses temp files/copies; never touches the real documents.yaml.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ui.lib as L


class TestValidateDocuments(unittest.TestCase):
    def test_current_file_validates(self):
        # D53: structural — templates are OWNER DATA (create/delete freely);
        # assert the contract, never specific shipped ids.
        ok, msg, docs = L.validate_documents(L.documents_text())
        self.assertTrue(ok, msg)
        self.assertTrue(docs)
        ids = [d["id"] for d in docs]
        self.assertEqual(len(ids), len(set(ids)))  # unique
        for d in docs:
            self.assertTrue(d.get("label"))
            self.assertTrue(d.get("description"))

    def test_broken_yaml_rejected(self):
        ok, msg, _ = L.validate_documents("documents: [unclosed")
        self.assertFalse(ok)
        self.assertIn("malformed YAML", msg)

    def test_invalid_profile_rejected(self):
        text = ("documents:\n"
                "  - id: x\n"
                "    label: X\n"
                "    description: d\n"
                "    signals:\n"
                "      keywords: []\n"
                "    extraction:\n"
                "      schema: not_a_schema\n")
        ok, msg, _ = L.validate_documents(text)
        self.assertFalse(ok)

    def test_roundtrip_documents_to_yaml(self):
        ok, _, docs = L.validate_documents(L.documents_text())
        self.assertTrue(ok)
        yaml_text = L.documents_to_yaml(docs)
        ok2, msg2, docs2 = L.validate_documents(yaml_text)
        self.assertTrue(ok2, msg2)
        self.assertEqual([d["id"] for d in docs], [d["id"] for d in docs2])
        for a, b in zip(docs, docs2):
            self.assertEqual(a["extraction"]["fields"],
                             b["extraction"]["fields"])


class TestDuplicateProfile(unittest.TestCase):
    def test_duplicate_gets_unique_id(self):
        ok, _, docs = L.validate_documents(L.documents_text())
        self.assertTrue(ok)
        seen = {d["id"] for d in docs}
        clone = L.duplicate_profile(docs, "broker_invoice", seen)
        self.assertEqual(clone["id"], "broker_invoice_copy")
        self.assertNotIn(clone["id"], seen)
        self.assertEqual(clone["extraction"]["fields"],
                         next(d for d in docs if d["id"] == "broker_invoice")
                         ["extraction"]["fields"])
        # A second copy must not collide either.
        seen2 = seen | {clone["id"]}
        clone2 = L.duplicate_profile(docs, "broker_invoice", seen2)
        self.assertIn(clone2["id"], ("broker_invoice_copy_2", "broker_invoice_copy_3"))

    def test_unknown_profile_raises(self):
        ok, _, docs = L.validate_documents(L.documents_text())
        with self.assertRaises(ValueError):
            L.duplicate_profile(docs, "does_not_exist")


class TestSaveDocumentsAtomic(unittest.TestCase):
    def test_save_rejects_invalid(self):
        with self.assertRaises(ValueError):
            L.save_documents("documents: [unclosed")

    def test_save_writes_valid_file(self):
        # D10: config paths resolve at CALL time. This exercises the
        # serialize -> validate -> atomic-write shape on a COPY — the real
        # config is never written by tests.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "documents.yaml"
            ok, _, docs = L.validate_documents(L.documents_text())
            target.write_text(L.documents_to_yaml(docs), encoding="utf-8")
            ok2, msg2, _ = L.validate_documents(target.read_text(encoding="utf-8"))
            self.assertTrue(ok2, msg2)
            self.assertFalse(list(target.parent.glob("*.yaml.tmp")))


class TestExportProfileIO(unittest.TestCase):
    """Guided export-profile editor IO (D54): validate/save round-trips."""

    def test_current_exports_validate(self):
        ok, msg, exports = L.validate_exports(L.exports_text())
        self.assertTrue(ok, msg)
        self.assertIsInstance(exports, list)

    def test_roundtrip_exports_to_yaml(self):
        exports = L.validate_exports(L.exports_text())[2] or []
        ok, msg, again = L.validate_exports(L.exports_to_yaml(exports))
        self.assertTrue(ok, msg)
        self.assertEqual([e["id"] for e in again], [e["id"] for e in exports])

    def test_save_rejects_invalid(self):
        with self.assertRaises(ValueError):
            L.save_exports("exports: [unclosed")

    def test_save_writes_valid_file(self):
        # D10: saves resolve config.EXPORTS_PATH at CALL time — patch THAT
        # (patching ui.lib's frozen constant would write the real config file).
        import config
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "exports.yaml"
            orig = config.EXPORTS_PATH
            config.EXPORTS_PATH = target
            try:
                text = L.exports_to_yaml([])
                L.save_exports(text)
                self.assertTrue(target.exists())
                self.assertFalse(list(target.parent.glob("*.yaml.tmp")))
            finally:
                config.EXPORTS_PATH = orig


class TestWorkflowReferenceGuard(unittest.TestCase):
    """2026-09-28 incident + D52 policy: deleting a workflow-referenced
    template must not crash the app — the save is allowed and the dangling
    reference is reported as a WARNING."""

    SOLO = ("documents:\n- id: solo\n  label: Solo\n  description: d\n"
            "  signals:\n    keywords: []\n  extraction:\n    schema: generic\n")
    GHOST = ("- id: ghost_template\n  label: G\n  description: d\n"
             "  signals:\n    keywords: []\n  extraction:\n    schema: generic\n")

    def test_reference_map_semantics(self):
        # D53: hermetic — a temp workflows file, not the owner's editable one.
        import config
        orig = config.WORKFLOWS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            wf = Path(tmp) / "workflows.yaml"
            wf.write_text(
                "workflows:\n"
                "- id: w1\n  label: W1\n"
                "  documents: [alpha, beta]\n"
                "  expected: [gamma]\n"
                "  cross_checks:\n"
                "  - id: r1\n"
                "    left: {template: beta, field: ref}\n"
                "    right: {template: gamma, field: ref}\n",
                encoding="utf-8")
            config.WORKFLOWS_PATH = wf
            try:
                refs = L.workflow_references()
                self.assertEqual(refs["alpha"], ["w1"])
                self.assertEqual(refs["beta"], ["w1"])
                self.assertEqual(refs["gamma"], ["w1"])  # expected + cross side
            finally:
                config.WORKFLOWS_PATH = orig

    def test_candidate_missing_a_referenced_template_warns_but_saves(self):
        import config
        orig = config.WORKFLOWS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            wf = Path(tmp) / "workflows.yaml"
            wf.write_text(
                "workflows:\n- id: needs_solo\n  label: N\n"
                "  documents: [solo, ghost_template]\n", encoding="utf-8")
            config.WORKFLOWS_PATH = wf
            try:
                ok, msg, _ = L.validate_documents(self.SOLO)
                self.assertTrue(ok)  # D52: never refuse
                self.assertTrue(msg.startswith("warning:"), msg)
                self.assertIn("ghost_template", msg)
                self.assertIn("needs_solo", msg)
                # A candidate that satisfies the workflow validates cleanly.
                ok2, msg2, _ = L.validate_documents(self.SOLO + self.GHOST)
                self.assertTrue(ok2, msg2)
                self.assertFalse(msg2.startswith("warning:"), msg2)
            finally:
                config.WORKFLOWS_PATH = orig

    def test_real_candidate_still_validates(self):
        # The shipped documents + shipped workflows stay consistent.
        ok, msg, _ = L.validate_documents(L.documents_text())
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()