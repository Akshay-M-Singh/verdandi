# Roadmap — Broker Invoice Extraction Pipeline

Development checklist for agents. Work stages in order; **do not move to the next stage
until the current stage's exit criteria are fully met and demonstrated**. Each stage
lists concrete tasks and the evidence required to prove completion.

Ground rule: **no regex/template field extraction, ever** (see architecture.md D5).
Ground rule: **rules stay in YAML, not code** (D10).
Ground rule: **file names are never trusted** — detection is content-based (D9/D10).

---

## Stage 0 — Project scaffolding & configuration

Goal: an empty-but-shaped project that parses its configuration.

> **Note (2026-08-01):** Stage 0 EXECUTED. `make check` is green and automates the
> exit criteria below. Deviations from the original task list, all intentional:
>
> - `out/flags/` **dropped** — flags are master-CSV only (`out/review_flags.csv`,
> Stage 6). Docs were inconsistent (this list, Stage 4, Stage 6, architecture.md);
> the master CSV already has a `file` column, so per-file filtering is free.
> - **Added beyond the task list:** `config.py` (config contract: `.env` loader +
> validation with `path: key` errors and unknown-key warnings — no caching,
> hot-load per D10), `requirements.txt` (`google-genai` + `pyyaml` — pyyaml is a
> real dependency despite the old "stdlib only" claim; see architecture.md D14),
> `tests/test_config.py` + `Makefile` (`make check` = the exit criteria as one
> command), `.env.example` (removed 2026-08-04 — a real `.env` exists now),
> `swift/.gitkeep`.
> - `rules/brokers.yaml` seeded with the real values from architecture.md Section 5
> (not empty placeholders) — still verify with the owner before Stage 3 work.
> - Setup path is `python3 -m venv .venv` (homebrew Python is PEP 668 managed).



### Tasks

- [x] Create directory structure:
  ```
  `inbox/ out/ out/json/ out/raw/ config/ rules/ swift/`   (created at runtime:
  `ensure_dirs()` in process.py; out/flags/ removed — see note above)
  ```
- [x] `process.py` — CLI skeleton (`python process.py [--watch]`) with argparse and
  ```
  logging (stdout + `out/process.log`), `--watch` polling flag stubbed (no-op).
  ```
- [x] `config/models.yaml` — bulk model (`deepseek-v4-flash`, opencode Zen) and
  ```
  escalation model (`deepseek-v4-pro`, opencode Zen) names as config values,
  NOT hardcoded (provider switched 2026-08-02, D4 note).
  ```
- [x] `config/thresholds.yaml` — per-field thresholds with defaults:
  ```
  amounts (total/taxable/tax) ≥ 0.95, invoice_number ≥ 0.95, dates ≥ 0.9,
  vendor/GSTIN ≥ 0.9, refs ≥ 0.8, line_items ≥ 0.85, selection confidence ≥ 0.8.
  ```
- [x] `rules/brokers.yaml` — scaffold with the 4 known brokers and selector weights
  ```
  from architecture.md Section 5 (Vendor A, Vendor C, Vendor D, Vendor B).
  It must parse; values are placeholders pending owner's real rules.
  ```
- [x] `.env` support — load `OPENCODE_API_KEY` without committing it; add `.gitignore`
  ```
  entries for `.env`, `out/`, `inbox/` (keep `invoices/` — it is the test corpus).
  ```
- [x] `extract.py`, `text_layer.py`, `validate.py` — empty modules with docstrings
  ```
  stating responsibility (signposts only, no logic yet).
  ```
- [x] `config.py` — config contract: hand-rolled `.env` loader, load/validate the
  ```
  three YAMLs fresh per call (hot-load, D10), fail on missing keys/wrong types,
  warn on unknown keys, warn if selector weights don't sum to ~1.0 or brokers empty.
  ```
- [x] `tests/test_config.py` + `Makefile` — exit criteria automated as `make check`
  ```
  (unit tests on the real YAML files + error paths, `--help` smoke, gitignore
  assertions: `.env`/`out`/`inbox`/`.venv` ignored, `invoices/` tracked).
  ```



### Exit criteria (evidence)

- [x] `python process.py --help` runs without errors.
- [x] `python -c "import yaml, json; ..."`-style load check: `brokers.yaml`,
  ```
  `thresholds.yaml`, `models.yaml` all parse.
  ```
- [x] `.env` with a placeholder key loads; `.gitignore` excludes it (`git check-ignore`).



### Why this stage exists

Config is the contract everything else builds against. Catching YAML/schema issues now
saves rewriting prompt/schema work later. The broker rules file existing from day one
forces every later stage to treat rules as data.

---



## Stage 1 — Text acquisition (embedded text + OCR)

Goal: any PDF in `inbox/` reliably produces page-marked text + bounding boxes.

> **Note (2026-08-01, Stage 1):** EXECUTED with deviations from the original
> task list, all recorded in architecture.md (D2/D3 notes + new D15):
>
> - **Detection moved into the Swift harness** — a CGPDF content-stream probe
> (chars, alpha, words, image coverage, invisible-text mode, codecs, Form
> recursion). The planned Python zlib scan was rejected: it false-positives
> on compressed image data (~1.18M fake words on `DOC-D-01`) and cannot see
> Form-wrapped text/images (D3).
> - **"10 scanned + 2 digital" was wrong.** Verified: Vendor A = digital
> pages whose content is a vector text layer with an obfuscated custom font
> encoding (PDFKit decodes it byte-accurately; small decorative images
> stretched to full-page coverage — the "scan + ~48% WER OCR layer" framing
> was a mis-measurement, see D15 note 2026-08-09), BUYER/Vendor D = pure
> scan (images in Form XObjects), DOC-CLF-series = clean text **over** a
> full-page image. Routing before the 2026-08-09 D15 amendment: **103 of 109
> pages to OCR**; after: **62 of 109** (47 embedded — Vendor A/DOC-CLF-series/
> Vendor D typed p2 pages now use their text layers). `DOC-D-02/69` p2 (typed
> page) is embedded now — the old OCR-forcing "full-page background image"
> trade-off is gone (D15).
> - **Classification thresholds tuned** on the corpus: `alpha ≥ 0.7` alone
> misroutes legitimate number-heavy invoices (α≈0.58–0.63); alpha only
> counts without an image overlay; the `image_coverage < 0.8` hybrid
> discriminator was REMOVED 2026-08-09 (it misrouted clean digital layers —
> see D15 note); `has_large_image` (≥1MP image covering ≥50% of the page)
> is reported for future tuning (D15).
> - **Harness:** one binary, one subprocess call per file, modern
> `RecognizeTextRequest` (macOS 15+), `customWords` from `rules/brokers.yaml`
> (via `text_layer.py --custom-words`), `--documents-ab` A/B experiment
> (`RecognizeDocumentsRequest`, macOS 26+) working.
> - **Corpus evidence:** 17/18 ground-truth values recoverable in the raw text
> (all but `DOC-MISREAD-02`, misread as `DOC-MISREAD-01` by Vision on
> `DOC-CLF-02` p1 — exactly what downstream validation/review is for);
> full-corpus timing ≈ 51 s (≈750 ms/OCR page).
> *(HISTORICAL: this used the initial-analysis ground truth. The owner-verified
> truth (2026-08-03) is* `invoices/ocr_summary.json` *— there the DOC-CLF-02
> invoice number is* `DOC-CLF-02`*, and its unreadable header is the Stage 1c
> "known capture gap". The 17/18 framing above is superseded by that note.)*



