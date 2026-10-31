"""Desktop app (D66) tests — offline, no paid calls, no network.

Covers the three contracts of the Phase 0 scaffold:
1. ISOLATION — no core module imports anything from `desktop/`.
2. WORKSPACE + JOBS — path expansion, staging, queue lifecycle, progress.
3. HTTP API — endpoints over an injected fake runner (fastapi TestClient).

The real runner (desktop/engine_service/runner.py) is thin glue over the
CLI's own run_batch functions; it is not exercised here (that would need
PDFs + paid calls) — the fake runner writes the same artifacts the CLI
produces, which is exactly what the UI consumes.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.engine_service import jobs as jobs_mod  # noqa: E402
from desktop.engine_service import workspace as ws  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import desktop.engine_service.app as app_mod  # noqa: E402
    HAVE_FASTAPI = True
except Exception:  # noqa: BLE001 — desktop deps optional; starlette raises
    HAVE_FASTAPI = False  # RuntimeError (missing httpx2), not ImportError


# --- 1. isolation ------------------------------------------------------------

_CORE_DIRS = ("engines", "prompts", "ui", "tools")
_CORE_FILES = ("config.py", "extract.py", "compare.py", "verify.py", "validate.py",
               "text_layer.py", "aggregate.py", "grounding.py", "containment.py",
               "process.py", "eval.py", "eval_verification.py", "export.py",
               "model_catalog.py", "llm_client.py", "bench.py", "score.py",
               "download.py", "azure_acu.py")


class TestDesktopIsolation(unittest.TestCase):
    def test_core_never_imports_desktop(self):
        offenders: list[str] = []
        paths = [ROOT / n for n in _CORE_FILES]
        for d in _CORE_DIRS:
            paths.extend(sorted((ROOT / d).rglob("*.py")) if (ROOT / d).exists() else [])
        for path in paths:
            if not path.exists() or "desktop" in path.parts:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("import desktop", "from desktop")):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_desktop_imports_no_benchmark(self):
        offenders: list[str] = []
        for path in sorted((ROOT / "desktop").rglob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("import benchmark", "from benchmark")):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


# --- 2. workspace + jobs ------------------------------------------------------

def _touch_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")


def _read_text_with_retry(path: Path, tries: int = 20) -> str:
    """Windows: an immediate read after an atomic os.replace can transiently
    fail with PermissionError (antivirus/indexer holding the file) — retry
    briefly before giving up."""
    last: OSError | None = None
    for _ in range(tries):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError as exc:
            last = exc
            time.sleep(0.1)
    raise last  # type: ignore[misc]


def _fake_runner(job):
    """Writes the same artifacts the real capture+extract path produces."""
    run = Path(job.run_dir)
    for stem in job.stems:
        (run / "raw" / stem).mkdir(parents=True, exist_ok=True)
        (run / "raw" / stem / "report.json").write_text('{"pages": 1}\n')
        (run / "json").mkdir(exist_ok=True)
        (run / "json" / f"{stem}.json").write_text(json.dumps({
            "status": "extracted",
            "selected_document": {"name": "ACME CORP", "confidence": 0.9},
            "header_fields": [{"name": "invoice_number", "value": "X-1"}],
            "line_items": [{"description": "fee", "amount": "10.00"}],
        }, indent=2))
    with (run / "flags.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "field", "reason"])
        w.writerow([job.stems[0], "vendor_name", "verification_grounding"])


class TestWorkspace(unittest.TestCase):
    def test_expand_paths_recurses_and_dedupes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "a.pdf")
            _touch_pdf(t / "sub" / "b.PDF")
            (t / "notes.txt").write_text("x")
            found = ws.expand_pdf_paths([str(t), str(t / "a.pdf")])
            self.assertEqual([f.name for f in found], ["a.pdf", "b.PDF"])

    def test_expand_paths_errors(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with self.assertRaises(ValueError):
                ws.expand_pdf_paths([str(Path(tmp) / "missing")])
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(ValueError):
                ws.expand_pdf_paths([str(empty)])

    def test_stage_inputs_collisions_get_suffix(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            f1 = t / "one" / "same.pdf"
            f2 = t / "two" / "same.pdf"
            _touch_pdf(f1)
            _touch_pdf(f2)
            inbox = t / "inbox"
            stems = ws.stage_inputs([f1, f2], inbox)
            self.assertEqual(stems, ["same", "same_2"])
            self.assertEqual(len(list(inbox.glob("*.pdf"))), 2)


class TestJobManager(unittest.TestCase):
    def test_lifecycle_and_progress(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=_fake_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                done = mgr.wait(job.id)
                self.assertEqual(done.state, "done", done.error)
                self.assertEqual(done.stems, ["doc"])
                rows = mgr.progress(done)
                self.assertEqual(rows, [{"stem": "doc", "status": "extracted",
                                         "found": True, "error": None}])
                self.assertTrue((Path(done.run_dir) / "job.json").exists())
            finally:
                mgr.shutdown()

    def test_failure_is_captured(self):
        def boom(job):
            raise RuntimeError("engine exploded")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=boom, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                done = mgr.wait(job.id)
                self.assertEqual(done.state, "failed")
                self.assertIn("engine exploded", done.error)
            finally:
                mgr.shutdown()

    def test_terminal_pending_file_is_a_capture_failure(self):
        """A done job with no capture output must not show a 'pending' file."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=lambda job: None, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m",
                                 mode="capture")
                done = mgr.wait(job.id)
                self.assertEqual(done.state, "done")
                self.assertEqual(done.files[0]["status"], "failed")
                self.assertIn("capture produced no output",
                              done.files[0]["error"])
            finally:
                mgr.shutdown()

    def test_verification_model_recorded_and_retried(self):
        def partial(job):
            raw = Path(job.run_dir) / "raw" / job.stems[0]
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "report.json").write_text('{"pages": 1}\n')

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "a.pdf")
            _touch_pdf(t / "b.pdf")
            mgr = jobs_mod.JobManager(runner=partial, root=t / "ws")
            try:
                job = mgr.submit([str(t / "a.pdf"), str(t / "b.pdf")],
                                 doc_id="d", model="m", mode="capture",
                                 verification_model="off", name="  July batch ")
                done = mgr.wait(job.id)
                self.assertEqual(done.verification_model, "off")
                self.assertEqual(done.name, "July batch")  # trimmed
                retried = mgr.retry(job.id)
                self.assertEqual(retried.verification_model, "off")
                self.assertEqual(retried.name, "July batch")
                self.assertEqual(retried.stems, ["b"])
            finally:
                mgr.shutdown()

    def test_rename_persists_and_clears(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=_fake_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                renamed = mgr.rename(job.id, "  Forwarder July  ")
                self.assertEqual(renamed.name, "Forwarder July")
                data = json.loads(
                    _read_text_with_retry(Path(renamed.run_dir) / "job.json"))
                self.assertEqual(data["name"], "Forwarder July")
                self.assertEqual(mgr.rename(job.id, "").name, "")
                self.assertEqual(mgr.get(job.id).name, "")
                with self.assertRaises(ValueError):
                    mgr.rename("ghost", "x")
            finally:
                mgr.shutdown()

    def test_submit_rejects_bad_paths(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            mgr = jobs_mod.JobManager(runner=_fake_runner, root=Path(tmp) / "ws")
            try:
                with self.assertRaises(ValueError):
                    mgr.submit(["/nope/nothing.pdf"], doc_id="d", model="m")
            finally:
                mgr.shutdown()


class TestVerificationResolver(unittest.TestCase):
    """Backup model: "" -> primary, "off" -> disabled, id -> id (D66)."""

    def _job(self, verification_model: str, model: str = "primary"):
        return jobs_mod.Job(
            id="x", state="queued", created_at="", paths=[], stems=[],
            run_dir="", doc_id="d", model=model, workers=1,
            verification_model=verification_model)

    def test_empty_follows_primary(self):
        from desktop.engine_service import runner as runner_mod

        self.assertEqual(
            runner_mod.resolve_verification_model(self._job("")), "primary")

    def test_off_disables(self):
        from desktop.engine_service import runner as runner_mod

        self.assertEqual(
            runner_mod.resolve_verification_model(self._job("off")), "")

    def test_explicit_model(self):
        from desktop.engine_service import runner as runner_mod

        self.assertEqual(
            runner_mod.resolve_verification_model(self._job("m2")), "m2")


# --- 3. HTTP API --------------------------------------------------------------

@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestServiceAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        t = Path(self.tmp.name)
        _touch_pdf(t / "doc_a.pdf")
        _touch_pdf(t / "doc_b.pdf")
        self.mgr = jobs_mod.JobManager(runner=_fake_runner, root=t / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        self.tmp.cleanup()

    def test_health_and_defaults(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        d = self.client.get("/defaults").json()
        self.assertTrue(d["profiles"])
        self.assertTrue(d["primary_model"])

    def test_submit_and_poll(self):
        r = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "test-model",
        })
        self.assertEqual(r.status_code, 202)
        job_id = r.json()["id"]
        self.mgr.wait(job_id)
        detail = self.client.get(f"/jobs/{job_id}").json()
        self.assertEqual(detail["state"], "done")
        self.assertEqual(detail["files"][0]["status"], "extracted")

        docs = self.client.get(f"/jobs/{job_id}/documents").json()
        self.assertEqual(docs[0]["selected_name"], "ACME CORP")
        self.assertEqual(docs[0]["fields"], 1)

        doc = self.client.get(f"/jobs/{job_id}/documents/doc_a").json()
        self.assertEqual(doc["status"], "extracted")

        flags = self.client.get(f"/jobs/{job_id}/flags").json()
        self.assertEqual(flags[0]["reason"], "verification_grounding")

    def test_unknown_job_404s(self):
        self.assertEqual(self.client.get("/jobs/nope").status_code, 404)
        self.assertEqual(self.client.get("/jobs/nope/documents").status_code, 404)
        self.assertEqual(self.client.get("/jobs/nope/flags").status_code, 404)

    def test_submit_validation_400(self):
        r = self.client.post("/jobs", json={"paths": ["/nope/nothing.pdf"]})
        self.assertEqual(r.status_code, 400)


# --- 4. Phase 1: settings, evidence, review, export, retry, history ----------

from desktop.engine_service import settings as settings_mod  # noqa: E402


def _rich_runner(job):
    """Capture report + page render + extraction + one grounding flag."""
    run = Path(job.run_dir)
    from PIL import Image

    for stem in job.stems:
        raw = run / "raw" / stem
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "report.json").write_text('{"pages": 1}\n')
        Image.new("RGB", (400, 600), "white").save(raw / "page001.render-vision.png")
        (run / "json").mkdir(exist_ok=True)
        (run / "json" / f"{stem}.json").write_text(json.dumps({
            "status": "extracted",
            "_meta": {"document": job.doc_id,
                      "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            "selected_document": {"name": "ACME CORP", "confidence": 0.9},
            "header_fields": [{
                "name": "vendor_name", "value": "ACME", "confidence": 0.9,
                "source_region": {"page": 1, "bbox": [0.1, 0.1, 0.5, 0.2]},
                "verification": {"checks": [{
                    "check": "grounding", "passed": True,
                    "match": {"page": 1, "line_bbox": [0.1, 0.12, 0.5, 0.2],
                              "fragment_bbox": [0.1, 0.12, 0.5, 0.2],
                              "text": "Vendor: ACME"},
                }]},
            }],
            "line_items": [{"description": "fee", "amount": "10.00"}],
        }, indent=2))
    (run / "flags.csv").write_text(
        "file,template,field,reason,detail\n"
        f"{job.stems[0]},broker_invoice,vendor_name,verification_grounding,"
        "Vendor: ACME\n", encoding="utf-8")


def _capture_runner(job):
    run = Path(job.run_dir)
    for stem in job.stems:
        raw = run / "raw" / stem
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "report.json").write_text('{"pages": 2}\n')


class TestSettingsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        self._old_key = os.environ.pop("OPENCODE_API_KEY", None)
        os.environ["VERDANDI_WORKSPACE"] = self.tmp.name

    def tearDown(self):
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        if self._old_key is not None:
            os.environ["OPENCODE_API_KEY"] = self._old_key
        self.tmp.cleanup()

    def test_roundtrip(self):
        settings_mod.save_settings({"profile": "broker_invoice", "model": "m1",
                                    "workers": 3})
        data = settings_mod.load_settings()
        self.assertEqual(data["profile"], "broker_invoice")
        self.assertEqual(data["model"], "m1")
        self.assertEqual(data["workers"], 3)
        # blank values never clobber saved ones
        settings_mod.save_settings({"profile": "", "workers": None})
        self.assertEqual(settings_mod.load_settings()["profile"], "broker_invoice")

    @staticmethod
    def _no_keyring():
        """Deterministic 'no keyring backend'.

        CI runners install keyring AND have working backends (the old test
        assumed neither) — simulate absence so the honesty path is tested
        everywhere and no host keychain is ever touched."""
        return mock.patch.dict(sys.modules, {"keyring": None})

    def test_key_env(self):
        with self._no_keyring():
            self.assertFalse(settings_mod.key_present())
        os.environ["OPENCODE_API_KEY"] = "sk-test"
        self.assertTrue(settings_mod.key_present())

    def test_set_key_activates_env(self):
        with self._no_keyring():
            settings_mod.set_key("sk-live")  # env is set even without a keychain
        self.assertEqual(os.environ["OPENCODE_API_KEY"], "sk-live")

    def test_set_key_reports_persistence_honestly(self):
        # No keyring backend -> False (key works this session only).
        with self._no_keyring():
            self.assertFalse(settings_mod.set_key("sk-session"))
        self.assertEqual(os.environ.get("OPENCODE_API_KEY"), "sk-session")

    def test_set_key_persists_with_keyring(self):
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"keyring": fake}):
            self.assertTrue(settings_mod.set_key("sk-live"))
        fake.set_password.assert_called_once_with(
            settings_mod.SERVICE, settings_mod.ACCOUNT, "sk-live")

    def test_remove_key_clears_env(self):
        os.environ["OPENCODE_API_KEY"] = "sk-x"
        with self._no_keyring():
            settings_mod.remove_key()  # env is cleared regardless
        self.assertNotIn("OPENCODE_API_KEY", os.environ)

    def test_onboarded_roundtrip(self):
        self.assertFalse(settings_mod.load_settings().get("onboarded", False))
        settings_mod.save_settings({"onboarded": True})
        self.assertTrue(settings_mod.load_settings()["onboarded"])

    def test_verification_model_roundtrip(self):
        # "" is meaningful (same as primary) and must persist, unlike other keys
        settings_mod.save_settings({"verification_model": "off"})
        self.assertEqual(settings_mod.load_settings()["verification_model"],
                         "off")
        settings_mod.save_settings({"verification_model": ""})
        self.assertEqual(settings_mod.load_settings()["verification_model"],
                         "")
        settings_mod.save_settings({"verification_model": "m2"})
        self.assertEqual(settings_mod.load_settings()["verification_model"],
                         "m2")


