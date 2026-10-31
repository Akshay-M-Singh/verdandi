"""Capture-engine seam tests (D63) — offline, no PDFs, no LLM.

Detection/selection logic, provenance shape, and the text_layer dispatcher
delegation. Engine behavior itself is covered by the harness fixture suite.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engines
import text_layer


class TestDetection(unittest.TestCase):
    def test_apple_when_swift_available(self):
        with mock.patch.object(engines, "swift_available", return_value=True):
            self.assertEqual(engines.detect_engine(), "apple_vision")

    def test_crossplatform_when_swift_missing(self):
        with mock.patch.object(engines, "swift_available", return_value=False):
            self.assertEqual(engines.detect_engine(), "crossplatform")

    def test_resolve_precedence_explicit_over_env(self):
        with mock.patch.dict(os.environ, {engines.ENV_VAR: "crossplatform"}):
            self.assertEqual(engines.resolve_engine("apple_vision"), "apple_vision")
            self.assertEqual(engines.resolve_engine(None), "crossplatform")
            self.assertEqual(engines.resolve_engine("auto"),
                             engines.detect_engine())

    def test_env_var_overrides_auto_only(self):
        with mock.patch.dict(os.environ, {engines.ENV_VAR: "crossplatform"}):
            with mock.patch.object(engines, "swift_available", return_value=True):
                self.assertEqual(engines.resolve_engine(None), "crossplatform")

    def test_unknown_engine_raises(self):
        with self.assertRaises(engines.EngineError):
            engines.resolve_engine("paddle-direct")


class TestEngineObjects(unittest.TestCase):
    def test_get_engine_returns_named_engines(self):
        apple = engines.get_engine("apple_vision")
        self.assertEqual(apple.name, "apple_vision")
        self.assertIsInstance(apple.describe(), dict)
        self.assertEqual(apple.describe()["name"], "apple_vision")
        cross = engines.get_engine("crossplatform")
        self.assertEqual(cross.name, "crossplatform")

    def test_crossplatform_missing_deps_raise_engine_error(self):
        engine = engines.get_engine("crossplatform")
        with mock.patch.object(engine, "available", return_value=False):
            with self.assertRaises(engines.EngineError):
                engine.process(Path("x.pdf"), Path("out"))

    def test_crossplatform_missing_pdf_raises_clearly(self):
        engine = engines.get_engine("crossplatform")
        if not engine.available():
            self.skipTest("cross-platform deps not installed")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                engine.process(Path(tmp) / "x.pdf", Path(tmp) / "out")
            self.assertIn("cannot open PDF", str(ctx.exception))

    @unittest.skipUnless(engines.swift_available(), "macOS + swiftc required")
    def test_apple_available_on_this_host(self):
        self.assertTrue(engines.get_engine("apple_vision").available())


class TestDispatcher(unittest.TestCase):
    def test_ocr_pdf_delegates_to_selected_engine(self):
        calls = {}

        class FakeEngine:
            name = "fake"

            def describe(self):
                return {"name": "fake"}

            def process(self, pdf_path, out_dir, **kwargs):
                calls.update(kwargs)
                calls["pdf"] = pdf_path
                return {"status": "ok", "engine": self.describe()}

        with mock.patch.object(engines, "get_engine", return_value=FakeEngine()):
            report = text_layer.ocr_pdf(Path("in.pdf"), Path("out"),
                                        engine="apple_vision", vision_cap=1536,
                                        ocr_only=True)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(calls["vision_cap"], 1536)
        self.assertTrue(calls["ocr_only"])
        self.assertEqual(calls["pdf"], Path("in.pdf"))

    def test_run_batch_resolves_engine_once(self):
        with mock.patch.object(engines, "resolve_engine",
                               return_value="apple_vision") as res:
            with mock.patch.object(text_layer, "ocr_pdf",
                                   return_value={"pages_report": []}) as op:
                rc = text_layer.run_batch(Path("nope"), Path("nope-out"),
                                          engine="auto")
        self.assertEqual(rc, 0)  # no PDFs in the dir
        res.assert_called_once_with("auto")
        op.assert_not_called()


class TestSwiftAvailability(unittest.TestCase):
    """The Apple engine needs the harness source AND a working compiler (D66).

    `_swiftc_available` is the compiler probe (tested below); here we mock it
    so the source/platform logic is exercised independently.
    """

    def _probe(self, source):
        with mock.patch("platform.system", return_value="Darwin"), \
             mock.patch.object(engines, "_swiftc_available", return_value=True), \
             mock.patch.object(engines, "SWIFT_SOURCE", source):
            return engines.swift_available()

    def test_missing_source_falls_back(self):
        with mock.patch("platform.system", return_value="Darwin"), \
             mock.patch.object(engines, "_swiftc_available", return_value=True), \
             mock.patch.object(engines, "SWIFT_SOURCE",
                               Path("/nope/ocr.swift")):
            self.assertFalse(engines.swift_available())
            self.assertEqual(engines.detect_engine(), "crossplatform")

    def test_present_source_is_available(self):
        self.assertTrue(self._probe(Path(__file__)))

    def test_no_working_compiler_falls_back(self):
        with mock.patch("platform.system", return_value="Darwin"), \
             mock.patch.object(engines, "_swiftc_available", return_value=False), \
             mock.patch.object(engines, "SWIFT_SOURCE", Path(__file__)):
            self.assertFalse(engines.swift_available())
            self.assertEqual(engines.detect_engine(), "crossplatform")

    def test_missing_source_without_swiftc(self):
        with mock.patch("platform.system", return_value="Darwin"), \
             mock.patch.object(engines, "_swiftc_available", return_value=False):
            self.assertFalse(engines.swift_available())


class TestSwiftcProbe(unittest.TestCase):
    """The /usr/bin/swiftc shim exists without Xcode CLT and pops the system
    'install developer tools' dialog when invoked — the probe must resolve
    swiftc through xcrun (a real binary that fails cleanly) instead."""

    def setUp(self):
        engines._swiftc_available.cache_clear()

    def tearDown(self):
        engines._swiftc_available.cache_clear()

    def _run(self, which_xcrun, run_result=None, run_exc=None):
        with mock.patch("shutil.which",
                        side_effect=lambda name: which_xcrun
                        if name == "xcrun" else None):
            if run_exc is not None:
                with mock.patch("subprocess.run", side_effect=run_exc):
                    return engines._swiftc_available()
            if run_result is not None:
                with mock.patch("subprocess.run", return_value=run_result):
                    return engines._swiftc_available()
            return engines._swiftc_available()

    def test_xcrun_finds_swiftc(self):
        result = mock.Mock(returncode=0,
                           stdout="/Library/Developer/CommandLineTools/usr/bin/swiftc\n")
        self.assertTrue(self._run("/usr/bin/xcrun", run_result=result))

    def test_xcrun_failure_falls_back_cleanly(self):
        # No CLT: xcrun exits non-zero with an error — never invoke the shim.
        result = mock.Mock(returncode=1, stdout="")
        self.assertFalse(self._run("/usr/bin/xcrun", run_result=result))

    def test_xcrun_error_falls_back_cleanly(self):
        self.assertFalse(self._run("/usr/bin/xcrun", run_exc=OSError("boom")))

    def test_no_xcrun_uses_which(self):
        engines._swiftc_available.cache_clear()
        with mock.patch("shutil.which",
                        side_effect=lambda name: "/usr/bin/swiftc"
                        if name == "swiftc" else None):
            self.assertTrue(engines._swiftc_available())

    def test_probe_is_cached(self):
        result = mock.Mock(returncode=0, stdout="/x/swiftc\n")
        with mock.patch("shutil.which", return_value="/usr/bin/xcrun"), \
             mock.patch("subprocess.run", return_value=result) as run:
            self.assertTrue(engines._swiftc_available())
            self.assertTrue(engines._swiftc_available())
        run.assert_called_once()


class TestEnsureBinaryCache(unittest.TestCase):
    """The compiled harness is cached by SOURCE HASH, not mtime: PyInstaller
    onefile extraction stamps fresh mtimes on every launch, which made
    packaged apps recompile once per start (2026-10-11)."""

    def setUp(self):
        from engines import apple_vision

        self.mod = apple_vision
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "ocr.swift"
        self.source.write_text("// harness v1\n", encoding="utf-8")
        self.binary = self.dir / "ocr"
        self.stamp = self.dir / "ocr.sha256"

    def _ensure(self):
        with mock.patch.object(self.mod, "BINARY", self.binary), \
             mock.patch.object(engines, "SWIFT_SOURCE", self.source):
            return self.mod.ensure_binary()

    def _fake_compile(self):
        def run(cmd, **kwargs):
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(b"fake-binary")
            return mock.Mock(returncode=0, stderr="")
        return run

    def test_matching_hash_reuses_binary_without_compiling(self):
        self.binary.write_bytes(b"cached")
        self.stamp.write_text(
            self.mod._source_digest(self.source) + "\n", encoding="utf-8")
        with mock.patch("subprocess.run") as run:
            self.assertEqual(self._ensure(), self.binary)
        run.assert_not_called()
        self.assertEqual(self.binary.read_bytes(), b"cached")

    def test_changed_source_recompiles(self):
        self.binary.write_bytes(b"stale")
        self.stamp.write_text("0" * 64 + "\n", encoding="utf-8")
        with mock.patch("subprocess.run", side_effect=self._fake_compile()):
            self.assertEqual(self._ensure(), self.binary)
        self.assertEqual(self.binary.read_bytes(), b"fake-binary")
        self.assertEqual(self.stamp.read_text(encoding="utf-8").strip(),
                         self.mod._source_digest(self.source))

    def test_missing_stamp_recompiles(self):
        self.binary.write_bytes(b"legacy-mtime-cache")
        with mock.patch("subprocess.run", side_effect=self._fake_compile()):
            self._ensure()
        self.assertEqual(self.binary.read_bytes(), b"fake-binary")

    def test_compile_failure_stays_loud_and_leaves_no_tmp(self):
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=1,
                                               stderr="error: broken")):
            with self.assertRaises(RuntimeError) as ctx:
                self._ensure()
        self.assertIn("swiftc failed", str(ctx.exception))
        self.assertFalse((self.dir / "ocr.tmp").exists())
        self.assertFalse(self.binary.exists())

    def test_ensure_binary_reports_missing_source(self):
        from engines import apple_vision

        with mock.patch.object(engines, "SWIFT_SOURCE",
                               Path("/nope/ocr.swift")):
            with self.assertRaises(RuntimeError) as ctx:
                apple_vision.ensure_binary()
        self.assertIn("source missing", str(ctx.exception))


class TestSwiftBinaryPath(unittest.TestCase):
    """Compiled harness cache: env > user cache (frozen) > dev .build."""

    def setUp(self):
        from engines import apple_vision

        self.mod = apple_vision
        self._frozen = getattr(sys, "frozen", None)
        if self._frozen is not None:
            del sys.frozen

    def tearDown(self):
        if self._frozen is not None:
            sys.frozen = self._frozen

    def test_env_override(self):
        with mock.patch.dict(
                os.environ, {self.mod.ENV_BIN_CACHE: "~/swiftbin"}):
            self.assertEqual(
                self.mod._binary_path(), Path.home() / "swiftbin" / "ocr")

    def test_frozen_uses_user_cache(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.mod.ENV_BIN_CACHE, None)
            sys.frozen = True
            self.assertEqual(
                self.mod._binary_path(),
                Path.home() / ".verdandi" / "cache" / "swift" / "ocr")

    def test_dev_uses_build_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.mod.ENV_BIN_CACHE, None)
            self.assertEqual(
                self.mod._binary_path(),
                self.mod.SWIFT_DIR / ".build" / "ocr")


class TestModelCache(unittest.TestCase):
    """Packaged builds must not cache RapidOCR models inside the bundle."""

    def setUp(self):
        from engines import crossplatform

        self.mod = crossplatform
        self._frozen = getattr(sys, "frozen", None)
        if self._frozen is not None:
            del sys.frozen

    def tearDown(self):
        if self._frozen is not None:
            sys.frozen = self._frozen

    def test_dev_checkout_uses_out_cache(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.mod.ENV_MODEL_CACHE, None)
            self.assertEqual(
                self.mod.model_cache(),
                self.mod.REPO_ROOT / "out" / "cache" / "rapidocr")

    def test_frozen_build_uses_user_workspace(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.mod.ENV_MODEL_CACHE, None)
            sys.frozen = True
            self.assertEqual(
                self.mod.model_cache(),
                Path.home() / ".verdandi" / "cache" / "rapidocr")

    def test_env_override_wins(self):
        with mock.patch.dict(
                os.environ, {self.mod.ENV_MODEL_CACHE: "~/custom/models"}):
            sys.frozen = True
            self.assertEqual(
                self.mod.model_cache(),
                Path.home() / "custom" / "models")


if __name__ == "__main__":
    unittest.main()
