# Benchmark score — 2026-10-06T06:41:00

truth documents: 100


## verdandi (benchmark/data/results/verdandi/2026-10-06/2)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 72 | 73 | 98.6% |
| vendor_name | 65 | 68 | 95.6% |
| buyer_name | 43 | 46 | 93.5% |
| currency | 46 | 51 | 90.2% |
| subtotal | 74 | 77 | 96.1% |
| tax_total | 57 | 59 | 96.6% |
| total_amount | 94 | 98 | 95.9% |
| **macro** | | | **95.8%** |

line items: TP=232 FP=15 FN=13 P=93.9 R=94.7 F1=0.9431


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 506,878 | $0.2/1M | $0.1014 |
| completion tokens | 191,044 | $1.2/1M | $0.2293 |

**verdandi total: $0.3306** (rates: models.dev hint rates for gpt-5.6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/2)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 69 | 70 | 98.6% |
| invoice_date | 73 | 73 | 100.0% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 43 | 46 | 93.5% |
| currency | 47 | 51 | 92.2% |
| subtotal | 72 | 77 | 93.5% |
| tax_total | 57 | 59 | 96.6% |
| total_amount | 94 | 98 | 95.9% |
| **macro** | | | **96.3%** |

line items: TP=236 FP=12 FN=9 P=95.2 R=96.3 F1=0.9574


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 103 | $5.0/1k | $0.5150 |
| contextualization tokens | 103,000 | $1.0/1M | $0.1030 |
| model input tokens (uncached) | 87,018 | $5.0/1M | $0.4351 |
| model input tokens (cached) | 335,360 | $0.5/1M | $0.1677 |
| model output tokens | 38,221 | $30.0/1M | $1.1466 |

**acu total: $2.3674** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

