# Verdandi × Azure Content Understanding — Final Benchmark Report

**Date:** 2026-10-06 · **Dataset:** 100 frozen documents / 103 pages · **Runs:** two per system, same day, interleaved
**Verdict:** Verdandi **97.0%** field macro accuracy and **0.958** line-item F1 at **≈$0.26 per 100 documents** vs ACU **96.2%** and **0.947** at **≈$2.55** — a **0.8-point accuracy win at 1/9.7th the cost**.

This document records every aspect of the benchmark: the systems, the data, the
environment, the fairness controls, the scoring method, all accuracy and token
numbers, the complete cost analysis (including a per-model correction to the
reported cost), the run artifacts, exact reproduction commands, and every known
caveat. Nothing is omitted by intent; anything unverifiable is labeled as such.

---

## 1. Executive summary

| metric | Verdandi | ACU (Azure Content Understanding) |
|---|---|---|
| field macro accuracy (mean of 2 runs) | **97.0%** (97.0 / 97.0) | 96.2% (96.3 / 96.1) |
| line items P / R / F1 (mean) | 95.4 / 96.3 / **0.958** | 94.2 / 95.3 / 0.947 |
| documents found | 100/100 both runs | 100/100 both runs |
| cost per 100 docs — as reported | $0.2121 (see §8.3) | $2.5522 (billed) |
| cost per 100 docs — corrected per-model | **$0.2619** | $2.5522 |
| cost ratio (corrected) | **1×** | **9.7×** |
| tokens per run | ~1.31M (primary ~948k + rechecks ~371k) | ~568k (incl. 103k contextualization) |
| wall time per run | 661 s / 1931 s (retry) | 817 s / 995 s |

Where each side won:

- **Verdandi:** clean receipts **24/24** vs ACU **18/24** (both runs), degraded
  receipts 21/24 vs 19/24, plus per-field buyer_name (+5.4pt), subtotal
  (+3.3pt), total_amount (+3.1pt), vendor_name (+0.8pt).
- **ACU:** messy receipt photos 54–53/55 vs Verdandi 50/55, invoice_date
  (100% vs 98.6%), invoice_number (+1.5pt), tax_total (+2.6pt), currency
  (+1.0pt).
- **Draw:** invoice/scan stratum (52–53/58 each), invoice/clean (330/332 each
  in run A), invoice/degraded and invoice/handwriting (100% both).

---

## 2. Systems under test

### 2.1 Verdandi (this repository)

| aspect | detail |
|---|---|
| architecture | Vision-primary: ONE schema-constrained vision call per document (selection + extraction in the same call), then a deterministic local verification stack (grounding / arithmetic / format / gated narrow recheck), then confidence-thresholded review flags |
| model | `gpt-6-luna` (primary), served via the opencode Zen **GO** gateway |
| transport | Responses API (`api_style: responses`, `image_url_form: flat` — per-model override in `config/models.yaml`) |
| prompt | version **v11**, sha256 `f68cb8642b925b24b0e43f0b184652dc52dfe31c189e23fa7f092391c3c2c391` (recorded per file in `_meta.prompt`) |
| vision input | **full-resolution page renders** (`--vision-cap 0 --full-res`): each page's full-resolution normalized render is promoted to the vision slot — no downscaling |
| recheck (gated second opinion) | model `gpt-5.6-luna` (the configured verification model), narrow (disputed fields only, from the pages their regions cite), capped at 1 call per document; it FLAGS disagreement but never changes a value |
| capture | Apple Vision engine (macOS + swiftc), 103 pages, quality gate applied (poor pages reach the vision model; only blank pages are withheld) |
| parallelism | 3 workers (files in parallel; same number of calls either way) |
| document profile | `benchmark/rules_documents.yaml` (benchmark-only, derived from `benchmark/schema.yaml`) |
| harness | `benchmark/run_verdandi.py`; commit `d12e8c1` (+ working-tree harness changes below) |

### 2.2 Azure Content Understanding (ACU)