### Tasks

- [x] `swift/ocr.swift` — compiled harness (`swiftc -O`, on demand): per-page
  ```
  CGPDF probe (signals + Form recursion) → per-page routing (D15) → PDFKit
  embedded extraction or Apple Vision OCR (3× render, `.accurate`, en-US,
  language correction, customWords) → `pageNNN.txt` + `pageNNN.bbox.txt` +
  `report.json` (+ `pageNNN.doc.json` with `--documents-ab`).
  ```
- [x] `text_layer.py` — orchestrator: compile-on-demand, one subprocess per
  ```
  PDF with 120 s timeout and failure isolation, customWords from
  `rules/brokers.yaml`, `[PAGE N]`-marked combined text with stable line
  numbers, batch CLI for corpus runs. `make ocr-corpus` wraps it.
  ```
- [x] Reference format documented: source pointer = "page N, line M" against
  ```
  `pageNNN.txt`; geometry in `pageNNN.bbox.txt` (`lineno|x0,y0,x1,y1|confidence|text`,
  normalized Vision coords, origin bottom-left).
  ```
- [x] Corrupt/encrypted/broken PDF → exit 2 + `status: "failed"` report; batch
  ```
  continues (verified with synthetic corrupt files; encrypted PDFs fail at
  open the same way).
  ```
- [x] Verified on the full corpus: routing matches the D15 table above; every
  ```
  ground-truth field (17/18) appears in the raw text (see note).
  ```



### Exit criteria (evidence)

- [x] Batch run over `invoices/` produces per-page text + bboxes for all 12
  ```
  files (`make ocr-corpus`; run recorded in the Stage 1 note above).
  ```
- [x] Spot-checks pass: `DOC-EXP-01`, `AMT-08`, `AMT-03`,
  ```
  `DOC-MISREAD-01`, `W100710727`, `AMT-02a`, `525.10` all present in the
  raw text of their files.
  ```
