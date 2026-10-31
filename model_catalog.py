"""Model catalog service — availability, capability hints, vision probes (D35).

Three layers, in trust order:

1. AVAILABILITY — the live GO/Zen catalog (`GET {endpoint}/models`, ~3KB, no
   auth): the only authority on which model ids exist right now. TTL-cached
   (5 min) with a last-snapshot fallback for offline use. The fetch also
   reports which ids appeared/disappeared since the previous snapshot.
2. CAPABILITY HINT — models.dev (`api.json`, community DB): per-model
   `modalities.input` (image/pdf/...), cost and context. Endpoint -> provider
   mapping (`/zen/go/v1` -> `opencode-go`, `/zen/v1` -> `opencode`).
   TTL-cached (24h) as a slim projection. A HINT only — its rows have
   disagreed with live gateway behaviour before (kimi-k3, grok-4.5,
   mimo-v2-omni were rejected live while models.dev claimed image input).
3. GATEWAY TRUTH — the PROBE: one tiny image call through llm_client. The
   first working transport wins (per-model override -> chat flat -> chat
   nested -> responses) and is recorded, so a probe also discovers the
   SWAP-TRAP transport for that model. Only probed models are selectable in
   the UI (`vision_verified:` in config/models.yaml).

No streamlit, stdlib only (D14). The UI imports this; extract.py/process.py
use cached_catalog_ids() for a no-network preflight warning.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import config

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_ROOT / "out" / "cache"
ENV_CACHE_DIR = "VERDANDI_CATALOG_CACHE"


def cache_dir() -> Path:
    """Where catalog snapshots live.

    `VERDANDI_CATALOG_CACHE` wins; packaged apps must use a stable
    user-writable dir (`~/.verdandi/cache`) because the bundle's `_MEIPASS`
    is read-only or wiped between launches — otherwise the drift baseline,
    TTL caching and probe-transport records silently die. Dev checkouts keep
    `out/cache` so existing caches and tests are unaffected.
    """
    env = os.environ.get(ENV_CACHE_DIR)
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        return Path.home() / ".verdandi" / "cache"
    return CACHE_DIR

GO_CATALOG_TTL_S = 300          # availability: 5 min
GO_CATALOG_TIMEOUT_S = 5
MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_TTL_S = 86400        # capability hint: 24h
MODELS_DEV_TIMEOUT_S = 30
USER_AGENT = "verdandi-invoice-lab/1.0"  # the gateway WAF blocks urllib's default UA

# Endpoint -> models.dev provider (the two Zen catalogs are different model sets).
PROVIDER_BY_ENDPOINT = {"zen/go": "opencode-go", "zen": "opencode"}

PROBE_PROMPT = "Reply with exactly: OK"
# 1024, not 8 (2026-10-09): reasoning models (e.g. step-5-preview-free) spend
# their completion budget on the `reasoning` field first and return
# content: null when the cap truncates them — with an 8-token cap the probe
# saw "unexpected content type: NoneType" (no HTTP status -> "transient")
# even though the model reads images fine. A short answer costs the same.
PROBE_MAX_TOKENS = 1024
# One retry: a single 429/5xx/timeout used to fail the probe (retries=0) even
# though a failed call is not billed. Interactive, so only one extra attempt.
PROBE_RETRIES = 1
# Transport candidates tried by the probe, in order, when no override/primary
# transport is known. Covers every live combination on the GO gateway.
PROBE_TRANSPORTS = (("chat_completions", "flat"), ("chat_completions", "nested"),
                    ("responses", "flat"))
# 64x64 PNG fallback (the repo logo is used when present) — content is
# irrelevant, the probe only asks the gateway to accept an image.
PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAABtklEQVR4nOXaMcrqUBAF4JPj32udJWghMY1yS92HpeuxEASRYOMaFG3VQkjpBtLYxUYiNvOK8IIgTwQjz+P/Vbk3EGaYOymG65kZlBHiCHGEuJ/bhed5UGA3fftdFch98n/Juzsj8hUgxBHiCHGEOEIcIY4QR4gjxBHiCHGEOEIcIY4QR4gjxBHiWMpXoigKw7DT6YRhOJvN8s1arZY/JEkSBMHxeMQ72I37nWcsFgvnXJqmZpamqXNutVqZWbVaNbMsy5xzu93OyoD7mB+/fka3291ut8Vys9n0er0igX6/P5lMXgv7zQn4vp9lWbHMssz3/TyB4XA4GAysPLiLsPwmNrN8enO9Xkej0buO/l8lJFCv1+M4LpZxHDcaDQCVSmW/35/P5/F4jPexhwV6xnK5dM6dTqeiidfrddEDSZL4vn84HD63B8xsOp0GQdBut1utVhRF+WaegJnN5/Nms3m5XF6I/J8RereT0Pzsfv5s1L5pOk2II8QR4ghxhDhCHCGOEEeII8QR4ghxhDhCHCGOEEeII8QR4ghxhDji++7MeSJ3F7+kAt4nz+F+RQUIcfzfAbzqD7hoeP5DsCuuAAAAAElFTkSuQmCC"
)


# ---------------------------------------------------------------------------
# cache plumbing (out/cache/*.json — gitignored runtime, best effort)
# ---------------------------------------------------------------------------

def _cache_path(name: str, root: Path | None = None) -> Path:
    return (root or cache_dir()) / name


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # a cache write must never break a UI render


# ---------------------------------------------------------------------------
# 1. availability: the live catalog
# ---------------------------------------------------------------------------

def fetch_go_catalog(endpoint: str, *, ttl_s: float = GO_CATALOG_TTL_S,
                     force: bool = False, cache_dir: Path | None = None,
                     api_key: str | None = None) -> dict:
    """Live model ids from `{endpoint}/models`, TTL-cached with snapshot fallback.

    Returns {ids, fetched_at, from_cache, stale, new, removed, error}.
    `new`/`removed` diff against the PREVIOUS snapshot for the same endpoint —
    a first fetch (no snapshot) is a baseline and reports no drift. The key is
    sent when available (account-scoped catalogs) and the request is retried
    unauthenticated on 401/403, so the public catalog keeps working.
    """
    path = _cache_path("go_catalog.json", cache_dir)
    prev = _read_json(path)
    now = time.time()
    fresh = (prev.get("endpoint") == endpoint
             and now - float(prev.get("fetched_at") or 0) < ttl_s)
    if fresh and not force and prev.get("ids"):
        return {"ids": list(prev["ids"]), "fetched_at": prev.get("fetched_at"),
                "from_cache": True, "stale": False, "new": [], "removed": [],
                "error": None}

    def _request(with_key: bool) -> list[str]:
        headers = {"User-Agent": USER_AGENT}
        if with_key and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(f"{endpoint.rstrip('/')}/models",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=GO_CATALOG_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m["id"] for m in data.get("data", [])
               if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if not ids:
            raise ValueError("empty catalog payload")
        return ids

    try:
        try:
            ids = _request(with_key=True)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and api_key:
                ids = _request(with_key=False)  # public catalog fallback
            else:
                raise
    except Exception as exc:
        if prev.get("endpoint") == endpoint and prev.get("ids"):
            return {"ids": list(prev["ids"]), "fetched_at": prev.get("fetched_at"),
                    "from_cache": True, "stale": True, "new": [], "removed": [],
                    "error": str(exc)}
        return {"ids": [], "fetched_at": None, "from_cache": False, "stale": True,
                "new": [], "removed": [], "error": str(exc)}
    prior = (set(prev["ids"]) if prev.get("endpoint") == endpoint and prev.get("ids")
             else None)
    _write_json(path, {"fetched_at": now, "endpoint": endpoint, "ids": ids})
    return {"ids": ids, "fetched_at": now, "from_cache": False, "stale": False,
            "new": sorted(set(ids) - prior) if prior is not None else [],
            "removed": sorted(prior - set(ids)) if prior is not None else [],
            "error": None}


def cached_catalog_ids(endpoint: str, *, max_age_s: float = 86400,
                       cache_dir: Path | None = None) -> list[str] | None:
    """Last snapshot's ids without any network call (None when absent/stale).

    Used by the pipeline preflight to warn when primary_model is no longer in
    the catalog — never to fail a run.
    """
    prev = _read_json(_cache_path("go_catalog.json", cache_dir))
    if prev.get("endpoint") != endpoint or not prev.get("ids"):
        return None
    if max_age_s <= 0:
        # "must be strictly fresh" — without this guard a just-written cache
        # reads as age 0.0 and passes on low-resolution clocks (Windows).
        return None
    age = time.time() - float(prev.get("fetched_at") or 0)
    return list(prev["ids"]) if age <= max_age_s else None


# ---------------------------------------------------------------------------
# 2. capability hint: models.dev
# ---------------------------------------------------------------------------

def provider_for_endpoint(endpoint: str) -> str:
    """models.dev provider id for a gateway endpoint (both Zen catalogs differ)."""
    return PROVIDER_BY_ENDPOINT["zen/go"] if "zen/go" in (endpoint or "") \
        else PROVIDER_BY_ENDPOINT["zen"]


def project_models_dev(raw: dict, provider: str) -> dict[str, dict]:
    """Slim per-model projection: {id: {name, vision, modalities, cost, context}}."""
    prov = raw.get(provider) or {}
    out: dict[str, dict] = {}
    for mid, rec in (prov.get("models") or {}).items():
        if not isinstance(rec, dict):
            continue
        inp = ((rec.get("modalities") or {}).get("input")) or []
        cost = rec.get("cost") or {}
        limit = rec.get("limit") or {}
        out[mid] = {
            "name": rec.get("name") or mid,
            "vision": "image" in inp,
            "modalities": [str(m) for m in inp],
            "cost_input": cost.get("input"),
            "cost_output": cost.get("output"),
            "context": limit.get("context"),
        }
    return out


def fetch_models_dev(*, endpoint: str = "", ttl_s: float = MODELS_DEV_TTL_S,
                     force: bool = False, cache_dir: Path | None = None) -> dict:
    """Capability hints for the endpoint's provider, TTL-cached + offline fallback.

    Returns {models, fetched_at, from_cache, provider, error}.
    """
    provider = provider_for_endpoint(endpoint)
    path = _cache_path("models_dev.json", cache_dir)
    prev = _read_json(path)
    now = time.time()
    fresh = (prev.get("provider") == provider
             and now - float(prev.get("fetched_at") or 0) < ttl_s)
    if fresh and not force and prev.get("models"):
        return {"models": prev["models"], "fetched_at": prev.get("fetched_at"),
                "from_cache": True, "provider": provider, "error": None}
    try:
        req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=MODELS_DEV_TIMEOUT_S) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        models = project_models_dev(raw, provider)
        if not models:
            raise ValueError(f"no models for provider {provider!r}")
    except Exception as exc:
        if prev.get("provider") == provider and prev.get("models"):
            return {"models": prev["models"], "fetched_at": prev.get("fetched_at"),
                    "from_cache": True, "provider": provider, "error": str(exc)}
        return {"models": {}, "fetched_at": None, "from_cache": False,
                "provider": provider, "error": str(exc)}
    _write_json(path, {"fetched_at": now, "provider": provider, "models": models})
    return {"models": models, "fetched_at": now, "from_cache": False,
            "provider": provider, "error": None}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def classify(model: str, catalog_ids: list[str], dev_models: dict,
             verified: list[str]) -> str:
    """One of: verified | candidate | text_only | unknown | missing.

    `missing` is only reported when the catalog is known (a failed fetch with
    no snapshot must not make every model look removed).
    """
    if catalog_ids and model not in catalog_ids:
        return "missing"
    if model in verified:
        return "verified"
    rec = dev_models.get(model)
    if rec is None:
        return "unknown"
    return "candidate" if rec.get("vision") else "text_only"


def describe(model: str, dev_models: dict) -> str:
    """Compact label suffix: '$0.20/$1.20 · 1M ctx' (empty when no hint)."""
    rec = dev_models.get(model) or {}
    bits = []
    ci, co = rec.get("cost_input"), rec.get("cost_output")
    if isinstance(ci, (int, float)) and isinstance(co, (int, float)):
        bits.append(f"${ci:g}/${co:g}")
    ctx = rec.get("context")
    if isinstance(ctx, int) and ctx > 0:
        bits.append(f"{ctx / 1_000_000:g}M ctx" if ctx >= 1_000_000 else f"{ctx // 1000}K ctx")
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# 3. gateway truth: the vision probe
# ---------------------------------------------------------------------------

_HTTP_RE = re.compile(r"HTTP\s+(\d{3})")
_QUOTA_RE = re.compile(
    r"quota|subscription|billing|insufficient[_\s-]?quota|payment required",
    re.I,
)
_ATTEMPT_PREFIX_RE = re.compile(r"^llm call failed after \d+ attempts:\s*")


def _http_status(message: str) -> int | None:
    m = _HTTP_RE.search(message or "")
    return int(m.group(1)) if m else None


def _probe_error_class(status: int | None, error: str) -> str:
    """auth/route fail fast; quota is a plan problem; 407/429/5xx/no-status stay inconclusive."""
    text = error or ""
    if status in (401, 403):
        return "quota" if _QUOTA_RE.search(text) else "auth"
    if status == 404:
        return "route"
    if status == 402 or _QUOTA_RE.search(text):
        return "quota"
    if status == 407 or status is None or status == 429 or (status is not None and status >= 500):
        return "transient"
    return "rejected"


def _attempt_detail(result: dict) -> str:
    """Short redacted cause from the attempt that matches the outcome class."""
    attempts = result.get("attempts") or []
    want = result.get("error_class") or "transient"
    chosen = None
    for attempt in attempts:
        if _probe_error_class(attempt.get("status"), attempt.get("error") or "") == want:
            chosen = attempt
    if chosen is None and attempts:
        chosen = attempts[-1]
    if not chosen:
        return ""
    err = _ATTEMPT_PREFIX_RE.sub("", str(chosen.get("error") or "").strip())
    err = re.sub(r"\s+", " ", err)
    if not err:
        status = chosen.get("status")
        return f"HTTP {status}" if status else ""
    import llm_client
    return llm_client._redact(err)[:180]


def probe_outcome_message(model: str, result: dict) -> str:
    """Human text for a non-success probe. Success is the caller's message."""
    if result.get("vision") is False:
        return f"{model}: rejected the image on every transport — not vision-capable"
    cls = result.get("error_class") or "transient"
    detail = _attempt_detail(result)
    if cls == "quota":
        extra = f" ({detail})" if detail else ""
        return (f"{model}: no remaining GO quota / plan access for this model"
                f"{extra} — check your plan at opencode.ai")
    if detail:
        return f"{model}: probe inconclusive ({cls}: {detail}) — try again later"
    return f"{model}: probe inconclusive ({cls}) — try again later"


