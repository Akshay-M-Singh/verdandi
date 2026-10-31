# Benchmark score — 2026-09-30T20:01:29

truth documents: 100


## verdandi (benchmark/data/results/verdandi/2026-09-30/6)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 69 | 70 | 98.6% |
| invoice_date | 72 | 73 | 98.6% |
| vendor_name | 66 | 68 | 97.1% |
| buyer_name | 44 | 46 | 95.7% |
| currency | 47 | 51 | 92.2% |
| subtotal | 76 | 77 | 98.7% |
| tax_total | 57 | 59 | 96.6% |
| total_amount | 96 | 98 | 98.0% |
| **macro** | | | **97.2%** |

line items: TP=234 FP=13 FN=11 P=94.7 R=95.5 F1=0.9512


### verdandi cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| prompt tokens | 507,968 | $0.2/1M | $0.1016 |
| completion tokens | 193,022 | $1.2/1M | $0.2316 |

**verdandi total: $0.3332** (rates: models.dev hint rates for gpt-5.6-luna (GO subscription quota proxy))


## acu (benchmark/data/results/acu/2026-09-30/5)

| field | correct | scored | accuracy |
|---|---|---|---|
| invoice_number | 68 | 70 | 97.1% |
| invoice_date | 73 | 73 | 100.0% |
| vendor_name | 67 | 68 | 98.5% |
| buyer_name | 42 | 46 | 91.3% |
| currency | 48 | 51 | 94.1% |
| subtotal | 72 | 77 | 93.5% |
| tax_total | 57 | 59 | 96.6% |
| total_amount | 89 | 98 | 90.8% |
| **macro** | | | **95.2%** |

line items: TP=220 FP=29 FN=25 P=88.4 R=89.8 F1=0.8907


### acu cost & tokens

| usage | value | rate | cost |
|---|---|---|---|
| document pages | 103 | $5.0/1k | $0.5150 |
| contextualization tokens | 103,000 | $1.0/1M | $0.1030 |
| model input tokens (uncached) | 30,058 | $0.25/1M | $0.0075 |
| model input tokens (cached) | 392,320 | $0.025/1M | $0.0098 |
| model output tokens | 41,797 | $2.0/1M | $0.0836 |

**acu total: $0.7189** (rates: Azure Retail Prices API (eastus, USD, pulled 2026-09-30) + Foundry GPT-5 mini Global Standard)

