"""Latency + payload benchmark for the extraction call (D42).

One paid vision call per run via `extract.extract_file` — no verification, no
gated rechecks — so it measures the raw gateway round-trip. Use it to compare
models, transports, render formats (PNG vs JPEG) and vision caps:

  .venv/bin/python bench.py --stem <file> --runs 3
  .venv/bin/python bench.py --stem <file> --models gpt-5.6-luna,qwen3.8-flash
  make bench ARGS="--stem <file> --models gpt-5.6-luna,qwen3.8-flash"

Default raw dir = the canonical cap-1536 corpus capture (stable, embedded
geometry). Prints wall time, the HTTP elapsed recorded by llm_client, token
splits and effective tok/s per run plus the median per model. Nothing is
written into runs/.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import config
import extract

DEFAULT_RAW = Path("out/runs/2026-09-03/5/raw")


def _payload(raw_dir: Path, stem: str) -> tuple[int, float]:
    """(pages, request payload MB) for the vision input the model would see."""
    renders = extract.page_renders(raw_dir / stem)
    b64_chars = sum(len(b) for b, _ in renders)
    return len(renders), (b64_chars * 3 / 4) / 1e6  # base64 -> bytes -> MB


def _run_once(raw_dir: Path, stem: str, model: str, max_tokens: int,
              timeout_s: float) -> tuple[float, dict]:
    started = time.perf_counter()
    data = extract.extract_file(stem, raw_dir, model=model,
                                max_tokens=max_tokens, timeout_s=timeout_s)
    wall = time.perf_counter() - started
    usage = (data.get("_meta") or {}).get("usage") or {}
    return wall, usage


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extraction latency benchmark (D42)")
    ap.add_argument("--stem", required=True, help="raw dir name under --raw")
    ap.add_argument("--raw", default=str(DEFAULT_RAW),
                    help=f"raw capture dir (default: {DEFAULT_RAW})")
    ap.add_argument("--model", help="single model (default: primary_model)")
    ap.add_argument("--models", help="comma-separated models to compare")
    ap.add_argument("--runs", type=int, default=3, help="runs per model (default 3)")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)

    raw_dir = Path(args.raw)
    if not (raw_dir / args.stem).is_dir():
        print(f"no raw dir {raw_dir / args.stem}", file=sys.stderr)
        return 1
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()]
    if not models:
        models = [args.model or config.load_models()["primary_model"]]

    pages, mb = _payload(raw_dir, args.stem)
    print(f"stem={args.stem}  pages={pages}  payload={mb:.2f}MB  raw={raw_dir}")
    for model in models:
        walls: list[float] = []
        for i in range(max(args.runs, 1)):
            try:
                wall, usage = _run_once(raw_dir, args.stem, model,
                                        args.max_tokens, args.timeout)
            except Exception as exc:  # a failed model must not kill the comparison
                print(f"  {model} run {i + 1}: FAILED — {exc}")
                continue
            walls.append(wall)
            total = usage.get("total_tokens") or 0
            print(f"  {model} run {i + 1}: {wall:.1f}s wall · "
                  f"{usage.get('elapsed_s', '?')}s http · "
                  f"{usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} tokens · "
                  f"{total / max(wall, 0.001):.0f} tok/s")
        if walls:
            print(f"  {model}: median {statistics.median(walls):.1f}s "
                  f"over {len(walls)} run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
