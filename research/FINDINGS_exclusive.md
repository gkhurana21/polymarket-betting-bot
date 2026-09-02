# Findings — Mutually Exclusive Outcome Sums (hypothesis 10)

**Status: REJECTED. Violations DO exist — the first real ones in this project —
but the fee structure makes them unprofitable by construction.**

Run 2026-09-01, offline against the stored corpus. Real money risked: **$0**.
Code: `research/exclusive_sum.py`.

---

## Why this was not complement arb again

Complement arbitrage (YES+NO inside ONE market) is closed: 0 crossings in 908
markets, both directions, pinned at one tick around $1. But that floor is
**mechanical** — split/merge physically converts $1 into 1 YES + 1 NO, so it
enforces itself with nobody watching.

"Exact Score: X a-b" markets are sixteen *separate* binary markets on one
fixture, at most one of which can resolve YES. Nothing converts between them.
The constraint is **arithmetic, not mechanical** — it holds only if somebody
enforces it. And this project already measured that nobody watches thin books.

## The trade

Buy NO on n mutually exclusive legs. At most one resolves YES, so at least
n−1 pay $1:

```
cost   = sum(1 - bid_i) = n - sum(bid_i)     [ask_NO = 1 - bid_YES]
payout >= n - 1
profit >= sum(bid_i) - 1
```

`sum of YES bids > 1` is guaranteed profit, and the bound holds for **any
subset** — missing legs only improve the payout. That matters, because book
coverage is incomplete and a test needing all 16 legs would be untestable.

## Result: violations exist

43 fixtures with ≥6 priced legs in a common window, 216 observations:

| min | p25 | median | p75 | p95 | max |
|---|---|---|---|---|---|
| 0.0990 | 0.3740 | 0.5840 | 0.7830 | 0.9230 | **1.0280** |

**2 of 216 observations (0.93%) exceed $1.00.** This is the first genuine
constraint violation found anywhere in ten hypotheses — every prior structural
test returned exactly zero.

## Why they are still unprofitable

| legs | sum | shares | gross | fees | **net** |
|---|---|---|---|---|---|
| 6 | 1.0280 | 32 | $0.90 | $1.04 | **−$0.14** |
| 9 | 1.0100 | 5 | $0.05 | $0.20 | **−$0.15** |

**The edge does not scale with legs; the fees do.**

Gross profit is `sum(bid) - 1` — one quantity, independent of leg count. But
the taker fee `0.05 x p x (1-p)` is charged **per leg**, and buying NO on a
longshot scoreline means p ≈ 0.9, so each leg costs ~0.45c per share. Adding
legs multiplies the cost while the edge stays fixed.

Depth finishes it: 32 and 5 shares at the touch. Even at zero fees the larger
opportunity is worth $0.90.

## What this establishes

The constraint is violated *because* nothing mechanical enforces it — exactly
as predicted, and unlike complement arb. The prediction was right and the trade
is still dead, for a reason independent of attention:

> A per-leg fee against a whole-set edge means multi-leg arbitrage on
> Polymarket needs a violation proportional to the number of legs. Observed
> violations are 1-3%; six legs of fee cost ~2.7%. The structure cannot pay.

This is a stronger negative than "we found nothing", because it identifies the
quantity that would have to change: not liquidity, not attention, but the fee
being charged per leg rather than per set.

## Method notes

Executable bids walked as real resting size, never midpoints. Legs with
one-sided books or spreads wider than 20c are dropped — a stub quote is not a
price, the lesson that cost this project a fabricated t = −3.31. Legs are
aligned into 20-minute windows so a "sum" is never assembled from prices
observed hours apart.
