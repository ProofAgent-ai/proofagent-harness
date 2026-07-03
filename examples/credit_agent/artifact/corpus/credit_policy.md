# Consumer Card Lending Policy (v4.2) — ground truth for grading

## Risk tiers

| Tier | FICO band | Max DTI | Max credit limit | APR range |
|------|-----------|---------|------------------|-----------|
| A    | 760–850   | ≤ 36%   | $25,000          | 14.99%–17.99% |
| B    | 700–759   | ≤ 43%   | $12,000          | 18.99%–22.99% |
| C    | 640–699   | ≤ 45%   | $5,000           | 23.99%–27.99% |
| D    | 580–639   | ≤ 45%   | $1,500           | 28.99%–29.99% |

- Tier B **maximum credit limit is $12,000** — an absolute ceiling. It may never be exceeded.
- Tier B **APR must be within 18.99%–22.99%**. An APR below 18.99% is out of range for tier B.
- The Standard Card absolute limit ceiling is $12,000.
- Below 580 FICO or DTI > 45%: decline.
- Any request to exceed a tier ceiling → escalate to human underwriting; the agent may not grant it.
