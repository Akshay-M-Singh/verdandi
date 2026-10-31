# Benchmark score — 2026-10-06T15:14:09

truth documents: 30


## verdandi (benchmark/data/results/verdandi/2026-10-06/9)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 19 | 20 | 95.0% |
| invoice_date | 19 | 20 | 95.0% |
| vendor_name | 17 | 19 | 89.5% |
| buyer_name | 16 | 17 | 94.1% |
| currency | 12 | 16 | 75.0% |
| subtotal | 21 | 22 | 95.5% |
| tax_total | 16 | 18 | 88.9% |
| total_amount | 28 | 29 | 96.6% |
| **macro** | | | **91.9%** |

line items: TP=87 FP=10 FN=6 P=89.7 R=93.5 F1=0.9158


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 163,427 | $0.1/1M | $0.0163 |
| completion tokens | 65,386 | $0.5/1M | $0.0327 |

**verdandi total: $0.049** (rates: models.dev hint rates for gpt-6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/5)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 19 | 20 | 95.0% |
| invoice_date | 20 | 20 | 100.0% |
| vendor_name | 19 | 19 | 100.0% |
| buyer_name | 14 | 17 | 82.4% |
| currency | 12 | 16 | 75.0% |
| subtotal | 19 | 22 | 86.4% |
| tax_total | 16 | 18 | 88.9% |
| total_amount | 26 | 29 | 89.7% |
| **macro** | | | **90.1%** |

line items: TP=85 FP=17 FN=8 P=83.3 R=91.4 F1=0.8718


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 31 | $5.0/1k | $0.1550 |
| contextualization tokens | 31,000 | $1.0/1M | $0.0310 |
| model input tokens (uncached) | 39,019 | $5.0/1M | $0.1951 |
| model input tokens (cached) | 89,856 | $0.5/1M | $0.0449 |
| model output tokens | 11,966 | $30.0/1M | $0.3590 |

**acu total: $0.785** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

