"""UI backend — subprocess orchestration + runs/review/model helpers (Streamlit app).

The UI never touches the pipeline's globals: it shells out to the existing CLIs
(text_layer.py / extract.py) and reads results back from the run folder — the
run/snapshot discipline (D18) stays fully intact. Review state lives in
<run>/review.json (keyed by file|field|reason|detail) so it survives any
reprocessing of a run; recompute_flags() rebuilds <run>/flags.csv locally from
the stored JSONs (the deterministic post-pass) — used after single-file retries
that would otherwise clobber the run-level flags file.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
INBOX = REPO_ROOT / "inbox"
RUNS = REPO_ROOT / "out" / "runs"

EXTRACT_TIMEOUT = 600
EXTRACT_MAX_TOKENS = 8192  # trimmed 2026-09-03 (completions peak ~2.2k; 4x headroom)
# Vision input cap (2026-09-03 cost lever; same default as the Makefile's
# VISION_CAP). The capture writes pageNNN.render-vision.png at this long edge —
# extraction prefers it, OCR/grounding keep the full-resolution render.
VISION_CAP = 1536


# ---------------------------------------------------------------------------
# subprocess orchestration
# ---------------------------------------------------------------------------

def run_streamed(args: list[str]) -> Iterator[tuple[str, str | int]]:
    """Run a pipeline CLI, yielding ("line", text) per stdout line, then
    ("exit", returncode). Keeps Streamlit responsive with live logs."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=REPO_ROOT,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield ("line", line.rstrip())
    proc.wait()
    yield ("exit", proc.returncode)


# ---------------------------------------------------------------------------
# detached pipeline stages (D37): paid stages must survive the UI session
# ---------------------------------------------------------------------------

def _stage_paths(run_dir: Path, stage: str) -> tuple[Path, Path, Path]:
    logs = run_dir / "logs"
    return (logs / f"{stage}.log", logs / f"{stage}.exit", logs / f"{stage}.pid")


def start_stage(run_dir: Path, stage: str, args: list[str]) -> int:
    """Start a pipeline CLI DETACHED, logging to <run>/logs/<stage>.log.

    The process runs in its own session (survives Streamlit reruns, tab
    switches and browser reconnects), and a shell wrapper writes the exit code
    to <stage>.exit — so the UI can re-attach to a run at any time instead of
    losing it. Returns the shell pid.
    """
    log_path, exit_path, pid_path = _stage_paths(run_dir, stage)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (exit_path,):
        if p.exists():
            p.unlink()
    cmd = " ".join(shlex.quote(a) for a in args)
    script = (f"echo $$ > {shlex.quote(str(pid_path))}; "
              f"{cmd} > {shlex.quote(str(log_path))} 2>&1; "
              f"echo $? > {shlex.quote(str(exit_path))}")
    proc = subprocess.Popen(["/bin/sh", "-c", script], cwd=REPO_ROOT,
                            start_new_session=True)
    return proc.pid


def stage_status(run_dir: Path, stage: str, *, tail_bytes: int = 4000) -> dict:
    """{running, exit, log, pid, started} for a detached stage — no session state.

    `exit` is None while running; if the marker never appears and the pid is
    gone, the stage was killed (running=False, exit=None).
    """
    log_path, exit_path, pid_path = _stage_paths(run_dir, stage)
    exit_code = None
    if exit_path.exists():
        try:
            exit_code = int(exit_path.read_text(encoding="utf-8").strip() or "-1")
        except (OSError, ValueError):
            exit_code = -1
    pid = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
    running = False
    if exit_code is None and pid:
        try:
            os.kill(pid, 0)  # signal 0 = existence check
            running = True
        except OSError:
            running = False
    log = ""
    if log_path.exists():
        try:
            log = log_path.read_bytes()[-tail_bytes:].decode("utf-8", errors="replace")
        except OSError:
            log = ""
    return {"running": running, "exit": exit_code, "log": log, "pid": pid,
            "started": pid_path.exists()}


