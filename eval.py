"""Evaluation harness — field x file accuracy matrix + source-pointer audit.

Compares extraction output (out/latest/json/<stem>.json) against the owner's
manually-verified ground truth (tests/ground_truth.json — 100% accurate,
owner statement 2026-08-03) and prints a field x file matrix:
correct / wrong / missing, plus a source-pointer audit.

Scoring (profile-aware, D27): `selection` (the broker name the vision pass
read, compared to the truth vendor) plus the broker_invoice profile's fields
that exist in the ground truth (invoice_number, invoice_date,
total_invoice_amount). Fields the profile doesn't extract are not scored.

Normalization rules (documented, deterministic):
  - amounts: strip currency symbols/commas/whitespace, compare as float with
    tolerance 0.01 (D8 rounding tolerance; ground truth is the target).
  - dates: parse dd/MM/yyyy, dd-MM-yyyy, dd-MMM-yyyy -> ISO; compare.
  - selection/vendor names: lowercase, strip punctuation and legal suffixes
    (pvt/private/ltd/limited/inc/corp), compare by containment either way.
  - invoice_number: exact after whitespace/underscore/case normalization.

Source-pointer audit: every non-null header value must resolve to
"page N, line M" (or "page N, lines M-K" / "lines A, B" for multi-line
citations) against out/latest/raw/<stem>/pageNNN.txt. Scalar values must also
appear among the cited lines (alnum-normalized); object values (refs,
tax_breakdown) resolve when the pointer is real and at least one non-null
component appears in the cited lines. Unresolved pointers are reported
(the audit requires 100%).

Usage:
  python eval.py                          # out/latest vs tests/ground_truth.json
  python eval.py --json out/latest/json --truth tests/ground_truth.json --raw out/latest/raw

Exit code: 0 = ok; 1 = extraction missing (not yet run) or unexpected truth
format; 2 = accuracy below the bar (>=98% header fields, 100% sources).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import config
from compare import (amount_eq, date_eq, name_eq, norm_key, profile_field_kinds,
                     selected_name, str_eq)

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_JSON = REPO_ROOT / "out" / "latest" / "json"
DEFAULT_RAW = REPO_ROOT / "out" / "latest" / "raw"
DEFAULT_TRUTH = REPO_ROOT / "tests" / "ground_truth.json"
# D47 line-item truth: expected body-row count + row TOTAL amounts per corpus
# file (hand-verified from the raw text channel — never from model output).
DEFAULT_LINE_ITEM_TRUTH = REPO_ROOT / "tests" / "line_item_truth.json"

# Owner-declared exclusions (2026-08-03): one corpus file's header exists on
# the paper but the OCR channel cannot read it — the owner dropped that file
# from the eval matrix. Keys are the raw directory stems (also the
# ground-truth key after alnum normalization); the content-verified mapping
# lives in the decision log.
EXCLUDED = {
    "classic_1": "owner-declared: OCR cannot read the invoice header (2026-08-03)",
}

# Scorable field names per DOCUMENT PROFILE (D27): the broker profile's
# user-defined fields that also exist in the ground truth. The eval matrix is
# profile-aware — fields the profile doesn't extract are simply not scored.
def _profile_strict_fields() -> tuple[str, ...]:
    from config import load_documents
    docs = load_documents()["documents"]
    broker = next((d for d in docs if d["id"] == "broker_invoice"), None)
    profile_fields = [f["name"] for f in (broker or {}).get("extraction", {}).get("fields", [])]
    mapped = [k for k in ("invoice_number", "invoice_date", "total_invoice_amount")
              if k in profile_fields]
    return ("selection",) + tuple(mapped) if mapped else ("selection",)


STRICT_FIELDS = _profile_strict_fields()

TRUTH_KEYS = {
    "invoice_number": "invoice_number", "invoice_date": "invoice_date",
    "total_invoice_amount": "total",
}


def load_truth(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for row in data:
        key = norm_key(str(row["file"]))
        # D29 (vision-led): no broker registry — the truth vendor string IS
        # the reference; selection is scored by name equality below.
        row["_broker"] = row.get("vendor")
        out[key] = row
    return out


def resolve_stems(truth: dict, raw_dir: Path) -> dict[str, str]:
    """truth alnum-key -> stem that exists on disk (raw dir or manifest).

    Bookkeeping join only: the truth `file` column labels the raw stem. It is
    NOT a semantic factor — the pairing is verified by content (invoice
    numbers in the raw text), never trusted from file names (AGENTS.md).

    D60: UI/workflow runs ingest uploads under content-hash stems and keep the
    original filenames in `<run>/manifest.json` — join truth rows by the
    ORIGINAL name (stem) as well, so eval works on UI runs, not only on the
    corpus dir names.
    """
    by_alnum = {norm_key(d.name): d.name for d in raw_dir.iterdir() if d.is_dir()}
    manifest = raw_dir.parent / "manifest.json"
    if manifest.exists():
        try:
            files = json.loads(manifest.read_text(encoding="utf-8")).get("files") or []
        except (OSError, json.JSONDecodeError):
            files = []
        for e in files:
            stem, original = e.get("stem"), e.get("original") or ""
            if not stem:
                continue
            by_alnum.setdefault(norm_key(stem), stem)
            if original:
                by_alnum.setdefault(norm_key(Path(original).stem), stem)
    out = {}
    for k in truth:
        stem = by_alnum.get(k) or by_alnum.get(norm_key(k))
        if stem:
            out[k] = stem
    return out


def run_info(json_dir: Path) -> dict:
    """Model + prompt provenance across a run's JSONs (D60 benchmark header).

    Returns {models: {name: n}, prompts: {version: n}, files, mixed}. A run
    with more than one model or prompt version is NOT a single benchmark
    sample — callers print a loud warning.
    """
    models: dict[str, int] = {}
    prompts: dict[str, int] = {}
    files = 0
    for jp in sorted(json_dir.glob("*.json")):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files += 1
        meta = data.get("_meta") or {}
        model = meta.get("model") or "?"
        prompt = (meta.get("prompt") or {})
        version = prompt.get("version") if isinstance(prompt, dict) else str(prompt)
        version = version or "?"
        models[model] = models.get(model, 0) + 1
        prompts[version] = prompts.get(version, 0) + 1
    return {"models": models, "prompts": prompts, "files": files,
            "mixed": len(models) > 1 or len(prompts) > 1}


def _scoring_view(data: dict, profile_id: str = "broker_invoice") -> dict | None:
    """The scorable document view of a run JSON (D60).

    Single-document JSONs are already flat. Multi-document workflow runs keep
    each document in `documents[]` — score the FIRST found entry of the
    profile under test (the corpus truth is broker-invoice shaped). Selection
    and `_meta` are carried through (with `_meta.document` defaulted to the
    entry's template) so the existing scoring logic is unchanged.
    """
    if "header_fields" in data:
        return data
    entries = [d for d in data.get("documents", [])
               if isinstance(d, dict) and d.get("found")
               and d.get("template_id") == profile_id]
    if not entries:
        return None
    entry = entries[0]
    meta = dict(data.get("_meta") or {})
    meta.setdefault("document", entry.get("template_id"))
    return {
        "header_fields": entry.get("header_fields", []),
        "line_items": entry.get("line_items", []),
        "selected_document": entry.get("selected_document") or {},
        "_meta": meta,
        "status": data.get("status"),
    }


def load_line_item_truth(path: Path) -> dict[str, dict]:
    """{corpus key: {rows, amounts}} — `_meta` is skipped."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {norm_key(k): v for k, v in data.items() if not k.startswith("_")}


def compare_line_items(line_items: list[dict], truth_row: dict | None,
                       columns: list[dict] | None = None) -> dict | None:
    """D47 truth check for one file: row-count exact + row-amount multiset.

    The extracted value is the template's `amount`-role column (D49; defaults
    to the generic `amount`). Returns None when the file has no truth entry;
    `amounts_matched` counts expected printed row amounts found in the
    extraction (amount equality, 0.01).
    """
    if not truth_row:
        return None
    cols = columns or list(config.DEFAULT_LINE_ITEM_COLUMNS)
    amount_name = next((c["name"] for c in cols if c.get("role") == "amount"),
                       "amount")
    actual = [li.get(amount_name) for li in line_items or []]
    expected = list(truth_row.get("amounts") or [])
    remaining = list(actual)
    matched = 0
    for e in expected:
        hit = next((a for a in remaining if amount_eq(a, e)), None)
        if hit is not None:
            remaining.remove(hit)
            matched += 1
    return {"rows_ok": len(actual) == int(truth_row.get("rows", -1)),
            "rows_got": len(actual), "rows_truth": truth_row.get("rows"),
            "amounts_matched": matched, "amounts_truth": len(expected)}


def field_value(fields: list[dict], name: str):
    for f in fields or []:
        if f.get("name") == name:
            return f.get("value")
    return None


def compare(truth_val, extracted_val, kind: str) -> bool:
    if kind == "amount":
        return amount_eq(truth_val, extracted_val)
    if kind == "date":
        return date_eq(truth_val, extracted_val)
    if kind == "name":
        return name_eq(truth_val, extracted_val)
    return str_eq(truth_val, extracted_val)


def _components(value) -> list[str]:
    """Flatten an object value into its non-null leaf strings."""
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out.extend(_components(v))
        return out
    if value in (None, ""):
        return []
    return [str(value)]


def _cited_lines(src: str) -> tuple[int, list[int]] | None:
    """Parse "page N, line M" / "page N, lines M-K" / "lines A, B, C-D".

    Returns (page, [lines]) or None when no valid pointer is present.
    """
    m = re.search(r"page\s+(\d+),\s*lines?\s+([\d,\s\-]+)", src, re.IGNORECASE)
    if not m:
        return None
    page = int(m.group(1))
    lines: list[int] = []
    for part in m.group(2).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.strip().isdigit() and b.strip().isdigit()):
                return None
            lines.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            lines.append(int(part))
        else:
            return None
    return (page, lines) if lines else None


