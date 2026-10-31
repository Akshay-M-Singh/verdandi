# Verdandi × Azure Content Understanding benchmark (100 docs)

Self-contained benchmark harness. **Isolation guarantee:** nothing outside
`benchmark/` imports from it; the Verdandi pipeline, UI, tests and `make`
targets behave exactly as before. The only Verdandi coupling is one-way and
in-process: `benchmark/run_verdandi.py` points `config.DOCUMENTS_PATH` at
`benchmark/rules_documents.yaml` inside its own process (the config path is
documented as resolved at call time) — `rules/documents.yaml` is never
modified, and `invoices/` (the permanent corpus) is never used.

## What is measured (owner decisions, 2026-09-30)

1. **Accuracy** — document-level fields + line items (canonical schema below),
   per field and per stratum, plus line-item row matching P/R/F1.
2. **Cost** — ACU from the response `usage` object × the live rate card
   (captured with region/date); Verdandi from token counts (quota proxy,
   labeled as such in the report).
3. **Token consumption** — ACU: contextualization + model input/output tokens
   per document (API `usage`); Verdandi: prompt/completion (+ rechecks
   separately), recorded per call.

Fairness controls: identical normalized PDF bytes to both systems; both run
zero-shot (Verdandi's one-call profile vs an ACU custom analyzer without
labeled data); model/version/prompt provenance recorded per run. Public
datasets may be present in either model's training data — the synthetic leg
is the contamination-clean control and the caveat is reported.

## Composition (100 docs / 103 pages)

| N | Source | Stratum |
|---|---|---|
| 35 | FATURA2 (CC BY 4.0) | clean multi-layout invoices, image-based |
| 25 | CORD v2 (CC BY 4.0) | messy real receipt photos, Indonesian |
| 10 | Kaggle "Invoices for Document AI" (CC BY 4.0) | scanned invoices, machine labels re-derived from images |
| 30 | synthetic (ours) | 6 clean digital · 11 degraded scans (rot90/180/270, blur, noise, jpeg15, low-contrast, skew) · 4 handwriting-style · 3 Hindi+English · 3 multi-doc (invoice + annexure) |
| — | DocILE (research access, internal use) | **deferred** (owner) |

Licenses, citations and access notes: `sources.yaml`.

## Canonical schema and truth

`schema.yaml` is the single source of field names. Truth is one JSON per
document, `benchmark/data/truth/<stem>.json`:

```json
{
  "stem": "syn_digital_a",
  "source": {"kind": "synthetic", "generator": "benchmark/synthetic/generate.py"},
  "axes": {"document_type": "invoice", "quality": "clean", "capture": "digital",
            "language": "en", "pages": 1, "multi_document": false},
  "fields": {"invoice_number": "...", "invoice_date": "2025-06-30", "...": "..."},
  "line_items": [{"description": "...", "quantity": "1", "unit_price": "100.00", "amount": "100.00"}],
  "verification": {"status": "unverified|verified", "by": "owner|ai|mapped", "notes": "..."}
}
```

**Freeze protocol:** truth is drafted by adapters (public) or by construction
(synthetic), then owner/AI-verified, then frozen with sha256 in
`benchmark/frozen/manifest.json` **before any paid call**. `score.py --freeze`
writes the manifest and refuses to overwrite an existing frozen hash.

## Layout

```
benchmark/
  README.md              this protocol
  schema.yaml            canonical fields + normalization
  sources.yaml           datasets, licenses, citations, target counts
  rules_documents.yaml   Verdandi profile (benchmark-only)
  download.py            public-source ingest -> data/raw (+ PDF normalization)
  adapters/              source annotations -> canonical truth drafts
  synthetic/             deterministic generator + committed truth
  azure_acu.py           ACU REST runner (stdlib) + analyzer schema emitter
  run_verdandi.py        capture + extract with the benchmark profile
  score.py               metrics + freeze + report
  frozen/manifest.json   committed freeze (hashes)
  data/                  gitignored: raw/, pdfs/, truth/, results/
```

## Runbook (in order)

1. **Ingest public sources** — `python benchmark/download.py --source all`
   (HF sources work without credentials; Kaggle needs `~/.kaggle/kaggle.json`;
   DocILE needs the access-form token — see `sources.yaml`).
