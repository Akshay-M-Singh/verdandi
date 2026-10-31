<p align="center">
  <img src="docs/branding/mark.png" width="112" alt="Verdandi">
</p>

<h1 align="center">Verdandi</h1>

<p align="center">
  <strong>Read documents with vision AI. Verify every value locally.</strong><br>
  One paid model call per document, a deterministic guard you can audit,
  review flags with evidence, and a typed Excel export.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg?logo=apache&logoColor=white"></a>
  <a href="https://github.com/Akshay-M-Singh/verdandi/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/Akshay-M-Singh/verdandi?sort=semver"></a>
  <a href="https://github.com/Akshay-M-Singh/verdandi/actions/workflows/check.yml"><img alt="CI" src="https://github.com/Akshay-M-Singh/verdandi/actions/workflows/check.yml/badge.svg?branch=main"></a>
  <img alt="macOS and Windows" src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey">
</p>

---

Verdandi is a local-first document extraction engine and desktop app. Point it
at PDFs — invoices, bills of entry, air waybills, receipts, or anything else —
and it captures the pages, reads them with **one vision model call per
document**, checks the model's own output with a **deterministic verification
stack that runs on your machine**, and turns anything doubtful into a review
flag with the page crop and the located text beside it.

The text layer never extracts values. It only proves or disputes what the
vision model claimed — so a new layout works zero-shot, and every value has an
evidence trail.

**Why not just ask a model for the fields?** You can, and it mostly works —
until a digit transposes, a value is read from the neighbouring column, or a
total silently stops adding up. Verdandi treats the model as a reader, not an
oracle: the text layer cross-checks every claim, arithmetic checks the
document's own sums, and anything unproven reaches a human with the evidence
attached.

## Head to head with Azure Content Understanding

On a frozen 100-document benchmark, same PDF bytes, both systems zero-shot,
two runs each: Verdandi matches Azure Content Understanding on accuracy — and
on this run edges it on both headline metrics — at about a twelfth of the cost.

| | Verdandi | Azure Content Understanding |
|---|---|---|
| Field macro accuracy | **97.0%** (97.0 / 97.0) | 96.2% (96.3 / 96.1) |
| Line items — precision / recall / F1 | 95.4 / 96.3 / **0.958** | 94.2 / 95.3 / 0.947 |
| Documents found | 100/100 | 100/100 |
| Cost per run | **$0.2121** | $2.5523 (**12.0×**) |

<details>
<summary><b>Per-field accuracy and where each system wins</b></summary>

<br>

| Field | Verdandi | ACU |
|---|---|---|
| invoice_number | 97.1% | 98.6% |
| invoice_date | 98.6% | 100.0% |
| vendor_name | **99.3%** | 98.5% |
| buyer_name | **98.9%** | 93.5% |
| currency | 91.2% | 92.2% |
| subtotal | **96.8%** | 93.5% |
| tax_total | 94.9% | 97.5% |
| total_amount | **98.0%** | 94.9% |

By stratum (cells, run A / run B): Verdandi wins the clean-receipt class
decisively (24/24 both runs vs 18/24) and degraded receipts (21/24 vs 19/24);
ACU leads the messy receipt photos (54/55, 53/55 vs 50/55); invoices and scans
are a draw (52–53/58). ACU's residual is deterministic receipt semantics;
Verdandi's is CORD-class receipt totals and scan currency.

</details>

**How fair this is:** both systems saw identical PDF bytes, both were
zero-shot, and one schema wording generated both configurations. Verdandi ran
its best measured model (gpt-6-luna, full-resolution renders — the shipped
default is gpt-5.6-luna with images capped at 1536 px); ACU ran its best
supported model (GPT 5.5). Verdandi's cost is a gateway quota proxy, ACU's is
billed spend. Truth values are AI-verified, not human-audited, and the public
datasets may appear in either model's training data — the 30 synthetic
documents are the contamination-clean control.

The full harness, truth files, every number and every caveat are in
[`benchmark/`](benchmark), with the standalone record in
[`BENCHMARK-REPORT-2026-10-06.md`](benchmark/BENCHMARK-REPORT-2026-10-06.md).

## See it

| Inspect a capture | Results, with the evidence overlay |
|---|---|
| ![Captured pages, ready to extract](docs/screens/inspect.png) | ![Extracted fields and page boxes](docs/screens/results.png) |

| Review a flag | Build an export |
|---|---|
| ![Review pane with crop and located text](docs/screens/review.png) | ![Export builder](docs/screens/export.png) |

## Why it is built this way

- **Vision-primary.** One call per document reads the page and returns
  selection plus structured fields. No template matching, no regex extraction,
  no per-layout engineering — a layout the model has never seen still works.
- **A deterministic guard, not a second extractor.** Grounding, arithmetic,
  format checks and a gated disagreement re-read run locally and cost nothing.
  They only ever flag; a value is never silently corrected.
- **Local-first, bring your own key.** Runs, page renders, extractions,
  reviews and exports live in your workspace folder. The only cloud call is the
  page image sent to the model provider you configure.
- **Built for batches.** Parallel workers, resumable runs, multi-document
  files, workflows that bundle several document kinds, cross-document checks
  and per-run cost accounting.

## How it works

```
PDF
 └─ capture  (local · free)
      page renders · text geometry · quality gate · orientation
    └─ ONE vision call
         document selection + structured extraction (+ capped re-checks
         on disputed fields only)
       └─ verification  (local · free)
            grounding · arithmetic · format · gated disagreement
            line-item row checks · duplicate/overlap backstops
            cross-document checks (workflows)
          └─ confidence thresholds → review flags → Excel export
```

