# Findings — Thin-Market Liquidity Provision

**Status: NOT REJECTED. The first hypothesis in eight to survive scrutiny.**
**Not the same as "found an edge" — see Limits.**

Run 2026-08-30/31 against live public data. Real money risked: **$0**.
Code: `research/thin_markout.py`. No orders, no keys.

---

## Why this is not a rerun of Friday

Friday tested market making on LIQUID markets, where the spread is ~0.1c so
there is nothing to capture and the only income is liquidity rewards. Those
covered 17% of measured adverse selection. Rejected.

Thin markets are a different object: median spread **1.6c**, p95 **15c** —
16x to 150x wider. The income is the spread itself, not rewards. That
distinction only became visible after the thin-market scan measured those
spreads for the first time.

The default explanation for a wide spread is that it *prices* adverse
selection: nobody quotes tight on "Will Treg Taylor advance from the 2026
Alaska primary" because whoever lifts the offer may know something. Wide
spread and toxic flow are usually the same fact. This measured it.

## Method

Public trade history records the **taker's** side, which determines what the
maker did:

```
taker BUY  at P  ->  maker SOLD   at P  ->  maker gains if price FALLS
taker SELL at P  ->  maker BOUGHT at P  ->  maker gains if price RISES
```

Markout is computed on fills that **actually happened**, against a maker who
actually existed. No simulated fills and no assumed fill rate — the two places
every earlier estimate in this project went wrong.

## Result

Pooled over 89 markets, ~10,000 fills:

| horizon | fills | mean markout | median | share-wtd |
|---|---|---|---|---|
| 1h | 10,269 | **+0.179c** | +0.050c | +0.042c |
| 6h | 9,928 | +0.172c | +0.050c | +0.071c |
| 24h | 9,413 | +0.181c | +0.050c | +0.136c |

**Markout is positive for the maker.** Flow in obscure thin markets is
genuinely uninformed — the opposite of what killed Friday.

Paired per-market (each market's own spread against its own markout), which
is the honest version:

| p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|
| +0.082c | +0.100c | **+0.100c** | +0.471c | +1.071c |

**81 of 83 markets (98%) positive.**

## Limits — read before believing any of this

**1. One-sided flow is severe.** A maker earns half the spread only if filled
on BOTH sides. Measured buy/sell imbalance: median **38.9%**, p75 90.1%, p90
**100%**. At p90 the flow is entirely one-directional — you do not make a
market, you take the other side of everyone and carry inventory to resolution.
Only **30 of 83** markets are round-trippable (<20% imbalance); their median
net is +0.196c/fill.

**2. Queue priority is undemonstrated.** The markout measures trades that were
filled by *somebody*. Capturing them requires being at the front of a queue,
which this study does not establish.

**3. The income is small.** At 10% of flow captured: median **$1.37 per
market-day**; $385/day aggregate only if capital and queue priority exist in
83 markets simultaneously.

**4. Sample is the top of the thin band** — markets sorted by volume
descending, $200–$10k/day.

---

## Bug caught — it produced a confident, wrong, opposite result

The first run reported **−1.039c net per fill** and "thin-market making loses
money." That was wrong.

`data-api/trades?market={cid}` returns trades for **both** outcome tokens,
each tagged with `asset`. Price history is per-token. Scoring NO-token trades
at 0.999 against the YES token's 0.001 history manufactured ±100c artifacts on
**76% of fills**.

It was not caught by the headline, which looked plausible and agreed with the
prior. It was caught because **mean (−1.839c), median (+0.050c), and
share-weighted (+1.864c) disagreed** — three measures of one quantity pointing
three directions is impossible for clean data. After the fix they agree
(+0.179c / +0.050c / +0.042c) and the sign reverses.

A false negative agreeing with the prior is the most dangerous error available,
because nothing about it looks wrong.

## Honest verdict

**Not rejected**, which in this project is new. Two findings are real: markout
is positive, and 98% of markets show positive net per fill.

But per-fill economics are thin (+0.100c) and the round-trip assumption fails
in 64% of markets. The correct next step is forward paper testing with
Friday's existing harness pointed at thin markets — not any conclusion about
money.
