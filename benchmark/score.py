"""Score benchmark runs + freeze truth (benchmark/README.md protocol).

  python benchmark/score.py --freeze              # immutable truth manifest
  python benchmark/score.py                       # score latest runs
  python benchmark/score.py --report              # + markdown report file
  python benchmark/score.py --verdandi-run <dir> --acu-run <dir>

Metrics: per-field normalized accuracy (with per-field denominators), line
item row matching P/R/F1 + cell accuracy, per-stratum breakdowns, and the
cost/token summaries recorded by each runner.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from compare import norm_amount, norm_date  # noqa: E402

PDFS = HERE / "data" / "pdfs"
TRUTH_DIRS = (HERE / "data" / "truth", HERE / "synthetic" / "truth")
RESULTS = HERE / "data" / "results"
FROZEN = HERE / "frozen" / "manifest.json"

FIELDS = ("invoice_number", "invoice_date", "vendor_name", "buyer_name",
          "currency", "subtotal", "tax_total", "total_amount")
_NAME_FIELDS = ("vendor_name", "buyer_name")
_AMOUNT_FIELDS = ("subtotal", "tax_total", "total_amount")
_ITEM_COLUMNS = ("description", "quantity", "unit_price", "amount")
# cell comparison reuses the field semantics: money cells compare as amounts,
# quantity numerically ("1.00" == "1"), description as text.
_CELL_FIELDS = {"description": "description", "quantity": "subtotal",
                "unit_price": "subtotal", "amount": "subtotal"}

# Currency symbols/codes that denote the same ISO currency in scoring.
_CURRENCY_ALIASES = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR",
                     "eur": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR",
                     "rs": "INR", "rs.": "INR", "inr": "INR", "idr": "IDR",
                     "bf": "BEF", "bef": "BEF"}


def _norm_key(value) -> str:
    """Casefold + keep unicode letters/digits (Devanagari, accents…)."""
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


# Scoring-only date normalization: the corpus prints month-first English dates
# (kaggle scans) that Verdandi's day-first normalizer deliberately rejects
# (D58). The benchmark must still score them, identically for both systems.
_SCORE_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
                       "%m/%d/%Y", "%m/%d/%y", "%d.%b.%Y", "%d.%b.%y",
                       "%d-%B-%y", "%d %B %y")


def _score_date(value) -> str | None:
    import datetime as _dt
    normalized = norm_date(value)
    if normalized is not None:
        return normalized
    if not value:
        return None
    text = str(value).strip()
    for fmt in _SCORE_DATE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm_currency(value) -> str | None:
    """ISO code or symbol -> ISO code ("$" == "USD")."""
    raw = str(value).strip().casefold()
    if raw in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[raw]
    return _CURRENCY_ALIASES.get(_norm_key(value))


def _field_correct(name: str, truth, pred) -> bool:
    if truth in (None, "") or pred in (None, ""):
        return False
    if name in _NAME_FIELDS:
        t, p = _norm_key(truth), _norm_key(pred)
        return bool(t) and bool(p) and (t in p or p in t)
    if name in _AMOUNT_FIELDS:
        a, b = norm_amount(truth), norm_amount(pred)
        return a is not None and b is not None and abs(a - b) <= 0.01
    if name in ("invoice_date",):
        t = _score_date(truth)
        return t is not None and t == _score_date(pred)
    if name == "currency":
        return _norm_currency(truth) is not None and _norm_currency(truth) == _norm_currency(pred)
    return _norm_key(truth) == _norm_key(pred)


def _truth_files() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for directory in TRUTH_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            found[path.stem] = {"truth": json.loads(path.read_text()), "path": path}
    return found


def _verdandi_predictions(run_dir: Path) -> dict[str, dict]:
    preds: dict[str, dict] = {}
    for path in sorted((run_dir / "json").glob("*.json")):
        data = json.loads(path.read_text())
        fields = {item.get("name"): item.get("value")
                  for item in (data.get("header_fields") or [])
                  if isinstance(item, dict)}
        found = data.get("status") not in ("flag_no_document_match", "failed")
        preds[path.stem] = {
            "fields": fields if found else {name: None for name in FIELDS},
            "line_items": data.get("line_items") or [] if found else [],
            "found": found,
            "status": data.get("status"),
        }
    return preds


def _acu_predictions(run_dir: Path) -> dict[str, dict]:
    preds: dict[str, dict] = {}
    for path in sorted(run_dir.glob("*.json")):
        data = json.loads(path.read_text())
        preds[path.stem] = {
            "fields": data.get("fields") or {},
            "line_items": data.get("line_items") or [],
            "found": bool(data.get("found", True)),
            "usage": data.get("usage"),
            "status": data.get("status"),
        }
    return preds


def _item_amount(row: dict):
    return norm_amount((row or {}).get("amount"))


def _match_items(truth_rows: list[dict], pred_rows: list[dict]):
    """Greedy match on amount, then description similarity."""
    used: set[int] = set()
    matches: list[tuple[dict, dict]] = []
    unmatched_truth: list[dict] = []
    for trow in truth_rows:
        t_amount = _item_amount(trow)
        best = None
        best_score = -1.0
        for i, prow in enumerate(pred_rows):
            if i in used:
                continue
            p_amount = _item_amount(prow)
            if t_amount is None or p_amount is None or abs(t_amount - p_amount) > 0.01:
                continue
            t_desc, p_desc = _norm_key(trow.get("description")), _norm_key(prow.get("description"))
            score = 1.0 if t_desc and t_desc == p_desc else (
                0.5 if t_desc and p_desc and (t_desc in p_desc or p_desc in t_desc) else 0.0)
            if score > best_score:
                best, best_score = i, score
        if best is None:
            unmatched_truth.append(trow)
        else:
            used.add(best)
            matches.append((trow, pred_rows[best]))
    unmatched_pred = [r for i, r in enumerate(pred_rows) if i not in used]
    return matches, unmatched_truth, unmatched_pred


def score(preds: dict[str, dict], truths: dict[str, dict]) -> dict:
    field_stats = {name: {"n": 0, "correct": 0} for name in FIELDS}
    stratum: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {name: {"n": 0, "correct": 0} for name in FIELDS})
    items = {"tp": 0, "fp": 0, "fn": 0, "cells": {c: {"n": 0, "correct": 0} for c in _ITEM_COLUMNS}}
    docs_scored = 0
    found_ok = 0
    for stem, truth in sorted(truths.items()):
        pred = preds.get(stem)
        if pred is None:
            continue
        docs_scored += 1
        if pred.get("found"):
            found_ok += 1
        axes = truth["truth"].get("axes") or {}
        key = f"{axes.get('document_type', '?')}/{axes.get('quality', '?')}"
        for name in FIELDS:
            expected = truth["truth"]["fields"].get(name)
            if expected in (None, ""):
                continue
            field_stats[name]["n"] += 1
            stratum[key][name]["n"] += 1
            if _field_correct(name, expected, pred["fields"].get(name)):
                field_stats[name]["correct"] += 1
                stratum[key][name]["correct"] += 1
        truth_rows = truth["truth"].get("line_items")
        if truth_rows is None:
            continue
        matches, unmatched_t, unmatched_p = _match_items(truth_rows, pred["line_items"])
        items["tp"] += len(matches)
        items["fn"] += len(unmatched_t)
        items["fp"] += len(unmatched_p)
        for trow, prow in matches:
            for col in _ITEM_COLUMNS:
                if trow.get(col) in (None, ""):
                    continue
                items["cells"][col]["n"] += 1
                if _field_correct(_CELL_FIELDS[col], trow.get(col), prow.get(col)):
                    items["cells"][col]["correct"] += 1

    def _pct(correct: int, n: int) -> float | None:
        return round(100.0 * correct / n, 1) if n else None

    missing = sorted(stem for stem in truths if stem not in preds
                     and (PDFS / f"{stem}.pdf").exists())
    result = {
        "documents": docs_scored,
        "missing": len(missing),
        "missing_stems": missing[:10],
        "found": found_ok,
        "fields": {name: {**stats, "accuracy": _pct(stats["correct"], stats["n"])}
                   for name, stats in field_stats.items()},
        "line_items": {
            **items,
            "precision": _pct(items["tp"], items["tp"] + items["fp"]),
            "recall": _pct(items["tp"], items["tp"] + items["fn"]),
            "f1": (round(2 * items["tp"] / (2 * items["tp"] + items["fp"] + items["fn"]), 4)
                   if items["tp"] + items["fp"] + items["fn"] else None),
            "cells": {c: {**s, "accuracy": _pct(s["correct"], s["n"])}
                      for c, s in items["cells"].items()},
        },
        "strata": {key: {name: {**stats, "accuracy": _pct(stats["correct"], stats["n"])}
                         for name, stats in cells.items()}
                   for key, cells in sorted(stratum.items())},
    }
    total_n = sum(s["n"] for s in field_stats.values())
    total_c = sum(s["correct"] for s in field_stats.values())
    result["field_macro_accuracy"] = _pct(total_c, total_n)
    return result


def _print_scores(label: str, result: dict) -> None:
    print(f"=== {label}: {result['documents']} docs scored, "
          f"{result['found']} found, {result['missing']} missing ===")
    for name, stats in result["fields"].items():
        if stats["n"]:
            print(f"  {name:20s} {stats['correct']:>3}/{stats['n']:<3} "
                  f"{stats['accuracy']:>5}%")
    print(f"  field macro accuracy: {result['field_macro_accuracy']}%")
    li = result["line_items"]
    if li["tp"] + li["fp"] + li["fn"]:
        print(f"  line items: TP={li['tp']} FP={li['fp']} FN={li['fn']} "
              f"P={li['precision']}% R={li['recall']}% F1={li['f1']}")
        for col, stats in li["cells"].items():
            if stats["n"]:
                print(f"    {col:12s} {stats['correct']:>3}/{stats['n']:<3} {stats['accuracy']:>5}%")
    for key, cells in result["strata"].items():
        n = sum(c["n"] for c in cells.values())
        c = sum(c["correct"] for c in cells.values())
        print(f"  stratum {key:22s} {c}/{n}")


def _latest_run(root: Path) -> Path | None:
    candidates = sorted(root.glob("*/*")) if root.exists() else []
    return candidates[-1] if candidates else None


def freeze(allow_draft: bool = False, force: bool = False) -> int:
    truths = _truth_files()
    if FROZEN.exists() and not force:
        print(f"freeze refused: {FROZEN} already exists (truth is frozen). "
              f"Use --force to re-freeze deliberately.", file=sys.stderr)
        return 1
    docs = []
    unverified = []
    for stem, entry in sorted(truths.items()):
        pdf = PDFS / f"{stem}.pdf"
        if not pdf.exists():
            print(f"freeze refused: missing PDF for {stem}", file=sys.stderr)
            return 1
        status = (entry["truth"].get("verification") or {}).get("status")
        if status not in ("verified", "ai-verified"):
            unverified.append(f"{stem} ({status})")
        docs.append({
            "stem": stem,
            "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "truth_sha256": hashlib.sha256(entry["path"].read_bytes()).hexdigest(),
            "truth_path": str(entry["path"].relative_to(ROOT)),
            "axes": entry["truth"].get("axes"),
            "source": (entry["truth"].get("source") or {}).get("dataset")
                      or (entry["truth"].get("source") or {}).get("kind"),
            "verification": entry["truth"].get("verification"),
        })
    if unverified and not allow_draft:
        print("freeze refused — truth must be owner-verified first:\n  "
              + "\n  ".join(unverified), file=sys.stderr)
        return 1
    manifest = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "schema_sha256": hashlib.sha256((HERE / "schema.yaml").read_bytes()).hexdigest(),
        "documents": docs,
    }
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    FROZEN.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"frozen {len(docs)} documents -> {FROZEN}"
          + (" (drafts allowed)" if unverified else ""))
    return 0


def _verdandi_cost(meta: dict) -> tuple[float | None, dict]:
    usage = meta.get("usage") or {}
    rates = meta.get("rate_card") or {}
    prompt = (usage.get("prompt_tokens") or 0) + (usage.get("recheck_prompt_tokens") or 0)
    completion = (usage.get("completion_tokens") or 0) + (usage.get("recheck_completion_tokens") or 0)
    totals = {"prompt_tokens": prompt, "completion_tokens": completion,
              "rechecks": usage.get("rechecks") or 0}
    if rates.get("input_per_1m") is None or rates.get("output_per_1m") is None:
        return None, totals
    cost = (prompt / 1e6 * rates["input_per_1m"]
            + completion / 1e6 * rates["output_per_1m"])
    return round(cost, 4), totals


def _acu_cost(meta: dict) -> tuple[float | None, dict]:
    usage = meta.get("usage_totals") or {}
    rates = meta.get("rate_card") or {}
    cached = usage.get("model_cached_input") or 0
    uncached = max((usage.get("model_input") or 0) - cached, 0)
    totals = {"pages": usage.get("documentPages") or 0,
              "contextualization_tokens": usage.get("contextualization") or 0,
              "model_input_tokens": usage.get("model_input") or 0,
              "model_cached_input_tokens": cached,
              "model_output_tokens": usage.get("model_output") or 0}
    needed = ("document_page_standard", "contextualization_per_1m",
              "model_input_per_1m", "model_output_per_1m")
    if any(rates.get(k) is None for k in needed):
        return None, totals
    cost = (totals["pages"] / 1000 * rates["document_page_standard"]
            + totals["contextualization_tokens"] / 1e6 * rates["contextualization_per_1m"]
            + uncached / 1e6 * rates["model_input_per_1m"]
            + cached / 1e6 * rates.get("model_cached_input_per_1m",
                                       rates["model_input_per_1m"])
            + totals["model_output_tokens"] / 1e6 * rates["model_output_per_1m"])
    return round(cost, 4), totals


def _cost_markdown(label: str, run_dir: Path, cost_fn) -> list[str]:
    meta_path = Path(run_dir) / "run.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text())
    cost, totals = cost_fn(meta)
    rates = meta.get("rate_card") or {}
    lines = [f"\n### {label} cost & tokens\n",
             "| usage | value | rate | cost |",
             "|---|---|---|---|"]
    if "prompt_tokens" in totals:
        lines.append(f"| prompt tokens | {totals['prompt_tokens']:,} | "
                     f"${rates.get('input_per_1m')}/1M | "
                     f"${totals['prompt_tokens'] / 1e6 * (rates.get('input_per_1m') or 0):.4f} |")
        lines.append(f"| completion tokens | {totals['completion_tokens']:,} | "
                     f"${rates.get('output_per_1m')}/1M | "
                     f"${totals['completion_tokens'] / 1e6 * (rates.get('output_per_1m') or 0):.4f} |")
    else:
        lines.append(f"| document pages | {totals['pages']} | "
                     f"${rates.get('document_page_standard')}/1k | "
                     f"${totals['pages'] / 1000 * (rates.get('document_page_standard') or 0):.4f} |")
        lines.append(f"| contextualization tokens | {totals['contextualization_tokens']:,} | "
                     f"${rates.get('contextualization_per_1m')}/1M | "
                     f"${totals['contextualization_tokens'] / 1e6 * (rates.get('contextualization_per_1m') or 0):.4f} |")
        lines.append(f"| model input tokens (uncached) | "
                     f"{(totals['model_input_tokens'] - totals['model_cached_input_tokens']):,} | "
                     f"${rates.get('model_input_per_1m')}/1M | "
                     f"${(totals['model_input_tokens'] - totals['model_cached_input_tokens']) / 1e6 * (rates.get('model_input_per_1m') or 0):.4f} |")
        lines.append(f"| model input tokens (cached) | {totals['model_cached_input_tokens']:,} | "
                     f"${rates.get('model_cached_input_per_1m', rates.get('model_input_per_1m'))}/1M | "
                     f"${totals['model_cached_input_tokens'] / 1e6 * (rates.get('model_cached_input_per_1m', rates.get('model_input_per_1m')) or 0):.4f} |")
        lines.append(f"| model output tokens | {totals['model_output_tokens']:,} | "
                     f"${rates.get('model_output_per_1m')}/1M | "
                     f"${totals['model_output_tokens'] / 1e6 * (rates.get('model_output_per_1m') or 0):.4f} |")
    lines.append(f"\n**{label} total: ${cost}** ({'rates: ' + str(rates.get('source', 'n/a')) if cost is not None else 'rate card incomplete — tokens recorded'})\n")
    return lines


def verify_frozen() -> int:
    """sha256-check every frozen PDF + truth against frozen/manifest.json.

    Run BEFORE any paid call (2026-10-05): the head-to-head is only fair if
    both systems see the exact bytes the truth was frozen against.
    """
    if not FROZEN.exists():
        print("no frozen manifest — run score.py --freeze first", file=sys.stderr)
        return 1
    manifest = json.loads(FROZEN.read_text(encoding="utf-8"))
    problems: list[str] = []
    schema_sha = hashlib.sha256((HERE / "schema.yaml").read_bytes()).hexdigest()
    if manifest.get("schema_sha256") and schema_sha != manifest["schema_sha256"]:
        problems.append("schema.yaml changed since the freeze")
    for doc in manifest.get("documents", []):
        stem = doc.get("stem", "?")
        checks = ((PDFS / f"{stem}.pdf", doc.get("pdf_sha256"), "pdf"),
                  (ROOT / str(doc.get("truth_path") or ""),
                   doc.get("truth_sha256"), "truth"))
        for path, expected, label in checks:
            if not path.exists():
                problems.append(f"{stem} {label}: missing {path}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected and digest != expected:
                problems.append(f"{stem} {label}: sha256 mismatch")
    if problems:
        print(f"FROZEN VERIFY FAILED — {len(problems)} problem(s):")
        for problem in problems[:25]:
            print("  " + problem)
        return 1
    print(f"frozen verify OK: {len(manifest.get('documents', []))} documents "
          f"+ schema match frozen/manifest.json")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark scorer + truth freeze")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--verify-frozen", action="store_true",
                    help="sha256-check every PDF + truth against the frozen manifest")
    ap.add_argument("--allow-draft", action="store_true",
                    help="freeze even with unverified truth (development only)")
    ap.add_argument("--force", action="store_true", help="re-freeze over an existing manifest")
    ap.add_argument("--report", action="store_true", help="write a markdown report")
    ap.add_argument("--verdandi-run", help="benchmark/data/results/verdandi/<...> dir")
    ap.add_argument("--acu-run", help="benchmark/data/results/acu/<...> dir")
    ap.add_argument("--subset", help="JSON file with {stems: [...]} to score")
    args = ap.parse_args(argv)
    if args.verify_frozen:
        return verify_frozen()
    if args.freeze:
        return freeze(args.allow_draft, args.force)

    truths = _truth_files()
    if args.subset:
        wanted = json.loads(Path(args.subset).read_text())
        wanted = set(wanted["stems"] if isinstance(wanted, dict) else wanted)
        truths = {k: v for k, v in truths.items() if k in wanted}
    missing = [stem for stem in truths if not (PDFS / f"{stem}.pdf").exists()]
    if missing:
        print(f"warning: no PDF for {len(missing)} truth doc(s): "
              f"{', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}",
              file=sys.stderr)

    report: dict = {"created_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "truth_documents": len(truths)}
    v_run = Path(args.verdandi_run) if args.verdandi_run else _latest_run(RESULTS / "verdandi")
    if v_run and (v_run / "json").exists():
        result = score(_verdandi_predictions(v_run), truths)
        _print_scores(f"verdandi {v_run.name}", result)
        report["verdandi"] = {"run": str(v_run), "scores": result,
                              "run_meta": json.loads((v_run / "run.json").read_text())
                              if (v_run / "run.json").exists() else None}
    a_run = Path(args.acu_run) if args.acu_run else _latest_run(RESULTS / "acu")
    if a_run and a_run.exists():
        result = score(_acu_predictions(a_run), truths)
        _print_scores(f"acu {a_run.name}", result)
        report["acu"] = {"run": str(a_run), "scores": result}

    out_dir = RESULTS / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"score-{stamp}.json").write_text(json.dumps(report, indent=2) + "\n",
                                                 encoding="utf-8")
    if args.report:
        lines = [f"# Benchmark score — {report['created_at']}",
                 f"\ntruth documents: {len(truths)}\n"]
        for key in ("verdandi", "acu"):
            if key not in report:
                continue
            scores = report[key]["scores"]
            lines.append(f"\n## {key} ({report[key]['run']})\n")
            lines.append("| field | correct | scored | accuracy |")
            lines.append("|---|---|---|---|")
            for name, stats in scores["fields"].items():
                if stats["n"]:
                    lines.append(f"| {name} | {stats['correct']} | {stats['n']} | {stats['accuracy']}% |")
            lines.append(f"| **macro** | | | **{scores['field_macro_accuracy']}%** |")
            li = scores["line_items"]
            lines.append(f"\nline items: TP={li['tp']} FP={li['fp']} FN={li['fn']} "
                         f"P={li['precision']} R={li['recall']} F1={li['f1']}\n")
            cost_fn = _verdandi_cost if key == "verdandi" else _acu_cost
            lines.extend(_cost_markdown(key, Path(report[key]["run"]), cost_fn))
        markdown = out_dir / f"report-{stamp}.md"
        markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report: {markdown}")
    else:
        print(f"score: {out_dir / f'score-{stamp}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