| aspect | detail |
|---|---|
| analyzer | `verdandi_benchmark` — custom analyzer, zero-shot (no labeled training data) |
| base analyzer | `prebuilt-document` |
| completion model | **`gpt-5.5`** (Foundry deployment name `gpt-5.5`, Global Standard, short-context rates) — ACU's best supported model on this subscription |
| embedding model | `text-embedding-3-large` |
| API version | `2025-11-01` |
| analyzer config | `returnDetails: true`, `enableFormula: false`, `estimateFieldSourceAndConfidence: true` |
| field schema | generated from the SAME `benchmark/schema.yaml` as Verdandi's profile (8 header fields + line items) |
| resource | Azure AI Foundry resource in region **eastus** (resource name withheld) |
| call pattern | ONE analyze call per document, raw PDF bytes base64-uploaded |
| harness | `benchmark/azure_acu.py` |

**Analyzer definition (key excerpt, as created for this round):**

```json
{
  "description": "Verdandi benchmark analyzer (canonical schema, zero-shot)",
  "baseAnalyzerId": "prebuilt-document",
  "models": {"completion": "gpt-5.5", "embedding": "text-embedding-3-large"},
  "config": {"returnDetails": true, "enableFormula": false,
             "estimateFieldSourceAndConfidence": true},
  "fieldSchema": {"fields": { "...": "8 fields + line_items array from schema.yaml" }}
}
```

The analyzer was **deleted and recreated** immediately before this round so both
systems used the identical v11 schema wording (PUT on an existing analyzer
returns 409 `ModelExists`; delete first — documented in the README runbook).
The v11 wording that both sides now share: amounts are "plain number" (never
"as printed"); currency is "the ISO 4217 code of the currency the document's
final total is printed in … never a secondary foreign-amount column's
currency".

---

## 3. Dataset (frozen v2, 100 documents / 103 pages)

| N | source | stratum | license / citation |
|---|---|---|---|
| 35 | FATURA2 (multi-layout generated invoices, image-based) | `invoice/clean` (35 docs) | CC BY 4.0 · Limam, Dhiaf, Kessentini — "FATURA" (arXiv 2311.11856, 2023) |
| 25 | CORD v2 (real Indonesian receipt photos) | `receipt/messy` | CC BY 4.0 · Park et al. — "CORD" (NeurIPS DocIntelligence Workshop, 2019) |
| 10 | Kaggle "Invoices for Document AI" (scanned TIFF invoices; machine labels re-derived from images by AI review) | `invoice/scan` | CC BY 4.0 · Kaggle holtskinner (TIFFs from Kaggle manishthem) |
| 30 | Synthetic (ours; deterministic generator) | `synthetic`: 20 clean digital, 6 degraded scans (rot90/180/270, blur, noise, jpeg15, low-contrast, skew), 4 handwriting-style, 3 Hindi+English, 3 multi-doc — mapped to `invoice/clean`, `receipt/clean`, `receipt/degraded`, `invoice/degraded`, `invoice/handwriting` | ours · `benchmark/synthetic/generate.py` (fixed seeds) |
| — | DocILE | **deferred** (owner decision) | research access only |

- **Truth:** one JSON per document (canonical fields + line items + axes +
  provenance). AI-assisted verification (owner decision), per-document notes in
  each truth file. Not human-audited.
- **Freeze:** `benchmark/frozen/manifest.json` — per-document `pdf_sha256` and
  `truth_sha256`, plus `schema_sha256`. Re-frozen for the v11 schema wording
  immediately before this round (PDF and truth hashes unchanged; only the
  schema hash and timestamp changed). The 30-doc pilot freeze is preserved at
  `benchmark/frozen/pilot-30docs-manifest.json`.
- **Integrity:** `python benchmark/score.py --verify-frozen` — sha256-checks
  every PDF + truth and the schema hash; must print OK before any paid call.
  It did.
- **Normalization of PDFs:** both systems receive the identical normalized PDF
  bytes (Verdandi reads its page renders of them; ACU receives the bytes
  base64-encoded).

---

## 4. Environment

| aspect | value |
|---|---|
| machine | Apple Silicon (`arm64`), macOS **27.0** (build 26A428) |
| Python | **3.14.6** (project `.venv`) |
| date / window | **2026-10-06**, 15:32–16:55 local (runs interleaved, see §7.5) |
| gateway | `https://opencode.ai/zen/go/v1` (opencode Zen **GO** subscription gateway) |
| gateway key | `OPENCODE_API_KEY` from `.env` (never printed, never committed) |
| Azure | AI Foundry resource in region **eastus** (resource name withheld); CU API `2025-11-01`; key `AZURE_CU_KEY` from `.env` (never printed) |
| network | consumer internet; three transient read-timeouts hit Verdandi run B (§7.4) and were retried |
| capture engine | Apple Vision (Swift harness compiled locally) |
| rate sources | Azure Retail Prices API (eastus, USD, pulled 2026-10-05); models.dev API (cached projection; identical entries under `opencode-go`, `opencode` and `openai`) |

