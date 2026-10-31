# Benchmark score — 2026-09-30T20:01:20

truth documents: 100


## verdandi (benchmark/data/results/verdandi/2026-09-30/5)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 72 | 73 | 98.6% |
| vendor_name | 65 | 68 | 95.6% |
| buyer_name | 45 | 46 | 97.8% |
| currency | 46 | 51 | 90.2% |
| subtotal | 75 | 77 | 97.4% |
| tax_total | 55 | 59 | 93.2% |
| total_amount | 95 | 98 | 96.9% |
| **macro** | | | **96.1%** |

line items: TP=232 FP=13 FN=13 P=94.7 R=94.7 F1=0.9469


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 508,089 | $0.2/1M | $0.1016 |
| completion tokens | 190,851 | $1.2/1M | $0.2290 |

**verdandi total: $0.3306** (rates: models.dev hint rates for gpt-5.6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-09-30/4)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 73 | 73 | 100.0% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 42 | 46 | 91.3% |
| currency | 47 | 51 | 92.2% |
| subtotal | 73 | 77 | 94.8% |
| tax_total | 57 | 59 | 96.6% |
| total_amount | 89 | 98 | 90.8% |
| **macro** | | | **95.2%** |

line items: TP=222 FP=29 FN=23 P=88.4 R=90.6 F1=0.8952


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 103 | $5.0/1k | $0.5150 |
| contextualization tokens | 103,000 | $1.0/1M | $0.1030 |
| model input tokens (uncached) | 18,538 | $0.25/1M | $0.0046 |
| model input tokens (cached) | 403,840 | $0.025/1M | $0.0101 |
| model output tokens | 41,552 | $2.0/1M | $0.0831 |

**acu total: $0.7158** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-09-30) + Foundry GPT-5 mini Global Standard)

