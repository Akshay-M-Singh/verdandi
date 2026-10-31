"""LLM extraction — vision-primary, one schema-constrained call (architecture.md D24/D25).

Pivot (2026-08-19, owner decision): the extraction pass reads PAGE IMAGES, not
text. The old text-bulk + vision-escalation two-tier pipeline is REMOVED —
measured token evidence (2026-08-19): vision used ~37% fewer tokens than the
text pass for equal-or-better quality, and vision fixed every chronic
text-channel failure (a date misread, a garbled identifier). The text channel
(embedded/OCR, D15) feeds ONLY the
deterministic verification stack (2026-09-02 — a SANITY CHECKER, not a second
extractor; replaced the retired label-adjacency locator + crosscheck):

  PDF -> page renders (unconditional, D24) -> vision (D25: reads the TARGET
          DOCUMENT PROFILE from rules/documents.yaml and identifies the
          matching document among several in one PDF)
          ── verification stack (verify.py: grounding / arithmetic / format
             -> per-field accept|review verdict) ──> merged JSON + flags
        -> text channel (embedded/OCR)      -> grounding existence check
  + GATED Check 4 (disagreement): a cheap-model re-extraction with a
    DIFFERENT framing, ONLY on flagged fields, capped per file
    (config/thresholds.yaml verification: — D24 cost discipline)
  + selection guards (no_document_match / sub-min_score / pin-drift)
  + <run>/flags.csv (same FLAG_COLUMNS contract — dual-anchored rows)

Per-file output (same schema as before, extended):
  { selected_document, header_fields[+source_region, verification],
    line_items, notes, status, _meta{model, mode, attempts, usage,
    verification{fields:{checks,score,level}, recheck}} }
Every check result is kept in `_meta.verification` (audit); the primary
model's values and confidence are never silently re-scored (D7/D24).
`--model` overrides the primary model from config/models.yaml (model-swap
verification: run the corpus + `make eval` before promoting).

Run modes:
  python extract.py out/latest/raw out/latest/json            # batch (default document)
  python extract.py out/latest/raw out/latest/json --file <stem>
  python extract.py out/latest/raw out/latest/json --model <candidate-model>
  python extract.py out/latest/raw out/latest/json --document <profile-id>  # D25

Rules: prompts/ (text.py literals + compose.py assembly, D44),
rules/documents.yaml (profiles, D25/D29), llm_client.py (GO gateway),
D1/D4/D5/D7/D9/D10. See roadmap.md Stage 2/2b/3/4 and architecture.md D24/D25/D30.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import sys
from pathlib import Path

import aggregate
import config
import llm_client
import model_catalog
import validate as validate_mod
import verify as verify_mod
from compare import (FLAG_COLUMNS, _field_source, field_threshold,
                     profile_field_kinds, selected_name, values_equal)
from prompts import compose

logger = logging.getLogger("extract")

SCHEMA_FIELDS = {"selected_document", "header_fields", "line_items", "notes", "status"}


class SchemaError(ValueError):
    """Extraction JSON failed the schema shape check."""


def _profile(cfg: dict, doc_id: str) -> dict:
    profile = next((d for d in cfg["documents"]["documents"] if d["id"] == doc_id), None)
    if profile is None:
        raise ValueError(f"unknown document profile {doc_id!r} — rules/documents.yaml")
    return profile


def profile_fields(cfg: dict, doc_id: str) -> list[dict]:
    """The document profile's user-defined extraction fields (D27) — [] when
    the profile uses the generic minimal schema (D26). Wrapper over
    config.profile_fields (D44: the canonical accessor lives in config so the
    prompt assembly never imports extract)."""
    return config.profile_fields(cfg["documents"], doc_id)


def parse_pin_pages(spec: str) -> list[int]:
    """Parse a pin spec like '1-3,5' into [1, 2, 3, 5]. Raises ValueError."""
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"bad pinned-pages range {part!r}")
            pages.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            pages.append(int(part))
        else:
            raise ValueError(f"bad pinned-pages entry {part!r}")
    if not pages or any(p < 1 for p in pages):
        raise ValueError("pinned pages must be non-empty 1-based page numbers")
    return sorted(set(pages))


def _line_item_problems(li, where: str, columns: list[dict]) -> list[str]:
    """Shape-check ONE line-item row against the template's declared columns
    (D48): the description-role column must be a string; only declared columns
    (plus confidence/source/source_region) are allowed; a row source_region
    must be a valid {page, bbox} block."""
    if not isinstance(li, dict):
        return [f"{where} is not an object"]
    problems: list[str] = []
    col_names = {c["name"] for c in columns}
    desc_name = next((c["name"] for c in columns if c.get("role") == "description"),
                     "description")
    if not isinstance(li.get(desc_name), str):
        problems.append(f"{where}.{desc_name} missing string value")
    for key in li:
        if key in ("confidence", "source", "source_region"):
            continue
        if key not in col_names:
            problems.append(f"{where}.{key} is not a declared line-item column")
    conf = li.get("confidence")
    if conf is not None and (not isinstance(conf, (int, float)) or not 0 <= conf <= 1):
        problems.append(f"{where}.confidence not in 0-1")
    if not isinstance(li.get("source"), (str, type(None))):
        problems.append(f"{where}.source not a string/null")
    region = li.get("source_region")
    if region is not None:
        if not isinstance(region, dict) or not isinstance(region.get("page"), int):
            problems.append(f"{where}.source_region needs page: int")
        else:
            bbox = region.get("bbox")
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or not all(isinstance(c, (int, float)) and 0 <= c <= 1 for c in bbox)):
                problems.append(f"{where}.source_region.bbox not 4 coords in 0-1")
    return problems


def validate_schema(data: dict, field_names: set[str],
                    columns: list[dict] | None = None) -> list[str]:
    """Shape-check the extraction JSON. Returns a list of problems ([] = ok).

    `field_names` (D26/D27): the profile's schema — its declared fields, or
    GENERIC_SCHEMA_FIELDS for the generic minimal schema. Checks the schema
    contract only (field presence, types, geometry shapes); value correctness
    is the verification stack's job (grounding/arithmetic/format — D30, a
    sanity checker, not a second extractor). Any problem is fed back into a
    retry. Optional per-field extras allowed: `source_region` (D24
    dual-anchored flags), `verification` (the per-field audit record).
    """
    if not isinstance(data, dict):
        # 2026-09-25: models sometimes wrap the object in an array; without
        # this guard the next .get() raised TypeError and SKIPPED the retry
        # (the file failed with "'list' object has no attribute 'get'").
        return [f"response is not a JSON object (got {type(data).__name__})"]
    problems = []
    missing = SCHEMA_FIELDS - set(data)
    if missing:
        problems.append(f"missing top-level keys: {sorted(missing)}")
    for field in ("selected_document", "header_fields", "line_items", "notes"):
        if field not in data:
            continue
        if field == "selected_document" and not isinstance(data[field], dict):
            problems.append("selected_document is not an object")
        if field == "header_fields" and not isinstance(data[field], list):
            problems.append("header_fields is not a list")
        if field == "line_items" and not isinstance(data[field], list):
            problems.append("line_items is not a list")
        if field == "notes" and not isinstance(data[field], list):
            problems.append("notes is not a list")
    for i, f in enumerate(data.get("header_fields", [])):
        if not isinstance(f, dict):
            problems.append(f"header_fields[{i}] is not an object")
            continue
        if f.get("name") not in field_names:
            problems.append(f"header_fields[{i}].name invalid: {f.get('name')!r}")
        if not isinstance(f.get("value"), (str, int, float, dict, type(None))):
            problems.append(f"header_fields[{i}].value type invalid")
        conf = f.get("confidence")
        if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
            problems.append(f"header_fields[{i}].confidence not in 0-1: {conf!r}")
        if not isinstance(f.get("source"), (str, type(None))):
            problems.append(f"header_fields[{i}].source not a string/null")
        region = f.get("source_region")
        if region is not None:
            if not isinstance(region, dict) or not isinstance(region.get("page"), int):
                problems.append(f"header_fields[{i}].source_region needs page: int")
                continue
            bbox = region.get("bbox")
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or not all(isinstance(c, (int, float)) and 0 <= c <= 1 for c in bbox)):
                problems.append(f"header_fields[{i}].source_region.bbox not 4 coords in 0-1")
    columns = columns or list(config.DEFAULT_LINE_ITEM_COLUMNS)
    for i, li in enumerate(data.get("line_items", [])):
        problems.extend(_line_item_problems(li, f"line_items[{i}]", columns))
    status = data.get("status")
    if status not in ("extracted", "flag_no_document_match", "flagged"):
        problems.append(f"status invalid: {status!r}")
    return problems


MULTI_STATUSES = ("extracted_all", "partial", "flag_no_document_match", "flagged")


def _bad_region(region) -> bool:
    """True when a field's source_region is not a valid {page, bbox} block."""
    if region is None:
        return False
    if not isinstance(region, dict) or not isinstance(region.get("page"), int):
        return True
    bbox = region.get("bbox")
    return not (isinstance(bbox, list) and len(bbox) == 4
                and all(isinstance(c, (int, float)) and 0 <= c <= 1 for c in bbox))