_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".webp": "image/webp", ".png": "image/png"}


def _probe_image(image_path: Path | None) -> tuple[str, str]:
    """(base64, mime) for the probe — mime inferred from the file suffix so a
    JPEG render can validate the gateway's JPEG acceptance (D42)."""
    if image_path:
        try:
            if image_path.exists():
                mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/png")
                return base64.b64encode(image_path.read_bytes()).decode("ascii"), mime
        except OSError:
            pass
    return PROBE_PNG_B64, "image/png"


def _transport_candidates(model: str, cfg: dict) -> list[tuple[str, str]]:
    """Known transport first (override -> primary), then the probe matrix."""
    out: list[tuple[str, str]] = []
    over = (cfg.get("model_overrides") or {}).get(model) or {}
    if over.get("api_style"):
        out.append((over["api_style"],
                    over.get("image_url_form", cfg.get("image_url_form", "flat"))))
    if model == cfg.get("primary_model"):
        out.append((cfg.get("api_style", "chat_completions"),
                    cfg.get("image_url_form", "flat")))
    out.extend(PROBE_TRANSPORTS)
    seen: set[tuple[str, str]] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


def _persist_attempts(attempts: list | None) -> list[dict]:
    """Last few attempts, redacted — the toast and the cache must agree."""
    import llm_client
    out = []
    for attempt in (attempts or [])[-3:]:
        item = {
            "api_style": attempt.get("api_style"),
            "image_url_form": attempt.get("image_url_form"),
            "status": attempt.get("status"),
        }
        if attempt.get("error"):
            item["error"] = llm_client._redact(str(attempt["error"]))[:200]
        out.append(item)
    return out


