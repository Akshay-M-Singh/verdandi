# Architecture — Broker Invoice Extraction Pipeline

## 1. Purpose

Extract structured data from **freight broker invoices** delivered as PDF attachments.
Attachments may contain **multiple documents** (broker invoice + supporting docs such as
terminal charges, handling bills, packing lists, AWB/BL copies). We only care about the
**broker invoice**, identified by **content-based rules** that are **user-editable at any
time** (no code changes required).

Every extracted data point carries a **confidence score**. Fields below a configurable
per-field threshold land in a **human review queue** with a **source pointer** (which page
and line the value came from) so a human can verify quickly.

Deliverable per attachment: structured JSON + flag rows. Volume is
**high (>500 attachments/month)**, so cost and throughput matter.

---

## 2. Context & Evidence Base

The project folder ships with a real test corpus: `invoices/` contains 12 PDFs of actual
freight/logistics invoices from 4 vendors, renamed 2026-08-09 to lowercase broker labels
(human readability only — file names are never a factor, see D18). The corpus is
**local-only — never committed or pushed** (owner rule 2026-09-30; gitignored, history
purged):

| File(s) | Vendor (actual) | Billed to | Pages | Nature |
|---|---|---|---|---|
| `sample_b_1/2/3.pdf` (old OLD-EXP-01/2235/8903) | Vendor A | Buyer A | 16/14/14 | **Digital pages with decorative background images**: the invoice content is a vector text layer with an obfuscated custom font encoding (no ToUnicode; PDFKit decodes it byte-accurately). Small banners/logos (≤927 px) are stretched to full-page coverage — NOT scans. One true scan page each (sample_b_3 p14). See D15 note 2026-08-09 (the old "scan + ~48% WER OCR layer" framing was wrong — the layer is the authoritative text) |
| `sample_c_1/2.pdf` (old BUYER OLD-NIP-01/170 — the name was misleading) | **Vendor C** | Buyer C | 15/14 | Pure scan, image wrapped in a Form XObject, ~150 DPI |
| `sample_a_1/2/3.pdf` (old DOC-D-01/56/69 — the name was misleading) | **Vendor D** | Buyer D | 10 each | Pure scan (Form XObjects), ~150 DPI; `sample_a_2/3` have exactly one typed (embedded-text) page each — p2 — which still carries a full-page background image and is OCR'd per D15 |
| `classic_1.pdf` (old DOC-CLF-01) | Vendor B | Buyer B | 1 | Phone-photo PDF, 3507×2480 px placed at 1px=1pt (D20): full-page image normalized by the D20 render fix. Header (letterhead, `DOC-CLF-01`) now OCR-readable; owner-excluded from eval |
| `classic_2.pdf` (old DOC-CLF-02) | Vendor B (p1) + Vendor E (p2) + Vendor F (p3) — **3 documents in one PDF** | Buyer B | 3 | Phone-photo PDFs, 1px=1pt placement (D20): all 3 pages normalized; `DOC-CLF-02` now OCR-readable |
| `classic_3/4.pdf` (old DOC-CLF-03/03097) | Vendor B | Buyer B | 1 each | Hybrid: clean embedded text **plus** a full-page DCTDecode image (routed to OCR per D15) |

> **Note (2026-08-01, Stage 1; amended 2026-08-09):** the old "10 scanned + 2 digital" split was
> wrong — it came from string-length checks that missed image layers and
> Form-wrapped scans. Verified reality: Vendor A pages are digital (vector
> text layer over decorative background images), DOC-CLF-series pages are clean
> text over a full-page image, and only Vendor D/Vendor C/Classic_1-2 are true scans.
> Post-amendment routing (D15 note 2026-08-09): **47 of 109 pages use embedded
> text, 62 route to OCR** (was 103 OCR before the amendment).

Learned facts that constrain the design:

- **File names are NEVER a factor (D18, owner emphatic 2026-08-09).** Names
  like `classic_1` are human-readable labels only — never a reference, rule,
  detection signal, or pointer key. Content-based detection is mandatory.
- **One PDF can contain several documents** (`classic_2.pdf`: broker invoice on p1,
  two supplier bills on p2–3). Selection must be **page-aware**.
- **Scans are low-resolution** (124–300 DPI). OCR must upscale (3× render) for accuracy.
- **OCR accuracy was empirically validated**: Apple Vision OCR on these exact scans
  achieved 100% correct extraction of critical fields (invoice no, dates, job numbers,
  amounts, GSTIN) and 95–97% token recall vs ground truth. See Section 6.
- **Template/regex parsing was tried and rejected** (see Decision D5).

---

## 3. Pipeline (end to end)

```
inbox/*.pdf                     # user drops attachments here manually
   │
   ▼
[1] Text layer + renders — one Swift subprocess per file (D2/D3/D15/D24)
   │   per-page CGPDF probe: embedded-text quality, image coverage,
   │   invisible-text mode, codecs
   │   route: embedded → PDFKit text | hybrid/scan → OCR (Vision, 3×)
   │   ALWAYS writes the per-page vision input (D24 — unconditional since
   │   2026-08-19): pageNNN.image.png (raw embedded photo, D21) +
   │   pageNNN.render-ocr-input.png (the page render; embedded pages included)
   │   the TEXT channel (pageNNN.txt + bbox + segments) feeds ONLY the
   │   rules validator ([2b]) — never an LLM
   │   quality gate (D16): per-page good|poor|blank|ignored → recorded+flagged
   │
   ▼
[2] Vision extraction — ONE schema-constrained LLM call (D1/D24/D25; PRIMARY)
   │   reads ALL page images; identifies the TARGET DOCUMENT profile
   │   (rules/documents.yaml signals — D25) + extracts fields
   │   per-field confidence + source ("page N (render image)") + source_region
   │   output = { selected_document, header_fields, line_items, notes, status }
   │           + _meta {model, mode, attempts, usage}
   │
   ▼
[2b] Verification stack (LOCAL, free, deterministic — the sanity checker, D30)
   │    Check 1 grounding: the claimed value exists near its claimed
   │    source_region in the text channel (strict normalized-token match,
   │    two bands — no region = skip) · Check 2 arithmetic: profile-declared
   │    invariants (data) · Check 3 format: value_type table
   │    -> aggregate -> accept | review_secondary | review_human
   │    (+ GATED Check 4: cheap-model recheck, different framing, flagged
   │    fields only, capped per document)
   │
   ▼
[3] Aggregation (D30): weighted verdict per field → accept |
   │   review_secondary | review_human; arithmetic/disagreement hard-fail.
   │   Line-item row checks (D31): every row carries an amount, row math holds.
   │   Workflow guards: missing_expected_document (completeness contract),
   │   content_mismatch (primary id must live in its own file's text).
   │   Selection guards: no_document_match / sub-min_score / pin-drift.
   │
   ▼
[4] Output (per run — see D18)
      <run>/flags.csv              # dual-anchored flags for human review
      <run>/json/<file>.json       # full extraction + _meta.verification (all checks)
      <run>/summary.csv            # per (file, template) provenance rows (bulk)
      <run>/manifest.json          # upload stems -> original names, sha (bulk)
      <run>/raw/<file>/            # cached text + renders for audit
```

---

## 4. Components

| Component | File | Responsibility |
|---|---|---|
| Orchestrator | `process.py` | CLI entry (`python process.py [--watch]`, `--new-run`), creates `out/runs/<YYYY-MM-DD>/<N>/` runs + `out/latest` symlink (D18), iterates inbox, invokes stages, dedup/manifest |
| Text layer | `text_layer.py` | Orchestrates capture: compile-on-demand `swiftc -O`, one subprocess per PDF (120 s timeout, failure isolation), feeds customWords from document-profile keywords (D29; brokers.yaml gone), `--files` scoping (bulk runs capture only the just-uploaded files), quality gate (D16). The text channel serves ONLY the verification stack (D30) |
| OCR harness | `swift/ocr.swift` | Single binary, one subprocess call per PDF: per-page CGPDF probe (embedded-text quality, image coverage, invisible text, codecs, Form recursion) → per-page routing (D15) → embedded extraction (PDFKit) or OCR (`RecognizeTextRequest`, 3× render, `.accurate`, en-US, language correction, customWords) → LOCAL orientation detection (D22) → per-page `image_stats` (std-dev, dark ratio, Laplacian variance, D16) → writes `pageNNN.txt`, `pageNNN.bbox.txt`, `report.json` + ALWAYS the vision inputs `pageNNN.image.png` (raw photo, D21) and `pageNNN.render-ocr-input.png` (page render — unconditional since D24, 2026-08-19) |
| Extraction | `extract.py` | VISION-PRIMARY (D24): builds the prompt from broker rules + schema; one schema-constrained LLM call per attachment (D1) reading the page renders via `llm_client.py`; then orchestrates the rules locator, the crosscheck, the D8 validation and <run>/flags.csv. `--model` overrides the primary model for swap verification |
| Vision prompt | `prompts/text.py` + `prompts/compose.py` | Literal prompt text (`text.py`, `PROMPT_VERSION`) + runtime assembly (`compose.py`) — primary vision pass (D29/D30 era; D44 reorganized the retired extract_v3/v4 modules; production code: bump PROMPT_VERSION + `make eval` before accepting a regression; golden fixtures pin the assembled bytes in `tests/fixtures/prompts/`) |
| Verification | `verify.py` | D30 sanity-checker orchestration: per-field offline checks (grounding/arithmetic/format -> verdict via aggregate), line-item row checks (D31), content binding + duplicate/overlap backstops (bulk), selection guards, `_meta.verification` audit + dual-anchored flag rows |
| Grounding | `grounding.py` | Check 1 (D30): model source_region (top-left origin, measured) -> coordinate flip -> tight/loose two-band existence search over text-channel spans (segments + real embedded per-line geometry); strict normalized-token containment; no region = skip (never fail) |
| Validation | `validate.py` | Deterministic arithmetic + format checks (D8); confidence caps + flag rows — never silently corrects |
| LLM client | `llm_client.py` | The ONLY module that talks HTTP to an LLM: stdlib `urllib` POST to the opencode Zen gateway (OpenAI-compatible; api_style responses | chat_completions per model — luna routes /responses since its chat route broke 2026-08-24; image_url_form flat | nested per model), `json_object` mode, retries with backoff (max 2), `LLMError` on exhaustion; per-call `usage` capture (token accounting, 2026-08-19) |
| Config contract | `config.py` | Loads `.env` (hand-rolled, os.environ wins), loads + validates the five YAMLs fresh on every call (models/thresholds/documents/workflows — hot-load, D10), clear `path: key` error messages, unknown-key warnings |
| Bulk workflows | `rules/workflows.yaml` | **User-editable** BULK bundles (2026-09-05): named sets of document profiles extracted per file in ONE call (`documents`; order = prompt order) + an OPTIONAL completeness contract (`expected`, may exceed documents — "extract 3, expect 4"); missing members raise `missing_expected_document` flag rows. Combined with the run manifest (unique content-hash stems → original names) and the `content_mismatch` binding assert, this makes cross-file mix-ups structurally impossible to pass silently (G1-G3: ingest uniqueness, per-file binding, one-call-per-file hard rule). CLI `--workflow`, UI Workflows tab, `--skip-done` resumable runs, `<run>/summary.csv`. |
| Excel export | `rules/exports.yaml` + `export.py` | **User-editable** export profiles (D54): ordered columns (`meta` / `template+field` / `line_item` / `cross_check` sources), `grain: file \| line_item` (one row per line item, header fields repeated), typed values + colour-coded statuses (amber pending · red issue/mismatch · grey not-checked) with cell comments, plus `Issues`/`Meta`/`Line items` sheets. Engine is local/deterministic (openpyxl lazy, UI deps); CLI `export.py <run> --export <id>` → `<run>/export/<id>_<timestamp>.xlsx`. |
| Document profiles | `rules/documents.yaml` | **User-editable** TARGET DOCUMENT profiles (D25): id/label/description/signals source/extraction contract — the pipeline extracts from the profile the user picks |
| Containment | `containment.py` | Plain-Python, kind-aware normalized-token containment + n-gram evidence ratio (no regex — D5, no deps — D14) |
| Aggregation | `aggregate.py` | Weighted per-field verdict accept | review_secondary | review_human; hard-fail checks; weights/floors from `verification:` in thresholds.yaml |
| Models | `config/models.yaml` | Primary model (`gpt-5.6-luna`, `primary_mode: vision` — D24) + Zen gateway (GO subscription) `endpoint` |
| Secrets | `.env` | `OPENCODE_API_KEY` (never committed) |
| Tests | `tests/test_config.py` | Stage 0 config-contract tests, run via `make check` |
| Eval harness | `eval.py` | Matrix: extraction vs `tests/ground_truth.json` (field × file correct/wrong/missing) + source-pointer audit + reconciliation + D24 crosscheck stats; `make eval` |
| Operator UI | `desktop/` (Tauri 2 + React/TS + Python engine sidecar) | The app (D66/D67 — Streamlit retired 2026-10-02): Run/Results/Review/Export/Runs operator flow, in-app Templates/Workflows/Export-profile builders, Settings + keychain + onboarding + diagnostics + auto-update. `ui/lib.py` + `ui/render.py` remain as shared pure helpers; the engine service drives the same CLI entry points (`text_layer.run_batch` → `extract.run_batch`), core never imports `desktop/` (D18/D66 intact) |
| Docs | `architecture.md`, `roadmap.md`, `AGENTS.md` | This file + staged development checklist + agent instructions |

> **Note (2026-08-01, Stage 0):** `config.py`, `config/models.yaml`,
> `tests/test_config.py`, `Makefile`, `requirements.txt`, `.env.example` added as
> part of scaffolding. `config/models.yaml` was previously implied but not listed
> in this table. (2026-08-04: `.env.example` removed — a real `.env` exists;
> Stage 2 added `eval.py` + `prompts/` above.)

---

## 5. Rules Configuration (data, not code - D10)

> **Note (2026-08-09, amended 2026-08-29/09-05):** the original per-broker
> registry (`rules/brokers.yaml`) is REMOVED (D29 - selection is vision-led).
> The historical paragraphs below remain for the decision record. Today's
> rules files: `rules/documents.yaml` (profiles, D25/D27-D29),
> `rules/workflows.yaml` (bulk bundles + completeness, 2026-09-05),
> `config/thresholds.yaml` (bars, quality floors, verification block).

The single most important design constraint: **rules are data, not code.** The user will
provide custom rules per broker and must be able to change them at any time, including in
emergencies, without touching code.

```yaml
brokers:
  - name: "Vendor A"
    pans: ["PAN-02"]
    vendor_keywords: ["Vendor A"]
  - name: "Vendor C"
    pans: ["PAN-05"]
    vendor_keywords: ["Vendor C"]
  - name: "Vendor D"
    pans: ["PAN-04"]
    vendor_keywords: ["Vendor D"]
  - name: "Vendor B"
    pans: ["PAN-01"]
    vendor_keywords: ["CLASSIC CLEARING & FORWARDING"]

selector:
  pan_weight: 0.7        # strongest signal — the broker's PAN, printed on every invoice
  vendor_weight: 0.3     # vendor name match in header
  min_score: 0.5         # below this → no selection → flag for human review
  allow_no_broker_match: false  # never guess; flag instead
```

> **Note (2026-08-09, owner decision — selection signals changed):**
> - **`gstins` replaced by `pans`.** GSTINs vary by entity/branch and were
>   partly misread from garbled OCR (the Vendor A GSTIN in the old config,
>   `GSTIN-01`, was itself OCR garbage — the printed GSTIN is
>   `33PAN-022ZJ`, built from the PAN). The broker's **PAN is stable and
>   printed on every invoice** (`PAN # PAN-02`). PANs are unique across the
>   four brokers (distinct by the 3rd character).
> - **`fee_keywords` REMOVED.** 200+ fee descriptions per broker, heavily
>   overlapping — useless as a selection signal. Selection now scores
>   PAN (0.7) + vendor keyword (0.3).
> - **Vendor D's `ignore_pages` removed.** Per-broker page rules encode
>   corpus-specific facts; the universal D16 quality-gate floors handle bad
>   pages. The `ignore_pages` key stays OPTIONAL in the schema for genuine
>   future cases. (Corpus effect: Vendor D p6–8 excluded by floors, p9–10 and
>   sample_a_2 p8 — the floor-invisible handwritten page — now reach the LLM.)

How selection works: each document (page or page-cluster) in the attachment is scored
against every broker in the list. The document with the highest score above `min_score`
is the broker invoice. If no document clears the bar, the file is **flagged for review,
not guessed**.

**Rules are rendered into the LLM prompt at runtime** — the LLM both selects and
extracts. The YAML weights exist to express *which signals matter most*, not to do
arithmetic in code.

**`ignore_pages`** (optional, per broker): 1-based page numbers that must never be
fed to extraction. Declares owner-known trash pages as data, not code — hot-loaded
like everything else (D10). **Not used as of 2026-08-09** (owner decision: page rules
are corpus-specific; the universal D16 quality-gate floors catch bad pages), but the
key stays in the schema for genuine future cases. Pages excluded by
the quality gate (D16) are still counted for routing/OCR, but are dropped from the
combined text and recorded in `report.json` with a reason.

**Verification-stack data** (D30, 2026-09-02 — the deterministic sanity
checker's contract, all rules as data — D10): the checks carry NO
label/alias tables (the rules locator + `rules/labels.yaml` are RETIRED —
label variance is the primary model's business, not the validator's). The
stack's data is: (1) `config/thresholds.yaml` `verification:` block —
grounding bands/scores, check weights, accept/review floors, hard-fail
checks, recheck trigger + cap; (2) `extraction.arithmetic` per document
profile — the invariants Check 2 enforces (e.g. line_items_sum_to
total_invoice_amount, tolerance); (3) the profile's field `type`s — the
Check 3 validator table keys. Everything else is plain, auditable,
dependency-free Python (containment.py).

**`rules/documents.yaml`** (D25/D27-D29, 2026-08-28 — the TARGET DOCUMENT
contract): one profile per document kind — {id, label, description,
identification, extraction: fields[]-with-inline-bars}. Profiles are
SELF-CONTAINED (thresholds/labels inline, D28); identification is VISION-LED
(profile keywords are helper words; the pin is the ultimate identification —
D29); the prompt schema is GENERATED from the profile fields (D27). A new
document kind = a new YAML profile, no code. Also `rules/workflows.yaml`
(2026-09-05): bulk bundles of profiles + expected-completeness contracts.

---

## 6. Decision Log (why each choice was made)

Every decision below was explicitly discussed with the project owner. Do not silently reverse any of them; if a decision must change, update this file and the roadmap. Status legend: **ACTIVE** = binding now · **AMENDED** = binding with the noted change · **SUPERSEDED** = replaced by a later decision (kept for the record).

### 6.1 Decision index

One row per decision: what it covers, when it was made, and whether it is currently binding. Use this table to navigate; read the cards (6.2) for the current state of each decision and the appendix (6.3) for the verbatim archive.

| D# | Decision | Date | Status |
|---|---|---|---|
| D1 | Two-stage pipeline: deterministic capture + LLM interpretation | 2026-08-01 | Active |
| D2 | OCR engine: Apple Vision via a small Swift harness | 2026-08-01 | Active |
| D3 | Skip OCR when the PDF has an embedded text layer | 2026-08-01 | Active |
| D4 | Extraction via the opencode Zen gateway (OpenAI-compatible) | 2026-08-02 | Active |
| D5 | No regex/template extraction — learned the hard way | 2026-08-01 | Active · D24 boundary |
| D6 | Two-tier routing (escalation on low confidence) | 2026-08-03 | superseded → D24 |
| D7 | Per-field confidence thresholds + source pointers | 2026-08-01 | amended → D24 |
| D8 | Deterministic validation layer (no AI) | 2026-08-01 | Active · implemented 2026-08-19 |
| D9 | Page-aware multi-document handling | 2026-08-01 | Active |
| D10 | Broker rules as hot-loaded YAML | 2026-08-01 | Active · + D30 rules |
| D11 | Output: per-run flags + per-file JSON + raw cache | 2026-08-01 | Active · amended D18 |
| D12 | Dedup + idempotency | 2026-08-01 | Active · pending Stage 7 |
| D13 | Manual drop + CLI batch (no email pipeline) | 2026-08-01 | Active |
| D14 | Runtime: Python 3 stdlib + pyyaml; Swift only for OCR | 2026-08-01 | Active |
| D15 | Per-page text-source routing: embedded \| OCR \| empty | 2026-08-01 | Active · amended → D24 |
| D16 | Document-quality gate: blank/illegible pages excluded | 2026-08-01 | Active |
| D17 | OCR output hardening: geometry-based restructuring allowed | 2026-08-03 | Active |
| D18 | Run-based output layout + file names are never a factor | 2026-08-09 | Active |
| D19 | Capture reshaping: fragment persistence + reconstruction | 2026-08-09/10 | Active |
| D20 | Render fix: normalized placement + rotation + native resolution | 2026-08-10 | Active |
| D21 | Raw embedded-image extraction for the vision pass | 2026-08-10 | Active · amended → D24 |
| D22 | Local page-orientation detection (Apple Vision geometry) | 2026-08-10 | Active |
| D23 | Dead-code cleanup | 2026-08-11 | Active · process record |
| D24 | Vision-primary pivot: one vision LLM call + rules validator | 2026-08-19 | Active — current architecture |
| D25 | Document identification profiles | 2026-08-28 | Active — staged |
| D26 | Document discovery & pinning | 2026-08-29 | Active — staged |
| D27 | Per-document field schemas (user-defined) | 2026-08-29 | Active |
| D28 | Self-contained profile fields (thresholds, labels inline) | 2026-08-29 | Active |
| D29 | Vision-led selection, brokers.yaml removed | 2026-08-29 | Active |
| D30 | Verification stack replaces the rules locator | 2026-09-02 | Active |
| D31 | Line-item row checks (amount mandatory, row math) | 2026-09-07 | Active |
| D32 | Cross-document validation rules (workflow `cross_checks`) | 2026-09-07 | Active |
| D33 | Template learning from samples (Learn tab, `--discover-fields`) | 2026-09-07 | Removed 2026-09-23 (D40) |
| D68 | Open-source release under Apache-2.0; access check removed | 2026-10-30 | Active |

### 6.2 Decision cards — D1–D24 (current state, curated)

Every decision below was explicitly discussed with the project owner on the
stated date. Statuses: **ACTIVE** = currently binding · **AMENDED** = still
binding, with the change noted · **SUPERSEDED** = replaced by a later decision
(kept for the record). The complete original entries, evidence and dated notes
are preserved verbatim in **6.3 Appendix A** (no data is lost — cards are the
readable digest, the appendix is the archive).

---

### D1 — Two-stage pipeline: deterministic capture + LLM interpretation  `ACTIVE`
**Date:** 2026-08-01
**Decision:** Deterministic capture first (renders + text channel), then ONE
schema-constrained LLM call for selection + extraction. No template matching,
no regex field parsing.
**Why:** Template/regex pipelines are brittle against vendor layout changes;
one schema-constrained LLM call handles new layouts zero-shot, and splitting
capture from interpretation keeps each stage testable.
**Status:** Active. D24 keeps the one-call design — the LLM call now reads page
renders, and the text channel feeds the deterministic validator instead.

---

### D2 — OCR engine: Apple Vision via a small Swift harness  `ACTIVE`
**Date:** 2026-08-01
**Decision:** Render each page at 3× with PDFKit, OCR with Vision
(`RecognizeTextRequest`, `.accurate`, en-US, language correction, customWords
from `rules/brokers.yaml`). One compiled subprocess per PDF.
**Why:** Free and local (zero marginal cost at >500/month); validated on this
exact corpus (100% on critical fields, 95–97% token recall); no installs;
alternatives rejected (Tesseract worse on noisy tables, cloud OCR costs per
page). 3× upscale because corpus scans are 124–300 DPI.

---

### D3 — Skip OCR when the PDF has an embedded text layer  `ACTIVE`
**Date:** 2026-08-01
**Decision:** Embedded-text detection is a CGPDF content-stream probe IN the
Swift harness (not a Python zlib scan). Clean layers are used directly, OCR is
skipped.
**Why:** Digital pages are exact, free, instant — OCR only adds error. The
zlib scan false-positives on compressed image data (~1.18M fake words on
DOC-D-01) and cannot see Form-wrapped or invisible text (`Tr 3`).

---

### D4 — Extraction via the opencode Zen gateway (OpenAI-compatible)  `ACTIVE`
**Date:** 2026-08-02
**Decision:** All LLM traffic goes through `llm_client.py` to the opencode GO
gateway (`https://opencode.ai/zen/go/v1/chat/completions`, OpenAI-compatible)
with the `OPENCODE_API_KEY`. The model comes from `config/models.yaml`
(`primary_model` — never hardcoded). Output constrained via
`response_format: json_object` + prompt-embedded schema + validate-and-retry
(max 2 retries); Gemini's native `responseSchema` is unavailable through the
gateway.
**Why:** A GO subscription covers extraction at a flat monthly cost that beats
per-token Gemini billing at >500 attachments/month.
**Watch-outs:** GO usage is dollar-quota'd ($12/5h, $30/wk, $60/mo) —
`GoUsageLimitError`-class 4xx is not retried; Luna's documented GO endpoint is
`/responses`, but chat/completions works (verified live 2026-08-04).
**(2026-09-07 amendment):** the Zen gateway now REQUIRES session-affinity
headers on every request — since 09/06, requests missing `x-opencode-session`
error HTTP 400 `MissingSessionID` ("cannot be routed efficiently"). llm_client
sends `x-opencode-session` (stable opaque value per process; prompt-cache
affinity within a run, avoids the dead-sticky-replica trap of a reused id) +
`x-opencode-client: verdandi` + a fresh `x-opencode-request` per attempt.
`OPENCODE_SESSION` env override for cross-run affinity.
**Status:** Active. Model/mode history (flash 403, mimo pick, D24 primary
switch) → Appendix A4.

---

### D5 — No regex/template extraction — learned the hard way  `ACTIVE`
**Date:** 2026-08-01
**Decision:** Zero field extraction via regex/templates, ever.
**Why:** The prior attempt was ~80–90% vendor-specific (literal amounts
hardcoded, dispatch keyed on filenames, silent blank failures) and worked on
the test files only by luck. Rejected entirely: vendor detection by content,
extraction by LLM, validation by arithmetic.
**Status:** Active. D24 boundary: the rules locator LOCALIZES by label
adjacency + accepts via format checks only — it never matches value patterns
(see D24 card).

---

### D6 — Two-tier routing (escalation on low confidence)  `SUPERSEDED → D24`
**Date:** 2026-08-03
**Decision (original):** documents go to a cheap model; any required field
below threshold is re-run with the stronger model; the better result wins.
**Why (original):** >500/month makes per-document cost matter — escalate only
failures.
**Status:** **SUPERSEDED 2026-08-19 by D24** — the two-tier routing is
removed; vision is primary for every attachment and the deterministic
validator guards it. The 2026-08-10 amendment (escalation = gpt-5.6-luna with
VISION input, `mimo-v2.5-pro` rejected as text-only) and its merge-guard
details are preserved in Appendix A6.

---

### D7 — Per-field confidence thresholds + source pointers  `AMENDED (D24)`
**Date:** 2026-08-01
**Decision:** Every field carries {name, value, confidence (0–1), source};
per-field thresholds in `config/thresholds.yaml`; below threshold → a row in
`<run>/flags.csv`, so a human can verify quickly and find the exact spot.
**Why:** Data above threshold is trusted and used; below it, a human checks —
source pointers are mandatory for that to be practical. Amount fields get
stricter thresholds than reference fields.
**Status:** Amended 2026-08-19 (D24): thresholds no longer trigger an
escalation re-run — they produce review flags (below_threshold), and the
trust boundary is inverted: the deterministic rules channel is the guard on
the vision primary (amount conflicts arbitrated by line-item sums; everything
else flags with both sources).

---

### D8 — Deterministic validation layer (no AI)  `ACTIVE (implemented 2026-08-19)`
**Date:** 2026-08-01
**Decision:** `validate.py` checks arithmetic (taxable + tax ≈ total;
line-item sums; tax_breakdown components; tolerance ~₹0.01–0.02) and formats
(date, 15-char ASCII GSTIN, currency). Failures LOWER confidence (cap 0.3) and
flag — they never silently correct.
**Why:** LLMs hallucinate and OCR garbles digits; deterministic checks catch
both regardless of model confidence.
**Status:** Active; implemented 2026-08-19 with the D24 pivot (multiplied into
`validate_output()` inside `extract.py run_batch`). Corpus quirk: classic_2
prints AMT-02a while its line items sum to AMT-02b — tolerated + flagged,
never "fixed".

---

### D9 — Page-aware multi-document handling  `ACTIVE`
**Date:** 2026-08-01
**Decision:** Extraction output carries page markers; broker selection
operates per document (page or page-cluster), not per file.
**Why:** `classic_2.pdf` contains three documents in three pages — file-level
selection would be wrong.

---

### D10 — Broker rules as hot-loaded YAML  `ACTIVE`
**Date:** 2026-08-01
**Decision:** `rules/brokers.yaml` (+ `rules/labels.yaml` since D24) read fresh
on every run; structure and weights are the contract.
**Why:** Owner requirement: rules changeable at any time (including
emergencies) with no code changes — this is data, not code.

---

### D11 — Output: per-run flags + per-file JSON + raw cache  `ACTIVE`
**Date:** 2026-08-01 (amended 2026-08-09, D18)
**Decision:** All stage output lives inside a run: `<run>/flags.csv` (per-run
flags — no master CSV), full detail in `<run>/json/<file>.json`, cached raw
text in `<run>/raw/`.
**Why:** JSON preserves confidence + sources; raw text makes review and
debugging possible without re-OCR; runs are self-contained snapshots.

---

### D12 — Dedup + idempotency  `ACTIVE (pending Stage 7)`
**Date:** 2026-08-01
**Decision:** Skip files already in the processed manifest; skip rows whose
(vendor, invoice number) already exists in the run's flags/manifest.
**Why:** >500/month, manual drops, and re-runs must not create duplicates;
invoice number + vendor is the natural key.

---

### D13 — Manual drop + CLI batch (no email pipeline)  `ACTIVE`
**Date:** 2026-08-01
**Decision:** User drops PDFs into `inbox/`; the pipeline runs as
`python process.py` (optional `--watch` polling flag).
**Why:** Owner explicitly said mailbox→folder automation is out of scope.

---

### D14 — Runtime: Python 3 stdlib + `pyyaml`; Swift only for OCR  `ACTIVE`
**Date:** 2026-08-01
**Decision:** Python for everything except the OCR primitive, which stays a
compiled Swift subprocess.
**Why:** Minimal dependencies (pyyaml — the YAML format D10 is built on),
reuses the empirically proven OCR harness, keeps OCR reproducible (source in
`swift/ocr.swift`, compiled on demand).

---

### D15 — Per-page text-source routing: embedded | OCR | empty  `ACTIVE · AMENDED (D24)`
**Date:** 2026-08-01
**Decision:** Every page is classified independently by the Swift harness:
embedded (clean text, not invisible-text mode) → PDFKit extraction; scan (no
layer, or layer below the quality floors) → OCR; blank → empty output. Rules:
embedded requires `chars ≥ 100`, no invisible text, and (`alpha ≥ 0.5` or
`words ≥ 20`).
**Why:** (1) Digital pages ship obfuscated custom-font layers (no ToUnicode)
that PDFKit decodes byte-accurately — re-OCR'ing them fuses vector text over
backgrounds into garbage; (2) a strict alpha gate misroutes number-heavy
invoices; (3) scans often wrap images in Form XObjects, which the probe
resolves.
**Status:** Amended 2026-08-19 (D24): routing now decides the quality of the
TEXT channel, which feeds ONLY the rules validator — the vision pass is fed by
renders written for every page regardless of routing.

---

### D16 — Document-quality gate: blank/illegible pages are excluded  `ACTIVE`
**Date:** 2026-08-01
**Decision:** `text_layer.py` post-processes every page into
`good | poor | blank | ignored` (order: ignored-rule → blank-0-lines-with-pixel-
confirmation → poor-below-floors → good). Excluded pages are dropped from the
combined text, recorded in `report.json` with a machine-readable reason, and
flagged `document_quality` — exclusion is recorded and flagged, never silent,
never a hard skip.
**Why:** DOC-D-01/56/69 share blank p6 + handwritten p7–10 whose garbage OCR
would poison extraction; no single automatic metric separates the handwriting
from legitimately sparse pages — floors were tuned by corpus sweep (precision
1.00, recall 8/9).
**Details:** harness `image_stats` (D16.1), line-category trash band (D16.2),
config-gated readability probe (D16.3, off by default), advisory suggestion
engine (D16.4), `gate-tune` sweep (D16.5 — removed 2026-08-11, floors locked
in `config/thresholds.yaml`). Full implementation notes → Appendix A16.

---

### D17 — OCR output hardening: geometry-based restructuring allowed  `ACTIVE`
**Date:** 2026-08-03
**Decision:** Before extraction, the raw capture may be reshaped by
deterministic geometry/text processing (reading-order sort, line
re-segmentation, column reconstruction, annotations). This is NOT field
extraction — no pattern matches a semantic value; only how lines are
presented changes. Owner's line: "as long as it is not some hardcode regex BS."
**Why:** Corpus evidence: merged table cells, collapsed label:value runs,
script confusions — structure reconstruction before the model beats more
prompting; the honest gate is the eval matrix.

---

### D18 — Run-based output layout + "file names are never a factor"  `ACTIVE`
**Date:** 2026-08-09
**Decision:** (1) Runs are first-class and self-contained
(`out/runs/<YYYY-MM-DD>/<N>/`, `make new-run`, `out/latest` symlink; never
overwrite a run). (2) FILE NAMES ARE NEVER A FACTOR — names are human-readable
labels only; detection/selection/quality/matching are content-based. (3)
Corpus renamed 12/12 (content-verified, old→new map). (4) Ground truth moved
to `tests/ground_truth.json`. (5) `gate-tune` deprecated.

---

### D19 — Capture reshaping: fragment persistence + deterministic reconstruction  `AMENDED (D30)`
**Date:** 2026-08-09/10
**Decision:** Per-fragment geometry persisted (`pageNNN.segments.txt`,
additive); `reconstruct.py` reshapes OCR pages — 1c-a fused-token splitting
(Indian lakh/crore grouping + duplicate signals only) and 1c-b column
alignment — into `pageNNN.rec.txt`, line count preserved so source pointers
never shift.
**Why/A-B results (historical):** 1c-a+1c-b path A delivered 86.9% header accuracy,
reconciliation 11/11); path B (Apple native tables) REJECTED; 1c-d (6× crop
re-OCR) conditional/zero matrix movement; 1c-c deferred; eval.py gained a
line-item source audit + reconciliation check. Source-file lesson: a Preview
re-save changed `sample_a_1.pdf` content — corpus files must never be opened in
auto-saving editors. **AMENDED 2026-09-02 (D30 cleanup):** `reconstruct.py` +
`--reconstruct` + the rec-file preference removed — rec files are never produced
in the standard flow (make ocr-corpus), and the verification stack's grounding
reads the per-page segments/bbox directly; the D19 segment-geometry persistence
(core of the old 1c pre-step) remains, still serving grounding.

