"""Detached pipeline-stage runner tests (D37) — ui/lib helpers, offline.

The runner exists so paid stages survive Streamlit reruns/reconnects: the
process is detached, its log/exit live on disk, and `stage_status` can
re-attach to it from any session.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import ui.lib as L


def _await(run_dir: Path, stage: str, timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    status = L.stage_status(run_dir, stage)
    while status["exit"] is None and time.time() < deadline:
        time.sleep(0.2)
        status = L.stage_status(run_dir, stage)
    return status


@unittest.skipIf(os.name == "nt",
                 "ui.lib.start_stage launches via /bin/sh (POSIX only)")
class TestStageRunner(unittest.TestCase):
    def test_start_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            L.start_stage(run_dir, "demo",
                          [sys.executable, "-c", "print('hello stage')"])
            status = _await(run_dir, "demo")
            self.assertEqual(status["exit"], 0)
            self.assertFalse(status["running"])
            self.assertIn("hello stage", status["log"])

    def test_nonzero_exit_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            L.start_stage(run_dir, "demo",
                          [sys.executable, "-c", "import sys; sys.exit(3)"])
            status = _await(run_dir, "demo")
            self.assertEqual(status["exit"], 3)

    def test_killed_process_leaves_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            logs = run_dir / "logs"
            logs.mkdir(parents=True)
            (logs / "demo.pid").write_text("999999", encoding="utf-8")  # dead pid
            status = L.stage_status(run_dir, "demo")
            self.assertFalse(status["running"])
            self.assertIsNone(status["exit"])
            self.assertTrue(status["started"])

    def test_restart_clears_stale_exit_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            logs = run_dir / "logs"
            logs.mkdir(parents=True)
            (logs / "demo.exit").write_text("1", encoding="utf-8")
            L.start_stage(run_dir, "demo",
                          [sys.executable, "-c", "print('fresh')"])
            status = _await(run_dir, "demo")
            self.assertEqual(status["exit"], 0)
            self.assertIn("fresh", status["log"])


class TestExtractArgs(unittest.TestCase):
    """The Upload & Run / pin-panel command builders (multi-profile support)."""

    def test_multi_profiles_become_documents(self):
        args = L.extract(Path("/tmp/raw"), Path("/tmp/json"), "m",
                         doc_ids=["broker_invoice", "hawb_doc"], files=["s1"])
        self.assertIn("--documents", args)
        self.assertEqual(args[args.index("--documents") + 1],
                         "broker_invoice,hawb_doc")
        self.assertEqual(args[args.index("--files") + 1], "s1")

    def test_single_profile_is_document(self):
        args = L.extract(Path("/tmp/raw"), Path("/tmp/json"), "m",
                         doc_ids=["hawb_doc"])
        self.assertIn("--document", args)
        self.assertNotIn("--documents", args)
        self.assertEqual(args[args.index("--document") + 1], "hawb_doc")

    def test_pin_args_sorted(self):
        args = L.extract(Path("/tmp/raw"), Path("/tmp/json"), "m",
                         doc_ids=["a", "b"], pin_pages=[2, 1], pin_label="X")
        self.assertEqual(args[args.index("--pin-pages") + 1], "1,2")
        self.assertEqual(args[args.index("--pin-label") + 1], "X")


class TestResolveStem(unittest.TestCase):
    def test_manifest_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "manifest.json").write_text(json.dumps({"files": [
                {"stem": "abc-01", "original": "BOE.pdf", "sha": "abc"}]}),
                encoding="utf-8")
            self.assertEqual(L.resolve_stem(run, "abc-01"), "BOE.pdf")
            self.assertEqual(L.resolve_stem(run, "missing"), "missing")


if __name__ == "__main__":
    unittest.main()