def save_probe_result(model: str, result: dict, *, cache_dir: Path | None = None) -> None:
    """Persist one probe outcome in out/cache/model_probes.json (audit + transport)."""
    path = _cache_path("model_probes.json", cache_dir)
    data = _read_json(path)
    data[model] = {
        "checked_at": time.time(),
        "vision": result.get("vision"),
        "transport": result.get("transport"),
        "tokens": result.get("tokens"),
        "error_class": result.get("error_class"),
        "attempts": _persist_attempts(result.get("attempts")),
    }
    _write_json(path, data)


def probe_transport(model: str, *, cache_dir: Path | None = None) -> dict | None:
    """The transport a previous successful probe discovered for `model`, if any."""
    rec = _read_json(_cache_path("model_probes.json", cache_dir)).get(model) or {}
    tr = rec.get("transport")
    return dict(tr) if isinstance(tr, dict) and tr.get("api_style") else None


def _finish_probe(model: str, result: dict, cache_dir: Path | None) -> dict:
    save_probe_result(model, result, cache_dir=cache_dir)
    if result.get("vision") is not True:
        detail = _attempt_detail(result)
        log.warning("probe %s: %s%s", model, result.get("error_class"),
                    f" ({detail})" if detail else "")
    return result


def probe_vision(model: str, *, endpoint: str | None = None,
                 image_path: Path | None = None, timeout_s: float = 90.0,
                 cache_dir: Path | None = None) -> dict:
    """One tiny image call per transport until one succeeds (paid, ~100-300 tokens).

    Returns {vision: bool|None, transport, tokens, error_class, attempts}:
      - vision True  -> the gateway accepted the image; `transport` is recorded
                        (and cached for promote_model).
      - vision False -> every transport/model combination rejected the image
                        (error_class "rejected").
      - vision None  -> auth/route/quota, or transient (407/429/5xx/no status):
                        do NOT blacklist; error_class "auth"|"route"|"quota"|"transient".
    """
    import llm_client  # lazy: model_catalog stays importable without an API key

    cfg = config.load_models()
    endpoint = endpoint or cfg["endpoint"]
    b64, mime = _probe_image(image_path)
    attempts: list[dict] = []
    saw_transient = False
    result: dict
    for style, form in _transport_candidates(model, cfg):
        usage: list[dict] = []
        try:
            llm_client.chat_vision_pages(
                model, PROBE_PROMPT, [(b64, mime)], json_mode=False,
                max_tokens=PROBE_MAX_TOKENS, timeout_s=timeout_s,
                retries=PROBE_RETRIES,
                transport=(style, form), usage_out=usage,
            )
            tokens = sum((u or {}).get("total_tokens", 0) or 0 for u in usage)
            attempts.append({"api_style": style, "image_url_form": form, "status": 200})
            result = {"vision": True,
                      "transport": {"api_style": style, "image_url_form": form},
                      "tokens": tokens, "error_class": None, "attempts": attempts}
            save_probe_result(model, result, cache_dir=cache_dir)
            return result
        except llm_client.LLMError as exc:
            status = _http_status(str(exc))
            error = str(exc)[:200]
            attempts.append({"api_style": style, "image_url_form": form,
                             "status": status, "error": error})
            cls = _probe_error_class(status, error)
            if cls in ("auth", "route", "quota"):
                result = {"vision": None, "transport": None, "tokens": 0,
                          "error_class": cls, "attempts": attempts}
                return _finish_probe(model, result, cache_dir)
            if cls == "transient":
                saw_transient = True
    result = {"vision": False if not saw_transient else None, "transport": None,
              "tokens": 0,
              "error_class": "rejected" if not saw_transient else "transient",
              "attempts": attempts}
    return _finish_probe(model, result, cache_dir)