def _drop_bad_regions(data: dict, multi: bool) -> list[dict]:
    """Drop source_regions the model got wrong on the FINAL schema attempt.

    D43: an out-of-range/malformed location box must not fail an otherwise-good
    extraction — the VALUES and confidence are kept, the box is removed, and
    the caller emits an `invalid_source_region` review flag per field.
    Returns [{template, name}] for the dropped regions.
    """
    def _drop_rows(line_items, tpl, entry_idx=None) -> None:
        for idx, li in enumerate(line_items or [], start=1):
            if isinstance(li, dict) and _bad_region(li.get("source_region")):
                li["source_region"] = None
                rec = {"template": tpl, "name": f"line_item_{idx}"}
                if entry_idx is not None:
                    rec["entry"] = entry_idx
                dropped.append(rec)

    dropped: list[dict] = []
    if multi:
        for i, entry in enumerate(data.get("documents", [])):
            if not isinstance(entry, dict):
                continue
            for f in entry.get("header_fields", []):
                if isinstance(f, dict) and _bad_region(f.get("source_region")):
                    f["source_region"] = None
                    dropped.append({"template": entry.get("template_id"),
                                    "name": f.get("name"), "entry": i})
            _drop_rows(entry.get("line_items"), entry.get("template_id"), i)
    else:
        for f in data.get("header_fields", []):
            if isinstance(f, dict) and _bad_region(f.get("source_region")):
                f["source_region"] = None
                dropped.append({"template": None, "name": f.get("name")})
        _drop_rows(data.get("line_items"), None)
    return dropped


def _dropped_region_flags(data: dict, cfg: dict, stem: str,
                          doc_id: str) -> list[dict]:
    """Review rows for source_regions dropped on the final schema attempt (D43).

    The value is kept, so the row is an attention flag (no evidence box can be
    drawn) — reason `invalid_source_region`; eval_verification ignores it.
    """
    rows: list[dict] = []
    for drop in (data.get("_meta") or {}).get("dropped_regions") or []:
        tpl = drop.get("template") or doc_id
        name = drop.get("name") or ""
        entry_idx = drop.get("entry", "")
        hf = data.get("header_fields", [])
        line_items = data.get("line_items", [])
        if drop.get("template") and data.get("documents"):
            docs = data["documents"]
            entry = (docs[entry_idx]
                     if isinstance(entry_idx, int) and 0 <= entry_idx < len(docs)
                     else next((d for d in docs
                                if d.get("template_id") == drop["template"]), {}))
            hf = entry.get("header_fields", [])
            line_items = entry.get("line_items", [])
        if name.startswith("line_item_"):
            # D48: a dropped ROW region — value = the row's total column.
            try:
                idx = int(name.rsplit("_", 1)[-1])
            except ValueError:
                idx = 0
            row = line_items[idx - 1] if 0 < idx <= len(line_items) else {}
            amount_col = config.line_item_column(cfg["documents"], tpl, "amount") \
                or {"name": "amount"}
            rows.append({
                "file": stem, "template": tpl, "field": name,
                "value": row.get(amount_col["name"]),
                "confidence": row.get("confidence") or 0.0,
                "threshold": None,
                "reason": "invalid_source_region",
                "sources": f"vision: {row.get('source') or ''}",
                "detail": ("line item row: model returned an invalid source_region "
                           "(page/bbox); row region dropped — review the row "
                           "without a box"),
                "entry": entry_idx,
            })
            continue
        f = next((x for x in hf if x.get("name") == name), {})
        rows.append({
            "file": stem, "template": tpl, "field": name,
            "value": f.get("value"), "confidence": f.get("confidence") or 0.0,
            "threshold": field_threshold(cfg, tpl, name),
            "reason": "invalid_source_region",
            "sources": f"vision: {_field_source(f)}",
            "detail": ("model returned an invalid source_region (page/bbox); "
                       "location dropped — review the value without a box"),
            "entry": entry_idx,
        })
    return rows


def _multi_problems(data: dict, field_sets: dict[str, set[str]],
                    columns_map: dict[str, list[dict]] | None = None) -> list[str]:
    """Shape-check a multi-document extraction. field_sets: template -> field
    names; columns_map: template -> declared line-item columns (D48)."""
    if not isinstance(data, dict):
        return [f"response is not a JSON object (got {type(data).__name__})"]
    problems: list[str] = []
    docs = data.get("documents")
    if not isinstance(docs, list) or not docs:
        return ["documents must be a non-empty list"]
    seen_pages: list[tuple[str, int]] = []
    for i, d in enumerate(docs):
        where = f"documents[{i}]"
        if not isinstance(d, dict):
            problems.append(f"{where} is not an object")
            continue
        tid = d.get("template_id")
        if tid not in field_sets:
            problems.append(f"{where}.template_id must be one of {sorted(field_sets)}")
        if not isinstance(d.get("found"), bool):
            problems.append(f"{where}.found must be a boolean")
        if d.get("uncertain_duplicate") is not None and not isinstance(d.get("uncertain_duplicate"), bool):
            problems.append(f"{where}.uncertain_duplicate must be a boolean")
        sel = d.get("selected_document")
        if sel is not None and not isinstance(sel, dict):
            problems.append(f"{where}.selected_document must be an object/null")
        if isinstance(sel, dict):
            pages = sel.get("document_pages")
            if not isinstance(pages, list) or not all(isinstance(p, int) for p in pages):
                problems.append(f"{where}.selected_document.document_pages must be an int list")
        elif d.get("found") is True:
            problems.append(f"{where}.selected_document must be an object when found is true")
        hf = d.get("header_fields")
        if not isinstance(hf, list):
            problems.append(f"{where}.header_fields must be a list")
        elif tid in field_sets:
            for j, f in enumerate(hf):
                if not isinstance(f, dict):
                    problems.append(f"{where}.header_fields[{j}] is not an object")
                    continue
                if f.get("name") not in field_sets[tid]:
                    problems.append(f"{where}.header_fields[{j}].name must be one of {sorted(field_sets[tid])}")
                if not isinstance(f.get("value"), (str, int, float, dict, type(None))):
                    problems.append(f"{where}.header_fields[{j}].value type invalid")
                conf = f.get("confidence")
                if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
                    problems.append(f"{where}.header_fields[{j}].confidence not in 0-1")
                region = f.get("source_region")
                if region is not None:
                    if not isinstance(region, dict) or not isinstance(region.get("page"), int):
                        problems.append(f"{where}.header_fields[{j}].source_region needs page: int")
                        continue
                    bbox = region.get("bbox")
                    if (not isinstance(bbox, list) or len(bbox) != 4
                            or not all(isinstance(c, (int, float)) and 0 <= c <= 1 for c in bbox)):
                        problems.append(f"{where}.header_fields[{j}].source_region.bbox not 4 coords in 0-1")
        li = d.get("line_items")
        if not isinstance(li, list):
            problems.append(f"{where}.line_items must be a list")
        else:
            cols = (columns_map or {}).get(tid) or list(config.DEFAULT_LINE_ITEM_COLUMNS)
            for j, item in enumerate(li):
                problems.extend(_line_item_problems(
                    item, f"{where}.line_items[{j}]", cols))
        if not isinstance(d.get("notes"), list):
            problems.append(f"{where}.notes must be a list")
    return problems