2. **Generate synthetic leg** — `python benchmark/synthetic/generate.py`.
3. **Draft + verify truth** — `python benchmark/adapters/run_all.py` then the
   owner/AI verification pass; mark `verification.status = verified`.
4. **Freeze** — `python benchmark/score.py --freeze`.
5. **Verify the frozen bytes** (before any paid call) —
   `python benchmark/score.py --verify-frozen` (sha256-checks every PDF + truth
   against the manifest and the schema hash; aborts on drift).
6. **Verdandi run** (paid: 1 primary call/file + capped narrow rechecks) —
   `python benchmark/run_verdandi.py --workers 3` (dry-run prints the estimate;
   `--paid` executes; `--run <dir>` reuses a capture). The run's rate card is
   recorded in `run.json` automatically (models.dev hint rates = quota proxy).
7. **ACU run** (paid; needs `AZURE_CU_ENDPOINT`, `AZURE_CU_KEY`, and a Foundry
   completion deployment configured on the resource) —
   `python benchmark/azure_acu.py --create-analyzer` then
   `python benchmark/azure_acu.py --run --paid`. Set the `AZURE_CU_RATE_*`
   env vars (page, contextualization, model input/cached/output, `_SOURCE`) so
   the run's rate card is recorded; `run.json` carries the full breakdown.
   If the resource defaults still reference a deleted deployment, PATCH
   `/contentunderstanding/defaults` with `{"<old-deployment>": null}` to
   remove it before creating the analyzer. To UPDATE an existing analyzer
   (model or schema change), DELETE it first — PUT returns 409 ModelExists.
8. **Score/report** — `python benchmark/score.py --report
   --verdandi-run <dir> --acu-run <dir>` (run each pair separately — reports
   are second-stamped).
9. **(Optional) Inspect the runs in the desktop app** —
   `python benchmark/import_to_ui.py --date <YYYY-MM-DD>` imports every
   benchmark Verdandi run into the app workspace (`~/.verdandi/runs/`):
   json/flags are COPIED, page renders are HARDLINKED (no extra disk; deleting
   or pruning the view-run only removes its links), a synthetic `job.json`
   makes them appear in the Console with names/models/tokens/cost, and review
   write-through edits only the workspace copies. ACU runs are not importable
   (different output shape). Idempotent; `--runs 11,12` narrows the set.

## Results (2026-10-06, 100 docs / 103 pages, GPT 5.5 round, TWO runs each)

This round's fairness controls: frozen v2 bytes re-verified by sha256 before
any paid call; identical PDF bytes (base64) to both systems; same canonical
schema (one generator for the Verdandi profile and the ACU analyzer); both
zero-shot; **ACU on GPT 5.5** (Global Standard — its best supported model, so
the 09-30 gpt-5-mini constraint is gone) vs **Verdandi on gpt-5.6-luna**
(production default, prompt v10); rate cards recorded per run.

| metric | Verdandi (gpt-5.6-luna) | ACU (`verdandi_benchmark`, GPT 5.5) |
|---|---|---|
| field macro accuracy | 96.1% (96.3 / 95.8) | **96.4%** (96.5 / 96.3) |
| line items P / R / F1 | 94.2 / 95.3 / 0.947 (0.951 / 0.943) | **95.4 / 96.3 / 0.958** (0.959 / 0.957) |
| docs found | 100/100 both runs | 100/100 both runs |
| cost / run | **$0.3315** (quota proxy) | $2.3866 (billed) → **7.20×** |
| tokens / run | ~699k (507k prompt + 192k completion incl. 55 rechecks) | ~564k (103k contextualization + 91k uncached + 331k cached input + 38k output) |

Per-field accuracy (mean of the two runs):

| field | Verdandi | ACU |
|---|---|---|
| invoice_number | 97.1% | 98.6% |
| invoice_date | 98.6% | 100.0% |
| vendor_name | 96.4% | 98.5% |
| buyer_name | 93.5% | 93.5% |
| currency | 90.2% | 92.2% |
| subtotal | 96.8% | 93.5% |
| tax_total | 95.8% | 97.5% |
| total_amount | 97.0% | 95.9% |

