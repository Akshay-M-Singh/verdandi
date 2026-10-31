"""Minimal OpenAI-compatible chat client for the opencode Zen gateway (architecture.md D4).

Provider contract (owner decision, 2026-08-02): every LLM call in the pipeline
goes through the Zen gateway — `POST {endpoint}/chat/completions` with the
opencode.ai/auth key. Model IDs come from config/models.yaml (never hardcoded);
the endpoint too (hot-loaded, D10). This module is the ONLY place that talks
HTTP to an LLM — extract.py (Stage 2, schema-constrained extraction) and the
D16 quality probe (text_layer.py) both build on it.

Dependencies: stdlib only (`http.client` + `json` + `urllib`) —
google-genai was dropped in the provider switch (D14 note). One keep-alive
HTTP(S) connection per process/thread (D42) avoids a DNS+TLS handshake on
every call; a failed send drops the connection and the retry reconnects.
Corporate networks that require an HTTP proxy are honored (env vars and, on
Windows, the WinINET registry) via CONNECT tunneling — a raw socket to the
gateway is dropped by those firewalls and the probe looks "transient".
JSON output is enforced with `response_format: json_object` (the gateway is
OpenAI-compatible; Gemini's native `responseSchema` is not available — schema
conformance is prompt + validate + retry, Stage 2).

Retry policy (Stage 2 contract: max 2 retries): transport errors, timeouts,
429/500/503 retried with backoff; auth errors (401/403) and other 4xx fail
fast. A failed call raises LLMError — callers decide to degrade (the optional
quality probe) or fail the file (extraction, per-file isolation).
"""

from __future__ import annotations

import base64
import http.client
import inspect
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

import config

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")


def _redact(text: str) -> str:
    """Never let a token shape reach errors or logs (headers are never logged)."""
    return _TOKEN_RE.sub("sk-***", text)

DEFAULT_TIMEOUT_S = 120
DEFAULT_RETRIES = 2
BACKOFF_BASE_S = 1.0

RETRY_STATUS = {429, 500, 502, 503, 504}

# The opencode Zen gateway requires session-affinity headers on every request
# (2026-09-06 change: requests missing `x-opencode-session` error HTTP 400
# `MissingSessionID` — "cannot be routed efficiently"). A STABLE opaque value
# per process keeps prompt-cache affinity within a run while avoiding the
# "dead sticky replica" trap of reusing one id forever (see AGENTS.md D4
# watch-outs). Override OPENCODE_SESSION for cross-run affinity if desired.
_SESSION_ID = os.environ.get("OPENCODE_SESSION") or f"ses_{uuid.uuid4().hex}"


class LLMError(RuntimeError):
    """An LLM call failed in a way the caller must handle (no silent retry)."""


def _model_transport(model: str) -> tuple[str, str]:
    """(api_style, image_url_form) for a model — per-model overrides win
    (config/models.yaml `model_overrides`, 2026-09-02 — e.g. a chat-completions
    model needing the nested image form while the primary uses the Responses
    API). Hot-loaded per call (D10)."""
    cfg = config.load_models()
    over = (cfg.get("model_overrides") or {}).get(model, {}) or {}
    style = over.get("api_style", cfg.get("api_style", "chat_completions"))
    form = over.get("image_url_form", cfg.get("image_url_form", "flat"))
    return style, form


def _endpoint(style: str) -> str:
    """Gateway URL for an api_style — per-MODEL style, not the top-level one
    (a model_override may differ from the primary's transport)."""
    cfg = config.load_models()
    route = "responses" if style == "responses" else "chat/completions"
    return f"{cfg['endpoint']}/{route}"


