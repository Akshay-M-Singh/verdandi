# Benchmark score — 2026-10-06T16:38:58

truth documents: 100


## verdandi (benchmark/data/results/verdandi/2026-10-06/12)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 72 | 73 | 98.6% |
| vendor_name | 68 | 68 | 100.0% |
| buyer_name | 45 | 46 | 97.8% |
| currency | 47 | 51 | 92.2% |
| subtotal | 74 | 77 | 96.1% |
| tax_total | 56 | 59 | 94.9% |
| total_amount | 96 | 98 | 98.0% |
| **macro** | | | **97.0%** |

line items: TP=235 FP=14 FN=10 P=94.4 R=95.9 F1=0.9514


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 1,093,962 | $0.1/1M | $0.1094 |
| completion tokens | 203,106 | $0.5/1M | $0.1016 |

**verdandi total: $0.2109** (rates: models.dev hint rates for gpt-6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/7)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 69 | 70 | 98.6% |
| invoice_date | 73 | 73 | 100.0% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 43 | 46 | 93.5% |
| currency | 47 | 51 | 92.2% |
| subtotal | 72 | 77 | 93.5% |
| tax_total | 57 | 59 | 96.6% |
| total_amount | 93 | 98 | 94.9% |
| **macro** | | | **96.1%** |

line items: TP=233 FP=11 FN=12 P=95.5 R=95.1 F1=0.953


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 103 | $5.0/1k | $0.5150 |
| contextualization tokens | 103,000 | $1.0/1M | $0.1030 |
| model input tokens (uncached) | 88,882 | $5.0/1M | $0.4444 |
| model input tokens (cached) | 336,896 | $0.5/1M | $0.1684 |
| model output tokens | 38,517 | $30.0/1M | $1.1555 |

**acu total: $2.3864** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