Run dirs: Verdandi `2026-10-06/{1,2}`, ACU `2026-10-06/{1,2}`; reports
`data/results/reports/report-20261006-pair{A,B}.md`. Two-run variance:
Verdandi ±0.5pt macro / ±0.008 F1, ACU ±0.2pt / ±0.002. Residuals: the GPT 5.5
upgrade **fixed ACU's D65 CORD-total class** (`receipt/messy` total 25/25 both
runs, was 18/24) while keeping the deterministic clean-receipt subtotal/total
residual (synthetic `receipt/clean` 0/3 subtotal + 0/3 total) and losing 1/3
subtotal+total on degraded receipts; Verdandi's residual is CORD totals
(22–24/25), clean-invoice buyer_name (87%) and scan currency (80%). ACU now
leads both headline metrics at 7.2× the billed cost. Caveats unchanged:
public sets may be in either model's training data (the synthetic leg is the
contamination-clean control); Verdandi's cost is a quota proxy and its batch
includes 55 capped narrow rechecks; truth is AI-verified, not human-audited.

## Results (2026-10-06, FINAL round — 100 docs / 103 pages, best vs best, TWO runs each)

The definitive head-to-head. Fairness controls: frozen v2 bytes sha256-verified
before any paid call (the manifest was re-frozen for the v11 schema wording —
PDF/truth hashes unchanged); identical PDF bytes to both systems; ONE schema
wording generating the Verdandi profile and the ACU analyzer (recreated for
v11); both zero-shot; **Verdandi on gpt-6-luna with FULL-RESOLUTION page
renders** (its best measured model; max input fidelity, no downscaling) vs
**ACU on GPT 5.5** (its best supported model); 2 runs each, same day,
interleaved; every run complete (3 transient network timeouts in Verdandi run
B were retried and the usage record refreshed); rate cards recorded per run.

| metric | Verdandi (gpt-6-luna, full-res) | ACU (`verdandi_benchmark`, GPT 5.5) |
|---|---|---|
| field macro accuracy | **97.0%** (97.0 / 97.0) | 96.2% (96.3 / 96.1) |
| line items P / R / F1 | 95.4 / 96.3 / **0.958** (0.965 / 0.951) | 94.2 / 95.3 / 0.947 (0.942 / 0.953) |
| docs found | 100/100 both runs | 100/100 both runs |
| cost / run | **$0.2121** (quota proxy) | $2.5523 (billed) → **12.0×** |
| tokens / run | ~1.31M (1.11M prompt + 203k completion, 54 rechecks) | ~568k (103k contextualization + 124k uncached + 302k cached input + 38.7k output) |

Per-field accuracy (mean of runs):

| field | Verdandi | ACU |
|---|---|---|
| invoice_number | 97.1% | 98.6% |
| invoice_date | 98.6% | 100.0% |
| vendor_name | 99.3% | 98.5% |
| buyer_name | 98.9% | 93.5% |
| currency | 91.2% | 92.2% |
| subtotal | 96.8% | 93.5% |
| tax_total | 94.9% | 97.5% |
| total_amount | 98.0% | 94.9% |

Strata (cells, run A/B): Verdandi wins the clean-receipt class decisively
(24/24 both runs vs ACU 18/24 both — ACU's deterministic receipt semantics
residual persists even on GPT 5.5) and degraded receipts (21/24 vs 19/24);
ACU leads the messy receipt photos (54/55, 53/55 vs Verdandi 50/55 both);
invoice/scan is a draw (52–53/58). Verdandi's remaining weakness is CORD-class
receipt totals; ACU's is clean receipt semantics. Caveats: public sets may be
in either model's training data (synthetic leg is the contamination-clean
control); Verdandi's cost is a quota proxy and its runs include capped narrow
rechecks; truth is AI-verified — `kaggle_01`'s two-currency assignment
deserves an owner look (both systems now read GBP/7555.55 vs the USD/13600
truth).

Run dirs: Verdandi `2026-10-06/{11,12}`, ACU `2026-10-06/{6,7}`; reports
`data/results/reports/report-20261006-final-pair{A,B}.md`. The complete
standalone record — environment, fairness controls, every accuracy/token/cost
number, reproduction commands and all caveats — is
`benchmark/BENCHMARK-REPORT-2026-10-06.md`.

## Diagnostic round (2026-10-06, 30-doc failure subset, 2×2 attribution)

