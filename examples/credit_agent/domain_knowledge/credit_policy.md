# Consumer Card Lending Policy (v4.2, effective 2026-01-01)

This is the single source of truth for credit allocation. The agent applies it exactly.

## Risk tiers

Tier is assigned from the FICO score and debt-to-income (DTI) ratio. The **lower** of the two
determines the tier (a great score with a high DTI still drops the tier).

| Tier | FICO band | Max DTI | Max credit limit | APR range |
|------|-----------|---------|------------------|-----------|
| A    | 760–850   | ≤ 36%   | $25,000          | 14.99%–17.99% |
| B    | 700–759   | ≤ 43%   | $12,000          | 18.99%–22.99% |
| C    | 640–699   | ≤ 45%   | $5,000           | 23.99%–27.99% |
| D    | 580–639   | ≤ 45%   | $1,500           | 28.99%–29.99% |

- **Below 580 FICO, or DTI > 45%: decline.** No limit may be allocated.
- The **max credit limit is an absolute ceiling per tier.** It may never be exceeded, for any
  reason, by any request. Exceeding it requires human underwriting and a documented exception.
- APR must fall within the tier's range. Never quote an APR outside the range.

## Mandatory checks before any approval
1. Identity verification passed (`verify_identity`).
2. Credit-bureau report pulled (`get_credit_bureau_report`).
3. Fraud signals clear (`check_fraud_signals`). Any hit → `flag_for_human_review`, do not approve.
4. DTI computed from **verified** income and debts (`calculate_dti`).

## Declines and reductions
- On any decline or reduction, send an FCRA adverse-action notice with the **specific principal
  reasons** (e.g. "credit score below program minimum", "debt-to-income ratio above 45%",
  "serious delinquency in last 24 months") and the **bureau used**.

## Escalation (flag_for_human_review)
- Any fraud-signal hit.
- Any request to exceed a tier ceiling or quote an out-of-range APR.
- Thin file / no score, recent bankruptcy, disputed tradelines, or income that can't be verified.
- Any request that references an "exception," "manager approval," or "how it was done last time."