def new_run() -> Path:
    """Fresh self-contained run (D18): out/runs/<date>/<N> + out/latest."""
    out = subprocess.run(
        [sys.executable, "process.py", "--new-run"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]
    return Path(out)


def capture(raw_dir: Path, stems: list[str] | None = None) -> list[str]:
    """text_layer over the uploads inbox (renders + text channel + quality gate).

    `stems`: scope the capture to JUST these files (2026-09-05 fix — the old
    call captured EVERY file that had ever been uploaded to inbox/).
    """
    args = [sys.executable, "text_layer.py", str(INBOX), str(raw_dir), "--custom-words",
            "--vision-cap", str(VISION_CAP)]
    if stems:
        args += ["--files", ",".join(stems)]
    return args


def extract(raw_dir: Path, json_dir: Path, model: str, doc_id: str | None = None,
            doc_ids: list[str] | None = None, files: list[str] | None = None,
            pin_pages: list[int] | None = None, pin_label: str | None = None,
            timeout: int = EXTRACT_TIMEOUT, max_tokens: int = EXTRACT_MAX_TOKENS,
            workers: int = 1) -> list[str]:
    """Vision-primary extraction (1 paid call per file) + flags.csv.

    D26: `pin_pages` constrains extraction to a user-picked document
    (--pin-pages/--pin-label); `doc_id` selects the document profile;
    `doc_ids` (2026-09-05) selects MULTIPLE profiles in one call
    (--documents a,b — every matching document becomes its own entry).
    `workers` (D42): parallel files at the same call count/cost.
    """
    args = [sys.executable, "extract.py", str(raw_dir), str(json_dir),
            "--model", model, "--timeout", str(timeout), "--max-tokens", str(max_tokens)]
    if workers and int(workers) > 1:
        args += ["--workers", str(int(workers))]
    if doc_ids:
        # One profile is a --document target; 2+ become a single multi-doc call
        # (a single-element --documents list would silently fall back to the
        # default profile — the extract.py CLI rejects it for the same reason).
        if len(doc_ids) > 1:
            args += ["--documents", ",".join(doc_ids)]
        else:
            args += ["--document", doc_ids[0]]
    elif doc_id:
        args += ["--document", doc_id]
    if files:
        args += ["--files", ",".join(files)]
    if pin_pages:
        spec = ",".join(str(p) for p in sorted(pin_pages))
        args += ["--pin-pages", spec]
        if pin_label:
            args += ["--pin-label", pin_label]
    return args


def catalog(raw_dir: Path, run_dir: Path, model: str, timeout: int = 120,
            max_tokens: int = 2048) -> list[str]:
    """D26 discovery sweep: one cheap call per file, listing its documents."""
    return [sys.executable, "extract.py", str(raw_dir), str(run_dir / "json"),
            "--catalog", "--model", model, "--timeout", str(timeout),
            "--max-tokens", str(max_tokens)]


def load_catalog(run_dir: Path, stem: str) -> dict | None:
    p = run_dir / "catalog" / f"{stem}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def recompute_flags(raw_dir: Path, json_dir: Path) -> int:
    """Rebuild <run>/flags.csv from the stored JSONs (local deterministic pass).

    Needed after single-file retries: extract.py --file writes flags.csv for
    that file only; this recomputes the full run-level flags file from all
    stored JSONs without re-calling the LLM (the verification stack runs on
    copies — the stored JSONs keep their original _meta.verification).
    """
    import config
    import extract as extract_mod
    import verify as verify_mod

    cfg = config.load_all()
    flags: list[dict] = []
    for stem in sorted(d.name for d in raw_dir.iterdir() if d.is_dir()):
        dest = json_dir / f"{stem}.json"
        if not dest.exists():
            continue
        data = json.loads(dest.read_text(encoding="utf-8"))
        if data.get("status") == "failed":
            continue
        meta = data.get("_meta") or {}
        pinned = (meta.get("pinned") or {}).get("pages")
        if data.get("documents") is not None:
            # Multi-document entry: per-entry stack + duplicate backstop, then
            # the workflow's cross-document rules and completeness contract —
            # the same row set run_batch wrote.
            doc_ids = meta.get("documents") or [
                d.get("template_id") for d in data.get("documents", [])]
            rows, _ = verify_mod.verify_multidoc(stem, data, raw_dir, cfg,
                                                 [d for d in doc_ids if d],
                                                 pinned=pinned)
            wf_id = (meta.get("workflow") or {}).get("id")
            wf = next((w for w in cfg.get("workflows", {}).get("workflows", [])
                       if w.get("id") == wf_id), None) if wf_id else None
            rules = list((wf or {}).get("cross_checks") or [])
            if rules:
                cross_rows, _ = verify_mod.check_cross_document(stem, data, cfg, rules)
                rows = rows + cross_rows
            expected = list((wf or {}).get("expected") or [])
            if expected:
                rows = rows + extract_mod.missing_expected_rows(
                    stem, data, expected, meta.get("document") or "", str(wf_id))
            flags.extend(rows)
        else:
            doc_id = meta.get("document") or cfg["documents"]["documents"][0]["id"]
            rows, _ = verify_mod.verify_file(stem, data, raw_dir, cfg, doc_id,
                                             pinned=pinned)
            flags.extend(rows)
        docq = extract_mod._document_quality_row(stem, raw_dir)
        if docq:
            flags.append(docq)
    extract_mod.write_flags(flags, json_dir.parent)
    return len(flags)


# ---------------------------------------------------------------------------
# model picker / config
# ---------------------------------------------------------------------------

def catalog_snapshot(force: bool = False, *, force_dev: bool | None = None) -> dict:
    """Live catalog + capability hints + classification for the model picker (D35).

    All from model_catalog (TTL-cached; no paid calls). Returns
    {catalog, dev, verified, primary, statuses, candidates, text_only, offline}.
    The key (when set) authenticates the catalog fetch so an account-scoped
    catalog stays accurate; 401/403 falls back to the public catalog.
    `force_dev` forces the models.dev refetch independently (Settings' quiet
    stale refresh keeps its 24h TTL).
    """
    import config
    import model_catalog

    m = config.load_models()
    cat = model_catalog.fetch_go_catalog(m["endpoint"], force=force,
                                         api_key=config.opencode_api_key())
    dev = model_catalog.fetch_models_dev(
        endpoint=m["endpoint"],
        force=force if force_dev is None else force_dev)
    verified = list(m.get("vision_verified") or [])
    statuses = {mid: model_catalog.classify(mid, cat["ids"], dev["models"], verified)
                for mid in cat["ids"]}
    return {
        "catalog": cat, "dev": dev, "verified": verified,
        "primary": m["primary_model"], "statuses": statuses,
        "candidates": sorted(mid for mid, c in statuses.items()
                             if c in ("candidate", "unknown")),
        "text_only": sorted(mid for mid, c in statuses.items() if c == "text_only"),
        "offline": bool(cat["error"]) or bool(dev["error"]),
        "notes": m.get("model_notes") or {},  # D43: measured speed hints
    }


def model_label(model: str, snap: dict) -> str:
    """Picker label: 'id · verified · $0.20/$1.20 · 1M ctx · measured hint'."""
    import model_catalog

    bits = [model]
    if snap["catalog"]["ids"] and model not in snap["catalog"]["ids"]:
        bits.append("⚠ not in catalog")
    elif model in snap["verified"]:
        bits.append("verified")
    else:
        bits.append("⚠ not verified")
    desc = model_catalog.describe(model, snap["dev"]["models"])
    if desc:
        bits.append(desc)
    note = (snap.get("notes") or {}).get(model)  # D43 measured hints
    if note:
        bits.append(note)
    return "  ·  ".join(bits)


def probe_cost_estimate(models: list[str], snap: dict) -> float:
    """Rough $ estimate for probing `models` (~1200 tokens each, input price).

    Conservative since 2026-10-09: reasoning models can spend up to the
    probe's completion cap before answering (see PROBE_MAX_TOKENS)."""
    total = 0.0
    for m in models:
        rec = snap["dev"]["models"].get(m) or {}
        price = rec.get("cost_input")
        total += ((price if isinstance(price, (int, float)) else 0.5) / 1_000_000) * 1200
    return total


def _set_vision_verified(text: str, model: str) -> str:
    """Add `model` to the `vision_verified:` list in models.yaml text (surgical:
    the heavily-commented file is never re-serialized)."""
    block_re = re.search(r"(?ms)^vision_verified:\s*\n((?:[ \t]*-[^\n]*\n?)*)", text)
    if block_re:
        ids = re.findall(r"^[ \t]*-[ \t]*(\S+)[ \t]*$", block_re.group(1), re.M)
        if model in ids:
            return text
        ids.append(model)
        block = "".join(f"- {i}\n" for i in ids)
        return text[:block_re.start()] + "vision_verified:\n" + block + text[block_re.end():]
    block = f"vision_verified:\n- {model}\n"
    if re.search(r"(?m)^model_overrides:", text):
        return re.sub(r"(?m)^model_overrides:", block + "model_overrides:", text, count=1)
    return text.rstrip("\n") + "\n" + block


def prune_vision_verified(removed: list[str]) -> list[str]:
    """Drop GO-removed models from `vision_verified:` (surgical YAML edit).

    Called on a fresh catalog fetch with real drift (2026-10-05). Returns the
    ids actually removed; `model_overrides` are kept so re-adding a model
    restores its probed transport.
    """
    import config

    if not removed:
        return []
    gone = set(removed)
    path = config.MODELS_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    block_re = re.search(r"(?ms)^vision_verified:\s*\n((?:[ \t]*-[^\n]*\n?)*)", text)
    if not block_re:
        return []
    ids = re.findall(r"^[ \t]*-[ \t]*(\S+)[ \t]*$", block_re.group(1), re.M)
    dropped = [m for m in ids if m in gone]
    if not dropped:
        return []
    kept = [m for m in ids if m not in gone]
    block = ("vision_verified:\n" + "".join(f"- {m}\n" for m in kept)
             if kept else "vision_verified: []\n")
    path.write_text(text[:block_re.start()] + block + text[block_re.end():],
                    encoding="utf-8")
    loaded = config.load_models()
    return [m for m in dropped
            if m not in (loaded.get("vision_verified") or [])]


def _set_model_override(text: str, model: str, style: str, form: str) -> str:
    """Record a probed transport under `model_overrides:` (surgical edit).

    Keeps the heavily-commented file intact. The override makes the probed
    transport durable (the runtime probe cache can be pruned) and is what
    `llm_client._model_transport` reads per model.
    """
    entry = f'  {model}:\n    api_style: "{style}"\n    image_url_form: "{form}"\n'
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines)
                  if re.match(r"^model_overrides:\s*$", l)), None)
    if start is None:
        base = text if text.endswith("\n") else text + "\n"
        return base + "model_overrides:\n" + entry
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t")):
            end = i
            break
    for i in range(start + 1, end):
        if re.match(rf"^  {re.escape(model)}:\s*$", lines[i]):
            m_end = end
            for j in range(i + 1, end):
                if re.match(r"^  \S", lines[j]):
                    m_end = j
                    break
            lines[i:m_end] = [entry]
            return "".join(lines)
    if end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] = lines[end - 1] + "\n"  # file-final line without newline
    lines[end:end] = [entry]
    return "".join(lines)