def _resolve_doc_ids(cfg: dict, doc_ids: list[str]) -> list[str]:
    known = {d["id"] for d in cfg["documents"]["documents"]}
    for did in doc_ids:
        if did not in known:
            raise ValueError(f"unknown document profile {did!r} — rules/documents.yaml")
    return doc_ids


def _resolve_workflow(cfg: dict, workflow_id: str) -> tuple[list[str], list[str], list[dict]]:
    """(doc_ids, expected_ids, cross_rules) for a workflow bundle (2026-09-05;
    cross-document rules added 2026-09-07, D32).

    D52: templates that no longer exist (or became fieldless) are skipped with
    a warning; `expected` keeps the raw ids so missing_expected_document rows
    still flag them. A workflow with NO usable template is a clear error.
    """
    wf = next((w for w in cfg.get("workflows", {}).get("workflows", [])
               if w.get("id") == workflow_id), None)
    if wf is None:
        raise ValueError(f"unknown workflow {workflow_id!r} — rules/workflows.yaml")
    doc_ids = [d for d in wf["documents"]
               if config.profile_fields(cfg["documents"], d)]
    skipped = [d for d in wf["documents"] if d not in doc_ids]
    if skipped:
        logger.warning("workflow %s: skipping missing/fieldless template(s): %s",
                       workflow_id, ", ".join(skipped))
    if not doc_ids:
        raise ValueError(f"workflow {workflow_id!r} has no existing document "
                         f"templates — update rules/workflows.yaml")
    return (doc_ids, list(wf.get("expected") or []),
            list(wf.get("cross_checks") or []))


def missing_expected(data: dict, expected: list[str], doc_id: str) -> list[str]:
    """Expected profile ids without a found entry (workflow completeness)."""
    docs = data.get("documents")
    if isinstance(docs, list) and docs:
        found = {d.get("template_id") for d in docs if d.get("found")}
    else:
        found = {doc_id} if data.get("status") in ("extracted", "flagged",
                                                   "extracted_all", "partial") else set()
    return [e for e in expected if e not in found]


def missing_expected_rows(stem: str, data: dict, expected: list[str], doc_id: str,
                          workflow_label: str) -> list[dict]:
    """Flag rows for the workflow completeness contract (expected-but-absent).

    Shared by run_batch and ui/lib.recompute_flags so a rebuilt flags.csv is
    identical to the one the extraction run wrote.
    """
    return [{
        "file": stem, "template": exp, "field": "selection",
        "value": None, "confidence": "", "threshold": "",
        "reason": "missing_expected_document",
        "sources": f"workflow {workflow_label} expects {exp}",
        "detail": "workflow completeness contract: no found entry "
                  "for this document kind in the file",
    } for exp in missing_expected(data, expected, doc_id)]


def _sum_usage(usage_list: list[dict]) -> dict | None:
    """Sum usage blocks from all HTTP attempts into one token-totals dict.

    `None` when no attempts reported usage (gateway omitted it) — the `_meta`
    key is simply left out in that case.
    """
    if not usage_list:
        return None
    totals: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        totals[key] = sum(u.get(key, 0) or 0 for u in usage_list if isinstance(u, dict))
    elapsed = sum(u.get("elapsed_s", 0) or 0 for u in usage_list if isinstance(u, dict))
    if elapsed:
        totals["elapsed_s"] = round(elapsed, 2)
    return totals


def _excluded_pages(raw_dir: Path) -> set[int]:
    """Pages the vision channel must skip: BLANK only (D62).

    The D16 gate records both `blank` and `poor` exclusions. Blank pages are
    empty paper — nothing to read, never sent. A `poor` page means the OCR
    engine failed on a page whose pixels show real ink (audit: Apple Vision
    returns 0 lines on readable framed pages; sparse/inverted scans miss the
    floors) — exactly where the vision model adds value, so those pages are
    now sent (the text channel still drops them: its OCR output is
    unreliable). The `document_quality` flag is unchanged. [] when no
    report.json (archived runs)."""
    report = raw_dir / "report.json"
    if not report.exists():
        return set()
    try:
        return {e["page"] for e in
                (json.loads(report.read_text(encoding="utf-8"))
                 .get("quality_gate", {}).get("excluded_pages", []))
                if e.get("quality") == "blank"}
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return set()


def page_renders(raw_dir: Path, pages: list[int] | None = None) -> list[tuple[str, str]]:
    """Load per-page vision inputs as (base64, mime), page order.

    Preference order (D21/D24, 2026-08-19; D30 cost amendment):
      0. `pageNNN.render-vision.png` — the capped vision render (2026-09-03,
         `text_layer.py --vision-cap N` A/B: image tokens follow dimensions,
         so the model sees a downscaled copy; OCR/grounding keep the
         full-resolution render). Used when the capture wrote it. (A JPEG q85
         variant measured LARGER than PNG for these text renders, 2026-09-25 —
         PNG stays.)
      1. `pageNNN.image.png` — the RAW embedded image (color, full intrinsic
         resolution, /Rotate applied). The complete photo IS the document; a
         page-bounds render can clip 1px=1pt photos to the page box, cutting
         off the letterhead.
      2. `pageNNN.render-ocr-input.png` — the page render (the image the OCR
         harness reads; for pure-image pages the D20 normalized full-photo
         render; for embedded pages a plain PDFKit page render).
      3. `pageNNN.render-images-only.png` — compat fallback for runs created
         before the D21.1 rename (archived runs stay evaluable).

    FILTERING (2026-09-03, cost + honesty; amended 2026-09-29, D62):
      - BLANK pages (report.json quality_gate.excluded_pages with
        quality=blank) are never sent to the model; `poor` pages ARE sent —
        the OCR engine failed on real ink and the vision model is the tool
        that can still read them (see _excluded_pages);
      - `pages` (the user's PIN) restricts the vision input to exactly the
        pinned pages — the pin IS the identification, the model does not need
        to re-scan the file.
    """
    candidates: list[Path] = []
    for pattern in ("page*.render-vision.png", "page*.image.png",
                    "page*.render-ocr-input.png", "page*.render-images-only.png"):
        candidates = sorted(raw_dir.glob(pattern))
        if candidates:
            break
    excluded = _excluded_pages(raw_dir)
    allowed = set(pages) if pages else None
    renders: list[tuple[str, str]] = []
    for path in candidates:
        try:
            page_no = int(path.stem.split(".")[0].removeprefix("page"))
        except ValueError:
            continue
        if page_no in excluded:
            logger.info("skipping quality-excluded page %d render (D16 gate)", page_no)
            continue
        if allowed is not None and page_no not in allowed:
            continue
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        renders.append((b64, "image/png"))
    return renders


