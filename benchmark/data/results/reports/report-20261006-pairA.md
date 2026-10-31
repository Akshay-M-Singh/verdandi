# Benchmark score — 2026-10-06T06:40:58

truth documents: 100


## verdandi (benchmark/data/results/verdandi/2026-10-06/1)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 72 | 73 | 98.6% |
| vendor_name | 66 | 68 | 97.1% |
| buyer_name | 43 | 46 | 93.5% |
| currency | 46 | 51 | 90.2% |
| subtotal | 75 | 77 | 97.4% |
| tax_total | 56 | 59 | 94.9% |
| total_amount | 96 | 98 | 98.0% |
| **macro** | | | **96.3%** |

line items: TP=235 FP=14 FN=10 P=94.4 R=95.9 F1=0.9514


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 506,803 | $0.2/1M | $0.1014 |
| completion tokens | 192,465 | $1.2/1M | $0.2310 |

**verdandi total: $0.3323** (rates: models.dev hint rates for gpt-5.6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/1)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 69 | 70 | 98.6% |
| invoice_date | 73 | 73 | 100.0% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 43 | 46 | 93.5% |
| currency | 47 | 51 | 92.2% |
| subtotal | 72 | 77 | 93.5% |
| tax_total | 58 | 59 | 98.3% |
| total_amount | 94 | 98 | 95.9% |
| **macro** | | | **96.5%** |

line items: TP=236 FP=11 FN=9 P=95.5 R=96.3 F1=0.9593


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 103 | $5.0/1k | $0.5150 |
| contextualization tokens | 103,000 | $1.0/1M | $0.1030 |
| model input tokens (uncached) | 95,722 | $5.0/1M | $0.4786 |
| model input tokens (cached) | 326,656 | $0.5/1M | $0.1633 |
| model output tokens | 38,195 | $30.0/1M | $1.1459 |

**acu total: $2.4058** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

