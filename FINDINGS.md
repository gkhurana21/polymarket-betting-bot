# Findings — Polymarket Whale Copy-Trading

**Status: CONCLUDED. The strategy does not work. Do not trade it live.**

Project ran 2026-05-23 → 2026-08-02 (~71 days). Real money risked: **$0**.

---

## Verdict

Copy-trading Polymarket whales from the public data API **has no exploitable edge.**
This was tested to destruction across four independent lines of evidence, each of
which independently rejects the strategy.

| Test | Sample | Result |
|---|---|---|
| Paper: copy all whale signals | 21,243 resolved | **+0.7% ROI** (noise; −2.9% out-of-sample) |
| Paper: "best" mid-band + open signals | 690 OOS | +7.5% — later shown to be an artifact |
| Live-path forward test, realistic fills | 35 settled | **−18.1% ROI, t ≈ 0** |
| Filled at the whale's *own* price | 26 | **−22.9% ROI** |

---

## Why it fails

### 1. Win rate is not profit
`copy_whale` won **59%** of bets and still lost money. The high win rate came from
heavy favorites (entry > 0.80, 93.5% win) where a win pays pennies and one loss
erases twenty. ROI by entry-price bucket:

| Entry price | Win % | ROI |
|---|---|---|
| < 0.30 | 12.3% | −10.0% |
| 0.30–0.60 | 43.0% | **+15.5%** |
| 0.60–0.80 | 71.2% | −14.9% |
| > 0.80 | 93.5% | −1.3% |

Only the mid band looked profitable — which led to the mid-band strategy, which
then failed for the reasons below.

### 2. The apparent edge was a measurement artifact
The dry-run assumed every order filled at the whale's own price. Measured against
real order books, the obtainable price was on average **4.08 cents worse** (worse in
53% of orders). Recomputing at real prices dropped the forward test from **+21.8%
to +4.8%**, and modeling fills properly took it to **−18.1%**.

*Lesson: an optimistic fill assumption manufactures alpha that does not exist.*

### 3. Speed is NOT the binding constraint — do not rebuild for latency
Detection latency is p50 ≈ 47 min, but that lag is **entirely Polymarket's public
data-api**; our poller completes 62s cycles exactly as configured. The tempting
conclusion — "go on-chain for ~2s detection" — is wrong:

| Execution quality | n | ROI |
|---|---|---|
| Filled **better** than whale's price | 9 | −0.2% |
| Filled at **~the whale's price** | 26 | **−22.9%** |

Executing at the whale's exact price is the theoretical best case for *any*
zero-latency detector, and it still loses. **Do not build an on-chain or websocket
watcher hoping to recover the edge.**

### 4. Adverse selection
Only **29%** of signals fill (38 of 129 attempts). We fill precisely when someone is
willing to sell to us — which skews toward the trades we should not be taking. The
71% that don't fill are the ones where the price ran away, i.e. the good ones.

---

## Bugs found (each would have cost real money)

1. `OrderArgs.side` set to `"YES"/"NO"` — must be `BUY`/`SELL`.
2. `OrderArgs.size` passed as USD — must be **shares** (`usd / price`).
3. `OrderArgs(order_type=…)` — kwarg does not exist in the SDK; `TypeError` on first live order.
4. Daily-drawdown guard counted **capital deployed** as realised loss — one max-size bet tripped the halt and blocked 687/688 valid intents.
5. **NO-side blindness** — executor read `no_token_id` only from the scanner cache, which was never populated for whale-traded markets. 131 fresh NO signals → 0 orders. The forward test was YES-only for its first 18 days.
6. Optimistic fill assumption (see §2) — the one that invented the edge.

Every one of these was caught in dry-run.

---

## What is reusable

The infrastructure is sound and hypothesis-independent:

- `bot/client.py` — retry + circuit-breaker CLOB wrapper
- `bot/tracker.py` — SQLite persistence, WAL, audit log, settlement
- `bot/resolution_poller.py` — market resolution + P&L settlement
- `bot/risk.py` — position/drawdown/duplicate/size guards
- `bot/executor.py` — realistic fill modeling (`_simulate_fill`)
- Four-stream paper-trading harness with a null baseline
- Out-of-sample time-split methodology

What it needs is a different **hypothesis**. "Follow someone else's trades" fails
because that information is already in the price before any outside observer can
act on it.

---

## Operational state at conclusion

- The observation daemon is stopped.
- `dry_run: true` throughout. `PRIVATE_KEY` never set. No wallet funded.
- `data/trades.db` is kept locally and is not in the repo: 82k+ paper trades and 140 settled dry-run orders on the
  live path: 105 from before the realistic-fill fix and the 35 after it that make up the clean forward test above.
- Four more of the post-fix orders settled after this write-up. All 39 together: −25.1% ROI (checked 2026-11-19). The
  verdict does not change.