---

## 5. Fairness controls (how the playing field was leveled)

1. **Identical bytes.** Same 100 frozen PDFs, sha256-verified against the
   manifest immediately before the runs. ACU receives the PDF bytes directly;
   Verdandi reads its renders of the same files. No file was re-processed,
   re-normalized or substituted between systems.
2. **One schema, one wording.** The Verdandi profile and the ACU analyzer are
   both generated from `benchmark/schema.yaml`; the ACU analyzer was recreated
   in this round so both sides saw the identical v11 descriptions. Field names,
   types and semantics are identical.
3. **Both zero-shot.** Verdandi uses its one-call profile (no examples, no
   few-shot); ACU uses a custom analyzer with no labeled training data.
4. **Best vs best.** Verdandi ran its strongest measured model (`gpt-6-luna`);
   ACU ran its best supported model (`gpt-5.5`). The earlier constraint that
   forced ACU onto `gpt-5-mini` no longer applies and is not used here.
5. **Maximum input fidelity for Verdandi.** Full-resolution page renders — no
   downscaling — removing any "resolution disadvantage" argument. (Measured
   earlier: capped vs full-res made no difference for gpt-6-luna on the hard
   subset, at half the tokens; full-res was chosen here for the strongest
   fairness stance.)
6. **No filename signals.** Both systems see content only; file names are
   bookkeeping labels for the scorer join, never inputs.
7. **Same page set.** Verdandi's capture produced 103/103 pages; only blank
   pages are withheld from the model (poor pages ARE sent). ACU analyzed all
   103 pages. No page-level advantage either way.
8. **Identical scoring.** One scorer (`benchmark/score.py`) with identical
   normalizers for both systems (§6). Same truth, same denominators, same
   strata.
