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
# … through smoke_module11.py
```

See [`PLAN.md`](PLAN.md) §14 for build order and module map.

---

## License

See repository license file if present.