def extract_file(
    stem: str,
    raw_dir: Path,
    *,
    model: str | None = None,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    pinned_pages: list[int] | None = None,
    pinned_label: str | None = None,
    recheck: bool = False,
    retries: int = 2,
    max_tokens: int = 8192,
    timeout_s: float = 300.0,
) -> dict:
    """Extract one file's page renders into structured JSON (D24/D25/D26).

    One schema-constrained call (D1); the model identifies the TARGET DOCUMENT
    profile (D25 — rules/documents.yaml + rendered signals) and extracts —
    the text channel is never consulted here. D26: `pinned_pages` constrains
    extraction to user-picked pages (discovery-driven mode; drift is a hard
    flag in the verification stack), and the schema follows the profile
    (profile fields | generic minimal schema). Returns the full extraction
    dict (includes `_meta` with model, mode, document, pinned, usage). Raises
    llm_client.LLMError / SchemaError if the call fails after all retries.
    """
    cfg = config.load_all()
    model = model or cfg["models"]["primary_model"]
    doc_id = doc_id or cfg["documents"]["documents"][0]["id"]
    doc_ids = doc_ids or [doc_id]
    doc_ids = _resolve_doc_ids(cfg, doc_ids)
    multi = len(doc_ids) > 1
    if multi:
        fieldless = [did for did in doc_ids if not profile_fields(cfg, did)]
        if fieldless:
            raise ValueError(
                f"multi-document extraction needs field-based profiles — "
                f"{', '.join(fieldless)} use the generic minimal schema "
                f"(single-document only)")
    profile = _profile(cfg, doc_id)
    schema = "profile" if profile_fields(cfg, doc_id) else profile["extraction"]["schema"]
    renders = page_renders(raw_dir / stem, pages=pinned_pages)
    if not renders:
        raise llm_client.LLMError(
            f"no vision image (pageNNN.image.png / render-ocr-input.png) in {raw_dir / stem} — "
            f"run text_layer.py (the harness writes renders unconditionally, D24)"
        )

    if multi:
        # Multi-document mode: EVERY listed template is identified + extracted
        # in this one call (each document gets its own array entry). D44: all
        # prompt composition lives in prompts/compose.py.
        line_items_map = {did: _profile(cfg, did)["extraction"].get("line_items", True)
                          for did in doc_ids}
        system, user = compose.build_multi_prompt(
            cfg, doc_ids, line_items_map, pinned_pages=pinned_pages,
            pinned_label=pinned_label, recheck=recheck)
        field_sets = {did: {f["name"] for f in profile_fields(cfg, did)}
                      if profile_fields(cfg, did) else set() for did in doc_ids}
        columns_map = {did: config.line_item_columns(cfg["documents"], did)
                       for did in doc_ids if line_items_map.get(did, True)}
        columns = None
    else:
        fields = profile_fields(cfg, doc_id)
        if fields:
            # D27: profile-defined field schema — prompt schema + notes generated.
            field_names = {f["name"] for f in fields}
        else:
            # D26 generic minimal schema — config guarantees `schema: generic`
            # when a profile declares no fields (fixed_v1 was retired).
            field_names = set(config.GENERIC_SCHEMA_FIELDS)
        system, user = compose.build_single_prompt(
            cfg, doc_id, pinned_pages=pinned_pages,
            pinned_label=pinned_label, recheck=recheck)
        columns_map = None
        columns = config.line_item_columns(cfg["documents"], doc_id) \
            if profile["extraction"].get("line_items", True) and fields else None

    def _finalize(data: dict, attempt_no: int, dropped: list[dict]) -> dict:
        if multi:
            found = [d for d in data.get("documents", []) if d.get("found")]
            if all(d.get("found") for d in data.get("documents", [])):
                data["status"] = "extracted_all"
            elif found:
                data["status"] = "partial"
            elif data.get("status") not in MULTI_STATUSES:
                data["status"] = "flag_no_document_match"
        all_field_names = sorted(set().union(*field_sets.values())) if multi else sorted(field_names)
        data["_meta"] = {"model": model, "attempts": attempt_no, "mode": "vision",
                         "document": doc_id,
                         "documents": doc_ids if multi else [doc_id],
                         "schema": schema if not multi else "multi",
                         "fields": all_field_names,
                         "prompt": compose.prompt_fingerprint(system, user)}
        if dropped:
            data["_meta"]["dropped_regions"] = dropped
        if pinned_pages:
            data["_meta"]["pinned"] = {"pages": pinned_pages,
                                       "label": pinned_label or profile["label"]}
        token_use = _sum_usage(usage)
        if token_use:
            data["_meta"]["usage"] = token_use
        return data

    last_problems = []
    usage: list[dict] = []
    for attempt in range(retries + 1):
        if last_problems:
            user_msg = user + (
                "\n\nYour previous response failed validation. Fix EXACTLY these "
                "problems and return the corrected JSON:\n- "
                + "\n- ".join(last_problems)
            )
        else:
            user_msg = user
        content = llm_client.chat_vision_pages(
            model, user_msg, renders, system=system, json_mode=True,
            max_tokens=max_tokens, timeout_s=timeout_s,
            retries=0,  # llm_client retries transport; schema retry is ours
            usage_out=usage,
        )
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            last_problems.append(f"response was not valid JSON: {exc}")
            logger.warning("%s: retry %d/%d: %s", stem, attempt + 1, retries, last_problems)
            continue
        problems = _multi_problems(data, field_sets, columns_map) if multi \
            else validate_schema(data, field_names, columns)
        if not problems:
            return _finalize(data, attempt + 1, [])
        if attempt == retries:
            # Last attempt: a response whose ONLY problems are malformed
            # source_regions is accepted with the bad boxes dropped (D43) —
            # the values and confidence stay; run_batch emits a review flag
            # per dropped region.
            hard = [p for p in problems if "source_region" not in p]
            if not hard:
                dropped = _drop_bad_regions(data, multi)
                remaining = _multi_problems(data, field_sets, columns_map) if multi \
                    else validate_schema(data, field_names, columns)
                if dropped and not remaining:
                    logger.warning("%s: accepted with %d invalid source_region(s) "
                                   "dropped (values kept; review flags added)",
                                   stem, len(dropped))
                    return _finalize(data, attempt + 1, dropped)
        last_problems = problems
        logger.warning("%s: schema retry %d/%d: %s", stem, attempt + 1, retries, problems)
    raise SchemaError(
        f"{stem}: schema invalid after {retries + 1} attempts: {last_problems}"
    )


