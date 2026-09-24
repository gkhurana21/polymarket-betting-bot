# Findings — Two-Leg Monotonicity Arbitrage (hypothesis 11)

**Status: REAL violations found, with real depth. REJECTED on capital
efficiency — 1.17% annualised.**

Run 2026-09-08 against live books. Real money risked: **$0**.
Code: `research/monotonicity.py`.

---

## Why this was worth testing after hypothesis 10

Exact-score and neg-risk sets died on **fee geometry**, not on absent
violations. Gross profit there is `sum(bid) - 1` — one quantity regardless of
leg count — while the taker fee is charged PER LEG. Six legs of fee is ~2.7c
against violations of 1–3%, so the structure could never pay.

This trade has **two legs**, so fee drag is ~0.5c total and a 1c violation
survives. And nothing pins these prices: "by June" and "by December" are
separate events with no conversion path between them, unlike complement pairs
(split/merge) or neg-risk sets (their own conversion). The constraint is pure
logic, enforced only by somebody noticing.

## The trade

If A implies B (reaching $180k implies reaching $170k), then P(A) ≤ P(B).
Buy YES on the weaker claim B, NO on the stronger claim A:

| outcome | YES(B) | NO(A) | total |
|---|---|---|---|
| A true → B true | 1 | 0 | 1 |
| A false, B true | 1 | 1 | **2** |
| both false | 0 | 1 | 1 |

Payout is always ≥ 1, so `bid(A) > ask(B)` is locked profit.

## Violations are real

268 threshold pairs scanned live. Confirmed inversions in `lastTradePrice`,
verified against executable books:

```
Bitcoin reach $180,000 = 0.019  >  $170,000 = 0.012
Treasury hit 6.0%      = 0.049  >  hit 5.5%  = 0.037
Treasury dip below 1.0%= 0.016  >  below 2.0%= 0.009
```

These are genuine logical inconsistencies in live prices — the first violations
in this project found with meaningful depth behind them (6,000 shares on the
Bitcoin pair).

## Why it still does not pay

| trade | capital | net | annualised |
|---|---|---|---|
| Bitcoin $180k vs $170k | $6,000 | $15.14 | **0.76%** |
| Treasury 6.0% vs 5.7% | $820 | $7.33 | 2.68% |
| Treasury dip 3.5% vs 3.0% | $179 | $3.08 | 5.16% |
| Metamask FDV | $285 | $0.34 | 0.36% |
| **total** | **$7,299** | **$28.56** | **1.17%** |

**The general law this establishes, which explains four prior rejections:**

> A binary market settles at $1. To guarantee $1 back you must post ~$1. So
> the return IS the violation size — cents on a dollar — and it cannot be
> levered. The edge does not scale with capital; the capital requirement
> scales with the edge.

This is why cross-venue paid 9.2%, why exact-score paid nothing, and why this
pays 1.17%. Finding *larger* violations does not help: they occur on thinner
books. The Trump-approval pair showed +19c and 53% annualised on **15 shares**
($2.67), and its direction is ambiguous anyway.

## Bug caught

`DOWN` lacked "or lower" / "or below", so "reach 2.0% **or lower**" matched
`UP` on "reach" and the entire Fed ladder was scored backwards — reporting four
violations that do not exist. The live prices there are correctly ordered
(1.75% = 0.013 < 2.0% = 0.038). `DOWN` now wins whenever both match.

Verified separately that `outcomes[0] == "Yes"` in 400/400 markets, so
`clobTokenIds[0]` is safe to treat as the YES token — the asset-mismatch class
of bug that sign-flipped the thin-market markout study.
