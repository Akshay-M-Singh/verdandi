# Benchmark score — 2026-10-06T13:49:02

truth documents: 30


## verdandi (benchmark/data/results/verdandi/2026-10-06/4)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 19 | 20 | 95.0% |
| invoice_date | 19 | 20 | 95.0% |
| vendor_name | 19 | 19 | 100.0% |
| buyer_name | 16 | 17 | 94.1% |
| currency | 12 | 16 | 75.0% |
| subtotal | 20 | 22 | 90.9% |
| tax_total | 17 | 18 | 94.4% |
| total_amount | 26 | 29 | 89.7% |
| **macro** | | | **91.9%** |

line items: TP=81 FP=15 FN=12 P=84.4 R=87.1 F1=0.8571


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 345,973 | $0.1/1M | $0.0346 |
| completion tokens | 63,293 | $0.5/1M | $0.0316 |

**verdandi total: $0.0662** (rates: models.dev hint rates for gpt-6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/4)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 19 | 20 | 95.0% |
| invoice_date | 20 | 20 | 100.0% |
| vendor_name | 19 | 19 | 100.0% |
| buyer_name | 13 | 17 | 76.5% |
| currency | 13 | 16 | 81.2% |
| subtotal | 19 | 22 | 86.4% |
| tax_total | 17 | 18 | 94.4% |
| total_amount | 27 | 29 | 93.1% |
| **macro** | | | **91.3%** |

line items: TP=84 FP=17 FN=9 P=83.2 R=90.3 F1=0.866


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 31 | $5.0/1k | $0.1550 |
| contextualization tokens | 31,000 | $1.0/1M | $0.0310 |
| model input tokens (uncached) | 27,759 | $5.0/1M | $0.1388 |
| model input tokens (cached) | 100,096 | $0.5/1M | $0.0500 |
| model output tokens | 11,911 | $30.0/1M | $0.3573 |

**acu total: $0.7322** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