# ---------------------------------------------------------------------------
# CLI: python model_catalog.py [--refresh] — status table (make models)
# ---------------------------------------------------------------------------

def _status_table(force: bool = False) -> str:
    cfg = config.load_models()
    endpoint = cfg["endpoint"]
    cat = fetch_go_catalog(endpoint, force=force)
    dev = fetch_models_dev(endpoint=endpoint, force=force)
    verified = list(cfg.get("vision_verified") or [])
    lines = [
        f"endpoint: {endpoint}  (provider {provider_for_endpoint(endpoint)})",
        f"catalog: {len(cat['ids'])} model(s)"
        + (" [cached]" if cat["from_cache"] else "")
        + (f" — ERROR {cat['error']}" if cat["error"] else ""),
        f"capability hint: {len(dev['models'])} model(s)"
        + (" [cached]" if dev["from_cache"] else "")
        + (f" — ERROR {dev['error']}" if dev["error"] else ""),
        f"verified (probed): {', '.join(verified) or '—'}",
    ]
    if cat["new"]:
        lines.append(f"new since last snapshot: {', '.join(cat['new'])}")
    if cat["removed"]:
        lines.append(f"removed since last snapshot: {', '.join(cat['removed'])}")
    lines.append("")
    for mid in cat["ids"]:
        cls = classify(mid, cat["ids"], dev["models"], verified)
        desc = describe(mid, dev["models"])
        name = (dev["models"].get(mid) or {}).get("name") or ""
        lines.append(f"  {mid:34} {cls:10} {desc:18} {name}")
    primary = cfg["primary_model"]
    if primary not in cat["ids"] and cat["ids"]:
        lines.append("")
        lines.append(f"WARNING: primary_model {primary!r} is not in the live catalog")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Model catalog status (D35)")
    ap.add_argument("--refresh", action="store_true",
                    help="bypass the TTL caches and re-fetch catalog + models.dev")
    args = ap.parse_args(argv)
    print(_status_table(force=args.refresh))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
