# Cross-venue divergence — Polymarket vs Kalshi

**Status: measured. Real gaps exist, but they do not pay enough to be worth
taking. Observation only; no account, no capital, nothing traded.**

---

## Why this was worth testing

Three earlier hypotheses died inside Polymarket, two of them to
Polymarket-internal mechanisms. In particular, complement arbitrage is floored
by the CTF: anyone can split $1 USDC into 1 YES + 1 NO, so the pair can never
trade below $1.

**No equivalent mechanism links Polymarket and Kalshi.** Separate exchanges,
separate collateral, separate settlement. Nothing forces their prices to agree
except slow, capital-constrained, jurisdictionally-gated arbitrage. The same
structural reasoning that closed complement arb *predicts* cross-venue gaps can
persist — so the hypothesis was mechanism-first, not backtest-first.

## The arb condition

Hedging across venues means buying YES on one and NO on the other. Buying NO at
price q is buying at (1 − bid_YES) on that venue, so:

```
cost   = ask_YES(A) + (1 − bid_YES(B))
profit  ⟺  cost < 1  ⟺  ask_YES(A) < bid_YES(B)
```

One venue's best ask strictly below the other's best bid.

## Matching is the hard part, not pricing

The first matcher reported a **+0.937 "arbitrage"**:

> PM: *"Will the Democrats **win** the Pennsylvania governor race in 2026?"* — 0.95
> KL: *"**Closest** Governor race in 2026? Pennsylvania"* — 0.001

Same year, same state, same nouns, **completely different question**. Token
overlap cannot see the difference; the claim words can. Adding a predicate
constraint (`win` / `closest` / `before` / `above` … must agree on both sides)
cut 177 candidates to 27, and near-identical token sets to 18.

This is the characteristic way cross-venue prediction arb fails. A pair that
resolves on different criteria is not a hedge — it is two directional bets that
look like one, and you find out at settlement.

## Result

18 high-confidence pairs (13 with identical token sets):

| | |
|---|---|
| positive gross spread | 8 / 18 |
| gross spread | min 0.2c, median 1.0c, **max 4.1c** |
| **positive after fees** | **4 / 18** |
| best net | **+3.16c** |

Fees modelled as `C × rate × p × (1−p)` per leg — Polymarket 0.05 (documented),
Kalshi 0.07 (assumed, needs verification).

**Then capital lock-up decides it.** These pairs settle in late 2026 / early
2027, so the capital is committed for months:

| net | capital | resolves | months | annualised |
|---|---|---|---|---|
| 3.16c | $0.959 | 2027-01-01 | 4.1 | **9.9%** |
| 2.89c | $0.950 | 2026-12-31 | 4.1 | **9.2%** |
| 0.43c | $0.990 | 2026-10-31 | 2.1 | 2.5% |
| 0.05c | $0.990 | 2026-10-31 | 2.1 | 0.3% |

## Verdict

The best genuinely-hedged pair on the board pays **~10% annualised**, and only
two of eighteen clear 9%. Against that you carry:

* **Resolution-criteria risk** — the dominant danger. Both venues must define
  "recognize Palestine" identically. If they do not, the position is not hedged
  and the loss is far larger than 3c.
* **Capital on two venues**, unnettable, for months.
* **Execution risk** on two legs across two systems.
* **Top-of-book only** — 4.1c may exist for a handful of shares; depth was not
  measured.
* **Jurisdiction** — Kalshi is CFTC-regulated and requires US KYC. Not
  accessible from India, where Polymarket is also ISP-blocked.

A T-bill pays 4–5% risk-free with none of that. Ten percent gross, before any
of the above goes wrong, is not compensation for the risk — and the two pairs
carrying it are a sample of two.

**Not a dead end in the way the previous three were** — the gaps are real and
the mechanism permitting them is real. But it does not clear the bar, and the
jurisdictional wall makes it moot regardless.

## Reusable

* `fetch.py` — public market data from both venues, no auth
* `match.py` — strict matcher: exact numeric fingerprint + predicate agreement
  + token similarity
* `analyse.py` — spread, fee, and annualised-return scoring

If Kalshi ever becomes accessible, the honest next step is measuring
**persistence and depth** on identical-token pairs, not adding more matches.