def recheck_fields(
    stem: str,
    raw_dir: Path,
    *,
    field_defs: list[dict],
    pages: list[int] | None,
    model: str,
    doc_id: str,
    max_tokens: int = 4096,
    timeout_s: float = 300.0,
    retries: int = 2,
) -> dict:
    """Check-4 NARROW second opinion (2026-09-03, D30 cost lever).

    Re-extracts ONLY the disputed fields from ONLY the disputed pages —
    measured cost of the old full re-extraction (every page, full schema) was
    ~103-113% of the primary prompt; the narrow pass pays for a few fields +
    one page (plus the vision-cap discount). Same independence framing
    (prompts/text.py RECHECK_NOTE / RECHECK_NARROW_SYSTEM): the answer is the
    model's FRESH read, diffed per field by the caller.

    Returns {"header_fields": [...], "usage": {...}, "prompt": {...}} (or
    raises). `prompt` is the D44 provenance fingerprint of this recheck call.
    """
    cfg = config.load_all()
    profile = _profile(cfg, doc_id)
    system, user = compose.build_recheck_prompt(cfg, doc_id, field_defs)
    renders = page_renders(raw_dir / stem, pages=pages)
    if not renders:
        raise llm_client.LLMError(f"no renders for recheck on {stem} (pages {pages})")
    wanted = {fd["name"] for fd in field_defs}

    last_problems: list[str] = []
    usage: list[dict] = []
    for attempt in range(retries + 1):
        user_msg = user + (f"\n\nYour previous response failed validation. "
                           f"Fix EXACTLY these problems and return the corrected "
                           f"JSON:\n- " + "\n- ".join(last_problems)
                           if last_problems else "")
        content = llm_client.chat_vision_pages(
            model, user_msg, renders,
            system=system, json_mode=True,
            max_tokens=max_tokens, timeout_s=timeout_s,
            retries=0, usage_out=usage,
        )
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            last_problems.append(f"response was not valid JSON: {exc}")
            continue
        problems: list[str] = []
        if not isinstance(data, dict):
            last_problems = [f"response is not a JSON object (got {type(data).__name__})"]
            logger.warning("%s: narrow recheck retry %d/%d: %s",
                           stem, attempt + 1, retries, last_problems)
            continue
        hf = data.get("header_fields")
        if not isinstance(hf, list):
            problems.append("header_fields is not a list")
        else:
            names = []
            for i, f in enumerate(hf):
                if not isinstance(f, dict):
                    problems.append(f"header_fields[{i}] is not an object")
                    continue
                if f.get("name") not in wanted:
                    problems.append(f"header_fields[{i}].name must be one of {sorted(wanted)}")
                conf = f.get("confidence")
                if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
                    problems.append(f"header_fields[{i}].confidence not in 0-1")
                region = f.get("source_region")
                if region is not None and not isinstance(region, dict):
                    problems.append(f"header_fields[{i}].source_region must be an object/null")
                names.append(f.get("name"))
            for missing in sorted(wanted - set(names)):
                problems.append(f"missing field '{missing}'")
        if problems:
            last_problems = problems
            logger.warning("%s: narrow recheck retry %d/%d: %s",
                           stem, attempt + 1, retries, problems)
            continue
        token_use = _sum_usage(usage)
        return {"header_fields": hf, "usage": token_use or {},
                "prompt": compose.prompt_fingerprint(system, user)}
    raise SchemaError(f"{stem}: narrow recheck invalid after {retries + 1} attempts: {last_problems}")


def _document_quality_row(stem: str, raw_dir: Path) -> dict | None:
    """Lift the D16 document_quality flag from report.json into a flag row."""
    report = raw_dir / stem / "report.json"
    if not report.exists():
        return None
    try:
        docq = (json.loads(report.read_text(encoding="utf-8"))
                .get("quality_gate", {}).get("document_quality"))
    except (json.JSONDecodeError, OSError):
        return None
    if not docq:
        return None
    return {
        "file": stem, "field": "document_quality", "value": docq.get("value"),
        "confidence": "", "threshold": "", "reason": "document_quality",
        "sources": docq.get("reason") or "", "detail": f"pages {docq.get('pages')}",
    }