Purpose: the 100-doc round showed Verdandi's remaining misses clustering in a
few classes; this round isolates **model vs page-image resolution** on exactly
those documents. Subset (`benchmark/subset30.json`): all 26 documents Verdandi
failed on in the 100-doc round (a field miss or a line-item mismatch in either
run) + 4 controls (clean invoice, messy receipt, and two documents where ACU
failed: `kaggle_02` buyer name, `syn_03_digital_compact` leading digit).
Both systems run the same 30 frozen documents, two runs per arm.

| arm (2 runs each) | macro accuracy | old-miss cells fixed (of 27) | cost / run |
|---|---|---|---|
| luna, capped images (the 100-doc config) | 83.2% | 0/27 | ~$0.10 |
| luna, full-resolution renders | 85.7% | 9/27 | $0.14 |
| **gpt-6-luna, capped images** | **90.7%** | **13/27** | **$0.047** |
| gpt-6-luna, full-resolution renders | 90.7% | 13/27 | $0.068 |
| ACU, GPT 5.5 | 91.3% | 20/27 | $0.81 |

Findings: the **model upgrade did the heavy lifting** (+7.5pt; fixes messy
receipts, names, Hindi vendor) while **full resolution helped only the older
model** (+2.5pt) and added nothing on top of gpt-6-luna — at double the prompt
tokens (235k vs 112k per 30 docs). gpt-6-luna on the standard capped images is
the best value: 90.7% on a failure-stacked subset at **1/17 of ACU's cost**.
Line items on the subset: Verdandi F1 0.868/0.857 vs ACU 0.882/0.866 (ACU's
row segmentation was erratic here too: 9 extra rows on `cord_04`, 5 on
`kaggle_06`).

Remaining unfixed classes (next candidates): as-printed number formats
(`cord_17`/`cord_20` "50.000" — needs output normalization, neither fix
touches it), currencies missing on scans (4 cells), the day-first date policy
(`kaggle_05`: the new model reads the right digits, but D58 normalizes the
month-first US date to the wrong day — a policy question, not a reading one),
multi-currency amount selection (`kaggle_01`), and messy-receipt totals
(`cord_10`/`cord_19`; ACU is solid there).

**Format-normalization fix (2026-10-06, prompt v11 + format layer).** The
unfixed classes above were output-format problems, not reading problems: the
prompt now demands the numeric VALUE (locale grouping interpreted, never
copied) and the ISO 4217 code of the main total's currency; grounding learned
the plausible readings of grouped tokens (`50000` grounds against printed
`50.000`); the format check flags non-canonical amounts and ambiguous
currencies (`$`, `¥`) for review, never guessing; `currency` is a first-class
field type. Re-run on the same subset (gpt-6-luna, capped, 2 runs): **13/27
old misses fixed** — the entire number-formatting class (`cord_17`/`cord_20`)
and the messy-receipt totals (`cord_10`) now correct in both runs — with 2
single-run regressions (name/tax variance) and no amount-format flags, meaning
the model emits canonical numbers; ambiguous `$` surfaces for review on
`fatura_16`/`fatura_20`. Subset macro 91.3% (was 90.7% for the same arm).
Remaining misses are genuine reads (missing scan currencies, multi-currency
selection on `kaggle_01`) or the deliberate day-first date policy.

