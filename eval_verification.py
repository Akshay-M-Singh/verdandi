"""Verification-truth measurement — the `make eval-verify` harness.

Two measurement sections:

  FLAGS — each run's field-level flagging decisions (flags.csv) vs
  tests/verification_truth.json: TP/FN/FP/TN + precision/recall/accuracy.
  For current runs this scores the shipped stack; for archived pre-D30 runs
  it scores the retired pipeline (the historical baseline numbers).

  STACK — the verification check stack (grounding / arithmetic / format /
  aggregation; disagreement is gated and offline-absent) run over the same
  cells + the synthetic fixtures: value + source_region + line items from the
  run/fixture JSON, raw text channel from a corpus capture. The stack's
  "flagged" decision is matched against the same truth. The owner tunes
  weights/floors against it (config/thresholds.yaml `verification:`).

Truth semantics: correct = extracted == printed (normalized); should_flag =
(not correct) OR a listed source quirk (a design-flag) OR the fixture's
expect_flag override. Runs derive from value correctness only.

Usage: python eval_verification.py [--runs 2026-09-03/5]
                                     [--raw out/runs/2026-09-03/5/raw]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import aggregate
import config
import grounding
from compare import norm_amount, norm_date, norm_key
from eval import resolve_stems, run_info
from validate import check_arithmetic, check_format, profile_arithmetic_rules, value_type_for

REPO = Path(__file__).resolve().parent
TRUTH_PATH = REPO / "tests" / "verification_truth.json"
FIXTURES_DIR = REPO / "tests" / "fixtures" / "verification"
RUNS_ROOT = REPO / "out" / "runs"

# The full-corpus run with the D30 stack (luna primary) — the current
# measurement target. Archived pre-D30 runs can be scored by passing --runs.
DEFAULT_RUNS = ["2026-09-03/5"]

KIND_BY_FIELD = {
    "invoice_number": "key",
    "invoice_date": "date",
    "boe_number": "key",
    "boe_date": "date",
    "total_invoice_amount": "amount",
}

# Old fixed_v1 profile extracted "total"; the D27 broker_invoice profile
# extracts "total_invoice_amount". Map old names onto the truth schema.
FIELD_ALIASES = {"total": "total_invoice_amount"}


def _field_names(fname: str) -> list[str]:
    """All extraction-name spellings for a truth field (aliases included)."""
    return [fname] + [a for a, t in FIELD_ALIASES.items() if t == fname]


def _field_value(fields: dict, fname: str):
    """Extraction value for a truth field, honoring old-schema aliases."""
    for name in _field_names(fname):
        if fields.get(name) is not None:
            return fields[name]
    return None


def _correct(value, truth, kind: str) -> bool:
    if kind == "amount":
        a, b = norm_amount(value), norm_amount(truth)
        return a is not None and b is not None and a == b
    if kind == "date":
        a, b = norm_date(value), norm_date(truth)
        return a is not None and b is not None and a == b
    return norm_key(str(value)) == norm_key(str(truth))


def load_truth() -> dict:
    data = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    return {
        "fields": data["fields"],
        "quirk": {tuple(pair) for pair in data["quirk_should_flag"]},
        "docs": data["docs"],
    }


def read_run_flags(run_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """(stem, field) -> flag rows, excluding presentation-only reasons.

    `document_quality` is a page-gate record, `invalid_source_region` marks a
    dropped location box (D43) and `missing_value` marks a declared field with
    no value on a found document (D55) — none is a value disagreement, so they
    must not count as phantom flags in the precision metric.
    """
    flags_path = run_dir / "flags.csv"
    if not flags_path.exists():
        return {}
    flags: dict[tuple[str, str], list[dict]] = {}
    with flags_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("reason") in ("document_quality", "invalid_source_region",
                                     "missing_value"):
                continue
            flags.setdefault((row["file"], row["field"]), []).append(row)
    return flags


def read_run_values(run_dir: Path) -> dict[str, dict[str, dict]]:
    """stem -> {template_id ("" for single-document): fields dict}.

    D60: multi-document workflow runs keep each entry in `documents[]`; each
    found entry becomes one view keyed by its template_id, so truth cells can
    name the template they belong to. Single-document JSONs use the "" key.
    """
    out: dict[str, dict[str, dict]] = {}
    for jp in sorted((run_dir / "json").glob("*.json")):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        by_tpl: dict[str, dict] = {}
        if data.get("documents"):
            for entry in data["documents"]:
                if not entry.get("found"):
                    continue
                fields = {"line_items": entry.get("line_items", [])}
                for f in entry.get("header_fields", []):
                    if f.get("value") is None:
                        continue
                    fields[f["name"]] = {
                        "value": f["value"],
                        "confidence": f.get("confidence"),
                        "source_region": f.get("source_region"),
                    }
                by_tpl[entry.get("template_id") or ""] = fields
        else:
            fields = {"line_items": data.get("line_items", [])}
            for f in data.get("header_fields", []):
                if f.get("value") is None:
                    continue
                fields[f["name"]] = {
                    "value": f["value"],
                    "confidence": f.get("confidence"),
                    "source_region": f.get("source_region"),
                }
            by_tpl[""] = fields
        if by_tpl:
            out[jp.stem] = by_tpl
    return out


def _view_fields(by_tpl: dict[str, dict], fmeta: dict) -> dict | None:
    """The fields view a truth cell scores against (D60).

    A truth cell with `template` pins the entry; otherwise the top-level ("")
    view, falling back to the first entry (legacy single-template truth).
    """
    tpl = fmeta.get("template") or ""
    if tpl:
        return by_tpl.get(tpl)
    if "" in by_tpl:
        return by_tpl[""]
    return next(iter(by_tpl.values()), None)


def _truth_stem_map(run_dir: Path, truth: dict) -> dict[str, str]:
    """run stem -> truth doc key (manifest-aware, D60)."""
    raw = run_dir / "raw"
    if not raw.is_dir():
        return {}
    return {stem: key for key, stem in resolve_stems(truth["docs"], raw).items()}


def _flag_matches(flags: dict, stem: str, field: str, fmeta: dict) -> list[dict]:
    """Flag rows that count for a truth cell: template-pinned cells only take
    flags of that template (or file-level rows without one); unpinned cells
    keep the legacy any-template behaviour."""
    rows = flags.get((stem, field)) or []
    tpl = fmeta.get("template") or ""
    if not tpl:
        return rows
    return [r for r in rows if (r.get("template") or "") in ("", tpl)]


def score_run(run_id: str, truth: dict, runs_root: Path = RUNS_ROOT) -> dict:
    """Current-pipeline flag decisions vs truth for one run (D60: multi-doc
    entries + manifest stem join)."""
    run_dir = runs_root / run_id
    values = read_run_values(run_dir)
    flags = read_run_flags(run_dir)
    stem_map = _truth_stem_map(run_dir, truth)
    buckets = {"TP": [], "FN": [], "FP": [], "TN": [], "absent": []}
    for stem, by_tpl in sorted(values.items()):
        tkey = stem_map.get(stem, stem)
        doc = truth["docs"].get(tkey)
        if not doc:
            continue
        for fname, fmeta in doc.items():
            if fname not in truth["fields"]:
                continue
            fields = _view_fields(by_tpl, fmeta)
            if fields is None:
                buckets["absent"].append(f"{stem}#{fname}")
                continue
            want = fmeta.get("value")
            kind = KIND_BY_FIELD.get(fname, "key")
            got = _field_value(fields, fname)
            # Old-schema runs never extracted boe fields -> absent, not scored.
            if got is None:
                buckets["absent"].append(f"{stem}#{fname}")
                continue
            correct = _correct(str(got["value"]), want, kind)
            should = (not correct) or ((tkey, fname) in truth["quirk"])
            matched = [r for n in _field_names(fname)
                       for r in _flag_matches(flags, stem, n, fmeta)]
            cell = {
                "stem": stem, "field": fname,
                "value": got["value"], "truth": want,
                "correct": correct, "conf": got["confidence"],
                "region": got["source_region"],
                "flags": [r["reason"] for r in matched],
            }
            if should and matched:
                buckets["TP"].append(cell)
            elif should and not matched:
                buckets["FN"].append(cell)
            elif not should and matched:
                buckets["FP"].append(cell)
            else:
                buckets["TN"].append(cell)
    return buckets


def score_fixtures() -> list[dict]:
    """Fixture cell inputs (value/region/line_items) for the stack scorer."""
    out = []
    for fp in sorted(FIXTURES_DIR.glob("syn_*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        fields = {"line_items": data.get("line_items", [])}
        for f in data.get("header_fields", []):
            if f.get("value") is None:
                continue
            fields[f["name"]] = {
                "value": f["value"],
                "confidence": f.get("confidence"),
                "source_region": f.get("source_region"),
                "source": f.get("source"),
            }
        out.append({"file": data["file"],
                    "raw_stem": data.get("raw_stem", data["file"]),
                    "mutation": data.get("mutation", ""),
                    "fields": {"": fields}})  # D60: one top-level view
    return out


# --- Phase 2: the verification check stack ----------------------------------

def _stack_cell(stem: str, fname: str, fmeta: dict, stem_fields: dict, cfg: dict,
                doc_id: str, raw_dir: Path, raw_stem: str | None = None) -> dict:
    """Run the offline stack (grounding/arithmetic/format -> aggregate) on one cell."""
    raw_stem = raw_stem or stem
    got = _field_value(stem_fields, fname)
    if got is None:
        raise KeyError(f"{stem}#{fname}: extraction value not present for a scored cell")
    value = got["value"]
    region = (got.get("source_region") or {}).get("bbox")
    page = (got.get("source_region") or {}).get("page", 1)
    vtype = value_type_for(cfg, doc_id, fname)
    kind = grounding.KIND_BY_TYPE.get(vtype, "code")

    vcfg = cfg["thresholds"]["verification"]
    checks = [check_format(fname, value, vtype)]

    if region is not None:
        g = grounding.check_grounding(
            raw_stem, page, value, region, raw_dir,
            margin=vcfg["grounding_margin_rel"],
            search_margin=vcfg["grounding_search_rel"],
            loose_score=vcfg["grounding_loose_score"],
            ngram_k=vcfg["containment_ngram"],
            kind=kind)
        if g is not None:
            checks.append(g)

    rules = profile_arithmetic_rules(cfg, doc_id)
    if rules:
        fields = {n: {"value": f["value"]} for n, f in stem_fields.items() if n != "line_items"}
        # Old-schema runs name it "total" — expose the profile's name too so
        # the rule (target: total_invoice_amount) can find the value.
        for alias, name in FIELD_ALIASES.items():
            if alias in fields and name not in fields:
                fields[name] = fields[alias]
        amount_col = config.line_item_column(cfg["documents"], doc_id, "amount") \
            if doc_id else None
        arith = [r for r in check_arithmetic(
            fields, stem_fields.get("line_items", []), rules,
            amount_col=(amount_col or {}).get("name", "amount"))
            if r.get("field") == fname]
        checks.extend(arith)

    verdict = aggregate.aggregate(
        fname, checks,
        weights=vcfg["check_weights"], hard_fail=tuple(vcfg["hard_fail_checks"]),
        accept=vcfg["accept_score"], review=vcfg["review_score"])

    failed = [c["check"] for c in checks if not c["passed"]]
    return {
        "stem": stem, "field": fname, "value": value,
        "truth": fmeta["value"], "kind": kind, "vtype": vtype,
        "checks": checks, "verdict": verdict, "failed": failed,
    }


def score_stack(cases: dict, truth: dict, raw_dir: Path,
                cfg: dict, doc_id: str, raw_map: dict | None = None,
                stem_map: dict | None = None) -> dict:
    """Score stack verdicts against truth: buckets per cell + gap details.

    D60: `cases` values are {template_id: fields} views ("" = single-doc);
    a truth cell with `template` picks its entry, and `stem_map` joins run
    stems to truth doc keys through the manifest.
    """
    raw_map = raw_map or {}
    stem_map = stem_map or {}
    buckets = {"TP": [], "FN": [], "FP": [], "TN": [], "absent": []}
    for stem, by_tpl in cases.items():
        tkey = stem_map.get(stem, stem)
        doc = truth["docs"].get(tkey)
        if not doc:
            continue
        raw_stem = raw_map.get(stem, stem)
        for fname, fmeta in doc.items():
            if fname not in truth["fields"]:
                continue
            fields = _view_fields(by_tpl, fmeta)
            if fields is None:
                buckets["absent"].append(f"{stem}#{fname}")
                continue
            got = _field_value(fields, fname)
            if got is None:
                buckets["absent"].append(f"{stem}#{fname}")
                continue
            kind = KIND_BY_FIELD.get(fname, "key")
            correct = _correct(str(got["value"]), fmeta["value"], kind)
            if "expect_flag" in fmeta:
                should = bool(fmeta["expect_flag"])
            else:
                should = (not correct) or ((tkey, fname) in truth["quirk"])
            cell = _stack_cell(stem, fname, fmeta, fields, cfg,
                               fmeta.get("template") or doc_id, raw_dir,
                               raw_stem=raw_stem)
            cell["correct"] = correct
            cell["should"] = should
            flagged = cell["verdict"].flagged
            if should and flagged:
                buckets["TP"].append(cell)
            elif should and not flagged:
                buckets["FN"].append(cell)
            elif not should and flagged:
                buckets["FP"].append(cell)
            else:
                buckets["TN"].append(cell)
    return buckets


def _fmt_gaps(cells: list[dict]) -> list[str]:
    out = []
    for c in cells:
        if "checks" in c:  # stack cell
            checks = " | ".join(f"{r['check']}={r['score']:.1f}"
                                for r in c["checks"])
            out.append(
                f"  {c['stem']}#{c['field']}: got {c['value']!r} vs truth {c['truth']!r}"
                f" ({c['kind']}) [failed: {c['failed'] or 'none'}] "
                f"— {checks} | verdict={c['verdict'].level}")
        else:  # baseline cell
            out.append(
                f"  {c['stem']}#{c['field']}: got {c['value']!r} vs truth {c['truth']!r}"
                f" (conf {c['conf']}, {c['region'] if c['region'] else 'no region'})")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", default=",".join(DEFAULT_RUNS),
                    help="comma-separated run ids under the runs root/")
    ap.add_argument("--runs-root", default=str(RUNS_ROOT),
                    help="runs root dir (default: out/runs)")
    ap.add_argument("--raw", default=None,
                    help="raw text channel dir for the stack checks "
                         "(default: the first run's own raw/ capture)")
    args = ap.parse_args(argv)

    truth = load_truth()
    runs = [r.strip() for r in args.runs.split(",") if r.strip()]
    runs_root = Path(args.runs_root)
    raw_dir = Path(args.raw) if args.raw else (runs_root / runs[0] / "raw")
    if not raw_dir.exists():
        raw_dir = REPO / "out" / "latest" / "raw"

    totals = {k: 0 for k in ("TP", "FN", "FP", "TN", "absent")}
    for run_id in runs:
        buckets = score_run(run_id, truth, runs_root)
        n = {k: len(v) for k, v in buckets.items()}
        for k in totals:
            totals[k] += n[k]
        print(f"=== flags run {run_id} ===")
        info = run_info(runs_root / run_id / "json")
        if info["files"]:
            print("  run info: models " + ", ".join(
                f"{m} x{c}" for m, c in sorted(info["models"].items()))
                + " | prompt " + ", ".join(
                    f"{p} x{c}" for p, c in sorted(info["prompts"].items())))
            if info["mixed"]:
                print("  WARNING: mixed models/prompt versions — not a "
                      "single-sample benchmark")
        print(f"  TP={n['TP']} FN={n['FN']} FP={n['FP']} TN={n['TN']} absent*={n['absent']}")
        if buckets["FN"]:
            print("  MISSED WRONG READS (should flag, went unflagged):")
            print("\n".join(_fmt_gaps(buckets["FN"])))
        if buckets["FP"]:
            print("  PHANTOM FLAGS (correct values flagged):")
            print("\n".join(_fmt_gaps(buckets["FP"])))
        print()

    tp, fn, fp, tn = totals["TP"], totals["FN"], totals["FP"], totals["TN"]
    scored = tp + fn + fp + tn
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    accuracy = (tp + tn) / scored if scored else float("nan")
    print("=== flags totals (all runs) ===")
    print(f"  totals: TP={tp} FN={fn} FP={fp} TN={tn} absent*={totals['absent']} scored={scored}")
    print(f"  precision={precision:.4f} recall={recall:.4f} accuracy={accuracy:.4f}")
    print("  *absent = extraction schema lacked the field (old fixed_v1 runs have no boe fields)")

    # ---- the verification stack over the same ground truth ----
    print(f"\n=== verification STACK (offline checks; raw={raw_dir}) ===")
    cfg = config.load_all()
    docs = cfg.get("documents", {}).get("documents", [])
    doc_id = docs[0]["id"] if docs else ""
    stack_totals = {k: 0 for k in ("TP", "FN", "FP", "TN", "absent")}
    for run_id in runs:
        run_path = runs_root / run_id
        cases = read_run_values(run_path)
        buckets = score_stack(cases, truth, raw_dir, cfg, doc_id,
                              stem_map=_truth_stem_map(run_path, truth))
        n = {k: len(v) for k, v in buckets.items()}
        for k in stack_totals:
            stack_totals[k] += n[k]
        print(f"=== stack run {run_id} ===")
        print(f"  TP={n['TP']} FN={n['FN']} FP={n['FP']} TN={n['TN']} absent*={n['absent']}")
        if buckets["FN"]:
            print("  MISSED WRONG READS (stack accepted):")
            print("\n".join(_fmt_gaps(buckets["FN"])))
        if buckets["FP"]:
            print("  PHANTOM FLAGS (stack flagged correct values):")
            print("\n".join(_fmt_gaps(buckets["FP"])))
        print()

    fx_entries = score_fixtures()
    fx_cases = {fx["file"]: fx["fields"] for fx in fx_entries}
    fx_raw = {fx["file"]: fx["raw_stem"] for fx in fx_entries}
    fx_buckets = score_stack(fx_cases, truth, raw_dir, cfg, doc_id,
                             raw_map=fx_raw)
    n = {k: len(v) for k, v in fx_buckets.items()}
    for k in stack_totals:
        stack_totals[k] += n[k]
    print("=== stack synthetic fixtures ===")
    print(f"  TP={n['TP']} FN={n['FN']} FP={n['FP']} TN={n['TN']} absent*={n['absent']}")
    if fx_buckets["FN"]:
        print("  MISSED (fixture expected a flag, stack accepted):")
        print("\n".join(_fmt_gaps(fx_buckets["FN"])))
    if fx_buckets["FP"]:
        print("  PHANTOM (fixture expected clean, stack flagged):")
        print("\n".join(_fmt_gaps(fx_buckets["FP"])))
    print()

    tp, fn, fp, tn = (stack_totals["TP"], stack_totals["FN"],
                      stack_totals["FP"], stack_totals["TN"])
    scored = tp + fn + fp + tn
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    accuracy = (tp + tn) / scored if scored else float("nan")
    print("=== stack totals (runs + fixtures) ===")
    print(f"  TP={tp} FN={fn} FP={fp} TN={tn} scored={scored}")
    print(f"  precision={precision:.4f} recall={recall:.4f} accuracy={accuracy:.4f}")
    print("  (disagreement/Check 4 absent offline — its 0.35 weight drops out of the renormalization)")
    return 0


if __name__ == "__main__":
    sys.exit(main())