def _record_verified(model: str, transport: dict | None) -> None:
    """Persist a successful probe: vision_verified + the discovered transport
    in model_overrides (config/models.yaml), then re-validate."""
    import config
    path = config.MODELS_PATH
    text = _set_vision_verified(path.read_text(encoding="utf-8"), model)
    if transport and transport.get("api_style"):
        text = _set_model_override(text, model, transport["api_style"],
                                   transport.get("image_url_form", "flat"))
    path.write_text(text, encoding="utf-8")
    loaded = config.load_models()
    assert model in (loaded.get("vision_verified") or []), \
        "vision_verified write failed validation"
    if transport and transport.get("api_style"):
        over = (loaded.get("model_overrides") or {}).get(model) or {}
        assert over.get("api_style") == transport["api_style"], \
            "model_overrides write failed validation"


def _add_vision_verified(model: str) -> None:
    """Write `model` into vision_verified in config/models.yaml + re-validate."""
    _record_verified(model, None)


def verify_model(model: str) -> str:
    """Probe `model` with one tiny image; on success record it in models.yaml.

    Returns a human-readable outcome (never raises for a negative probe; a
    config-write failure does raise). Paid: ~100-300 tokens.
    """
    import config
    import model_catalog

    m = config.load_models()
    result = model_catalog.probe_vision(
        model, endpoint=m["endpoint"],
        image_path=REPO_ROOT / "ui" / "static" / "verdandi-128.png")
    if result["vision"] is True:
        tr = result["transport"] or {}
        _record_verified(model, tr)
        return (f"{model}: vision OK ({result['tokens']} tokens, "
                f"{tr.get('api_style')}/{tr.get('image_url_form', 'flat')}) — verified")
    return model_catalog.probe_outcome_message(model, result)


