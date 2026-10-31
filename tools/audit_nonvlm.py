#!/usr/bin/env python3
"""Non-VLM subsystem audit (2026-09-29) — routing / blank / quality /
orientation / grounding. Local only, zero LLM calls.

Sections:
  fixtures   build + process every synthetic adversarial PDF; per-page table
  replay     re-run the harness over the corpus and diff against the baseline
             capture (routing / quality / orientation / geometry regressions)
  grounding  fuzz check_grounding + locate_value over the baseline text channel
             (coordinate shifts, wrong page, row-band, value-format edge cases)
  packet     copy every orientation-corrected render for owner labelling +
             write orientation_labels.json (the ground truth for the P0)

Usage: .venv/bin/python tools/audit_nonvlm.py [--out DIR] [--only fixtures,...]
Default out: out/audit/nonvlm-<YYYY-MM-DD>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

BASELINE_RAW = REPO / "out" / "runs" / "2026-09-03" / "5" / "raw"
CORPUS = REPO / "invoices"
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import nonvlm_util as U  # noqa: E402
import grounding  # noqa: E402
from compare import norm_date  # noqa: E402


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# --- fixtures ---------------------------------------------------------------

def section_fixtures(out: Path) -> dict:
    fixtures = U.build_fixtures(out / "fixtures" / "pdfs")
    raws = out / "fixtures" / "raw"
    reports = U.process_all(fixtures, raws)
    rows = []
    for name, rep in sorted(reports.items()):
        p = rep["pages_report"][0]
        sig = p.get("signals", {})
        text = U.page_text(raws / name)
        gate = rep.get("quality_gate") or {}
        rows.append({
            "fixture": name, "source": p.get("source"),
            "quality": p.get("quality"), "quality_reason": p.get("quality_reason"),
            "lines": p.get("lines"), "words": p.get("words"),
            "orientation": p.get("orientation"),
            "orientation_scores": p.get("orientation_scores"),
            "render": p.get("render"), "render_note": p.get("render_note"),
            "marker_order": U.marker_order(text),
            "excluded": gate.get("excluded_pages"),
            "signals": {k: sig.get(k) for k in (
                "chars", "alpha", "words", "invisible_text", "text_ops",
                "image_coverage", "images", "image_decode_failures",
                "has_large_image", "top_level_images")},
            "text_head": text[:160],
        })
    _write(out / "fixtures" / "fixtures.json", rows)
    print("== fixtures ==")
    for r in rows:
        print(f"  {r['fixture']:22} src={str(r['source']):8} q={str(r['quality']):5} "
              f"lines={r['lines']} words={r['words']} orient={r['orientation']} "
              f"order={r['marker_order']}")
        if r["quality"] != "good":
            print(f"  {'':22} reason: {r['quality_reason']}")
    return {"rows": rows}


# --- corpus replay ----------------------------------------------------------

def section_replay(out: Path) -> dict:
    if not BASELINE_RAW.is_dir():
        print("== replay: no baseline capture, skipped ==")
        return {}
    replay = out / "replay" / "raw"
    print("== replay: re-running the harness on the corpus (local, no LLM) ==")
    diffs = []
    for pdf in sorted(CORPUS.glob("*.pdf")):
        report = U.process(pdf, replay / pdf.stem)
        base_path = BASELINE_RAW / pdf.stem / "report.json"
        base = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
        bpages = {p["page"]: p for p in base.get("pages_report", [])}
        for p in report["pages_report"]:
            b = bpages.get(p["page"], {})
            fields = ("source", "quality", "orientation", "embedded_geometry",
                      "lines", "words", "render", "render_note")
            delta = {f: [b.get(f), p.get(f)] for f in fields
                     if b.get(f) != p.get(f)}
            bs, ps = b.get("signals", {}), p.get("signals", {})
            for k in ("chars", "words", "alpha", "image_coverage", "invisible_text",
                      "text_ops", "has_large_image", "images"):
                if bs.get(k) != ps.get(k):
                    delta[f"signals.{k}"] = [bs.get(k), ps.get(k)]
            if delta:
                diffs.append({"file": pdf.stem, "page": p["page"], "diff": delta})
    _write(out / "replay" / "diff.json", diffs)
    print(f"  per-page diffs vs baseline: {len(diffs)}")
    for d in diffs[:40]:
        print(f"    {d['file']} p{d['page']}: {d['diff']}")
    return {"diffs": diffs}


# --- orientation packet (owner labelling) -----------------------------------

def section_packet(out: Path) -> dict:
    src = out / "replay" / "raw"
    if not src.is_dir():
        src = BASELINE_RAW
    pdir = out / "orientation"
    pdir.mkdir(parents=True, exist_ok=True)
    pages = []
    for report_path in sorted(src.glob("*/report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        stem = report_path.parent.name
        for p in report.get("pages_report", []):
            if p.get("orientation") in (None, "upright"):
                continue
            render = report_path.parent / f"page{p['page']:03d}.render-ocr-input.png"
            if not render.exists():
                continue
            dest = pdir / f"{stem}_p{p['page']:03d}_{p['orientation']}.png"
            shutil.copyfile(render, dest)
            pages.append({
                "file": stem, "page": p["page"],
                "chosen_delta": p["orientation"],
                "tall_fraction": p.get("orientation_tall_fraction"),
                "scores": p.get("orientation_scores"),
                "render": dest.name,
                "label": "",
            })
    labels = {
        "_instructions": (
            "Open each PNG. The harness KEPT the page in its original frame "
            "(D61: it never rotates — the 90/270 choice is OCR noise). Label "
            "what the image SHOWS: 'upright' if the print reads normally, "
            "'sideways' if it is rotated 90/270, 'upside-down' if it is 180 "
            "off. Optional note: the true correction you would apply."),
        "pages": pages,
    }
    _write(pdir / "orientation_labels.json", labels)
    print(f"== orientation packet: {len(pages)} corrected pages -> {pdir} ==")
    for p in pages:
        print(f"  {p['file']} p{p['page']} chosen={p['chosen_delta']} "
              f"scores={p['scores']} -> {p['render']}")
    return {"pages": pages}


# --- grounding fuzz ---------------------------------------------------------

def _claim(bbox_bl: list[float], dx: float = 0.0, dy: float = 0.0,
           page: int | None = None) -> dict:
    """Top-left claim bbox from a bottom-left span bbox (+offset)."""
    x0, y0, x1, y1 = bbox_bl
    tl = [x0, 1.0 - y1, x1, 1.0 - y0]
    tl = [tl[0] + dx, tl[1] + dy, tl[2] + dx, tl[3] + dy]
    return {"page": page, "bbox": tl}


def section_grounding(out: Path) -> dict:
    if not BASELINE_RAW.is_dir():
        print("== grounding: no baseline capture, skipped ==")
        return {}
    import config
    from validate import value_type_for

    cfg = config.load_all()
    rows = []
    for jp in sorted((REPO / "out" / "runs" / "2026-09-03" / "5" / "json").glob("*.json")):
        data = json.loads(jp.read_text(encoding="utf-8"))
        stem = jp.stem
        for f in data.get("header_fields", []):
            reg = f.get("source_region") or {}
            if not reg.get("bbox"):
                continue
            page = int(reg.get("page") or 1)
            spans = grounding._load_spans(BASELINE_RAW, stem, page)
            value = f.get("value")
            if not spans or value in (None, ""):
                continue
            kind = grounding.KIND_BY_TYPE.get(
                value_type_for(cfg, "broker_invoice", f["name"]), "code")
            # find the span whose text carries the value (if any)
            hit = next((s for s in spans
                        if grounding.containment.exact_containment(str(value), s["text"], kind)),
                       None)
            if hit is None:
                continue
            base_bbox = [hit["x0"], hit["y0"], hit["x1"], hit["y1"]]
            cases = {
                "own_span": _claim(base_bbox, page=page),
                "shift_3pct": _claim(base_bbox, dx=0.03, dy=0.03, page=page),
                "shift_8pct": _claim(base_bbox, dx=0.08, dy=0.08, page=page),
                "shift_25pct": _claim(base_bbox, dx=0.25, dy=0.25, page=page),
                "wrong_page": _claim(base_bbox, page=page + 1),
                "row_opposite_x": _claim([0.01, base_bbox[1], 0.03, base_bbox[3]], page=page),
            }
            for case, claim in cases.items():
                g = grounding.check_grounding(
                    stem, claim["page"], value, claim["bbox"], BASELINE_RAW,
                    margin=0.035, search_margin=0.12, loose_score=0.6,
                    ngram_k=3, kind=kind)
                rows.append({"stem": stem, "field": f["name"], "value": value,
                             "case": case, "passed": g["passed"] if g else None,
                             "score": g["score"] if g else None})
    # value-format edge cases against real span texts (first amount + date hit
    # anywhere in the baseline capture)
    edge_rows = []
    import re
    amount_hit = date_hit = None
    for report_path in sorted(BASELINE_RAW.glob("*/report.json")):
        for seg in sorted(report_path.parent.glob("page*.segments.txt")):
            spans = grounding._parse_spans_text(seg.read_text(encoding="utf-8"))
            for s in spans:
                if amount_hit is None and re.search(r"\d,\d{3}\.\d{2}", s["text"]):
                    amount_hit = (report_path.parent.name, int(seg.name[4:7]), s)
                if date_hit is None and re.search(r"\d{1,2}[-/]\w{3}[-/]\d{4}", s["text"]):
                    date_hit = (report_path.parent.name, int(seg.name[4:7]), s)
            if amount_hit and date_hit:
                break
        if amount_hit and date_hit:
            break

    def _edge(case: str, value: str, kind: str, anchor) -> None:
        stem, page, span = anchor
        bbox = _claim([span["x0"], span["y0"], span["x1"], span["y1"]], page=page)["bbox"]
        g = grounding.check_grounding(
            stem, page, value, bbox, BASELINE_RAW, margin=0.035,
            search_margin=0.12, loose_score=0.6, ngram_k=3, kind=kind)
        edge_rows.append({"case": case, "value": value, "anchor": span["text"][:60],
                          "passed": g["passed"] if g else None,
                          "score": g["score"] if g else None,
                          "detail": (g or {}).get("detail", "")[:140]})

    if amount_hit:
        token = re.search(r"\d[\d,]*\.\d{2}", amount_hit[2]["text"]).group(0)
        _edge("amount_plain", token, "amount", amount_hit)
        _edge("amount_currency_prefix", f"INR {token}", "amount", amount_hit)
        _edge("amount_parens", f"({token})", "amount", amount_hit)
        _edge("amount_trailing_dot", f"{token}.", "amount", amount_hit)
        # multi-token names and OCR-spaced digits need an anchor whose text
        # actually carries them — covered by tests/test_nonvlm_quality.py.
    if date_hit:
        raw = date_hit[2]["text"]
        m = re.search(r"\d{1,2}[-/]\w{3}[-/]\d{4}", raw)
        if m:
            _edge("date_plain", m.group(0), "date", date_hit)
            _edge("date_trailing_dot", m.group(0) + ".", "date", date_hit)
            iso = norm_date(m.group(0))
            if iso:
                y, mo, d = iso.split("-")
                _edge("date_dotted", f"{d}.{mo}.{y}", "date", date_hit)
                _edge("date_2digit_year", f"{d}/{mo}/{y[2:]}", "date", date_hit)
    _write(out / "grounding" / "fuzz.json", {"coordinate": rows, "value_edges": edge_rows})
    print("== grounding fuzz ==")
    from collections import Counter
    counts = Counter((r["case"], r["passed"]) for r in rows)
    for case in ("own_span", "shift_3pct", "shift_8pct", "shift_25pct",
                 "wrong_page", "row_opposite_x"):
        p = counts.get((case, True), 0)
        n = p + counts.get((case, False), 0)
        print(f"  {case:16} pass {p}/{n}")
    for r in edge_rows:
        print(f"  value {r['case']:18} value={r['value']!r:18} passed={r['passed']} "
              f"score={r['score']}")
    return {"coordinate": rows, "value_edges": edge_rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPO / "out" / "audit" /
                                         f"nonvlm-{date.today().isoformat()}"))
    ap.add_argument("--only", default="fixtures,replay,packet,grounding",
                    help="comma-separated sections")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"audit dir: {out}")
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if "fixtures" in only:
        section_fixtures(out)
    if "replay" in only:
        section_replay(out)
    if "packet" in only:
        section_packet(out)
    if "grounding" in only:
        section_grounding(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
