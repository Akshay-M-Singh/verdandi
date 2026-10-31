"""Run the real Verdandi capture + extraction over the frozen benchmark PDFs.

  python benchmark/run_verdandi.py                 # capture only (free)
  python benchmark/run_verdandi.py --paid          # + extraction (PAID)

Isolation: this process points config.DOCUMENTS_PATH at
benchmark/rules_documents.yaml before calling the pipeline — the shipped
rules/documents.yaml is never read or written. Results land under
benchmark/data/results/verdandi/<date>/<n>/ (a fresh run dir every time;
nothing is overwritten). One vision call per file + capped narrow rechecks.
"""

from __future__ import annotations

import argparse
import shutil
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import extract  # noqa: E402

PDFS = HERE / "data" / "pdfs"
RESULTS = HERE / "data" / "results" / "verdandi"
PROFILE = HERE / "rules_documents.yaml"
MODEL_NOTE_TOKENS_PER_PAGE = 2200  # measured envelope (D63 evidence, cap 1536)


def _new_run_dir() -> Path:
    base = RESULTS / dt.date.today().isoformat()
    n = 1
    while (base / str(n)).exists():
        n += 1
    run = base / str(n)
    run.mkdir(parents=True)
    return run


def _capture(raw: Path, stems: list[str] | None = None,
             vision_cap: int = 1536) -> None:
    """Capture the benchmark PDFs (or a subset).

    `vision_cap=0` skips the downscaled vision render so the extractor sends
    FULL-RESOLUTION page images (the raw photo / full render fallbacks).
    """
    cmd = [sys.executable, str(ROOT / "text_layer.py"), str(PDFS), str(raw)]
    if vision_cap > 0:
        cmd += ["--vision-cap", str(vision_cap)]
    if stems:
        cmd += ["--files", ",".join(stems)]
    subprocess.run(cmd, check=True)


def _promote_full_res(raw: Path) -> int:
    """Benchmark full-resolution mode: copy each page's full-resolution render
    (`render-ocr-input.png`) over the vision slot so the extractor sends IT to
    the model instead of a downscaled copy. Idempotent."""
    promoted = 0
    for page in sorted(raw.glob("*/page*.render-ocr-input.png")):
        target = page.with_name(
            page.name.replace("render-ocr-input", "render-vision"))
        if not target.exists():
            shutil.copy2(page, target)
            promoted += 1
    return promoted


def _retry_failed(run: Path, args) -> int:
    """Re-extract only the files whose JSON says status == failed.

    Transient gateway/network failures must not silently shrink a benchmark
    run; the retried stems are recorded in run.json and the usage totals are
    recomputed so the cost record stays complete.
    """
    json_dir = run / "json"
    failed = sorted(
        p.stem for p in json_dir.glob("*.json")
        if json.loads(p.read_text(encoding="utf-8")).get("status") == "failed")
    if not failed:
        print("nothing to retry — no failed files")
        return 0
    print(f"retrying {len(failed)} failed file(s): {', '.join(failed)}")
    model = args.model or config.load_models()["primary_model"]
    config.DOCUMENTS_PATH = PROFILE  # benchmark-only, this process only
    rc = extract.run_batch(run / "raw", json_dir, doc_id="benchmark_document",
                           workers=args.workers, model=model,
                           files_filter=set(failed))
    meta_path = run / "run.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(_run_stats(run / "raw", json_dir))
        meta.setdefault("retried", []).extend(failed)
        meta["retried_at"] = dt.datetime.now().isoformat(timespec="seconds")
        meta["exit_code"] = rc
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"retry: exit={rc} — usage record refreshed")
    return rc


def _subset_stems(path: str | None) -> list[str] | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    stems = data["stems"] if isinstance(data, dict) else data
    return sorted(str(s) for s in stems)


def _run_stats(raw: Path, json_dir: Path) -> dict:
    files = sorted(p.name for p in raw.iterdir() if p.is_dir())
    pages = 0
    for report in raw.glob("*/report.json"):
        pages += json.loads(report.read_text()).get("pages") or 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0,
             "recheck_prompt_tokens": 0, "recheck_completion_tokens": 0,
             "rechecks": 0}
    for path in sorted(json_dir.glob("*.json")):
        data = json.loads(path.read_text())
        meta = data.get("_meta") or {}
        call_usage = meta.get("usage") or {}
        usage["prompt_tokens"] += call_usage.get("prompt_tokens") or 0
        usage["completion_tokens"] += call_usage.get("completion_tokens") or 0
        recheck = (meta.get("verification") or {}).get("recheck") or {}
        recheck_usage = recheck.get("usage") or {}
        if recheck_usage:
            usage["rechecks"] += 1
        usage["recheck_prompt_tokens"] += recheck_usage.get("prompt_tokens") or 0
        usage["recheck_completion_tokens"] += recheck_usage.get("completion_tokens") or 0
    return {"files": len(files), "pages": pages, "usage": usage}