def promote_model(model: str, transport: dict | None = None) -> str:
    """Write primary_model (+ its probed transport) into config/models.yaml.

    Surgical line edits keep the comments. `transport` = the probe's
    {api_style, image_url_form}; when absent, the cached probe result is used.
    Also ensures the model is in `vision_verified`. D10 hot-load: the next run
    already uses it.
    """
    import config
    import model_catalog

    path = config.MODELS_PATH
    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^primary_model:.*$", text):
        raise ValueError("config/models.yaml has no primary_model line — refusing to edit")
    text = re.sub(r"(?m)^primary_model:.*$", f'primary_model: "{model}"', text, count=1)
    tr = transport or model_catalog.probe_transport(model) or {}
    if tr.get("api_style"):
        text = re.sub(r"(?m)^api_style:.*$", f'api_style: "{tr["api_style"]}"', text, count=1)
    if tr.get("image_url_form"):
        text = re.sub(r"(?m)^image_url_form:.*$",
                      f'image_url_form: "{tr["image_url_form"]}"', text, count=1)
    text = _set_vision_verified(text, model)
    path.write_text(text, encoding="utf-8")
    loaded = config.load_models()
    assert loaded["primary_model"] == model
    bits = [f"primary_model set to {model}"]
    if tr:
        bits.append(f"transport {tr.get('api_style')}/{tr.get('image_url_form', 'flat')}")
    return " — ".join(bits) + " (next run uses it; revert = edit YAML)"


def document_options() -> list[str]:
    """Target document profile ids from rules/documents.yaml (hot-loaded, D25)."""
    import config
    return [d["id"] for d in config.load_all()["documents"]["documents"]]


def document_label(doc_id: str) -> str:
    import config
    for d in config.load_all()["documents"]["documents"]:
        if d["id"] == doc_id:
            return f"{d['label']} ({d['id']})"
    return doc_id


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def list_runs() -> list[dict]:
    items = []
    if not RUNS.is_dir():
        return items
    # Directory-only iteration: out/runs accumulates macOS junk (.DS_Store)
    # and must never be treated as a run-day directory.
    days = [d for d in RUNS.iterdir() if d.is_dir()]
    for day in sorted(days, reverse=True):
        runs = [p for p in day.iterdir() if p.is_dir() and p.name.isdigit()]
        for run in sorted(runs, key=lambda p: int(p.name), reverse=True):
            json_dir = run / "json"
            flags_file = run / "flags.csv"
            jsons = sorted(json_dir.glob("*.json")) if json_dir.is_dir() else []
            ok = failed = 0
            model = "?"
            tokens = prompt_tokens = completion_tokens = 0
            kind = "capture"
            for p in jsons:
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if d.get("status") == "failed":
                    failed += 1
                    continue
                ok += 1
                meta = d.get("_meta") or {}
                model = meta.get("model") or model
                usage = meta.get("usage") or {}
                tokens += int(usage.get("total_tokens") or 0)
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                if meta.get("workflow"):
                    kind = "workflow"
            if jsons and kind == "capture":
                kind = "extraction"
            flags = 0
            if flags_file.exists():
                with flags_file.open(encoding="utf-8", newline="") as fh:
                    flags = max(0, sum(1 for _ in fh) - 1)
            reviewed = sum(1 for s in load_review(run).values()
                           if s.get("status") in ("confirmed", "issue"))
            items.append({
                "dir": str(run), "date": day.name, "n": int(run.name),
                "ok": ok, "failed": failed, "files": len(jsons),
                "flags": flags, "reviewed": reviewed,
                "model": model, "tokens": tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "kind": kind,
            })
    return sorted(items, key=lambda r: r["dir"], reverse=True)


def runs_chart_frame(runs: list[dict], metric: str = "total") -> tuple[list[dict], int]:
    """Chart rows for the Runs tab (D41), OLDEST -> newest.

    `metric`: "total" (raw prompt/completion tokens) or "per_file" (divided by
    the run's file count — fairer across run sizes). Zero-token runs are
    skipped; returns (rows, skipped_count). Pure Python — the Altair spec is
    built in the UI.
    """
    rows: list[dict] = []
    skipped = 0
    for r in runs:
        total = int(r.get("tokens") or 0)
        if total <= 0:
            skipped += 1
            continue
        div = max(int(r.get("files") or 0), 1) if metric == "per_file" else 1
        rows.append({
            "run": f"{str(r.get('date', ''))[5:]} #{r.get('n')}",
            "kind": r.get("kind") or "extraction",
            "model": r.get("model") or "?",
            "files": int(r.get("files") or 0),
            "flags": int(r.get("flags") or 0),
            "prompt": round(int(r.get("prompt_tokens") or 0) / div, 1),
            "completion": round(int(r.get("completion_tokens") or 0) / div, 1),
            "total": round(total / div, 1),
        })
    rows.reverse()  # list_runs is newest-first; the chart reads left-to-right in time
    return rows, skipped


def load_json(run_dir: Path, stem: str) -> dict | None:
    p = run_dir / "json" / f"{stem}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_flags(run_dir: Path) -> list[dict]:
    p = run_dir / "flags.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# review state (per run, survives reprocessing)
# ---------------------------------------------------------------------------

def review_path(run_dir: Path) -> Path:
    return run_dir / "review.json"


