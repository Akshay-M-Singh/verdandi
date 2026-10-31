"""Runs dashboard tests (D41) — offline.

`list_runs` enrichment (prompt/completion split, run kind, reviewed count) and
the chart frame builder (chronological order, per-file math, zero-token skip).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ui.lib as L


def _make_run(runs_root: Path, day: str, n: int, *,
              jsons: dict[str, dict] | None = None,
              flags: int = 0, reviewed: int = 0) -> Path:
    run = runs_root / day / str(n)
    (run / "json").mkdir(parents=True, exist_ok=True)
    for stem, data in (jsons or {}).items():
        (run / "json" / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")
    if flags:
        lines = ["file,field,reason"] + [
            f"f{i},x,verification_grounding" for i in range(flags)]
        (run / "flags.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if reviewed:
        rev = {f"f{i}|t|field|reason|detail": {"status": "confirmed", "note": ""}
               for i in range(reviewed)}
        (run / "review.json").write_text(json.dumps(rev), encoding="utf-8")
    return run


class TestListRuns(unittest.TestCase):
    def test_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "2026-09-20", 1, jsons={
                "a": {"status": "extracted",
                      "_meta": {"model": "m1", "usage": {"total_tokens": 1000,
                                                         "prompt_tokens": 700,
                                                         "completion_tokens": 300}}},
                "b": {"status": "failed", "notes": ["x"]},
            }, flags=3, reviewed=1)
            _make_run(root, "2026-09-21", 2, jsons={
                "c": {"status": "extracted",
                      "_meta": {"model": "m1", "workflow": {"id": "w"},
                                "usage": {"total_tokens": 500, "prompt_tokens": 400,
                                          "completion_tokens": 100}}},
            })
            _make_run(root, "2026-09-21", 3)  # capture-only
            with mock.patch.object(L, "RUNS", root):
                runs = L.list_runs()
            by = {(r["date"], r["n"]): r for r in runs}

            r1 = by[("2026-09-20", 1)]
            self.assertEqual((r1["ok"], r1["failed"], r1["files"]), (1, 1, 2))
            self.assertEqual(r1["tokens"], 1000)
            self.assertEqual(r1["prompt_tokens"], 700)
            self.assertEqual(r1["completion_tokens"], 300)
            self.assertEqual(r1["kind"], "extraction")
            self.assertEqual(r1["flags"], 3)
            self.assertEqual(r1["reviewed"], 1)

            r2 = by[("2026-09-21", 2)]
            self.assertEqual(r2["kind"], "workflow")
            self.assertEqual(r2["tokens"], 500)

            r3 = by[("2026-09-21", 3)]
            self.assertEqual((r3["kind"], r3["files"], r3["tokens"]),
                             ("capture", 0, 0))
            self.assertEqual(r3["reviewed"], 0)

    def test_no_runs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(L, "RUNS", Path(tmp) / "missing"):
                self.assertEqual(L.list_runs(), [])


class TestRunsChartFrame(unittest.TestCase):
    RUNS = [
        {"date": "2026-09-21", "n": 2, "kind": "workflow", "model": "m",
         "files": 2, "flags": 1, "tokens": 1000, "prompt_tokens": 800,
         "completion_tokens": 200},
        {"date": "2026-09-20", "n": 1, "kind": "extraction", "model": "m",
         "files": 0, "flags": 0, "tokens": 0, "prompt_tokens": 0,
         "completion_tokens": 0},
        {"date": "2026-09-20", "n": 3, "kind": "extraction", "model": "m",
         "files": 4, "flags": 0, "tokens": 800, "prompt_tokens": 400,
         "completion_tokens": 400},
    ]

    def test_total_chronological_and_skip(self):
        rows, skipped = L.runs_chart_frame(self.RUNS, "total")
        self.assertEqual(skipped, 1)
        self.assertEqual([r["run"] for r in rows], ["09-20 #3", "09-21 #2"])
        self.assertEqual(rows[0]["total"], 800)
        self.assertEqual(rows[1]["prompt"], 800)

    def test_per_file(self):
        rows, _ = L.runs_chart_frame(self.RUNS, "per_file")
        first = next(r for r in rows if r["run"] == "09-21 #2")
        self.assertEqual(first["prompt"], 400.0)
        self.assertEqual(first["completion"], 100.0)
        self.assertEqual(first["total"], 500.0)

    def test_per_file_zero_files_does_not_divide_by_zero(self):
        runs = [{"date": "2026-09-20", "n": 9, "files": 0, "tokens": 10,
                 "prompt_tokens": 10, "completion_tokens": 0}]
        rows, _ = L.runs_chart_frame(runs, "per_file")
        self.assertEqual(rows[0]["total"], 10.0)


if __name__ == "__main__":
    unittest.main()