def write_flags(flags: list[dict], run_root: Path) -> None:
    """Write <run>/flags.csv (per-run flags, D11/D18). No rows = no file."""
    if not flags:
        return
    flags_path = run_root / "flags.csv"
    with flags_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(FLAG_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in sorted(flags, key=lambda r: (r.get("file", ""), r.get("field", ""))):
            writer.writerow(row)
    logger.info("flags: %d row(s) -> %s", len(flags), flags_path)


def _process_file(
    stem: str, raw_dir: Path, json_dir: Path, cfg: dict, *,
    model: str, doc_id: str, doc_ids: list[str], multi: bool,
    vcfg: dict, vmodel: str, expected: list[str] | None, rules: list[dict],
    workflow: str | None, pinned_pages: list[int] | None,
    pinned_label: str | None, max_tokens: int, timeout_s: float,
    skip_done: bool,
) -> dict:
    """One file through the full pipeline: extraction -> verification ->
    cross-document rules -> gated recheck -> JSON + flags.

    Returns {stem, skipped, failed, flags, verify_totals, rechecks}; the
    batch merges these in submission order. PARALLEL-SAFE (D42): only this
    file's JSON is written here — flags/counters are merged by the caller,
    so `--workers N` runs files concurrently without shared mutable state.
    """
    dest = json_dir / f"{stem}.json"
    flags: list[dict] = []
    vtotals: dict[str, int] = {}
    n_rechecks = 0
    if skip_done and dest.exists() and (raw_dir / stem).exists():
        try:
            prior = json.loads(dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = None
        if prior is not None and prior.get("status") != "failed":
            logger.info("%s: skipped (done, status %s) — --skip-done",
                        stem, prior.get("status"))
            return {"stem": stem, "skipped": True, "failed": False,
                    "flags": [], "verify_totals": {}, "rechecks": 0}
    try:
        data = extract_file(stem, raw_dir, model=model, doc_id=doc_id,
                            doc_ids=doc_ids if multi else None,
                            pinned_pages=pinned_pages, pinned_label=pinned_label,
                            max_tokens=max_tokens, timeout_s=timeout_s)
    except Exception as exc:
        logger.error("%s: extraction failed: %s", stem, exc)
        dest.write_text(json.dumps({
            "file": stem, "status": "failed",
            "notes": [f"extraction failed: {exc}"],
        }, indent=2), encoding="utf-8")
        return {"stem": stem, "skipped": False, "failed": True,
                "flags": [], "verify_totals": {}, "rechecks": 0}
    # The recheck cap is PER FILE (cost discipline: at most
    # recheck_max_calls second-opinion calls per file, not per batch).
    rechecks_used = 0
    sel = data.get("selected_document") or {}

    # Verification stack (offline checks) + selection guards. Single-document:
    # the file-level verify_file. Multi-document: per-entry verification +
    # the duplicate/page-overlap backstop.
    if multi:
        verif_rows, vrec = verify_mod.verify_multidoc(
            stem, data, raw_dir, cfg, doc_ids, pinned=pinned_pages)
    else:
        verif_rows, vrec = verify_mod.verify_file(
            stem, data, raw_dir, cfg, doc_id, pinned=pinned_pages)
    for row in verif_rows:
        row.setdefault("template", doc_id)
    flags.extend(verif_rows)
    for check, n in vrec.get("summary", {}).get("failed_by_check", {}).items():
        vtotals[check] = vtotals.get(check, 0) + n

    # Cross-document validation rules (2026-09-07, D32): workflow-declared
    # equalities between a field on one document entry and a field on
    # another entry of the SAME file. Deterministic, zero cost, per-side
    # dual-anchored flag rows; audit in _meta.verification.cross_checks.
    # Runs only in multi-document (workflow) mode — single-document runs
    # have nothing to cross. No gated recheck (both values come from the
    # same primary call; each side's own grounding already guards its read).
    cross_record: dict = {}
    if multi and rules:
        cross_rows, cross_record = verify_mod.check_cross_document(
            stem, data, cfg, rules)
        for row in cross_rows:
            row.setdefault("template", doc_id)
        flags.extend(cross_rows)
        n_failed = sum(1 for r in cross_record.values()
                       if r.get("evaluable") and r.get("passed") is False)
        if n_failed:
            vtotals["cross_document"] = vtotals.get("cross_document", 0) + n_failed

    # Gated Check 4 (disagreement): a NARROW recheck per disputed document
    # (multi) or of the file (single) — disputed fields only, from the
    # pages their regions cite — capped by recheck_max_calls per file.
    disputed_groups: list[tuple[str, list[str], list[int], list[dict]]] = []
    if multi:
        ver_docs = vrec.get("documents", {}) or {}
        for entry in data.get("documents", []):
            rec = ver_docs.get(entry.get("template_id")) or {}
            names = [n for n, fr in (rec.get("fields") or {}).items()
                     if fr.get("level") != "accept"]
            if not names:
                continue
            hf = entry.get("header_fields", [])
            pages = sorted({int(((f.get("source_region") or {}).get("page") or 1))
                            for f in hf if f.get("name") in names})
            disputed_groups.append((entry.get("template_id"), names, pages, hf))
    else:
        names = [fname for fname, rec in vrec["fields"].items()
                 if rec["level"] != "accept"]
        if names:
            disputed_groups.append((doc_id, names, [], data.get("header_fields", [])))
    max_rechecks = int(vcfg["recheck_max_calls"])
    recheck_record: dict = {"available": bool(vmodel),
                            "triggered": False, "max_calls": max_rechecks}
    if disputed_groups and vmodel and max_rechecks > 0:
        recheck_record["fields"] = {}
        sec_usages: list[dict] = []
        for tpl, dnames, pages0, hfields in disputed_groups:
            if rechecks_used >= max_rechecks:
                break
            try:
                # NARROW recheck (2026-09-03): disputed fields only, from the
                # pages their regions cite (force-constrained to the pin in
                # pinned mode). The full re-extraction re-read every page at
                # the primary's price — measured ~103-113% overhead.
                disputed_defs = [fd for fd in profile_fields(cfg, tpl)
                                 if fd["name"] in dnames]
                recheck_pages: set[int] = set(pinned_pages or []) or set(pages0)
                if not recheck_pages:
                    for f in hfields:
                        region = f.get("source_region") or {}
                        if f.get("name") in dnames and region.get("page"):
                            recheck_pages.add(int(region["page"]))
                if disputed_defs:
                    narrow = recheck_fields(
                        stem, raw_dir, field_defs=disputed_defs,
                        pages=sorted(recheck_pages) or None,
                        model=vmodel, doc_id=tpl,
                        max_tokens=min(max_tokens, 4096), timeout_s=timeout_s)
                    secondary = {"header_fields": narrow["header_fields"]}
                    sec_usage = narrow.get("usage") or {}
                    recheck_record["prompt"] = narrow.get("prompt")
                    recheck_record["mode"] = "narrow" + ("" if not multi else "-multi")
                else:
                    # No profile field defs (legacy schema) — full fallback.
                    secondary = extract_file(stem, raw_dir, model=vmodel, doc_id=tpl,
                                             pinned_pages=pinned_pages,
                                             pinned_label=pinned_label,
                                             recheck=True,
                                             max_tokens=max_tokens, timeout_s=timeout_s)
                    sec_usage = (secondary.get("_meta") or {}).get("usage") or {}
                    recheck_record["prompt"] = (secondary.get("_meta") or {}).get("prompt")
                    recheck_record["mode"] = "full"
            except Exception as exc:
                logger.warning("%s: gated recheck failed: %s (first-pass flags stand)",
                               stem, exc)
                continue
            rechecks_used += 1
            n_rechecks += 1
            recheck_record["triggered"] = True
            recheck_record["model"] = vmodel
            recheck_record["attempts"] = recheck_record.get("attempts", 0) + 1
            if sec_usage:
                sec_usages.append(sec_usage)
            sec_fields = {f["name"]: f.get("value")
                          for f in secondary.get("header_fields", [])}
            kinds = profile_field_kinds(cfg, tpl)
            for name in dnames:
                primary_val = next((f.get("value") for f in hfields
                                    if f.get("name") == name), None)
                secondary_val = sec_fields.get(name)
                f = next(f for f in hfields if f["name"] == name)
                agree = secondary_val not in (None, "") and values_equal(
                    primary_val, secondary_val, kinds.get(name, "str"))
                recheck_record["fields"][name] = {
                    "primary": primary_val, "secondary": secondary_val,
                    "agree": agree,
                }
                recheck = verify_mod.verify_field(
                    stem, raw_dir, f, {f["name"]: f for f in hfields},
                    data.get("line_items", []),
                    [r for r in validate_mod.profile_arithmetic_rules(cfg, tpl)],
                    cfg, tpl)
                dcheck = {
                    "check": "disagreement", "passed": bool(agree),
                    "score": 1.0 if agree else 0.0,
                    "detail": (f"secondary ({vmodel}) "
                               f"{'AGREES' if agree else 'DISAGREES'}: "
                               f"{secondary_val!r}"),
                }
                verdict = aggregate.aggregate(
                    name, recheck["checks"] + [dcheck],
                    weights=vcfg["check_weights"],
                    hard_fail=tuple(vcfg["hard_fail_checks"]),
                    accept=vcfg["accept_score"], review=vcfg["review_score"])
                f.setdefault("verification", {})["recheck"] = {
                    "agree": agree, "level": verdict.level}
                if not agree:
                    flags.append({
                        "file": stem, "template": tpl, "field": name,
                        "value": f.get("value"), "confidence": f.get("confidence") or 0.0,
                        "threshold": None,
                        "reason": "verification_disagreement",
                        "sources": (_field_source(f)
                                    + f"; secondary {vmodel}: {secondary_val!r}"),
                        "detail": dcheck["detail"],
                    })
        if sec_usages:
            recheck_record["usage"] = _sum_usage(sec_usages)
    data.setdefault("_meta", {})["verification"] = vrec
    if recheck_record["triggered"] or recheck_record["available"]:
        data["_meta"]["verification"]["recheck"] = recheck_record
    if cross_record:
        data["_meta"]["verification"]["cross_checks"] = cross_record

    # D43: bad location boxes dropped on the final schema attempt get one
    # review flag per field — the VALUE is kept, the box is gone.
    flags.extend(_dropped_region_flags(data, cfg, stem, doc_id))

    # Workflow completeness contract: every EXPECTED profile must have a
    # found entry (expected may exceed documents — "extract 3, expect 4").
    if expected:
        flags.extend(missing_expected_rows(
            stem, data, expected, doc_id, str(workflow or doc_ids)))
    if workflow:
        data.setdefault("_meta", {})["workflow"] = {
            "id": workflow, "documents": doc_ids, "expected": expected or []}
    docq = _document_quality_row(stem, raw_dir)
    if docq:
        flags.append(docq)
    dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if multi:
        first_found = next((d for d in data.get("documents", []) if d.get("found")), {})
        document_name = selected_name(first_found.get("selected_document"))
        n_docs = len(data.get("documents", []))
        logger.info(
            "%s: %s (%s) %d document(s), %d attempt(s) — verification %s%s",
            stem, data.get("status"), document_name, n_docs,
            data.get("_meta", {}).get("attempts"),
            vrec.get("summary", {}).get("flag_rows", 0),
            f"; recheck {recheck_record.get('fields', {})}" if recheck_record.get("triggered") else "",
        )
    else:
        logger.info(
            "%s: %s (%s) %d fields, %d line items, %d attempt(s) — verification %s%s",
            stem, data.get("status"), selected_name(sel),
            len(data.get("header_fields", [])), len(data.get("line_items", [])),
            data.get("_meta", {}).get("attempts"),
            vrec.get("summary", {}).get("failed_by_check", {}),
            f"; recheck {recheck_record.get('fields', {})}" if recheck_record.get("triggered") else "",
        )
    return {"stem": stem, "skipped": False, "failed": False,
            "flags": flags, "verify_totals": vtotals, "rechecks": n_rechecks}


def run_batch(raw_dir: Path, json_dir: Path, *, only: str | None = None, model: str | None = None,
              doc_id: str | None = None, doc_ids: list[str] | None = None,
              workflow: str | None = None,
              expected: list[str] | None = None,
              skip_done: bool = False,
              pinned_pages: list[int] | None = None, pinned_label: str | None = None,
              files_filter: set[str] | None = None,
              max_tokens: int = 8192, timeout_s: float = 300.0,
              workers: int = 1,
              verification_model: str | None = None) -> int:
    """Extract every corpus file: vision pass -> verification stack -> flags.

    Per file: one vision LLM call (primary, D24), then the deterministic
    verification stack (verify.py — grounding/arithmetic/format -> per-field
    verdict, 2026-09-02, replacing the retired rules locator + crosscheck),
    the selection guards (no_document_match / sub-min_score / pin-drift),
    the GATED Check 4 (cheap-model recheck on flagged fields only, capped per
    document — the disagreement check; D24 cost discipline), and per-run flag
    rows. Every check result is kept in `_meta.verification` (audit), the
    full extraction JSON keeps the primary model's values + confidence
    untouched (D7 — verification never silently re-scores). Per-file failure
    isolation: a failed file is logged and recorded as minimal JSON with
    status "failed" — the batch continues.

    MULTI-DOCUMENT MODE (2026-09-05): `doc_ids` with 2+ profile ids selects
    every listed kind in the same one call; each found document gets its own
    `documents[]` entry tagged with its template_id; verification runs per
    entry (its own template's rules) plus the deterministic duplicate/
    page-overlap backstop; status = extracted_all | partial |
    flag_no_document_match. Flags carry the `template` column.

    CROSS-DOCUMENT RULES (2026-09-07, D32): a workflow's `cross_checks`
    declare equalities between a field on one entry and a field on another
    entry of the same file ("BOE number on the broker invoice must equal the
    BOE number on the attached BOE") — evaluated deterministically per file,
    per-side dual-anchored flag rows, audit in _meta.verification.cross_checks.

    PARALLELISM (2026-09-24, D42): `workers > 1` runs the files concurrently
    (`_process_file` is parallel-safe: each file writes only its own JSON and
    returns its flags/counters, merged here in submission order). Same number
    of paid calls, same cost; a batch wall-clock win.
    """
    cfg = config.load_all()
    model = model or cfg["models"]["primary_model"]
    # D52: dangling workflow references are warnings, not failures — surface
    # them once per batch so the operator sees them.
    for w in (cfg.get("workflows", {}).get("warnings") or []):
        logger.warning("config: %s", w)
    # Preflight (D35): warn — never fail — when the cached catalog snapshot no
    # longer lists the model (removed/deprecated). No network call here.
    known = model_catalog.cached_catalog_ids(cfg["models"]["endpoint"])
    if known and model not in known:
        logger.warning("model %r is not in the cached catalog snapshot — it may have "
                       "been removed (check `make models`)", model)
    rules: list[dict] = []
    if workflow:
        # A workflow's documents list is the target set — including a
        # single-document workflow, where the profile is NOT the default one.
        doc_ids, expected, rules = _resolve_workflow(cfg, workflow)
        doc_id = doc_ids[0]
    elif doc_ids:
        doc_ids = _resolve_doc_ids(cfg, doc_ids)
        doc_id = doc_ids[0]
    else:
        doc_id = doc_id or cfg["documents"]["documents"][0]["id"]
        doc_ids = _resolve_doc_ids(cfg, [doc_id])
    multi = len(doc_ids) > 1
    vcfg = cfg["thresholds"]["verification"]
    # Explicit per-run override (desktop backup-model setting): "" disables the
    # gated recheck; None keeps the config default (CLI behavior unchanged).
    vmodel = (verification_model if verification_model is not None
              else (cfg["models"].get("verification_model") or ""))
    json_dir.mkdir(parents=True, exist_ok=True)
    stems = sorted(d.name for d in raw_dir.iterdir() if d.is_dir())
    if not stems:
        logger.info("no raw dirs under %s", raw_dir)
        return 0
    ok = failed = skipped = rechecks = 0
    all_flags: list[dict] = []
    verify_totals: dict[str, int] = {}
    stems = [s for s in stems
             if only is None or s == only]  # single-file override
    if files_filter is not None:
        stems = [s for s in stems if s in files_filter]

    def _work(s: str) -> dict:
        return _process_file(
            s, raw_dir, json_dir, cfg, model=model, doc_id=doc_id,
            doc_ids=doc_ids, multi=multi, vcfg=vcfg, vmodel=vmodel,
            expected=expected, rules=rules, workflow=workflow,
            pinned_pages=pinned_pages, pinned_label=pinned_label,
            max_tokens=max_tokens, timeout_s=timeout_s, skip_done=skip_done)

    workers = max(1, int(workers or 1))
    if workers > 1 and len(stems) > 1:
        from concurrent.futures import ThreadPoolExecutor
        logger.info("extracting %d file(s) with %d parallel worker(s) — "
                    "same calls, same cost", len(stems), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_work, stems))  # input order preserved
    else:
        results = [_work(s) for s in stems]
    for res in results:
        if res["skipped"]:
            skipped += 1
            continue
        if res["failed"]:
            failed += 1
            continue
        ok += 1
        rechecks += res["rechecks"]
        all_flags.extend(res["flags"])
        for check, n in res["verify_totals"].items():
            verify_totals[check] = verify_totals.get(check, 0) + n
    write_flags(all_flags, json_dir.parent)
    _write_summary(json_dir, raw_dir, doc_ids=doc_ids, multi=multi,
                   workflow=workflow, expected=expected)
    logger.info(
        "extraction batch done: %d ok, %d failed, %d skipped (vision %s, "
        "document %s%s%s); verification failures by check: %s; rechecks: %d",
        ok, failed, skipped, model, doc_id,
        f", workflow {workflow}" if workflow else "",
        f", pinned pages {pinned_pages}" if pinned_pages else "",
        verify_totals, rechecks,
    )
    return 0 if failed == 0 else 1


def _write_summary(json_dir: Path, raw_dir: Path, *, doc_ids: list[str],
                   multi: bool, workflow: str | None, expected: list[str] | None) -> None:
    """<run>/summary.csv — one row per (file, template) (2026-09-05 bulk)."""
    try:
        dirs = sorted(d for d in raw_dir.iterdir() if d.is_dir())
    except OSError:
        return
    flag_counts = _flag_counts(json_dir)
    rows: list[dict] = []
    for stem in dirs:
        jp = json_dir / f"{stem.name}.json"
        if not jp.exists():
            continue
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = data.get("status", "failed")
        if status == "failed":
            rows.append({"file": stem.name, "template": "", "found": "failed",
                         "document_pages": "", "identification": "",
                         "primary_number": "", "line_items": 0,
                         "flags": 0, "status": status})
            continue
        entries = data.get("documents") if multi else [
            {"template_id": doc_ids[0], "found": status not in (
                "flag_no_document_match", "failed"),
             "selected_document": data.get("selected_document") or {},
             "header_fields": data.get("header_fields") or [],
             "line_items": data.get("line_items") or []}]
        for entry in entries:
            sel = entry.get("selected_document") or {}
            fields = entry.get("header_fields") or []
            primary = next((f.get("value") for f in fields
                            if f.get("value") not in (None, "")), "")
            pages = sel.get("document_pages") or []
            rows.append({
                "file": stem.name,
                "template": entry.get("template_id", ""),
                "found": "yes" if entry.get("found") else "no",
                "document_pages": ",".join(map(str, pages)),
                "identification": selected_name(sel) or "",
                "primary_number": str(primary),
                "line_items": len(entry.get("line_items") or []),
                "flags": flag_counts.get(stem.name, 0),
                "status": status,
            })
    cols = ("file", "template", "found", "document_pages", "identification",
            "primary_number", "line_items", "flags", "status")
    try:
        with (json_dir.parent / "summary.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        logger.warning("summary.csv write failed: %s", exc)


def _flag_counts(json_dir: Path) -> dict[str, int]:
    """Flag rows in <run>/flags.csv by file (one local recount per summary)."""
    flags_path = json_dir.parent / "flags.csv"
    counts: dict[str, int] = {}
    if not flags_path.exists():
        return counts
    with flags_path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            name = r.get("file")
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def run_catalog(raw_dir: Path, catalog_dir: Path, *, model: str | None = None,
                files_filter: set[str] | None = None,
                max_tokens: int = 2048, timeout_s: float = 120.0) -> int:
    """D26 discovery sweep: ONE cheap call per file that ONLY lists documents.

    Catalog output per stem: <catalog_dir>/<stem>.json =
    {file, documents: [{type, label, pages}]} (or {file, status: failed}).
    The sweep is advisory — user pinning is the source of truth; extraction is
    never gated on it. Per-file failure isolation like run_batch.
    `files_filter` scopes the sweep like run_batch's (the UI passes the
    just-uploaded stems).
    """
    cfg = config.load_all()
    model = model or cfg["models"]["primary_model"]
    catalog_dir.mkdir(parents=True, exist_ok=True)
    stems = sorted(d.name for d in raw_dir.iterdir() if d.is_dir())
    if files_filter is not None:
        stems = [s for s in stems if s in files_filter]
    if not stems:
        logger.info("no raw dirs under %s", raw_dir)
        return 0
    ok = failed = 0
    for stem in stems:
        dest = catalog_dir / f"{stem}.json"
        try:
            renders = page_renders(raw_dir / stem)
            if not renders:
                raise llm_client.LLMError(f"no renders for {stem} — run text_layer first")
            usage: list[dict] = []
            catalog_system, catalog_user = compose.build_catalog_prompt()
            content = llm_client.chat_vision_pages(
                model, catalog_user, renders,
                system=catalog_system, json_mode=True,
                max_tokens=max_tokens, timeout_s=timeout_s, retries=0, usage_out=usage,
            )
            data = json.loads(content)
            docs_a = data.get("documents")
            if not isinstance(docs_a, list):
                raise SchemaError(f"{stem}: catalog response has no documents list")
            clean = []
            for dd in docs_a:
                if not isinstance(dd, dict):
                    raise SchemaError(f"{stem}: catalog document entry is not an object")
                pages = [int(pp) for pp in (dd.get("pages") or [])]
                clean.append({"type": dd.get("type"), "label": dd.get("label"),
                              "pages": pages})
            out = {"file": stem, "documents": clean}
            token_use = _sum_usage(usage)
            out["_meta"] = {"model": model,
                            "prompt": compose.prompt_fingerprint(catalog_system, catalog_user)}
            if token_use:
                out["_meta"]["usage"] = token_use
            dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
            ok += 1
            logger.info("%s: catalog: %s", stem, ", ".join(
                f"{c['type']} ({c['label']}) pages {c['pages']}" for c in clean))
        except Exception as exc:
            logger.error("%s: catalog failed: %s", stem, exc)
            dest.write_text(json.dumps({"file": stem, "status": "failed",
                                        "error": str(exc)}, indent=2), encoding="utf-8")
            failed += 1
    logger.info("catalog batch done: %d ok, %d failed (vision %s)", ok, failed, model)
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract.py",
        description="Vision-primary extraction: one schema-constrained call per "
                    "attachment (selection + extraction) + verification stack + "
                    "flags. Bulk workflows: --workflow <id> (rules/workflows.yaml).",
    )
    parser.add_argument("raw_dir", nargs="?", help="out/latest/raw (per-file subdirs with renders + text)")
    parser.add_argument("json_dir", nargs="?", help="output dir for out/latest/json/<stem>.json")
    parser.add_argument("--file", help="process only this stem (directory name in raw_dir)")
    parser.add_argument("--model", help="override primary_model from config/models.yaml "
                                        "(model-swap verification: corpus + `make eval`)")
    parser.add_argument("--document", default=None,
                        help="target document profile id from rules/documents.yaml "
                             "(default: first profile — currently broker_invoice; D25)")
    parser.add_argument("--documents", default=None, metavar="ID1,ID2",
                        help="MULTI-DOCUMENT mode (2026-09-05): comma-separated profile ids "
                             "to extract in ONE call (e.g. profile_a,profile_b); every "
                             "matching document is reported as its own entry tagged with "
                             "template_id")
    parser.add_argument("--workflow", default=None, metavar="ID",
                        help="BULK WORKFLOW (2026-09-05): a named bundle from "
                             "rules/workflows.yaml — resolves to its documents list "
                             "(--documents) and raises missing_expected_document rows "
                             "for the workflow's expected contract. Mutually exclusive "
                             "with --document/--documents")
    parser.add_argument("--files", default=None, metavar="STEM1,STEM2",
                        help="process ONLY these raw stems (2026-09-05: the UI "
                             "scopes a run to the just-uploaded files)")
    parser.add_argument("--skip-done", action="store_true",
                        help="resume: skip stems whose <json>/<stem>.json exists and is "
                             "not failed (retries failures + new files only)")
    parser.add_argument("--catalog", action="store_true",
                        help="D26 discovery sweep: list the documents in each file "
                             "(one cheap call/file) into <json_dir.parent>/catalog/, no extraction")
    parser.add_argument("--pin-pages", default=None, metavar="1-3,5",
                        help="D26: extract ONLY from these pages (user-pinned document); "
                             "drift off the pin is a hard selection_conflict flag")
    parser.add_argument("--pin-label", default=None, metavar="LABEL",
                        help="D26: description of the pinned document (shown to the model; "
                             "defaults to the profile label)")
    parser.add_argument("--max-tokens", type=int, default=8192,
                        help="max completion tokens per call (trimmed 2026-09-03: "
                             "observed completions peak ~2.2k; 8192 = 4x headroom. "
                             "The 16384 default predates the verification stack)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="per-call timeout in seconds (raised 2026-08-26: qwen3.8-max "
                             "took ~30s per page — 120s failed 15-16-page files)")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel files (default 1; D42): each file is an independent "
                             "vision call, so N workers cut batch wall-clock at the SAME "
                             "cost — 429s are absorbed by the client's backoff")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def parse_args(argv=None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.raw_dir or not args.json_dir:
        parser.print_help(sys.stderr)
        return 64
    files_filter = None
    if args.files:
        files_filter = {s.strip() for s in args.files.split(",") if s.strip()}
    elif args.file:
        # D46 bug fix: `--file <stem>` must scope the catalog sweep too — the
        # UI's per-file "What's in this file?" appended --file and silently
        # paid for a call on EVERY file in the run (files_filter was only
        # built from --files).
        files_filter = {args.file}
    if args.catalog:
        return run_catalog(Path(args.raw_dir), Path(args.json_dir).parent / "catalog",
                           model=args.model, files_filter=files_filter,
                           max_tokens=args.max_tokens, timeout_s=args.timeout)
    pinned = None
    if args.pin_pages:
        try:
            pinned = parse_pin_pages(args.pin_pages)
        except ValueError as exc:
            parser.error(f"--pin-pages: {exc}")
    doc_ids = None
    if args.documents:
        doc_ids = [d.strip() for d in args.documents.split(",") if d.strip()]
    if args.workflow and (args.document or doc_ids):
        parser.error("--workflow is mutually exclusive with --document/--documents")
    if args.document and doc_ids:
        parser.error("--document is mutually exclusive with --documents")
    return run_batch(Path(args.raw_dir), Path(args.json_dir), only=args.file,
                     model=args.model, doc_id=args.document, doc_ids=doc_ids,
                     workflow=args.workflow,
                     files_filter=files_filter,
                     skip_done=args.skip_done,
                     pinned_pages=pinned, pinned_label=args.pin_label,
                     max_tokens=args.max_tokens, timeout_s=args.timeout,
                     workers=args.workers)


if __name__ == "__main__":
    sys.exit(main())
