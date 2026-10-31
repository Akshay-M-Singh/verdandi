# Benchmark score — 2026-10-06T16:38:56

truth documents: 100


## verdandi (benchmark/data/results/verdandi/2026-10-06/11)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 72 | 73 | 98.6% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 46 | 46 | 100.0% |
| currency | 46 | 51 | 90.2% |
| subtotal | 75 | 77 | 97.4% |
| tax_total | 56 | 59 | 94.9% |
| total_amount | 96 | 98 | 98.0% |
| **macro** | | | **97.0%** |

line items: TP=237 FP=9 FN=8 P=96.3 R=96.7 F1=0.9654


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 1,116,382 | $0.1/1M | $0.1116 |
| completion tokens | 203,139 | $0.5/1M | $0.1016 |

**verdandi total: $0.2132** (rates: models.dev hint rates for gpt-6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-10-06/6)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 69 | 70 | 98.6% |
| invoice_date | 73 | 73 | 100.0% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 43 | 46 | 93.5% |
| currency | 47 | 51 | 92.2% |
| subtotal | 72 | 77 | 93.5% |
| tax_total | 58 | 59 | 98.3% |
| total_amount | 93 | 98 | 94.9% |
| **macro** | | | **96.3%** |

line items: TP=234 FP=18 FN=11 P=92.9 R=95.5 F1=0.9416


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 103 | $5.0/1k | $0.5150 |
| contextualization tokens | 103,000 | $1.0/1M | $0.1030 |
| model input tokens (uncached) | 159,538 | $5.0/1M | $0.7977 |
| model input tokens (cached) | 266,240 | $0.5/1M | $0.1331 |
| model output tokens | 38,976 | $30.0/1M | $1.1693 |

**acu total: $2.7181** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-10-05): Azure Content Understanding standard pages + contextualization; Foundry Models GPT 5.5 Global Standard (short context) input/cached/output)