def _render_source(src: str) -> int | None:
    """Parse a vision source "page N (render image)".

    Returns the 1-based page number, or None when the source is not a render
    pointer. Vision-sourced values point at the page render PNG itself, not a
    text line — the audit resolves them when the render file exists.
    """
    m = re.match(r"page\s+(\d+)\s+\(render image\)", src.strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _render_exists(raw_dir: Path, stem: str, page: int) -> bool:
    base = raw_dir / stem / f"page{page:03d}"
    return base.with_name(f"page{page:03d}.render-ocr-input.png").exists() or \
           base.with_name(f"page{page:03d}.render-images-only.png").exists() or \
           base.with_name(f"page{page:03d}.image.png").exists()


def audit_sources(stem: str, json_data: dict, raw_dir: Path) -> tuple[int, list[str]]:
    """Count non-null header values whose claimed source resolves (page/line).

    Audit bar: EVERY non-null value carries a source that resolves to a real
    line in the raw text. Scalar values must additionally
    appear (alnum-normalized) in the cited line(s); object values resolve
    when the pointer is real and at least one non-null component appears
    there.
    """
    ok, unresolved = 0, []
    page_rows: dict[int, list[str]] = {}

    def rows_for(page: int) -> list[str] | None:
        if page not in page_rows:
            txt = raw_dir / stem / f"page{page:03d}.txt"
            if not txt.exists():
                page_rows[page] = []
                return None
            page_rows[page] = txt.read_text(encoding="utf-8").splitlines()
        return page_rows[page]

    for f in json_data.get("header_fields", []):
        name = f.get("name")
        value = f.get("value")
        if value is None or value == "":
            continue
        src = f.get("source") or ""
        render_page = _render_source(src)
        if render_page is not None:
            if _render_exists(raw_dir, stem, render_page):
                ok += 1
            else:
                unresolved.append(f"{name}: render image missing for page {render_page}")
            continue
        ptr = _cited_lines(src)
        if ptr is None:
            unresolved.append(f"{name}: no page/line in source {src!r}")
            continue
        page, lines = ptr
        rows = rows_for(page)
        if rows is None:
            unresolved.append(f"{name}: page {page} text missing")
            continue
        beyond = [ln for ln in lines if ln > len(rows) or ln < 1]
        if beyond:
            unresolved.append(
                f"{name}: line(s) {beyond} beyond {len(rows)} rows (p{page})")
            continue
        hay = " ".join(norm_key(rows[ln - 1]) for ln in lines)
        if isinstance(value, dict):
            parts = [norm_key(p) for p in _components(value) if norm_key(p)]
            if parts and not any(p in hay for p in parts):
                unresolved.append(
                    f"{name}: no component found in p{page} l{lines}: "
                    f"{rows[lines[0] - 1][:60]!r}"
                )
                continue
        else:
            needle = norm_key(str(value))
            if needle and needle not in hay:
                unresolved.append(
                    f"{name}: value {value!r} not found in p{page} l{lines}: "
                    f"{rows[lines[0] - 1][:60]!r}"
                )
                continue
        ok += 1
    return ok, unresolved


def audit_line_item_sources(stem: str, json_data: dict, raw_dir: Path,
                            columns: list[dict] | None = None) -> tuple[int, list[str]]:
    """Line-item source pointers resolve like header ones (no ground truth).

    Same rules as audit_sources (value must appear in the cited line, alnum-
    normalized); separate counters so line-item health doesn't pollute the
    header-field source audit. The audited value is the template's
    description-role column (D48; defaults to `description`).
    """
    if columns is None:
        doc_id = (json_data.get("_meta") or {}).get("document") or ""
        columns = config.line_item_columns(config.load_documents(), doc_id) \
            if doc_id else list(config.DEFAULT_LINE_ITEM_COLUMNS)
    desc_name = next((c["name"] for c in columns if c.get("role") == "description"),
                     "description")
    ok, unresolved = 0, []
    page_rows: dict[int, list[str]] = {}

    def rows_for(page: int) -> list[str] | None:
        if page not in page_rows:
            txt = raw_dir / stem / f"page{page:03d}.txt"
            if not txt.exists():
                page_rows[page] = []
                return None
            page_rows[page] = txt.read_text(encoding="utf-8").splitlines()
        return page_rows[page]

    for i, li in enumerate(json_data.get("line_items", [])):
        value = li.get(desc_name)
        src = li.get("source") or ""
        name = f"line_items[{i}]"
        if value is None or value == "":
            continue
        render_page = _render_source(src)
        if render_page is not None:
            if _render_exists(raw_dir, stem, render_page):
                ok += 1
            else:
                unresolved.append(f"{name}: render image missing for page {render_page}")
            continue
        ptr = _cited_lines(src)
        if ptr is None:
            unresolved.append(f"{name}: no page/line in source {src!r}")
            continue
        page, lines = ptr
        rows = rows_for(page)
        if rows is None:
            unresolved.append(f"{name}: page {page} text missing")
            continue
        beyond = [ln for ln in lines if ln > len(rows) or ln < 1]
        if beyond:
            unresolved.append(f"{name}: line(s) {beyond} beyond {len(rows)} rows (p{page})")
            continue
        hay = " ".join(norm_key(rows[ln - 1]) for ln in lines)
        needle = norm_key(str(value))
        if needle and needle not in hay:
            unresolved.append(
                f"{name}: value {value!r} not found in p{page} l{lines}: "
                f"{rows[lines[0] - 1][:60]!r}"
            )
            continue
        ok += 1
    return ok, unresolved


def run_eval(json_dir: Path, raw_dir: Path, truth_path: Path) -> int:
    truth = load_truth(truth_path)
    li_truth = load_line_item_truth(DEFAULT_LINE_ITEM_TRUTH)
    stems = resolve_stems(truth, raw_dir)
    missing = [k for k, v in stems.items() if not (json_dir / f"{v}.json").exists()]
    if missing:
        print("no extraction JSON for ground-truth files (run `make extract-corpus` first):")
        for k in missing:
            print(f"  {k}  (raw stem: {stems[k]})")
        return 1

    info = run_info(json_dir)
    if info["files"]:
        print("run info: models " + ", ".join(f"{m} x{n}" for m, n in
                                              sorted(info["models"].items()))
              + " | prompt " + ", ".join(f"{p} x{n}" for p, n in
                                         sorted(info["prompts"].items())))
        if info["mixed"]:
            print("  WARNING: mixed models/prompt versions in this run — "
                  "not a single-sample benchmark")

    rows, audits = [], []
    for key, stem in sorted(stems.items()):
        truth_row = truth[key]
        json_path = json_dir / f"{stem}.json"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        view = _scoring_view(data)  # D60: multi-document workflow runs
        if data.get("status") == "failed" or view is None:
            rows.append({"file": stem, "status": "failed"})
            continue
        row = {"file": stem}
        if norm_key(stem) in {norm_key(e) for e in EXCLUDED}:
            row["excluded"] = EXCLUDED.get(stem) or EXCLUDED.get(
                next(e for e in EXCLUDED if norm_key(e) == norm_key(stem)))
        sel = view.get("selected_document") or {}
        # D29 vision-led selection: the model reports the name it READ on the
        # target document (selected_document.name, free-form; legacy `broker`
        # accepted for pre-D45 runs); score it with name equality against the
        # truth vendor — no registry.
        read_name = selected_name(sel)
        row["selection"] = (
            "ok"
            if read_name and name_eq(read_name, truth_row.get("vendor") or "")
            else "wrong"
        )
        fields = view.get("header_fields", [])
        kinds = profile_field_kinds(config.load_all(), "broker_invoice")
        for name in STRICT_FIELDS[1:]:
            tv = truth_row.get(TRUTH_KEYS[name])
            ev = field_value(fields, name)
            if ev is None or ev == "":
                verdict = "missing"
            elif compare(tv, ev, kinds.get(name, "str")):
                verdict = "ok"
            else:
                verdict = "wrong"
            row[name] = verdict
        sources_ok, unresolved = audit_sources(stem, view, raw_dir)
        audits.append((stem, sources_ok, unresolved))
        row["sources_ok"] = sources_ok
        row["sources_bad"] = len(unresolved)
        li_ok, li_unresolved = audit_line_item_sources(stem, view, raw_dir)
        row["li_sources"] = (li_ok, li_ok + len(li_unresolved))
        doc_id = (view.get("_meta") or {}).get("document") or ""
        li_columns = config.line_item_columns(config.load_documents(), doc_id) \
            if doc_id else None
        row["li_truth"] = compare_line_items(view.get("line_items") or [],
                                             li_truth.get(key), columns=li_columns)
        rows.append(row)

    # ---- print matrix ----
    header = ["file"] + list(STRICT_FIELDS) + ["sources"]
    print(" | ".join(f"{h:>16}" for h in header))
    print("-" * (17 * len(header)))
    totals = {f: {"ok": 0, "wrong": 0, "missing": 0} for f in STRICT_FIELDS}
    for row in rows:
        if row.get("excluded"):
            print(f"{row['file']:<16} EXCLUDED: {row['excluded']}")
            continue
        if row.get("status") == "failed":
            print(f"{row['file']:<16} FAILED (no extraction)")
            continue
        cells = [row["file"][:16]]
        for f in STRICT_FIELDS:
            v = row[f]
            totals[f][v if v in totals[f] else "missing"] += 1
            mark = {"ok": "OK", "wrong": "WRONG", "missing": "MISS"}.get(v, v)
            cells.append(f"{mark:>16}")
        cells.append(f"{row['sources_ok']}/{row['sources_ok'] + row['sources_bad']:>16}")
        print(" | ".join(cells))

    scored = {f: 0 for f in STRICT_FIELDS}
    total_fields = 0
    for row in rows:
        if row.get("excluded") or row.get("status") == "failed":
            continue
        for f in STRICT_FIELDS:
            if row[f] == "ok":
                scored[f] += 1
            total_fields += 1
    print("-" * (17 * len(header)))
    print("accuracy per field (excluded/failed rows dropped):")
    for f in STRICT_FIELDS:
        n = sum(1 for r in rows if not r.get("excluded") and r.get("status") != "failed")
        print(f"  {f:>16}: {scored[f]}/{n}  ({100 * scored[f] / n:.1f}%)" if n else f"  {f}: no rows")
    field_acc = sum(scored.values()) / max(total_fields, 1)
    source_ok = sum(a[1] for a in audits)
    source_total = sum(a[1] + len(a[2]) for a in audits)
    print(f"\nheader-field accuracy: {100 * field_acc:.1f}%  (target >=98%)")
    print(f"source-pointer resolution: {source_ok}/{source_total}")
    for stem, _, unresolved in audits:
        for u in unresolved[:5]:
            print(f"  source issue {stem}: {u}")
        if len(unresolved) > 5:
            print(f"  ... and {len(unresolved) - 5} more in {stem}")

    # ---- line-item source resolution (no ground truth) ----
    print("\nline-item source resolution:")
    for row in rows:
        if row.get("excluded") or row.get("status") == "failed":
            continue
        ok, total = row["li_sources"]
        print(f"  {row['file']:<16} {ok}/{total}")

    # ---- line-item truth (D47: row count + row amounts) ----
    if li_truth:
        print("\nline items (truth: row count + row amounts):")
        files_rows_ok = files_scored = matched = expected_total = 0
        for row in rows:
            if row.get("status") == "failed":
                continue  # header exclusions don't apply to the table truth
            t = row.get("li_truth")
            if not t:
                continue
            files_scored += 1
            files_rows_ok += 1 if t["rows_ok"] else 0
            matched += t["amounts_matched"]
            expected_total += t["amounts_truth"]
            mark = "OK" if t["rows_ok"] and t["amounts_matched"] == t["amounts_truth"] else "CHECK"
            print(f"  {row['file']:<16} rows {t['rows_got']}/{t['rows_truth']} · "
                  f"amounts {t['amounts_matched']}/{t['amounts_truth']}  {mark}")
        print(f"  row counts: {files_rows_ok}/{files_scored} files exact; "
              f"row amounts: {matched}/{expected_total} matched")
    # ---- verification stats (2026-09-02 stack; legacy runs keep the D24
    # crosscheck printout) ----
    meta0 = {}
    try:
        meta0 = (json.loads((json_dir / f"{rows[0]['file']}.json").read_text(encoding="utf-8"))
                 .get("_meta", {}))
    except (OSError, json.JSONDecodeError, IndexError):
        pass
    if "crosscheck" in meta0:
        print("\ncrosscheck (vision primary vs rules locator, D24 — archived runs):")
        x_total = {"agreed": 0, "conflicts": 0, "rules_only": 0, "vision_only": 0, "arbitrated": 0}
        for row in rows:
            if row.get("excluded") or row.get("status") == "failed":
                continue
            json_path = json_dir / f"{row['file']}.json"
            try:
                xc = (json.loads(json_path.read_text(encoding="utf-8"))
                      .get("_meta", {}).get("crosscheck", {}))
            except (json.JSONDecodeError, OSError):
                continue
            summary = xc.get("summary", {})
            conflicts = xc.get("conflicts", {})
            selection = xc.get("selection")
            parts = [f"{k}={summary.get(k, 0)}" for k in x_total]
            if selection:
                agree = "agree" if selection.get("agree") else "CONFLICT"
                if "pinned" in selection:
                    parts.append(f"pin:{agree}(pinned={selection['pinned']}, actual={selection.get('actual')})")
                else:
                    parts.append(f"selection:{agree}")
            print(f"  {row['file']:<16} " + " ".join(parts))
            for k in x_total:
                x_total[k] += summary.get(k, 0)
            for field, rec in sorted(conflicts.items()):
                print(f"      conflict {field}: vision={rec.get('vision')!r} "
                      f"vs rules={rec.get('rules')!r}")
        print("  totals: " + " ".join(f"{k}={v}" for k, v in x_total.items()))
    elif "verification" in meta0:
        print("\nverification stack (grounding/arithmetic/format + gated recheck):")
        v_total: dict[str, int] = {}
        rechecks = 0
        for row in rows:
            if row.get("excluded") or row.get("status") == "failed":
                continue
            json_path = json_dir / f"{row['file']}.json"
            try:
                meta = (json.loads(json_path.read_text(encoding="utf-8"))
                        .get("_meta", {}))
            except (json.JSONDecodeError, OSError):
                continue
            ver = meta.get("verification", {})
            summary = ver.get("summary", {})
            flags = summary.get("flag_rows", 0)
            recheck = ver.get("recheck", {})
            agree = {f: rec["agree"] for f, rec in (recheck.get("fields") or {}).items()}
            parts = [f"flags={flags}"] + [f"{k}={v}" for k, v
                                          in summary.get("failed_by_check", {}).items()]
            if recheck.get("triggered"):
                parts.append(f"recheck={agree}")
                rechecks += 1
            print(f"  {row['file']:<16} " + " ".join(parts))
            for k, v in summary.get("failed_by_check", {}).items():
                v_total[k] = v_total.get(k, 0) + v
        print("  totals: " + " ".join(f"{k}={v}" for k, v in v_total.items())
              + f"; rechecks: {rechecks}")

    ok = field_acc >= 0.98 and source_total > 0 and source_ok == source_total
    print("EXIT: PASS" if ok else "EXIT: FAIL (below the accuracy bar)")
    return 0 if ok else 2


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eval.py",
        description="Field x file accuracy matrix vs tests/ground_truth.json "
                    "+ source-pointer audit.",
    )
    p.add_argument("--json", default=str(DEFAULT_JSON), help="out/latest/json dir")
    p.add_argument("--raw", default=str(DEFAULT_RAW), help="out/latest/raw dir")
    p.add_argument("--truth", default=str(DEFAULT_TRUTH), help="ground truth JSON")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return run_eval(Path(args.json), Path(args.raw), Path(args.truth))


if __name__ == "__main__":
    sys.exit(main())