def _rate_card(model: str) -> dict:
    """models.dev hint rates for the run model (GO subscription quota proxy).

    Recorded in run.json so scoring never needs an external lookup — the same
    basis the D65 reports used.
    """
    try:
        import model_catalog

        dev = model_catalog.fetch_models_dev(
            endpoint=config.load_models()["endpoint"])
        rec = (dev.get("models") or {}).get(model) or {}
        ci, co = rec.get("cost_input"), rec.get("cost_output")
        if isinstance(ci, (int, float)) and isinstance(co, (int, float)):
            return {
                "input_per_1m": ci, "output_per_1m": co,
                "source": f"models.dev hint rates for {model} "
                          f"(GO subscription quota proxy)",
            }
    except Exception:  # noqa: BLE001 — a missing hint must not fail a run
        pass
    return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark: Verdandi capture + extraction")
    ap.add_argument("--paid", action="store_true",
                    help="actually run the paid extraction (after the cost statement)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--model", default=None, help="override primary_model")
    ap.add_argument("--run", help="reuse an existing benchmark run dir (skip capture)")
    ap.add_argument("--subset", help="JSON file with {stems: [...]} to run")
    ap.add_argument("--vision-cap", type=int, default=1536,
                    help="page-image long edge sent to the model (0 = no downscaled copy)")
    ap.add_argument("--full-res", action="store_true",
                    help="send the full-resolution page render to the model")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-extract only files whose JSON status is 'failed' "
                         "and refresh the run's usage record")
    args = ap.parse_args(argv)

    stems = _subset_stems(args.subset)
    run = Path(args.run) if args.run else _new_run_dir()
    if args.retry_failed:
        return _retry_failed(run, args)
    raw, json_dir = run / "raw", run / "json"
    raw.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    if not any(raw.iterdir()):
        scope = f"{len(stems)} selected file(s)" if stems else str(PDFS)
        print(f"capture: {scope} -> {raw} (local, free; "
              f"vision-cap {'off' if args.vision_cap == 0 else args.vision_cap})")
        _capture(raw, stems, args.vision_cap)
    if args.full_res:
        promoted = _promote_full_res(raw)
        print(f"full-res mode: {promoted} page render(s) promoted to the "
              f"vision input (idempotent)")

    stats = _run_stats(raw, json_dir)
    model = args.model or config.load_models()["primary_model"]
    prompt_est = stats["pages"] * MODEL_NOTE_TOKENS_PER_PAGE
    print(f"benchmark run: {run}")
    print(f"documents: {stats['files']} files / {stats['pages']} pages")
    print(f"paid estimate: {stats['files']} primary vision call(s) + up to "
          f"{stats['files']} narrow recheck(s) ~= {prompt_est:,} prompt tokens "
          f"(heuristic {MODEL_NOTE_TOKENS_PER_PAGE}/page, cap 1536), model={model}")
    if not args.paid:
        print("dry stop — nothing was spent; pass --paid to run extraction")
        return 0

    config.DOCUMENTS_PATH = PROFILE  # benchmark-only, this process only
    started = time.time()
    rc = extract.run_batch(raw, json_dir, doc_id="benchmark_document",
                           workers=args.workers, model=model)
    stats = _run_stats(raw, json_dir)
    meta = {
        "harness": "benchmark/run_verdandi.py",
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - started, 1),
        "model": model,
        "documents_profile": str(PROFILE.relative_to(ROOT)),
        "vision_input": ("full-resolution render" if args.full_res
                         else f"vision-cap {args.vision_cap}"),
        "exit_code": rc,
        "rate_card": _rate_card(model),
        **stats,
    }
    (run / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"verdandi: exit={rc} in {meta['elapsed_s']}s — "
          f"{stats['usage']['prompt_tokens']:,}+{stats['usage']['completion_tokens']:,} "
          f"tokens, {stats['usage']['rechecks']} recheck(s), results in {json_dir}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
