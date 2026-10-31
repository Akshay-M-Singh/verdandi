"""Azure Content Understanding runner for the benchmark (REST, stdlib only).

Setup (once):  export AZURE_CU_ENDPOINT=https://<resource>.services.ai.azure.com
               export AZURE_CU_KEY=<key>
               export AZURE_CU_COMPLETION=gpt-5.5           # Foundry deployment
               export AZURE_CU_EMBEDDING=text-embedding-3-large

  python benchmark/azure_acu.py --print-analyzer      # emit the analyzer JSON
  python benchmark/azure_acu.py --create-analyzer     # PUT + wait (free)
  python benchmark/azure_acu.py --probe syn_01_digital_ruled
  python benchmark/azure_acu.py --run --paid          # analyze all benchmark PDFs

One analyze call per document (base64 upload, same PDF bytes Verdandi sees);
raw responses and canonical predictions land in
benchmark/data/results/acu/<date>/<n>/. The response `usage` object carries
documentPages / contextualization / model tokens — the cost and token
datapoints are read from it, never guessed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from config import load_dotenv  # noqa: E402  (shared .env loader, D4)

load_dotenv()

PDFS = HERE / "data" / "pdfs"
RESULTS = HERE / "data" / "results" / "acu"
SCHEMA = HERE / "schema.yaml"
ANALYZER_ID = "verdandi_benchmark"
API_VERSION = "2025-11-01"

FIELDS = ("invoice_number", "invoice_date", "vendor_name", "buyer_name",
          "currency", "subtotal", "tax_total", "total_amount")
_AMOUNT_FIELDS = ("subtotal", "tax_total", "total_amount")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _num(name: str) -> float | None:
    value = _env(name)
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _endpoint() -> str:
    endpoint = _env("AZURE_CU_ENDPOINT").rstrip("/")
    if not endpoint:
        raise SystemExit("AZURE_CU_ENDPOINT is not set (see the module docstring)")
    return endpoint


def _key() -> str:
    key = _env("AZURE_CU_KEY")
    if not key:
        raise SystemExit("AZURE_CU_KEY is not set (see the module docstring)")
    return key


def analyzer_definition() -> dict:
    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    fields: dict[str, dict] = {}
    for name, spec in schema["fields"].items():
        entry: dict = {"type": {"str": "string", "name": "string", "date": "date",
                                "amount": "number"}.get(spec["type"], "string"),
                       "method": "extract", "description": spec["description"]}
        fields[name] = entry
    item_props = {}
    for col, spec in schema["line_items"]["columns"].items():
        item_props[col] = {
            "type": "number" if spec["type"] == "amount" else "string",
            "method": "extract", "description": spec["description"]}
    fields["line_items"] = {
        "type": "array", "method": "extract",
        "description": "every printed charge/menu row of the document's table",
        "items": {"type": "object", "properties": item_props},
    }
    return {
        "description": "Verdandi benchmark analyzer (canonical schema, zero-shot)",
        "baseAnalyzerId": "prebuilt-document",
        "models": {
            "completion": _env("AZURE_CU_COMPLETION", "gpt-5.2"),
            "embedding": _env("AZURE_CU_EMBEDDING", "text-embedding-3-large"),
        },
        "config": {"returnDetails": True, "enableFormula": False,
                   "estimateFieldSourceAndConfidence": True},
        "fieldSchema": {"fields": fields},
    }


def _request(method: str, url: str, body: dict | None = None,
             timeout: float = 120) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Ocp-Apim-Subscription-Key": _key(),
               "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"HTTP {exc.code} on {method} {url}\n{detail}") from exc


def _poll(url: str, timeout_s: float = 600) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, payload, _ = _request("GET", url)
        status = str(payload.get("status", "")).lower()
        if status in ("succeeded", "failed", "canceled"):
            if status != "succeeded":
                raise SystemExit(f"operation {status}: {json.dumps(payload)[:400]}")
            return payload
        time.sleep(2)
    raise SystemExit(f"operation timed out after {timeout_s}s: {url}")


def set_defaults() -> None:
    url = f"{_endpoint()}/contentunderstanding/defaults?api-version={API_VERSION}"
    completion = _env("AZURE_CU_COMPLETION", "gpt-5.2")
    embedding = _env("AZURE_CU_EMBEDDING", "text-embedding-3-large")
    status, payload, _ = _request("PATCH", url, {
        "modelDeployments": {completion: completion, embedding: embedding}})
    print(f"defaults set ({status}): {json.dumps(payload)[:300]}")


def create_analyzer() -> None:
    url = f"{_endpoint()}/contentunderstanding/analyzers/{ANALYZER_ID}?api-version={API_VERSION}"
    status, payload, headers = _request("PUT", url, analyzer_definition())
    op = headers.get("Operation-Location") or headers.get("operation-location")
    if status in (200, 201) and op:
        _poll(op)
    print(f"analyzer '{ANALYZER_ID}' ready ({status})")


def _base64_input(pdf: Path) -> dict:
    return {"data": base64.b64encode(pdf.read_bytes()).decode(),
            "mimeType": "application/pdf", "name": pdf.stem}


def _value(field: dict):
    if not isinstance(field, dict):
        return field
    for key in ("valueString", "valueDate", "valueNumber", "valueInteger",
                "valueBoolean"):
        if key in field:
            return field[key]
    return None


def canonicalize(fields: dict) -> tuple[dict, list[dict]]:
    out: dict[str, str | None] = {}
    for name in FIELDS:
        value = _value(fields.get(name))
        if value is None:
            out[name] = None
        elif name in _AMOUNT_FIELDS:
            try:
                out[name] = f"{float(value):.2f}"
            except (TypeError, ValueError):
                out[name] = str(value)
        elif name == "invoice_date":
            out[name] = str(value)[:10]
        else:
            out[name] = str(value).strip() or None
    items: list[dict] = []
    raw_items = fields.get("line_items") or {}
    for entry in raw_items.get("valueArray") or []:
        values = (entry or {}).get("valueObject") or {}
        row = {col: _value(values.get(col))
               for col in ("description", "quantity", "unit_price", "amount")}
        row["description"] = str(row["description"]).strip() if row["description"] is not None else None
        for col in ("quantity", "unit_price", "amount"):
            if row[col] is not None:
                try:
                    row[col] = f"{float(row[col]):.2f}"
                except (TypeError, ValueError):
                    row[col] = str(row[col])
        if row.get("amount") is not None:
            items.append(row)
    return out, items


def analyze_pdf(pdf: Path, timeout_s: float = 600) -> dict:
    submit = (f"{_endpoint()}/contentunderstanding/analyzers/{ANALYZER_ID}"
              f":analyze?api-version={API_VERSION}")
    status, payload, headers = _request("POST", submit,
                                        {"inputs": [_base64_input(pdf)]}, timeout=300)
    result_id = payload.get("id")
    op = headers.get("Operation-Location") or headers.get("operation-location")
    if op:
        payload = _poll(op, timeout_s)
    elif status == 202 and result_id:
        payload = _poll(f"{_endpoint()}/contentunderstanding/analyzerResults/"
                        f"{result_id}?api-version={API_VERSION}", timeout_s)
    return payload


def _flatten(payload: dict, stem: str) -> dict:
    result = payload.get("result") or {}
    contents = result.get("contents") or []
    fields = contents[0].get("fields") if contents and isinstance(contents[0], dict) else {}
    canonical, items = canonicalize(fields or {})
    pages = sum((c or {}).get("endPageNumber", (c or {}).get("startPageNumber", 1))
                - (c or {}).get("startPageNumber", 1) + 1 for c in contents) or 1
    return {"stem": stem, "fields": canonical, "line_items": items,
            "found": bool(contents), "pages": pages,
            "usage": payload.get("usage"),
            "warnings": result.get("warnings")}


def _new_run_dir() -> Path:
    base = RESULTS / dt.date.today().isoformat()
    n = 1
    while (base / str(n)).exists():
        n += 1
    run = base / str(n)
    run.mkdir(parents=True)
    return run


def _sum_usage(preds: dict[str, dict]) -> dict:
    totals: dict[str, float] = {"documentPages": 0, "contextualization": 0,
                                "model_input": 0, "model_cached_input": 0,
                                "model_output": 0}
    for pred in preds.values():
        usage = pred.get("usage") or {}
        # GA reports documentPagesStandard/Basic/Inline and
        # contextualizationTokens at the top level; the pricing explainer's
        # shape nests contextualization under `tokens`. Accept both.
        for key, value in usage.items():
            if key.startswith("documentPages") and isinstance(value, (int, float)):
                totals["documentPages"] += value
            elif key in ("contextualizationTokens", "advancedContextualizationTokens"):
                totals["contextualization"] += value or 0
        tokens = usage.get("tokens") or {}
        totals["contextualization"] += tokens.get("contextualization") or 0
        for key, value in tokens.items():
            if "cached" in key:
                totals["model_cached_input"] += value or 0
            elif key.endswith("-input") or key.endswith("Input"):
                totals["model_input"] += value or 0
            elif key.endswith("-output") or key.endswith("Output"):
                totals["model_output"] += value or 0
    return totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Azure Content Understanding benchmark runner")
    ap.add_argument("--print-analyzer", action="store_true")
    ap.add_argument("--set-defaults", action="store_true",
                    help="PATCH the resource model defaults (needed once per resource)")
    ap.add_argument("--create-analyzer", action="store_true")
    ap.add_argument("--probe", help="analyze one PDF stem")
    ap.add_argument("--run", action="store_true", help="analyze every benchmark PDF")
    ap.add_argument("--subset", help="JSON file with {stems: [...]} to analyze")
    ap.add_argument("--paid", action="store_true", help="confirm the paid analyze calls")
    args = ap.parse_args(argv)

    if args.print_analyzer:
        print(json.dumps(analyzer_definition(), indent=2))
        return 0
    if args.set_defaults:
        set_defaults()
        return 0
    if args.create_analyzer:
        create_analyzer()
        return 0
    if args.probe:
        pdf = PDFS / f"{args.probe}.pdf"
        if not pdf.exists():
            print(f"no PDF: {pdf}", file=sys.stderr)
            return 1
        payload = analyze_pdf(pdf)
        print(json.dumps(_flatten(payload, args.probe), indent=2,
                         ensure_ascii=False)[:4000])
        return 0
    if not args.run:
        ap.print_help()
        return 0

    pdfs = sorted(PDFS.glob("*.pdf"))
    if args.subset:
        wanted = json.loads(Path(args.subset).read_text())
        wanted = set(wanted["stems"] if isinstance(wanted, dict) else wanted)
        pdfs = [p for p in pdfs if p.stem in wanted]
    print(f"ACU run: {len(pdfs)} documents in {PDFS}")
    print("paid estimate: 1 analyze call per document + page/contextualization/"
          "model token usage per the service `usage` object — confirm current "
          "rates before interpreting cost; pass --paid to proceed")
    if not args.paid:
        print("dry stop — nothing was spent")
        return 0
    if not _env("AZURE_CU_ENDPOINT"):
        raise SystemExit("AZURE_CU_ENDPOINT is not set")

    run = _new_run_dir()
    preds: dict[str, dict] = {}
    started = time.time()
    for pdf in pdfs:
        payload = analyze_pdf(pdf)
        flat = _flatten(payload, pdf.stem)
        (run / f"{pdf.stem}.raw.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        (run / f"{pdf.stem}.json").write_text(
            json.dumps(flat, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        preds[pdf.stem] = flat
        print(f"acu: {pdf.stem} ok (usage={json.dumps(flat.get('usage'))})")
    meta = {
        "harness": "benchmark/azure_acu.py",
        "analyzer": ANALYZER_ID,
        "api_version": API_VERSION,
        "completion_model": _env("AZURE_CU_COMPLETION", "gpt-5.2"),
        "embedding_model": _env("AZURE_CU_EMBEDDING", "text-embedding-3-large"),
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - started, 1),
        "documents": len(preds),
        "usage_totals": _sum_usage(preds),
        "rate_card": {
            "document_page_standard": _num("AZURE_CU_RATE_PAGE_STANDARD"),
            "contextualization_per_1m": _num("AZURE_CU_RATE_CONTEXTUALIZATION"),
            "model_input_per_1m": _num("AZURE_CU_RATE_INPUT"),
            "model_cached_input_per_1m": _num("AZURE_CU_RATE_CACHED_INPUT"),
            "model_output_per_1m": _num("AZURE_CU_RATE_OUTPUT"),
            "source": _env("AZURE_CU_RATE_SOURCE") or None,
            "note": "fill the AZURE_CU_RATE_* env vars (region/model dependent) "
                    "to compute cost; usage is recorded regardless",
        },
    }
    (run / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"acu: {len(preds)} documents in {meta['elapsed_s']}s -> {run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