---

### D20 — Render fix: normalized image placement + rotation + native resolution  `ACTIVE`
**Date:** 2026-08-10
**Decision:** The classic_1/2 "unreadable header" was a RENDER bug: phone-photo
PDFs placed at 1px=1pt crop to the mediaBox sliver. Fixed in the harness via
three parts: normalized image placement (unit-rect draw), /Rotate honored with
calibrated branch directions, and native-resolution scale for pure-image pages.
**Why/evidence:** pixel-proven (render = JPEG bottom-left crop, MAD 9.4);
viewer screenshot reads 8/8 with Apple Vision and Luna vision; 4 pages change
(classic_1 p1, classic_2 p1–3), Vendor D/Vendor C byte-identical.
**Residual:** classic_2's printed date is misread as `30-Jun-20:25` by Apple
Vision at every scale (Luna vision reads `30-Jun-2025` — the case that
motivated vision-primary, D24).

---

### D21 — Raw embedded-image extraction for the vision pass  `ACTIVE · AMENDED (D24)`
**Date:** 2026-08-10
**Decision:** The vision pass reads `pageNNN.image.png` — the RAW embedded
image XObject (color, full intrinsic resolution, /Rotate applied) for
pure-image pages — NOT a page-bounds render, because every page rasterizer
(PDFKit, Poppler-class, even the gateway's own PDF input, probed live)
clips 1px=1pt photos and cuts off the letterhead. Preference order:
`image.png` → `render-ocr-input.png` (the OCR input; rename of the old
`render-images-only.png` label, kept as compat fallback).
**Status:** Amended 2026-08-19 (D24): with the vision-primary pivot both dumps
are written UNCONDITIONALLY for every page (embedded pages get a PDFKit page
render as `render-ocr-input.png`); `--dump-renders` was removed.

---

### D22 — Local page-orientation detection (Apple Vision geometry, no LLM)  `ACTIVE`
**Date:** 2026-08-10
**Decision:** Sideways pages (Vendor D invoices print rotated ~90°) are detected and
corrected locally: tall line boxes trigger a 4-way A/B (0/90/180/270), best
composite wins; the winner's render becomes both the OCR input and the vision
input. Never uses an LLM for orientation.
**Why:** a single +90° A/B cannot disambiguate 90°-off from 180°-off (both
yield tall boxes); the 4-way A/B self-heals even render-branch errors.
**Evidence:** Vendor D eval improved (selection 3/3, dates 3/3, invoice_number 2/3);
residual sample_a_2 invoice_number variance + confident-bulk tax garbage documented.

---

### D23 — Dead-code cleanup  `ACTIVE (process record)`
**Date:** 2026-08-11
**Decision:** Removed dormant/deprecated machinery with no pipeline caller
(gate-tune sweep, `--only-pages`, `--documents-ab`/`--reconstruct-docab`,
`--crop-reocr`, `--images-only`, empty `.cursor/`), verified byte-identical
after removal (run 2026-08-11/1 == run 2026-08-10/10 page-for-page).
Kept: `validate.py` (stage-4 signpost → implemented 2026-08-19), quality
probe (config-gated), `--reconstruct` path A, `--ocr-only`/`--dump-embedded`/
`--scale` diagnostics.

---

### D24 — Vision-primary pivot: one vision LLM call + a deterministic rules validator  `AMENDED (D30)`
**Date:** 2026-08-19
**Decision (supersedes D6):** The pipeline is VISION-PRIMARY. A single
schema-constrained vision call per attachment (D1) reads the page renders
(written unconditionally; text-LLM extraction REMOVED) and does selection +
extraction. The text channel feeds ONLY a deterministic, local, free rules
pass — the VALIDATOR, never a second extractor:
1. **Rules locator** (`locator.py`): label-adjacency candidates
   (`rules/labels.yaml`, D10) over the selected pages' text for the
   owner-chosen fields (`locator_fields` in thresholds.yaml); format checks
   only (number/date/15-char ASCII GSTIN shape) — never value patterns (D5);
   silence is a valid output.
2. **Crosscheck** (`compare.py`): agree → keep vision confidence (owner
   decision); amount conflicts → deterministic ARBITRATION by line-item
   reconciliation (the value the document's own line items vouch for wins;
   both/neither → flag); non-amount conflicts and rules-only → flag rows with
   BOTH sources (render region + text line — dual-anchored flags). Every
   disagreement recorded in `_meta.crosscheck`.
3. **Deterministic validation** (`validate.py`, D8): arithmetic + format
   checks cap confidence (0.3) and flag — never silently correct.
All flags land in `<run>/flags.csv` (D11/D18). The trust boundary INVERTS the
D7/D6 design: vision is primary, the deterministic channel is the guard (the
D7 lesson — a wrong-but-confident vision read on sample_a_1 — is caught by conflict
flags and arbitration instead of a restrictive merge).
**Why:** project evidence — (1) 2026-08-19 token test on sample_b_1: vision
12,739 total tokens vs text 20,301 (−37%) for equal-or-better quality; (2)
vision fixed every chronic text failure (classic_2 date, sample_b
vendor_gstin/tax); (3) the second tier was a second expensive LLM call — the
validator is local and free; costs drop from up-to-2 calls per attachment to
exactly 1.
**Swap story:** `primary_model` in `config/models.yaml` is the single place a
vision model is chosen; swap ritual = edit YAML → corpus run → `make eval`
(matrix + crosscheck stats) → promote. Executions: 2026-08-26 luna (provider
down since 08-24) → qwen3.8-max (corpus 100%, run out/runs/2026-08-26/1);
2026-08-28 (cost) → deepseek-v4-flash-vision-exp. Swap #3 caveat: deepseek
read classic_2's total as the line-item sum AMT-01a vs printed AMT-01 —
internally consistent (arbitration + validation pass) but one wrong eval cell
(qwen inherited the same 0.20 quirk in the opposite direction and flagged it).
Image form is per-model: `image_url_form` flat (luna/qwen) | nested
(deepseek REQUIRED); verified live 2026-08-27/28.
**Watch-outs:** embedded pages now render for vision (PDFKit page render);
`source_region` is a prompt request the model may omit (flags stay
text-anchored via the locator); the locator must never become a full-value
extractor.
**Status:** Amended 2026-09-02 (D30): the label-adjacency LOCATOR + the
candidate crosscheck were replaced by the verification stack (grounding /
arithmetic / format / gated disagreement — see D30). The D24 core (one
vision call, text channel = deterministic guard only, flags dual-anchored,
thresholds produce review flags, trust boundary inverted) is unchanged; the
rules locator history lives in git.

---

### D25 — Document identification profiles  `ACTIVE — STAGED`
**Date:** 2026-08-28
**Decision (owner, staged):** document identity is DATA. `rules/documents.yaml`
defines TARGET DOCUMENT profiles — each {id, label, description, signals,
extraction} — and the pipeline extracts from the profile the user picks
(`extract.py --document`, UI picker; hot-loaded, D10). This replaces the
hardcoded "broker invoice" assumption and solves the same-name-documents
problem: the model identifies the target by the profile's SIGNALS (broker
PANs, required_PANs, vendor keywords), never by type name — a commercial
invoice is also "an invoice" but does not match the broker profile's signals.
- **Stage 1 (this decision):** one profile (`broker_invoice`), signal values
  sourced from `rules/brokers.yaml`; fixed v1 schema
  (`extraction.schema: fixed_v1`); status renamed `flag_no_broker_match` →
  `flag_no_document_match`; and a **selection guard**: the deterministic
  text-channel document scan (compare.detect_document →
  text_layer.detect_broker) is cross-checked against the vision selection —
  disagreement → `selection_conflict` flag row (dual-anchored); a MATCHED
  selection below the selector `min_score` now flags (`below_threshold`)
  where it was previously accepted silently. Prompt: `prompts/extract_v2.py` (deleted 2026-09-02 — superseded by v4).
- **Follow-up (not built):** dynamic per-document field schemas, direct
  signal lists per profile, negative/adversarial fixture suite expansion.
**Why:** disambiguating "which invoice in a multi-invoice PDF" needs
user-communicable signal contracts, not model intuition (owner example: PAN
numbers); and a confident-wrong DOCUMENT choice poisons every field
downstream — the D6/D7 logic, applied at the selection level.
**Watch-outs:** the text-scan guard inherits text-channel quality (OCR garbage
can cause selection_conflict flags on legit files — measure, never assume);
profiles beyond broker_invoice have no corpus ground truth — QA shifts to
crosscheck + validation + review.

---