class TestDiskHistory(unittest.TestCase):
    def _write_job(self, run: Path, job_id: str, state: str) -> None:
        run.mkdir(parents=True, exist_ok=True)
        (run / "job.json").write_text(json.dumps({
            "id": job_id, "state": state, "created_at": "2026-01-01T00:00:00",
            "paths": [], "stems": ["a"], "run_dir": str(run), "doc_id": "d",
            "model": "m", "workers": 1, "files": [],
        }))

    def test_history_loads_and_interrupted_becomes_failed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self._write_job(root / "runs" / "2026-01-01" / "1", "old1", "done")
            self._write_job(root / "runs" / "2026-01-02" / "1", "old2", "running")
            mgr = jobs_mod.JobManager(runner=_fake_runner, root=root)
            try:
                ids = [j.id for j in mgr.list()]
                self.assertIn("old1", ids)
                self.assertIn("old2", ids)
                self.assertEqual(mgr.get("old1").state, "done")
                interrupted = mgr.get("old2")
                self.assertEqual(interrupted.state, "failed")
                self.assertIn("interrupted", interrupted.error)
            finally:
                mgr.shutdown()


# Tests must never depend on rules/exports.yaml — the owner edits it live in
# the app (D53 hermetic principle). Seed a synthetic profile instead.
_SYNTHETIC_EXPORTS = """\
exports:
- id: unit_test_export
  label: Unit Test Export
  columns:
  - header: File
    source:
      meta: stem
  - header: Vendor
    source:
      template: broker_invoice
      field: vendor_name
"""


def _seed_temp_exports(case) -> None:
    """Repoint config.EXPORTS_PATH at a temp file with a synthetic profile."""
    import config

    case._exports_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    path = Path(case._exports_tmp.name) / "exports.yaml"
    path.write_text(_SYNTHETIC_EXPORTS, encoding="utf-8")
    case._old_exports_path = config.EXPORTS_PATH
    config.EXPORTS_PATH = path