def _responses_body(model: str, messages: list[dict], *, json_mode: bool,
                    temperature: float, max_tokens: int | None) -> dict:
    """Convert chat-style messages to the OpenAI Responses API payload.

    gpt-5.6-luna on the GO gateway routes through /responses (chat/completions
    is provider-broken for it — 500 since 2026-08-24; verified live against
    /responses 2026-08-29). Shape ground truth (probed live):
      - system message -> top-level `instructions` (not an input role)
      - user content parts: text -> {"type": "input_text"}, images ->
        {"type": "input_image", "image_url": "data:..."} (Responses-native
        types — chat-style types are rejected upstream)
      - json mode -> top-level "text": {"format": {"type": "json_object"}}
      - max_tokens -> max_output_tokens (provider floor 16)
      - usage keys: input_tokens / output_tokens / total_tokens
    """
    body: dict[str, Any] = {"model": model}
    system = [m for m in messages if m.get("role") == "system"]
    if system:
        body["instructions"] = system[0].get("content", "")
    converted = []
    for msg in messages:
        if msg.get("role") == "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts = [{"type": "input_text", "text": content}]
        else:
            parts = []
            for part in content or []:
                if part.get("type") == "text":
                    parts.append({"type": "input_text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    url = part.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    parts.append({"type": "input_image", "image_url": url})
        converted.append({"role": msg.get("role", "user"), "content": parts})
    body["input"] = converted
    # The Responses API for gpt-5.6-luna rejects `temperature` upstream
    # (verified live 2026-08-29) — we always sample at temperature 0.0 anyway.
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    if max_tokens is not None:
        body["max_output_tokens"] = max(max_tokens, 16)
    return body


def _parse_responses(data: dict) -> tuple[str, dict | None]:
    """Extract (content, usage) from a /responses answer.

    Usage keys normalize to the OpenAI-compatible trio
    (input->prompt, output->completion) so callers are API-agnostic.
    """
    content_parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                content_parts.append(part["text"])
    content = "".join(content_parts)
    usage = data.get("usage")
    if isinstance(usage, dict):
        usage = {
            "prompt_tokens": usage.get("input_tokens", 0) or 0,
            "completion_tokens": usage.get("output_tokens", 0) or 0,
            "total_tokens": usage.get("total_tokens", 0) or 0,
        }
    return content, usage


_CONN = threading.local()
_LOGGED_ROUTES: set[tuple] = set()
_LOGGED_ROUTES_LOCK = threading.Lock()
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_SET_TUNNEL_HEADERS = "headers" in inspect.signature(
    http.client.HTTPConnection.set_tunnel).parameters


def _split_host_port(netloc: str, default: int) -> tuple[str, int]:
    if netloc.startswith("["):
        host, _, rest = netloc[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default
    host, sep, port_s = netloc.rpartition(":")
    if sep and port_s.isdigit():
        return host, int(port_s)
    return netloc, default


def _parse_proxy_url(url: str) -> tuple[str, int, str | None]:
    """(host, port, Proxy-Authorization value or None). Raises LLMError on SOCKS."""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if (parsed.scheme or "").startswith("socks"):
        raise LLMError(
            f"SOCKS proxy is configured ({parsed.scheme}://{parsed.hostname}) — "
            "only HTTP proxies are supported; set HTTPS_PROXY to an http:// proxy"
        )
    if not parsed.hostname:
        raise LLMError("proxy URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 8080)
    auth = None
    if parsed.username is not None:
        user = urllib.parse.unquote(parsed.username)
        password = urllib.parse.unquote(parsed.password or "")
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        auth = f"Basic {token}"
    return parsed.hostname, port, auth


def _proxy_for(scheme: str, netloc: str) -> tuple[str, int, str | None] | None:
    """(proxy_host, proxy_port, basic_auth) or None for a direct connection.

    `urllib.request.getproxies()` reads HTTPS_PROXY/HTTP_PROXY and, on
    Windows, the WinINET registry (ProxyEnable + ProxyServer). PAC
    auto-config URLs are not evaluated — Python cannot run them. Loopback
    targets always go direct so local tests are unaffected.
    """
    host, _ = _split_host_port(netloc, 443 if scheme == "https" else 80)
    if host.lower() in _LOOPBACK:
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except Exception:
        logger.debug("proxy_bypass failed for %s", host, exc_info=True)
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        logger.warning("proxy detection failed; connecting directly", exc_info=True)
        return None
    if scheme == "https":
        url = proxies.get("https") or proxies.get("http")
    else:
        url = proxies.get("http")
    if not url:
        return None
    return _parse_proxy_url(url)


def _log_route(scheme: str, netloc: str, proxy: tuple | None) -> None:
    key = (scheme, netloc, None if proxy is None else (proxy[0], proxy[1]))
    with _LOGGED_ROUTES_LOCK:
        if key in _LOGGED_ROUTES:
            return
        _LOGGED_ROUTES.add(key)
    if proxy:
        logger.info("llm transport: proxy http://%s:%s -> %s://%s",
                    proxy[0], proxy[1], scheme, netloc)
    else:
        logger.info("llm transport: direct %s://%s", scheme, netloc)


def _open_connection(scheme: str, netloc: str, timeout_s: float, proxy: tuple | None):
    https = scheme == "https"
    cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    if not proxy:
        return cls(netloc, timeout=timeout_s)
    phost, pport, pauth = proxy
    conn = cls(phost, pport, timeout=timeout_s)
    if https:
        host, port = _split_host_port(netloc, 443)
        if pauth and _SET_TUNNEL_HEADERS:
            conn.set_tunnel(host, port, {"Proxy-Authorization": pauth})
        else:
            conn.set_tunnel(host, port)
    return conn


def _connection(scheme: str, netloc: str, timeout_s: float):
    """Thread-local keep-alive HTTP(S) connection (D42).

    One DNS + TLS handshake per process/thread instead of per call. The
    connection is dropped and re-created when the host or proxy changes or a
    send fails (the gateway may close idle connections); callers retry.
    An HTTP proxy (env or Windows registry) is tunneled with CONNECT.
    """
    proxy = _proxy_for(scheme, netloc)
    conn = getattr(_CONN, "conn", None)
    same = (conn is not None and getattr(_CONN, "netloc", None) == netloc
            and getattr(_CONN, "scheme", None) == scheme
            and getattr(_CONN, "proxy", None) == proxy)
    if not same:
        _drop_connection()
        conn = _open_connection(scheme, netloc, timeout_s, proxy)
        _CONN.conn = conn
        _CONN.netloc = netloc
        _CONN.scheme = scheme
        _CONN.proxy = proxy
        _log_route(scheme, netloc, proxy)
    else:
        conn.timeout = timeout_s
    return conn


def _drop_connection() -> None:
    conn = getattr(_CONN, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _CONN.conn = None
    _CONN.netloc = None
    _CONN.scheme = None
    _CONN.proxy = None


def _connect_error(exc: BaseException, *, netloc: str, proxy: tuple | None) -> LLMError:
    """Turn a socket failure into a message that names proxy vs firewall."""
    text = _redact(str(exc))
    if proxy:
        where = f"proxy {proxy[0]}:{proxy[1]}"
        if "407" in text:
            return LLMError(
                f"{where} requires authentication (HTTP 407) — "
                "set HTTPS_PROXY=http://user:pass@host:port or ask IT"
            )
        return LLMError(f"could not reach {netloc} via {where} ({text})")
    low = text.lower()
    if "certificate" in low or "ssl" in low or "tls" in low:
        return LLMError(
            f"TLS error reaching {netloc} ({text}) — "
            "a corporate proxy may be intercepting HTTPS"
        )
    return LLMError(
        f"could not reach {netloc} ({text}) — a proxy or firewall may be blocking it"
    )


def _request(
    model: str,
    messages: list[dict],
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int | None,
    retries: int,
    timeout_s: float,
    api_key: str | None,
    transport: tuple[str, str] | None = None,
) -> tuple[str, dict | None]:
    """POST messages to the gateway with retry/backoff.

    Routes per model (config/models.yaml `api_style` + per-model
    `model_overrides`, hot-loaded): luna uses the OpenAI Responses API
    (/responses — its chat/completions route 500s provider-side since
    2026-08-24); deepseek/qwen use chat/completions. `transport` overrides the
    (api_style, image_url_form) pair for this call only — the vision probe
    (model_catalog.probe_vision) uses it to discover a model's working
    transport. Returns (content, usage) — usage normalized to
    {prompt_tokens, completion_tokens, total_tokens} or None if omitted.
    """
    api_key = api_key or config.opencode_api_key()
    if not api_key:
        raise LLMError("OPENCODE_API_KEY not set — put it in .env (opencode.ai/auth)")

    style = transport[0] if transport else _model_transport(model)[0]
    if style == "responses":
        body: dict[str, Any] = _responses_body(
            model, messages, json_mode=json_mode, temperature=temperature,
            max_tokens=max_tokens)
    else:
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

    payload = json.dumps(body).encode("utf-8")
    parsed = urllib.parse.urlsplit(_endpoint(style))
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            # The gateway WAF blocks urllib's default "Python-urllib/3.x" UA
            # (error 1010) — a descriptive UA is required for the request to
            # reach the model router at all.
            "User-Agent": "verdandi-broker-invoices/1.0",
            # Session-affinity headers the Zen gateway now REQUIRES
            # (2026-09-06: HTTP 400 MissingSessionID without them). A fresh
            # request id per attempt aids correlation; the session id stays
            # stable per process (prompt-cache affinity within a run).
            "x-opencode-session": _SESSION_ID,
            "x-opencode-client": "verdandi",
            "x-opencode-request": uuid.uuid4().hex,
        }
        started = time.perf_counter()
        scheme = parsed.scheme or "https"
        try:
            conn = _connection(scheme, parsed.netloc, timeout_s)
            req_path = path
            proxy = getattr(_CONN, "proxy", None)
            if proxy and scheme == "http":
                req_path = f"http://{parsed.netloc}{path}"
                if proxy[2]:
                    headers["Proxy-Authorization"] = proxy[2]
            conn.request("POST", req_path, body=payload, headers=headers)
            resp = conn.getresponse()
            body_text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
            detail = f": {_redact(body_text[:200])}" if body_text else ""
            if status == 407:
                raise LLMError(
                    f"proxy requires authentication (HTTP 407){detail} — "
                    "set HTTPS_PROXY=http://user:pass@host:port or ask IT"
                )
            if status in (401, 403, 404):
                raise LLMError(f"LLM gateway rejected request (HTTP {status}){detail}")
            if status != 200:
                if status not in RETRY_STATUS:
                    raise LLMError(f"LLM gateway error (HTTP {status}){detail}")
                last_error = LLMError(f"HTTP {status}{detail}")
                _drop_connection()
            else:
                data = json.loads(body_text)
                if style == "responses":
                    content, usage = _parse_responses(data)
                else:
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage")
                if not isinstance(content, str):
                    raise LLMError(f"unexpected content type: {type(content).__name__}")
                elapsed = time.perf_counter() - started
                usage = dict(usage or {})
                usage["elapsed_s"] = round(elapsed, 2)
                _, form = transport if transport else _model_transport(model)
                logger.info(
                    "llm %s: %.1fs, %s+%s tokens (%.0f tok/s) [%s/%s]",
                    model, elapsed,
                    usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                    (usage.get("total_tokens") or 0) / max(elapsed, 0.001),
                    style, form)
                return (content, usage)
        except LLMError:
            raise
        except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
            proxy = getattr(_CONN, "proxy", None)
            err = _connect_error(exc, netloc=parsed.netloc, proxy=proxy)
            _drop_connection()
            if "requires authentication" in str(err):
                raise err
            last_error = err
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            last_error = LLMError(f"malformed gateway response: {exc}")
        if attempt < retries:
            delay = BACKOFF_BASE_S * (2 ** attempt)
            logger.warning(
                "llm call failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1, retries + 1, last_error, delay,
            )
            time.sleep(delay)
    raise LLMError(f"llm call failed after {retries + 1} attempts: {last_error}")


def chat(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    retries: int = DEFAULT_RETRIES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    api_key: str | None = None,
    usage_out: list | None = None,
) -> str:
    """One chat-completions call. Returns the message content as a string.

    `json_mode=True` adds `response_format: {"type": "json_object"}` — the
    gateway guarantees valid JSON, not schema conformance. Raises LLMError on
    failure after retries are exhausted.

    `usage_out`: if provided, each successful attempt's `usage` block is
    appended (token accounting; e.g. extract.py sums it into `_meta.usage`).
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    content, usage = _request(
        model, messages, json_mode=json_mode, temperature=temperature,
        max_tokens=max_tokens, retries=retries, timeout_s=timeout_s, api_key=api_key,
    )
    if usage is not None and usage_out is not None:
        usage_out.append(usage)
    return content


def chat_vision_pages(
    model: str,
    prompt: str,
    images: list[tuple[str, str]],
    *,
    system: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    retries: int = DEFAULT_RETRIES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    api_key: str | None = None,
    usage_out: list | None = None,
    transport: tuple[str, str] | None = None,
) -> str:
    """One multimodal call with several inline images (image order preserved).

    `images` is a list of (base64_string, mime) pairs — e.g. the page renders
    of an attachment in page order (the D24 vision-primary pass).

    IMAGE_URL FORM is per-model (config/models.yaml `image_url_form`,
    hot-loaded — D10): luna/qwen accepted the FLAT string form
    (`image_url: "data:...;base64,..."`); deepseek-v4-flash-vision-exp
    REQUIRES the nested object form (`image_url: {"url": "data:..."}` —
    verified live 2026-08-27; its upstream rejects the flat string with
    "Failed to deserialize... invalid type: string"). Default = flat.

    `transport` overrides (api_style, image_url_form) for this call only —
    the vision probe uses it to discover a model's working transport.

    Same retry/error semantics as chat(). `usage_out` behaves like chat()'s
    (token accounting).
    """
    if transport is not None:
        _, form = transport
    else:
        form = _model_transport(model)[1]
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    content: list[dict] = [{"type": "text", "text": prompt}]
    for image_b64, mime in images:
        data_url = f"data:{mime};base64,{image_b64}"
        if form == "nested":
            url_part: str | dict[str, str] = {"url": data_url}
        else:
            url_part = data_url
        content.append({"type": "image_url", "image_url": url_part})
    messages.append({"role": "user", "content": content})
    result, usage = _request(
        model, messages, json_mode=json_mode, temperature=temperature,
        max_tokens=max_tokens, retries=retries, timeout_s=timeout_s, api_key=api_key,
        transport=transport,
    )
    if usage is not None and usage_out is not None:
        usage_out.append(usage)
    return result
