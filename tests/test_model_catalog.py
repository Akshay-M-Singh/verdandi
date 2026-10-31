"""Model catalog service tests (D35) — offline, no network, no paid calls.

Covers: endpoint -> models.dev provider mapping, the slim projection,
classification, catalog TTL/snapshot fallback + drift diff (mocked HTTP), and
the vision probe's transport discovery + error classification (mocked
llm_client).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import llm_client
import model_catalog


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestProviderMapping(unittest.TestCase):
    def test_go_and_zen_map_to_different_providers(self):
        self.assertEqual(
            model_catalog.provider_for_endpoint("https://opencode.ai/zen/go/v1"),
            "opencode-go")
        self.assertEqual(
            model_catalog.provider_for_endpoint("https://opencode.ai/zen/v1"),
            "opencode")
        self.assertEqual(model_catalog.provider_for_endpoint(""), "opencode")


class TestProjection(unittest.TestCase):
    RAW = {"opencode-go": {"models": {
        "vision-model": {"name": "Vision", "modalities": {"input": ["text", "image"]},
                         "cost": {"input": 1.5, "output": 3}, "limit": {"context": 1000000}},
        "text-model": {"name": "Text", "modalities": {"input": ["text"]}},
        "weird": "not-a-dict",
    }}}

    def test_projection_fields(self):
        out = model_catalog.project_models_dev(self.RAW, "opencode-go")
        self.assertEqual(set(out), {"vision-model", "text-model"})
        self.assertTrue(out["vision-model"]["vision"])
        self.assertFalse(out["text-model"]["vision"])
        self.assertEqual(out["vision-model"]["context"], 1000000)

    def test_describe(self):
        out = model_catalog.project_models_dev(self.RAW, "opencode-go")
        self.assertEqual(model_catalog.describe("vision-model", out), "$1.5/$3 · 1M ctx")
        self.assertEqual(model_catalog.describe("text-model", out), "")
        self.assertEqual(model_catalog.describe("nope", out), "")


class TestClassify(unittest.TestCase):
    def test_statuses(self):
        cat = ["v", "c", "t", "u"]
        dev = {"c": {"vision": True}, "t": {"vision": False}}
        verified = ["v"]
        self.assertEqual(model_catalog.classify("v", cat, dev, verified), "verified")
        self.assertEqual(model_catalog.classify("c", cat, dev, verified), "candidate")
        self.assertEqual(model_catalog.classify("t", cat, dev, verified), "text_only")
        self.assertEqual(model_catalog.classify("u", cat, dev, verified), "unknown")
        self.assertEqual(model_catalog.classify("gone", cat, dev, verified), "missing")

    def test_empty_catalog_is_not_missing(self):
        # A failed fetch with no snapshot must not make everything look removed.
        self.assertEqual(model_catalog.classify("v", [], {}, ["v"]), "verified")
        self.assertEqual(model_catalog.classify("x", [], {}, []), "unknown")


class TestGoCatalogCache(unittest.TestCase):
    ENDPOINT = "https://opencode.ai/zen/go/v1"

    def _snapshot(self, cache_dir: Path, ids: list[str], age_s: float = 0.0):
        (cache_dir / "go_catalog.json").write_text(json.dumps({
            "fetched_at": time.time() - age_s, "endpoint": self.ENDPOINT, "ids": ids,
        }), encoding="utf-8")

    def test_fresh_snapshot_served_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            self._snapshot(cdir, ["a", "b"])
            with mock.patch("urllib.request.urlopen") as urlopen:
                got = model_catalog.fetch_go_catalog(self.ENDPOINT, cache_dir=cdir)
            urlopen.assert_not_called()
            self.assertTrue(got["from_cache"])
            self.assertEqual(got["ids"], ["a", "b"])

    def test_refresh_reports_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            self._snapshot(cdir, ["a", "b"], age_s=10_000)
            fake = _FakeResponse({"object": "list", "data": [{"id": "b"}, {"id": "c"}]})
            with mock.patch("urllib.request.urlopen", return_value=fake):
                got = model_catalog.fetch_go_catalog(self.ENDPOINT, cache_dir=cdir)
            self.assertFalse(got["from_cache"])
            self.assertEqual(got["ids"], ["b", "c"])
            self.assertEqual(got["new"], ["c"])
            self.assertEqual(got["removed"], ["a"])

    def test_failure_falls_back_to_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            self._snapshot(cdir, ["a"], age_s=10_000)
            with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
                got = model_catalog.fetch_go_catalog(self.ENDPOINT, cache_dir=cdir)
            self.assertTrue(got["from_cache"])
            self.assertTrue(got["stale"])
            self.assertEqual(got["ids"], ["a"])
            self.assertIn("offline", got["error"])

    def test_cached_catalog_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            self._snapshot(cdir, ["a"])
            self.assertEqual(
                model_catalog.cached_catalog_ids(self.ENDPOINT, cache_dir=cdir), ["a"])
            self.assertIsNone(model_catalog.cached_catalog_ids(
                self.ENDPOINT, max_age_s=0, cache_dir=cdir))

    def test_first_fetch_is_a_baseline_not_all_new(self):
        # 2026-10-05: no prior snapshot (e.g. a fresh packaged app) must not
        # report every model as new — it's the baseline.
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            fake = _FakeResponse({"data": [{"id": "a"}, {"id": "b"}]})
            with mock.patch("urllib.request.urlopen", return_value=fake):
                got = model_catalog.fetch_go_catalog(self.ENDPOINT, cache_dir=cdir)
            self.assertEqual(got["new"], [])
            self.assertEqual(got["removed"], [])
            # A SECOND live fetch now has a baseline to diff against.
            fake2 = _FakeResponse({"data": [{"id": "b"}, {"id": "c"}]})
            with mock.patch("urllib.request.urlopen", return_value=fake2):
                got2 = model_catalog.fetch_go_catalog(self.ENDPOINT, force=True,
                                                      cache_dir=cdir)
            self.assertEqual(got2["new"], ["c"])
            self.assertEqual(got2["removed"], ["a"])

    def test_auth_header_sent_and_401_falls_back_public(self):
        import urllib.error

        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            seen: list[dict] = []

            def fake_urlopen(req, timeout=None):
                seen.append(dict(req.header_items()))
                if req.get_header("Authorization"):
                    raise urllib.error.HTTPError(req.full_url, 401, "nope", {}, None)
                return _FakeResponse({"data": [{"id": "a"}]})

            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                got = model_catalog.fetch_go_catalog(
                    self.ENDPOINT, cache_dir=cdir, api_key="sk-test")
            self.assertEqual(got["ids"], ["a"])
            self.assertEqual(len(seen), 2)
            self.assertTrue(seen[0].get("Authorization", "").startswith("Bearer sk-"))
            self.assertNotIn("Authorization", seen[1])


class TestCacheDirResolver(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(
                model_catalog.os.environ,
                {model_catalog.ENV_CACHE_DIR: "~/catalog-cache"}):
            self.assertEqual(model_catalog.cache_dir(),
                             Path.home() / "catalog-cache")

    def test_frozen_uses_user_workspace(self):
        import sys

        with mock.patch.dict(model_catalog.os.environ, {}, clear=False):
            model_catalog.os.environ.pop(model_catalog.ENV_CACHE_DIR, None)
            with mock.patch.object(sys, "frozen", True, create=True):
                self.assertEqual(model_catalog.cache_dir(),
                                 Path.home() / ".verdandi" / "cache")

    def test_dev_uses_out_cache(self):
        import sys

        with mock.patch.dict(model_catalog.os.environ, {}, clear=False):
            model_catalog.os.environ.pop(model_catalog.ENV_CACHE_DIR, None)
            if hasattr(sys, "frozen"):
                del sys.frozen
            self.assertEqual(model_catalog.cache_dir(), model_catalog.CACHE_DIR)


class TestModelsDevCache(unittest.TestCase):
    def test_failure_falls_back_to_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            (cdir / "models_dev.json").write_text(json.dumps({
                "fetched_at": time.time() - 10_000, "provider": "opencode-go",
                "models": {"m": {"vision": True}},
            }), encoding="utf-8")
            with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
                got = model_catalog.fetch_models_dev(endpoint="https://x/zen/go/v1",
                                                     cache_dir=cdir)
            self.assertTrue(got["from_cache"])
            self.assertEqual(got["models"]["m"]["vision"], True)


class TestProbe(unittest.TestCase):
    def _probe(self, side_effect, cache_dir: Path):
        with mock.patch.object(llm_client, "chat_vision_pages", side_effect=side_effect):
            return model_catalog.probe_vision(
                "some-model", image_path=None, cache_dir=cache_dir, timeout_s=1)

    def test_success_on_first_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            calls = []

            def ok(model, prompt, images, **kwargs):
                calls.append(kwargs.get("transport"))
                return "OK"

            res = self._probe(ok, cdir)
            self.assertTrue(res["vision"])
            self.assertEqual(res["transport"],
                             {"api_style": "chat_completions", "image_url_form": "flat"})
            self.assertEqual(calls, [("chat_completions", "flat")])
            # recorded for promote_model
            self.assertEqual(
                model_catalog.probe_transport("some-model", cache_dir=cdir),
                {"api_style": "chat_completions", "image_url_form": "flat"})

    def test_probe_grants_reasoning_models_token_headroom(self):
        """Reasoning models spend the completion budget on `reasoning` and
        return content: null under a tiny cap (step-5-preview-free,
        2026-10-09) — the probe must give them room to answer."""
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}

            def ok(model, prompt, images, **kwargs):
                seen["max_tokens"] = kwargs.get("max_tokens")
                return "OK"

            res = self._probe(ok, Path(tmp))
            self.assertTrue(res["vision"])
            self.assertGreaterEqual(seen["max_tokens"], 512)

    def test_fallback_to_nested_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def side(model, prompt, images, **kwargs):
                calls.append(kwargs.get("transport"))
                if kwargs.get("transport") == ("chat_completions", "nested"):
                    return "OK"
                raise llm_client.LLMError("LLM gateway error (HTTP 400): bad image")

            res = self._probe(side, Path(tmp))
            self.assertTrue(res["vision"])
            self.assertEqual(res["transport"],
                             {"api_style": "chat_completions", "image_url_form": "nested"})
            self.assertEqual(len(calls), 2)

    def test_all_rejected_is_not_vision(self):
        with tempfile.TemporaryDirectory() as tmp:
            def side(model, prompt, images, **kwargs):
                raise llm_client.LLMError("LLM gateway error (HTTP 400): no image input")

            res = self._probe(side, Path(tmp))
            self.assertFalse(res["vision"])
            self.assertEqual(res["error_class"], "rejected")
            self.assertIsNone(res["transport"])

    def test_transient_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            def side(model, prompt, images, **kwargs):
                raise llm_client.LLMError("LLM gateway error (HTTP 503): overloaded")

            res = self._probe(side, Path(tmp))
            self.assertIsNone(res["vision"])
            self.assertEqual(res["error_class"], "transient")

    def test_probe_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}

            def ok(model, prompt, images, **kwargs):
                seen["retries"] = kwargs.get("retries")
                return "OK"

            self._probe(ok, Path(tmp))
            self.assertEqual(seen["retries"], 1)

    def test_quota_aborts_and_names_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def side(model, prompt, images, **kwargs):
                calls.append(1)
                raise llm_client.LLMError(
                    "HTTP 429: insufficient_quota — check your plan")

            res = self._probe(side, Path(tmp))
            self.assertIsNone(res["vision"])
            self.assertEqual(res["error_class"], "quota")
            self.assertEqual(len(calls), 1)
            msg = model_catalog.probe_outcome_message("some-model", res)
            self.assertIn("no remaining GO quota", msg)
            self.assertIn("opencode.ai", msg)

    def test_proxy_407_is_transient_with_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            def side(model, prompt, images, **kwargs):
                raise llm_client.LLMError(
                    "proxy 10.0.0.1:8080 requires authentication (HTTP 407)")

            res = self._probe(side, Path(tmp))
            self.assertIsNone(res["vision"])
            self.assertEqual(res["error_class"], "transient")
            msg = model_catalog.probe_outcome_message("some-model", res)
            self.assertIn("inconclusive", msg)
            self.assertIn("10.0.0.1:8080", msg)
            self.assertIn("407", msg)

    def test_failed_attempts_are_persisted_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)

            def side(model, prompt, images, **kwargs):
                raise llm_client.LLMError(
                    "could not reach opencode.ai ([Errno 61] Connection refused) "
                    "sk-abcdef123456 — a proxy or firewall may be blocking it")

            res = self._probe(side, cdir)
            self.assertEqual(res["error_class"], "transient")
            saved = json.loads((cdir / "model_probes.json").read_text(encoding="utf-8"))
            err = saved["some-model"]["attempts"][-1]["error"]
            self.assertIn("Connection refused", err)
            self.assertNotIn("sk-abcdef123456", err)
            self.assertIn("sk-***", err)

    def test_auth_error_aborts_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def side(model, prompt, images, **kwargs):
                calls.append(1)
                raise llm_client.LLMError("LLM gateway rejected request (HTTP 401): bad key")

            res = self._probe(side, Path(tmp))
            self.assertIsNone(res["vision"])
            self.assertEqual(res["error_class"], "auth")
            self.assertEqual(len(calls), 1)


class TestModelLabelNotes(unittest.TestCase):
    """D43: measured model_notes are appended to the picker label."""

    def _snap(self, notes=None):
        return {"catalog": {"ids": ["gpt-5.6-luna"]},
                "verified": ["gpt-5.6-luna"], "dev": {"models": {}},
                **({"notes": notes} if notes is not None else {})}

    def test_note_appended(self):
        from ui import lib as L
        label = L.model_label("gpt-5.6-luna", self._snap({"gpt-5.6-luna": "fastest (bench)"}))
        self.assertIn("fastest (bench)", label)

    def test_no_notes_key_is_safe(self):
        from ui import lib as L
        label = L.model_label("gpt-5.6-luna", self._snap())
        self.assertEqual(label, "gpt-5.6-luna  ·  verified")

    def test_unnoted_model_has_no_suffix(self):
        from ui import lib as L
        label = L.model_label("gpt-5.6-luna", self._snap({"other": "x"}))
        self.assertNotIn("x", label)


class TestVerifyWriter(unittest.TestCase):
    """verify_model persists the probed transport durably (D35 follow-up)."""

    def _with_temp_models(self):
        import shutil
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        p = Path(tmp.name) / "models.yaml"
        shutil.copy(config.MODELS_PATH, p)
        return tmp, p

    def test_success_writes_list_and_override(self):
        import config
        import ui.lib as L
        tmp, path = self._with_temp_models()
        orig = config.MODELS_PATH
        config.MODELS_PATH = path
        try:
            with mock.patch("model_catalog.probe_vision", return_value={
                    "vision": True,
                    "transport": {"api_style": "responses", "image_url_form": "flat"},
                    "tokens": 123, "error_class": None, "attempts": []}):
                msg = L.verify_model("new-model-x")
            m = config.load_models()
            self.assertIn("new-model-x", m["vision_verified"])
            self.assertEqual(m["model_overrides"]["new-model-x"]["api_style"], "responses")
            self.assertIn("vision OK", msg)
        finally:
            config.MODELS_PATH = orig
            tmp.cleanup()

    def test_rejected_and_transient_do_not_write(self):
        import config
        import ui.lib as L
        tmp, path = self._with_temp_models()
        orig = config.MODELS_PATH
        config.MODELS_PATH = path
        try:
            with mock.patch("model_catalog.probe_vision", return_value={
                    "vision": False, "transport": None, "tokens": 0,
                    "error_class": "rejected", "attempts": []}):
                msg = L.verify_model("bad-model")
            self.assertIn("not vision-capable", msg)
            self.assertNotIn("bad-model", config.load_models()["vision_verified"])
            with mock.patch("model_catalog.probe_vision", return_value={
                    "vision": None, "transport": None, "tokens": 0,
                    "error_class": "transient", "attempts": []}):
                msg = L.verify_model("flaky-model")
            self.assertIn("inconclusive", msg)
            with mock.patch("model_catalog.probe_vision", return_value={
                    "vision": None, "transport": None, "tokens": 0,
                    "error_class": "transient",
                    "attempts": [{"status": None, "error":
                                  "could not reach opencode.ai via proxy 10.1.2.3:8080"}]}):
                msg = L.verify_model("flaky-model")
            self.assertIn("10.1.2.3:8080", msg)
            self.assertIn("inconclusive", msg)
            self.assertNotIn("flaky-model", config.load_models()["vision_verified"])
        finally:
            config.MODELS_PATH = orig
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