def _restore_temp_exports(case) -> None:
    import config

    config.EXPORTS_PATH = case._old_exports_path
    case._exports_tmp.cleanup()


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestPhase1API(unittest.TestCase):
    def setUp(self):
        _seed_temp_exports(self)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.t = Path(self.tmp.name)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        os.environ["VERDANDI_WORKSPACE"] = str(self.t / "home")
        _touch_pdf(self.t / "doc_a.pdf")
        _touch_pdf(self.t / "doc_b.pdf")
        self.mgr = jobs_mod.JobManager(runner=_rich_runner, root=self.t / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        self.tmp.cleanup()
        _restore_temp_exports(self)

    def _run(self, stem: str = "doc_a", mode: str = "full") -> str:
        r = self.client.post("/jobs", json={
            "paths": [str(self.t / f"{stem}.pdf")],
            "doc_id": "broker_invoice", "model": "test-model", "mode": mode,
        })
        self.assertEqual(r.status_code, 202)
        job_id = r.json()["id"]
        self.mgr.wait(job_id)
        return job_id

    def test_flags_evidence_and_crop(self):
        job_id = self._run()
        flags = self.client.get(f"/jobs/{job_id}/flags").json()
        self.assertEqual(len(flags), 1)
        self.assertIsNone(flags[0]["review"])
        evidence = flags[0]["evidence"]
        self.assertEqual(evidence["page"], 1)
        self.assertEqual(evidence["text"], "Vendor: ACME")

        crop = self.client.get(
            f"/jobs/{job_id}/documents/doc_a/crop",
            params={"page": 1, "bbox": "0.1,0.12,0.5,0.2"})
        self.assertEqual(crop.status_code, 200)
        self.assertEqual(crop.headers["content-type"], "image/png")
        self.assertTrue(crop.content.startswith(b"\x89PNG"))

        page = self.client.get(f"/jobs/{job_id}/documents/doc_a/page/1")
        self.assertTrue(page.content.startswith(b"\x89PNG"))

        bad = self.client.get(f"/jobs/{job_id}/documents/doc_a/crop",
                              params={"bbox": "1,2,3"})
        self.assertEqual(bad.status_code, 400)

    def test_review_write_through(self):
        job_id = self._run()
        row = {"file": "doc_a", "template": "broker_invoice",
               "field": "vendor_name", "reason": "verification_grounding",
               "detail": "Vendor: ACME"}
        r = self.client.post(f"/jobs/{job_id}/review", json={
            "row": row, "status": "confirmed", "note": "checked", "value": "ACME CORP"})
        self.assertEqual(r.status_code, 200, r.text)

        flags = self.client.get(f"/jobs/{job_id}/flags").json()
        self.assertEqual(flags[0]["review"]["status"], "confirmed")

        job = self.mgr.get(job_id)
        data = json.loads((Path(job.run_dir) / "json" / "doc_a.json").read_text())
        self.assertEqual(data["header_fields"][0]["value"], "ACME CORP")
        self.assertEqual(data["header_fields"][0]["review"]["original_value"], "ACME")
        self.assertTrue((Path(job.run_dir) / "review.json").exists())

        bad = self.client.post(f"/jobs/{job_id}/review", json={
            "row": row, "status": "maybe"})
        self.assertEqual(bad.status_code, 400)

    def test_export_build_and_download(self):
        job_id = self._run()
        profiles = self.client.get("/exports").json()
        self.assertTrue(profiles)
        r = self.client.post(f"/jobs/{job_id}/export",
                             json={"profile": profiles[0]["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        name = r.json()["file"]
        dl = self.client.get(f"/jobs/{job_id}/export/{name}")
        self.assertEqual(dl.status_code, 200)
        self.assertTrue(dl.content.startswith(b"PK"))  # xlsx = zip
        self.assertEqual(self.client.get(
            f"/jobs/{job_id}/export/..%2Fjob.json").status_code, 404)
        missing = self.client.post(f"/jobs/{job_id}/export",
                                   json={"profile": "nope"})
        self.assertEqual(missing.status_code, 400)

    def test_retry_failed_files_only(self):
        job_id = self._run()
        job = self.mgr.get(job_id)
        (Path(job.run_dir) / "json" / "doc_a.json").unlink()
        r = self.client.post(f"/jobs/{job_id}/retry")
        self.assertEqual(r.status_code, 202, r.text)
        retry_id = r.json()["id"]
        self.mgr.wait(retry_id)
        retry = self.mgr.get(retry_id)
        self.assertEqual(retry.stems, ["doc_a"])
        self.assertEqual(retry.state, "done")

    def test_capture_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            mgr = jobs_mod.JobManager(runner=_capture_runner, root=Path(tmp) / "ws")
            app = app_mod.create_app(manager=mgr)
            client = TestClient(app)
            try:
                _touch_pdf(Path(tmp) / "doc_c.pdf")
                r = client.post("/jobs", json={
                    "paths": [str(Path(tmp) / "doc_c.pdf")],
                    "doc_id": "broker_invoice", "model": "m", "mode": "capture"})
                job_id = r.json()["id"]
                mgr.wait(job_id)
                detail = client.get(f"/jobs/{job_id}").json()
                self.assertEqual(detail["mode"], "capture")
                self.assertEqual(detail["files"][0]["status"], "captured")
                docs = client.get(f"/jobs/{job_id}/documents").json()
                self.assertEqual(docs[0]["pages"], 2)
                retry = client.post(f"/jobs/{job_id}/retry")
                self.assertEqual(retry.status_code, 400)
            finally:
                mgr.shutdown()

    def test_recompute_and_apply_all(self):
        job_id = self._run()
        row = {"file": "doc_a", "template": "broker_invoice",
               "field": "vendor_name", "reason": "verification_grounding",
               "detail": "Vendor: ACME"}
        self.client.post(f"/jobs/{job_id}/review", json={
            "row": row, "status": "confirmed", "note": "ok", "value": "ACME CORP"})

        rec = self.client.post(f"/jobs/{job_id}/flags/recompute")
        self.assertEqual(rec.status_code, 200, rec.text)
        self.assertIsInstance(rec.json()["flags"], int)

        job = self.mgr.get(job_id)
        data = json.loads((Path(job.run_dir) / "json" / "doc_a.json").read_text())
        data["header_fields"][0]["value"] = "CLOBBERED"
        (Path(job.run_dir) / "json" / "doc_a.json").write_text(
            json.dumps(data), encoding="utf-8")

        applied = self.client.post(f"/jobs/{job_id}/review/apply-all")
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertGreaterEqual(applied.json()["entries"], 1)
        data = json.loads((Path(job.run_dir) / "json" / "doc_a.json").read_text())
        self.assertEqual(data["header_fields"][0]["value"], "ACME CORP")

    def test_diagnostics_bundle_has_no_documents(self):
        job_id = self._run()
        job = self.mgr.get(job_id)
        logs = Path(job.run_dir) / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "worker.log").write_text("hello from the worker\n")

        r = self.client.get("/diagnostics")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(r.content)) as bundle:
            names = bundle.namelist()
            info = json.loads(bundle.read("info.json"))
        self.assertIn("info.json", names)
        self.assertTrue(any(n.startswith("logs/") for n in names))
        self.assertTrue(any(n.endswith(".pdf") for n in names) is False)
        self.assertTrue(all(not n.startswith("json/") for n in names))
        self.assertNotIn("api_key", json.dumps(info))

    def test_settings_endpoints(self):
        view = self.client.get("/settings").json()
        self.assertTrue(view["profile"])
        self.assertTrue(view["model"])
        self.assertIn("key_present", view)
        self.assertIn("onboarded", view)
        # Keychain discovery is entry-point based; the field tells support
        # (and the release smoke) whether the packaged app can persist a key.
        self.assertIn("keychain_backend", view)
        self.assertTrue(view["keychain_backend"] is None
                        or isinstance(view["keychain_backend"], str))
        r = self.client.post("/settings", json={"profile": "broker_invoice",
                                                "workers": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["profile"], "broker_invoice")
        self.assertEqual(r.json()["workers"], 3)


# --- 5. Batch 1: user config layer + model picker ----------------------------

from desktop.engine_service import modelsapi as modelsapi_mod  # noqa: E402
from desktop.engine_service import userconfig as userconfig_mod  # noqa: E402


class _ConfigPathGuard(unittest.TestCase):
    """Keep the engine's global config-path overrides from leaking between tests."""

    _KEYS = ("MODELS_PATH", "THRESHOLDS_PATH", "DOCUMENTS_PATH",
             "WORKFLOWS_PATH", "EXPORTS_PATH")

    def setUp(self):
        super().setUp()
        import config

        self._saved_paths = {k: getattr(config, k) for k in self._KEYS}
        self._saved_env = {k: os.environ.get(k)
                           for k in ("VERDANDI_WORKSPACE", "VERDANDI_USER_CONFIG")}

    def tearDown(self):
        import config

        for key, value in self._saved_paths.items():
            setattr(config, key, value)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()


class TestUserConfig(_ConfigPathGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["VERDANDI_WORKSPACE"] = self.tmp.name
        os.environ["VERDANDI_USER_CONFIG"] = "1"

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_bootstrap_seeds_and_points_engine_at_user_copies(self):
        import config

        info = userconfig_mod.bootstrap()
        self.assertTrue(info["active"])
        self.assertEqual(len(info["created"]), 5)
        self.assertEqual(config.MODELS_PATH,
                         Path(self.tmp.name) / "config" / "models.yaml")
        self.assertEqual(config.EXPORTS_PATH,
                         Path(self.tmp.name) / "rules" / "exports.yaml")
        # rules/* seed from the GENERIC starter set — never the maintainer's
        # working config (which may carry private templates).
        root = Path(self.tmp.name)
        bundled = userconfig_mod.bundled_root()
        seeded = (root / "rules" / "documents.yaml").read_text(encoding="utf-8")
        self.assertEqual(
            seeded, (bundled / "rules" / "starter" / "documents.yaml")
            .read_text(encoding="utf-8"))
        self.assertNotEqual(
            seeded, (bundled / "rules" / "documents.yaml")
            .read_text(encoding="utf-8"))
        self.assertEqual(config.load_workflows(root / "rules" / "workflows.yaml"),
                         {"workflows": [], "warnings": []})
        # second start never overwrites
        self.assertEqual(userconfig_mod.bootstrap()["created"], [])

    def test_reset_restores_shipped_defaults(self):
        userconfig_mod.bootstrap()
        models = Path(self.tmp.name) / "config" / "models.yaml"
        models.write_text("broken: true\n")
        written = userconfig_mod.reset_defaults()
        self.assertIn("config/models.yaml", written)
        self.assertIn("primary_model", models.read_text(encoding="utf-8"))

    def test_inactive_in_a_dev_checkout(self):
        os.environ.pop("VERDANDI_USER_CONFIG", None)
        info = userconfig_mod.bootstrap()
        self.assertFalse(info["active"])
        self.assertEqual(info["created"], [])


_SNAP_FIXTURE = {
    "catalog": {"ids": ["m1", "m2", "m3"], "error": None},
    "dev": {"error": None, "models": {
        "m1": {"cost_input": 0.2, "cost_output": 1.2, "context": 1_000_000},
    }},
    "verified": ["m1"],
    "primary": "m1",
    "statuses": {},
    "candidates": ["m2"],
    "text_only": [],
    "offline": False,
    "notes": {"m1": "fastest (measured)"},
}


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestModelsAPI(_ConfigPathGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["VERDANDI_WORKSPACE"] = str(Path(self.tmp.name) / "home")
        os.environ.pop("VERDANDI_USER_CONFIG", None)  # no global path mutation
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        self.mgr = jobs_mod.JobManager(runner=_fake_runner,
                                       root=Path(self.tmp.name) / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        self.tmp.cleanup()
        super().tearDown()

    def test_snapshot_builds_choices_and_candidates(self):
        with mock.patch("ui.lib.catalog_snapshot",
                        return_value=dict(_SNAP_FIXTURE)):
            body = self.client.get("/models").json()
        ids = [c["id"] for c in body["choices"]]
        self.assertEqual(ids[0], body["current"])  # current always selectable
        self.assertIn("m1", ids)                   # primary from the snapshot
        m1 = next(c for c in body["choices"] if c["id"] == "m1")
        self.assertTrue(m1["verified"])
        self.assertEqual([c["id"] for c in body["unverified"]], ["m2"])
        self.assertFalse(body["offline"])

    def test_choice_label_is_price_only(self):
        """Owner 2026-10-04: pickers show the model name + cost, nothing else."""
        with mock.patch("ui.lib.catalog_snapshot",
                        return_value=dict(_SNAP_FIXTURE)):
            body = self.client.get("/models").json()
        m1 = next(c for c in body["choices"] if c["id"] == "m1")
        self.assertEqual(m1["label"], "$0.2/$1.2")
        for noise in ("verified", "ctx", "fastest", "not verified"):
            self.assertNotIn(noise, m1["label"])

    def test_models_fall_back_when_current_left_go(self):
        # 2026-10-05: a saved model no longer in GO switches to the first
        # still-offered choice (config primary first) and persists.
        self.client.post("/settings", json={"model": "ghost-model"})
        with mock.patch("ui.lib.catalog_snapshot",
                        return_value=dict(_SNAP_FIXTURE)):
            body = self.client.get("/models").json()
        self.assertEqual(body["fallback"],
                         {"from": "ghost-model", "to": "m1"})
        self.assertEqual(self.client.get("/settings").json()["model"], "m1")

    def test_models_prune_reports_and_calls(self):
        self.client.post("/settings", json={"model": "m1"})  # in-catalog current
        snap = dict(_SNAP_FIXTURE)
        snap["catalog"] = {"ids": ["m1"], "error": None, "from_cache": False,
                           "removed": ["m2"], "new": [], "fetched_at": 1.0}
        with mock.patch("ui.lib.catalog_snapshot", return_value=snap), \
             mock.patch("ui.lib.prune_vision_verified",
                        return_value=["m2"]) as prune:
            body = self.client.get("/models").json()
        prune.assert_called_once_with(["m2"])
        self.assertEqual(body["pruned"], ["m2"])

    def test_verify_is_explicit_and_reports(self):
        with mock.patch("ui.lib.verify_model",
                        return_value="m2: vision OK (180 tokens) — verified"):
            r = self.client.post("/models/verify", json={"model": "m2"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("vision OK", r.json()["message"])

    def test_make_default_persists_in_settings(self):
        r = self.client.post("/models/default", json={"model": "m2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["model"], "m2")
        self.assertEqual(self.client.get("/settings").json()["model"], "m2")

    def test_snapshot_error_is_500(self):
        with mock.patch("ui.lib.catalog_snapshot", side_effect=RuntimeError("boom")):
            r = self.client.get("/models")
        self.assertEqual(r.status_code, 500)


# --- 6. Batch 2: capture -> inspect -> pin -> extract -------------------------


def _stage_runner(job):
    """Capture (unless extract mode), then extraction when asked."""
    run = Path(job.run_dir)
    if job.mode != "extract":
        for stem in job.stems:
            raw = run / "raw" / stem
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "report.json").write_text('{"pages": 2}\n')
    if job.mode == "capture":
        return
    (run / "json").mkdir(exist_ok=True)
    for stem in (job.extract_stems or job.stems):
        (run / "json" / f"{stem}.json").write_text(json.dumps({
            "status": "extracted",
            "selected_document": {"name": "ACME", "confidence": 0.9},
            "header_fields": [{"name": "invoice_number", "value": "X-1"}],
            "line_items": [],
        }, indent=2))
    (run / "flags.csv").write_text("file,field,reason,detail\n", encoding="utf-8")


class TestParsePages(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(ws.parse_pages("1-3,5"), [1, 2, 3, 5])
        self.assertEqual(ws.parse_pages(" 2 , 2 "), [2])
        self.assertIsNone(ws.parse_pages(""))
        self.assertIsNone(ws.parse_pages(None))
        with self.assertRaises(ValueError):
            ws.parse_pages("3-1")
        with self.assertRaises(ValueError):
            ws.parse_pages("x")
        with self.assertRaises(ValueError):
            ws.parse_pages("0")


class TestDevPreflight(unittest.TestCase):
    """`make desktop-dev` must fail clearly when ports are taken (D66)."""

    def test_port_in_use_detects_listener(self):
        import socket

        from desktop import dev

        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            self.assertTrue(dev._port_in_use(port))
        self.assertFalse(dev._port_in_use(port))

    def test_free_port_not_in_use(self):
        import socket

        from desktop import dev

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertFalse(dev._port_in_use(free))


class TestExtractStage(unittest.TestCase):
    def test_extract_requeues_same_job_with_pins(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=_stage_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m",
                                 mode="capture")
                mgr.wait(job.id)
                self.assertEqual(mgr.get(job.id).mode, "capture")
                mgr.extract(job.id, pinned_pages="1-3,5", pinned_label="broker")
                done = mgr.wait(job.id)
                self.assertEqual(done.mode, "extract")
                self.assertEqual(done.pinned_pages, "1-3,5")
                self.assertEqual(done.pinned_label, "broker")
                self.assertEqual(done.state, "done")
                self.assertEqual(done.files[0]["status"], "extracted")
            finally:
                mgr.shutdown()

    def test_extract_requires_capture_output(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=_stage_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                mgr.wait(job.id)
                # full mode still has raw output -> allowed (re-extract)
                mgr.extract(job.id)
                mgr.wait(job.id)
                # a job with no raw at all is rejected
                empty = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m",
                                   mode="capture")
                mgr.wait(empty.id)
                shutil.rmtree(Path(empty.run_dir) / "raw")
                with self.assertRaises(ValueError):
                    mgr.extract(empty.id)
            finally:
                mgr.shutdown()

    def test_bad_pin_spec_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(runner=_stage_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m",
                                 mode="capture")
                mgr.wait(job.id)
                with self.assertRaises(ValueError):
                    mgr.extract(job.id, pinned_pages="nope")
                self.assertEqual(mgr.get(job.id).mode, "capture")
            finally:
                mgr.shutdown()


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestBatch2API(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.t = Path(self.tmp.name)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        os.environ["VERDANDI_WORKSPACE"] = str(self.t / "home")
        _touch_pdf(self.t / "doc_a.pdf")
        self.mgr = jobs_mod.JobManager(runner=_stage_runner, root=self.t / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        self.tmp.cleanup()

    def _capture_job(self) -> str:
        r = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m", "mode": "capture"})
        job_id = r.json()["id"]
        self.mgr.wait(job_id)
        return job_id

    def test_extract_endpoint(self):
        job_id = self._capture_job()
        r = self.client.post(f"/jobs/{job_id}/extract",
                             json={"pinned_pages": "1-3", "pinned_label": "inv"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["mode"], "extract")
        self.mgr.wait(job_id)
        detail = self.client.get(f"/jobs/{job_id}").json()
        self.assertEqual(detail["state"], "done")
        self.assertEqual(detail["pinned_pages"], "1-3")
        self.assertEqual(detail["files"][0]["status"], "extracted")

        bad = self.client.post(f"/jobs/{job_id}/extract",
                               json={"pinned_pages": "junk"})
        self.assertEqual(bad.status_code, 400)

    def test_catalog_endpoint(self):
        job_id = self._capture_job()
        run_dir = Path(self.mgr.get(job_id).run_dir)

        def fake_catalog(rd, *, model, files=None):
            (rd / "catalog").mkdir(exist_ok=True)
            (rd / "catalog" / "doc_a.json").write_text(json.dumps({
                "file": "doc_a",
                "documents": [{"type": "invoice", "label": "ACME",
                               "pages": [1]}],
            }))
            return 0

        with mock.patch("desktop.engine_service.cataloging.run",
                        side_effect=fake_catalog):
            r = self.client.post(f"/jobs/{job_id}/catalog")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["cards"][0]["documents"][0]["type"], "invoice")

        cards = self.client.get(f"/jobs/{job_id}/catalog").json()
        self.assertEqual(cards[0]["stem"], "doc_a")
        self.assertTrue((run_dir / "catalog" / "doc_a.json").exists())


# --- 7. Batch 3: multi-profile + workflows -----------------------------------


def _multi_runner(job):
    """Writes a multi-document (workflow-style) extraction result."""
    run = Path(job.run_dir)
    for stem in job.stems:
        raw = run / "raw" / stem
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "report.json").write_text('{"pages": 1}\n')
    (run / "json").mkdir(exist_ok=True)
    for stem in job.stems:
        (run / "json" / f"{stem}.json").write_text(json.dumps({
            "status": "extracted_all",
            "documents": [
                {"template_id": "broker_invoice", "found": True,
                 "selected_document": {"name": "ACME", "confidence": 0.9},
                 "header_fields": [{"name": "invoice_number", "value": "X-1"}],
                 "line_items": [{"description": "fee", "amount": "10.00"}]},
                {"template_id": "bill_of_entry", "found": False,
                 "header_fields": [], "line_items": []},
            ],
            "notes": [],
        }, indent=2))
    (run / "flags.csv").write_text("file,field,reason,detail\n", encoding="utf-8")


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestBatch3API(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.t = Path(self.tmp.name)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        os.environ["VERDANDI_WORKSPACE"] = str(self.t / "home")
        _touch_pdf(self.t / "doc_a.pdf")
        self.mgr = jobs_mod.JobManager(runner=_multi_runner, root=self.t / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        self.tmp.cleanup()

    def test_multi_profile_job_and_documents_view(self):
        r = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc_a.pdf")],
            "doc_ids": ["broker_invoice", "bill_of_entry"],
            "model": "m"})
        self.assertEqual(r.status_code, 202, r.text)
        job_id = r.json()["id"]
        self.assertEqual(r.json()["doc_ids"],
                         ["broker_invoice", "bill_of_entry"])
        self.mgr.wait(job_id)
        docs = self.client.get(f"/jobs/{job_id}/documents").json()
        self.assertTrue(docs[0]["found"])
        self.assertEqual(docs[0]["selected_name"], "ACME")
        self.assertEqual(docs[0]["fields"], 1)       # counted across entries
        self.assertEqual(docs[0]["line_items"], 1)

    def test_workflow_job_and_conflicting_selectors(self):
        r = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc_a.pdf")],
            "workflow": "forwarder_chain", "model": "m"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["workflow"], "forwarder_chain")
        self.mgr.wait(r.json()["id"])

        bad = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc_a.pdf")],
            "workflow": "forwarder_chain", "doc_id": "broker_invoice"})
        self.assertEqual(bad.status_code, 400)
        bad2 = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc_a.pdf")],
            "doc_ids": ["a", "b"], "doc_id": "a"})
        self.assertEqual(bad2.status_code, 400)

    def test_workflows_endpoint_reads_shipped_config(self):
        body = self.client.get("/workflows").json()
        ids = [w["id"] for w in body["workflows"]]
        self.assertIn("forwarder_chain", ids)
        wf = next(w for w in body["workflows"] if w["id"] == "forwarder_chain")
        self.assertIn("broker_invoice", wf["documents"])
        self.assertTrue(wf["cross_checks"])
        self.assertIn("warnings", body)


# --- 8. Batch 5: worker-process jobs + cancel --------------------------------


def _stub_worker_cmd(run_dir: str, code: int = 0) -> list[str]:
    """A fake worker: marks every staged stem extracted, exits with `code`."""
    script = (
        "import json,pathlib,sys\n"
        f"r=pathlib.Path({run_dir!r})\n"
        "for p in sorted((r/'inbox').glob('*.pdf')):\n"
        "    (r/'raw'/p.stem).mkdir(parents=True, exist_ok=True)\n"
        "    (r/'json').mkdir(exist_ok=True)\n"
        "    (r/'json'/(p.stem+'.json')).write_text(json.dumps("
        "{'status':'extracted','header_fields':[],'line_items':[]}))\n"
        f"sys.exit({code})\n"
    )
    return [sys.executable, "-c", script]


class _WorkerWait:
    def _wait_state(self, mgr, job_id, states, timeout=15.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = mgr.get(job_id)
            if job and job.state in states:
                return job
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} never reached {states}")


class TestWorkerProcessJobs(unittest.TestCase, _WorkerWait):
    def test_subprocess_job_success(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(
                root=t / "ws",
                worker_cmd=lambda job: _stub_worker_cmd(job.run_dir))
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                done = mgr.wait(job.id)
                self.assertEqual(done.state, "done", done.error)
                self.assertEqual(done.files[0]["status"], "extracted")
                self.assertTrue(
                    (Path(done.run_dir) / "logs" / "worker.log").exists())
            finally:
                mgr.shutdown()

    def test_subprocess_failure_reports_exit_code(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(
                root=t / "ws",
                worker_cmd=lambda job: _stub_worker_cmd(job.run_dir, code=3))
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                done = mgr.wait(job.id)
                self.assertEqual(done.state, "failed")
                self.assertIn("code 3", done.error)
            finally:
                mgr.shutdown()

    def test_cancel_running_worker(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc.pdf")
            mgr = jobs_mod.JobManager(
                root=t / "ws",
                worker_cmd=lambda job: [sys.executable, "-c",
                                        "import time; time.sleep(30)"])
            try:
                job = mgr.submit([str(t / "doc.pdf")], doc_id="d", model="m")
                self._wait_state(mgr, job.id, ("running",))
                cancelled = mgr.cancel(job.id)
                self.assertEqual(cancelled.state, "cancelled")
                import time

                deadline = time.monotonic() + 5
                while mgr._procs.get(job.id) is not None and time.monotonic() < deadline:
                    time.sleep(0.05)  # the worker thread reaps + deregisters
                self.assertIsNone(mgr._procs.get(job.id))
            finally:
                mgr.shutdown()

    def test_cancel_queued_job_never_runs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "a.pdf")
            _touch_pdf(t / "b.pdf")
            mgr = jobs_mod.JobManager(
                root=t / "ws",
                worker_cmd=lambda job: [sys.executable, "-c",
                                        "import time; time.sleep(30)"])
            try:
                first = mgr.submit([str(t / "a.pdf")], doc_id="d", model="m")
                self._wait_state(mgr, first.id, ("running",))
                second = mgr.submit([str(t / "b.pdf")], doc_id="d", model="m")
                mgr.cancel(second.id)
                import time

                time.sleep(0.6)
                self.assertEqual(mgr.get(second.id).state, "cancelled")
                self.assertFalse(
                    (Path(second.run_dir) / "logs" / "worker.log").exists())
                mgr.cancel(first.id)
            finally:
                mgr.shutdown()


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestBatch5API(unittest.TestCase, _WorkerWait):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.t = Path(self.tmp.name)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        os.environ["VERDANDI_WORKSPACE"] = str(self.t / "home")
        _touch_pdf(self.t / "doc.pdf")
        self.mgr = jobs_mod.JobManager(
            root=self.t / "ws",
            worker_cmd=lambda job: [sys.executable, "-c",
                                    "import time; time.sleep(30)"])
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        self.tmp.cleanup()

    def test_cancel_endpoint_and_logs(self):
        r = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc.pdf")],
            "doc_id": "broker_invoice", "model": "m"})
        job_id = r.json()["id"]
        self._wait_state(self.mgr, job_id, ("running",))

        cancelled = self.client.post(f"/jobs/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "cancelled")

        again = self.client.post(f"/jobs/{job_id}/cancel")
        self.assertEqual(again.status_code, 400)

        logs = self.client.get(f"/jobs/{job_id}/logs").json()
        self.assertIn("log", logs)


# --- 9. Batch 1 (Phase 3): run lifecycle & disk ------------------------------


class TestRunLifecycle(unittest.TestCase):
    def test_storage_prune_delete(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc_a.pdf")
            mgr = jobs_mod.JobManager(runner=_rich_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc_a.pdf")], doc_id="d", model="m")
                mgr.wait(job.id)

                storage = mgr.storage(job.id)
                self.assertEqual(storage["renders_files"], 1)
                self.assertGreater(storage["renders_bytes"], 0)
                self.assertGreater(storage["total_bytes"],
                                   storage["renders_bytes"])

                res = mgr.prune_renders(job.id)
                self.assertEqual(res["files"], 1)
                self.assertGreater(res["freed_bytes"], 0)
                self.assertTrue(mgr.get(job.id).renders_pruned)
                self.assertEqual(mgr.storage(job.id)["renders_files"], 0)
                # metadata survives the prune
                self.assertTrue(
                    (Path(job.run_dir) / "json" / "doc_a.json").exists())

                mgr.delete(job.id)
                self.assertIsNone(mgr.get(job.id))
                self.assertFalse(Path(job.run_dir).exists())
            finally:
                mgr.shutdown()

    def test_delete_refuses_busy_runs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc_a.pdf")
            mgr = jobs_mod.JobManager(
                root=t / "ws",
                worker_cmd=lambda job: [sys.executable, "-c",
                                        "import time; time.sleep(30)"])
            try:
                job = mgr.submit([str(t / "doc_a.pdf")], doc_id="d", model="m")
                deadline = time.monotonic() + 15
                while mgr.get(job.id).state != "running" and time.monotonic() < deadline:
                    time.sleep(0.05)
                with self.assertRaises(ValueError):
                    mgr.delete(job.id)
                mgr.cancel(job.id)
                mgr.delete(job.id)
                self.assertIsNone(mgr.get(job.id))
            finally:
                mgr.shutdown()


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestDiskAPI(unittest.TestCase):
    def setUp(self):
        _seed_temp_exports(self)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.t = Path(self.tmp.name)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        os.environ["VERDANDI_WORKSPACE"] = str(self.t / "home")
        _touch_pdf(self.t / "doc_a.pdf")
        self.mgr = jobs_mod.JobManager(runner=_rich_runner, root=self.t / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        self.tmp.cleanup()
        _restore_temp_exports(self)

    def _run(self) -> str:
        r = self.client.post("/jobs", json={
            "paths": [str(self.t / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m"})
        job_id = r.json()["id"]
        self.mgr.wait(job_id)
        return job_id

    def test_storage_prune_delete_endpoints(self):
        job_id = self._run()
        st = self.client.get(f"/jobs/{job_id}/storage").json()
        self.assertEqual(st["renders_files"], 1)

        pruned = self.client.post(f"/jobs/{job_id}/prune").json()
        self.assertEqual(pruned["files"], 1)
        self.assertEqual(
            self.client.get(f"/jobs/{job_id}/storage").json()["renders_files"], 0)

        deleted = self.client.delete(f"/jobs/{job_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get(f"/jobs/{job_id}").status_code, 404)

    def test_export_auto_prunes_by_default(self):
        job_id = self._run()
        pid = self.client.get("/exports").json()[0]["id"]
        r = self.client.post(f"/jobs/{job_id}/export",
                             json={"profile": pid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pruned"]["files"], 1)
        self.assertEqual(
            self.client.get(f"/jobs/{job_id}/storage").json()["renders_files"], 0)

    def test_export_keeps_renders_when_setting_off(self):
        view = self.client.post("/settings",
                                json={"auto_prune_renders": False}).json()
        self.assertFalse(view["auto_prune_renders"])
        job_id = self._run()
        pid = self.client.get("/exports").json()[0]["id"]
        r = self.client.post(f"/jobs/{job_id}/export",
                             json={"profile": pid})
        self.assertIsNone(r.json()["pruned"])
        self.assertEqual(
            self.client.get(f"/jobs/{job_id}/storage").json()["renders_files"], 1)

    def test_workspace_storage_and_default_setting(self):
        self._run()
        usage = self.client.get("/storage").json()
        self.assertEqual(usage["runs"], 1)
        self.assertGreater(usage["total_bytes"], 0)
        self.assertTrue(self.client.get("/settings").json()["auto_prune_renders"])


# --- 10. Batch 2 (Phase 3): large-batch UX -----------------------------------


def _failing_runner(job):
    run = Path(job.run_dir)
    (run / "json").mkdir(exist_ok=True)
    for stem in job.stems:
        (run / "raw" / stem).mkdir(parents=True, exist_ok=True)
        (run / "raw" / stem / "report.json").write_text('{"pages": 1}\n')
        (run / "json" / f"{stem}.json").write_text(json.dumps({
            "file": stem, "status": "failed",
            "notes": ["extraction failed: boom"],
        }), encoding="utf-8")


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestBatchUxAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.t = Path(self.tmp.name)
        self._old_ws = os.environ.get("VERDANDI_WORKSPACE")
        os.environ["VERDANDI_WORKSPACE"] = str(self.t / "home")
        _touch_pdf(self.t / "a.pdf")
        _touch_pdf(self.t / "b.pdf")

    def tearDown(self):
        if self._old_ws is None:
            os.environ.pop("VERDANDI_WORKSPACE", None)
        else:
            os.environ["VERDANDI_WORKSPACE"] = self._old_ws
        self.tmp.cleanup()

    def _app(self, mgr):
        self.addCleanup(mgr.shutdown)
        return TestClient(app_mod.create_app(manager=mgr))

    def test_paths_summary(self):
        mgr = jobs_mod.JobManager(runner=_fake_runner, root=self.t / "ws")
        client = self._app(mgr)
        r = client.post("/paths/summary", json={"paths": [str(self.t)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["files"], 2)
        self.assertGreater(r.json()["bytes"], 0)
        bad = client.post("/paths/summary", json={"paths": ["/nope/x.pdf"]})
        self.assertEqual(bad.status_code, 400)

    def test_queue_position(self):
        mgr = jobs_mod.JobManager(
            root=self.t / "ws",
            worker_cmd=lambda job: [sys.executable, "-c",
                                    "import time; time.sleep(30)"])
        client = self._app(mgr)
        first = client.post("/jobs", json={
            "paths": [str(self.t / "a.pdf")], "doc_id": "d", "model": "m"}).json()
        second = client.post("/jobs", json={
            "paths": [str(self.t / "b.pdf")], "doc_id": "d", "model": "m"}).json()
        deadline = time.monotonic() + 15
        while mgr.get(first["id"]).state != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        detail = client.get(f"/jobs/{second['id']}").json()
        self.assertEqual(detail["queue_position"], 1)
        mgr.cancel(first["id"])

    def test_failed_file_reports_error(self):
        mgr = jobs_mod.JobManager(runner=_failing_runner, root=self.t / "ws")
        client = self._app(mgr)
        job = client.post("/jobs", json={
            "paths": [str(self.t / "a.pdf")], "doc_id": "d", "model": "m"}).json()
        mgr.wait(job["id"])
        detail = client.get(f"/jobs/{job['id']}").json()
        self.assertEqual(detail["files"][0]["status"], "failed")
        self.assertIn("boom", detail["files"][0]["error"])


# --- 11. Phase 4 Batch 1: templates editor -----------------------------------

from desktop.engine_service import templates as templates_mod  # noqa: E402

RULES_DIR = ROOT / "rules"


class _TempRulesMixin(_ConfigPathGuard):
    """Repoint config paths at temp COPIES of the shipped rules (never the
    real files) for template-editing tests."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        rules = Path(self.tmp.name) / "rules"
        rules.mkdir(parents=True)
        for name in ("documents.yaml", "workflows.yaml"):
            shutil.copy2(RULES_DIR / name, rules / name)
        # Synthetic exports (never the owner-edited shipped file, D53).
        (rules / "exports.yaml").write_text(_SYNTHETIC_EXPORTS, encoding="utf-8")
        import config

        config.DOCUMENTS_PATH = rules / "documents.yaml"
        config.WORKFLOWS_PATH = rules / "workflows.yaml"
        config.EXPORTS_PATH = rules / "exports.yaml"

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()


class TestTemplatesService(_TempRulesMixin):
    def test_save_update_duplicate_delete(self):
        listing = templates_mod.list_templates()
        ids = [t["id"] for t in listing["templates"]]
        self.assertIn("broker_invoice", ids)
        self.assertIn("broker_invoice", listing["workflow_refs"])

        # create
        created = templates_mod.save_template({
            "id": "unit_test_tpl", "label": "Unit Test",
            "description": "a unit test template",
            "signals": {"keywords": ["UNIT"], "min_score": 0.5},
            "extraction": {"fields": [{"name": "document_title", "type": "str",
                                       "description": "title", "threshold": 0.8}]},
        })
        self.assertEqual(created["profile"]["id"], "unit_test_tpl")
        self.assertIn("unit_test_tpl",
                      [t["id"] for t in templates_mod.list_templates()["templates"]])

        # update by original id (label change)
        updated = templates_mod.save_template({
            "id": "unit_test_tpl", "label": "Unit Test 2",
            "description": "a unit test template",
            "signals": {"keywords": [], "min_score": 0.5},
            "extraction": {"fields": [{"name": "document_title", "type": "str",
                                       "description": "title", "threshold": 0.8}]},
        }, original_id="unit_test_tpl")
        self.assertEqual(updated["profile"]["label"], "Unit Test 2")

        # duplicate
        dup = templates_mod.duplicate_template("unit_test_tpl")
        self.assertNotEqual(dup["profile"]["id"], "unit_test_tpl")
        self.assertIn("copy", dup["profile"]["id"])

        # delete + workflow warnings for a referenced template
        deleted = templates_mod.delete_template("broker_invoice")
        self.assertEqual(deleted["deleted"], "broker_invoice")
        self.assertTrue(deleted["warnings"])
        self.assertNotIn("broker_invoice",
                         [t["id"] for t in templates_mod.list_templates()["templates"]])

    def test_invalid_profile_refused(self):
        with self.assertRaises(ValueError):
            templates_mod.save_template({
                "id": "broken", "label": "Broken", "description": "d",
                "signals": {"keywords": []},
                "extraction": {"fields": [{"name": "x"}]},  # missing type/description
            })

    def test_save_preserves_advanced_profile_data(self):
        # The desktop editor keeps profile dicts verbatim: learned anchors,
        # line-item columns and row rules survive a save untouched (the
        # Streamlit draft round-trip tests retired with the UI — D67).
        profile = {
            "id": "advanced_tpl", "label": "Advanced", "description": "d",
            "signals": {"keywords": ["K"], "min_score": 0.5},
            "allow_no_match": True,
            "extraction": {
                "fields": [{"name": "x", "type": "str", "description": "xd",
                            "threshold": 0.9, "labels": ["X Label"],
                            "anchor": {"part": "2", "section": "B",
                                       "value_side": "below"}}],
                "line_items": True,
                "line_items_columns": [
                    {"name": "description", "description": "d", "type": "str",
                     "role": "description"},
                    {"name": "net", "description": "n", "type": "amount"},
                    {"name": "vat", "description": "v", "type": "amount"},
                    {"name": "gross", "description": "g", "type": "amount",
                     "role": "amount"},
                ],
                "row_arithmetic": [{"rule": "sum_equals",
                                    "fields": ["net", "vat"],
                                    "target": "gross", "tolerance": 0.02,
                                    "missing": "zero"}],
            },
        }
        templates_mod.save_template(profile)
        saved = next(t for t in templates_mod.list_templates()["templates"]
                     if t["id"] == "advanced_tpl")
        self.assertEqual(saved["extraction"]["fields"][0]["anchor"],
                         {"part": "2", "section": "B", "value_side": "below"})
        self.assertEqual(saved["extraction"]["row_arithmetic"],
                         profile["extraction"]["row_arithmetic"])
        self.assertEqual(saved["extraction"]["line_items_columns"],
                         profile["extraction"]["line_items_columns"])
        self.assertTrue(saved["allow_no_match"])

    def test_yaml_roundtrip_and_validation(self):
        text = templates_mod.get_yaml()
        ok = templates_mod.validate_yaml(text)
        self.assertTrue(ok["ok"], ok["message"])
        self.assertIn("broker_invoice", ok["ids"])

        bad = templates_mod.validate_yaml("documents: [unclosed")
        self.assertFalse(bad["ok"])

        saved = templates_mod.save_yaml(text)
        self.assertTrue(saved["path"].endswith("documents.yaml"))


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestTemplatesAPI(_TempRulesMixin):
    def setUp(self):
        super().setUp()
        os.environ["VERDANDI_WORKSPACE"] = str(Path(self.tmp.name) / "home")
        self.mgr = jobs_mod.JobManager(runner=_fake_runner,
                                       root=Path(self.tmp.name) / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        os.environ.pop("VERDANDI_WORKSPACE", None)
        super().tearDown()

    def test_template_endpoints(self):
        listing = self.client.get("/templates").json()
        self.assertTrue(listing["templates"])
        self.assertIn("broker_invoice", listing["workflow_refs"])

        saved = self.client.post("/templates", json={"profile": {
            "id": "api_tpl", "label": "API", "description": "d",
            "signals": {"keywords": [], "min_score": 0.5},
            "extraction": {"fields": [{"name": "document_title", "type": "str",
                                       "description": "t", "threshold": 0.8}]},
        }})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["profile"]["id"], "api_tpl")

        dup = self.client.post("/templates/duplicate", json={"id": "api_tpl"})
        self.assertEqual(dup.status_code, 200, dup.text)
        self.assertNotEqual(dup.json()["profile"]["id"], "api_tpl")

        deleted = self.client.delete("/templates/api_tpl")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted"], "api_tpl")

        yaml_text = self.client.get("/templates/yaml").json()["text"]
        ok = self.client.post("/templates/validate", json={"text": yaml_text})
        self.assertTrue(ok.json()["ok"])
        saved_yaml = self.client.post("/templates/yaml", json={"text": yaml_text})
        self.assertEqual(saved_yaml.status_code, 200, saved_yaml.text)

        bad = self.client.post("/templates", json={"profile": {"id": "x"}})
        self.assertEqual(bad.status_code, 400)


# --- 12. Phase 4 Batch 2: workflows editor -----------------------------------

from desktop.engine_service import workflows as workflows_mod  # noqa: E402


class TestWorkflowsService(_TempRulesMixin):
    def test_save_update_duplicate_delete(self):
        listing = workflows_mod.list_workflows()
        ids = [w["id"] for w in listing["workflows"]]
        self.assertIn("forwarder_chain", ids)
        self.assertIn("broker_invoice", [p["id"] for p in listing["profiles"]])
        broker = next(p for p in listing["profiles"] if p["id"] == "broker_invoice")
        self.assertIn("invoice_number", broker["fields"])

        base = {
            "id": "unit_wf", "label": "Unit",
            "documents": ["broker_invoice"], "expected": [],
            "cross_checks": [],
        }
        created = workflows_mod.save_workflow(dict(base))
        self.assertEqual(created["workflow"]["id"], "unit_wf")

        updated = workflows_mod.save_workflow(
            {**base, "label": "Unit 2"}, original_id="unit_wf")
        self.assertEqual(updated["workflow"]["label"], "Unit 2")

        dup = workflows_mod.duplicate_workflow("unit_wf")
        self.assertNotEqual(dup["workflow"]["id"], "unit_wf")
        self.assertIn("_copy", dup["workflow"]["id"])

        deleted = workflows_mod.delete_workflow("unit_wf")
        self.assertEqual(deleted["deleted"], "unit_wf")
        self.assertNotIn("unit_wf",
                         [w["id"] for w in workflows_mod.list_workflows()["workflows"]])

    def test_dangling_reference_is_a_warning_not_a_failure(self):
        res = workflows_mod.save_workflow({
            "id": "dangling_wf", "label": "D",
            "documents": ["ghost_template"], "expected": [],
            "cross_checks": [],
        })
        # D52: the save succeeds and the dangling reference is reported.
        self.assertTrue(any("ghost_template" in w for w in res["warnings"]),
                        res["warnings"])
        self.assertTrue(res["message"].startswith("warning:"), res["message"])

    def test_invalid_workflow_refused(self):
        with self.assertRaises(ValueError):
            workflows_mod.save_workflow({"label": "no id"})
        with self.assertRaises(ValueError):
            workflows_mod.save_workflow({
                "id": "bad_wf", "label": "B", "documents": "nope",
                "expected": [], "cross_checks": [],
            })

    def test_yaml_roundtrip_and_validation(self):
        text = workflows_mod.get_yaml()
        ok = workflows_mod.validate_yaml(text)
        self.assertTrue(ok["ok"], ok["message"])
        self.assertIn("forwarder_chain", ok["ids"])
        bad = workflows_mod.validate_yaml("workflows: [unclosed")
        self.assertFalse(bad["ok"])
        saved = workflows_mod.save_yaml(text)
        self.assertTrue(saved["path"].endswith("workflows.yaml"))


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestWorkflowsAPI(_TempRulesMixin):
    def setUp(self):
        super().setUp()
        os.environ["VERDANDI_WORKSPACE"] = str(Path(self.tmp.name) / "home")
        self.mgr = jobs_mod.JobManager(runner=_fake_runner,
                                       root=Path(self.tmp.name) / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        os.environ.pop("VERDANDI_WORKSPACE", None)
        super().tearDown()

    def test_workflow_endpoints(self):
        listing = self.client.get("/workflows").json()
        self.assertTrue(listing["workflows"])
        self.assertTrue(listing["profiles"])

        saved = self.client.post("/workflows", json={"workflow": {
            "id": "api_wf", "label": "API", "documents": ["broker_invoice"],
            "expected": [], "cross_checks": [],
        }})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["workflow"]["id"], "api_wf")

        dup = self.client.post("/workflows/duplicate", json={"id": "api_wf"})
        self.assertEqual(dup.status_code, 200, dup.text)

        deleted = self.client.delete("/workflows/api_wf")
        self.assertEqual(deleted.status_code, 200, deleted.text)

        yaml_text = self.client.get("/workflows/yaml").json()["text"]
        ok = self.client.post("/workflows/validate", json={"text": yaml_text})
        self.assertTrue(ok.json()["ok"])
        saved_yaml = self.client.post("/workflows/yaml", json={"text": yaml_text})
        self.assertEqual(saved_yaml.status_code, 200, saved_yaml.text)

        bad = self.client.post("/workflows", json={"workflow": {"label": "x"}})
        self.assertEqual(bad.status_code, 400)


# --- 13. Phase 4 Batch 3: export builder -------------------------------------


class TestExportBuilderService(_TempRulesMixin):
    def test_catalog_and_warnings(self):
        draft = {
            "id": "d", "label": "D", "workflow": "forwarder_chain",
            "grain": "line_item", "line_item_template": "broker_invoice",
            "columns": [
                {"header": "File", "source": {"meta": "original"}},
                {"header": "Inv", "source": {"template": "broker_invoice",
                                             "field": "invoice_number"}},
                {"header": "Total", "source": {"line_item": "total"}},
                {"header": "BOE", "source": {"cross_check":
                    "broker_invoice.boe_number__bill_of_entry.boe_number"}},
            ],
        }
        from desktop.engine_service import exporting as exp_mod

        cat = exp_mod.catalog(draft)
        labels = [s["label"] for s in cat["sources"]]
        self.assertTrue(any("Original filename" in l for l in labels))
        self.assertTrue(any("Invoice Number" in l for l in labels))
        self.assertTrue(any("Line items · Total" in l for l in labels))
        self.assertTrue(any(l.startswith("Check ·") for l in labels))
        self.assertEqual(cat["warnings"], [])
        self.assertIn("broker_invoice", cat["line_item_templates"])

        bad = exp_mod.catalog({**draft, "columns": [
            {"header": "Ghost", "source": {"template": "ghost", "field": "x"}},
        ]})
        self.assertTrue(bad["warnings"])

    def test_profile_crud_and_seed(self):
        from desktop.engine_service import exporting as exp_mod

        seeded = exp_mod.seed_from_workflow("forwarder_chain")["profile"]
        self.assertEqual(seeded["grain"], "line_item")
        self.assertEqual(seeded["line_item_template"], "broker_invoice")
        headers = [c["header"] for c in seeded["columns"]]
        self.assertIn("Invoice Number", headers)
        self.assertIn("Total", headers)
        self.assertIn("BOE check", headers)

        created = exp_mod.save_profile(seeded)
        self.assertEqual(created["profile"]["id"], "forwarder_chain_export")
        updated = exp_mod.save_profile({**seeded, "label": "Seeded v2"},
                                       original_id="forwarder_chain_export")
        self.assertEqual(updated["profile"]["label"], "Seeded v2")

        dup = exp_mod.duplicate_profile("forwarder_chain_export")
        self.assertIn("_copy", dup["profile"]["id"])

        deleted = exp_mod.delete_profile("forwarder_chain_export")
        self.assertEqual(deleted["deleted"], "forwarder_chain_export")
        self.assertNotIn("forwarder_chain_export",
                         [p["id"] for p in exp_mod.list_profiles()])

        with self.assertRaises(ValueError):
            exp_mod.save_profile({"label": "no id"})

    def test_preview_uses_the_newest_run(self):
        from desktop.engine_service import exporting as exp_mod

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "doc_a.pdf")
            mgr = jobs_mod.JobManager(runner=_rich_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "doc_a.pdf")], doc_id="broker_invoice",
                                 model="m")
                mgr.wait(job.id)
                exp_mod.bind_manager(mgr)
                try:
                    draft = {
                        "id": "p", "label": "P", "grain": "file",
                        "columns": [{"header": "Vendor", "source": {
                            "template": "broker_invoice", "field": "vendor_name"}}],
                    }
                    prev = exp_mod.preview(draft)
                    self.assertEqual(prev["headers"], ["Vendor"])
                    self.assertEqual(prev["rows"][0]["cells"][0]["value"], "ACME")
                    with self.assertRaises(ValueError):
                        exp_mod.preview(draft, run_id="nope")
                finally:
                    exp_mod.bind_manager(None)
            finally:
                mgr.shutdown()


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestExportBuilderAPI(_TempRulesMixin):
    def setUp(self):
        super().setUp()
        os.environ["VERDANDI_WORKSPACE"] = str(Path(self.tmp.name) / "home")
        self.mgr = jobs_mod.JobManager(runner=_rich_runner,
                                       root=Path(self.tmp.name) / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        os.environ.pop("VERDANDI_WORKSPACE", None)
        super().tearDown()

    def test_export_builder_endpoints(self):
        profiles = self.client.get("/exports").json()
        self.assertTrue(profiles)
        self.assertIn("columns", profiles[0])

        seed = self.client.post("/exports/seed",
                                json={"workflow_id": "forwarder_chain"})
        self.assertEqual(seed.status_code, 200, seed.text)
        draft = seed.json()["profile"]

        cat = self.client.post("/exports/catalog", json={"profile": draft})
        self.assertEqual(cat.status_code, 200, cat.text)
        self.assertTrue(cat.json()["sources"])

        saved = self.client.post("/exports", json={"profile": draft})
        self.assertEqual(saved.status_code, 200, saved.text)
        pid = saved.json()["profile"]["id"]

        dup = self.client.post("/exports/duplicate", json={"id": pid})
        self.assertEqual(dup.status_code, 200, dup.text)

        deleted = self.client.delete(f"/exports/{pid}")
        self.assertEqual(deleted.status_code, 200, deleted.text)

        yaml_text = self.client.get("/exports/yaml").json()["text"]
        ok = self.client.post("/exports/validate", json={"text": yaml_text})
        self.assertTrue(ok.json()["ok"])

        bad = self.client.post("/exports", json={"profile": {"label": "x"}})
        self.assertEqual(bad.status_code, 400)

    def test_export_preview_endpoint(self):
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        job = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m"}).json()
        self.mgr.wait(job["id"])
        r = self.client.post("/exports/preview", json={"profile": {
            "id": "p", "label": "P", "grain": "file",
            "columns": [{"header": "Vendor", "source": {
                "template": "broker_invoice", "field": "vendor_name"}}],
        }})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows"][0]["cells"][0]["value"], "ACME")


# --- 14. Phase 4 parity close-out: scoped extract/catalog, metrics, guards ----


class TestScopedExtract(_TempRulesMixin):
    def test_extract_scoped_to_one_stem(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "a.pdf")
            _touch_pdf(t / "b.pdf")
            mgr = jobs_mod.JobManager(runner=_stage_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "a.pdf"), str(t / "b.pdf")],
                                 doc_id="d", model="m", mode="capture")
                mgr.wait(job.id)
                mgr.extract(job.id, stems=["a"], pinned_pages="1")
                done = mgr.wait(job.id)
                self.assertEqual(done.extract_stems, ["a"])
                self.assertTrue((Path(done.run_dir) / "json" / "a.json").exists())
                self.assertFalse((Path(done.run_dir) / "json" / "b.json").exists())
            finally:
                mgr.shutdown()

    def test_unknown_scope_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t = Path(tmp)
            _touch_pdf(t / "a.pdf")
            mgr = jobs_mod.JobManager(runner=_stage_runner, root=t / "ws")
            try:
                job = mgr.submit([str(t / "a.pdf")], doc_id="d", model="m",
                                 mode="capture")
                mgr.wait(job.id)
                with self.assertRaises(ValueError):
                    mgr.extract(job.id, stems=["ghost"])
            finally:
                mgr.shutdown()


@unittest.skipUnless(HAVE_FASTAPI, "desktop deps not installed")
class TestParityCloseout(_TempRulesMixin):
    def setUp(self):
        super().setUp()
        os.environ["VERDANDI_WORKSPACE"] = str(Path(self.tmp.name) / "home")
        self.mgr = jobs_mod.JobManager(runner=_rich_runner,
                                       root=Path(self.tmp.name) / "ws")
        self.app = app_mod.create_app(manager=self.mgr)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.mgr.shutdown()
        os.environ.pop("VERDANDI_WORKSPACE", None)
        super().tearDown()

    def test_metrics_endpoint(self):
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        job = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m"}).json()
        self.mgr.wait(job["id"])
        m = self.client.get(f"/jobs/{job['id']}/metrics").json()
        self.assertEqual(m["flags"], 1)
        self.assertEqual(m["pending"], 1)
        self.assertEqual(m["prompt_tokens"], 100)
        self.assertEqual(m["completion_tokens"], 20)
        self.assertEqual(m["per_file"]["doc_a"]["prompt_tokens"], 100)
        self.assertEqual(m["per_file"]["doc_a"]["completion_tokens"], 20)
        self.assertEqual(m["per_file"]["doc_a"]["rechecks"], 0)

        batch = self.client.post("/jobs/metrics", json={"ids": [job["id"]]}).json()
        self.assertEqual(batch[job["id"]]["flags"], 1)
        self.assertEqual(batch[job["id"]]["per_file"]["doc_a"]["prompt_tokens"], 100)

    def test_rates_endpoint(self):
        fake = {
            "models": {"m": {"cost_input": 0.2, "cost_output": 1.2},
                       "free": {"cost_input": None, "cost_output": None}},
            "fetched_at": 1.0, "from_cache": True, "error": None,
        }
        with mock.patch("model_catalog.fetch_models_dev", return_value=fake):
            res = self.client.get("/models/rates").json()
        self.assertEqual(res["models"]["m"],
                         {"input_per_1m": 0.2, "output_per_1m": 1.2})
        self.assertNotIn("free", res["models"])  # no rates -> omitted
        self.assertEqual(res["source"], "models.dev")

    def test_rates_endpoint_offline_is_empty(self):
        with mock.patch("model_catalog.fetch_models_dev",
                        return_value={"models": {}, "fetched_at": None,
                                      "from_cache": False, "error": "offline"}):
            res = self.client.get("/models/rates").json()
        self.assertEqual(res["models"], {})
        self.assertEqual(res["error"], "offline")

    def test_backup_model_setting_flows_into_jobs(self):
        r = self.client.post("/settings", json={"verification_model": "off"})
        self.assertEqual(r.json()["verification_model"], "off")
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        job = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m"}).json()
        self.assertEqual(job["verification_model"], "off")  # saved default

        _touch_pdf(Path(self.tmp.name) / "doc_b.pdf")
        job2 = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_b.pdf")],
            "doc_id": "broker_invoice", "model": "m",
            "verification_model": "m2"}).json()
        self.assertEqual(job2["verification_model"], "m2")  # per-run wins

        r = self.client.post("/settings", json={"verification_model": ""})
        self.assertEqual(r.json()["verification_model"], "")  # same-as-primary

    def test_run_name_submit_and_rename(self):
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        job = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m",
            "name": "July batch"}).json()
        self.assertEqual(job["name"], "July batch")

        r = self.client.post(f"/jobs/{job['id']}/rename",
                             json={"name": "Forwarder July"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "Forwarder July")
        self.assertEqual(
            self.client.get(f"/jobs/{job['id']}").json()["name"],
            "Forwarder July")

        cleared = self.client.post(f"/jobs/{job['id']}/rename",
                                   json={"name": ""})
        self.assertEqual(cleared.json()["name"], "")
        self.assertEqual(
            self.client.post("/jobs/ghost/rename", json={"name": "x"}).status_code,
            404)

    def test_summary_endpoint(self):
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        job = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m"}).json()
        self.mgr.wait(job["id"])
        run = Path(self.mgr.get(job["id"]).run_dir)
        (run / "summary.csv").write_text(
            "file,template,status,pages\ndoc_a,broker_invoice,extracted,1\n")
        rows = self.client.get(f"/jobs/{job['id']}/summary").json()
        self.assertEqual(rows[0]["template"], "broker_invoice")

    def test_catalog_scoped_to_stem(self):
        _touch_pdf(Path(self.tmp.name) / "doc_a.pdf")
        job = self.client.post("/jobs", json={
            "paths": [str(Path(self.tmp.name) / "doc_a.pdf")],
            "doc_id": "broker_invoice", "model": "m",
            "mode": "capture"}).json()
        self.mgr.wait(job["id"])
        seen: dict = {}

        def fake_catalog(rd, *, model, files=None):
            seen["files"] = files
            return 0

        with mock.patch("desktop.engine_service.cataloging.run",
                        side_effect=fake_catalog):
            r = self.client.post(f"/jobs/{job['id']}/catalog",
                                 json={"stem": "doc_a"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen["files"], {"doc_a"})

    def test_models_force_refresh(self):
        # Save an in-catalog model so the auto-fallback (2026-10-05) doesn't
        # add a second snapshot call.
        self.client.post("/settings", json={"model": "m1"})
        with mock.patch("ui.lib.catalog_snapshot",
                        return_value=dict(_SNAP_FIXTURE)) as snap:
            self.client.get("/models?force=1")
        snap.assert_called_once_with(force=True)

    def test_last_template_and_workflow_guards(self):
        from desktop.engine_service import templates as tpl_mod
        from desktop.engine_service import workflows as wf_mod

        ids = [t["id"] for t in tpl_mod.list_templates()["templates"]]
        for tid in ids[:-1]:
            tpl_mod.delete_template(tid)
        with self.assertRaises(ValueError):
            tpl_mod.delete_template(ids[-1])

        wids = [w["id"] for w in wf_mod.list_workflows()["workflows"]]
        for wid in wids[:-1]:
            wf_mod.delete_workflow(wid)
        with self.assertRaises(ValueError):
            wf_mod.delete_workflow(wids[-1])


@unittest.skipUnless(os.name == "nt" and HAVE_FASTAPI,
                     "Windows-only: parent-death watchdog (needs desktop deps)")
class TestWatchParentWindows(unittest.TestCase):
    """watch_parent must exit the process when its parent dies (Windows).

    Without it, a shell crash orphans the engine on :8787 and app-quit
    orphans workers, which keep making paid calls. The fix holds a
    SYNCHRONIZE handle to the parent process object and waits on it.
    """

    SYNCHRONIZE = 0x00100000

    @classmethod
    def _kernel32(cls):
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                    ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        return k32

    def _alive(self, pid: int) -> bool:
        """Windows-safe liveness check (os.kill(pid, 0) TERMINATES on Windows)."""
        k32 = self._kernel32()
        handle = k32.OpenProcess(self.SYNCHRONIZE, 0, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) != 0  # 0 = WAIT_OBJECT_0
        finally:
            k32.CloseHandle(handle)

    def _terminate(self, pid: int) -> None:
        import subprocess

        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, check=False)

    def test_child_exits_when_parent_is_killed(self):
        import subprocess

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            child_src = tmp / "child.py"
            child_src.write_text(
                "import sys, time, os\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from desktop.engine_service.app import watch_parent\n"
                "watch_parent()\n"
                "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
                "time.sleep(120)\n",
                encoding="utf-8")
            parent_src = tmp / "parent.py"
            parent_src.write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2],\n"
                "                  sys.argv[3]])\n"
                "time.sleep(120)\n",
                encoding="utf-8")
            pid_file = tmp / "child.pid"
            parent = subprocess.Popen(
                [sys.executable, str(parent_src), str(child_src), str(root),
                 str(pid_file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_pid = None
            try:
                deadline = time.monotonic() + 30
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.2)
                self.assertTrue(pid_file.exists(), "child never started")
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                self.assertTrue(self._alive(child_pid))

                parent.kill()
                parent.wait(timeout=15)

                deadline = time.monotonic() + 15
                while self._alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.5)
                self.assertFalse(self._alive(child_pid),
                                 "child outlived its parent (orphan)")
            finally:
                if parent.poll() is None:
                    parent.kill()
                if child_pid is not None and self._alive(child_pid):
                    self._terminate(child_pid)


if __name__ == "__main__":
    unittest.main()
