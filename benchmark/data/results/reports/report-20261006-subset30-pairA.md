# Benchmark score — 2026-10-06T13:49:00

truth documents: 30


## verdandi (benchmark/data/results/verdandi/2026-10-06/3)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 19 | 20 | 95.0% |
| invoice_date | 19 | 20 | 95.0% |
| vendor_name | 19 | 19 | 100.0% |
| buyer_name | 16 | 17 | 94.1% |
| currency | 12 | 16 | 75.0% |
| subtotal | 20 | 22 | 90.9% |
| tax_total | 16 | 18 | 88.9% |
| total_amount | 26 | 29 | 89.7% |
| **macro** | | | **91.3%** |

line items: TP=82 FP=14 FN=11 P=85.4 R=88.2 F1=0.8677


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 359,874 | $0.1/1M | $0.0360 |
| completion tokens | 67,665 | $0.5/1M | $0.0338 |

**verdandi total: $0.0698** (rates: models.dev hint rates for gpt-6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/3)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 19 | 20 | 95.0% |
| invoice_date | 20 | 20 | 100.0% |
| vendor_name | 19 | 19 | 100.0% |
| buyer_name | 14 | 17 | 82.4% |
| currency | 13 | 16 | 81.2% |
| subtotal | 19 | 22 | 86.4% |
| tax_total | 16 | 18 | 88.9% |
| total_amount | 27 | 29 | 93.1% |
| **macro** | | | **91.3%** |

line items: TP=86 FP=16 FN=7 P=84.3 R=92.5 F1=0.8821


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 31 | $5.0/1k | $0.1550 |
| contextualization tokens | 31,000 | $1.0/1M | $0.0310 |
| model input tokens (uncached) | 61,295 | $5.0/1M | $0.3065 |
| model input tokens (cached) | 66,560 | $0.5/1M | $0.0333 |
| model output tokens | 12,038 | $30.0/1M | $0.3611 |

**acu total: $0.8869** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

