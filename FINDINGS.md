# Findings — Friday (liquidity-rewards market maker)

**Status: CONCLUDED. The strategy does not work. Do not fund it.**

Real money risked: **$0**.

---

## Verdict

Market-making for Polymarket liquidity rewards **loses money**. The reward
income is real and the mechanism is genuine, but it does not cover the cost of
being filled.

Replaying the quoting engine over **211,465 recorded order books**, scoring the
scanner's own top picks against their own measured cost:

| over 1,432.6 market-days | |
|---|---|
| reward income | **$337.79** |
| measured cost | **$1,944.72** |
| **net** | **−$1,606.93**  (−$1.12 / market-day) |

**Reward income covers 17.4% of cost.** 7 of 30 markets were net-positive, but
the distribution is sharply skewed: winners were +$0.93 to +$16.88, losers were
−$171, −$235, −$296. Small gains, occasional large losses — the signature of
adverse selection.

---

## Why it fails

Cost per market-day, measured:

| | |
|---|---|
| adverse selection | −$0.3260 |
| inventory mark-to-market | −$0.2863 |
| favourable fills | +$0.0201 |
| maker rebates | +$0.0349 |
| **net cost** | **−$0.5574** |

That sets a bar: rewards must supply ~$0.56/market-day. What share of a pool
does that need?

| pool | share needed |
|---|---|
| $10/day | 5.57% |
| $40/day | 1.39% |
| $88/day | 0.63% |

Measured share for 400 resting shares: **0.16%** on a median liquid book. We
reach 36–51% only on *thin* books — which are exactly the ones the scanner
rejects as toxic (`max_our_book_share`).

**The strategy needs a pro-rata share it can only obtain where it is most
likely to be picked off.** That is not a tuning problem; it is the shape of the
opportunity.

---

## Method, and its limits

Friday could always project its income — liquidity rewards accrue for *resting*,
no fill required. It could never measure its cost, because adverse selection
only appears on fills and dry-run records none by design.

**Why simulating maker fills is safe where taker fills were not.** For a taker,
assuming a fill is the *optimistic* error: it hands you the trade you wanted at
the price you wanted. That is exactly what turned a measured +21.8% into −18.1%
in the predecessor project. For a maker it is *pessimistic*: a fill is not a
prize, it is someone handing you inventory you did not ask for.

**The fill rule.** Snapshots are ~10 minutes apart, so trades are not directly
observable. The obvious rule — "our bid is no longer the best bid, so it was
taken" — is wrong, because the recorded book never contained our order. Once the
volatility estimate settles, Friday quotes *inside* the observed spread, so the
recorded best bid sits below ours in every interval and the rule fires
unconditionally. It reported a fill on a completely static book. Caught by test.

The rule now uses the counterparty side, which our absence does not distort:

    best_ask(t+1) <= our_bid   =>  our bid was hit
    best_bid(t+1) >= our_ask   =>  our ask was lifted

**Limits, stated plainly:**

* 10-minute spacing hides intra-interval fills, so both the adverse cost and the
  rebate income are **undercounts**.
* The rule detects crossings that *persist* (adverse) and misses those that
  *revert* (favourable), so the fill mix is **pessimistic by construction**.
* Per-market rates are unstable when eligible time is short; aggregate figures
  sum dollars and days separately and divide once.

The undercounting cuts toward flattering the strategy, and it still loses.

---

## What was built and is reusable

* `marketmaker.py` — mark estimation, toxicity guard, inventory skew, markout,
  volatility floor (ported from `mm-harness`)
* `scanner.py` — reward-efficiency ranking (pool ÷ resting depth) with a
  book-share safety gate
* `replay.py` — conservative maker-fill replay over historical books
* `inventory.py` / `guard.py` — portfolio caps, kill switches, the rolling
  reward-vs-inventory halt
* `lock.py` — single-instance enforcement
* `dashboard.py` — live table + force-directed architecture graph, stall detection

The infrastructure is sound. The hypothesis is not.

---

## Operating state at conclusion

* `dry_run: true` throughout. No `PRIVATE_KEY`. No wallet funded.
* 8 test suites pass.