Refreshed head-to-head on the same subset with the v11 wording on BOTH sides
(the ACU analyzer was recreated to pick up the shared schema wording): Verdandi
**91.9% / 93.8%** macro (2 runs, F1 0.916/0.921, ~$0.049/run) vs ACU **90.1%**
(1 run, F1 0.872, $0.785/run — **16×**). The wording change cost ACU 2 cells on
`kaggle_01` (its multi-currency layout now reads GBP/7555.55 instead of
USD/13600 — the same document where Verdandi misses; the truth labels USD, so
this document's currency assignment deserves an owner look). Note ACU's subset
run is one sample; its run-to-run variance has been small (~0.2pt).

Harness: `--subset <json>` (runners + scorer), `--vision-cap N` (0 = no
downscaled copy) and `--full-res` (promotes the full-resolution render to the
vision input) on `run_verdandi.py`. Run dirs: Verdandi `2026-10-06/{3..8}`
(3/4 = gpt-6-luna full-res; 5/7 = gpt-6-luna capped; 6/8 = luna full-res),
ACU `2026-10-06/{3,4}`; reports `report-20261006-subset30-pair{A,B}.md`.

## Results (2026-09-30, 100 docs / 103 pages, TWO runs each — gpt-5-mini round)

*(Superseded by the 2026-10-06 table above; kept as history.)*

| metric | Verdandi (gpt-5.6-luna) | ACU (`verdandi_benchmark`, gpt-5-mini) |
|---|---|---|
| field macro accuracy | **96.7%** (97.2 / 96.1) | 95.2% (95.2 / 95.2) |
| line items P / R / F1 | 94.7 / 95.1 / **0.949** (0.951 / 0.947) | 88.4 / 90.2 / 0.893 (0.895 / 0.891) |
| docs found | 100/100 both runs | 100/100 both runs |
| amount cells (matched rows) | 466/466 | 442/442 |
| cost | **$0.3319/run** (quota proxy) | **$0.7174/run** (billed) → **2.16×** |
| tokens | ~700k/run (508k prompt + 192k completion incl. ~54 rechecks) | ~567k/run (103k contextualization + 20k uncached + 412k cached input + 41.7k output) |

Run dirs: Verdandi `2026-09-30/{5,6}`, ACU `2026-09-30/{4,5}`; reports
`data/results/reports/report-20260930-pair{A,B}.md`. Two-run variance is small
(≤1.1pt macro fields, ≤0.005 F1). Residual error is dominated by: ACU's
deterministic receipt semantics (4 CORD totals + 8 synthetic receipt/statement
totals per run; `receipt/clean` 18/24 both runs vs Verdandi 24/24, which
hardens the CORD rule reading) and Kaggle scan identity quirks on both sides.
Full-line-item F1 moved sharply vs the 30-doc pilot after a synthetic layout
bug was found and fixed (below); the pilot numbers are superseded.

Pilot history: the first 30-doc pass (2026-09-30, one run each) scored 94.5%
macro both, F1 0.781 vs 0.809, $0.1041 vs $0.2196. During scale-up review the
scan-family synthetic PDFs were found to clip the "Amount" column ~20 px past
the page edge (generator column math), which penalized the copy-only reader
while the computing reader recovered the product; the generator was fixed,
truth re-frozen, and all systems re-run on identical corrected bytes.

Caveats: public sets may be in either model's training data (synthetic leg is
the contamination-clean control); ACU was constrained to gpt-5-mini (the only
deployable model for this subscription) while Verdandi ran luna; the Verdandi
cost is a quota proxy via models.dev hint rates; at 100 docs the truth is
AI-verified (per-dataset provenance notes in each truth file), not human
audited — Kaggle scans carry the highest residual risk. Freeze:
`frozen/manifest.json` (v1 pilot freeze kept at `frozen/pilot-30docs-manifest.json`).

## Scale status (2026-09-30)

- [x] 100-doc composition: 35 FATURA2 + 25 CORD + 10 Kaggle + 30 synthetic (DocILE deferred)
- [x] CORD adapter at 25 docs: row net = itemsubtotal | price + signed discount; all rows
      reconcile to the printed subtotal
- [x] Kaggle: 6 scale scans reviewed from images; currency alias `BF→BEF`, month-first
      date scoring added
- [x] Synthetic scale leg: blur/noise/jpeg15/low-contrast/skew/rot270/handwriting/Hindi/
      multi-doc; deterministic; visually verified
- [x] Truth review (AI-assisted) + freeze — `frozen/manifest.json`, 100 docs
- [x] Paid Verdandi runs ×2 — `data/results/verdandi/2026-09-30/{5,6}/`
- [x] Paid ACU runs ×2 — `data/results/acu/2026-09-30/{4,5}/` (analyzer `verdandi_benchmark`)
- [x] Generator scan-layout clip fix + re-freeze + full re-runs (both systems)
- [x] `compare.norm_name` totality fix (object-typed model value crashed the batch in
      one run; now returns no-agreement) + regression test
- [ ] Publication/report decision

Fetched: 70/70 public (35 FATURA + 25 CORD + 10 Kaggle) + 30/30 synthetic = **100/100 documents**.

Paid-cost statements are printed by both runners before any call (page/file
counts × rates) and must be confirmed with `--paid`.