### The verification stack

| Check | What it proves | What it catches |
|---|---|---|
| **Grounding** | The claimed value really is printed near the claimed region, as a normalized token in the text channel | Hallucinated values, wrong field, wrong page, cross-file mix-ups |
| **Arithmetic** | The document's own numbers agree (row sums, totals, per-row tax identity) | Transposed digits, dropped tax cells, totals that do not add up |
| **Line-item rows** | Every row carries an amount, stays in printed order, and is grounded in its own region | Summary rows read as items, merged rows, invented rows |
| **Format** | Dates and amounts parse under one normalizer (day-first and dotted forms included) | Impossible dates, malformed amounts |
| **Gated disagreement** | A capped re-read with different framing disputes only already-flagged fields | Confidently wrong reads that pass grounding |
| **Cross-document** | Declared rules hold across documents in the same file (for example, a Bill of Entry number must match) | Broken shipment chains; unbound documents in a pass-if-any rule |
| **Duplicate / overlap** | Two entries of the same template that share a page are compared | Double-counted scans, page-overlap conflicts |

Every flag carries a reason, a confidence and both anchors — the model's box
and the located text — and lands in the review queue rather than the output.

## Quickstart

Requirements: **Python 3.11+**. The desktop app also needs **Node 20+**; a
Rust toolchain is only required for the native window and packaged builds.
Capture uses Apple Vision on macOS (Xcode command-line tools) and the optional
cross-platform engine (pypdfium2 + RapidOCR) everywhere else.

### Desktop app

Prebuilt installers for macOS (Apple Silicon) and Windows (x64) are on the
[releases page](https://github.com/Akshay-M-Singh/verdandi/releases/latest);
install steps and troubleshooting are in [docs/INSTALL.md](docs/INSTALL.md).

To run it from source:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r desktop/requirements-desktop.txt
cd desktop && npm install && cd ..
make desktop-dev            # engine + interface → http://localhost:5173
```

### CLI

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Windows / Linux / macOS without Xcode: add the cross-platform capture engine.
.venv/bin/python -m pip install -r requirements-crossplatform.txt

cp .env.example .env        # add OPENCODE_API_KEY (https://opencode.ai/auth)

mkdir -p my-pdfs            # put your own PDFs here
.venv/bin/python text_layer.py my-pdfs out/raw   # capture (free)
.venv/bin/python extract.py out/raw out/json     # one vision call per document (paid)
```

`extract.py --document <id>` picks the template from `rules/documents.yaml`
(with no flag it uses the first one). The same pipeline drives the app;
`make new-run` + `make ocr-corpus` + `make extract-corpus` are the corpus
workflow used for development, and `make eval` scores a run against a truth
file.

Optional but recommended: `requirements-orientation.txt` adds the local
page-orientation classifier (offline, ~3 ms/page).

`invoices/` and the truth files are **not** in this repository — they are
private test data. The test suite self-skips the corpus assertions when they
are absent.

## Bring your own key

The shipped configuration points at the [opencode Zen](https://opencode.ai/auth)
gateway, but the endpoint is data, not code: edit
[`config/models.yaml`](config/models.yaml) to use any OpenAI-compatible
endpoint with vision. The app stores the key in the OS keychain; the CLI reads
`OPENCODE_API_KEY` from the environment or `.env`.

Only models that pass the built-in vision probe are offered in the app's model
picker, so a text-only model cannot be selected by accident.

## Privacy

- Page images go to the model provider **you** configure, with **your** key.
  Nothing else leaves the machine.
- No telemetry, no analytics, no access check, no feedback upload. The app
  makes no requests to the maintainers.
- Sandboxed to localhost: the desktop engine service binds `127.0.0.1` only.
- Official builds check the public GitHub release feed for updates; builds from
  source can change or remove that endpoint.

Details: [`legal/PRIVACY.md`](legal/PRIVACY.md).

## Repository layout

| Path | What it is |
|---|---|
| `extract.py`, `text_layer.py` | capture and extraction entry points |
| `verify.py`, `grounding.py`, `validate.py`, `aggregate.py` | the local verification stack |
| `compare.py`, `containment.py` | normalization, matching, cross-checks |
| `export.py` | typed, colour-coded Excel workbooks |
| `engines/` | Apple Vision and cross-platform capture engines, orientation model |
| `desktop/` | Tauri + React app and the local engine service |
| `rules/` | document profiles, workflows and export profiles (data, not code) |
| `config/` | models and thresholds |
| `benchmark/` | the Azure Content Understanding harness and data |
| `tests/` | offline tests (the private corpus is optional) |

## Development

```sh
make check                    # unit suite + CLI smoke + ignore assertions
make desktop-check            # offline desktop tests + UI build + engine smoke
cd desktop && npm run build   # TypeScript + Vite
```

The suite is offline: no paid calls, no network. 500+ tests cover the capture
harness, the verification stack, extraction plumbing, exports and the desktop
service.

Contributions are welcome — read [`CONTRIBUTING.md`](CONTRIBUTING.md) first.
Please report vulnerabilities privately via [`SECURITY.md`](SECURITY.md).

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). Third-party components and the
CC BY 4.0 benchmark datasets are attributed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and [`NOTICE`](NOTICE).

The Verdandi name and logo are not licensed for use as trademarks under
Apache-2.0 (Section 6); forks are welcome but should not imply endorsement.

<p align="center"><sub><i>Verðandi — “that which is becoming” — one of the three Norns.</i></sub></p>
