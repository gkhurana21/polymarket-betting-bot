# Polymarket Betting Bot

> **Disclaimer:** This software is for research and paper-trading only. Prediction markets involve financial risk. You can lose money. Phase A never places live orders, but misconfigured Phase B deployment can. Keep `dry_run: true` until you have passed every gate in [A→B transition](#ab-transition-checklist). Nothing here is financial advice.

A disciplined **copy-trading research rig** for [Polymarket](https://polymarket.com): observe top traders, run four parallel paper-trading strategies, and only consider live trading after statistical gates pass.

Full architecture and schema: see [`PLAN.md`](PLAN.md).

---

## Phase A vs Phase B

| | **Phase A — Observation** (build now) | **Phase B — Live trading** (gated) |
|---|---|---|
| **Purpose** | Collect data; measure whether copying whales beats random chance | Place real orders when data proves edge |
| **`PRIVATE_KEY`** | Not used — never loaded into memory | Required (Polygon wallet) |
| **`dry_run: true`** | Correct setting — paper trades only | Still useful for ongoing comparison |
| **`dry_run: false`** | **Refused at startup** with a loud error | Enables live CLOB orders |
| **What runs** | `serve` daemon: whale poll, market scan, paper streams, resolution | + `strategy`, `risk`, `executor` |
| **Exit criteria** | ≥14 days data + readiness gate + decision matrix verdict | Manual review of blacklist candidates |

**Hard rule:** Do not flip `dry_run: false` or build Phase B until `python main.py status` shows readiness all-green **and** decision matrix is `BUILD_B_RELAXED` or `BUILD_B_FULL`.

---

## The four paper-trade streams

Every whale signal (`open` / `add` by default) fans out to four strategies logged side-by-side. Same `triggering_signal_id` so you can compare apples-to-apples.

| Stream | What it tests | Entry |
|--------|---------------|-------|
| **`copy_whale`** | Does following the whale's **direction** work? | Immediate, whale's side and fill price, scaled size |
| **`stub_blend`** | Placeholder for Phase B blend formula (fixed α/β/γ weights) | Immediate at latest market mid |
| **`null_random_side`** | **Control** — random YES/NO (seeded by `market_id`, deterministic) | Immediate at latest mid |
| **`whale_side_random_timing`** | Is **timing** informative if direction is held? | Random delay 0–24 h, then enter at fresh mid |

After ≥50 resolved trades, a **decision matrix** compares `copy_whale` vs `null_random_side` (and timing). Verdicts: `HALT`, `INCONCLUSIVE`, `BUILD_B_RELAXED`, `BUILD_B_FULL`.

---

## Quickstart

### 1. Clone and install

```bash
git clone <your-repo-url> polymarket-bot
cd polymarket-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment (Phase A)

```bash
cp .env.example .env
```

Fill in **read-only** CLOB credentials (optional for public endpoints, recommended for rate limits):

```env
CLOB_API_KEY=...
CLOB_API_SECRET=...
CLOB_API_PASSPHRASE=...
```

**Do not set `PRIVATE_KEY` in Phase A.** Leave it commented out in `.env`.

### 3. Verify config

Ensure `config.yaml` has:

```yaml
dry_run: true    # must stay true in Phase A
phase: A
```

### 4. One-shot market scan

```bash
python main.py scan
```

Example output:

```text
Scan complete: fetched=120 passed=45 snapshots=45 books=45 errors=0
```

### 5. Start the observation daemon

```bash
python main.py serve
```

Runs until Ctrl+C or SIGTERM:

- **whale_poll** (60 s) — poll tracked wallets, classify signals
- **market_scan** (10 min) — snapshots + book depth + pending-trade sweep
- **resolution_poll** (30 min) — settle paper trades, update whale trust
- **paper_consumer** (async) — fan out signals to four streams

**Daemon health logs** (grep these during a 2-week run):

```text
whale_poll cycle complete — 3 signals emitted, 1 skipped (stale), 842ms
market_scan cycle complete — 118 markets scanned, 42 filtered in, 12450ms
resolution_poll cycle complete — 2 markets resolved, 4 trust updates, 3201ms
paper_consumer summary — 12 signals consumed, 36 paper trades created, 3 pending
```

### 6. Check progress (anytime)

```bash
python main.py status
```

---

## CLI reference

| Command | Description |
|---------|-------------|
| `scan` | One-shot market fetch + filter + snapshots |
| `whales` | Trust scorecard (`⚠️` for below-threshold candidates) |
| `whale-feed [--follow] [--limit N]` | Recent whale signals |
| `status` | Readiness counters + decision matrix (if ready) |
| `pnl --days N --by-source` | P&L by stream and per-whale |
| `add-whale ADDR --label X --trust 0.9` | Manual whale + Beta seed |
| `backtest` | Stream stats (requires ≥7 days data) |
| `serve` | Run Phase A daemon |
| `run` | **Refused** — Phase B only |

### `python main.py status`

```text
Readiness: 47/50 trades · 4/5 whales ready · 12/14 days
  ✗ resolved_trades: 47/50
  ✗ whales_with_5_trades: 4/5
  ✗ calendar_days: 12.0/14 days

Decision matrix: deferred until readiness gate is all-green
```

When all checks pass:

```text
Readiness: 52/50 trades · 6/5 whales ready · 15/14 days
  ✓ resolved_trades: 52/50
  ...

Decision matrix: BUILD_B_RELAXED
  BUILD_B_RELAXED: copy beats null (gap +12.0pp, p=0.008); timing ≈ copy (p=0.412 > 0.10)
  copy 35/52 (67.3%) vs null 22/52 (42.3%) [z_test p=0.008]
```

### `python main.py whales`

Table of `α`, `β`, trust mean, resolved count. Whales below `whale_perf.blacklist_threshold` (default 0.35) show `⚠️ below threshold`. **Phase A still polls them** — flag only, no removal.

### `python main.py add-whale 0xabc... --label "TopTrader" --trust 0.9`

Writes `data/whales_manual.json` and seeds Beta(9, 1) in `whale_trust`.

### `python main.py pnl --days 7 --by-source`

Per-stream win rate and P&L, plus per-whale breakdown for `copy_whale`.

### `python main.py backtest`

Refuses if fewer than 7 calendar days of resolved data. Otherwise prints all-time stream P&L and matrix verdict if readiness is met.

---

## A→B transition checklist

Do **not** skip steps. The infrastructure cost of two more weeks of Phase A is zero; the cost of live trading on a coin-flip edge is large.

### 1. Readiness gate (quantity)

`python main.py status` — all three must be green:

| Check | Threshold |
|-------|-----------|
| Resolved paper trades (`YES`/`NO` only) | ≥ 50 |
| Distinct whales with ≥5 resolved trades each | ≥ 5 |
| Calendar days of data | ≥ 14 |

### 2. Decision matrix (quality)

Still on `status` when readiness is green:

| Verdict | Meaning | Action |
|---------|---------|--------|
| **HALT** | Null control wins or ties | Do **not** build Phase B |
| **INCONCLUSIVE** | Edge may exist but underpowered (gap &lt;5pp or p&gt;0.10) | Extend Phase A ~14 more days |
| **BUILD_B_RELAXED** | Direction matters; timing doesn't | Phase B with relaxed latency |
| **BUILD_B_FULL** | Direction **and** timing matter | Phase B with low-latency execution |

### 3. Manual blacklist review

```bash
python main.py whales
```

Review every `⚠️ below threshold` whale. Phase A **never** auto-removes them from polling (that would corrupt the dataset). At A→B transition, **manually** decide which flagged whales to exclude from the live universe after reading their full history.

### 4. Phase B deployment (later)

1. Run calibration parameter fit → `data/calibration.yaml`
2. Implement / enable Phase B modules (`strategy`, `risk`, `executor`)
3. Set `PRIVATE_KEY` in `.env`
4. First run with `dry_run: true` for ≥24 h
5. Only then `dry_run: false` with small bankroll

---

## Data layout

| Path | Purpose |
|------|---------|
| `data/trades.db` | SQLite — all signals, trades, trust |
| `data/audit.log.jsonl` | Append-only forensic log |
| `data/state.json` | `last_seen_ts` per wallet (crash resume) |
| `data/whales_manual.json` | Manual whale overrides |

---

## Troubleshooting

### `dry_run: false` refused at startup

Expected in Phase A. Set `dry_run: true` in `config.yaml`.

### CLOB health_check failed / rate limits

- Verify `.env` credentials if using authenticated endpoints
- Check [Polymarket status](https://polymarket.com) and retry
- Circuit breaker opens after 5 consecutive failures; daemon pauses API calls until reset (see logs for `circuit breaker → OPEN`)
- Reduce `whale_universe.top_n_from_leaderboard` if polling too many wallets

### `database is locked` (SQLite)

- Only one `serve` instance per `data/trades.db`
- WAL mode is enabled; if lock persists, stop all processes and restart

### No whale signals appearing

- Confirm whales in universe: check logs at startup for count
- `whale_tracker.max_signal_age_ms` (default 60 s) records stale trades to DB but **does not enqueue** them — cold start may show many "skipped (stale)" until live trades arrive
- Run `python main.py whale-feed --follow` to watch the queue

### `backtest` refuses — not enough data

Need ≥7 calendar days of resolved `YES`/`NO` paper trades. Keep `serve` running.

### Daemon looks idle

Grep logs for cycle-complete lines (see Quickstart §5). Whale poll is every 60 s; market scan every 10 min; resolution every 30 min. Quiet periods are normal when whales aren't trading.

### SIGTERM / Ctrl+C

Handler stops threads (5 s timeout), writes `state.json`, closes DB. Safe to restart — position cache rebuilds from DB; `last_seen_ts` merged from `state.json`.

---

## Development

```bash
# Module smoke tests (offline unless noted)
.venv/bin/python tests/smoke_module1.py
# … through smoke_module12.py
```

See [`PLAN.md`](PLAN.md) §14 for build order and module map.

---

## Binary complement arbitrage (research module)

**Status: observation only. This module has never placed an order and contains
no code path that can.**

### The hypothesis

One YES share and one NO share of the same binary market must together pay
exactly **$1.00** at resolution, whichever way it settles. So if equal
quantities of both can be bought for materially less than $1.00 per pair, the
difference is locked in regardless of the outcome. Unlike whale-copying — which
this repository tested and [rejected](FINDINGS.md) — the claim is *structural*
rather than predictive: it needs no view on who wins.

### The exact cost model

Every figure is computed from **ask-side VWAP**. A midpoint or last-traded
price is not a price you can buy at, and using one is the easiest way to
manufacture an edge that evaporates on contact.

```
equal_shares   = largest quantity buyable on BOTH books within max_notional_usd,
                 capped by the weaker book's total ask depth
yes_cost       = VWAP cost of equal_shares walking the YES asks cheapest-first
no_cost        = VWAP cost of equal_shares walking the NO  asks cheapest-first

gross_payout   = equal_shares × $1.00
gross_cost     = yes_cost + no_cost
fees           = gross_cost × estimated_fee_bps_per_leg      × 2 / 10_000
slippage       = gross_cost × slippage_buffer_bps_per_leg    × 2 / 10_000

net_locked_pnl = gross_payout − gross_cost − fees − slippage
net_edge_bps   = net_locked_pnl / gross_cost × 10_000
```

A candidate is marked `executable` only when **all** hold: both books supply the
full equal-share quantity, the books are fresher than `max_book_age_seconds`,
`net_locked_pnl > 0`, `net_edge_bps >= min_net_edge_bps`, and the market passes
the active / open / order-book / binary checks.

Rejected candidates are stored too, with an explicit reason
(`missing_token`, `one_sided_book`, `insufficient_yes_depth`,
`insufficient_no_depth`, `stale_book`, `not_profitable`,
`edge_below_threshold`). The research question is how *often* an edge exists,
and that cannot be answered from successes alone.

### Measured result

A live scan of **252 markets** produced the following, with zero executable
candidates:

| pair cost = ask_vwap(YES) + ask_vwap(NO) | |
|---|---|
| minimum | **1.001000** |
| median | 1.010000 |
| maximum | 1.293 |
| **below $1.00** | **0 / 252** |

The minimum is exactly $1.00 plus one `minimum_tick_size`, across every market
measured — the mint/merge floor appearing in the data precisely where the
mechanism says it must.

Re-scored with the correct fee formula, the best net edge observed is
**-130 bps**. Setting the fee to **zero** (as on geopolitics markets) still
leaves **-110 bps**: the pair already costs more than $1.00 before any fee, so
no fee assumption can rescue it.

Two structural facts make this a closed question rather than an unlucky sample:

1. A pair is always obtainable for exactly $1 by splitting USDC, so the book
   cannot price it below that without being arbitraged against the mint.
2. VWAP over depth is by construction >= the best ask, so **a market that fails
   at the touch cannot pass at depth.**

### Cost assumptions — now RESOLVED

The fee rate was previously a guess. It has since been confirmed from
Polymarket's documentation and verified against their published fee table:

```
fee = C x feeRate x p x (1 - p)      # C = shares, p = price
```

* **Takers only. Makers are never charged.** Both legs of a complement pair
  cross the spread, so both pay.
* The fee is **not** proportional to notional. It peaks at p=0.5 and vanishes
  at the extremes, and is symmetric — a trade at 30c costs the same as one at
  70c. An earlier model here charged `cost x bps`, which overstated the fee on
  longshots and understated it near even money.
* Category rates: Crypto 0.07 | Sports 0.05 | Politics/Finance/Tech 0.04 |
  Economics/Culture/Weather/Other 0.05 | **Geopolitics 0**.
* The live `/fee-rate` endpoint confirms this per market — it returns
  `base_fee: 0` for geopolitics markets and non-zero elsewhere.

`smoke_module12` asserts the implementation against the published table at
p = 0.01, 0.05, 0.50 and 0.95.

**Still unvalidated:**

| Assumption | Why it needs checking |
|---|---|
| **$1.00 pair settlement** | Not every market settles conventionally. `neg_risk` multi-outcome markets and 50/50-invalid resolutions can break the identity. `evidence_json` stores full market metadata for hand review. |
| Two-leg execution risk | Not modelled. Filling one leg and missing the other converts a "locked" pair into an outright directional position. |
| Slippage buffer | A safety margin, not a measurement. |

### Usage

```bash
python main.py arb-scan              # one observation pass (read-only)
python main.py arb-status --days 7   # frequency, capacity, edge, rejections
```

Both commands are read-only against Polymarket. Results land in the
`arbitrage_opportunities` table, with one `arbitrage_opportunity` JSONL audit
event per row.

> **A positive observation is not permission to trade.** It is one data point
> under unvalidated fee and settlement assumptions. Establishing frequency,
> capacity, persistence, and validated costs comes first — and this repository's
> history is a reminder of what happens when a measurement is trusted before its
> ruler is checked.

## License

See repository license file if present.