9. **Two runs each, same day, interleaved.** A(V) → A(ACU) → B(V) → B(ACU);
   every run completed (the three transient timeouts in Verdandi B were
   re-extracted and the run's usage record refreshed — disclosed in §7.4).
10. **Costs recorded per run.** Each run's `run.json` carries its rate card
    with a source line; ACU costs are itemized from its `usage` object; the
    Verdandi cost basis is labeled (subscription quota proxy) and additionally
    corrected per model in §8.
11. **Disclosed asymmetries.** The systems are different products (a
    single-call vision pipeline vs a managed document-understanding service);
    their rechecks/extra passes and cost structures are documented rather than
    hidden. Public datasets may appear in either model's training data — the
    synthetic leg is the contamination-clean control (§11).

---

## 6. Scoring methodology

- **Fields (8):** invoice_number, invoice_date, vendor_name, buyer_name,
  currency, subtotal, tax_total, total_amount — canonical schema.
- **Comparisons:** names by casefolded alphanumeric substring (either direction);
  amounts numerically with ±0.01 tolerance; dates normalized to ISO (Verdandi's
  day-first normalizer first, then a benchmark-only month-first set for
  ambiguity — applied identically to both systems); currency via alias→ISO
  (symbols like `$`, `U.S. $`, `₹` normalized); other strings by casefolded
  alphanumeric equality. A cell is scored only where the truth has a value.
- **Macro accuracy:** mean of the per-field accuracies (per-field denominators
  differ; macro prevents easy fields from dominating).
- **Line items:** greedy row matching — a truth row matches a predicted row
  when amounts are within 0.01 AND descriptions are equal (1.0) or one contains
  the other (0.5); best-scoring unused candidate wins. P/R/F1 over matched
  rows.
- **Found / missing:** a document counts as found when the system produced an
  extraction for it (both systems: 100/100, missing 0).
- **Strata:** by document axes (document_type × quality): `invoice/clean`,
  `invoice/degraded`, `invoice/handwriting`, `invoice/scan`, `receipt/clean`,
  `receipt/degraded`, `receipt/messy`.

---

## 7. Results — every number

### 7.1 Headline

| | Verdandi run A (dir 11) | Verdandi run B (dir 12) | ACU run A (dir 6) | ACU run B (dir 7) |
|---|---|---|---|---|
| field macro | **97.0%** | **97.0%** | 96.3% | 96.1% |
| line items P | 96.3 | 94.4 | 92.9 | 95.5 |
| line items R | 96.7 | 95.9 | 95.5 | 95.1 |
| line items F1 | 0.9654 | 0.9514 | 0.9416 | 0.9530 |
| TP / FP / FN | 237/9/8 | 235/14/10 | 234/18/11 | 233/11/12 |
| docs found | 100/100 | 100/100 | 100/100 | 100/100 |
| wall time | 660.7 s | 1931.0 s¹ | 817.4 s | 995.0 s |

¹ includes the failed attempt time before the 3-file retry; the retry itself
succeeded (exit 0) and the usage record was refreshed.

### 7.2 Per-field accuracy (correct / scored)

| field | V-A | V-B | ACU-A | ACU-B |
|---|---|---|---|---|
| invoice_number | 68/70 (97.1%) | 68/70 (97.1%) | 69/70 (98.6%) | 69/70 (98.6%) |
| invoice_date | 72/73 (98.6%) | 72/73 (98.6%) | 73/73 (100%) | 73/73 (100%) |
| vendor_name | 67/68 (98.5%) | 68/68 (100%) | 67/68 (98.5%) | 67/68 (98.5%) |
| buyer_name | 46/46 (100%) | 45/46 (97.8%) | 43/46 (93.5%) | 43/46 (93.5%) |
| currency | 46/51 (90.2%) | 47/51 (92.2%) | 47/51 (92.2%) | 47/51 (92.2%) |
| subtotal | 75/77 (97.4%) | 74/77 (96.1%) | 72/77 (93.5%) | 72/77 (93.5%) |
| tax_total | 56/59 (94.9%) | 56/59 (94.9%) | 58/59 (98.3%) | 57/59 (96.6%) |
| total_amount | 96/98 (98.0%) | 96/98 (98.0%) | 93/98 (94.9%) | 93/98 (94.9%) |
| **macro** | **97.0%** | **97.0%** | **96.3%** | **96.1%** |

### 7.3 Per-stratum cells (correct / scored)

| stratum | V-A | V-B | ACU-A | ACU-B |
|---|---|---|---|---|
| invoice/clean | 330/332 | 329/332 | 330/332 | 330/332 |
| invoice/degraded | 21/21 | 21/21 | 21/21 | 21/21 |
| invoice/handwriting | 28/28 | 28/28 | 28/28 | 28/28 |
| invoice/scan | 52/58 | 53/58 | 52/58 | 52/58 |
| receipt/clean | **24/24** | **24/24** | 18/24 | 18/24 |
| receipt/degraded | 21/24 | 21/24 | 19/24 | 19/24 |
| receipt/messy | 50/55 | 50/55 | **54/55** | 53/55 |

### 7.4 Run completeness and retries

- All four runs produced 100/100 extractions; no missing documents.
- Verdandi run B hit **three transient network read-timeouts** (`cord_04`,
  `cord_07`, `cord_08`) during the first attempt. They were re-extracted with
  the new `--retry-failed` harness path (exit 0) and the run's `run.json` usage
  totals were recomputed so the token/cost record includes the retry.
- ACU runs: no failures; all 100 analyze calls succeeded per run.

### 7.5 Interleaving / timestamps

| run | started | elapsed |
|---|---|---|
| Verdandi A (dir 11) | 2026-10-06 15:32:38 | 660.7 s |
| ACU A (dir 6) | 2026-10-06 15:46:25 | 817.4 s |
| Verdandi B (dir 12) | 2026-10-06 16:19:23 | 1931.0 s (+retry) |
| ACU B (dir 7) | 2026-10-06 16:38:44 | 995.0 s |

---

## 8. Cost analysis — every number

### 8.1 Rate cards (with sources)

**ACU (billed, Azure Retail Prices API, eastus, USD, pulled 2026-10-05):**

| meter | rate |
|---|---|
| document pages (standard) | $5.00 / 1k pages |
| contextualization tokens | $1.00 / 1M |
| model input (uncached) | $5.00 / 1M (GPT 5.5 Global Standard, short context) |
| model input (cached) | $0.50 / 1M |
| model output | $30.00 / 1M |

**Verdandi (models.dev, per-token list prices; identical entries under
`opencode-go`, `opencode` (Zen) and `openai`):**

| model | input | output | cached read |
|---|---|---|---|
| gpt-6-luna (primary) | $0.10 / 1M | $0.50 / 1M | $0.01 / 1M |
| gpt-5.6-luna (rechecks) | $0.20 / 1M | $1.20 / 1M | $0.02 / 1M |

### 8.2 Token usage per run

| | V-A | V-B |
|---|---|---|
| primary prompt | 770,603 | 777,885 |
| primary completion | 177,736 | 180,787 |
| recheck prompt | 345,779 | 316,077 |
| recheck completion | 25,403 | 22,319 |
| recheck calls | 57 | 52 |
| **total** | **1,319,521** | **1,312,068** |

| | ACU-A | ACU-B |
|---|---|---|
| document pages | 103 | 103 |
| contextualization tokens | 103,000 | 103,000 |
| model input (total) | 425,778 | 425,778 |
| — of which cached | 266,240 | 336,896 |
| — of which uncached | 159,538 | 88,882 |
| model output | 38,976 | 38,517 |
| **total** | **567,754** | **567,295** |

### 8.3 Cost per run

**ACU (billed):**

| item | ACU-A | ACU-B |
|---|---|---|
| pages | $0.5150 | $0.5150 |
| contextualization | $0.1030 | $0.1030 |
| input uncached | $0.7977 | $0.4444 |
| input cached | $0.1331 | $0.1684 |
| output | $1.1693 | $1.1555 |
| **total** | **$2.7181** | **$2.3864** |

**Verdandi:**

| item | V-A | V-B |
|---|---|---|
| primary (gpt-6-luna) | $0.1659 | $0.1682 |
| rechecks (gpt-5.6-luna) | $0.0996 | $0.0900 |
| **corrected total** | **$0.2656** | **$0.2582** |
| as reported by `score.py` (all tokens at the primary card) | $0.2132 | $0.2109 |

The scorer priced recheck tokens at the primary model's rates; rechecks
actually ran on `gpt-5.6-luna`, which is 2×/2.4× pricier. The **corrected
per-model figures are the honest straight-up prices**: mean **$0.2619 per 100
documents**.

**Caching bound.** The Verdandi usage record does not separate cache-read
tokens. If the gateway's prompt cache applied to all input, the input side
bills at 10%: a **floor of ≈ $0.1326/run**. The true straight-up cost lies
between **$0.13 and $0.26**; $0.26 is the conservative (no-cache) bound used
for the ratio.

### 8.4 Ratios

| basis | Verdandi | ACU | ratio |
|---|---|---|---|
| as reported (mixed-model underprice) | $0.2121 | $2.5522 | 12.0× |
| **corrected per-model (no-cache bound)** | **$0.2619** | **$2.5522** | **9.7×** |
| if caching fully applied (floor) | $0.1326 | $2.5522 | 19.2× |

Per-document: Verdandi **$0.00262/doc**, ACU **$0.02552/doc** (corrected basis).

### 8.5 Full-day ledger (all rounds run on 2026-10-06)

| # | system | model | docs | reported | corrected | notes |
|---|---|---|---|---|---|---|
| V1 | Verdandi | gpt-5.6-luna | 100 | $0.3323 | $0.3323 | GPT 5.5 round (luna arm) |
| V2 | Verdandi | gpt-5.6-luna | 100 | $0.3306 | $0.3306 | GPT 5.5 round (luna arm) |
| V3 | Verdandi | gpt-6-luna | 30 | $0.0698 | $0.0888 | subset, full-res |
| V4 | Verdandi | gpt-6-luna | 30 | $0.0662 | $0.0827 | subset, full-res |
| V5 | Verdandi | gpt-6-luna | 30 | $0.0472 | $0.0566 | subset, capped |
| V6 | Verdandi | gpt-5.6-luna | 30 | $0.1380 | $0.1380 | subset, full-res |
| V7 | Verdandi | gpt-6-luna | 30 | $0.0470 | $0.0558 | subset, capped |
| V8 | Verdandi | gpt-5.6-luna | 30 | $0.1458 | $0.1458 | subset, full-res |
| V9 | Verdandi | gpt-6-luna | 30 | $0.0490 | $0.0591 | v11 formats subset |
| V10 | Verdandi | gpt-6-luna | 30 | $0.0492 | $0.0581 | v11 formats subset |
| V11 | Verdandi | gpt-6-luna | 100 | $0.2132 | **$0.2656** | **final round A** |
| V12 | Verdandi | gpt-6-luna | 100 | $0.2109 | **$0.2582** | **final round B** (3-file retry) |
| | | | | **$1.6994** | **$1.8717** | Verdandi day total |
| A1 | ACU | gpt-5.5 | 100 | $2.4058 | — | GPT 5.5 round |
| A2 | ACU | gpt-5.5 | 100 | $2.3674 | — | GPT 5.5 round |
| A3 | ACU | gpt-5.5 | 30 | $0.8869 | — | subset (old wording) |
| A4 | ACU | gpt-5.5 | 30 | $0.7322 | — | subset (old wording) |
| A5 | ACU | gpt-5.5 | 30 | $0.7850 | — | subset (v11 wording) |
| A6 | ACU | gpt-5.5 | 100 | $2.7181 | — | **final round A** |
| A7 | ACU | gpt-5.5 | 100 | $2.3864 | — | **final round B** |
| | | | | **$12.2817** | — | ACU day total |

Plus the one-document ACU pilot probe earlier in the day (≈$0.04, not ledgered
as a run). **Total spend for the day ≈ $14.2** (ACU ≈ $12.3 billed + Verdandi
≈ $1.9 list).

---

## 9. Residual analysis (where the errors are)

**Verdandi's remaining errors** concentrate in the CORD messy-photo stratum
(receipt/messy 50/55): hard-to-read Indonesian receipt totals, plus a handful
of scan misses (missing currency on noisy scans; multi-currency selection on
`kaggle_01`; one date-policy case). Its clean-receipt reading is perfect
(24/24 both runs) — a class ACU has never fully solved.

**ACU's remaining errors** are dominated by the clean-receipt class
(receipt/clean 18/24 both runs — deterministic receipt-semantics residuals that
persist even on GPT 5.5) and a few scan identity quirks; its messy-photo
reading is the best in this round (54–53/55).

**Shared failure — `kaggle_01` truth.** Both systems now read GBP / 7555.55 on
this two-currency document while the frozen truth says USD / 13600. The
document prints both currencies; whether the truth picked the right one is an
open owner question (flagged, not changed).

**Date policy.** Verdandi's day-first normalization (D58) deliberately rejects
ambiguous month-first dates; some Kaggle scans print US-style dates. This is a
policy, not a reading failure, and is scored identically for both systems.

**Synthetic-layout note.** In earlier subset rounds ACU dropped leading digits
on our "compact" synthetic receipts (high confidence, both runs) — a possible
generator layout artifact, not real-world evidence. In the final round the
synthetic leg is included in the strata above.

---

## 10. Artifacts and reproduction

**Run directories**

```
benchmark/data/results/verdandi/2026-10-06/11/   # final A (json/, run.json, flags.csv, raw/ gitignored)
benchmark/data/results/verdandi/2026-10-06/12/   # final B (+ retry record in run.json)
benchmark/data/results/acu/2026-10-06/6/         # final A (per-doc .json + .raw.json + run.json)
benchmark/data/results/acu/2026-10-06/7/         # final B
```

**Reports**

```
benchmark/data/results/reports/report-20261006-final-pairA.md
benchmark/data/results/reports/report-20261006-final-pairB.md
benchmark/data/results/reports/score-20261006-final-pair{A,B}.json
```

**Exact commands used**

```sh
# 0. integrity gate (must pass before any paid call)
python benchmark/score.py --verify-frozen

# 1. Verdandi A: full-resolution capture + gpt-6-luna (paid)
python benchmark/run_verdandi.py --vision-cap 0 --full-res --paid --workers 3 --model gpt-6-luna

# 2. ACU A (paid; env: AZURE_CU_ENDPOINT/KEY/COMPLETION=gpt-5.5 + rates)
python benchmark/azure_acu.py --run --paid

# 3. Verdandi B (fresh capture)
python benchmark/run_verdandi.py --vision-cap 0 --full-res --paid --workers 3 --model gpt-6-luna

# 3b. retry transient failures (run B)
python benchmark/run_verdandi.py --retry-failed --workers 3 --model gpt-6-luna \
    --run benchmark/data/results/verdandi/2026-10-06/12

# 4. ACU B
python benchmark/azure_acu.py --run --paid

# 5. score (per pair; reports are second-stamped, run each pair separately)
python benchmark/score.py --report \
    --verdandi-run benchmark/data/results/verdandi/2026-10-06/11 \
    --acu-run benchmark/data/results/acu/2026-10-06/6
```

**Analyzer lifecycle (ACU):** `--print-analyzer` (inspect) → DELETE the existing
analyzer → `--create-analyzer` (free). Resource defaults: PATCH
`/contentunderstanding/defaults` with `{"<old-deployment>": null}` to remove a
stale deployment, then set `{gpt-5.5: gpt-5.5, text-embedding-3-large: ...}`.

**Harness changes introduced this day (all in `benchmark/`):**
`--verify-frozen` (sha256 preflight), `--subset <json>`, `--vision-cap N`,
`--full-res` (promote full-resolution renders to the vision slot),
`--retry-failed` (re-extract failed files + refresh the usage record), and
automatic rate-card recording in both runners.

---

## 11. Caveats (complete list)

1. **Training-data contamination.** Public datasets (FATURA2, CORD, Kaggle) may
   appear in either model's training data. The synthetic leg is the
   contamination-clean control; its results are included in the strata.
2. **Truth is AI-verified, not human-audited** (owner decision). Kaggle scans
   carry the highest residual risk; `kaggle_01`'s currency assignment is an
   open question (§9).
3. **Cost basis asymmetry.** ACU is billed (Azure list prices, eastus);
   Verdandi's rates are per-token list prices on a subscription gateway billed
   by dollar quota — the "quota proxy" label. The corrected figures use
   straight-up per-token prices for each model actually used.
4. **Caching.** Verdandi's usage does not separate cache-read tokens; the
   cost has a floor of ≈$0.13/run if caching fully applied (§8.3).
5. **Recheck policy.** Verdandi's gated recheck runs on a second model
   (`gpt-5.6-luna`), capped at one call per document; it flags disagreement
   but never rewrites values. ACU has no equivalent per-field flagging in this
   benchmark; its confidence output was not scored.
6. **Day-first date policy.** Deliberate (D58); ambiguous month-first dates
   remain a documented limitation.
7. **Single-machine timing.** Wall times are one machine's wall clock; not a
   scored metric.
8. **Subset rounds are not part of the final table** — they exist in the day
   ledger and the README's diagnostic section for traceability.
9. **Verdandi run B retry.** Three transient timeouts were retried; the retry
   tokens are included in the usage record (§7.4). No other run needed retries.
10. **ACU analyzer updates require delete+create**; documented in the runbook.

---

## 12. Round history (same day, for traceability)

| round | what | outcome |
|---|---|---|
| pilot | 1 ACU doc on gpt-5.5 (plumbing + cost shape) | read correctly; ~$0.04 |
| GPT 5.5 round | 100 docs ×2 each, gpt-5.6-luna (capped) vs ACU gpt-5.5 | V 96.1% vs A 96.4%; $0.33 vs $2.39/run (as reported) |
| subset diagnostics | 30-doc failure set (26 failures + 4 controls), 2×2 attribution | model ≫ resolution: gpt-6-luna capped 90.7% (13/27 fixed) vs luna capped 83.2% |
| format fix (v11) | canonical numbers + first-class currency + tolerant grounding | 13/27 old misses fixed; subset 91.3% (gpt-6-luna capped) |
| subset refresh | fresh ACU on the recreated v11 analyzer | V 91.9/93.8% vs A 90.1% on the hard subset |
| **final** | **100 docs, best vs best, full-res** | **V 97.0% / F1 0.958 / $0.26 vs A 96.2% / 0.947 / $2.55** |

---

*All figures in this report come from the committed run artifacts and score
reports listed in §10. Token counts are as reported by the gateway/service
`usage` objects; costs are computed from the rate cards recorded in each run's
`run.json`. Nothing in this document was estimated except where explicitly
labeled (caching bound, pilot cost).*
