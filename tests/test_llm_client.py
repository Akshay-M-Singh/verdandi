"""llm_client efficiency tests (D42) — offline.

Keep-alive connection reuse (one DNS+TLS per thread, reconnect on a dropped
connection) and the per-call elapsed_s timing that lands in usage.
"""

from __future__ import annotations

import base64
import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import config
import llm_client


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    connections = 0
    close_after = 0  # number of initial responses that ask the client to close
    replies = 0

    def setup(self):
        type(self).connections += 1
        super().setup()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        body = json.dumps({
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        type(self).replies += 1
        if type(self).replies <= type(self).close_after:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Server:
    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()


class TestKeepAlive(unittest.TestCase):
    def setUp(self):
        _Handler.connections = 0
        _Handler.close_after = 0
        _Handler.replies = 0
        self.server = _Server()
        self._orig = config.load_models
        config.load_models = lambda *a, **k: {
            "endpoint": self.server.endpoint, "api_style": "chat_completions",
            "image_url_form": "flat", "primary_model": "m",
        }

    def tearDown(self):
        config.load_models = self._orig
        llm_client._drop_connection()
        self.server.stop()

    def test_reuses_one_connection(self):
        for _ in range(3):
            self.assertEqual(llm_client.chat("m", "hi", api_key="k"), "OK")
        self.assertEqual(_Handler.connections, 1)

    def test_reconnects_when_server_closes(self):
        _Handler.close_after = 1  # first response closes the connection
        self.assertEqual(llm_client.chat("m", "hi", api_key="k"), "OK")
        self.assertEqual(llm_client.chat("m", "hi", api_key="k"), "OK")
        self.assertEqual(_Handler.connections, 2)

    def test_usage_carries_elapsed(self):
        usage: list[dict] = []
        llm_client.chat("m", "hi", api_key="k", usage_out=usage)
        self.assertIn("elapsed_s", usage[0])
        self.assertGreaterEqual(usage[0]["elapsed_s"], 0.0)

    def test_loopback_stays_direct_when_proxy_set(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://127.0.0.1:1"}):
            self.assertEqual(llm_client.chat("m", "hi", api_key="k"), "OK")
        self.assertEqual(_Handler.connections, 1)


class TestSumUsageElapsed(unittest.TestCase):
    def test_sums_tokens_and_elapsed(self):
        import extract
        totals = extract._sum_usage([
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
             "elapsed_s": 1.5},
            {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6,
             "elapsed_s": 0.5},
        ])
        self.assertEqual(totals["total_tokens"], 18)
        self.assertEqual(totals["elapsed_s"], 2.0)

    def test_no_usage_is_none(self):
        import extract
        self.assertIsNone(extract._sum_usage([]))


class _FakeConn:
    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tunnel = None
        self.calls: list[str] = []
        type(self).last = self

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel = (host, port, headers)

    def request(self, method, path, body=None, headers=None):
        self.calls.append(path)
        raise llm_client.LLMError("stop")

    def close(self):
        pass


class TestProxy(unittest.TestCase):
    def tearDown(self):
        llm_client._drop_connection()

    def test_https_uses_http_proxy_and_tunnels(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://proxy.example:8888"}), \
             mock.patch("urllib.request.proxy_bypass", return_value=False), \
             mock.patch.object(http.client, "HTTPSConnection", _FakeConn):
            conn = llm_client._connection("https", "opencode.ai", 5)
        self.assertEqual(conn.host, "proxy.example")
        self.assertEqual(conn.port, 8888)
        self.assertEqual(conn.tunnel[:2], ("opencode.ai", 443))
        self.assertIsNone(conn.tunnel[2])

    def test_tunnel_sends_basic_auth(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"https": "http://user:p%40ss@proxy.example:8080"}), \
             mock.patch("urllib.request.proxy_bypass", return_value=False), \
             mock.patch.object(http.client, "HTTPSConnection", _FakeConn):
            conn = llm_client._connection("https", "opencode.ai", 5)
        expected = "Basic " + base64.b64encode(b"user:p@ss").decode("ascii")
        self.assertEqual(conn.tunnel[2], {"Proxy-Authorization": expected})

    def test_proxy_bypass_and_loopback(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"https": "http://proxy.example:8080"}) as got, \
             mock.patch("urllib.request.proxy_bypass", return_value=True):
            self.assertIsNone(llm_client._proxy_for("https", "internal.corp"))
            got.assert_not_called()
        self.assertIsNone(llm_client._proxy_for("http", "127.0.0.1:9"))

    def test_socks_is_rejected(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"https": "socks5://proxy.example:1080"}), \
             mock.patch("urllib.request.proxy_bypass", return_value=False):
            with self.assertRaises(llm_client.LLMError) as ctx:
                llm_client._proxy_for("https", "opencode.ai")
        self.assertIn("SOCKS", str(ctx.exception))

    def test_http_proxy_uses_absolute_url(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://proxy.example:8080"}), \
             mock.patch("urllib.request.proxy_bypass", return_value=False), \
             mock.patch.object(http.client, "HTTPConnection", _FakeConn), \
             mock.patch.object(config, "load_models", return_value={
                 "endpoint": "http://example.com/v1",
                 "api_style": "chat_completions",
                 "image_url_form": "flat",
                 "primary_model": "m",
             }):
            with self.assertRaises(llm_client.LLMError):
                llm_client.chat("m", "hi", api_key="k", retries=0, timeout_s=1)
        self.assertEqual(_FakeConn.last.calls,
                         ["http://example.com/v1/chat/completions"])
        self.assertEqual(_FakeConn.last.host, "proxy.example")

    def test_dead_proxy_names_the_proxy(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"https": "http://127.0.0.1:1"}), \
             mock.patch("urllib.request.proxy_bypass", return_value=False):
            with self.assertRaises(llm_client.LLMError) as ctx:
                llm_client.chat("m", "hi", api_key="k", retries=0, timeout_s=2)
        msg = str(ctx.exception)
        self.assertIn("127.0.0.1:1", msg)
        self.assertIn("proxy", msg.lower())


if __name__ == "__main__":
    unittest.main()