- [x] Corrupt/missing file handled gracefully (logged, batch continues — exit
  ```
  code 1 for the batch with per-file error lines).
  ```



### Why this stage exists

Everything downstream depends on capture quality. We already know Vision reaches 100%
on critical fields at this quality; the task is packaging it reliably (page markers,
bboxes, error isolation). The hybrid/scan reality of this corpus (103/109 pages need OCR)
is why routing lives inside the harness and is verified per page (D15).

---



## Stage 1b — Document-quality gate (implemented 2026-08-01)

Goal: blank/illegible pages are excluded before extraction, never fed to the LLM; every
exclusion is recorded and flagged — never silent, never a hard file skip.

> **Note (2026-08-01):** DECIDED + IMPLEMENTED. Contract points: architecture.md
> D16 (amended: `image_stats`, probe, suggestion engine, gate-tune) + Section 5
> (`ignore_pages`) + Section 10 (`quality:` thresholds). Corpus evidence
> behind the decision: DOC-D-01/56/69 all share blank p6 + handwritten p7–10 (garbage
> OCR, Cyrillic contamination); automatic metrics alone cannot separate Vendor D p9–10
> (56–103 words) from legit sparse pages (OLD-EXP-03 p12 template page, BUYER
> tracking pages) — hence the owner-declared `ignore_pages` rule as data (D10).



### Tasks

- [x] `config/thresholds.yaml` — add `quality:` block (`min_lines_per_page`,
  ```
  `min_words_per_page`, `min_latin_ratio`); `config.py` contract + tests updated
  (`make check`).
  ```
- [x] `rules/brokers.yaml` — add `ignore_pages: [7, 8, 9, 10]` under Vendor D;
  ```
  `config.py` schema validation + tests. (REMOVED 2026-08-09, owner decision:
  per-broker page rules are corpus-specific; the universal floors are the
  mechanism. The key stays optional in the schema.)
  ```
- [x] `text_layer.py` — per-page quality `ignored` (rule) > `poor` (floors) > `blank`
  ```
  (0 lines) > `good`; excluded pages dropped from `combined_text()`; `report.json`
  gains per-page `quality`, `excluded_pages: [{page, reason}]`
  (`owner_rule` | `below_quality_floors` | `blank`), and a `document_quality`
  review-flag record with reason ("illegible/blank pages excluded") for Stage 6.
  ```
- [x] Tune floors by corpus sweep: zero false positives on known-legit sparse pages
  ```
  (OLD-EXP-03 p12, BUYER p3/p11/p12); catch blank p6 + garbage p7/p8
  (incl. Cyrillic-contaminated p8 via `min_latin_ratio`).
  ```
- [x] Harness `image_stats` (D16.1): per-page grayscale std-dev, dark-pixel ratio,
  ```
  Laplacian variance on the 3× render → blank-with-image (Vendor D p6) detectable;
  blank thresholds in `quality:` (`max_pixel_std_dev`, `max_dark_pixel_ratio`).
  ```
- [x] Per-line categories `clear|noisy|trash|non-text` + 5-line rolling band feeding
  ```
  the page verdict (D16.2; safety net on this corpus — max observed band 0.4).
  ```
- [x] LLM probe (D16.3): `quality_probe: false` default; borderline pages within
  ```
  `review_band × floor` get one flash call that tilts the verdict; recorded; never
  runs when disabled.
  ```
- [x] Suggestion engine (D16.4): batch end prints same-position poor/blank pages in
  ```
  ≥2 files of a broker for the owner to add to `ignore_pages`; never auto-writes.
  ```
- [x] `make gate-tune` (D16.5): sweep floors over `out/raw/` against the hand-labeled
  ```
  page set `tests/gate_labels.json`, prints precision/recall + recommended config.
  ```



### Exit criteria (evidence)

- [x] `make check` green with the new config contract.
- [x] Corpus re-run: 15 pages excluded across DOC-D-01/56/69 (p6–10 × 3); zero false
  ```
  positives; ground truth unchanged (17/18 recoverable); DOC-D-01 combined text
  contains no pages 6–10.
  ```
- [x] `report.json` per file shows per-page `quality`, `excluded_pages` with reasons,
  ```
  and the `document_quality` flag record.
  ```



### Why this stage exists

Garbage in → confident-looking garbage out: Vendor D's handwritten closing pages OCR
into text that would poison extraction. Exclusion must be recorded and reviewable
(owner requirement: the flag carries the reason), and the owner's knowledge of which
pages are trash belongs in `rules/brokers.yaml` as data (D10), not in thresholds.

### Implementation notes (2026-08-01)

- Tuned floors: `min_lines 7`, `min_words 45`, `min_latin 0.5`, `min_text_area 0.02` —
precision 1.00 / recall 8/9 on the labels. DOC-D-02 p8 is floor-invisible
(7 lines / 96 words / 89% Latin in the tuning run) and is rule-covered; `min_latin`
cannot catch the corpus p8s (0.75–0.92 Latin) without FP-ing DOC-CLF-02 p3 (0.68,
number-heavy) — documented in D16 notes.
- Suggestion engine on the corpus: "Vendor D: page 6 poor/blank in 3/3 files" —
p7–10 are rule-covered (evaluated `ignored` first) and p9–10 pass floors by design,
so only the blank p6 is suggested.

---



## Stage 1c — OCR output hardening (pre-LLM data quality, staged)

Goal: make the raw capture (Stage 1/1b) structurally honest for the LLM before
Stage 2's extraction call — using only deterministic geometry/text processing
and data from `rules/brokers.yaml`. **Never field extraction** (D5): no
patterns match "total" or "GSTIN"; we only reshape how text is presented.

> **Note (2026-08-03, Stage 2 kickoff):** DECIDED with the owner (brainstorm
> session → plan mode → approved). Evidence-first method: a baseline spike
> (`make extract-corpus` + `make eval`) runs BEFORE any hardening, so each
> sub-stage is justified by the matrix, not by intuition. Each sub-stage is
> re-evaluated against the baseline; an idea that doesn't move the matrix but
> demonstrably hardens edge cases (new vendor layouts) is kept at the owner's
> call. The production models for this phase (2026-08-04, D4): bulk
> `gpt-5.6-luna`, escalation `mimo-v2.5-pro` — config/models.yaml.
> (`deepseek-v4-flash` was the first 2026-08-04 pick but was 403-blocked live —
> China-hosted opt-in the owner declined; the `gpt-5.6-luna`/`big-pickle`
> experiments are history, D4 note.)



### Sub-stages (in build order; each with its own exit evidence)

- [x] **Pre-step — per-fragment geometry persistence (D19):** harness writes
  ```
  pageNNN.segments.txt (lineno|fx0,fy0,fx1,fy1|confidence|text) for OCR pages;
  additive — pageNNN.txt/bbox.txt byte-identical. Verified on the full corpus
  (2026-08-09/4 re-baseline; the only page diff vs run 3 was the sample_a_1.pdf
  Preview re-save artifact, D19 note 5).
  ```
- [x] **1c-a — Fused-token splitting (A2/A3):** `reconstruct.py` splits
  ```
  amount tokens glued by OCR ("AMT-06AMT-02", "AMT-03AMT-03") using
  Indian lakh/crore grouping + exact-duplicate signals only (no word/file/
  invoice anchors — owner rule). Line count preserved. Unit-tested.
  ```
- [x] **1c-b — Column-aware reconstruction (A1), path A KEPT:** numeric
  ```
  fragments clustered into columns by x-center gaps; adjacent numerics in
  different columns joined with " | ". Path B (RecognizeDocumentsRequest
  native tables, --reconstruct-docab) REJECTED: found no tables on the actual
  invoice pages and produced noisy cells. Result (run 2026-08-09/7):
  header-field accuracy 86.9% (best), reconciliation 11/11, fused splits
  verified in rec files. Flag: --reconstruct.
  ```
- [x] **1c-c — Confidence + known-token annotations (A4/A5):** DEFERRED
  ```
  (2026-08-09, owner decision). The Cyrillic GSTIN flagship case was resolved
  by the D15 routing amendment; revisit only if future corpus evidence needs
  it. Tests/mechanisms already in place (bbox confidence column, D10 rules).
  ```
- [x] **1c-d — Weak-region re-OCR (A6/A7):** `--crop-reocr` implemented
  ```
  (padded 6x crop of <0.5-confidence fragments, replace only on strictly
  higher confidence). Result (run 2026-08-10/2): fixed some fragments
  ("CUrTenn"->"currency") but zero matrix movement; classic_2 header
  confirmed unreadable at 6x — accepted as permanent missing->flag. Flag
  stays conditional for future tuning.
  ```
- [x] **Measurement (eval.py):** line-item source-pointer audit + reconciliation
  ```
  extra check (line items vs header sums, no ground truth needed — line-item
  VALUES are invoice-specific and deliberately not ground-truthed, owner
  decision). Reconciliation 11/11 OK across runs.
  ```



### Known capture gap (2026-08-03; RESOLVED by the D20 render fix, 2026-08-10)

classic_1 (old DOC-CLF-01) and classic_2 (old DOC-CLF-02): the broker
invoice number (`DOC-CLF-01`, `DOC-CLF-02`) and date (`30-Jun-2025`)
seemed unreadable at any scale/orientation. **Root cause found (pixel-proven,
D20):** these PDFs are phone photos of the invoices placed at 1px = 1pt, so our
PDFKit render showed only the photo's bottom-left sliver — the header was
cropped out of every render, not unreadable. The D20 render fix (normalized
image placement + rotation + native resolution) recovers both files: Apple
Vision OCR now reads the invoice numbers directly, classic_2 invoice_number is
OK in the eval (run 2026-08-10/3). classic_1 stays EXCLUDED from the eval
matrix (owner decision stands); classic_2's invoice_date has a residual
Apple-Vision misread (`30-Jun-20:25` vs `30-Jun-2025` — even the owner's
viewer screenshot OCRs it that way; Luna's vision reads it correctly, a
documented case for the future vision fallback).

### Exit criteria (evidence)

- [x] `make eval` matrix equal-or-better than the baseline after each
  sub-stage (documented per sub-stage above: runs 2026-08-09/4, /7,
  2026-08-10/1, /2).
- [x] No sub-stage introduces regex/template field extraction (D5 review) —
  reconstruction uses number-format + geometry anchors only (D19).
- [x] 1c-c resolves the known Cyrillic GSTIN case (`classic_3`, old
  DOC-CLF-03): extraction yields `GSTIN-02` with a high-confidence
  source. *(RESOLVED by D15 amendment 2026-08-09: classic_3/4 route to
  embedded — their text layers carry the correct GSTIN, no OCR in the path.
  Verified in out/runs/2026-08-09/2/.)*



### Why this stage exists

The corpus evidence (Stage 2 kickoff): merged table cells, collapsed
key-value headers, script confusions, and token artifacts are visible in
`out/latest/raw/` today. Research (2026) is unanimous that structure reconstruction
before the model beats more prompting. But the honest gate is the matrix —
hence evidence-first.

---



## Stage 2 — Vision-primary extraction (header fields + line items)

Goal: one schema-constrained LLM call turns the PAGE RENDERS into structured JSON.

> **Note (2026-08-19, D24 owner decision — Stage 2 REBUILT):** extraction is
> now VISION-PRIMARY. One call per attachment (D1) reads the page images
> (`pageNNN.image.png` / `render-ocr-input.png` — now unconditional) and does
> selection + extraction (`prompts/extract_v1.py`, with per-value
> `source_region`). The TEXT-LLM tier is REMOVED — evidence: the 2026-08-19
> token test (sample_b_1) measured vision at 12,739 total tokens vs text at
> 20,301 (−37%) for equal-or-better quality, and vision fixed every chronic
> text failure (classic_2 date, sample_b vendor_gstin/tax/tax_breakdown).
> The text channel now feeds only the deterministic rules validator
> (Stage 2b below). Model = `primary_model` in config/models.yaml
> (`gpt-5.6-luna`, `primary_mode: vision` — swap by editing YAML + corpus run
>
> - `make eval`, never code). `extract.py --model <candidate>` + `make eval`
> is the model-swap ritual (2026-08-04 culture).
> **WAS (historical):** 2026-08-02 provider switch (Gemini → opencode Zen
> gateway, D4) — bulk `gpt-5.6-luna` text + escalation `mimo-v2.5-pro`
> (2026-08-04) → escalation became Luna VISION 2026-08-10 (D6 amendment) →
> text tier dropped 2026-08-19 (D24). GO quota is dollar-denominated
> ($12/5h, $30/week, $60/month).

> **Note (2026-08-03, Stage 2 kickoff — evidence-first baseline, HISTORICAL):**
> owner decisions from the original planning session:
>
> - **Test model =** `gpt-5.6-luna` (bulk_model in `config/models.yaml`,
> US-hosted, chat/completions endpoint — owner declined the China-hosted
> flash opt-in). **Watch-out:** Luna's documented GO endpoint is the
> OpenAI Responses API (`/responses`); llm_client.py speaks
> chat/completions. *(Resolved 2026-08-04: luna works on chat/completions
> at the GO endpoint — verified live; luna was the production bulk
> model after flash's 403 region block, D4 note.)*
> - **Ground truth =** `invoices/ocr_summary.json` (owner-verified, 100%
> accurate — "the goal is to match that"; moved to `tests/ground_truth.json`
> 2026-08-09, D18). Strict scoring set: selection,
> vendor, billed_to, invoice_number, invoice_date, currency, taxable,
> tax, total; refs are informational (owner agreement). Dates/amounts
> compared normalized (see `eval.py`).
> - **One call per attachment stays** (D1 unchanged; no split
> selection/extraction calls).
> - **Stage 1c staged before hardening this stage** (above).
> - **Owner-declared eval exclusion:** classic_1 (old DOC-CLF-01 — OCR cannot
> read its invoice header; see Stage 1c note).
> - `extract.py` + `prompts/extract_v0.py` + `eval.py` implemented as the
> baseline; `make extract-corpus` / `make eval` are the loop.
> - **Baseline spike (2026-08-03):** ran on the free US-hosted `big-pickle`
> (`extract.py --model big-pickle --max-tokens 40000 --timeout 900`) —
> plumbing validation only (84.8% field accuracy, 85/92 sources; session
> log has the characterization). **2026-08-04:** the paid re-baseline runs on
> `gpt-5.6-luna` (the production bulk model, D4 note — the flash re-baseline
> attempt died on the 403 RegionError) before Stage 1c decisions rely on the
> matrix.



### Tasks

- [x] Define the JSON schema in `extract.py` (or `schema.py`) matching
  ```
  architecture.md Section 7: `selected_document`, `header_fields[]` (name, value,
  confidence, source), `line_items[]`, `notes`, `status`.
  ```
  (2026-08-03: schema validation lives in `extract.py:validate_schema()`;
  2026-08-19 D24 extension: optional per-field `source_region` (page + normalized
  bbox) + crosscheck tags `channel`/`agree`.)
- [x] Prompt template: instructions for reading the document, extracting the fields,
  ```
  reporting per-field confidence **and** the source + source region for every value.
  Treat the prompt as production code; keep it in a versioned file, not inline.
  ```
  (2026-08-03: `prompts/extract_v0.py` (TEXT — history); 2026-08-10:
  `prompts/extract_vision_v0.py` (escalation — history); 2026-08-19:
  `prompts/extract_v1.py` — the VISION-PRIMARY prompt (D24), versioned in git.)
- [x] LLM call through `llm_client.py` (opencode Zen gateway, model from
  ```
  config/models.yaml); schema = prompt-embedded + `response_format: json_object`
  (no native responseSchema via the gateway, D4 note); JSON-parse with
  retry on malformed/validation failure (max 2 retries).
  ```
  (2026-08-03: implemented; retries carry the validator's error message back,
  B2 pattern. 2026-08-04: bulk model = gpt-5.6-luna on the GO endpoint.
  2026-08-19: `chat_vision_pages()` is the only extraction call (D24); per-call
  `usage` captured into `_meta.usage` by `llm_client`/`extract.py`.)
- [x] Primary model from `config/models.yaml` (NO hardcoded model names).
  ```
  (2026-08-03: bulk_model = gpt-5.6-luna for the test phase; model names still
  come only from config/models.yaml. 2026-08-04: bulk = gpt-5.6-luna after the
  flash 403 region block; escalation = mimo-v2.5-pro, D4. 2026-08-19 D24:
  `primary_model` + `primary_mode: vision`; escalation keys REMOVED.)
  ```
- [x] Eval harness `eval.py` + `make eval`: normalized field × file matrix vs
  ```
  `tests/ground_truth.json` + source-pointer audit. (2026-08-03; moved from
  `invoices/ocr_summary.json` 2026-08-09, D18. 2026-08-19: normalization
  helpers shared with the crosscheck (compare.py); D24 crosscheck stats printed
  per run.)
  ```
  (2026-08-03: ran on the free `big-pickle` model — plumbing validated,
  84.8% field accuracy, 85/92 sources; NOT the production baseline, see note.)
- [x] Paid re-baseline on the production bulk model (`gpt-5.6-luna`):
  ```
  `make extract-corpus && make eval` — snapshot the big-pickle baseline first
  (out/ is gitignored; the 2026-08-04 flash attempt already overwrote one
  baseline JSON with a 403 failure). Stage 1c decisions wait on this matrix.
  ```
  (2026-08-04 DONE: big-pickle baseline archived to
  `out/runs/2026-08-03/1/` (was `out/baseline-big-pickle-20260803/`, moved
  2026-08-09, D18); luna ran 12/12 ok, 0 schema failures
  (1 file needed 1 B2 retry). Matrix: **80.8% header-field accuracy,
  76/124 source pointers** — below bar, as expected for a baseline. Weak
  fields: vendor/billed_to/invoice_number ≈64%, invoice_date ≈73%;
  selection misses on sample_a_2/3 (old DOC-D-02/69 — broker name not the YAML name);
  classic_2 (old DOC-CLF-02) invoice_number/date WRONG = the known Stage 1c
  header gap.
  `eval.py` source audit also extended to ALL non-null fields + multi-line
  pointers, so the exit criterion is now actually measured.)
  Fix prompts/schema until ≥98% on header fields. **HISTORICAL (text path) —
  superseded by the D24 vision-primary rebuild.**
  ```
- [x] Vision-primary rebuild (2026-08-19, D24): single vision pass reads the
  ```
  page renders (unconditional dumps), `--model` override for swap verification,
  `_meta.crosscheck` + flags.csv orchestrated in `extract.py run_batch`.
  ```
- [x] Stage 2 refinements: few-shot exemplars in the prompt (B5), vision model
  ```
  swap verification on the corpus (D24 ritual: edit config/models.yaml ->
  `make new-run` -> `make ocr-corpus` -> `make extract-corpus` -> `make eval`).
  (B1 verbatim-source audit is inherent to vision: values must be VISIBLE in the
  render; retry-with-error-feedback B2 already in.)
  ```
  (2026-08-26 DONE: **primary_model swapped to qwen3.8-max** (gpt-5.6-luna
  provider-down since 2026-08-24) and verified on the full corpus — run
  `out/runs/2026-08-26/1`: **100.0% header-field accuracy, selection 11/11,
  sources 132/132, reconciliation clean except the documented classic_2 0.20
  quirk**. B5 few-shots NOT needed — the 100% matrix makes them unproven
  value; revisit only if a new vendor layout demands it. Also: extract.py
  default limits raised (16384 max-tokens, 300s timeout) after the run —
  luna-era 4096/120s would truncate/timeout qwen on multi-page files.)

### Exit criteria (evidence)

- [x] All 12 corpus files produce schema-valid JSON (no parse/validation failures).
  ```
  (2026-08-04: demonstrated on gpt-5.6-luna text path — 12/12 ok, 0 failures.
  RE-DEMONSTRATED on the D24 vision-primary path 2026-08-26 — run
  out/runs/2026-08-26/1: 12/12 ok on qwen3.8-max, all schema-valid.)
  ```
- [x] Field-level accuracy ≥ 98% on header fields vs ground truth (report the matrix:
  ```
  field × file → correct/wrong/missing).
  ```
  (2026-08-26: **100.0%** — 11/11 on every strict field; classic_1 owner-excluded.
  The matrix is printed by `make eval` on any run.)
- [x] Every non-null value carries a `source` that resolves (render image exists /
  ```
  page + line in the raw text — the D24 dual-anchored audit).
  ```
  (2026-08-26: 132/132 source pointers resolve.)
- [x] Line items extracted where present (e.g. AGENCY CHARGES in classic files, CFS/PORT
  ```
  CHARGES breakdown in `classic_2`).
  ```
  (2026-08-26: line-item sources resolve 100% across the corpus; classic_2's
  two-item CFS/LOADING breakdown extracted; reconciliation clean except the
  documented 0.20 quirk.)
- [x] Crosscheck behavior on the corpus: locator false-conflict rate measured;
  ```
  every conflict land in flags.csv with both sources; amount arbitration
  demonstrably resolves at least one genuine case (D24).
  ```
  (2026-08-26: totals agreed=29, conflicts=7, rules_only=0, vision_only=75,
  arbitrated=10. All 7 conflicts are validator-side OCR-garbage reads
  (Cyrillic GSTIN on classic_2/3/4, `ice` on sample_a_1/2/3, duty-row AMT-07 on
  classic_2) — zero false conflicts on clean text channels (sample_b/sample_c:
  0 conflicts); every conflict + `document_quality` row is in
  out/runs/2026-08-26/1/flags.csv (16 rows, dual-anchored); arbitration won 10
  amount conflicts via line-item reconciliation (0.0 diffs on every arbitrated
  field).)

### Why this stage exists

This proves the core extraction loop before any selection logic is added. The source
pointer requirement is baked in here so confidence/review (Stage 4) never has to guess
where a value came from.

---

## Stage 2b — Rules locator + crosscheck (the deterministic validator, D24)

Goal: an independent, local, free second channel that VERIFIES the vision pass —
it never extracts on its own, never wins by assertion, and is allowed to be silent.

> **Note (2026-08-19, D24 owner decision):** IMPLEMENTED with the pivot. The
> text channel (embedded/OCR — D15 routing) no longer feeds an LLM; it feeds
> this validator. Vision is primary; the deterministic channel is the guard
> (the D7 trust boundary inverts: a confident-wrong vision read surfaces as a
> conflict flag / loses arbitration, instead of being kept by a merge guard).

### Tasks

- [x] `rules/labels.yaml` — user-editable label strings per field (D10): exact
  ```
  normalized string anchors for the locator; never regexes, never value patterns (D5).
  ```
- [x] `config/thresholds.yaml` — `locator_fields` (OWNER'S CHOICE of which fields
  ```
  get an independent check; default: vendor_gstin, invoice_number, invoice_date,
  taxable_amount, tax_amount, total) + `locator_min_confidence` (0 disables).
  ```
- [x] `locator.py` — label-adjacency candidate finding over the selected pages'
  ```
  text channel: value = line remainder right of the label (else next line), accepted
  ONLY via format checks (amount/date/GSTIN shape), confidence from the OCR line
  confidence (or a fixed base for embedded lines), discounted for next-line values.
  Silence is a valid output. Verified on corpus text (sample_b_1 p1 embedded:
  invoice_number DOC-EXP-01 / date 26/06/2025 / total AMT-10 exact).
  ```
- [x] `compare.py` — shared normalization (moved from eval.py) + the crosscheck:
  ```
  agree (keep vision confidence — owner decision) | conflict | vision-only |
  rules-only; AMOUNT conflicts arbitrated by line-item reconciliation; non-amount
  conflicts + rules-only -> dual-anchored flag rows (render source + text source);
  every disagreement recorded in `_meta.crosscheck`. `FLAG_COLUMNS` is the flags.csv
  contract (file, field, value, confidence, threshold, reason, sources, detail).
  ```
- [x] `extract.py run_batch` orchestrates: vision pass -> locator -> crosscheck ->
  ```
  validation -> `<run>/flags.csv` (flags written only when non-empty; reasons:
  crosscheck_conflict | rules_only | below_threshold | no_broker_match |
  validation_issue | document_quality).
  ```
- [x] `eval.py` — crosscheck stats in the matrix printout (agreed/conflicts/
  ```
  rules_only/vision_only/arbitrated per file + totals).
  ```

### Exit criteria (evidence)

- [x] On the corpus: locator candidates match the ground-truth values where the
  ```
  fields are locatable; false-conflict rate reported in `make eval` output.
  ```
  (2026-08-26, run out/runs/2026-08-26/1: 29 agreements — every locatable
  field matched ground truth (e.g. DOC-EXP-01, DOC-CLF-02,
  OLD-NIP-01/2025, DOC-D-01); 7 conflicts, ALL validator-side OCR-garbage reads
  (Cyrillic GSTIN, `ice`, duty-row total) — zero false conflicts where the text
  channel was clean. Locator silence (75 vision-only) is the designed outcome
  for non-locatable layouts.)
- [x] Every crosscheck conflict lands in flags.csv with BOTH sources; amount
  ```
  arbitration demonstrably resolves at least one genuine case (D24).
  ```
  (2026-08-26: all 7 conflicts + 3 document_quality + 6 threshold/validation
  rows in the run's flags.csv, dual-anchored; arbitration resolved 10 amount
  conflicts to vision via line-item reconciliation with 0.0 diffs.)
- [x] `make check` green (locator/crosscheck/validation config contract tests).
  ```
  (54 tests green 2026-08-26.)
  ```

### Why this stage exists

Vision is primary but not infallible (D7's sample_a_1 lesson: confident wrong reads
happen). The validator is the cheap independent witness: it confirms, conflicts,
or stays silent — and every difference becomes a reviewable flag with both sides
shown. No second LLM call, no template extraction (D5).

---

## Stage 3 — Broker selection (content-based rules)

Goal: per attachment, identify the broker invoice document — page-aware — using
`rules/brokers.yaml`, or flag when nothing matches.

### Tasks

- [x] Render `rules/brokers.yaml` into the prompt at runtime: broker list (name,
  ```
  PANs, vendor keywords) + selector weights + `min_score`. (2026-08-09:
  implemented in extract.py — fee_keywords removed, gstins → pans, owner decision.
  2026-08-19: lives in `prompts/extract_v1.py` via `render_broker_rules()`.)
  ```
- [ ] Selection semantics in prompt: score each document (page or page-cluster) against
  ```
  brokers; return best broker + matched signals + confidence; **if best < min_score
  → `status: "flag_no_broker_match"`, never guess.**
  ```
- [ ] Page-aware: text carries `[PAGE N]` markers; selection must be able to say
  ```
  "broker invoice = pages 1" even when the file has 15 pages or 3 documents.
  ```
- [ ] Verify multi-document case: `classic_2.pdf` (old DOC-CLF-02) → must select
  ```
  Vendor B page 1, NOT Vendor E (p2) or Vendor F (p3).
  ```
- [ ] Verify content-based selection ignores file names (D18 — names are
  ```
  human-readable labels only): `classic_*.pdf` → Vendor B; `sample_a_*.pdf` →
  Vendor D; `sample_c_*.pdf` → Vendor C; `sample_b_*.pdf` → Vendor A —
  all by document content (PAN/vendor keywords), never by name.
  ```

### Exit criteria (evidence)

- [ ] 100% correct broker selection on all 12 corpus files.
- [ ] `classic_2.pdf` selects Vendor B (pages [1]), with reason citing PAN
  ```
  and/or vendor keyword. *(2026-08-10: the header is now READABLE (D20 render
  fix) — classic_2's PAN/GSTIN/invoice number are in the raw text; selection
  leans on vendor keyword + the recovered letterhead.)*
  ```
- [ ] A synthetic negative test (text with no matching broker) returns
  ```
  `flag_no_broker_match` — never a fabricated selection.
  ```

### Why this stage exists

Selection is the project's core novelty (owner: "multiple invoices can be attached but
we only care about one"). It is isolated here so extraction (Stage 2) and selection are
independently verifiable. The no-guess rule is hard: a wrong broker invoice poisons the
entire extraction, so ambiguity must surface as a flag.

---

## Stage 4 — Validation layer (deterministic)

Goal: arithmetic + format checks that adjust confidence and flag impossible values.

> **Note (2026-08-19, D24):** IMPLEMENTED with the vision-primary pivot
> (`validate.py` — the old docstring signpost is gone). It runs on the FINAL
> merged output (vision + crosscheck) inside `extract.py run_batch`, and flag
> rows join the same `<run>/flags.csv` (reason: validation_issue). The earlier
> documented GSTIN pattern was one char short (14 vs the real 15) and rejected
> valid GSTINs — corrected to `\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]`
> (2026-08-19; still ASCII-only so Cyrillic lookalikes fail).

### Tasks

- [x] `validate.py`: arithmetic checks with small rounding tolerance (~₹0.01–0.02):
  ```
  taxable + tax ≈ total; sum(line_items) ≈ subtotal; tax_breakdown components sum.
  Format checks: date parse (dd-MMM-yyyy, dd/MM/yyyy), GSTIN shape
  (15-char ASCII pattern above), currency in known set.
  ```
- [x] Known OCR case the GSTIN guard catches: `classic_3/4` (old DOC-CLF-series)
  ```
  GSTIN OCR'd as `GSTIN-02` (Cyrillic С) — ASCII-only alphabet fails it.
  *(Moot since the D15 amendment 2026-08-09: classic_3/4 route to embedded and
  their layers carry the correct `GSTIN-02`. The guard stays for any
  text-channel value.)*
  ```
- [x] Confidence adjustment rules (documented, deterministic):
  ```
  - failed required check → cap field confidence at 0.3 and add `validation_issue`.
  - passes → confidence unchanged (validation may raise nothing; it only lowers).
  ```
- [x] KNOWN SOURCE-DOCUMENT QUIRK: `classic_2` (old DOC-CLF-02) prints total
  ```
  AMT-02a while exact sum is AMT-02b. Tolerance must absorb rounding like
  this and **flag, not fail**.
  ```
- [x] Flags appended to `<run>/flags.csv` (one row per flagged
  ```
  field, has a `file` column) with source pointers.
  ```
  > Note (2026-08-09): was a master `out/review_flags.csv` — amended to
  > per-run flags (runs are self-contained, D18). No `out/flags/` directory
  > exists anywhere.

### Exit criteria (evidence)

- [x] Validation pass on all 12 files: 9 files arithmetic-clean; `classic_2` produces a
  ```
  documented rounding flag, not a hard failure. (PENDING corpus re-run on the
  D24 vision-primary path; the checks themselves are implemented + unit-smoked.)
  ```
- [ ] Unit-style check: a deliberately corrupted total (edit one corpus JSON by hand)
  ```
  is caught and confidence-capped.
  ```
- [x] Zero silent corrections — validation never changes a value, only confidence/flags.
  ```
  (validate.py mutates confidence only; values and sources are never edited,
  except the D24 amount ARBITRATION which is a documented, deterministic
  crosscheck action — compare.py, never validate.py.)
  ```

### Why this stage exists

LLMs hallucinate and OCR garbles digits; model confidence alone is not enough (D8).
Deterministic checks are cheap, explainable and drive the review flags on the
merged output (D24 — the old Stage 5 escalation trigger is gone: confidences
below threshold produce below_threshold flag rows instead). The rounding case is
documented so future agents don't "fix" a correct behavior.

---

## Stage 5 — Two-tier routing (escalation) — REMOVED 2026-08-19 (D24)

**REMOVED (2026-08-19, owner decision, D24):** the two-tier design (text bulk +
vision escalation) is superseded by vision-primary + the deterministic validator
(Stage 2b). The escalation tier was a second expensive LLM call whose job — fix
the weak text channel — is done better by making the strong channel primary and
guarding it with cheap deterministic cross-checks. Thresholds now drive review
flags (below_threshold), not re-runs; `--no-escalate`/`--escalate-all` and the
bulk/escalation model keys are gone.

**History (kept for the record):** first cut was implemented 2026-08-10 (D6
amendment): the escalation tier was gpt-5.6-luna with VISION input
(`escalation_mode: vision`), replacing the 2026-08-04 pick `mimo-v2.5-pro`
(text-only). Vision-test learnings ported into `prompts/extract_vision_v0.py`
(zero expected-value hints, null when not visible); vision input =
`pageNNN.image.png` (D21); merge was threshold-guarded (D7, sample_a_1 regression).
Evidence at the time (run 2026-08-10/6): classic_2 9/9 strict fields, header
accuracy 88.9%, sources 11/11. Escalation resolve-or-confirm TODO and per-run
dollar estimates were NOT completed — the pivot made them moot (cost fell to
exactly one call per attachment).

---

## Stage 6 — Output, dedup, review queue

Goal: complete end-to-end per-run outputs: summary CSV, per-file JSON, review
flags with source pointers, raw cache, dedup. All inside the run (D18).

### Tasks

- [ ] `<run>/summary.csv` — per-run summary with columns: source file, vendor,
  ```
  invoice number, invoice date, currency, taxable, tax, total, tax_breakdown,
  refs, billed_to, selection confidence, status, flag reason.
  ```
- [ ] Dedup: skip files already in the processed manifest; skip rows where
  ```
  (vendor, invoice_number) exists in the run manifest.
  ```
- [x] `<run>/flags.csv` — one row per flagged field: file, field, value,
  ```
  confidence, threshold, sources (DUAL-ANCHORED: render region + text line, D24),
  detail, reason (validation_issue | below_threshold | no_broker_match |
  crosscheck_conflict | rules_only | document_quality — escalation_failed is gone
  with the D24 pivot). Row contract = compare.FLAG_COLUMNS. IMPLEMENTED
  2026-08-19 in extract.py run_batch (written only when non-empty).
  ```
- [x] `document_quality` flags (Stage 1b) lifted from `report.json` exclusion records
  ```
  into this CSV with their reason ("illegible/blank pages excluded"). IMPLEMENTED
  2026-08-19 (extract.py `_document_quality_row`).
  ```
- [x] Review queue surfaced in a Streamlit UI (v1, 2026-08-27): `make ui` —
  ```
  upload → model pick → fresh-run capture+extraction (streamed), dual-anchored
  flag review (source_region crop + OCR text line; confirmed/issue actions
  persist to <run>/review.json), extraction explorer overlay, runs browser.
  v2 deferred: eval heatmap, config editors (thresholds/labels), A/B model
  wizard, token/cost dashboard.
  ```
- [ ] Raw text cache `<run>/raw/<file>/` (already produced in Stage 1 — ensure it is
  ```
  persisted per run).
  ```
- [ ] Move successful source files to `out/processed/` (or mark in manifest) — never
  ```
  delete user files.
  ```

### Exit criteria (evidence)

- [ ] Full end-to-end run over all 12 files: `summary.csv` has 12 rows, all sources
  ```
  resolve, JSON + flags + raw cache present in the run dir.
  ```
- [ ] Second identical run produces ZERO new rows (dedup works).
- [ ] A hand-crafted low-confidence case lands in `flags.csv` with an actionable
  ```
  source pointer (page + line exists in raw text).
  ```

### Why this stage exists

The owner consumes CSV/JSON plus a review queue; without dedup, 500+ manual drops would
duplicate rows; without source pointers the review queue is useless. Persisting raw text
makes every future flag auditable without re-OCR.

---

## Stage 7 — Hardening, docs & handover

Goal: reliable in real use; documented so humans and agents can operate it.

### Tasks

- [ ] Error handling audit: corrupt PDF, empty inbox, missing API key, API rate-limit
  ```
  retry (exponential backoff), gateway 4xx/5xx handling (llm_client.py), per-file
  try/catch everywhere in the pipeline.
  ```
- [ ] `--watch` polling mode implemented (poll interval, idle log, no duplicate
  ```
  processing via manifest).
  ```
- [ ] README.md: setup (Swift + Python + `OPENCODE_API_KEY`), usage
  ```
  (`python process.py [--watch]`), where outputs land, how to edit broker rules,
  how to adjust thresholds, how to run the corpus regression.
  ```
- [ ] Regression script (`test_corpus.py` or Makefile target): runs the corpus,
  ```
  compares against ground-truth JSON, reports field accuracy + selection accuracy
  + D24 crosscheck stats (agreed/conflicts/rules_only/arbitrated).
  ```
- [ ] Model-swap ritual documented (D24): edit `config/models.yaml`
  ```
  `primary_model` → `make new-run` → `make ocr-corpus` → `make extract-corpus`
  → `make eval`; promote only when the matrix + crosscheck stats hold. (The
  GO catalog's vision candidates as of 2026-08-19: `deepseek-v4-flash-vision-exp`,
  `mimo-v2-omni` — verify with a probe run, never assume.)
  ```
- [ ] Update `architecture.md`/`roadmap.md` with any deviations made during build
  ```
  (decision log discipline).
  ```

### Exit criteria (evidence)

- [ ] Two consecutive full batch runs over the corpus: zero unhandled errors, zero
  ```
  duplicates, identical outputs between runs (deterministic modulo model output;
  flags stable).
  ```
- [ ] Simulated failures (missing key, bad PDF, kill mid-run) recover cleanly.
- [ ] README complete; regression script passes ≥98% field accuracy and 100% selection.
- [ ] Human (owner) accepts a live batch from `inbox/` with a handful of real new files.

### Why this stage exists

A pipeline that fails on network errors or corrupt files is worse than manual entry at
500/month. Determinism and regression coverage are what make the two-tier confidence
design trustworthy in production.

---

## Future / explicitly out of scope

- Email polling, web UI, DB sink, multi-user (architecture.md Section 9).
- Cloud OCR engines, local LLM inference, regex extraction — do not add without
reopening the decision log.

## Ground-truth reference (authoritative: `tests/ground_truth.json`)

> Supersedes the initial-analysis table that was here before (which listed the
> wrong invoice numbers for the DOC-CLF-0x files — `DOC-MISREAD-01`/`DOC-MISREAD-02`
> are supplier/supporting bill numbers, not the broker invoice numbers).
> `tests/ground_truth.json` (was `invoices/ocr_summary.json`, moved 2026-08-09,
> D18) is owner-verified, 100% accurate — "the goal is to match that"
> (2026-08-03, Stage 2 kickoff). The `file` column uses the 2026-08-09 corpus
> labels (bookkeeping join keys, content-verified — D18); old names in parens.


| File (new / old)                | Broker           | Invoice no      | Total (INR)                                                            |
| ------------------------------- | ---------------- | --------------- | ---------------------------------------------------------------------- |
| sample_b_1 (OLD-EXP-01)       | Vendor A       | DOC-EXP-01 | AMT-10                                                               |
| sample_b_2 (OLD-EXP-02)       | Vendor A       | DOC-EXP-02 | AMT-10                                                               |
| sample_b_3 (OLD-EXP-03)       | Vendor A       | DOC-EXP-03 | 525.10                                                                 |
| sample_c_1 (BUYER OLD-NIP-01) | Vendor C   | OLD-NIP-01/2025  | AMT-08                                                               |
| sample_c_2 (BUYER OLD-NIP-02) | Vendor C   | OLD-NIP-02/2025  | AMT-12                                                               |
| sample_a_1 (DOC-D-01)                | Vendor D    | DOC-D-01        | AMT-03                                                               |
| sample_a_2 (DOC-D-02)                | Vendor D    | DOC-D-02        | AMT-03                                                               |
| sample_a_3 (DOC-D-03)                | Vendor D    | DOC-D-03        | AMT-03                                                               |
| classic_1 (DOC-CLF-01)        | Vendor B      | DOC-CLF-01 | AMT-03 (EXCLUDED from eval)                                          |
| classic_2 (DOC-CLF-02)        | Vendor B (p1) | DOC-CLF-02 | AMT-01 (sub-invoices AMT-02a + 708.00; 0.20 rounding quirk, D8) |
| classic_3 (DOC-CLF-03)     | Vendor B      | DOC-CLF-03 | AMT-09                                                               |
| classic_4 (DOC-CLF-04)     | Vendor B      | DOC-CLF-04 | AMT-11                                                               |