### D26 — Document discovery & pinning  `ACTIVE — STAGED`
**Date:** 2026-08-29
**Decision (owner):** two ways to target the document to extract:
1. **Discovery-driven (interactive, <=2 calls/file):** the UI shows the page
   THUMBNAIL grid (free — renders already exist) so the user clicks the
   document they want (pages pinned); an optional CATALOG sweep
   (`extract.py --catalog`, 1 cheap call/file with a catalog-only system
   prompt — extract_v3) lists the PDF's documents {type, label, pages}
   rendered as pickable cards ("Invoice 2 — ENNORE CARGO CONTAINER TERMINAL
   [p3]"). Pinning constrains extraction to those pages; DRIFT OFF THE PIN IS
   A HARD selection_conflict FLAG (never silent re-selection).
2. **Profile-driven (bulk, 1 call/file):** profiles now define signals INLINE
   (pans/vendor_keywords/required_pans/any_keywords + per-profile min_score)
   or keep referencing brokers.yaml (the broker profile — phase "keep for
   now"). any_keywords = presence rule — the bulk-targeting mode ("the invoice
   containing Vendor D/Vendor B/Vendor A/Vendor C").
Non-invoice documents extract with a GENERIC MINIMAL schema
(document_title/date/reference/amount; no line items) — acceptance: generic
profiles are ground-truthless, QA shifts to crosscheck + validation + review.
**Why:** the user picks documents by WHAT THEY SEE, not by remembering their
rules; and "which invoice" in bulk runs must be expressible as a rule
(keywords), not model intuition.
**Watch-outs:** (1) discovery mode = up to 2 paid calls/file (bulk stays 1);
(2) the catalog is advisory — pinning by thumbnails is the source of truth;
(3) generic-schema fields have no corpus ground truth; (4) per-document
arbitrary field schemas remain the later follow-up.

---

### D27 — Per-document field schemas (user-defined extraction fields)  `ACTIVE`
**Date:** 2026-08-29
**Decision (owner):** document profiles define their OWN extraction schema —
`extraction.fields: [{name, type, description}]` in rules/documents.yaml (types:
str | date | amount | name | object). The prompt's schema block + field notes
are GENERATED from the profile at runtime (extract_v3 PROFILE_USER_TEMPLATE),
and validation/fields-kind/arbitration/flag-thresholds derive from them.
The broker_invoice profile now extracts: invoice_number, invoice_date,
boe_number (BOE == BE, Bill of Entry), boe_date, total_invoice_amount +
line items (description + amount, per the profile's line_items_note) —
replacing the old 11-field fixed_v1 set (vendor/GSTIN/currency/taxables are
gone from the DEFAULT output; add them back with one YAML field line each).
**Why:** the extracted data should be the business's field list, not the
pipeline's; BOE information belongs on the broker invoice profile.
**Watch-outs:** (1) eval is now profile-aware — the strict matrix scores
profile fields ∩ ground truth (invoice_number/invoice_date/
total_invoice_amount + selection; boe fields have no truth entries yet —
can be added to tests/ground_truth.json); (2) corpus re-run needed to
re-baseline (spot check: classic_2 BE/BOE BOE-01 + 30-Apr-2025 read
correctly, conf 0.99); (3) legacy schemas (fixed_v1/generic) remain supported
for other profiles; (4) line-item amount drift surfaces via the
total_invoice_amount reconciliation flag (designed catch).

### D28 — Self-contained profile fields (thresholds, labels inline)  `ACTIVE`
**Date:** 2026-08-29
**Decision:** a document profile's fields carry their OWN wiring:
`extraction.fields[]` accepts `threshold` (0–1) and `labels: []` inline.
Threshold resolution everywhere (crosscheck + validation): profile field's
inline threshold -> thresholds.yaml -> TYPE DEFAULT (amount 0.95 / date 0.9 /
name 0.9 / str 0.8 / object 0.8) — NEVER a silent no-bar. Locator labels
resolve field-inline -> labels.yaml -> silent. Whitelists are seeded DYNAMICALLY
from documents.yaml (profile-declared names are always known — no warning
noise). One profile block = the whole contract: signals + fields (with bars +
labels).
**Why:** editing a document profile must be a single self-contained YAML block
(plus the optional UI settings editor later) — no file-hopping across
thresholds.yaml / labels.yaml / config whitelists.
**Watch-outs:** thresholds.yaml/labels.yaml remain the global fallback for
legacy schemas (fixed_v1/generic); type defaults are the last resort and are
overridable per field.

### D29 — Vision-led selection, brokers.yaml REMOVED  `ACTIVE`
**Date:** 2026-08-29
**Decision (owner):** the broker-identification system is REMOVED for now. The
vision model identifies the target document by its OWN printed name/heading
(selected_document.broker = the name READ on the document, free-form — no PAN
registry, no broker signal blocks, no required_pans); the profile's
USER-DEFINED `keywords` are helper presence-words for ambiguous same-name
documents, and the user's PIN (thumbnail selector) is the ultimate
identification. `rules/brokers.yaml` is DELETED; the text-channel broker scan
(detect_broker/detect_document) is REMOVED — the selection guard is now
PIN-DRIFT (hard flag) + selection-confidence + validation + review. Custom OCR
words come from profile keywords; eval scores selection by NAME equality vs the
ground-truth vendor, no registry.
**Why (owner):** per-broker routing (identifiers like PAN per broker, automatic
profile dispatch across 100 files) is future bulk-pipeline work with a real
requirement behind it — it returns THEN, built deliberately; carrying the
machinery now is dead weight (D23 discipline: remove dormant systems).
**Watch-outs:** (1) a profile with empty keywords identifies by description
only; (2) low-volume selection quality now rests on the model reading names +
the pin + the hard guards — corpus spot-checked (classic_2: name read
correctly, keywords cited in the selection reason); (3) brokers.yaml history
lives in git.

### D30 — Verification stack replaces the rules locator  `ACTIVE`
**Date:** 2026-09-02
**Decision (owner):** the deterministic rules validator is no longer a
label-adjacency EXTRACTOR (locator.py + rules/labels.yaml — retired with the
"BE"⊂"NUMBER" substring class and the phantom-conflict noise it produced:
the 2026-08-26 corpus run flagged 7 conflicts, ALL validator-side OCR garbage;
run 2026-08-29/10 flagged correct boe values on all three sample_b files).
It is now a SANITY CHECKER over the primary model's own output — four
independent checks, aggregated per field into accept | review_secondary |
review_human (verify.py / grounding.py / aggregate.py / containment.py;
arithmetic + format rules in validate.py, data-driven):
1. **Grounding** — the claimed value exists as a normalized token in the text
   channel near the claimed source_region (strict, kind-aware containment —
   plain string ops, no regex, no third-party libs; a 1-char drift like
   MODMW36 vs DOC-D-01 FAILS by design). Two bands: tight (1.0) and a loose
   row band (grounding_loose_score 0.6 default) for the measured x-offset
   imprecision of vision regions. No region → check skipped, never failed.
2. **Arithmetic** — invariants declared per profile (extraction.arithmetic,
   data not code); a failed rule implicates ALL involved fields (never a
   culprit guess) and hard-fails to review_human.
3. **Format** — value_type-driven table; fail only when a validator exists
   and fails.
4. **Disagreement** — GATED: a cheap-model re-extraction with a DIFFERENT
   prompt framing (extract_v4.RECHECK_NOTE), only when a field is flagged
   (aggregate level != accept), capped per file
   (verification.recheck_max_calls: 1 — the D6 economics lesson, D24; a cap
   > 1 rechecks the next disputed document in turn). Same-model-framing is
   the spec-sanctioned weak form; a second
   model is a YAML swap (verification_model + model_overrides). Disagreement
   hard-fails to review_human; it never overrules the primary.
Aggregation: weighted score (defaults grounding .25 / arithmetic .25 /
format .15 / disagreement .35 — convex, hypothesis-tuned via
`make eval-verify`); hard-fail checks force review regardless of score.
Confidence stays the primary model's — verification never re-scores (D7).
Flags keep FLAG_COLUMNS + dual anchors (claimed region + text-channel
evidence); per-field check audit in `_meta.verification` (+ recheck record).
Selection guards (no_document_match / sub-min_score / pin-drift /
below_threshold) moved to verify.py unchanged.
**Why:** measurement (tests/verification_truth.json + eval_verification.py:
84 scored corpus cells + 10 synthetic fixtures): the old pipeline = precision
0.636 / recall 0.875 (1 confident-wrong read missed: sample_c_1 date 18/06 vs
16/06). The stack = recall 1.000 (the missed class caught by strict grounding)
with FPs limited to documented classes (OCR-garble dates the text channel
cannot confirm, e.g. classic_2 30-Jun-20:25; correct headers under imprecise
regions; line-item drift surfacing via arithmetic). The embedded-geometry
prereq (PDFKit characterBounds per line, pageNNN.bbox.txt rows now real for
embedded pages; pageNNN.txt byte-identical) landed in the harness for
grounding to see typed pages. Corpus evidence (2026-09-02/2, luna primary):
selection 11/11, header accuracy 95.5% (the 2 sample_a invoice-number drifts —
both FLAGGED by grounding with the correct value in the text channel +
recheck disagreement), zero phantom conflicts, 6 gated rechecks, sources
52/52.
**Cost levers (2026-09-03, continuation — measured):** (1) quality-gate
excluded pages never reach the model (`page_renders` reads
`quality_gate.excluded_pages`); (2) `--vision-cap <px>` (Makefile VISION_CAP,
default 1536) — the harness ALSO writes `pageNNN.render-vision.png`, the page
render downscaled to that long edge FOR THE VISION PASS ONLY (input tokens
scale with image dimensions; OCR/grounding keep full-res renders): corpus
prompt tokens 603,595 → 224,777 (−63%) with gates unchanged; (3) pin mode
sends only the pinned pages (the pin IS the identification); (4) the gated
Check-4 is NARROW (`recheck_fields`: disputed fields, disputed pages only) —
recheck overhead 103% → 11.6% of primary cost; (5) max_tokens default 8192
(completions peak ~2.2k). Total corpus cost: 267,444 tokens (was 619,688).
**Watch-outs:** (1) grounding inherits text-channel quality — a value the OCR
garbles at the token level ("30-Jun-20:25") is honestly unconfirmable and
flagging it is correct-by-design (review resolves in seconds); (2) loose-band
confirmation (0.6) flags correct values under imprecise regions at
review_secondary — the gated recheck resolves those; (3) model-catalog drift:
deepseek-v4-flash-vision-exp provider-down, qwen3.8-max lost its
chat/completions route as of 2026-09-02 — verification_model = luna
(framing-variant recheck) until a healthy cheap second model returns;
(4) eval-verify remains the tuning loop for weights/floors — nothing in the
aggregation is final.

### D31 — Line-item row checks  `ACTIVE`
**Date:** 2026-09-07
**Decision (owner):** the verification stack gains ROW-LEVEL line-item sanity
(`verify.check_line_items`) — driven by the 12-file line-item accuracy test
(profile `broker_lineitems_test`: line items + total tax only, all 12 corpus
invoices). (1) COMPLETENESS: every line item MUST carry an amount (its
`total` column) — a row without an amount is unusable; (2) CONSISTENCY:
`taxable + tax == total` (0.02 tolerance) when those columns are present;
tax ABSENT means a tax-free row — then `taxable == total` (tax may
legitimately be missing, an amount may not). Flag reason `line_item_issue`
(field `line_item_<n>`, row's source pointer); never decides, never
auto-fixes (D8 semantics). The test's findings, now permanent catches:
classic_2's cross-row tax split (li1 AMT-04 vs printed AMT-05 while
li2 carries exactly 108.00 — aggregates reconcile, rows don't) and
sample_c_2's dropped tax/total columns (header tax read fine). Unit-tested
(6 cases); verified by local recompute — zero paid calls.

### D32 — Cross-document validation rules (the per-shipment pertinence check)  `ACTIVE`
**Date:** 2026-09-07
**Decision (owner, brainstorming → spec):** workflows (`rules/workflows.yaml`)
may declare `cross_checks` — equality rules between a field extracted on ONE
document entry and a field on ANOTHER entry of the SAME file, the product's
core differentiator (the operator's daily mental model: "does the attached
BOE/HAWB/commercial invoice pertain to THIS broker invoice?"). Rule =
`{id, label?, left: {template, field}, right: {template, field}}`; data, not
code (D10); validated loudly by config (templates exist AND are in the
workflow's `documents` — they must be extracted to be compared; fields must
be profile-defined — legacy schemas cannot participate; kinds compatible:
date=date, amount=amount, str/name mix; a rule always spans two DIFFERENT
templates). Executor `verify.check_cross_document` (D30 discipline: a sanity
checker over the primary's own output — kind-aware equality via
`values_equal`, never a second extractor, never a pattern):
1. **Found entries only; silent skip** when either side's template is absent
   (the workflow `expected` completeness contract owns presence —
   `missing_expected_document`) or neither side carries a non-null value;
   the skip is recorded in the audit, never a flag.
2. **Multiplicity = pass if ANY candidate pair matches** (e.g. two supplier
   invoices, one matching the BOE reference → pertinence proven).
3. **All rules fire INDEPENDENTLY** (no depends_on) — a wrong BOE surfaces
   the whole broken chain to review at once; the engine never guesses which
   side is right.
4. **Output:** one dual-anchored flag row PER SIDE (FLAG_COLUMNS unchanged;
   `template` = that side; each row anchors on its own value + source_region,
   the detail names the other side's candidates + their sources), reason
   `cross_document_mismatch`; full per-pair audit in
   `_meta.verification.cross_checks`. No gated recheck (both values come from
   the same primary call; each side's own grounding already guards its read).
**Why:** at >500 shipments/month the validation between the attached documents
is the review workload; a deterministic identity check turns "did I attach the
right BOE/HAWB/CI" from eyeballing into a flag. It runs at zero marginal cost
on the extraction JSON the one paid call already produced.
**Shipped:** `bill_of_entry` profile (own BOE no./date + the HAWB and supplier
invoice numbers it REFERS to — the chain's anchor), demo workflow
`shipment_approval` (the 3-step chain: BOE on invoice == BOE; HAWB on BOE ==
HAWB; invoice on BOE == commercial invoice), config contract + contract tests
(11), executor + unit tests (11), run_batch wiring (audit + flag rows +
summary), UI Workflows-tab rule builder + workflow-summary cross column +
multi-doc evidence anchoring (`find_field` now resolves document entries).
**Watch-outs:** (1) rule references are STRICT code/date/amount tokens —
comma/format noise is absorbed by normalization, a real digit drift is not
(the MODMW36 class); (2) adding `bill_of_entry` to an existing corpus
workflow changes its paid prompt (4 templates) — the demo is a NEW workflow,
the corpus benchmark workflow stays untouched until real BOE-bearing
files exist; (3) cross flags join flags.csv and thus the eval-verify baseline
counts — future runs with the workflow need truth entries for the chain
(unit + synthetic fixtures cover it today).

### D33 — Template learning from samples (the Learn tab)  `REMOVED 2026-09-23 (D40)`
**Date:** 2026-09-07
**Decision (owner, brainstorming → spec):** for complex, boxy, multi-part
documents like the Indian Bill of Entry (30+ data points, 4–18 pages), the
manual per-field template builder is impractical and page-anchoring is
impossible (page counts vary). The Learn tab learns a template FROM EXAMPLES:
the user uploads ≥2 sample PDFs of the SAME kind, the pipeline learns the
DATA POINTS (field labels + structure) and the user picks which to extract.
- **Per-sample discovery** (1 paid vision call each, `prompts/discover_v1.py`
  DISCOVER_*): reads the sample's pages, reports the field LABELS organized
  by the document's OWN Part → Section, a proposed type (str|date|amount|
  name|object), the label-value geometry (`value_side`: right|left|above|
  below — the boxy-layout learning), identification keywords, and a
  has_line_items/line_items_note. **NEVER reports values printed on the
  paper** — data points only, and no sample value ever reaches extraction.
- **Text-only merge** (1 cheap call, MERGE_*): unions the per-sample lists,
  reconciles the same data point named differently per sample into one field
  (labels = all spellings), tags `seen_in` (union semantics; the UI can
  filter to fields seen in ALL samples). Output `<run>/discovery/merged.json`.
- **Profile fields gain an optional structural anchor** (`anchor:
  {part, section, value_side}`) — rendered into the extraction prompt's field
  notes ("Part 2 · Section B; value printed below the label"). Page-agnostic
  by design: the model finds the part wherever it sits in a new BOE.
- **Learn tab** (dedicated): upload samples → capture → `extract.py
  --discover-fields` → review the learned data points (pick/edit name/type/
  value_side; Part/Section + seen-in read-only) → create a normal profile in
  rules/documents.yaml → existing Templates-tab save-and-test.
**Why:** the operator's mental model — "lay the 3 documents on top of each
other and see what the data points are" — is exactly a cross-sample structural
consensus; encoding it as a discovery pass keeps extraction as ONE paid call
per file (the learned template is just data; the extraction prompt is still
generated from it, cost unchanged).
**Cost:** discovery is paid (N vision calls + 1 tiny text merge, vision-cap
applies); creating and using the template is free.
**Watch-outs:** (1) the discovery prompt must resist value-copying — enforced
by ground rules + schema validation (a value-shaped string would fail type/
shape review); (2) the merged candidate list can be large (BOEs > 30 fields)
— the UI groups by Part/Section and defaults all-on, user filters; (3) anchor
reliability depends on the kind's structure being standardized (owner
confirmed BOE parts generalize across ports); (4) E2E paid validation needs
the owner's real BOE samples — until then the offline tests cover the schema
contract + merge substitution only.

### D34 — Cleanup: dead code, dead config keys, fixed_v1 retirement  `ACTIVE (process record)`
**Date:** 2026-09-21
**Decision (owner):** stop feature work and remove what adds no value.
- **fixed_v1 RETIRED.** The legacy D8 `validate_output` path (validate.py),
  the fixed_v1 prompt block (prompts/extract_v3.USER_TEMPLATE + the v4
  re-export), `HEADER_FIELD_NAMES`, and the extract.py schema branch are
  deleted — no shipped profile used it and it crashed in multi-document mode.
  `ALLOWED_EXTRACTION_SCHEMAS` is now `["generic"]` (D26); fieldless profiles
  are single-document only, and `load_workflows` now REJECTS workflow members
  without `extraction.fields` (the multi prompt is generated from fields).
- **Dead config keys removed:** `verification.grounding_threshold` (never
  read), `thresholds.selection` (selection uses `signals.min_score`) and
  `thresholds.line_items` (row math, not a confidence bar),
  `documents[].max_documents` (validated but never enforced; the multi-doc
  prompt phrase is gone too — the prompt delta supersedes the 2026-09-05/07
  multi-doc evidence for a re-baseline on the next paid run).
- **Dead code removed:** eval.py `truth_broker`/`extract_sources`,
  grounding logger, llm_client `Optional`, Swift `assembleLines` +
  `ScanState.pageRef`, text_layer's unreachable `skipped` branch, UI
  `rules_pointer`/`text_lines` (they parsed a `rules: page N, line M` format
  no current flag emits — the Review tab now shows the sources/detail text),
  the shadowed legacy `save_uploads(unique=False)` branch, unused test
  helpers/imports.
- **Orphaned fixtures removed:** tests/fixtures/neg_commercial.pdf +
  mkpdf.swift (zero code references; the D25 evidence lives in this log).
- **Runtime clutter pruned:** out/abtest + the pre-D30 Aug runs + untracked
  testinvoices/ + .DS_Store/__pycache__; `eval_verification.py` DEFAULT_RUNS
  now targets the full-corpus D30 run `2026-09-03/5` (raw defaults to that
  run's capture).
**Why:** the repo had accumulated superseded paths that were config-reachable
but untested/unenforced, and the state docs disagreed with the code in
places. Removal is the cheapest way to keep the verification-first contract
honest. Model state restored the same day: luna + GO endpoint (see
config/models.yaml history).

### D35 — Vision-only model picker: live catalog + models.dev hints + gateway probes  `ACTIVE`
**Date:** 2026-09-22
**Decision (owner):** the UI must offer ONLY vision-capable models, and the
catalog must track opencode's live catalog (models are added/removed
constantly). Three layers, in trust order (`model_catalog.py`, stdlib only):
- **AVAILABILITY — the live catalog** (`GET {endpoint}/models`, ~3KB, no
  auth): the only authority on which ids exist. TTL 5 min + last-snapshot
  fallback; the refresh also diffs `new`/`removed` vs the previous snapshot
  (`out/cache/go_catalog.json`). The payload carries NO capability metadata
  (verified: `{id, object, created, owned_by}`; `/models/{id}` returns HTML).
- **CAPABILITY HINT — models.dev** (`api.json`, community DB): provider
  `opencode-go` (38/40 GO ids) with `modalities.input`, attachment, cost and
  context. TTL 24h, slim projection cached (`out/cache/models_dev.json`).
  A HINT only: it has disagreed with live gateway behaviour (kimi-k3,
  grok-4.5, mimo-v2-omni were rejected live while models.dev claimed image).
- **GATEWAY TRUTH — the PROBE**: one tiny image call (~200 tokens) through
  llm_client, trying transports in order (per-model override -> chat flat ->
  chat nested -> responses); the first success is recorded, so the probe also
  discovers the SWAP-TRAP transport. 4xx = rejected (not vision); 429/5xx =
  transient (never blacklisted); 401/403/404 = auth/route (abort).
- **UI**: selectable = `vision_verified` (config/models.yaml) ∩ live catalog,
  always including the configured primary with a ⚠ label; metadata text-only
  models hidden; a collapsed "Vision candidates" expander offers per-model
  **Verify** and a batch **Verify all (~$)** (manual only — no surprise
  spend); drift banner for new/removed; `Refresh catalog` button; labels show
  `$in/$out · ctx`. **Make default** writes the probed transport
  (api_style/image_url_form) — the SWAP TRAP is fixed end-to-end
  (llm_client._endpoint now routes per-MODEL style, not the top-level one).
- **Pipeline**: `extract.py`/`process.py` warn (never fail) when the cached
  snapshot no longer lists the primary; `make models` prints the status table
  (`ARGS=--refresh` bypasses TTLs).
**Why:** the old picker listed all 40 catalog models, so a text-only pick
failed at call time; and opencode's catalog changes without notice. Free
metadata narrows the candidates, a cheap probe settles capability on THIS
gateway, and the live catalog settles existence — each layer doing what it is
actually authoritative for.
**Watch-outs:** models.dev is third-party (no SLA; offline falls back to the
last snapshot, then verified-only); probe results in `vision_verified` are
paid evidence — `out/cache` holds only timestamps/transport (pruning it never
loses a verification); the verification_model (gated recheck) sends renders
too, so it should also come from `vision_verified` (owner follow-up).

### D36 — Trustworthy evidence boxes: text-channel locate + harness geometry fix  `ACTIVE`
**Date:** 2026-09-22
**Problem (owner, core-functionality):** the highlight box and the source-region
crop often showed the wrong spot ("not even close"), and crops were blurry /
too zoomed. Measured on the D30 corpus: the vision model's `source_region` is
an unverified estimate — median center error 6% of the page, 37/64 fields >5%
off, with wrong-adjacent-label (sample_b BOE claimed on the HAWB line),
wrong-row (sample_a_3 date), and wrong-PAGE cases (BOE date 21/02/2025 claimed p1,
printed on p3-9/13). Crops also upscaled a ~200x60px tight box to ~700px
(3x blur) with no label context.
**Decision:**
- **Text channel = the location authority (display-only).**
  `grounding.locate_value` finds where a value is actually printed: exact
  span matches (`exact_containment`) + partial line matches (n-gram ratio or a
  long value token — OCR-spaced digits like 'WAYBILL 45 7313 6702', truncated
  multi-token values). Ranking: strong candidate on the model's claimed page
  first (that is where it actually read it), then exact > partial, profile
  `labels` hit, claim proximity, longest line. It NEVER feeds verification —
  no flags, no confidence (D30 discipline).
- **The grounding match is persisted** (`verification.checks[grounding].match`
  = fragment+line bbox, text, band, lineno) — audit + stored fallback.
- **UI evidence rework:** Review Flags' "Location" pane shows the
  confirmed/likely crop at natural size with the located box drawn on it,
  plus a page thumbnail; page-mismatch is called out ("found on page 13;
  model claimed page 1"). Explorer draws located boxes (✓text / ~text) and the
  model claim as a muted outline when it differs; multi-document entries are
  flattened BEFORE the overlay (was empty for workflow runs); a wrong-page
  field auto-jumps the page selector once per file.
- **Harness geometry fix (root cause of loose boxes):** for embedded pages,
  `embeddedLinesGeometry` used `characterBounds(at:)` unions whose index space
  DRIFTS from `page.string` on multi-column invoices (sample_b_1 BOE union
  spanned both columns/two rows; classic_3 GSTIN union x 0.25-0.87). It now
  uses `page.selection(for: NSRange)` bounds (same index space as the text),
  with characterBounds as fallback. `text_layer.ensure_binary` now rebuilds
  when the Swift source is newer than the cached binary (a source edit was
  silently ignored before).
- **UI bug fixes found en route:** `lib.find_field` returned None for
  single-document flags (their rows carry the template column) so the Review
  pane showed no crop at all; fixed to fall through to top-level fields.
**Evidence:** locate over all recent runs — 227/229 fields located (99%), 17
page-mismatches surfaced, 45 partial (OCR-spaced/truncated), 0 errors; visual
before/after crops for the known-bad cases (sample_b BOE, sample_a_3 date,
p13 BOE date, spaced HAWB number) verified; 172 unit tests green.
**Watch-outs:** runs captured BEFORE 2026-09-22 keep the old (drifting)
embedded geometry — their locate boxes are looser (still far better than the
raw claim); re-capture for exact boxes. Locate is a display heuristic: when
the text channel is garbled it finds nothing and the UI falls back to the
model's claim, explicitly labeled unconfirmed.

### D37 — Paid stages survive the UI: detached runners + discovery resume  `ACTIVE`
**Date:** 2026-09-23
**Problem (owner):** "Learn structure" on two real BOEs finished the local
capture and BOTH paid per-sample discoveries (files on disk, 24-26k tokens
each) but the MERGE never completed and the UI spun forever. Diagnosis:
- the merge call **succeeded when re-run** (41.6s, 33KB, 0 validation
  problems) — the original failure was transient, and `_merge` used
  `retries=0`, so ONE 429/5xx/network blip killed the whole merge;
- `run_discover_fields` wrote **no artifact on merge failure** (only a log
  line to a stdout pipe that the UI had already abandoned), so nothing on disk
  explained the failure;
- the Learn pipeline ran INSIDE the Streamlit script (`stream_pipeline`): any
  rerun/reconnect abandons the generator, the status widget is never resolved,
  and the paid run's result is lost with no resume (re-clicking re-pays the
  per-sample discoveries).
**Decision:**
- **Merge resilience:** `merge_samples` = single text-only call
  (transport-retried via `llm_client.DEFAULT_RETRIES` + schema retries) with a
  **per-part fallback**: when the single call cannot produce a valid list,
  merge one document Part at a time (small payload/response per call) and
  UNION deterministically — `labels` = union of printed spellings, `seen_in`
  recomputed from the samples (never trusted from the model), section
  placement derived from where the labels were printed. A heartbeat log line
  precedes every attempt.
- **Discovery resume:** `extract.py --discover-fields --skip-done` reuses
  valid `discovery/<stem>.json` samples (no paid re-discovery) and a valid
  `merged.json`; a FAILED merge marker is retried (merge only). On merge
  failure the code writes `discovery/merged.json = {status: "failed", error,
  hint}` — the UI shows why and offers "Retry merge only".
- **UI detached stages (shared mechanism, Learn first):**
  `ui/lib.start_stage` launches a CLI in its own session with the log at
  `<run>/logs/<stage>.log` and a shell-written `<stage>.exit` (pid in
  `<stage>.pid`); `stage_status` re-attaches from disk. The Learn tab polls
  with `st.fragment(run_every=1)` and advances capture → discover → results;
  reruns, tab switches and browser reconnects can no longer lose a paid run,
  and a killed process (no exit marker) is surfaced with Retry/Dismiss.
**Evidence:** the failed run `2026-09-23/1` was resumed with the new path —
2 samples reused, merge 49s, exit 0, 5 parts / 97 fields; the Learn tab renders
the field picker from it; 184 tests green (merge fallback, deterministic
union, skip-done reuse, failure artifact, stage runner).
**Watch-outs:** other tabs (Upload & Run, Workflows) still use the in-script
`stream_pipeline` — migrate them to `start_stage` next; the batched fallback's
Part→Section placement is a deterministic approximation (single-call path is
preferred when healthy); `<run>/logs/<stage>.log` is the debugging trail for
any paid stage now.

### D38 — Upload & Run UX: in-tab target selection, optional pinning, detached stages  `ACTIVE`
**Date:** 2026-09-23
**Problem (owner):** after a normal Upload & Extract the D26 pin/capture panel
appeared anyway (it belongs to the capture-only flow); its File dropdown showed
content-hash ingest stems ("7737846a-01-x") the owner had never seen; pinned
extraction only accepted ONE template; and the document-kind selector lived in
the sidebar, far from the action.
**Decision:**
- **Target selection moved into Upload & Run** ("Extract these document
  kinds", multi-select; sidebar keeps the model picker + catalog status only;
  the `target` brand chip is gone). The selection drives the run AND becomes
  the pin panel's default.
- **Pinning is explicit:** expanded only right after "Upload & Capture only";
  after a normal run it is a collapsed "Pin a specific document (advanced)"
  expander. Capture-only runs show "Captured N file(s)" instead of zeroed
  metrics.
- **Original filenames everywhere:** the pin File dropdown labels come from
  the run manifest (`resolve_stem`), with the ingest stem shown as a caption;
  `pin_file_default` now stores the STEM (the old original-name default never
  matched, silently picking the first file). Failed-file warnings/retries also
  use original names.
- **Multi-template pinned extraction:** the pin profile selector is a
  multiselect (→ `--documents a,b`), the pin label defaults to the joined
  profile labels, and the catalog "Detected documents" cards use the selected
  profiles instead of the hardcoded invoice→broker mapping.
- **Upload & Run runs detached (D37):** capture→extract chains through
  `start_stage`/`stage_status` with a `st.fragment` poller; retry, pinned
  extraction and catalog sweeps are stages too (retry/pin run
  `recompute_flags` after exit). Paid runs here now survive reruns/reconnects
  and their logs live under `<run>/logs/`.
**Evidence:** detached E2E with the exact UI arg builders — capture exit 0 in
1s, extract exit 0 in 18s (classic_3 extracted, luna, 4,506 tokens); AppTest:
no sidebar selector, panel collapsed after a normal run and expanded with
capture-only, File dropdown shows "BOE3-41-49.pdf"/"BOE1-64-73.pdf" on a
hash-stem run; 188 tests green.
**Watch-outs:** Workflows + Templates still stream in-script (next migration);
the pin-label text input keeps its text when the profile selection changes
(edit it if the auto label is stale).

### D39 — Review write-through: confirmations and issues land in the final JSON  `ACTIVE`
**Date:** 2026-09-23
**Problem (owner):** Review-tab actions only wrote `<run>/review.json` — the
final `json/<stem>.json` never showed that a value was confirmed or disputed.
The evidence pane's fixed-width crop (up to 720px) overflowed its half column
and covered the text-channel pane; the flags table printed raw ingest stems
("d769c49b-01-x4") instead of the original filename.
**Decision:**
- **`ui/lib.apply_review`** mirrors every action into the extraction JSON:
  the matching field (top-level, or the `documents[]` entry for the flag's
  `template`) gets `review = {status, note, reviewed_at}`. On a CONFIRMED
  edit the value is replaced and `review.original_value` preserves the model's
  read; ISSUE never changes the value. Non-field rows (selection/line-item/
  duplicate/completeness) and a run-level summary go to
  `_meta.review.entries` + `confirmed`/`issues` counts. Writes are atomic;
  confidence and `verification` are never touched.
- `review.json` stays the source of truth (stable flag identity);
  `apply_reviews` re-mirrors it after a retry/pin re-extraction (the detached
  poller calls it before `recompute_flags`), and the Review tab offers
  "Apply all reviews to JSON" for runs re-extracted outside the UI.
- **Review tab UX:** stacked evidence inside a real `st.container(border=True)`
  — full-width crop (`width="stretch"`, no overflow) then thumbnail | text
  channel; an editable **value** field prefilled from the JSON; the flags
  table shows original filenames + a review-status column and gains a
  "Hide reviewed" toggle; the flag selector and the Explorer/Runs file
  selectors use `resolve_stem`.
**Evidence:** the owner's run `2026-09-23/5` (stem `d769c49b-01-x4`) now shows
`BOE1-64-73.pdf` in the table; on a copy of it, a confirmed correction wrote
`value=BOE-02-corrected-CORRECTED` + `review.original_value=BOE-02` into
`bill_of_entry.boe_number`, an issue marked `boe_date` without touching its
value, and `_meta.review` counted 1/1; 198 tests green.
**Watch-outs:** `flags.csv` is never edited (regenerated by recompute); a
value correction is always explicit (`review.original_value`); object-valued
fields are not editable in the UI (annotate only).

### D40 — Remove the Learn tab (sample-based template learning)  `ACTIVE (process record)`
**Date:** 2026-09-23
**Decision (owner):** the Learn tab (D33) is REMOVED — the discovery/merge
engineering (per-sample vision calls, text-only merge with per-part fallback,
anchored field review UI) served too few cases for its surface. Removed:
`discover.py`, `prompts/discover_v1.py`, `tests/test_discover.py`,
`extract.py --discover-fields` (+ dispatch), `ui/lib.discover/load_discovery`,
the Learn tab + its helpers (`tab_learn`, `_learn_rows`, `_unique_profile_id`,
`_start_learn_stage`, `_learn_stage_progress`), and the tab entry (UI is now
6 tabs). `--skip-done` remains (extraction resume) with its help trimmed.
**Explicitly kept:** the **Templates** tab (profile creator/editor), the
**Workflows** tab, the D26 **catalog/pinning** flow, the D37 **detached stage
runner** (used by Upload & Run; the discovery-resume half of D37 is moot), and
the optional structural `anchor` profile field (config validation + prompt
field notes + the Templates read-only Anchor column) — no shipped profile uses
it, but it is plain profile data and Templates preserves it.
**Why:** less surface, less paid-path complexity; profiles are still created
and edited in the Templates tab (or YAML), and the remaining tabs share the
same run/verification/review machinery unchanged.
**Evidence:** 173 tests green (the discovery suite removed), `extract.py
--help` no longer lists `--discover-fields`, AppTest renders 6 tabs with no
exceptions, orphan grep for all removed symbols is empty. Runtime
`out/runs/*/discovery/` artifacts (gitignored) were left on disk.

### D41 — Runs dashboard: friendly table + non-zoomable token chart  `ACTIVE`
**Date:** 2026-09-23
**Problem (owner):** the Runs table dumped raw dicts (absolute path + 9 cramped
columns, unformatted numbers) and the token chart was broken: its x labels
were the run NUMBER only (`r["dir"].split("/")[-1][-9:]` → "5"), and
`st.bar_chart` is Streamlit's native Vega chart — interactive by default
(scroll-zoom/drag-pan, no `interactive=False`), so it zoomed infinitely; its
default `sort=True` also ordered bars by value, not time. Zero-token runs
vanished silently.
**Decision:**
- **`list_runs` (additive):** `prompt_tokens`/`completion_tokens` from
  `_meta.usage`, `kind` (workflow if any JSON has `_meta.workflow`,
  extraction when JSONs exist, else capture), and `reviewed` (review.json
  confirmed+issue count). New pure helper `runs_chart_frame(runs, metric)`
  builds chronological chart rows ("total" or "per_file") and reports skipped
  zero-token runs.
- **Table:** friendly columns — Run (`09-23 · #5`), Kind, Model, Files,
  Failed, Flags (`ProgressColumn` scaled to the busiest run), Reviewed,
  Tokens (localized). Row selection opens a details panel (files/failed,
  flags/reviewed, prompt+completion tokens, total, path) and preselects the
  "Open a run" picker — ONLY when the selection changes, so a manual pick in
  the dropdown is never overwritten on reruns.
- **Chart:** an Altair layered bar spec (prompt vs completion stacked, gold/
  bronze), chronological `run:N` with `sort=None`, tooltips (run/kind/model/
  files/flags/tokens), `~s` y-axis, and **no `.interactive()`** — zoom/pan are
  impossible. Controls: "Runs shown (newest first)" slider (default 20) and a
  Total tokens | Tokens per file toggle (per-file divides by the run's file
  count); capture-only runs are excluded with a caption. Rendered with
  `st.altair_chart(chart, width="stretch")`.
- **`pick_run` labels** across Runs/Review/Explorer: `09-23 · #5 · model`
  instead of the absolute path (values stay the run dir).
- `requirements-ui.txt` pins `altair>=5` + `pandas>=2` (both were transitive
  Streamlit deps; now explicit because the UI imports them).
**Why:** the native chart's interactivity is not configurable and its labels
were wrong; Altair gives a deterministic, readable, non-zoomable chart with
real tooltips, and the table now reads like a dashboard instead of a dump.
**Evidence:** 178 tests (new `tests/test_runs.py`: list_runs enrichment +
chart frame ordering/per-file/skip); AppTest: table columns
`run/kind/model/files/failed/flags/reviewed/tokens`, one vega chart element,
"Runs shown" slider + "Chart metric" control, picker labels
`09-23 · #5 · gpt-5.6-luna`, no exceptions.
**Watch-outs:** row selection cannot be driven from AppTest (manual check);
the chart uses the Streamlit Altair theme with an explicit gold color scale;
historical runs without `_meta.usage` chart as 0 and are skipped.

### D42 — Gateway call efficiency: keep-alive, timing, parallel files  `ACTIVE`
**Date:** 2026-09-24
**Problem (owner):** the gateway round-trips felt inefficient. Measured with
the new bench (luna, corpus capture): 1-page call ~12.8s median (3.2k prompt),
8-page call ~18.9s median (16.9k prompt) — a large FIXED component per call,
not payload-bound.
**Decision (measured first, then adopted only what won):**
- **Keep-alive connections (`llm_client`):** replaced per-call
  `urllib.request.urlopen` with a `threading.local()` `http.client`
  HTTP(S) connection (reconnect + retry on a dropped/stale connection).
  Verified: 3 sequential calls → 1 TCP connection (was 3); the parallel run
  reuses one connection per worker.
- **Timing instrumentation:** every successful call records `elapsed_s` in the
  usage dict (`_meta.usage.elapsed_s` per file) and logs one INFO line
  (`llm <model>: 6.8s, 2476+615 tokens (458 tok/s) [responses/flat]`).
- **`bench.py` + `make bench`:** one paid vision call per run (no
  verification) — compares models/formats/caps on one file; prints wall/http
  time, token split and tok/s per run + median.
- **Parallel files (`extract.py --workers N`, default 1):** the per-file body
  moved to `_process_file` (parallel-safe: writes only its own JSON, returns
  flags/counters merged in submission order); `--workers 3` on 3 files
  measured **61s → 22s** at the SAME call count/cost. UI: Upload & Run has a
  "Parallel files" control (1–8, default 1); retry stages honor it too.
- **JPEG vision render REJECTED (measured, reverted):** q85 JPEG was LARGER
  than lossless PNG for these text-on-white renders (classic_3 366KB vs
  203KB; sample_a_1 3.2MB vs 2.7MB) — flat areas + crisp glyph edges favour PNG.
  The Swift vision render stays PNG; the JPEG encoder was removed.
- **Model latency bench (sample_a_1):** luna ~19s; qwen3.8-flash 116s (7.9k
  completion tokens);   deepseek-flash 108s (14.5k completion!); deepseek-v4.1-flash failed schema
  (empty/non-JSON responses). Luna stays
  primary — the cheap models are 5-6x slower and/or unreliable here.
**Why:** the dominant cost is provider/model latency per call plus one TLS
handshake per call; payload was NOT the bottleneck (JPEG lost), so the wins
are connection reuse, per-call visibility, and running independent files
concurrently.
**Evidence:** bench medians above; keep-alive unit test (3 calls/1 connection,
reconnect-on-close); parallel batch demo 61s → 22s; 185 tests green (new
tests/test_llm_client.py, tests/test_batch_workers.py).
**Watch-outs:** `--workers > 1` multiplies concurrent quota usage — 429s are
absorbed by the client backoff, but keep it modest (≤4) during peak; logs
interleave (stem-prefixed lines); the gated recheck is still per file, so
flagged files pay a second round-trip.

### D43 — Schema robustness: non-object replies + bad-region degradation  `ACTIVE`
**Date:** 2026-09-25
**Problem (measured on run `out/runs/2026-09-25/1`, qwen3.8-flash, 12 files):**
35.5 min wall, 5 files failed, 466k tokens:
- 2 files (classic_1, classic_2) failed after 3 attempts with
  `source_region.bbox not 4 coords in 0-1` — the VALUES were fine, only the
  location boxes were malformed;
- 3 files (classic_4, sample_b_3, sample_c_1) crashed with
  `'list' object has no attribute 'get'`: the model returned a top-level JSON
  ARRAY; `validate_schema` called `.get()` unguarded and the retry loop only
  caught `json.JSONDecodeError`, so the TypeError skipped the retry entirely.
**Decision:**
- **Non-object replies are a normal retryable problem** everywhere:
  `validate_schema`, `_multi_problems` and `recheck_fields` return/record
  `response is not a JSON object (got <type>)` instead of crashing.
- **Last-attempt bad-region degradation (D43):** on the FINAL schema attempt,
  when the ONLY remaining problems are malformed `source_region`s, the boxes
  are dropped (`_drop_bad_regions`), the values and confidence are KEPT, the
  drops are recorded in `_meta.dropped_regions`, and `run_batch` emits one
  `invalid_source_region` flag row per field (dual anchors: vision source;
  detail says the location was dropped — review the value without a box).
  Any hard problem (missing/wrong-typed field, bad confidence, non-object)
  still fails the file. `eval_verification.read_run_flags` ignores the reason
  (presentation-only, like `document_quality`), so flag precision is not
  polluted by attention rows.
- **Failed files re-extracted, not re-run:** the 5 failures were retried in
  place with `--files <stems> --model gpt-5.6-luna --workers 3`: 28s wall,
  1 attempt each, 12/12 files extracted, and the 5 files' 15/15 core truth
  cells (invoice_number/date/total) correct. Flags rebuilt with
  `ui.lib.recompute_flags` (18 rows) + summary regenerated.
- **`model_notes` (config/models.yaml):** short MEASURED picker hints
  (luna fastest 5 files/28s; qwen3.8-flash 35 min/12 files; deepseek-flash
  108s/file; deepseek-v4.1-flash fails schema; qwen3.8-max accurate but
  slow/expensive). Validated in `config.py`, appended by `ui/lib.model_label`
  via the catalog snapshot — documentation only, never read by routing.
**Why:** a wrong location box is a presentation artifact — failing an
otherwise-good extraction for it wastes a paid call; a crash that silently
skips the retry is a bug (it cost 3 files here). Values always keep running
the full verification stack, so degradation never weakens the guard.
**Evidence:** `tests/test_extract_schema.py` (9 tests: non-object guards,
degradation accept/reject paths, untouched valid regions, flag rows);
retry log 18:06:20→18:06:48 (5 ok / 0 failed, 1 attempt each); truth check
15/15; 198 tests green.
**Watch-outs:** degradation is final-attempt-only and only when NO hard
problem remains; the dropped-region flag means the Review tab shows the value
without a crop (the text-channel locate still runs — only the model's claim
is gone); `model_notes` must stay one line each (they render in a selectbox).

### D44 — Prompt package: text/compose split + provenance  `ACTIVE`
**Date:** 2026-09-25
**Problem (owner):** the prompt is the accuracy-critical artifact but its code
was hard to read: `prompts/extract_v4.py` was the "current" module while
re-exporting 7 unchanged blocks from `extract_v3.py`, whose own
`SYSTEM_INSTRUCTIONS` was the obsolete pre-D29 text (dead, but it LOOKED
authoritative). Nothing selected between the modules at runtime — the version
number carried no functional meaning — and no extraction recorded which
prompt produced it. Runtime assembly (`build_schema_block`,
`render_document_rules`, `build_multi_user`, the inline single/recheck user
strings) lived interleaved in `extract.py`.
**Decision:**
- **`prompts/text.py`** owns EVERY literal string (system instructions,
  user templates, notes) + `PROMPT_VERSION = "v4"` (the text is byte-identical
  to the retired v4). Dead `extract_v3.SYSTEM_INSTRUCTIONS` is not carried
  over. `prompts/compose.py` owns EVERY assembly function
  (`render_schema_block`, `render_multi_schema`, `render_profile_block`,
  `format_pages`, `build_single_prompt`, `build_multi_prompt`,
  `build_recheck_prompt`, `build_catalog_prompt`, `prompt_fingerprint`).
  `extract.py` keeps orchestration/validation/verification only.
- **Retired `extract_v3.py` / `extract_v4.py`** (git history only). The
  version discipline changes from "new module per revision" to **in-place
  text edits + PROMPT_VERSION bump + paid `make eval` gate**; git diff is the
  revision history.
- **Provenance:** every extraction records
  `_meta.prompt = {version, sha256(system + "\n\n" + user)}`; the narrow
  Check-4 recheck records its own fingerprint under
  `_meta.verification.recheck.prompt` (full-fallback rechecks inherit the
  primary's `_meta.prompt`).
- **Golden fixtures** `tests/fixtures/prompts/` pin the exact assembled
  (system, user) bytes for single / pinned / multi / generic / catalog /
  recheck — any text or assembly change shows up as a fixture diff, never
  silently. A synthetic fieldless profile JSON backs the generic variant.
- **Dependency cleanup:** `config.profile_def` / `config.profile_fields`
  are the canonical accessors (config.py); `extract._profile` /
  `extract.profile_fields` stay as thin wrappers.
**Why:** one read path for the prompt; the version constant + sha make
accuracy attributable to an exact prompt revision; golden fixtures make
prompt edits deliberate. YAMLs, verification wiring
(type/threshold/labels/arithmetic), transport and the brace-doubling /
json-mode quirks are untouched.
**Evidence:** refactor is byte-identical — all 6 golden fixture pairs match;
208 tests green (new tests/test_prompts.py: 10 tests); `make eval-verify`
unchanged (it scores stored runs; the prompt package is not in that path).
No paid eval was run for the refactor itself (zero text change); the next
text edit (v5) requires it.
**Watch-outs:** editing `rules/documents.yaml` description/keywords changes
the assembled prompt too — if the change is material to accuracy, treat it as
a prompt revision (bump PROMPT_VERSION, run `make eval`); the fixtures only
pin the SHIPPED profile data, so a YAML edit will fail the golden tests
until they are updated deliberately. Pre-D44 decision entries reference
`extract_v3`/`extract_v4` paths — historical, not current.

### D45 — Domain-neutral system prompt (v5) + `selected_document.name`  `ACTIVE`
**Date:** 2026-09-25
**Problem (owner):** Verdandi is a document intelligence platform, not a
customs tool — the freight corpus was chosen because it is hard, not because
it defines the product. The system role was freight-specific ("freight
logistics documents (India)", supporting-page examples, HSN/SAC columns, the
"AMT-03"/"AMT-03" example, the BOE dedupe example) and the output key
`selected_document.broker` named a freight intermediary. Domain specifics
belong in the profile (template); the system prompt must be a domain-neutral,
anti-assumption reading discipline.
**Decision:**
- **v5 `SYSTEM_INSTRUCTIONS`** (PROMPT_VERSION "v5"): generic role and
  multi-document framing; identification only by what is printed; rule 3
  strengthened ("never reconstruct, compute, or derive"); rule 6 "output only
  the fields the schema declares" + generic number/date formatting; rule 7
  generic tabular breakdown. Kept verbatim: `flag_no_document_match`,
  `selected_document.*`, confidence 0-1, `source_region` bbox 0-1,
  `document_pages`, "page N (render image)", page order.
- **`MULTIDOC_NOTE`** dedupe example genericized; **`RECHECK_NARROW_SYSTEM`**
  "a freight logistics document" → "a document". `GENERIC_*`/`RECHECK_NOTE`
  already neutral; `CATALOG_*` taxonomy deferred (see follow-ups).
- **Key rename:** the model writes `selected_document.name` (schema blocks in
  the user templates + multi wrapper). `compare.selected_name()` reads `name`
  with legacy `broker` fallback, so pre-D45 runs keep working in the UI, eval
  and `recompute_flags`; readers updated (verify.py, extract.py, eval.py,
  ui/app.py).
- **Golden fixtures** updated deliberately (5 of 6 pairs; catalog unchanged).
**Evidence (paid eval gate, luna, `--workers 3`):**
- The first v5 wording ("printed name/heading") made the model report the
  generic title ("TAX INVOICE") — run `2026-09-25/3` scored selection **0/11**.
  Rule 1 was reworded to generic issuer semantics ("the issuing
  organization's name as printed (letterhead/issuer), never a generic document
  title"; fallback to its own heading). That wording is LOAD-BEARING for the
  eval's truth-vendor selection scoring.
- Accepted on run `2026-09-25/5`: selection **11/11**, header **97.7%**
  (= v4 baseline `2026-09-03/5`), sources 64/64; flags grounding 10 /
  arithmetic 2 / format 1; run-only eval-verify TP 3 / FP 2 / FN 0 (baseline
  TP 2 / FP 2 / FN 0). Variance sample run `2026-09-25/4`: selection 11/11,
  header 95.5% (sample_a_1 + sample_a_3 invoice-number digit transposition — the
  documented luna class, both caught by content_mismatch/grounding).
- 211 tests green (new `compare.selected_name` compat tests).
**Why:** the system role now says HOW to read carefully; WHAT the document is
and which fields matter come entirely from the profile. The freight corpus
proves no loss (it cannot prove gains on other domains).
**Watch-outs / findings:**
- Split-tax transcription ("CGST 270.00; SGST 270.00") is read by
  `check_line_items` as tax-absent → "tax-free row" mismatches. PRE-EXISTING
  (classic_1/2 flag on v4 JSONs too; D31-era checker gap), amplified by
  transcription variance — fix in the line-item phase (parse components or a
  one-value-per-column prompt clause), not by reverting v5.
- Catalog taxonomy (`invoice | boe | hawb | ...`) is still freight; needs a
  generic/derived type list.
- No non-freight test documents yet — the portability claim is untested.
- The eval's selection scoring compares `selected_document.name` against the
  truth `vendor` column; generic issuer-name wording is what keeps that join
  working for freight profiles.

### D46 — Domain-neutral catalog sweep + `--file` scoping  `ACTIVE`
**Date:** 2026-09-25
**Problem (owner):** the catalog sweep was the last freight-specific system
prompt: "freight logistics PDF", freight document examples (broker tax
invoice, HAWB, BOE, packing list, supplier bill, terminal bill), and a closed
freight taxonomy in the output schema (`invoice | boe | hawb | packing_list |
supplier_bill | terminal_bill | other`). Separately, a smoke test exposed a
cost bug: `--file <stem>` did not scope the sweep (`files_filter` was only
built from `--files`), so the UI's per-file "What's in this file?" paid for a
catalog call on EVERY file in the run.
**Decision (v6, PROMPT_VERSION bump):**
- **`CATALOG_SYSTEM_INSTRUCTIONS`** generic: no freight wording, generic
  document examples (invoices/statements/forms/certificates/letters/reports),
  generic label examples; label semantics = the issuing organization's name
  and/or its title as printed, preferring the printed name.
- **Free-form `type`** (short lowercase snake_case, "other" when unclear) —
  the closed freight list is REMOVED from both the system rule and the output
  schema. `type` is display-only (UI cards); `run_catalog` never validated it
  against a list, so no code contract changed.
- **Catalog provenance:** each catalog JSON now records
  `_meta.prompt = {version, sha256}` (D44 fingerprint), like extractions.
- **`--file` scoping fix:** `main()` builds `files_filter` from `--file` when
  `--files` is absent, so a per-file catalog sweep is exactly one paid call
  (run_batch is unaffected — `only` and the filter select the same stem).
**Evidence:** golden `catalog.*` fixture updated deliberately (the only
fixture bytes that changed — extraction prompts untouched by v6); paid smoke
on `classic_2` (luna): catalog returned 3 documents with issuer-name labels
("TAX INVOICE — VENDOR-B", …) and sane
free-form types; post-fix smoke `--file classic_1` processed exactly 1 file
("catalog batch done: 1 ok"). 213 tests (new tests/test_prompts.py
TestCatalogPrompt: domain-neutral assertion + free-form type/provenance).
No corpus eval: the sweep is advisory (pinning is the source of truth) and is
not in the extraction eval path.
**Watch-outs:** `type` is now model-chosen free-form — if the UI ever needs
grouping/icons, derive them from the label or reintroduce a generic list;
catalog stays advisory and never gates extraction.

### D47 — Line items: table procedure, composite amounts, truth metric  `ACTIVE`
**Date:** 2026-09-27
**Problem (owner):** line items were the weakest prompt area — one sentence
("extract the row-wise breakdown"), no labels/anchors for rows, no per-row
geometry, and no ground truth. The failure classes: split tax cells
("CGST X; SGST Y") were read by `check_line_items` as tax-absent (false
"tax-free row" flags, classic_* class), and a misread row could pass the row
check while poisoning the HEADER arithmetic flag (sample_c_1 run /5:
total=taxable → "tax-free" pass → `line_items_sum_to` FP on the correct
header).
**Decision (v7, PROMPT_VERSION bump):**
- **System rule 7 → table procedure** (generic, domain-neutral): find the
  table's own header row and align printed columns to schema columns first;
  one line item = one printed BODY row, all rows, in order; a row's own amount
  goes in `total` whenever printed (a single-row table's amount printed in a
  summary line IS that row's total); never output header/subtotal/total/
  tax-summary rows as EXTRA line items; copy, never compute; multi-page
  continuation continues the same list.
- **Composite amount parsing** (`compare.norm_amount_components`): decimal
  amount tokens (2dp, not embedded in dates) sum across a cell; rate
  expressions (`18%`, `9.00%`) are stripped first. `verify.check_line_items`
  and `validate.check_arithmetic` use it — split tax cells now reconcile.
- **Shipped `line_items_note` rewritten as a table contract** (which table,
  what one row is, total mandatory, split cells as printed, exclusions) for
  `broker_invoice` / `commercial_invoice` / `broker_lineitems_test`; UI
  "Line items instruction" help/placeholder updated.
- **Truth metric:** `tests/line_item_truth.json` (hand-read from the raw text
  channel, never model output) = expected body-row count + row TOTAL amounts
  per corpus file; `eval.py` prints a line-items truth section (row counts +
  multiset totals) alongside the header matrix.
**Evidence (paid runs, luna, `--workers 3`):**
- Iteration caught by the metric: run `2026-09-27/1` (first rule-7 wording)
  made the model drop printed totals on the single-row sample_c layout
  (`total` null → "NO AMOUNT" row flags, truth totals 13/15); rule 7b/7c
  refined (total mandatory when printed; summary-line amount = the row's
  total).
- Accepted on run `2026-09-27/2`: selection **11/11**, header **100%**
  (invoice_number 11/11 — first clean run; baseline 97.7%), sources 66/66;
  line-item truth **rows 12/12, totals 15/15** (baseline run /5: 14/15);
  remaining `line_item_issue` flags are GENUINE row-level issues correctly
  attributed to rows (classic_4 misread tax; sample_c_1/2 dropped tax) — the
  header no longer absorbs row errors. eval-verify (runs+fixtures):
  precision 0.526 / recall 1.000 / accuracy 0.903 (baseline 0.500 / 1.000 /
  0.882). 223 tests.
**Why:** rows are a repeating structure, not labelled fields — the document's
own header row is the label set, and completeness + no-computation rules are
what a vision model can actually follow. The truth metric makes every future
line-item change measurable; the composite parser removes the known
false-positive class.
**Watch-outs / next:**
- Row errors now surface as row flags, but rows still have no geometry — the
  "tax dropped" class (sample_c) and misreads are caught by math/truth, not by
  grounding. Phase B adds per-row `source_region` + row grounding + a UI
  line-items view; Phase C makes columns template-declared (replacing the
  fixed `hsn_sac/taxable/tax`).
- `check_line_items`'s "no tax — tax-free row" wording conflates a genuinely
  tax-free row with a dropped tax cell (sample_c class) — improve in Phase B.
- Truth file is hand-verified data: update it deliberately, never from model
  output.

### D48 — Line items Phase B+C: row regions, row grounding, declared columns  `ACTIVE`
**Date:** 2026-09-27
**Problem:** rows were the only extraction surface without a proof
obligation (header fields carry `source_region` + grounding; rows carried
only a page pointer), and the row columns were hardwired
(description/hsn_sac/taxable/tax/total) — freight-ish and not template data.
**Decision (v8, PROMPT_VERSION bump):**
- **Per-row `source_region`** (Phase B): rule 7f asks for the page + bbox
  covering ALL printed lines of the row (amounts are often on separate
  lines); the row schema (single + multi wrapper) carries it; validation
  checks its shape; the D43 bad-region degradation now also drops bad ROW
  regions (value kept, `invalid_source_region` flag per row).
- **Row grounding** (`verify.check_line_item_grounding`): every claimed amount
  cell must be found in the text channel near the row region. Component-wise
  (`compare.amount_components`): "CGST 9.00% 270.00; SGST 9.00% 270.00" is
  proven by its two `270.00` components. Row search band is 2x the field
  band (0.24) — rows are taller than fields and models under-cover them;
  attribution precision stays the math checker's job. `containment._tokens`
  now splits on `|` (fused text-channel fragments like "2,000.00|2.00").
- **Template-declared `line_items_columns`** (Phase C): ordered
  `{name, description, type, role}` columns; roles `description` and `total`
  are required when columns are declared, `taxable`/`tax` optional and
  enabling row math; profiles without columns keep the legacy defaults.
  `config.line_item_columns`/`line_item_column` are the accessors;
  `compose.render_line_item_row` + per-column notes render them into the
  prompt; `validate_schema`/`_multi_problems` enforce declared keys; the
  checker, the dropped-row flags and `eval.audit_line_item_sources` resolve
  roles instead of hardcoded names. The three shipped line-item profiles
  migrated with per-column descriptions.
- **UI:** Explorer gains a Line-items table + per-row evidence crops (the
  rows were previously invisible); the Review evidence pane resolves
  `line_item_<n>` flags (`ui.lib.find_line_item` + amount-kind locate); the
  Templates tab gains a columns editor (name/description/type/role).
**Evidence (paid runs, luna, `--workers 3`):**
- Iteration caught by offline re-grounding: run `2026-09-27/3` (first B+C
  wording) produced 12 row-grounding flags — composite cells grounded as
  whole strings + a too-tight band on split-line rows (sample_c) + one fused
  token. After the component-wise/2x-band/pipe-split fixes, offline
  re-grounding of the SAME run's rows → **0 flags**.
- Accepted on run `2026-09-27/4`: selection **11/11**, header **97.7%**
  (= baseline), sources 65/65; line-item truth **rows 12/12, totals 15/15**;
  **0 row flags**, all 15 rows carry a region, no undeclared row keys;
  flags 20 (baseline 24: grounding 10/11, arithmetic 1/2, format 2/3);
  eval-verify precision **0.550** / recall 1.000 / accuracy 0.903 (baseline
  0.500 / 1.000 / 0.882). 231 tests.
**Watch-outs / next:**
- The 0.24 row band is deliberately generous: row grounding proves "printed
  near this row", not "printed in this exact cell" — cross-row attribution
  remains the math checker's (and the truth metric's) job.
- `containment._tokens` pipe-splitting also affects header grounding (fewer
  known OCR-garble FPs); eval-verify numbers above reflect it.
- UI has no automated tests — the new Explorer/Review/Templates surfaces need
  a manual smoke (import + syntax are covered by `make check`).
- Next candidates: batch variance measurement (repeat runs), non-freight
  fixture set, catalog type grouping.

### D49 — Generic verification vocabulary: roles are data, row math is data  `ACTIVE`
**Date:** 2026-09-27
**Problem (owner):** after D48, product code still carried invoice/GST
semantics: roles `total`/`taxable`/`tax`, the row math `taxable + tax ==
total`, `line_items_sum_to` summing the literal `"total"` key,
`FIELD_KINDS`/`value_type_for` legacy field maps, `REQUIRED_THRESHOLDS`
forcing invoice field names into thresholds.yaml, and `compare.reconcile`'s
hardcoded taxable/tax/total classes — a bottleneck for a general document
platform.
**Decision (v9, PROMPT_VERSION bump):**
- **Minimal generic roles:** `description` + `amount` only. Legacy alias:
  `total` reads as `amount`; legacy `taxable`/`tax` roles are dropped to
  plain columns (warned — their math must be declared). Default columns for
  undeclared profiles: `description` + `amount`.
- **Row math is template data:** `extraction.row_arithmetic` (validated;
  `sum_equals` only) evaluated per row by `validate.check_row_arithmetic` —
  e.g. `fields: [taxable, tax] target: total, missing: zero` (absent
  components count 0 → tax-free rows pass). `verify.check_line_items` =
  amount completeness + declared row rules; zero domain names in code.
- **Role-based plumbing:** `line_items_sum_to` sums the amount-role column
  (`check_arithmetic(..., amount_col=)`); row grounding checks every
  amount-typed column; dropped-row flags/eval truth/UI resolve the amount
  role. Shipped profiles keep their column names (template data) and declare
  the GST row rule explicitly.
- **Legacy fallback sweep:** `compare.FIELD_KINDS` and
  `validate.value_type_for` keep only the generic minimal schema names;
  `config.REQUIRED_THRESHOLDS = []` and `config/thresholds.yaml`'s map
  emptied (profile-inline thresholds are canonical; the YAML map stays as an
  optional fallback); `compare.reconcile` and eval's reconciliation section
  REMOVED (superseded by the row truth metric + row rules).
- **Prompt v9:** rule 7 says "the schema's amount column", "summary rows",
  "several sub-columns"; identification/catalog examples say "Invoice". The
  shipped tax columns' descriptions now say "the tax AMOUNT — the money
  value only, never the rate/percentage" (template guidance for an observed
  rate-in-cell class).
- **UI:** role options `description`/`amount` + a Row rules editor
  (addends / equals / tolerance / absent=0).
**Evidence (offline equivalence + paid runs, luna, `--workers 3`):**
- D49 code re-checked on the D48 run `2026-09-27/4`: 0 line-item/grounding
  flags, truth rows 12/12 + amounts 15/15 — behavior preserved.
- Run `2026-09-27/5` (v9 wording): selection 11/11, header **100%**, sources
  65/65, truth 15/15; 3 row flags from the model embedding the rate in the
  tax cell ("CGST 9.00 270.00") → tax-column guidance added.
- Run `2026-09-27/6` (accepted): selection 11/11, sources 65/65, truth
  12/12 + 15/15, **0 row flags**; header 95.5% — the two misses are the
  documented sample_a invoice-number digit-transposition class, both flagged by
  `content_mismatch` (run `/5` scored header 100%; the envelope is
  model variance, unaffected by D49). eval-verify: precision **0.591** /
  recall 1.000 / accuracy 0.903 (baseline 0.500 / 1.000 / 0.882).
- Integration bug caught by the metric: `eval_verification`'s offline stack
  called `check_arithmetic` without the amount column → a false negative on
  classic_2 (the 0.20 quirk); fixed. 235 tests.
**Watch-outs:** a profile declaring legacy `taxable`/`tax` roles loses the
implicit row math — declare `row_arithmetic`; docstrings still use historical
examples (decision log history is intentional); the amount-column parameter
must be threaded by every new `check_arithmetic` caller (verify + eval-verify
today).

### D50 — Close-out: rate-aware row math + UI regression coverage  `ACTIVE`
**Date:** 2026-09-27
**Problem (D48/D49 retrospective):** two open items.
(1) `amount_components` cannot tell a bare-decimal rate from an amount when
the model drops the `%` ("CGST 9.00 270.00; SGST 9.00 270.00" summed to 558
instead of 540) — run `2026-09-27/5` flagged two correct rows; the template
wording mitigated but did not fix it.
(2) The new UI surfaces (Explorer line-items table + row crops, Review
`line_item_*` evidence, Templates columns/row-rules editors) had no
automated tests — imports/syntax only.
**Decision:**
- **Rate-aware row math (`validate._addend_amount`):** before summing a row
  rule's addends, a component `r` (0 < r <= 100) is dropped as a RATE only
  when a self-validating numeric identity holds — some sibling component `v`
  in the same cell equals `base * r/100` within the rule tolerance, with
  `base` = the row's other rule values (never the cell itself). Everything
  else is kept; if all components would drop, fall back to the naive sum.
  `compare.amount_components` stays context-free (grounding/completeness
  unchanged). Domain-neutral: the identity is pure arithmetic, no
  tax/percentage vocabulary.
- **UI regression coverage:** `tests/test_ui_line_items.py`
  (`ui.lib.find_line_item` role/multi/legacy resolution; `ui.app` draft
  round-trip for columns, row rules incl. `missing: zero`, anchors) and
  `tests/test_ui_app_smoke.py` (Streamlit `AppTest` boots `ui/app.py`,
  asserts the tab labels; skipped when streamlit is absent so pipeline-only
  installs still pass `make check`). AGENTS.md gains a manual UI smoke
  checklist (Upload & Run → Explorer rows/crops → Review a `line_item_*`
  flag → Templates columns/row rules → Workflows).
**Evidence:** `tests/test_verification.py` `TestRateAwareRowMath` (7 tests:
bare-decimal and `%` rates dropped; genuine small amounts kept; identity
failure still flags; single-component unchanged). Offline replay of run
`2026-09-27/5` with the fix: 2 line_item_issue flags → **0**; run
`2026-09-27/6` stays 0. No prompt change → no paid run / fixture gate.
UI evidence: `tests/test_ui_line_items.py` (6 tests — `find_line_item`
role/multi/legacy resolution; Templates draft round-trip preserving columns,
row rules incl. `missing: zero`, and anchors) and
`tests/test_ui_app_smoke.py` (AppTest boot: no exception, the 6 tabs; with
runs on disk the data tables and evidence images render too; skipped when
streamlit is absent). Headless execution recorded here; the visual checklist
in AGENTS.md stays owner-facing. 249 tests.
**Watch-outs:** the identity heuristic can only drop components that
mathematically explain a sibling amount (tests pin the counter-example);
AppTest is network-sensitive — assertions are minimal and the manual
checklist is the fallback; accepted limitations from D48/D49 (row band
0.24, model digit-transposition variance, hand-verified truth) are unchanged.

### D51 — Templates tab: refuse saves that break workflows  `ACTIVE`
**Date:** 2026-09-28
**Problem (incident):** deleting a template in the Templates tab rewrote
`rules/documents.yaml` without checking `rules/workflows.yaml`. The working
tree lost `bill_of_entry` (and `broker_lineitems_test`) while the shipped
`shipment_approval` workflow still referenced `bill_of_entry`, so the next
`config.load_all()` raised `ConfigError: workflows[1].documents
'bill_of_entry' is not a known document profile` and the Streamlit app
crashed at startup. Root cause: the save path validated only the documents
file; the cross-file contract was enforced only at load time (after the
damage).
**Decision:**
- `ui.lib.workflow_references()` reads workflows.yaml directly and maps every
  referenced profile id (documents / expected / cross_checks sides) to the
  workflow ids using it.
- `ui.lib.validate_documents()` now also refuses a candidate documents.yaml
  that would leave a workflow dangling — the message names the missing
  template and the workflow(s) ("update rules/workflows.yaml first"). The
  Templates Save / Save & Test buttons were already disabled on validation
  failure; the Delete button now catches the refusal and shows an error
  instead of an unhandled exception.
**Evidence:** incident reproduced from the working tree (`load_all` raised);
restored `rules/documents.yaml` from HEAD (5 profiles, `make check` green);
end-to-end guard: attempting to save the documents file without
`bill_of_entry` is refused with "workflows still reference missing
template(s): 'bill_of_entry' (used by shipment_approval)" and the file is
untouched; 3 new tests in tests/test_profiles.py (real reference map,
dangling rejection + satisfied candidate, shipped files still validate);
252 tests green; AppTest smoke still boots.
**Watch-outs:** deleting a genuinely workflow-referenced template now
requires updating/removing the workflow first (intended — the error says so);
unreferenced templates (e.g. the test-only `broker_lineitems_test`) can still
be deleted freely; the guard reads the real workflows.yaml, so the expert
Workflows editor remains the way to change references.
> **Superseded 2026-09-28 by D52:** the save REFUSAL was replaced with a
> warning (deletion proceeds; the workflow keeps the reference and skips the
> missing member at run time). The `workflow_references()` helper remains and
> powers the warning message.

### D52 — Workflows tolerate missing templates: warn, never break  `ACTIVE`
**Date:** 2026-09-28
**Problem (owner):** D51 refused the save when a workflow still referenced a
template being deleted — correct but wrong UX: the owner deletes/creates
templates freely and wants a WARNING that a template no longer exists, not a
blocked edit or a broken app.
**Decision (supersedes D51's refusal):**
- **Lenient workflow loading:** `config.load_workflows` collects a `warnings`
  list instead of raising for dangling REFERENCES — a `documents` member that
  no longer exists or became fieldless, an `expected` member that no longer
  exists, and cross-check sides whose template/field is gone or whose kinds
  became incompatible. The raw references stay in the workflow (the UI shows
  them); only STRUCTURAL errors (wrong types, missing keys, duplicates, same
  template on both sides) still raise. The returned dict is now
  `{"workflows": [...], "warnings": [...]}`.
- **Run-time behavior:** `extract._resolve_workflow` skips missing/fieldless
  members (logging them), keeps `expected` raw so `missing_expected_document`
  rows flag deleted templates, and errors with a clear message only when NO
  member is usable.
- **UI:** `validate_documents` returns `ok=True` with a `"warning: ..."`
  message for dangling workflow references (Templates tab shows it as a
  warning; Save/Delete proceed); the Workflows tab lists the config warnings;
  CLI runs log them once per batch.
- **Owner cleanup:** deleted the `bill_of_entry` and `broker_lineitems_test`
  templates and the now-meaningless `shipment_approval` workflow (it existed
  solely for the BOE chain). The cross-document executor stays covered by
  tests over the remaining profiles (hand-built + temp-workflow rules).
**Evidence:** simulated the incident exactly — real docs minus
`commercial_invoice` + the shipped workflow → `load_all()` now returns 2
warnings instead of raising; 253 tests (rewritten cross-check contract tests:
dangling → warning, structural → raise; run-time resolution tests); AppTest
smoke boots; shipped config loads with zero warnings.
**Watch-outs:** a dangling reference stays in workflows.yaml until edited
(intended — the Workflows editor shows and cleans it); a workflow whose
members are ALL missing still errors when run (nothing to extract); the
`expected`-missing path intentionally produces review rows, so the operator
sees the deleted template at run time too.

### D53 — Workflow editor state + tests decoupled from owner data  `ACTIVE`
**Date:** 2026-09-28
**Problem (owner report):** after adding a data point to a template, it could
not be selected in the cross-document rule builder. Root cause (reproduced
headlessly on a repo copy): `tab_workflows` never persisted
`st.session_state["wf_editing"]` on first load, so `_wf_cross_checks_editor`
worked on a throwaway dict each rerun; `_ensure` assigned a fresh random `_k`
every run (the rule was rebuilt from the saved YAML, which has no `_k`), and
widget keys embed `_k` — keys rotated on EVERY run
(`xc_97c56df9_*` → `xc_5c3c6f71_*` → …) and every rule edit reverted to the
saved values (selecting Field `pan` snapped back; selecting a newly added
template as Document B snapped back). Document changes call `st.rerun()`,
discarding the throwaway draft immediately. Secondary: fixed widget keys
(`wf_docs_pick`) leaked state across workflows (switching showed the previous
workflow's documents).
**Decision:**
- Persist the workflow draft on first load (`wf_editing`).
- Stable rule widget keys: `_k` derives from the rule's persisted `id`
  (uuid only for brand-new rules) — stable across reruns AND saves (`_k` is
  stripped before YAML, already).
- Scope every editor widget key by the workflow id
  (`wf_docs_pick_<id>`, `wf_expected_pick_<id>`, `xc_<id>_<k>_*`).
- On a Document change, re-default the paired field and push its widget value
  BEFORE the field widget is instantiated (Streamlit forbids modifying a
  widget key after creation — the first fix attempt hit this), then rerun.
- `＋ New` defaults to the first existing template (no hardcoded
  `broker_invoice`).
- **Tests decoupled from owner data:** templates/workflows are editable data,
  so contract tests must not assume shipped ids. The multi prompt golden
  fixture now uses synthetic profiles
  (`tests/fixtures/prompts/synthetic_multi_profiles.json`,
  `multi_synthetic_pair.*`); `TestCrossChecks` builds synthetic temp
  documents; real-file assertions are structural; `workflow_references` is
  tested against a temp workflows file. New
  `tests/test_ui_workflow_editor.py` (Streamlit AppTest) proves field edits
  persist, document switches stick and re-default the paired field, and
  workflow switching shows the right documents (skips gracefully without
  streamlit or with <2 workflows).
**Evidence:** reproduction before/after on a repo copy: keys
`97c56df9` → `5c3c6f71` → `854d10a7` and `pan` reverting BEFORE; stable keys,
`pan` persisting, `broker_invoice` selectable, `HAWB` selectable and sticking
AFTER. `make check`: 255 tests green (1 conditional skip). No prompt/config
semantics changed → no paid run.
**Watch-outs:** the AppTest editor tests discover profiles/workflows from the
loaded config and SKIP when the shape doesn't fit (owner data is editable) —
a skip is not coverage; the manual UI smoke checklist still applies. The
owner's templates/workflows (`broker_invoice`, `bill_of_entry`,
`forwarder_chain`) are committed as owner config in the same change.

### D54 — Excel export profiles: typed, colour-coded workbooks  `ACTIVE`
**Date:** 2026-09-28
**Problem (owner):** after review, a run's data lives in JSON — not readable
or mutable for humans, and cross-document failures are invisible outside the
UI. The owner needs per-broker Excel layouts ("column A = invoice number,
column B = invoice date, …") with failures visible in-cell.
**Decision:**
- **`rules/exports.yaml` — export profiles (data, D10/D54):** ordered
  `columns` with sources `{meta}`, `{template, field}`, `{line_item}` (any
  declared line-item column, `index`, or the `amount` role alias) and
  `{cross_check: rule_id}`; `grain: file | line_item` (+ required
  `line_item_template`); `options` (`date_format`, `require_reviewed`,
  `include_issues`, `include_line_items_sheet`). Structural errors raise;
  dangling refs WARN (D52) and render blank. `config.load_all` gains
  `"exports"`. A starter `broker_chain_export` profile ships for the owner's
  `forwarder_chain` workflow.
- **Grain (owner decision):** `line_item` = one row per line item of the
  first found `line_item_template` entry, header fields repeated; a file
  without items gets one blank-item row. Extra same-template entries are
  noted in `Issues` (first-found rule, D54).
- **Engine `export.py`** (local, deterministic, no LLM; openpyxl lazy —
  added to `requirements-ui.txt`): reads `json/ + flags.csv + review.json +
  manifest.json`; resolves values, types (amounts → numbers, dates → real
  Excel dates via the existing normalizers), per-cell status and comments;
  writes `Data` (bold header, freeze, autofilter, widths), `Issues` (every
  flag row + every cross-check outcome incl. NOT CHECKED + multiplicity
  notes + review status/note/reviewed_at), `Meta` (provenance, counts,
  legend) and an optional long-format `Line items` sheet. CLI:
  `python export.py <run_dir> --export <id> [--out path] [--list]` →
  `<run>/export/<id>_<timestamp>.xlsx` (never overwrites).
- **Colour semantics (owner decision):** amber = flagged & unreviewed ·
  red = reviewed-as-issue OR cross-document mismatch (mismatch outranks
  issue on a cell) · grey = cross-check NOT evaluated (missing value or
  document) · clean cells uncoloured; every coloured cell carries a comment
  with the reason + detail. A `validation_status` meta column summarises the
  row and mirrors its worst status.
- **Review gate:** exports are allowed with unreviewed flags (clearly
  marked); `options.require_reviewed: true` turns it into a hard gate.
- **UI (Export tab, 7th):** run picker with reviewed/pending counts; profile
  list + guided builder (name/id, workflow, grain + line-item template,
  column `data_editor` with source kind/target/field/type, **Seed columns
  from workflow**, date format, require-reviewed, optional Line items sheet);
  guided saves validate + warn via `ui/lib.validate_exports`/`save_exports`
  (D52-lenient); a **styled preview** (pandas Styler mirroring the workbook
  fills) and **Build workbook → Download** (also saved to
  `<run>/export/`). Missing openpyxl shows an actionable install message.
**Evidence:** run `2026-09-28/2` (owner's forwarder_chain workflow) with the
starter profile: `Data` = 2 rows (one per broker_invoice line item across
sample_c_1/sample_c_2), header fields repeated, dates typed, amounts numeric,
BOE Check `PASS`/`NOT CHECKED` (the invoice's BOE number is null → grey),
Validation summaries `8 flag(s) pending review · 1 not checked` /
`5 flag(s) pending review`; `Issues` = 15 rows (13 flags + 2 cross-check
outcomes); `Meta` = 2 files · 2 rows · 13 flags · 0 mismatches · 1 not
checked. Tests: `tests/test_export.py` (8: grain/typing/statuses/cross-check
states/gate/workbook structure/flag_key equality) + `TestExports` contract
(6: structural raises, dangling warns, empty allowed). No paid calls.
**Watch-outs:** first-found grain means duplicate same-template entries are
NOT exported (Issues notes them); the CLI needs openpyxl (UI deps); column
references are D52-lenient (blank cells + warnings, never a crash); the
`cross_check` column shows NOT CHECKED for rules absent from the run audit;
raw printed values live in the cell comments, typed values in the cells.

### D55 — Missing values are flagged (completeness)  `ACTIVE`
**Date:** 2026-09-28
**Problem (owner):** a declared field with no value on a FOUND document was
silent — `verify_field` skips nulls, the confidence-bar loop skips nulls, and
arithmetic/cross rules skip when a side is missing. In the Excel export such
cells were blank and uncoloured, so missing data could pass review unnoticed.
**Decision:**
- **New review flag `missing_value`:** for every declared field of a FOUND
  document (profile fields, or the generic schema for fieldless profiles)
  whose value is null/empty — including a field the model omitted entirely —
  emit one row: field name, value null, confidence as reported, threshold
  from the normal chain, detail "declared field has no value on a found
  document — missing or unreadable (review)". Implemented in
  `verify.selection_guards` (single) and `selection_guards_entry` (multi)
  via `_declared_fields` + `_missing_value_row`.
- **No spam on no-match:** a file/entry that was NOT found does not emit
  `missing_value` rows — the single `no_document_match` row owns that state
  (and pinned no-match keeps its `below_threshold` selection row).
- **Not a value disagreement:** `eval_verification.read_run_flags` ignores
  the reason (like `document_quality`/`invalid_source_region`), so the
  precision metric is unaffected.
- **Export/UI:** no engine change needed — the flag maps to the
  (template, field) cell, so the cell turns amber (pending) and the row's
  `Validation` cell mirrors it; `Issues` lists the row; the Review tab queues
  it like any flag.
**Evidence:** unit tests (`TestMissingValueFlags`: null + absent field
flagged; no-match/no-entry not spammed; multi template attribution) and the
export test (`missing_value` flag → amber null cell + amber Validation).
Offline re-verification of the owner's run `2026-09-28/2` (in-memory, no
writes): c7d22038 → 2 rows (broker_invoice.boe_number, boe_date),
fc68bc92 → 1 row (boe_date). 279 tests.
**Watch-outs:** the flag cannot distinguish "not printed on this document"
from "failed to read it" — the reviewer decides; if a field is legitimately
absent on some documents, review it as confirmed (or remove it from the
template). Recompute/re-extract a run to pick the new flags up (flags.csv is
a run artifact).

### D56 — Export builder: Excel-feel column cards (no jargon)  `ACTIVE`
**Date:** 2026-09-29
**Problem (owner):** the export column builder was a raw `data_editor` with
technical fields (`Source: meta|template|line_item|cross_check`, free-text
`Target`/`Field`, `Type`) — you had to know profile ids, field names and the
source vocabulary, field meanings were invisible, and columns could not be
reordered. The preview/download were already good; only the builder felt
barebones.
**Decision (UI only — engine/config/prompt untouched):**
- **Friendly source catalog** (`_export_source_catalog`): File info (original,
  file id, status, flags total/pending, reviewed, validation summary), each
  document of the linked workflow in order (label "Broker Tax Invoice ·
  Invoice Number" + the field's own description as help), line-item columns
  (grain: line_item only), and cross-checks by label ("Check · BOE check").
- **One grouped dropdown per column** ("Data to show") — no kind/target/field
  keys; dangling sources stay visible with a ⚠ label and an inline warning
  (deleted template/field, unknown check, line-item source while grain=file).
- **Excel-style header strip** above the cards (A · Invoice No, B · …) as a
  live mirror of the header row, plus **column letters** on each card.
- **Card per column** (replaces the grid): badge + `◀ ▶` reorder, `⧉`
  duplicate, `✕` delete, the source dropdown, an editable header
  auto-filled from the source (`boe_number` → "Boe Number"; changing the
  source resets only an untouched header), the field description as caption,
  and a collapsed **Advanced** (value type override + raw source).
- **Add/Seed/Expert:** "＋ Add a column" expander (picker + Add), the
  existing Seed-columns-from-workflow button (now producing cards), and an
  expert-YAML toggle for parity with Templates/Workflows.
- **State discipline (D53 lessons):** the builder mutates the session draft's
  columns in place; each column carries session-only `_k` (stable widget keys
  across reorder/rename) and `_header_auto`; both are stripped before YAML but
  kept in the session after Save, so widgets never reset. The `data_editor`
  and the D54 `seed_ver` key-rotation hack are removed.
**Evidence:** AppTest interactions (all session-only): boot, seed (13→12
columns, `source: {meta: original}` first), add via the picker, header rename
persists, reorder swaps, delete removes, expert toggle renders the YAML
editor, Build workbook still produces the .xlsx — `tests/test_ui_export_tab.py`
(6 tests). 284 tests green (1 conditional skip). No paid calls.
**Watch-outs:** headers default to humanized field names (rename freely);
`_header_auto` is inferred from equality with the source's default header on
load, so a saved custom header is never clobbered by a source change; the
header strip is display-only (the styled preview below remains the source of
truth); a dangling source renders blank in the workbook (D52) with the card
warning as the early signal.

### D57 — Occurrence-aware flags, review and export  `ACTIVE`
**Date:** 2026-09-29
**Problem (edge-case review):** two documents of the SAME template in one file
produced identical flag rows (same file/template/field/reason/detail) — the
review key collided, `find_field`/`apply_review` always resolved
`documents[0]`, and the export coloured the first-found occurrence from a flag
that belonged to another one. Nothing could tell occurrences apart.
**Decision (owner-approved plan D57–D60, question 1/4 answers):**
- `FLAG_COLUMNS` gains `entry` (the documents[] index; empty for
  single-document and file-level rows). All writers populate it:
  `verify_multidoc` enumerates entries (`setdefault("entry", i)`),
  `check_duplicates` flags the LATER occurrence with the index (detail names
  `documents[i]`/`documents[j]`), `check_cross_document` threads the index
  through `_entry_values` (now `(value, field, entry)` triples) into both flag
  rows and the audit pairs (`left_entry`/`right_entry`), and
  `_drop_bad_regions`/`_dropped_region_flags` carry it for dropped regions.
- Review key format is now `file|entry|template|field|reason|detail`
  (`ui.lib.flag_key` / `export.flag_key`); `legacy_flag_key` + `review_state`
  keep old review.json entries matching their flags (the Review tab rewrites
  to the new key on the next action). `apply_reviews` parses both formats.
- `find_field`/`find_line_item` accept `entry` and pin the exact occurrence;
  `apply_review` writes into `documents[entry]` and the
  `_meta.review.entries` dedupe key includes entry. Review labels show
  `[template · entry N]` when an occurrence index exists.
- Export (owner answer 4): template cells colour from flags of the FIRST-found
  occurrence only, line-item cells from the grain occurrence
  (`export._flags_for`); rows without an `entry` are file-level
  (selection/completeness) and always kept. The Issues sheet gains an `Entry`
  column (after Stem).
**Evidence:** new offline tests — cross flags/audit pairs carry entries
(`test_cross_document`), duplicate flags carry the later entry
(`test_verification`), `verify_multidoc` rows carry entries, entry-aware
`apply_review` writes only the flagged occurrence, `flag_key` separates
occurrences, legacy review keys still apply (`test_review_apply`), export
entry filtering + Issues `Entry` header (`test_export`). Offline recompute of
run `2026-09-28/2` → 11 rows, `entry` 0 for broker_invoice and 1 for
bill_of_entry. 291 tests green (1 conditional skip). No paid calls.
**Watch-outs:** `entry` is a positional documents[] index — meaningful only
within a run's own JSON; pre-D57 flags have no entry and are treated as
file-level (never falsely filtered); cross-check flags anchor the first
failing candidate per side (entries that match NOTHING are D59's business).

### D58 — Day-first date normalization: dotted + 2-digit forms  `ACTIVE`
**Date:** 2026-09-29
**Problem (edge-case review):** `compare.norm_date` understood only
`%d/%m/%Y`, `%d-%m-%Y`, `%d-%b-%Y`, `%d %b %Y`, `%Y-%m-%d` — printed
day-first dotted dates (`04.07.2024`) and 2-digit years (`04.07.24`,
`04-Jul-24`) normalized to None, producing false `verification_format` flags
and missed cross-check/grounding date matches.
**Decision:** extend `_DATE_FMTS` with the day-first dotted and 2-digit
variants (`%d.%m.%y`, `%d.%m.%Y`, `%d/%m/%y`, `%d-%m-%y`, `%d-%b-%y`,
`%d %b %y`, `%d %B %Y`, `%d-%B-%Y`); ISO stays last. Month-first forms are
deliberately NOT added (ambiguous with day-first; the corpus is Indian
logistics). `%y`/`%Y` reject each other's widths, so no format is ambiguous.
The single normalizer feeds format validation, grounding token matching,
cross-document date equality, export typing and eval scoring — one change,
all consumers.
**Evidence:** `tests/test_cross_document.py` (9 new normalization assertions +
a dotted cross-check match). Replay of the baseline corpus run
`2026-09-03/5` + `make eval-verify`: numbers unchanged (precision 0.500 /
recall 1.000; the run has no dotted dates — no regression), 293 tests green.
No paid calls.
**Watch-outs:** month-first strings (`07/04/2024` = July 4th in the US) are
intentionally left unparsed rather than guessed; a field whose print format
is truly month-first should be normalized by the profile's type handling, not
by silent format guessing.

### D59 — Unbound-document check: pass-if-any no longer hides extras  `ACTIVE`
**Date:** 2026-09-29
**Problem (edge-case review):** cross rules use pass-if-ANY (multiplicity is
normal — a file may carry several supplier invoices), so an attached document
whose value matches NOTHING passed silently. Two BOEs in one file with only
one referenced by the broker invoice looked clean.
**Decision (owner-approved plan D59–D60, question 2 answer):**
- `verify.check_cross_document` records, per rule, the candidates that matched
  no value on the other side in `rec["unbound"]` ({left/right: [{entry, value,
  source}]}, always present when evaluable).
- When the rule PASSED but has unbound candidates, one `unbound_document` flag
  row per candidate (`template` + `entry` from D57; value/confidence/source
  from the entry's own field; detail names the rule and the other side's
  candidates with their entry indices). When the rule FAILED entirely, only
  the existing `cross_document_mismatch` rows fire — the audit still records
  every unbound candidate (no double-flagging).
- Export: an `unbound_document` cell is RED until the reviewer confirms it
  (`export._field_cell_status`) — a pertinence problem, not a to-do; a
  confirmed review clears the colour, an issue keeps it.
- Silent skip when a side is absent / has no value is unchanged (presence is
  the `expected` contract's business).
**Evidence:** 3 new cross-check tests (unbound entry flagged with rule still
passing; empty when all match; no unbound rows on total mismatch) + an export
red-until-confirmed test; the pre-existing any-candidate test updated to the
new semantics; `make eval-verify` on the baseline unchanged (no multi-entry
corpus files — no regression); 297 tests green. No paid calls.
**Watch-outs:** an extra document that genuinely does NOT pertain is the
target case — a reviewer must confirm it (red until then); the flag is a real
pertinence signal, so it is NOT ignored by eval_verification (unlike
missing_value/invalid_source_region); the all-mismatch case intentionally
does not emit unbound rows (mismatch rows own it).

### D60 — Benchmark-ready eval: manifest join + multi-doc scoring  `ACTIVE`
**Date:** 2026-09-29
**Problem (edge-case review):** the benchmarks only worked on corpus runs.
UI/workflow runs ingest uploads under content-hash stems, so `eval.py`'s
truth join found nothing, and `eval_verification` scored top-level
`header_fields` only — a multi-document run scored zero cells. A run mixing
models/prompt versions was silently averaged.
**Decision (owner-approved plan D60, question 3 answer):**
- `eval.resolve_stems` is manifest-aware: `<run>/manifest.json` maps each
  content-hash stem back to the ORIGINAL filename, so truth rows join by the
  original name too; lookup accepts raw or alnum-normalized truth keys.
  `eval_verification` imports the same function (`_truth_stem_map`).
- Multi-document scoring: `eval_verification.read_run_values` returns
  `{stem: {template_id: fields}}` ("" for single-document JSONs);
  a truth cell may carry an OPTIONAL `"template"` pin (question 3) and
  `_view_fields` resolves it, falling back to the top-level/first entry for
  legacy truth. Flags count for a pinned cell only from that template (or
  file-level rows). The stack scorer pins `doc_id` per truth cell.
- `eval._scoring_view` scores multi-document runs against the
  broker-invoice-shaped corpus truth by selecting the first found
  `broker_invoice` entry (header_fields/line_items/selected_document, with
  `_meta.document` defaulted) — single-document JSONs pass through unchanged.
- Run-info header: `eval.run_info` reports models + prompt versions with file
  counts and sets `mixed`; both harnesses print it and WARN loudly when a run
  mixes models or prompt versions (not a single benchmark sample).
**Evidence:** new `tests/test_eval_join.py` (10 tests: manifest join, corpus
dir names, scoring view, run info mixed/uniform, template-pinned multi-doc
scoring, view fallbacks); `make eval-verify` on the corpus baseline unchanged
(precision 0.500 / recall 1.000); `make eval` now scores the owner's UI run
`2026-09-28/2` end-to-end (selection 2/2, header 100%, sources 9/9, line items
2/2 via the manifest join) where it previously found no joinable files;
307 tests green (1 conditional skip). No paid calls.
**Watch-outs:** a template-pinned truth cell requires the run to carry that
entry (single-document runs only have the "" view — pin only multi-doc truth
sets); the stack section's fixtures still read the corpus capture, so scoring
a UI run with the default `--raw` (that run's own raw) makes fixture
grounding fail — pass `--raw` of a corpus capture when using fixtures; prompt
provenance is "?" for pre-D44 runs.

### D61 — Orientation: detect sideways, never rotate (the A/B was noise)  `ACTIVE`
**Date:** 2026-09-29
**Problem (non-VLM audit, `make audit-nonvlm`):** the D22 4-way A/B was
provably unreliable:
- the gate ran on ASSEMBLED lines; sideways columns share y-centers and merge
  into one wide union → tall fraction 0 → the gate never fired on such pages
  (`jpeg_print_rot90` fixture: raw fragments 100% tall, merged line wide);
- the composite cannot tell upright from 180°-flipped: Apple Vision reads
  upside-down text just as confidently (exact ties 3.15 vs 3.15 on the
  print-rot fixtures) and no page-content prior separates the two (text mass
  and largest-line-position measured over the corpus: overlapping ranges);
- the same page flips its winning delta between identical runs (7 pages
  changed orientation between the baseline capture and the replay), and
  file-level consensus amplifies the noise instead of fixing it (measured:
  consensus made sample_a_1 uniformly upside-down);
- visually confirmed wrong pages in the shipped baseline (`sample_a_1` p2,
  `sample_a_3` p8, `sample_c_1` p12 upside-down) and in the owner's run `2026-09-28/2`
  (`c7d22038` p11 upside-down → all 4 BOE fields failed grounding).
**Decision:**
- the gate runs on RAW fragments (sideways detection works, incl. merged
  columns) and the page is reported as `orientation: "unresolved"` +
  `orientation_confidence: "sideways-kept-original"`;
- pages are NEVER rotated by the harness. The page keeps its own frame, so
  the VLM render, the OCR spans and the vision dumps share one frame —
  grounding stays consistent and no page is ever flipped upside-down (the
  worst outcome, since it demonstrably breaks grounding);
- the 90/270/180 candidate OCR passes are removed (they fed only the
  unreliable choice; this also saves up to 3 OCR passes per sideways page);
- pure-180° pages (gate cannot fire — wide lines) remain a documented
  limitation: the VLM reads them in-frame, a reviewer sees them rotated;
  a semantic orientation classifier is future work.
**Evidence:** `tests/test_nonvlm_harness.py` TestOrientation (sideways
fixtures detected-and-kept, merged columns detected, two pages of one file
agree, vertical strip not flagged; pure-180 xfail as the documented
limitation); corpus replay: all 16 sideways pages `unresolved`, zero rotated
pages; owner labeling packet (ground truth) at
`out/audit/nonvlm-2026-09-29-d61/orientation/orientation_labels.json`
(16 PNGs); `make check` 366 tests green (10 expected failures documenting the
remaining gaps). No LLM calls.
**Watch-outs:** sideways pages stay sideways — reviewer crops are rotated (the
UI evidence pane shows the same frame the VLM read; models handle 90° well);
the fix applies to NEW captures (existing runs keep their renders — re-capture
to benefit); the detection threshold (`ORIENTATION_GATE 0.35` on fragments)
still governs the report only, never a rotation.

### D62 — Non-VLM hardening round: digits, ICC decode, poor pages, grounding edges  `ACTIVE`
**Date:** 2026-09-29
**Context:** fixes from the D61 audit findings (owner-approved hardening list).
**Decisions:**
- **F-Q1 — digits are script-neutral.** `text_layer._script_ratio` computes the
  Latin share over LETTERS only (a text with no letters is neutral, 1.0);
  `line_category` and `page_text_stats` use it. A readable numeric table is no
  longer "100% trash lines / latin_ratio 0.17" — `numeric_table_scan`
  (14 lines, 55 words) is `good`; Cyrillic+digits still trash.
- **F-R2 — `decodeImageXObject` resolves array/indirect ColorSpaces.**
  `CGPDFDictionaryGetArray` follows indirect references; `[/ICCBased <stream>]`
  exposes the component count via /N (1 gray / 3 RGB). FlateDecode + ICCBased
  scans (scanner output, CGPDFContext) now decode: `pageNNN.image.png` (D21) is
  written and the D20 normalized render engages (`photo_flate_icc` fixture:
  decode_failures 0, render normalized-images, TOP marker readable).
- **F-Q3 — only BLANK pages are dropped from the vision channel.** `poor`
  means the OCR engine failed on a page with real ink (audit: Apple Vision
  returns 0 lines on readable framed pages; sparse/inverted scans miss the
  floors) — exactly where the vision model adds value. `extract._excluded_pages`
  filters `quality == "blank"`; the text channel still drops poor pages (its
  OCR output is unreliable) and the `document_quality` flag is unchanged.
  Cost note: a corpus run sends the handful of handwritten/poor Vendor D pages to
  the model (capped renders, one call per file unchanged).
- **F-G1/G2/G3 — grounding edge classes.** `containment._match_token` strips
  edge punctuation from both sides (trailing sentence periods, quotes,
  parens); `containment._window_match` reconstructs a claimed value from
  ADJACENT tokens — full reconstruction only: OCR-spaced digits
  ("9 804 248" → BOE-01, digit-only windows, >= 5 digits so "1 2" never
  matches "12") and multi-token values (names, "30 Jun 2025", up to
  len(parts)+2 tokens). `locate_value` reports these as exact matches now.
- **F-R1 (visible junk text layer over a scan) — deliberately NOT fixed.**
  The only deterministic rule ("full-page image + visible layer → OCR") would
  regress the D15-certified class (DOC-CLF-series/Vendor A: real digital layers
  over full-page/decorative images) because a junk layer and a real layer are
  indistinguishable without reading the image — and OCR'ing those renders is
  the documented double-print-garbage failure. Accepted risk, documented; the
  VLM still reads the scan (vision-primary) and the junk channel shows up as
  grounding flags for review.
- **Pure-180° pages** remain the D61 documented limitation (semantic
  classifier is future work).
**Evidence:** `tests/test_nonvlm_quality.py` (digit neutrality, trailing
period, quoted value, multi-token, spaced digits, short-digit guard),
`tests/test_nonvlm_harness.py` (numeric table good, Flate/ICC decode +
normalized render + raw dump, blank-only vision filter; framed scan still
flagged poor but now kept for the model); `tests/test_grounding_locate.py`
updated for exact spaced digits; `make eval-verify` baseline unchanged
(0.500/1.000); 362 tests green (3 expected failures: 2 pure-180 + visible
junk). No LLM calls.

### D63 — Cross-platform capture engine: pypdfium2 + RapidOCR  `ACTIVE`
**Date:** 2026-09-30
**Problem (owner):** the entire capture stage was Apple-only — the Swift
harness owns the routing probe (CGPDF), embedded text + geometry (PDFKit),
renders for the vision pass, OCR with boxes (Apple Vision), orientation and
image stats. A Windows/Linux install had no capture stage at all: no renders
for the VLM, no text channel for verification.
**Decision (owner):** an engine seam (`engines/`) with `auto` selection —
Apple Vision when the host can run it (macOS + swiftc), else the
cross-platform engine. Both engines produce the SAME artifacts + report
contract, so quality/grounding/verify/eval/UI are untouched. `--engine` /
`VERDANDI_OCR_ENGINE` override; the chosen engine is recorded in
`report.json` (`engine` provenance).
- **Cross-platform engine:** pypdfium2 (PDFium; renders grayscale 3x with the
  4000px long-edge cap in the displayed /Rotate frame, per-char text
  geometry mapped from unrotated canvas space into the displayed frame,
  image-object coverage) + **RapidOCR** (PaddleOCR PP-OCRv6 models on ONNX
  Runtime; cls disabled, `text_score 0` to keep low-confidence lines like
  Vision; models auto-download to `out/cache/rapidocr`, ~30MB). Polygons →
  bottom-left normalized boxes (Vision's convention) + the Swift
  line-assembly port.
- **Routing probe:** text floors + invisible-text `Tr 3` (pypdf content
  streams) + XObject dims/codec (pypdf) + image coverage (pypdfium2 page
  objects) — the same `classify()` rules.
- **D61/D62 semantics ported:** orientation gate on RAW fragments (never
  rotate), blank-only vision filter (consumer side unchanged), float64
  image stats.
- **Deliberate differences (documented):** `customWords` has no RapidOCR
  equivalent (logged; the text channel only verifies, the VLM is primary);
  `pageNNN.image.png` (raw photo dump) is not written — PDFium renders full
  content per spec and `page_renders` falls back to `render-ocr-input.png`.
- **Fixtures are now cross-platform:** the Swift generator was retired for
  `tests/fixtures/pdfbuild.py` (stdlib + Pillow), so the harness suite runs
  on any OS; `VERDANDI_TEST_ENGINE` selects the engine under test.
- **Bug found by the port:** Swift `imageStats` accumulated in Float32 — a
  uniform 253 render reported `std_dev ~16`, misclassifying blank pages as
  "poor" (and D62 sends poor pages to the vision model). Now Double.
**Evidence:** fixture parity sweep **23/25 identical** (source/quality/
orientation); the 2 diffs are OCR-quality-only (print_rot180: Vision reads
upside-down text better; scan_invisible_layer: Paddle 50 words vs Vision 43
— one floor); corpus routing vs the Apple baseline **0 diffs** on
sample_b_1/classic_3/sample_c_1/sample_a_1 (42 pages); `make check` 376 tests;
CI matrix (ubuntu/windows/macos) with model caching.
**Watch-outs:** the two engine-specific quality diffs are accepted (text
channel only); Paddle reads sideways/upside-down text worse than Vision
(D61 keeps pages unrotated — expected); the first cross-platform run needs
network for model download; deps stay optional/lazy
(`requirements-crossplatform.txt`) so Apple installs remain pyyaml-only.

### D64 — Orientation solved: local 4-class doc-ori classifier  `ACTIVE`
**Date:** 2026-09-30
**Problem:** D61's "never rotate" (the Vision geometry A/B was noise) left
sideways pages in the VLM render. The D63 A/B measured the cost: sample_a_1's
invoice number/date were misread sideways (`IZYAAE56`/`06-Feb-2025` vs the
pre-D61 `DOC-D-01-misread`/`21-Feb-2025`), and Paddle reads sideways text even worse.
**Decision:** a trained LOCAL classifier decides orientation —
**PP-LCNet_x1_0_doc_ori** (Baidu/Paddle, Apache-2.0; 4 classes
0/90/180/270; 99.06% Top-1; ~3 ms CPU/page). Model committed at
`engines/models/PP-LCNet_x1_0_doc_ori.onnx` (7 MB, SHA256 `1db9914a…`),
converted with the official Paddle2ONNX tool from the official inference tar
— no official ONNX download exists, and the third-party HF conversion
measured worse (missed 180° on digital pages). Preprocessing pinned from the
model's `inference.yml`; **confidence = logit margin** (the model is not
softmax-calibrated; correct predictions measured 0.6–0.9, uncertain pages
< 0.5).
- **Correction pass** (`text_layer.correct_orientation`, after capture and
  BEFORE the quality gate): pages with margin ≥ 0.5 are rotated upright —
  the render dumps (OCR input / vision input / raw photo) and the
  text-channel geometry (segments re-assembled; embedded bbox rows rotated)
  move together, so the VLM render, OCR spans and reviewer crops share one
  frame. Uncertain pages keep their frame (D61 fallback).
- **Engine split:** the cross-platform engine classifies BEFORE OCR (Paddle
  reads sideways text poorly) and pre-rotates; the Apple engine's Vision
  reads sideways text well, so the text-layer pass rotates its geometry.
  Both end with identical upright artifacts; the text-layer pass skips
  engine-pre-rotated pages (`orientation: rotated-*`).
- Label semantics: the CCW angle needed to correct (PIL convention); pure
  180° is now handled — **F-O2 closed**.
**Evidence:** synthetic consistency 92.7% (327 rotations; inconsistent cases
all margin < 0.5 except two on upright pages); corpus: **16/16 sideways
pages corrected identically on both engines** (sample_a_1 p1/2/7/8, sample_a_2
p1/7/8, sample_a_3 p1/7/8, sample_c_1 p10/11/12, sample_c_2 p9/10/11) with
idempotence 16/16 and quality gains (sample_a p8 poor→good); visual verification
(sample_a_1 p2, sample_c_1 p12 upright); paid A/B: sample_a_1 fields fully recovered to
the pre-D61 baseline (`DOC-D-01-misread`, `21-Feb-2025`, `OCR-TOKEN-01`, `04.07.2024`,
`AMT-03`, `ACM6824H`; flags 5 vs 8 sideways), sample_c_2 invoice
number/date recovered; `make check` 382 tests (the F-O2 expectedFailure
removed).
**Watch-outs:** blank pages and some handwriting stay below the margin
threshold → they keep the D61 frame (`unresolved`); onnxruntime + Pillow are
optional (`requirements-orientation.txt`) — without them the pipeline keeps
the D61 behavior; the Swift `/Rotate` 90/180 render branches remain 180° off
(pre-existing D20 caveat, no corpus file uses them; PDFium renders /Rotate
correctly and the classifier corrects the cross-platform path).

### D65 — Azure Content Understanding benchmark harness (100 docs, two runs)  `ACTIVE`
**Date:** 2026-09-30
**Context (owner):** benchmark Verdandi against Azure Content Understanding
(ACU) on cost, accuracy and token consumption; scaled to 100 docs with
AI-only truth (owner: no human review; maximize truth accuracy).
**Decision:**
- **Containerized harness** `benchmark/` — no core module imports it (guard
  test `tests/test_benchmark_isolation.py`); its own deps file and its own
  `rules_documents.yaml` profile, loaded by pointing `config.DOCUMENTS_PATH`
  at it INSIDE the runner process only (the shipped documents.yaml is never
  read or written by the benchmark); data/results under `benchmark/data/`
  (gitignored); `invoices/` is never used (owner constraint).
- **Dataset (100 docs / 103 pages):** 35 FATURA2 (CC BY 4.0), 25 CORD v2
  (CC BY 4.0), 10 Kaggle "Invoices for Document AI" (CC BY 4.0; machine
  labels re-derived from the scans), 30 deterministic synthetic docs (clean
  digital, degraded scans rot90/180/270+blur+noise+jpeg15+low-contrast+skew,
  handwriting, Hindi+English, multi-doc); DocILE deferred (owner). Truth:
  adapters map source annotations, the 10 Kaggle docs were read from the
  images, synthetic by construction; all 100 AI-verified with per-dataset
  provenance notes and sha256-frozen in `benchmark/frozen/manifest.json`
  (pilot freeze kept as `frozen/pilot-30docs-manifest.json`) BEFORE any paid
  run. CORD rows = printed net (itemsubtotal | price + signed discount); all
  25 reconcile to the printed subtotal.
- **Fairness:** identical normalized PDF bytes to both systems; both
  zero-shot; ACU custom analyzer `verdandi_benchmark` (prebuilt-document
  base, resource defaults PATCHed to the deployed `gpt-5-mini` +
  `text-embedding-3-large`); Verdandi benchmark profile with gpt-5.6-luna;
  one call per document each (+ capped narrow rechecks for Verdandi).
- **Scoring:** per-field normalized accuracy (unicode-aware text; day-first
  via D58 plus month-first and 2-digit forms in the SCORER only; numeric
  amounts/quantities; currency symbols→ISO codes incl. BF→BEF), line-item
  row matching (amount primary) P/R/F1 + cell accuracy, cost from each
  system's recorded `usage` × a rate card (ACU: Azure Retail Prices API
  eastus 2026-09-30; Verdandi: models.dev hint rates — GO subscription quota
  proxy, labeled).
**Results (100 docs, two runs each):** field macro **96.7%** Verdandi
(97.2 / 96.1) vs **95.2%** ACU (95.2 / 95.2); line items P/R/F1 **94.7 /
95.1 / 0.949** (0.951 / 0.947) vs 88.4 / 90.2 / 0.893 (0.895 / 0.891);
found 100/100 both systems, both runs; amount cells 466/466 vs 442/442;
cost per run **$0.3319** (Verdandi, quota proxy) vs **$0.7174** (ACU,
billed) = **2.16×**; tokens ~700k/run (Verdandi) vs ~567k/run (ACU, incl.
103k contextualization). Residual ACU error is deterministic: receipt
semantics (4 CORD totals + 8 synthetic totals per run; `receipt/clean`
18/24 both runs vs Verdandi 24/24) + Kaggle scan identity quirks; Verdandi’s
residual is smaller and spread (scattered single cells, 3 syn currency).
**Later rounds (2026-10-06):** two further rounds re-ran the same 100 documents
after ACU's model deployments were upgraded to GPT 5.5 (removing the
gpt-5-mini constraint above). The definitive one — Verdandi on gpt-6-luna with
full-resolution renders vs ACU on GPT 5.5, two runs each, interleaved —
scored field macro **97.0%** vs 96.2%, line-item F1 **0.958** vs 0.947,
at **$0.2121** vs **$2.5523** per run (12.0×). Full record:
`benchmark/BENCHMARK-REPORT-2026-10-06.md`; the numbers above are the D65
round as accepted on 2026-09-30 and are kept for the record.
**Pilot note:** the first pass (30 docs, one run each) scored 94.5% macro
both, F1 0.781 vs 0.809, $0.1041 vs $0.2196. Scale-up review found the
scan-family synthetic PDFs clipped the Amount column ~20 px past the page
edge (generator column math), penalizing the copy-only reader while the
computing reader recovered the product; generator fixed (column right edge
= page width − margin), truth values unchanged, re-frozen, both systems
re-run on corrected bytes. Also surfaced: `compare.norm_name` crashed the
batch on an object-typed model value (schema permits dict for object
fields) — now returns no-agreement, never crashes; regression test added.
**Watch-outs:** truth at 100 docs is AI-verified (Kaggle scans carry the
highest residual risk; per-dataset notes live in each truth file); public
datasets may be in either model's training data (synthetic leg is the clean
control); ACU cost is real spend while Verdandi's is a quota proxy; FATURA
has no line-item labels (line-item scoring covers CORD/Kaggle/synthetic);
the scorer's normalizations are benchmark-only and do not change D58; ACU
requires resource model deployments + a defaults PATCH before analyzer
creation. Report: `benchmark/data/results/reports/`.

### D66 — Downloadable desktop application (Tauri + React + engine sidecar)  `ACTIVE`
**Date:** 2026-09-30
**Context (owner):** Verdandi must ship to outside customers/partners as a
DOWNLOADABLE application, not a Streamlit app. Decisions (owner): macOS
(Apple Silicon) + Windows; a proper window app (no browser tabs); operator
flow first (upload → run → review → export); partners bring their own
gateway key for now, with a clean seam for a managed relay or other
providers later; installers + auto-updates hosted on private GitHub
Releases (signing/notarization in Phase 2) — amended 2026-10-30 by D68:
the release feed is now public, and since 2026-10-31 it is this
repository's own Releases rather than a separate download repo.
**Decision:**
- **Stack:** Tauri 2 window + React/TypeScript interface (`desktop/src/`,
  Vite) + the EXISTING Python engine as a PyInstaller sidecar
  (`desktop/sidecar.py`, shipped as Tauri `externalBin` "engine"). The
  engine's logic is untouched — `desktop/engine_service/runner.py` calls
  the same `text_layer.run_batch` → `extract.run_batch` entry points as the
  CLI, so desktop results use the identical code path.
- **Local engine service:** `desktop/engine_service/` — a FastAPI helper on
  127.0.0.1 (`/health`, `/defaults`, `/jobs`, `/jobs/{id}`,
  `/jobs/{id}/documents[/{stem}]`, `/jobs/{id}/flags`). Jobs are queued on
  a single background thread; per-file progress is derived from the run
  dir (capture writes `raw/<stem>/report.json`, extraction writes
  `json/<stem>.json`) — crash-safe and pollable. Workspace runs live under
  `~/.verdandi/runs/<date>/<n>/` (VERDANDI_WORKSPACE overrides); inputs are
  copied into each run's `inbox/` so runs stay self-contained (D18).
- **Isolation:** no core module imports `desktop/` (guard test in
  `tests/test_desktop_service.py`); `desktop/` imports nothing from
  `benchmark/`; the shipped `rules/documents.yaml` and CLI/Make targets are
  untouched; Streamlit stays alive internally until parity.
- **Packaging chain:** `desktop/build_sidecar.py` (PyInstaller, collects
  uvicorn/fastapi/pydantic + the pypdfium2/RapidOCR/onnxruntime capture
  stack) → `src-tauri/binaries/engine-<target-triple>` →
  `.github/workflows/desktop.yml` builds the whole app on macos-14 and
  windows-latest and uploads installers as artifacts.
- **Key handling (Phase 0):** existing `.env`/env-var resolution; OS
  keychain storage is a Phase 1 item. The service binds localhost only.
**Phase 0 delivered (this entry):** engine service + 12 offline tests
(fake runner writes the CLI's artifacts; no paid calls), sidecar entry +
build script, React skeleton (path intake → live job table → document
viewer/raw JSON), Tauri shell that spawns/kills the sidecar
(VERDANDI_SIDECAR_PATH overrides for dev), icons generated from the wing
mark, CI workflow (build-only; first CI run is the Rust compile gate — no
Rust toolchain on the dev machine).
**Phase 1 operator flow (2026-10-01):** intake with native file/folder
pickers (tauri-plugin-dialog) + window drag & drop + capture-only mode;
run detail with per-file progress and retry-unfinished; results screen
(header fields/line items/confidence/verification, multi-document entries);
review screen with evidence crops from the D36 located box (page render
fallback), located text, claimed-page mismatch callout, confirm/edit and
mark-issue writing through to review.json + the run's JSON via ui.lib
(D39 semantics reused, never re-implemented); Excel export via
export.export_run (D54) with download; disk-backed runs history that
survives restarts (interrupted runs surface as failed); settings
(profile/model/workers in <workspace>/settings.json; gateway key in the OS
keychain via optional keyring, env fallback). Engine routes: /settings,
/jobs/{id}/retry, capture mode, /jobs/{id}/flags (enriched), crop/page
images, /jobs/{id}/review, /exports + /jobs/{id}/export[/{file}]. 22
offline desktop tests; window + picker plugin verified live (Rust toolchain
installed locally).
**Phase 3 Batch 1 — run lifecycle & disk (2026-10-02):** `GET
/jobs/{id}/storage`, `POST /jobs/{id}/prune` (deletes page-render PNGs, keeps
JSON/flags/reviews/exports), `DELETE /jobs/{id}` (refuses queued/running),
`GET /storage` workspace usage; `auto_prune_renders` setting (default ON)
prunes after a successful export; Review/Results show a "renders pruned"
fallback to located text. Owner decisions (2026-10-02): partner-ready
release phase, updates via a separate PUBLIC releases repo (binaries only,
source stays private), unsigned installers until certificates exist, renders
pruned after export/review. Live-verified: 143 KB freed on a real capture
run, metadata intact, delete 404s afterwards.
**Phase 3 Batch 2 — large-batch UX (2026-10-02):** `POST /paths/summary`
(intake: files/size/≈calls + large-batch hint), run ETA from elapsed/done +
queue position for queued jobs, per-file failure detail (error text from the
failed JSON), "Results so far" during a run, Runs multi-select + bulk delete.
Live-validated at volume: 100-file capture in 45 s (3 workers) → 151 MB of
page renders → prune freed 150.7 MB, 11.4 MB metadata kept.
**Phase 3 Batch 3 — releases + auto-update (2026-10-02):** tag-driven
`.github/workflows/release.yml` builds both platforms with
updater-signed artifacts (`createUpdaterArtifacts`) and publishes
installers + `latest.json` to the public Releases page (originally a
separate download repo, `verdandi-app`; folded into this repository
2026-10-31)
repo via a fine-grained PAT (`RELEASES_TOKEN` — owner action; both the PAT
and the separate repo were superseded on 2026-10-31, see the amendment
above);
Tauri updater
plugin wired (endpoint latest.json; quiet launch check, Settings check,
install & restart via the process plugin); `scripts/version.mjs` syncs
package.json / tauri.conf.json / Cargo.toml / engine `__version__`;
`scripts/make_latest_json.mjs` composes the manifest; updater keypair
generated and stored as repo secrets + local backup `~/.verdandi/keys/`;
certificate secrets, when added, activate OS signing with no workflow
rework. Runbook: `desktop/RELEASING.md`.
**Phase 3 Batch 4 — live paid E2E (2026-10-02):** one benchmark PDF through
the real service path: capture (free) → catalog (paid, correctly reported
"invoice, page 1") → pin p1 → extract → results (6 fields, 4 line items,
found) → review write-through (review.json + JSON) → workbook export →
auto-prune fired (2 renders, 143 KB). Total spend **$0.0036** (8,356 prompt
+ 1,597 completion tokens at hint rates). The desktop integration is
end-to-end verified on the live gateway.
**Phase 4 Batch 1 — in-app Templates editor (2026-10-02):** `templates.py`
service (list/save/delete/duplicate/validate over `ui.lib.documents_to_yaml`
+ the real `validate_documents` contract + atomic save) with routes
`/templates[/yaml|/validate|/duplicate]`; UI: profile list with
workflow-reference badges, guided editor (identity/keywords/min-score,
fields with type/threshold/labels, line-item columns + roles, row rules +
document rule, table note), duplicate/delete (D52 warnings, never refusals),
expert YAML with validate+save. CORE FIX: `ui.lib` resolved
DOCUMENTS/WORKFLOWS/EXPORTS paths at import time — now at CALL time (D10),
so packaged apps edit the user config layer (the test suite was updated to
patch `config.*_PATH`; one test had briefly written a temp exports file to
the real path before the fix, restored from git).
**Phase 4 Batch 2 — in-app Workflows editor (2026-10-02):** `workflows.py`
service (list with profile catalog + D52 warnings, save/delete/duplicate,
yaml validate/save) and routes `/workflows[/yaml|/validate|/duplicate]`;
UI: ordered documents/expected editors (order = prompt order, up/down),
cross-check rule builder (left/right template selects + field inputs with
profile-field datalists), duplicate/delete, expert YAML.
`ui.lib.validate_workflows` now surfaces D52 warnings in its message (the
Streamlit tab shows them too).
**Phase 4 Batch 3 — in-app Export builder (2026-10-02):** `exporting.py`
gains the D56-style source catalog (file info / workflow documents with
labels+descriptions / line-item columns / cross-checks), profile CRUD over
`ui.lib` (call-time config paths), workflow seeding, and a DRAFT preview via
`export.build_rows` against the newest extracted run (25 rows, review-status
colours); routes `/exports/catalog|/seed|/preview|/yaml|/validate` + POST
/DELETE /exports[/{id}]. UI: column cards (header, grouped "Data to show",
type override, reorder/duplicate/delete), workflow seed button, live preview,
profile list. CORE FIX (found by the preview): `export._found_entries`
consulted only `documents[]` entries, so template columns were SILENTLY
BLANK for every single-document run — the app's primary shape; it now binds
the file's own document when `_meta.document` matches (legacy runs without
`_meta.document` accepted). Regression tests in tests/test_export.py.
Phase 4 (owner tools) complete: Templates, Workflows, Export builder are all
in-app; Streamlit is now optional.

### D67 — Streamlit UI retired  `ACTIVE`
**Date:** 2026-10-02
**Context:** with Phase 4 complete, every capability the Streamlit app offered
had to exist in the desktop app before the old UI could be retired.
**Decision:** a full feature-parity audit of `ui/app.py` (7 tabs, ~2350
lines) against the desktop app was performed; genuine gaps were closed
first:
- per-file catalog sweep (`POST /jobs/{id}/catalog {stem}`), per-file
  extraction scope (`extract.stems` → `run_batch(files_filter=…)`, retry
  scoped too) and per-card "Extract this document" from catalog cards;
- Results: "show all fields" overlay (every located/claimed box on the
  page), raw extraction JSON view + download, workflow summary table
  (`GET /jobs/{id}/summary` from summary.csv);
- Review: field filter + confidence column;
- Runs/RunDetail: flag/review counters + token usage (`GET
  /jobs/{id}/metrics`, batch `POST /jobs/metrics`) with per-run stacked
  prompt/completion/recheck mini-bars (replaces the Altair chart);
- Templates: allow-no-match checkbox, document-rule kind selector
  (`line_items_sum_to` | `sum_equals`), last-template delete guard;
  Workflows: last-workflow delete guard; Export builder:
  `include_line_items_sheet` option;
- Intake: per-run model override (the sidebar picker equivalent); Settings:
  catalog refresh, new/removed drift lists, verify-all candidates.
Deliberately not ported (presentation-only or covered): the Excel-style
letter header strip and raw-source JSON in the export builder (the grouped
source dropdown carries the same information), the Streamlit draft
round-trip mapping (the desktop editor keeps profile dicts verbatim — the
old `ui.app` round-trip tests were replaced by a desktop preservation test),
and "save & test on a file" (equivalent: run the profile from Intake).
**Retired:** `ui/app.py`, `ui/theme.py`, `.streamlit/`, `requirements-ui.txt`
and the AppTest suites (`test_ui_app_smoke`, `test_ui_export_tab`,
`test_ui_workflow_editor`) are deleted; `make ui` is removed. `ui/lib.py` and
`ui/render.py` STAY — the desktop engine service reuses their pure helpers
(review write-through, flag keys, config serialization/validation, evidence
rendering) and `tests/test_ui_line_items.py` / `test_render_evidence.py` /
`test_ui_stages.py` keep covering them. Evidence: suite 462 green (desktop 77),
`make check` clean, cross-platform CI green, desktop packaging green.
**Watch-outs:** fixed sidecar port 8787 (env `VERDANDI_SIDECAR_PORT`);
CSP is null in Phase 0 — tighten before beta; the sidecar carries the
whole capture stack (~hundreds of MB installed — expected); native file
pickers and the Tauri dialog plugin land in Phase 1; signing/notarization
+ auto-update are Phase 2; the 1024px icon is an upscale of the 512px wing
logo (final artwork from the owner TBD).

### D68 — Open-source release under Apache-2.0  `ACTIVE`
**Date:** 2026-10-30
**Decision:** Verdandi is released as open source under the **Apache License,
Version 2.0**. The proprietary EULA is deleted (it cannot coexist with an
open-source grant: Apache-2.0 permits redistribution and does not allow a
revocation right). The local-first privacy policy is rewritten accordingly.
**Access control removed:** the Cloudflare access Worker, `access.py`, the
feedback transport (`feedback.py`, the `/access` and `/feedback*` routes, the
onboarding contact step and About → Send feedback) and every `_gate_paid`
call are deleted. The killswitch was a client-side courtesy gate; publishing
the source makes it bypassable, and an OSS license cannot reserve a revoke
right. The app now makes no requests to the maintainers.
**Benchmark data retained:** the 100-document analysis set stays in the
repository (CORD, FATURA2 and Kaggle "Invoices for Document AI", all
CC BY 4.0) with full attribution in `THIRD_PARTY_NOTICES.md` and
`benchmark/data/NOTICE.md`. DocILE data is not included.
**Publication model:** the internal engineering journal (`AGENTS.md`) is not
published; the public repository starts from a clean snapshot rather than
the private development history. `architecture.md` and `roadmap.md` remain
public design records.
**Why:** open source is the trust story for a BYO-key, local-first document
tool — users can audit what leaves their machine and how values are
verified. **Watch-outs:** the benchmark set makes clones heavy (~100 MB);
the shipped model gateway is a service dependency, not part of the license;
the Verdandi name and logo are not granted by Apache-2.0 (Section 6).

### 6.3 Appendix A — original decision entries (verbatim archive)

Preserved byte-for-byte (modulo trailing blank lines) so that no decision content, evidence number, or dated note is ever lost. Cards (6.2) are the curated digest; this is the archive. Grouped by decision:

#### A1 — Two-stage pipeline: deterministic capture + LLM interpretation
**Decision:** OCR (or embedded-text extraction) first, then one LLM call for
selection + extraction. No template matching, no regex field parsing.
**Why:** Industry research (2026) is unanimous: template/regex pipelines are brittle
against vendor layout changes; a single schema-constrained LLM call handles new vendor
layouts zero-shot. OCR is a deterministic, near-free step; the LLM provides the
reasoning layer. Splitting capture from interpretation also makes each stage testable
independently (roadmap Stages 1 and 2).
#### A2 — OCR engine: Apple Vision via a small Swift harness
**Decision:** Render each page at 3× scale with PDFKit, OCR with Vision
(`RecognizeTextRequest`, `.accurate`, `en-US`, `usesLanguageCorrection`,
`customWords` from `rules/brokers.yaml`). One compiled subprocess per PDF.
**Why:**
- Free and local (privacy + zero marginal cost at >500/month).
- Empirically validated on this exact corpus: 100% on critical fields, 95–97% token
  recall (Section 2).
- No installs: Swift ships with macOS; Python needs stdlib + `pyyaml` (D14).
  LLM calls go through `llm_client.py` (stdlib `urllib` → opencode Zen gateway, D4 note).
- Alternatives rejected: Tesseract/ocrmypdf (worse accuracy on noisy tables, needs brew
  install + preprocessing), cloud OCR (external upload, per-page cost — rejected by owner).
- 3× upscale because corpus scans are only 124–300 DPI; Vision needs effective ~400+ DPI
  for small print.

> **Note (2026-08-01, Stage 1):** implemented with the modern Swift API
> (`RecognizeTextRequest`, macOS 15+) instead of the deprecated
> `VNRecognizeTextRequest`; the harness also runs a per-page CGPDF probe and
> does the embedded-vs-OCR routing (D15), so one subprocess call covers the
> whole Stage 1 capture. `--documents-ab` optionally runs
> `RecognizeDocumentsRequest` (macOS 26+, A/B experiment, Stage 1 only).
#### A3 — Skip OCR when the PDF has an embedded text layer
**Decision:** Embedded-text detection is a **CGPDF content-stream probe in the
Swift harness**, not a Python zlib scan. If meaningful text exists without a
full-page image underneath, use it directly and skip OCR.
**Why:** Digital pages are exact, free, and instant — OCR would only add error.
The Python zlib approach was rejected during Stage 1: a byte-level scan for
`Tj`/`TJ` sequences false-positives massively on compressed **image** data
(`DOC-D-01` showed ~1.18M fake "words" from compressed scan pixels), and it
cannot see text inside Form XObjects or invisible-text mode (`Tr 3`).
The CGPDF scanner (with Form recursion and invisible-text tracking) is
content-aware, so hybrid pages are classified correctly instead of guessed.
#### A4 — Extraction: LLM via the opencode Zen gateway (OpenAI-compatible)
**Decision:** LLM calls go through the **opencode Zen gateway** (GO subscription,
`https://opencode.ai/zen/go/v1/chat/completions`, OpenAI-compatible) with the
opencode.ai/auth key (`OPENCODE_API_KEY`). Bulk model = `gpt-5.6-luna`;
escalation model = `mimo-v2.5-pro` (owner decision 2026-08-02 → `deepseek-v4-flash`
2026-08-04 → `gpt-5.6-luna` 2026-08-04 after the China-region opt-in block on
Flash; the 2026-08-03 `gpt-5.6-luna`/`big-pickle` experiments are recorded
below as history). Model names AND the endpoint come from
`config/models.yaml` — never hardcoded in code (same rule as before). All
HTTP-to-LLM traffic lives in `llm_client.py` (stdlib `urllib`, D14).
Output constrained via `response_format: json_object` + prompt-embedded schema
+ validate-and-retry (max 2 retries) — Gemini's native `responseSchema` is not
available through the OpenAI-compatible gateway.
**Why:** Owner decision (2026-08-02): an opencode GO subscription ($5 first
month, then $10/month) covers both tiers at a flat monthly cost that beats
per-token Gemini billing at >500 attachments/month. GPT-5.6-Luna is US-hosted,
zero-training, and requires no regional opt-in; MiMo-V2.5-Pro covers escalation
with stronger reasoning (~16.3k requests/month, D6). The switch
removed the `google-genai` dependency (D14 note).
**Watch-outs:** (1) Usage is dollar-quota'd ($12/5h, $30/week, $60/month) — a
hit limit returns `GoUsageLimitError`; `llm_client` retries 5xx/429 but not
this class of 4xx, so Stage 5's cost instrumentation must log request counts
against the quotas. (2) `gpt-5.6-luna` is documented at `/responses`
(Responses API) but works on chat/completions at the GO endpoint — verified
live 2026-08-04; if that ever changes, fall back to another
chat/completions GO model. (3) Schema conformance rests on prompt +
`json_object` + retry; Stage 2's exit criteria (≥98% field accuracy,
source pointers) are the proof it holds.

> **Note (2026-08-10, escalation-mode amendment — supersedes the 2026-08-04
> escalation pick):** the escalation tier is now **gpt-5.6-luna with vision
> input** (`escalation_mode: vision`) — same model ID as bulk; the difference
> is INPUT MODE, not model. `mimo-v2.5-pro` is REJECTED as escalation because
> it is text-only and the escalated channel is text (the weak link the tier
> exists to fix). Verified live 2026-08-10: Luna vision reads the classic
> phone-photo page renders 8/8 with zero hints (vision capability experiment,
> artifacts `out/vision-test/`, removed in the 2026-09-09 `out/` cleanup). Stage 5's first cut (trigger + vision pass +
> per-field merge) landed the same day — see D6 amendment.

> **Note (2026-08-04, model decision — supersedes the earlier notes below):**
> endpoint stayed `https://opencode.ai/zen/go/v1` (GO subscription). Primary
> model = **`gpt-5.6-luna`**, escalation = **`mimo-v2.5-pro`** — both
> chat/completions (no llm_client changes), both zero-retention/no-training,
> no regional opt-in needed. The switch away from `deepseek-v4-flash` was
> forced live: Flash's latest build is China-hosted and the gateway returned
> `403 RegionError` demanding a workspace opt-in the owner chose not to enable.
> The paid baseline re-run happened on `gpt-5.6-luna` (2026-08-04): 12/12
> schema-valid; 80.8% field accuracy, 76/124 source pointers — the Stage 1c/
> Stage 2 starting matrix. Baseline outputs are archived as runs:
> `out/runs/2026-08-03/1/` (big-pickle) and `out/runs/2026-08-04/1/` (luna,
> moved 2026-08-09 during the run-layout restructure, D18).

> **Note (2026-08-03, HISTORICAL — kickoff + baseline spike, superseded
> 2026-08-04):** Stage 2 kicked off with `gpt-5.6-luna` as test model
> (US-hosted, chat/completions; escalation was then `deepseek-v4-pro`); the
> baseline spike itself ran on the free **`big-pickle`** (`extract.py --model
> big-pickle --max-tokens 40000 --timeout 900`, reasoning-heavy, ~24k
> completion tokens/call; owner-approved free-tier data policy). Result:
> **84.8% header-field accuracy (84/99), 85/92 source pointers** — plumbing
> validated (prompt → schema → retry → eval), NOT the production baseline.
> Ground truth = `invoices/ocr_summary.json` (owner-verified; strict set =
> selection, vendor, billed_to, invoice_number, invoice_date, currency,
> taxable, tax, total — refs informational; owner-declared eval exclusion:
> DOC-CLF-01). Loop = `make extract-corpus` / `make eval`; one-call design
> kept (D1). The paid re-baseline runs on `gpt-5.6-luna` (the 403-blocked
> `deepseek-v4-flash` attempt overwrote one baseline JSON in `out/json`).
#### A5 — No regex/template extraction — learned the hard way
**Decision:** Zero field extraction via regex/templates.
**Why:** The previous attempt (`extract.py` experiment, ~151 lines) was ~80–90%
vendor-specific: literal amounts hardcoded in regexes, dispatch keyed on filenames,
silent blank failures. It worked on the 10 test files only by luck. This project
rejects that approach entirely: vendor detection by content, extraction by LLM,
validation by arithmetic.

> **Note (2026-08-19, D24 boundary):** the rules locator does NOT resurrect
> template extraction. It LOCALIZES a field by label adjacency (exact label
> strings from `rules/labels.yaml`, D10) and ACCEPTS the candidate only via
> format checks (number shape / parseable date / GSTIN shape — D8 validation
> semantics). It never matches value patterns, never hardcodes values, never
> keys on file names, and it is not the source of truth — vision is primary and
> the locator's output is a cross-check that flags differences (D24).
#### A6 — Two-tier routing (escalation on low confidence)
**Decision:** Bulk documents go to the cheap flash model. Any attachment with a required
field below its threshold is re-run with the stronger pro model; the better result wins.
**Why:** >500/month makes per-document cost matter. Research-backed pattern: escalate
only validation failures / low-confidence results. This keeps quality high without
paying pro prices for every document.
**Amendment (2026-08-10, owner decision):** the escalation tier is **gpt-5.6-luna
with vision input** (`escalation_model: gpt-5.6-luna`, `escalation_mode: vision` in
`config/models.yaml`) — replacing `mimo-v2.5-pro` (2026-08-04 pick), which is
TEXT-ONLY and therefore useless as an escalation tier: the weak channel being
escalated is TEXT (OCR misreads, e.g. classic_2's printed date read as
`30-Jun-20:25` by Apple Vision at every scale), so the re-run must read the
document's page RENDERS, not more text. The vision pass sends the pages the
bulk pass selected (`pageNNN.image.png` — the RAW embedded photo, D21; falls
back to the OCR-input render) in one call (D1), never leaks expected values
(confirmation-bias trap, demonstrated 2026-08-10), and returns null for
anything not visible. Trigger: any non-null required header field below its
threshold, selection below the broker selector min_score, or bulk schema
failure after retries. Both passes are kept in `<run>/json/<file>.json`
(`_meta.bulk_pass` / `_meta.escalated_pass`) and merged per-field with a
**threshold guard** (evidence 2026-08-10, sample_a_1 regression): vision overrides
a field only when bulk is null or below that field's threshold — a bulk value
at/above its threshold is kept even against a higher-confidence vision value
(D7 trust boundary); disagreements land in `_meta.escalation.disagreements`
for the Stage 6 review path. Cost control: `extract.py --no-escalate` /
`--escalate-all`.

> **SUPERSEDED (2026-08-19, D24):** the two-tier routing is REMOVED. Vision is
> now the single primary pass for every attachment; the second tier was a
> second expensive LLM call whose job (fix the weak text channel) is done
> better by making vision primary and guarding it with the cheap deterministic
> validator (locator + crosscheck + arbitration). Thresholds now produce review
> flags (below_threshold) instead of escalation triggers.
#### A7 — Per-field confidence thresholds + source pointers
**Decision:** Every field: `{name, value, confidence (0–1), source (page/line/bbox)}`.
Thresholds per field in `config/thresholds.yaml`. Below threshold → row in
`<run>/flags.csv` (per-run flags — D17 note amended 2026-08-09, D18).
**Why:** Owner requirement: data above a threshold is trusted and used; below it,
a human checks — and must be able to find the exact spot in the document. Source
pointers are mandatory for that to be practical. Amount fields get stricter thresholds
than reference fields (e.g., refs/notes like BL numbers tolerate more uncertainty than
`total`).

> **Note (2026-08-19, D24):** thresholds no longer drive an escalation re-run —
> they drive review flags (below_threshold) on the merged output. The D7
> trust-boundary role is inverted: the deterministic rules channel (locator +
> crosscheck) is now the guard on the vision primary — amount conflicts are
> arbitrated by the document's own line-item sums, everything else flags with
> both sources.
#### A8 — Deterministic validation layer (no AI)
**Decision:** `validate.py` checks arithmetic (taxable + tax ≈ total; line items sum to
subtotal, tolerance ~₹0.01–0.02 for rounding) and formats (date `dd-MMM-yyyy` /
`dd/MM/yyyy`, GSTIN `\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z\d]`, currency). Failures **lower
confidence and flag** — they never silently correct.
**Why:** LLMs can hallucinate and OCR can garble digits; deterministic checks catch both
regardless of model confidence. A field can have high model confidence yet fail the sum
check — that must trigger review. Validation failures also drive escalation (D6).
*Note from the corpus:* `classic_2` (old DOC-CLF-02) sums to AMT-02b but the document prints
AMT-02a — a 0.20 rounding difference *in the source document*. Validation must
tolerate small rounding discrepancies and flag, not fail.
#### A9 — Page-aware multi-document handling
**Decision:** OCR/extraction output carries page markers (`[PAGE N]`); broker selection
operates per document (page or page-cluster), not per file.
**Why:** `classic_2.pdf` (old DOC-CLF-02) contains three documents in three pages. File-level
selection would be wrong.
#### A10 — Broker rules as hot-loaded YAML
**Decision:** `rules/brokers.yaml` read fresh on every run; structure and weights
documented in Section 5.
**Why:** Owner requirement: rules per broker, changeable at any time (including
emergencies) with no code changes. This is data, not code.
#### A11 — Output: per-run flags + per-file JSON + raw cache (master CSV amended)
**Decision (amended 2026-08-09, D18):** all stage output lives inside a run:
`<run>/flags.csv` (per-run review flags — no master CSV), full detail in
`<run>/json/<file>.json`, cached OCR text in `<run>/raw/`.
**Why:** JSON preserves confidence + sources; raw OCR text makes review and
debugging possible without re-OCR. Runs are self-contained snapshots
(`out/runs/<YYYY-MM-DD>/<N>/`); cross-run aggregation, if ever needed, is a
Stage 7 export — nothing consumes a master CSV today. (Pre-2026-08-09 the
decision was a master `out/summary.csv` + `out/review_flags.csv`.)
#### A12 — Dedup + idempotency
**Decision:** Skip files already in the processed manifest; skip rows whose
(vendor, invoice number) already exists in the run's flags/manifest.
**Why:** >500/month, manual drops, and re-runs must not create duplicates. Invoice
number + vendor is the natural key (stable across vendors).
#### A13 — Manual drop + CLI batch (no email pipeline)
**Decision:** User drops PDFs into `inbox/`; pipeline runs as `python process.py`
(optional `--watch` polling flag).
**Why:** Owner explicitly said mailbox→folder automation is out of scope for now.
#### A14 — Runtime: Python 3 stdlib + `pyyaml`; Swift only for OCR
**Decision:** Python for everything except the OCR primitive, which stays a compiled
Swift subprocess.
**Why:** Minimal dependencies (only `pyyaml` — YAML is the broker-rules format D10 is
built on, so the stdlib-only claim does not hold; documented in `requirements.txt`),
reuses the empirically proven OCR harness, and keeps the OCR step reproducible
(source kept in `swift/ocr.swift`, compiled on demand).

> **Note (2026-08-01, Stage 0):** D14 wording amended — `pyyaml` is an explicit
> runtime dependency, not stdlib. Stage 0 confirmed the system Python is PEP 668
> managed, so a project `.venv` is the setup path.

> **Note (2026-08-02, provider switch):** `google-genai` dropped — LLM calls now
> go through `llm_client.py` (stdlib `urllib` only) to the opencode Zen gateway
> (D4 note). Dependencies are now `pyyaml` + stdlib.

---
#### A15 — Per-page text-source routing: embedded | OCR | empty (inside the harness)
**Decision:** Every page is classified independently by the Swift harness.
Embedded (clean text, not invisible-text mode) → PDFKit extraction.
Scan (no layer, or a layer that fails the quality floors) → OCR. Blank (no
text, no image, no text ops) → empty output. Rules: embedded requires
`chars ≥ 100`, no invisible text, and (`alpha ≥ 0.5` or `words ≥ 20`);
the "empty" case needs zero chars/ops/coverage; everything else is OCR.

> **Note (2026-08-19, D24 role change):** routing decides the quality of the
> TEXT CHANNEL, which since the vision-primary pivot serves ONLY the rules
> validator (locator). The vision pass is fed by renders (written for every
> page regardless of routing, D24) — routing never affects extraction input
> anymore. The "OCR of an embedded page produces garbage" trade-off now only
> degrades a VALIDATOR candidate (correctly low-confidence), never the primary.

**Why:** The corpus taught us three things. (1) Digital pages ship text layers
whose encoding is custom/obfuscated (no ToUnicode CMap — Vendor A' codes
are shifted, e.g. `7 D [ , Q Y R L F H` = `TAX INVOICE`), but PDFKit decodes
them **byte-accurately**: sample_b page-1 layers match ground truth exactly
(vendor, billed_to, GSTIN, invoice no/date, amounts). Re-OCR'ing such pages is
not just wasteful — it is actively harmful: the render fuses the vector text
over the decorative background images into double-print garbage (0.50-
confidence lines like "Ex Floor lets on or dia) Private Limited"). (2) A
strict `alpha ≥ 0.7` text-quality gate misroutes legitimate number-heavy
invoices (waybills, customs documents run α≈0.58–0.63) — alpha is only
meaningful without an image overlay. (3) Scanned pages often wrap their image
in a **Form XObject** (BUYER, Vendor D) — the probe recurses into forms
(depth-limited) and resolves XObjects via the content stream so coverage is
measured correctly.
Known trade-off: a genuinely bad text layer (OCR layer over a scan, alpha
below floors) still routes to OCR via the floors — the layer is only trusted
when it is text-like enough. The `has_large_image` signal (any image ≥1MP
covering ≥50% of the page) is reported per page for future tuning; corpus
true scans are 2.2–8.7MP, decorations ≤160K px.

> **Note (2026-08-01; SUPERSEDED 2026-08-09 on the "48% WER" claim):** Stage 1
> originally routed every full-page-image page to OCR, on the claim that
> "Vendor A pages have an embedded OCR layer with ~48% WER over a scan
> (text mostly inside Form XObjects)". That claim was a mis-measurement: the
> `imageCoverage < 0.8` gate caught ANY full-page coverage, including tiny
> stretched decorations, and the layer quality was judged without a working
> decoder. Direct evidence (2026-08-09): PDFKit extraction of the Vendor A
> layers is byte-accurate vs ground truth (see pageNNN.embedded.txt dumps in
> `out/runs/2026-08-09/2/`), and DOC-CLF-series's embedded GSTIN was the CORRECT
> `GSTIN-02` all along (OCR introduced the Cyrillic С — the D15-note
> trade-off "bit once" is now fixed by routing those pages to embedded).
> The `imageCoverage < 0.8` gate is REMOVED (2026-08-09): Vendor A (all but
> one scan page), DOC-CLF-series and Vendor D typed p2 pages now route to embedded —
> 47/109 pages, up from ~5/109.
#### A16 — Document-quality gate: blank/illegible pages are excluded, never fed to the LLM
**Decision:** `text_layer.py` post-processes every page into
`good | poor | blank | ignored`, evaluated in that order:
- `ignored` — page listed in the broker's `ignore_pages` (owner rule, D10);
- `blank` — zero lines of output **and** the page is visually blank: the Swift
  harness computes per-page `image_stats` on the existing 3× render
  (grayscale std-dev, dark-pixel ratio, Laplacian variance — vImage/vDSP,
  D16.1); a 0-line page whose image stats stay below `max_pixel_std_dev` /
  `max_dark_pixel_ratio` (or that has no image at all) is blank. A 0-line page
  with real image content is `poor` (unreadable scan), not blank;
- `poor` — below automatic floors in the `quality:` block of
  `config/thresholds.yaml`: `min_lines_per_page`, `min_words_per_page`,
  `min_latin_ratio` (script purity — now a guard against fully non-Latin OCR;
  corpus p8s carry too much Latin for it to separate them, D16.2),
  `min_text_area_ratio` (line-bbox area / page area, from `pageNNN.bbox.txt`);
  or a sustained band of garbage lines: per-line categories
  `clear|noisy|trash|non-text` with a 5-line rolling window — worst window
  >80% trash → poor (D16.2);
- `good` — everything else. **Embedded-sourced pages are always good**: D15's
  routing gate already certified their quality and they carry no OCR bbox
  geometry for the text-area floor — re-gating would re-litigate routing.

Excluded pages are dropped from the `[PAGE N]` combined text (the LLM never sees
them), recorded in `report.json` (`quality` + reason per page,
`quality_gate.excluded_pages` with a machine-readable reason:
`owner_rule` | `below_quality_floors` | `blank`), and the file carries a
`document_quality` review-flag record with the reason
("illegible/blank pages excluded") for Stage 6 to lift into
`<run>/flags.csv` (per-run flags — D17 note amended 2026-08-09, D18).
**Extraction proceeds on the remaining good pages** —
exclusion is recorded and flagged, never silent, never a hard skip.

- **Probe (D16.3, config-flagged):** pages inside `review_band × floor` of a
  floor are borderline; when `quality.quality_probe: true` (default **false** —
  determinism + cost) one flash-model call (name from `config/models.yaml`,
  D4) rates the page 0–1; ≥0.6 rescues a borderline `poor`, ≤0.4 condemns a
  borderline `good`, in between keeps the deterministic verdict. Probe results
  are recorded per page and in the gate summary; a failed/disabled probe never
  blocks the batch.
- **Suggestion engine (D16.4):** at batch end, same-position pages `poor`/`blank`
  in ≥2 files of the same broker print a suggestion to add `ignore_pages`.
  Never auto-writes — the owner confirms in YAML (D10).
- **Tuning (D16.5):** `make gate-tune` swept the floors over `out/latest/raw/`
  against the hand-labeled set `tests/gate_labels.json` and printed precision/recall.
  **DEPRECATED 2026-08-09** (labels deleted; floors tuned and locked in
  `config/thresholds.yaml`), **REMOVED 2026-08-11** (D23 cleanup).
  Tuned 2026-08-01: `min_lines 7`, `min_words 45`, `min_latin 0.5`,
  `min_text_area 0.02` — precision 1.00 (zero false positives, incl. the
  named sparse-but-legit pages), recall 8/9: sample_a_2 p8 (7 lines / 96 words /
  89% Latin) is floor-invisible — see "Why".

**Why:** DOC-D-01/56/69 share a layout with a blank p6 and handwritten
pages 7–10 (garbage OCR, Cyrillic contamination, hand-signed notes). OCR
"succeeds" there but the text is untrustworthy, and LLM extraction turns
garbage into confident-looking trash. No automatic metric separates Vendor D
p9–10 (56–103 words) from legitimately sparse pages (BUYER tracking
pages, OLD-EXP-03 p12 template page, DOC-CLF-02 p3 at 46 words). Automatic
floors were tuned by corpus sweep: zero false positives on known-legit
sparse pages while catching blank p6 and garbage p7/p8. One
caveat surfaced by the sweep: DOC-D-02 p8 OCR'd as 7 lines / 96 words / 89%
Latin in the 2026-08-01 run — indistinguishable from a sparse-but-legit page
by every automatic metric. It was rule-covered (`ignore_pages`) until the
owner REMOVED the per-broker rule on 2026-08-09 (page rules are
corpus-specific; the universal floors must be the mechanism). Post-removal:
Vendor D p6–8 excluded by floors, p9–10 + sample_a_2 p8 reach the LLM — the accepted
trade-off of "no corpus-specific page rules". The suggestion engine still
proposes re-adding the rule; it is advisory only (D10).

> **Notes (2026-08-01, Stage 1b implementation):**
> - **D16.1 — harness `image_stats`:** the decision log originally assumed blank
>   detection needs no pixels; the corpus (Vendor D p6) exposed blank-*with-image*
>   pages, so the harness now emits per-page `image_stats` on the existing 3×
>   render. Blank-with-image is detectable: std_dev 0 / dark_ratio 0.0002 /
>   Laplacian 2.4 for p6 vs std_dev 32–72 on real pages.
> - **D16.2 — `min_latin_ratio` cannot catch the corpus p8s:** they OCR at
>   0.75–0.92 Latin (handwriting is Latin-dominant with Cyrillic fragments)
>   while legit DOC-CLF-02 p3 sits at 0.68 (number-heavy). The floor stays as a
>   guard for fully non-Latin pages; p8s are caught by `min_lines`. The
>   5-line trash band likewise maxes at 0.4 on this corpus — it is a safety
>   net, not a corpus catcher.
> - **D16.3/D16.4/D16.5 — probe, suggestion engine, gate-tune:** all added
>   during implementation per the Stage 1b plan; config contract
>   (`quality:` block keys) is in `config.py`.
#### A17 — OCR output hardening: geometry-based restructuring allowed (D5 clarification)
**Decision (2026-08-03, owner agreement):** Before extraction, the raw capture
may be reshaped by deterministic geometry/text processing — reading-order sort
by (y, x), line re-segmentation, column reconstruction, confidence/known-token
annotations. This is NOT field extraction: no pattern matches a semantic value
("total", "GSTIN"); we only change how lines are presented to the LLM. The
owner's line: "as long as it is not some hardcode regex BS."
**Why:** Corpus evidence (Stage 2 kickoff): merged table cells
(`AMT-03AMT-03`, `AMT-06AMT-02`), collapsed `Label : value` runs,
script confusions (Cyrillic С in GSTIN), token artifacts are visible in
`out/latest/raw/` today. Research is unanimous that structure reconstruction before
the model beats more prompting — but the honest gate is the eval matrix.
**Method (evidence-first):** Stage 1c sub-stages (roadmap.md) are built one at
a time, each re-measured against the Stage 2 baseline via `make eval`
(≥98% field accuracy, 100% source pointers). A sub-stage that doesn't move
the matrix but hardens edge cases is kept only at the owner's call.
**Watch-outs:** (1) Re-segmentation must never alter a line's *content*, only
its grouping — the value-vs-source audit (`eval.py`) depends on pageNNN.txt
lines being stable. (2) Confidence annotations feed the prompt as hints, never
corrections (D8). (3) The old "DOC-CLF scan gap" (classic_1/2 header unreadable at
any scale/orientation) is RESOLVED 2026-08-10 by the D20 render fix — the
header was cropped by the render, not unreadable. classic_1 stays
owner-excluded from eval; classic_2's invoice_date retains an Apple-Vision
digit misread (`30-Jun-20:25`, see D20).
#### A18 — Run-based output layout + "file names are never a factor"
**Decision (2026-08-09, owner):**
1. **Runs are first-class and self-contained.** Every corpus pass lives under
   `out/runs/<YYYY-MM-DD>/<N>/` (N auto-incremented per day; `make new-run`
   creates one and symlinks `out/latest` → the run). Stage artifacts per run:
   `raw/` (OCR text + report.json), `json/` (extraction), `logs/`, and
   `<run>/flags.csv` for review flags. Stage CLIs take dirs as args and default
   to `out/latest/...` where sensible. Never overwrite a run — snapshot rule:
   create a new one. (Amends the pre-2026-08-09 flat `out/raw`+`out/json` +
   master-CSV layout; the old baselines were archived as `out/runs/2026-08-03/1/`
   and `out/runs/2026-08-04/1/`.)
2. **FILE NAMES ARE NEVER A FACTOR** (owner, emphatic). Invoice filenames
   (`classic_1`, `sample_a_2`, ...) are for human readability ONLY — not a
   reference, not a rule, not a detection signal, not a pointer key.
   Detection/selection/quality/matching are content-based only. The ground
   truth `file` column is a bookkeeping label for the eval join, verified by
   content (invoice numbers/refs in the text), never trusted by name.
3. **Corpus renames** (content-verified 2026-08-09, 12/12): `OLD-EXP-01→
   sample_b_1`, `OLD-EXP-02→sample_b_2`, `OLD-EXP-03→sample_b_3`,
   `OLD-NIP-01→sample_c_1`, `OLD-NIP-02→sample_c_2`,
   `DOC-D-01→sample_a_1`, `DOC-D-02→sample_a_2`, `DOC-D-03→sample_a_3`,
   `DOC-CLF-01→classic_1`, `DOC-CLF-02→classic_2`,
   `DOC-CLF-03→classic_3`, `DOC-CLF-04→classic_4`.
4. **Ground truth** moved to `tests/ground_truth.json` (CSV dropped; old
   `invoices/ocr_summary.*` and `invoices/ocr_raw/` deleted; `invoices/` holds
   only the 12 PDFs).
5. **`gate-tune` deprecated**: the hand-labeled page set `tests/gate_labels.json`
   was deleted; D16 floors are tuned and locked in `config/thresholds.yaml`.
   The sweep tool stays dormant (clean error if invoked).
#### A19 — Capture reshaping (Stage 1c): fragment persistence + deterministic reconstruction
**Decision (2026-08-09/10, evidence-first):**
1. **Per-fragment geometry is persisted** — `pageNNN.segments.txt`
   (`lineno|fx0,fy0,fx1,fy1|confidence|text`) written by the harness for OCR
   pages, additive: `pageNNN.txt`/`pageNNN.bbox.txt` are byte-identical, so
   all existing consumers (eval audit, D16 text-area floor) are untouched.
   `pageNNN.txt` + line numbers remain the true raw capture (D7).
2. **Reconstruction** (`reconstruct.py`, Python, runs after the D16 gate,
   before `combined_text()`): writes `pageNNN.rec.txt` (+ rec bbox) and
   `combined_text()` prefers rec files — "page N, line M" pointers resolve
   against the rec text (D7 amendment). Line count/order preserved, so
   pointers never shift. Two mechanisms, numeric/geometric only — zero word,
   file-name, or invoice-specific anchors (D5, owner rule):
   - **1c-a fused-token splitting**: two amount-shaped tokens glued by OCR
     (`AMT-06AMT-02`, `AMT-03AMT-03`) are separated using Indian
     lakh/crore grouping + exact-duplicate signals. A lone number is never
     altered (`525.101234` stays intact). Verbatim values preserved (D8).
   - **1c-b column alignment**: numeric fragments are clustered into columns
     by x-center gaps; adjacent numerics in different columns are joined
     with ` | `. No column is ever labeled.
3. **A/B results (corpus runs 2026-08-09/7, 2026-08-10/1-2):**
   - **1c-a + 1c-b path A (manual geometry): KEPT** — header-field accuracy
     86.9% (baseline 85.9%, within variance but never worse), reconciliation
     (line items vs header, no ground truth needed) 11/11 OK, fused-token
     splits verified in rec files.
   - **1c-b path B (RecognizeDocumentsRequest native tables): REJECTED** —
     Apple's table detection found 15 tables, NONE on the actual invoice
     pages (classic_2 p1 CFS/LOADING table missed), and the detected cells
     were noisy (`0.900 TO | 498,598 00/: Td`). Flag stays for future
     re-checks (`--reconstruct-docab`).
   - **1c-d (6× crop re-OCR, `--crop-reocr`): CONDITIONAL** — fixed some
     fragments (`CUrTenn`→`currency`) but moved the matrix by zero; the
     classic_2 header is confirmed unreadable at 6× too — accepted as
     permanent missing→flag. Flag stays for future tuning.
   - **1c-c deferred** (its Cyrillic flagship case was resolved by D15).
4. **eval.py additions (Stage 1c measurement, no ground truth):**
   line-item source-pointer audit + reconciliation extra check (Σ line items
   ≈ header taxable/tax/total, D8 tolerance; classic_2's 0.20 total diff is
   the documented source-side rounding quirk).
5. **Source-file lesson (2026-08-09):** a Preview re-save of `invoices/sample_a_1.pdf`
   (+2.7KB incremental update, same `/Root`) silently CHANGED page-1 content —
   run 2026-08-09/3's sample_a_1 raw came from the modified file (stats std 52 vs
   72 on the restored file). Restored via `git checkout`; runs 4+ use the
   canonical corpus file. Tracked corpus files must never be opened in
   editors that auto-save.
#### A20 — Render fix: normalized image placement + page rotation + native resolution
**Decision (2026-08-10, evidence-first, pixel-proven):** the classic_1/2 "unreadable
header" was NOT a reader limitation — it was a RENDER bug. Those PDFs place their
phone-photo JPEG at 1px = 1pt (image user-size = pixel size, 3507×2480 pts), so
PDFKit's `page.draw` bakes only the mediaBox sliver (the photo's bottom-left
~842×595-px region) — the letterhead and `DOC-CLF-01` were cropped out of
every render. PDF viewers (PDF.js, e.g. Cursor's) scale the image into the box
(unit-square semantics) and show the full document — proven by pixel-matching
(the render = the JPEG's bottom-left crop at MAD 9.4; everything else 21–40) and
by the owner's viewer screenshot, which both Apple Vision and gpt-5.6-luna vision
read 8/8.
**Fix (three parts, in `swift/ocr.swift`):**
1. **Normalized placement** — for pure-image pages (textOps == 0, coverage ≥ 0.95,
   exactly one top-level image, zero decode failures), draw the image into the
   UNIT rect under the original cm transform → the full photo fills the page box.
2. **Page rotation honored** — `renderPage` sizes the canvas in the DISPLAYED
   (rotated) dimensions and applies the transform itself (PDFKit does not apply
   /Rotate in `draw(with: .mediaBox)`). Branch directions calibrated against the
   viewer screenshot: /Rotate 90 → 90° CCW, /Rotate 270 → 90° CW (the initial
   swap produced an upside-down document — footer OCR first).
3. **Native-resolution scale** — pure-image pages render at the image's intrinsic
   pixels per point (4.17× for classic_1/2 → the full 3507×2480 photo) instead of
   the blurry 3× re-sample.
**Verification:** Apple Vision OCR now reads `DOC-CLF-01`/`03779` and
`30-Jun-2025` directly (no LLM); classic_2 invoice_number WRONG→OK; best header
accuracy 87.9% (run-to-run LLM variance: 85.9–87.9%); regression diff: exactly
4 pages change (classic_1 p1, classic_2 p1–3); Vendor D/Vendor C renders byte-identical
(their scans are Form-wrapped — the gate excludes them). Residual known issue:
Apple Vision misreads classic_2's printed date as `30-Jun-20:25` at every render
scale (even the owner's screenshot OCRs it that way) while Luna's vision reads
`30-Jun-2025` (RESOLVED 2026-08-10 by the Stage 5 vision escalation, D6/D21).
`--scale` unchanged. The 90°/180° rotation branches are
written but only 270° is corpus-calibrated (synthetic test pending; D22's
4-way orientation A/B self-heals branch-direction errors anyway).
#### A21 — Raw embedded-image extraction for the vision pass
**Decision (2026-08-10, evidence-first):** the Stage 5 vision pass reads
`pageNNN.image.png` — the RAW embedded image XObject (color, full intrinsic
resolution, /Rotate applied by the harness, `rotateImage` in `swift/ocr.swift`)
for pure-image pages — NOT a page-bounds render. Page renders clip 1px=1pt
photos to the mediaBox (D20) and cut off the letterhead; this is true of
every page rasterizer: PDFKit's `renderPage`, Poppler/MuPDF-class renderers,
and — probed live 2026-08-10 — the gateway's own PDF input. The PDF-input
probe: `chat/completions` rejects `application/pdf` (HTTP 400); the
`/responses` endpoint accepts an `input_file` data URL (HTTP 200, status
completed) but the provider rasterizes the page box, producing the same
shipping-block read with NO `TAX INVOICE`/`DOC-CLF-02` — so direct-PDF
input was rejected in favor of raw extraction. The raw path is deterministic:
the decoded CGImage (already in memory for OCR) is written as-is with the
calibrated /Rotate transform; there is no page geometry to get wrong, and the
image keeps its color (the render path is grayscale — linearGray context).
Vision preference order: `pageNNN.image.png` → `pageNNN.render-ocr-input.png`
(the OCR input; same full photo for these pages — D21.1 renamed the old
misleading `render-images-only.png` label; archived runs keep the old name as
a compat fallback in eval/extract). Evidence (run 2026-08-10/6): classic_2 9/9
strict fields incl. `DOC-CLF-02` + `30-Jun-2025` (conf 1.0), header
accuracy 88.9% (equal to run 4 — the raw image is pixel-equivalent for the
corpus, robust by construction for future photo PDFs).

> **Note (2026-08-19, D24):** the vision inputs are now UNCONDITIONAL — every
> page writes `pageNNN.image.png` (pure-image pages) and/or
> `pageNNN.render-ocr-input.png` (all pages, embedded pages now included via a
> PDFKit page render). The `--dump-renders` flag was REMOVED; the vision pass
> reads whatever the harness wrote, so the primary extraction never depends on
> a command-line flag.
#### A22 — Local page-orientation detection (Apple Vision geometry, no LLM)
**Decision (2026-08-10, evidence-first):** sideways pages (the Vendor D invoices
print rotated ~90° within the page) are detected and corrected locally in the
harness — an LLM is never used for orientation. Apple Vision returns
recognized-text line boxes in the ORIGINAL image space: a sideways print
yields TALL boxes (height > width); upright text yields wide boxes. When the
tall-aspect fraction exceeds a gate (0.35), the harness OCRs all FOUR
orientations of the render (0/90/180/270) and keeps the highest-scoring
composite (wide-line fraction ×2 + line count/50 + mean confidence). A
single +90° A/B is NOT enough: rotating a 90°-off render by 90° makes it
180°-off, which also yields wide boxes (180° preserves aspect in image space)
— the 4-way A/B disambiguates. The winner's render becomes the OCR input and
the vision input (`render-ocr-input.png` / `image.png` are overwritten with
the corrected orientation); `report.json` records `orientation`,
`orientation_tall_fraction`, and per-delta composite scores. Self-healing:
corrects even when the D20 renderPage /Rotate branch direction is wrong (the
90°/180° branch caveat is now moot). Also corrects genuinely-sideways
SUPPORTING pages (Vendor C delivery notes read cleanly after correction).
**Evidence (run 2026-08-10/10, Vendor D-only paid test):** sample_a_1 p1 corrected text
reads `TAX INVOICE` / `DOC-D-01` / `LM WIND POWER BLADES INDIA PRIVATE` /
clean amounts (was `MODMW36` mangled); Vendor D eval vs run 8: invoice_number
0/3→2/3 (`DOC-D-01`, `DOC-D-03` exact), invoice_date 2/3→3/3, billed_to
2/3→3/3, selection 3/3, taxable/total 3/3. Residual: sample_a_2 invoice_number
`DOC-D-02-misread` (vision misread; the corrected text itself reads `DOC-D-02` —
LLM variance); tax_amount `AMT-13` on sample_a_1/3 is confident-bulk garbage kept
by the D7 guard (vision read `990.00` correctly; the D8/Stage-4 deterministic
sum check is the designed catch — disagreement recorded in `_meta`).
#### A23 — Dead-code cleanup (2026-08-11)
**Decision:** removed dormant/deprecated machinery with no pipeline caller,
verified byte-identical after removal (run 2026-08-11/1 == run 2026-08-10/10
page-for-page, OCR text + orientation reports):
1. `gate-tune` sweep (text_layer.py + Makefile target + `--labels`) — labels
   file deleted 2026-08-09; the function only printed a deprecation error.
2. `--only-pages` (text_layer.py + ocr.swift) — no caller existed.
3. `--documents-ab` / `runDocumentsAB()` / `pageNNN.doc.json` and
   `--reconstruct-docab` / `append_doc_tables()` — Stage 1 path-B REJECTED
   (D19); `--reconstruct` path A untouched.
4. `--crop-reocr` / `reOcrWeakFragments()` — 1c-d, zero matrix movement (D19).
5. `--images-only` / `renderPageImagesOnly()` — D15 diagnostic superseded by
   D21/D22; the `render-images-only.png` FILENAME compat stays in
   extract.py/eval.py for archived runs.
6. `.cursor/` (empty).
Kept: `validate.py` (Stage-4 signpost), `quality_probe` (D16.3, config-gated),
`--reconstruct` path A, `--ocr-only`/`--dump-embedded`/`--scale` diagnostics.
#### A24 — Vision-primary pivot: one vision LLM call + a deterministic rules validator (2026-08-19)
**Decision (owner, 2026-08-19 — supersedes the D6 two-tier routing):** the
pipeline is now VISION-PRIMARY. A single schema-constrained vision call per
attachment (D1) reads the page renders (`pageNNN.image.png` / `render-ocr-input.png`,
now written UNCONDITIONALLY for every page — text-LLM extraction is REMOVED) and
does selection + extraction. The text channel (embedded/OCR, D15 routing) feeds
ONLY a deterministic, local, free rules pass — the VALIDATOR, never a second
extractor:
1. **Rules locator** (`locator.py`): label-adjacency candidates
   (`rules/labels.yaml`, D10) over the selected pages' text for the owner-chosen
   fields (`locator_fields` in thresholds.yaml). Format checks only (number /
   parseable date / 15-char ASCII GSTIN) — never value patterns (D5). Silence
   is a valid output: no candidate, the field stays vision-only.
2. **Crosscheck** (`compare.py`): agree → keep vision confidence (owner
   decision, 2026-08-19); amount conflicts → deterministic ARBITRATION by
   line-item reconciliation (the value the document's own line items vouch
   for wins; both/neither → flag); non-amount conflicts and rules-only
   (locator found a value the vision pass left null) → flag rows with BOTH
   sources (render region + text line — dual-anchored flags). Disagreements
   are always recorded in `_meta.crosscheck` (audit).
3. **Deterministic validation** (`validate.py`, D8): arithmetic + format checks
   cap confidence (0.3) and flag — never silently correct.
All flags land in `<run>/flags.csv` (D11/D18). The trust boundary INVERTS the
D7/D6 design: vision is primary, the deterministic channel is the guard (the
D7 lesson — a wrong-but-confident vision read on sample_a_1 — is now caught by
conflict flags and arbitration instead of a restrictive merge).
**Why:** token + quality evidence from the project itself: (1) 2026-08-19 token
test on sample_b_1 — vision 12,739 total tokens vs text mode 20,301 (−37%)
for equal-or-better quality; (2) vision fixed every chronic text failure
(classic_2 date `30-Jun-2025`, sample_b vendor_gstin/tax/breakdown) — the
D15/D20/D22 machinery exists because the text channel is fragile, and vision
reads what a human sees zero-shot for new vendor layouts; (3) the second tier
was a second expensive LLM call — the validator is local and free, and costs
drop from up-to-2 calls per attachment to exactly 1.
**Swap story:** `config/models.yaml` `primary_model` is the single place a
vision model is chosen (verified candidates in the GO catalog as of 2026-08-19:
`deepseek-v4-flash-vision-exp`, `mimo-v2-omni` — unverified). Swap ritual:
edit YAML → run the corpus → `make eval` (matrix + crosscheck stats) → only
then promote (verification-first, as with 2026-08-04).
**Watch-outs:** (1) embedded pages finally have renders — their vision input is
a PDFKit page render (digital pages are vector; the D20 normalized-photo path
still applies to pure-image pages); (2) `source_region` (normalized bbox per
value) is a prompt request — the model may omit it; flags stay text-anchored
via the locator when that happens; (3) the locator must never become a
full-value extractor (D5 boundary: labels + format checks, nothing more).
## 7. Output Schema (draft)

```jsonc
{
  "file": "classic_2.pdf",
  "status": "extracted",               // extracted | flagged | failed
  "selected_document": {
    "broker": "Vendor B",
    "document_pages": [1],             // pages of the broker invoice within the PDF
    "reason": "GSTIN GSTIN-02 matches broker; AGENCY CHARGES fee present",
    "confidence": 0.98
  },
  "header_fields": [
    // D24, 2026-08-19: sources are "page N (render image)" (vision-primary);
    // `source_region` anchors the printed value for dual-anchored review flags;
    // `channel`/`agree` record the crosscheck outcome (vision|both|conflict|
    // rules_arbitrated|vision_arbitrated|vision_miss).
    { "name": "vendor",         "value": "VENDOR-B", "confidence": 1.0,  "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.12, 0.18, 0.62, 0.20] }, "channel": "both", "agree": true },
    { "name": "vendor_gstin",   "value": "GSTIN-02",                        "confidence": 1.0,  "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.12, 0.21, 0.42, 0.23] }, "channel": "vision" },
    { "name": "invoice_number", "value": "DOC-MISREAD-01",                           "confidence": 0.98, "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.60, 0.30, 0.78, 0.32] }, "channel": "both", "agree": true },
    { "name": "invoice_date",   "value": "16-Apr-2025",                            "confidence": 0.97, "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.60, 0.33, 0.74, 0.35] }, "channel": "both", "agree": true },
    { "name": "currency",       "value": "INR",                                    "confidence": 1.0,  "source": "page 1 (render image)" },
    { "name": "taxable_amount", "value": "5500.00",                                "confidence": 0.96, "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.78, 0.71, 0.88, 0.73] }, "channel": "both", "agree": true },
    { "name": "tax_amount",     "value": "990.00",                                 "confidence": 0.96, "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.78, 0.74, 0.88, 0.76] }, "channel": "both", "agree": true },
    { "name": "total",          "value": "AMT-03",                                "confidence": 0.97, "source": "page 1 (render image)", "source_region": { "page": 1, "bbox": [0.78, 0.77, 0.88, 0.79] }, "channel": "both", "agree": true },
    { "name": "tax_breakdown",  "value": { "CGST": "495.00", "SGST": "495.00", "IGST": null },
                                 "confidence": 0.9,  "source": "page 1 (render image)" },
    { "name": "billed_to",      "value": "BUYER-B",           "confidence": 1.0,  "source": "page 1 (render image)", "channel": "vision" },
    { "name": "refs",           "value": { "mbl": "SNLFSKDLMF00139", "hbl": "SZOE25040707", "job": null, "po": null },
                                 "confidence": 0.95, "source": "page 1 (render image)" }
  ],
  "line_items": [
    { "description": "AGENCY CHARGES (IN BOND)", "hsn_sac": "996712", "amount": "3000.00",
      "confidence": 0.97, "source": "page 1, line 40" }
  ],
  "validation": {
    "arithmetic_ok": true,
    "issues": []
  }
}
```

Field list is a **baseline, not law** — the owner may add/remove fields. Line items
matter for reconciliation and for future broker rules, so they are included
(owner decision).

Files whose quality gate excluded pages (D16) carry a `document_quality` review
flag (reason included, e.g. "illegible/blank pages excluded") lifted into
`<run>/flags.csv` at Stage 6; the per-page exclusion record lives in
`<run>/raw/<file>/report.json`.

---

## 8. Verification Strategy

- **Test corpus:** the 12 PDFs in `invoices/` are the permanent regression set
  (renamed to lowercase broker labels 2026-08-09 — labels are human-readable
  only, D18).
- **Ground truth:** fields verified in the earlier manual extraction (invoice no, dates,
  job numbers, amounts, GSTIN/PAN per file), kept in `tests/ground_truth.json`
  (100% accurate, owner statement 2026-08-03; the `file` column is a bookkeeping
  join key, content-verified — D18).
  The "digital" `classic_3/4` (old DOC-CLF-series_*) actually carry full-page images
  and are OCR'd per D15 (see Section 2 note) — they no longer serve as a
  "must skip OCR" check.
- **Acceptance thresholds:** ≥98% field-level accuracy on the corpus; 100% correct
  broker selection (or explicit flag — never a wrong guess); review flags only on
  genuinely ambiguous fields; re-runs produce no duplicates.
- **Arithmetic spot-checks:** taxable + tax = total for every corpus file
  (note the AMT-02a vs AMT-02b rounding case in `DOC-CLF-02`).
- **Quality gate (D16):** 15 pages excluded across DOC-D-01/56/69 (p6–10 × 3);
  zero false positives on known-legit sparse pages (OLD-EXP-03 p12, BUYER
  tracking/template pages). Known OCR case the GSTIN regex must catch:
  `DOC-CLF-series` OCR'd `GSTIN-02` (Cyrillic С) — deferred here (see D15 note).

---

## 9. Out of Scope (do not build unless asked)

- Email/mailbox polling and attachment extraction
- Web UI or database sink
- Multi-user workflows
- Cloud OCR (Azure/Textract/Google DI) — rejected for privacy + cost
- Regex/template-based extraction — explicitly rejected (D5)
- Local LLM inference — rejected; owner chose the opencode Zen gateway (D4)

---

## 10. Config Reference

| File | Contents | Editable by |
|---|---|---|
| `rules/brokers.yaml` | Broker identities + selector weights | Owner, any time |
| `config/thresholds.yaml` | Per-field confidence thresholds + `quality:` page-gate floors (D16) | Owner |
| `.env` | `OPENCODE_API_KEY=...` | Owner (secrets, never committed) |
| `config/models.yaml` | Bulk + escalation model IDs, `escalation_mode` (text/vision, D6 amendment 2026-08-10), Zen gateway endpoint (D4) | Developers/owner |

