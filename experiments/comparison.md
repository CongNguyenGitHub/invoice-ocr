# Prompt Experiment Comparison

| Run | PSV | Split | N | name% | total_money% | product_name% | date% | receipt_num% | overall_avg% | Notes | Timestamp |
|-----|-----|-------|---|-------|--------------|---------------|-------|--------------|--------------|-------|-----------|
| 1 | v3.5 | dev | 939/941 | 62.1 | 84.2 | 85.1 | 75.0 | 85.2 | **89.1** | baseline | 2026-04-21T23:06 |
| 2 | v3.6 | dev | 940/941 | 74.7 | 81.0 | 91.6 | 78.9 | 81.2 | **89.9** | date verbatim, name full branch, receipt_number fix,total_money digit trap, cashier diacritics | 2026-04-21T23:47 |
| 3 | v3.7 | dev | 940/941 | 74.9 | 80.9 | 91.9 | 79.0 | 81.6 | **90.2** | fix BHX pos_id/name conflict, Lotte receipt adaptive, Satra total smallest-wins | 2026-04-22T17:53 |
