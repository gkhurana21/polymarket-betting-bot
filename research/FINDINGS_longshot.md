# Findings — Favourite-Longshot Bias

**Status: REJECTED. Polymarket's prices are well calibrated. No tradeable bias.**

Study run 2026-08-29 against the recorded corpus. Real money risked: **$0**.
Code: `research/longshot_study.py`. No network calls, no orders, read-only DB.

---

## Why this hypothesis was worth testing

Favourite-longshot bias — longshots trading above their true frequency — is the
most robustly documented anomaly in betting markets. It was the first hypothesis
in this project whose known failure modes did **not** already apply:

| prior rejection | why it did not apply here |
|---|---|
| whale copying — information is priced before we can act | this edge needs no information |
| complement arb — split/merge floors YES+NO at $1 | we buy one side; nothing pins it |
| market making — adverse selection exceeds rewards | we are deliberately the taker |
| cross-venue — capital locked to resolution | median hold here is ~21 hours |

Polymarket's fee curve actively favours it. `fee = C × rate × p × (1−p)` is
*maximised* at p=0.5 and vanishes at the extremes — at p=0.97 it is 0.145¢ per
share. Every prior hypothesis died on costs. This one was structurally cheap.

---

## Result

**2,379 resolved markets** with executable books, one observation per market,
6h buffer, spread ≤ 5c. Base rate 24.8% YES.

Realised frequency tracked the ask price within a few points in nearly every
bucket, with no monotone pattern in either direction:

| YES ask | n | mean ask | realised | edge |
|---|---|---|---|---|
| 0.02–0.05 | 335 | 0.032 | 0.027 | −0.006 |
| 0.05–0.10 | 288 | 0.070 | 0.062 | −0.008 |
| 0.10–0.20 | 390 | 0.138 | 0.146 | +0.009 |
| 0.20–0.35 | 381 | 0.265 | 0.210 | **−0.055** |
| 0.35–0.50 | 330 | 0.423 | 0.439 | +0.017 |
| 0.50–0.65 | 270 | 0.563 | 0.563 | +0.000 |
| 0.65–0.80 | 91 | 0.710 | 0.670 | −0.039 |
| 0.80–0.90 | 33 | 0.840 | 0.879 | +0.039 |

That is what a calibrated market looks like. The small negative bias everywhere
is the half-spread we pay by crossing.

## The one candidate, and how it died

`YES 0.20–0.35` was negative in **both** halves of a chronological split
(−21.2%, then −25.7% at t=−2.63) — the only stable result out of ~21 bucket
tests. Its mirror, **buy NO at 0.65–0.80**, was positive in both (+3.33%,
+2.55%), pooled at **+2.79%, t=0.91**.

Capacity was genuinely there — unlike every prior hypothesis. Median $1,119
resting at top of book; at a $1,000 target we would still deploy $678 average
across 355 markets. Capacity was not the constraint. Power was.

It dissolves under every slice:

| horizon | 6–12h | 12–24h | 1–3d | 3–14d |
|---|---|---|---|---|
| ROI | +3.32% | −2.65% | +12.54% | −1.39% |

| corpus quartile | Q1 | Q2 | Q3 | Q4 |
|---|---|---|---|---|
| ROI | +1.92% | +3.31% | +9.55% | −3.49% |

Sign flips in both. A structural bias appears at every horizon and in every
period; that is what makes it structural. At the observed capture rate of 4.11
qualifying markets/day, reaching t=1.96 would need **616 more days**.

## Resolution lag — also nothing

The 6h buffer excluded the window where an outcome might be publicly determined
but not yet priced. Checked directly: buying the favourite in the final hours
returns **−2.7% (1–3h), −9.4% (3–6h), −9.2% (6–12h)**. Mean absolute pricing
error in the last hour is 0.184 — prices reflect real uncertainty, not staleness.

---

## Bugs found (both would have manufactured a fake edge)

**1. Wide books priced as favourites.** The first run reported 25 markets at a
mean ask of 0.993 that resolved YES only 68% of the time — t=−3.31, a
zero-probability event if the price were honest. The eight failures had books
like **bid 0.001 / ask 0.998**: one stub order on each side of an empty book.
Technically executable, economically meaningless. Only 2.2% of markets are that
wide, but they cluster in exactly the extreme buckets the study is about.
Fixed with a `max_spread` filter; the fake t vanished.

This is the same class of error as using mid-price as an executable fill — the
mistake that turned whale-copying from +21.8% into −18.1%.

**2. Compounded annualisation.** `annualise()` printed **+2,270,324%** for a
bucket with t=0.91. Compounding a mean that cannot be distinguished from zero
manufactures a number with no meaning. Replaced with simple turnover scaling.

---

## What this means

Five hypotheses have now been tested to destruction. The collective finding is
not "we keep failing" — it is more specific and more useful:

> **Polymarket is efficient at the level observable from public book data.**

Copy-trading fails because information arrives priced. Complement arb fails
because the mint mechanism enforces parity. Market making fails because the
reward pool is smaller than adverse selection. Longshot bias fails because the
prices are simply right. Cross-venue is the sole case where a real gap exists —
and it pays 9.9% annualised behind a KYC wall.

Every edge measurable from the public book has been arbitraged away or was
never there. A sixth hypothesis drawing on the same data should be expected to
fail for the same reason. Beating this venue requires an input we do not have:
better information, faster execution, or a resolution-criteria view — not
another way of rearranging public prices.