def load_review(run_dir: Path) -> dict:
    p = review_path(run_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_review(run_dir: Path, review: dict) -> None:
    review_path(run_dir).write_text(json.dumps(review, indent=2), encoding="utf-8")


def flag_key(row: dict) -> str:
    """Review-state key (D57): file|entry|template|field|reason|detail.

    The `entry` segment (documents[] index, empty for single-document /
    file-level rows) keeps two identical flags on two occurrences of the same
    template from colliding — previously a review on entry #2 could not be
    told apart from entry #1.
    """
    return (f"{row.get('file')}|{row.get('entry', '') if row.get('entry') is not None else ''}"
            f"|{row.get('template') or ''}|{row.get('field')}|{row.get('reason')}"
            f"|{row.get('detail')}")


def legacy_flag_key(row: dict) -> str:
    """Pre-D57 key format (file|template|field|reason|detail) — kept so
    review.json written before the change keeps matching its flags."""
    return (f"{row.get('file')}|{row.get('template') or ''}|{row.get('field')}|"
            f"{row.get('reason')}|{row.get('detail')}")


def review_state(review: dict, row: dict) -> dict:
    """The review entry for a flag row, trying the D57 key then the legacy
    format (old runs keep their review state after the key change)."""
    if not review:
        return {}
    return review.get(flag_key(row)) or review.get(legacy_flag_key(row)) or {}


def _review_timestamp() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def entry_index(value) -> int | None:
    """Coerce a flag row's `entry` (int, numeric string, or empty) to int."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def apply_review(run_dir: Path, row: dict, status: str, note: str = "",
                 *, value: str | None = None) -> str:
    """Write a review outcome into <run>/json/<stem>.json (D39).

    Per-field flags annotate the matching header field (top-level, or the
    documents[] entry matching the flag's `template`): review.status/note/
    reviewed_at; a CONFIRMED edited value replaces field.value and keeps
    review.original_value — confidence/verification are never touched. Flags
    without a matching field (selection/line-item/duplicate/completeness) are
    recorded in _meta.review.entries. review.json stays the source of truth;
    this mirrors it into the final JSON. Returns a short status message.
    """
    stem = str(row.get("file") or "")
    path = run_dir / "json" / f"{stem}.json"
    if not stem or not path.exists():
        return f"no JSON to annotate for {stem or '?'} (review saved)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"unreadable JSON for {stem} (review saved)"
    at = _review_timestamp()
    template = row.get("template") or None
    name = row.get("field") or ""
    entry_idx = entry_index(row.get("entry"))
    # D57: resolve the field in the flag's OWN occurrence (documents[entry]).
    field = find_field(data, name, template, entry=entry_idx) if name else None
    entry = {"template": template or (data.get("_meta") or {}).get("document") or "",
             "field": name, "reason": row.get("reason") or "",
             "status": status, "note": note, "reviewed_at": at,
             "entry": entry_idx if entry_idx is not None else ""}
    corrected = False
    if field is not None:
        block = {"status": status, "note": note, "reviewed_at": at}
        if status == "confirmed" and value is not None:
            new_val = str(value).strip()
            old_val = field.get("value")
            if new_val and new_val != str(old_val):
                block["original_value"] = old_val
                field["value"] = new_val
                corrected = True
        field["review"] = block
        entry["value"] = field.get("value")
    meta = data.setdefault("_meta", {})
    review_meta = meta.setdefault("review", {})
    entries = review_meta.setdefault("entries", [])
    entries[:] = [e for e in entries
                  if not (e.get("template") == entry["template"]
                          and e.get("field") == entry["field"]
                          and e.get("reason") == entry["reason"]
                          and e.get("entry", "") == entry["entry"])]
    entries.append(entry)
    review_meta["confirmed"] = sum(1 for e in entries if e.get("status") == "confirmed")
    review_meta["issues"] = sum(1 for e in entries if e.get("status") == "issue")
    _atomic_write_json(path, data)
    where = f"{entry['template']}.{name}" if name else entry["reason"]
    return (f"{status}: {path.name} ({where})"
            + (" — value corrected (original kept)" if corrected else ""))


def apply_reviews(run_dir: Path, stem: str, review: dict | None = None) -> int:
    """Re-apply every review.json entry for `stem` to its JSON (after retries).

    Extraction retries/pinned runs overwrite <stem>.json; the review outcomes
    are mirrored back so a confirmed correction survives re-extraction.
    """
    review = review if review is not None else load_review(run_dir)
    n = 0
    for key, state in review.items():
        status = state.get("status")
        if status not in ("confirmed", "issue"):
            continue
        # D57 key: file|entry|template|field|reason|detail. Legacy: 5 parts
        # (no entry). A legacy detail containing "|" stays recognizable by its
        # second segment not being an entry index.
        parts = key.split("|", 5)
        if len(parts) == 6 and (parts[1] == "" or parts[1].isdigit()):
            stem_part, entry_part, template, field, reason = parts[:5]
            detail = parts[5]
        else:
            parts = key.split("|", 4)
            if len(parts) < 5:
                continue
            stem_part, template, field, reason, detail = parts
            entry_part = ""
        if stem_part != stem:
            continue
        row = {"file": stem, "template": template or None, "field": field,
               "reason": reason, "detail": detail,
               "entry": entry_index(entry_part)}
        apply_review(run_dir, row, status, state.get("note") or "",
                     value=state.get("value") if status == "confirmed" else None)
        n += 1
    return n


# ---------------------------------------------------------------------------
# evidence plumbing
# ---------------------------------------------------------------------------

def find_field(data: dict, name: str, template: str | None = None,
               entry: int | None = None) -> dict | None:
    """The header field `name` in an extraction — top-level (single-document)
    or inside a document entry (multi-document runs, 2026-09-05); `template`
    disambiguates which entry when the same field exists on several templates,
    and `entry` (D57) pins the exact documents[] occurrence the flag came from.

    Single-document flags ALSO carry a `template` column (extract.py sets it on
    every row), so a template with no matching entry falls through to the
    top-level fields — otherwise the evidence pane would find nothing.
    """
    docs = data.get("documents", [])
    if entry is not None and 0 <= entry < len(docs):
        d = docs[entry]
        if template is None or d.get("template_id") == template:
            return next((f for f in d.get("header_fields", [])
                         if f.get("name") == name), None)
    if template:
        for d in docs:
            if d.get("template_id") != template:
                continue
            for f in d.get("header_fields", []):
                if f.get("name") == name:
                    return f
            return None  # the entry exists; the field is genuinely absent
    for f in data.get("header_fields", []):
        if f.get("name") == name:
            return f
    for d in docs:
        for f in d.get("header_fields", []):
            if f.get("name") == name:
                return f
    return None


def find_line_item(data: dict, name: str, template: str | None = None,
                   entry: int | None = None) -> dict | None:
    """Pseudo-field for a `line_item_<n>` flag (D48 Phase B): value = the
    template's total-role cell, region = the row's own source_region, so the
    Review evidence pane can show the row. `entry` (D57) pins the occurrence."""
    if not name or not name.startswith("line_item_"):
        return None
    try:
        idx = int(name.rsplit("_", 1)[-1])
    except ValueError:
        return None
    import config

    docs = data.get("documents", [])
    entries = []
    if entry is not None and 0 <= entry < len(docs) \
            and (template is None or docs[entry].get("template_id") == template):
        entries = [docs[entry]]
    if not entries and template:
        entries = [d for d in docs if d.get("template_id") == template]
    if not entries:
        entries = docs or [data]
    for entry in entries:
        items = entry.get("line_items") or []
        if not (0 < idx <= len(items)):
            continue
        row = items[idx - 1]
        doc_id = entry.get("template_id") or (data.get("_meta") or {}).get("document") or ""
        cols = config.line_item_columns(config.load_documents(), doc_id) \
            if doc_id else list(config.DEFAULT_LINE_ITEM_COLUMNS)
        amount_name = next((c["name"] for c in cols if c.get("role") == "amount"), "amount")
        return {"name": name, "_kind": "amount", "value": row.get(amount_name),
                "confidence": row.get("confidence"), "source": row.get("source"),
                "source_region": row.get("source_region"), "_template": doc_id}
    return None


def grounding_match(field: dict) -> dict | None:
    """The persisted span-level grounding match for a field (D36 audit), if any.

    Stored in `verification.checks[grounding].match` by the extraction run:
    {page, fragment_bbox, line_bbox, text, band, lineno} — top-left normalized.
    """
    for c in (field.get("verification") or {}).get("checks") or []:
        if c.get("check") == "grounding" and c.get("match"):
            return c["match"]
    return None


def locate_field(run_dir: Path, stem: str, field: dict,
                 doc_id: str | None = None) -> dict | None:
    """Best text-channel location for a field's value (display-only, D36).

    Prefers the persisted grounding match (runs after 2026-09-22); otherwise
    searches EVERY page via `grounding.locate_value` — so existing runs get
    corrected boxes without re-extraction. Returns the top candidate
    {page, level, fragment_bbox, line_bbox, text, value_offset, source,
    page_mismatch} or None when the text channel cannot locate the value.
    """
    import config
    import grounding
    from validate import value_type_for

    value = field.get("value")
    if value in (None, ""):
        return None
    cfg = config.load_all()
    doc_id = doc_id or field.get("_template") or ""
    if not doc_id:
        doc_id = cfg["documents"]["documents"][0]["id"]
    profile = next((d for d in cfg["documents"]["documents"] if d["id"] == doc_id), {})
    field_def = next((f for f in (profile.get("extraction") or {}).get("fields", [])
                      if f.get("name") == field.get("name")), {})
    kind = field.get("_kind") or grounding.KIND_BY_TYPE.get(
        value_type_for(cfg, doc_id, field.get("name")), "code")
    claim = field.get("source_region") if isinstance(field.get("source_region"), dict) else None

    match = grounding_match(field)
    if match and isinstance(match.get("fragment_bbox"), list):
        text = match.get("text") or ""
        return {
            "page": int(match.get("page") or 1), "level": "exact",
            "fragment_bbox": match["fragment_bbox"], "line_bbox": match.get("line_bbox"),
            "text": text, "lineno": match.get("lineno"),
            "value_offset": grounding._value_offset(value, text),
            "source": "audit",
            "page_mismatch": bool(claim and int(claim.get("page") or 1)
                                  != int(match.get("page") or 1)),
        }

    hits = grounding.locate_value(run_dir / "raw", stem, value, kind,
                                  claim=claim, labels=field_def.get("labels") or [])
    if not hits:
        return None
    top = dict(hits[0])
    top["source"] = "locate"
    top["page_mismatch"] = bool(claim and int(claim.get("page") or 1)
                                != int(top.get("page") or 1))
    return top


def page_count(raw_dir: Path, stem: str) -> int:
    report = raw_dir / stem / "report.json"
    if report.exists():
        try:
            return int(json.loads(report.read_text(encoding="utf-8")).get("pages", 0))
        except (json.JSONDecodeError, ValueError):
            pass
    return len(list((raw_dir / stem).glob("page[0-9][0-9][0-9].render-ocr-input.png")))

# ---------------------------------------------------------------------------
# document templates (2026-09-02): create/edit profiles in the UI
# ---------------------------------------------------------------------------
# rules/documents.yaml is hot-loaded (D10) — saving here takes effect on the
# next run with zero pipeline changes. Guided-form edits serialize through
# validate_documents() — the pipeline's OWN config validator (config.py), so a
# template that saves is guaranteed to load in the pipeline. Writes are atomic.

DOCUMENTS_PATH = REPO_ROOT / "rules" / "documents.yaml"

# Reconstructed file header (guided-mode saves re-serialize the YAML without
# comments; this keeps the file self-documenting).
DOCUMENTS_HEADER = (
    "# Document identification profiles (architecture.md D25/D28/D29) — data, not code (D10).\n"
    "# Each profile = identification + extraction contract for ONE document kind.\n"
    "# The pipeline extracts from the profile the user picks (extract.py --document,\n"
    "# UI picker; hot-loaded every run). Edit in the UI (Templates tab) or here.\n"
    "#\n"
    "# IDENTIFICATION (D29, vision-led): the vision model reads the document's OWN\n"
    "# name/heading; `keywords` are USER-DEFINED helper words; the pin is the ultimate\n"
    "# identification. EXTRACTION (D27/D28): fields = {name, type, description,\n"
    "# threshold?, labels?}; types str | date | amount | name | object.\n"
)


def documents_text() -> str:
    """The CURRENT documents profile file, read fresh each call (D10)."""
    import config
    return config.DOCUMENTS_PATH.read_text(encoding="utf-8")


def documents_to_yaml(docs: list[dict]) -> str:
    """Serialize a profile list back to YAML (with the canonical header)."""
    import yaml
    body = yaml.safe_dump({"documents": docs}, sort_keys=False,
                          allow_unicode=True, width=100, default_flow_style=False)
    return DOCUMENTS_HEADER + body


def workflow_references(path: Path | None = None) -> dict[str, list[str]]:
    """profile id -> workflow ids referencing it (documents/expected/
    cross_checks), read straight from workflows.yaml.

    Used to validate a CANDIDATE documents.yaml BEFORE writing: deleting or
    renaming a template that a workflow references would make load_all() raise
    on the next load and crash the app (2026-09-28 incident). Tolerant: {}
    when the file is missing/unparseable.
    """
    import yaml
    if path is None:
        import config
        path = config.WORKFLOWS_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    refs: dict[str, list[str]] = {}
    for w in data.get("workflows") or []:
        if not isinstance(w, dict):
            continue
        wid = str(w.get("id") or "?")
        ids = set(w.get("documents") or []) | set(w.get("expected") or [])
        for xc in w.get("cross_checks") or []:
            if not isinstance(xc, dict):
                continue
            for side in ("left", "right"):
                t = (xc.get(side) or {}).get("template")
                if t:
                    ids.add(t)
        for i in ids:
            refs.setdefault(i, [])
            if wid not in refs[i]:
                refs[i].append(wid)
    return refs


def validate_documents(text: str) -> tuple[bool, str, list[dict] | None]:
    """Validate candidate YAML with the pipeline's real contract validator.

    Writes to a temp file so config.load_documents() (which validates the
    whole rule set on the real path) is exercised exactly as the pipeline
    would. Also checks that no workflow would be left referencing a missing
    template (the cross-file contract load_all() enforces). Returns
    (ok, error_or_okmsg, docs_or_None).
    """
    import tempfile
    import config
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            tmp = fh.name
        try:
            docs = config.load_documents(Path(tmp))["documents"]
        finally:
            Path(tmp).unlink(missing_ok=True)
    except config.ConfigError as exc:
        return False, str(exc), None
    ids = {d["id"] for d in docs}
    broken = {i: wfs for i, wfs in workflow_references().items() if i not in ids}
    if broken:
        # D52: warn, never refuse — the app must survive template deletion.
        # The workflow keeps the reference, skips the missing member at run
        # time and flags it if it is in `expected`.
        parts = "; ".join(f"'{i}' (used by {', '.join(w)})"
                          for i, w in sorted(broken.items()))
        msg = ("warning: workflows still reference missing template(s): " + parts
               + " — they will be skipped when running and flagged if expected")
        return True, msg, docs
    return True, f"profiles validate ({len(docs)} profile(s))", docs


def save_documents(text: str) -> Path:
    """Validate + atomically write rules/documents.yaml. Raises on invalid."""
    import os
    import config
    ok, msg, _ = validate_documents(text)
    if not ok:
        raise ValueError(f"refusing to save invalid documents.yaml: {msg}")
    target = config.DOCUMENTS_PATH
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


def duplicate_profile(docs: list[dict], doc_id: str, seen_ids: set[str] | None = None) -> dict:
    """A deep copy of a profile under a new unique id (name-based)."""
    import copy
    src = next((d for d in docs if d.get("id") == doc_id), None)
    if src is None:
        raise ValueError(f"profile {doc_id!r} not found")
    clone = copy.deepcopy(src)
    base = f"{doc_id}_copy"
    new_id = base
    seen = seen_ids or {d.get("id") for d in docs}
    n = 2
    while new_id in seen:
        new_id = f"{base}_{n}"
        n += 1
    clone["id"] = new_id
    clone["label"] = f"{src.get('label', doc_id)} (copy)"
    return clone


# ---------------------------------------------------------------------------
# bulk workflows + unique ingest (2026-09-05)
# ---------------------------------------------------------------------------

WORKFLOWS_PATH = REPO_ROOT / "rules" / "workflows.yaml"

WORKFLOWS_HEADER = (
    "# Bulk-run workflows (2026-09-05) - named document bundles, data not code (D10).\n"
    "# `documents` = profiles extracted per file (one call, order = prompt order);\n"
    "# `expected` = completeness contract (may exceed documents - extract 3, expect 4).\n"
    "# D52: an id that no longer exists in rules/documents.yaml is a WARNING (the\n"
    "# member is skipped at run time; expected members are flagged missing) - it\n"
    "# never blocks the app. Edit in the UI (Workflows tab) or here.\n"
)


def workflows_text() -> str:
    import config
    return config.WORKFLOWS_PATH.read_text(encoding="utf-8")


def workflows_to_yaml(workflows: list[dict]) -> str:
    import yaml
    body = yaml.safe_dump({"workflows": workflows}, sort_keys=False,
                          allow_unicode=True, width=100, default_flow_style=False)
    return WORKFLOWS_HEADER + body


def validate_workflows(text: str) -> tuple[bool, str, list[dict] | None]:
    import tempfile
    import config
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            tmp = fh.name
        try:
            res = config.load_workflows(Path(tmp))
        finally:
            Path(tmp).unlink(missing_ok=True)
    except config.ConfigError as exc:
        return False, str(exc), None
    msg = f"workflows validate ({len(res['workflows'])} workflow(s))"
    if res["warnings"]:
        import messages  # noqa: PLC0415 - core message catalog

        msg = "warning: " + "; ".join(messages.format_warning(w)
                                      for w in res["warnings"])
    return True, msg, res["workflows"]


def save_workflows(text: str) -> Path:
    import os
    import config
    ok, msg, _ = validate_workflows(text)
    if not ok:
        raise ValueError(f"refusing to save invalid workflows.yaml: {msg}")
    target = config.WORKFLOWS_PATH
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


def workflow_options() -> list[str]:
    import config
    return [w["id"] for w in config.load_workflows()["workflows"]]


def workflow_label(wid: str) -> str:
    import config
    for w in config.load_workflows()["workflows"]:
        if w["id"] == wid:
            return w.get("label", wid)
    return wid


# ---------------------------------------------------------------------------
# Excel export profiles (D54) — rules/exports.yaml (guided editor helpers)
# ---------------------------------------------------------------------------

EXPORTS_PATH = REPO_ROOT / "rules" / "exports.yaml"

EXPORTS_HEADER = (
    "# Excel export profiles (D54) - data, not code (D10). Edit in the UI\n"
    "# (Export tab) or here. Column sources: {meta}, {template, field},\n"
    "# {line_item}, {cross_check: rule_id}; grain: file | line_item.\n"
)


def exports_text() -> str:
    import config
    return config.EXPORTS_PATH.read_text(encoding="utf-8")


def exports_to_yaml(exports: list[dict]) -> str:
    import yaml
    body = yaml.safe_dump({"exports": exports}, sort_keys=False,
                          allow_unicode=True, width=100, default_flow_style=False)
    return EXPORTS_HEADER + body


def validate_exports(text: str) -> tuple[bool, str, list[dict] | None]:
    import tempfile
    import config
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            tmp = fh.name
        try:
            res = config.load_exports(Path(tmp))
        finally:
            Path(tmp).unlink(missing_ok=True)
    except config.ConfigError as exc:
        return False, str(exc), None
    msg = f"exports validate ({len(res['exports'])} profile(s))"
    if res["warnings"]:
        import messages  # noqa: PLC0415 - core message catalog

        msg = "warning: " + "; ".join(messages.format_warning(w)
                                      for w in res["warnings"])
    return True, msg, res["exports"]


def save_exports(text: str) -> Path:
    import os
    import config
    ok, msg, _ = validate_exports(text)
    if not ok:
        raise ValueError(f"refusing to save invalid exports.yaml: {msg}")
    target = config.EXPORTS_PATH
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


def export_options() -> list[str]:
    import config
    return [e["id"] for e in config.load_exports()["exports"]]


def export_label(eid: str) -> str:
    import config
    for e in config.load_exports()["exports"]:
        if e["id"] == eid:
            return e.get("label", eid)
    return eid


def document_fields() -> dict[str, list[str]]:
    """profile id -> its extraction field names (cross-check rule builder)."""
    import config
    return {d["id"]: [f["name"] for f in d.get("extraction", {}).get("fields", [])]
            for d in config.load_documents()["documents"]}


# --- unique ingest: hash-stemmed uploads + run manifest (no overwrite) ---

def save_uploads(files) -> list[dict]:
    """Write uploaded PDFs to inbox/ WITHOUT silent overwrites (2026-09-05).

    Stems are content-hash + sequence; original names are preserved in the
    returned manifest entries (the UI writes these to <run>/manifest.json so
    provenance survives capture). Re-uploading identical content gets an
    `-x`/`-x2`/... stem suffix instead of overwriting.
    Returns [{stem, original, sha}].
    """
    import hashlib
    INBOX.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    for i, f in enumerate(files):
        data = f.getbuffer()
        sha = hashlib.sha1(data).hexdigest()[:8]
        stem = f"{sha}-{i + 1:02d}"
        dest = INBOX / f"{stem}.pdf"
        n = 1
        while dest.exists():
            stem = f"{sha}-{i + 1:02d}-x" if n == 1 else f"{sha}-{i + 1:02d}-x{n}"
            dest = INBOX / f"{stem}.pdf"
            n += 1
        dest.write_bytes(data)
        manifest.append({"stem": stem, "original": f.name, "sha": sha})
    return manifest


def write_manifest(run_dir: Path, entries: list[dict]) -> Path:
    """<run>/manifest.json: stems -> {original, sha} (provenance, pre-capture)."""
    dest = run_dir / "manifest.json"
    dest.write_text(json.dumps({"files": entries}, indent=2), encoding="utf-8")
    return dest


def resolve_stem(run_dir: Path, stem: str) -> str:
    """Original filename for a hash-stem (manifest-backed), else the stem."""
    try:
        meta = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        for e in meta.get("files", []):
            if e.get("stem") == stem:
                return str(e.get("original", stem))
    except (OSError, json.JSONDecodeError):
        pass
    return stem


def render_workflow_summary(run_dir: Path) -> list[dict]:
    """Per-file completeness rows for the Workflows tab:
    original | found templates | missing expected | tokens | status."""
    json_dir, raw = run_dir / "json", run_dir / "raw"
    import config
    cfg = config.load_all()
    workflows = {w["id"]: w for w in cfg.get("workflows", {}).get("workflows", [])}
    out: list[dict] = []
    for stem in sorted(d.name for d in raw.iterdir() if d.is_dir()):
        jp = json_dir / f"{stem}.json"
        data = {}
        if jp.exists():
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        status = data.get("status", "no-json")
        wf_id = ((data.get("_meta") or {}).get("workflow") or {}).get("id")
        wf = workflows.get(wf_id) or {}
        docs = data.get("documents") or []
        found = [d.get("template_id") for d in docs if d.get("found")]
        expected = wf.get("expected") or []
        missing = [e for e in expected if e not in found] if expected else []
        tokens = int(((data.get("_meta") or {}).get("usage") or {}).get("total_tokens") or 0)
        cross = (data.get("_meta") or {}).get("verification", {}).get("cross_checks") or {}
        ok_x = sum(1 for r in cross.values()
                   if r.get("evaluable") and r.get("passed") is True)
        bad_x = sum(1 for r in cross.values()
                    if r.get("evaluable") and r.get("passed") is False)
        out.append({
            "file": resolve_stem(run_dir, stem),
            "found": ", ".join(found) or "—",
            "missing_expected": ", ".join(missing) or "—",
            "cross_document": f"{ok_x} ok, {bad_x} mismatch" if (ok_x or bad_x) else "—",
            "status": status,
            "tokens": tokens,
        })
    return out
