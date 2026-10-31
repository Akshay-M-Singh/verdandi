"""Parallel batch-worker tests (D42) — offline, no paid calls.

`run_batch(workers=N)` runs files concurrently; each `_process_file` writes
only its own JSON and returns its flags/counters, merged by the caller in
submission order (deterministic flags.csv).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import extract


def _make_raw(tmp: str, stems: list[str]) -> Path:
    raw = Path(tmp) / "raw"
    for stem in stems:
        (raw / stem).mkdir(parents=True, exist_ok=True)
    return raw


class TestBatchWorkers(unittest.TestCase):
    def _run(self, workers: int):
        with tempfile.TemporaryDirectory() as tmp:
            raw = _make_raw(tmp, ["f1", "f2", "f3", "f4"])
            json_dir = Path(tmp) / "json"
            calls: list[str] = []

            def fake_extract(stem, raw_dir, **kw):
                calls.append(stem)
                return {"status": "extracted", "header_fields": [], "line_items": [],
                        "selected_document": {"name": stem},
                        "_meta": {"document": kw.get("doc_id"), "attempts": 1}}

            def fake_verify_file(stem, data, raw_dir, cfg, doc_id, pinned=None):
                return ([{"file": stem, "field": "x",
                          "reason": "verification_grounding"}],
                        {"fields": {}, "summary": {"failed_by_check": {"grounding": 1}}})

            captured: list[dict] = []
            with mock.patch.object(extract, "extract_file", fake_extract), \
                 mock.patch.object(extract.verify_mod, "verify_file", fake_verify_file), \
                 mock.patch.object(extract, "_document_quality_row", lambda *a: None), \
                 mock.patch.object(extract, "_write_summary", lambda *a, **k: None), \
                 mock.patch.object(extract, "write_flags",
                                   lambda flags, root: captured.extend(flags)):
                rc = extract.run_batch(raw, json_dir, doc_ids=["broker_invoice"],
                                       workers=workers)
            written = sorted(p.stem for p in json_dir.glob("*.json"))
            return rc, calls, captured, written

    def test_parallel_same_calls_and_order(self):
        rc, calls, flags, written = self._run(workers=3)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(calls), ["f1", "f2", "f3", "f4"])
        # merge is in submission order (stems sorted), so flags.csv is stable
        self.assertEqual([f["file"] for f in flags], ["f1", "f2", "f3", "f4"])
        self.assertEqual(written, ["f1", "f2", "f3", "f4"])

    def test_sequential_matches(self):
        rc, calls, flags, _ = self._run(workers=1)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["f1", "f2", "f3", "f4"])
        self.assertEqual([f["file"] for f in flags], ["f1", "f2", "f3", "f4"])


if __name__ == "__main__":
    unittest.main()
