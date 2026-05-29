# PLAN.md — Polymarket Betting Bot

**Status:** DRAFT v4 — final review before build approval
**Mode:** Plan-first. No implementation code until "PLAN APPROVED — START BUILDING".

> **Module-9 PLAN deviations (2026-05):** `trust_score` split into `whale_perf:` config block;
> `trust_mean_min` lowered from 0.1 → 0.0; `blacklist_threshold: 0.35` added (Phase A
> log/flag only); `resolution_poller` calls `whale_perf.update_trust_for_resolved_market()`
> after `_settle_market`; idempotency via `processed_trust_updates` table.
>
> **v4 changes from v3:** explicit void semantics on `pending_paper_trades`
> (`VOIDED_PRE_FILL` / `VOIDED_MISSED_WINDOW` paper-trade rows; never back-fill
> at stale mid); explicit DESC index on `book_snapshots`; INCONCLUSIVE fourth
> row in the decision matrix backed by a two-proportion z-test (Fisher's exact
> at small N) computed by `calibration.py`. `[UPDATED-v4]` tags mark v4-changed
> sections.

---

## 0. Phase split

| | **Phase A — Observation** | **Phase B — Live trading** |
|---|---|---|
| Build now? | **Yes** | **No — after ≥14 days of Phase A data + calibration gate passes** |
| Wallet signing key | **Not required** (read-only CLOB) | Required |
| `dry_run` flag | `true` ⇒ paper-trade. `false` ⇒ **refuse to start with loud error** | `true` ⇒ paper-trade. `false` ⇒ live orders |
| Modules built | scanner, whale tracker, resolution poller, paper trader, tracker, calibration | + strategy, risk, executor |
| CLI | `scan`, `whales`, `whale-feed`, `backtest`, `status`, `pnl`, `add-whale` | + `run` |
| What gets persisted | whale signals (incl. exits/flips), market snapshots, book snapshots (depth 10), paper trades (4 streams), resolutions, detection latency | + live orders, fills, realised P&L |
| Decision output | four parallel paper streams logged side-by-side | live `BetIntent` → risk gate → CLOB |

**Hard gate from A → B:** `validate_calibration_readiness()` all-green AND the three-way decision matrix (§11) doesn't reject. See §11.

---

## 1. Locked-in decisions [UPDATED-v4]

| Area | Decision |
|---|---|
| Wallet | Plain private key in `.env`. Polygon mainnet. **Phase A leaves `PRIVATE_KEY` unset.** |
| Polygonscan | Optional enrichment. Absent ⇒ skip + single startup warning. |
| Storage | SQLite (`data/trades.db`) + append-only `data/audit.jsonl`. |
| Resolution | Poll CLOB API. Paper-trade markets only. |
| Whale discovery | Daily refresh from leaderboard. Unreachable ⇒ freeze + warn, never halt. |
| Market scope | Binary YES/NO only. Outcome labels match `["Yes","No"]` case-insensitive (config key for future widening). Crypto-price category blacklisted. |
| Polling cadence | Whale 60 s, market scan 10 min, book snapshots 10 min, deferred paper-trade sweep 10 min. |
| Whale interface | `AbstractWhaleTracker` — polling impl now, websocket later. |
| Deployment | Local first, VPS-ready: SIGTERM, crash resume, exponential backoff. |
| **Paper streams (four)** | `copy_whale`, `stub_blend`, `null_random_side`, `whale_side_random_timing` |
| **Whale `signal_type`** | `open` / `add` / `exit` / `flip` — all logged. Streams act only on `open` + `add`. |
| **Backtest gate** | Refuses with clear message if DB has < 7 days of data. |
| **`dry_run: false` in Phase A** | Refuse to start with loud error pointing to §0. |
| **Calibration gate** | ≥ 50 resolved paper-trades · ≥ 5 whales × ≥ 5 trades each · ≥ 14 days. |
| **Detection latency** | Logged on every whale signal as `detection_latency_ms`. |
| **Stale-signal guard** | Trades with `detection_latency_ms > whale_tracker.max_signal_age_ms` (default 60 s) are recorded to DB but **not** enqueued. Prevents cold-start whales from flooding paper_trader with months-old "would-have-paper-trades". |
| **`acts_on_signal_types` per stream** | `copy_whale` and `whale_side_random_timing`: `[open, add]` only. `stub_blend` and `null_random_side`: all four (`open, add, exit, flip`) — null baseline must be comparable to copy_whale on every signal it sees; stub is a placeholder for Phase B blend output which weighs every signal type. |
| **Schema** | `paper_trades.resolved_outcome` (renamed from `outcome` for clarity vs `live_orders`). |
| **Pending void semantics** | Two void modes both written as full `paper_trades` rows with `pnl_usd=0`: `VOIDED_PRE_FILL` (market resolved before scheduled fire) and `VOIDED_MISSED_WINDOW` (sweep arrived past miss tolerance — never back-fill at stale mid). |
| **Statistical A→B test** | Two-proportion z-test on `copy_whale` vs `null_random_side` (Fisher's exact if any cell < 5). INCONCLUSIVE if win-rate gap < 5pp **or** p-value > 0.10. |

---

## 2. Folder structure

```
polymarket-bot/
├── .env.example                  # CLOB_API_KEY/SECRET/PASSPHRASE (read-only ok),
│                                 # POLYGONSCAN_API_KEY (optional),
│                                 # PRIVATE_KEY (Phase B only — leave blank in A)
├── .gitignore                    # excludes .env, data/, __pycache__, *.db, *.jsonl
├── config.yaml                   # all tunable parameters (§6)
├── requirements.txt              # pinned versions per spec
├── README.md                     # quickstart, CLI reference, A→B transition checklist
├── PLAN.md                       # this document
├── main.py                       # CLI entrypoint; refuses dry_run:false in Phase A
├── bot/
│   ├── __init__.py               # package marker, version, PHASE constant
│   ├── models.py                 # dataclasses: WhaleSignal, MarketSnapshot,
│   │                             # BookSnapshot, PaperTrade, PendingPaperTrade,
│   │                             # ReadinessReport, ReadinessCheck
│   ├── client.py                 # CLOB wrapper: read-only in A, signing in B
│   ├── market_scanner.py         # fetch + filter binary markets, snapshots,
│   │                             # depth-10 book capture
│   ├── whale_tracker.py          # AbstractWhaleTracker + PollingWhaleTracker,
│   │                             # detection_latency_ms, signal_type classifier
│   ├── resolution_poller.py      # outcome polling, paper-trade close-out; after _settle_market
│   │                             # calls whale_perf.update_trust_for_resolved_market()
│   ├── paper_trader.py           # four streams; manages pending_paper_trades for
│   │                             # whale_side_random_timing
│   ├── tracker.py                # SQLite schema, audit log, P&L queries, state restore
│   ├── whale_perf.py             # post-resolution Bayesian Beta trust update
│   ├── calibration.py            # validate_calibration_readiness();
│   │                             # Phase-B parameter fit helpers (built later)
│   ├── strategy.py               # [PHASE B] pure compute: blend, Kelly, BetIntent
│   ├── risk.py                   # [PHASE B] RiskManager.pre_trade_checks()
│   └── executor.py               # [PHASE B] gated order placement
├── utils/
│   ├── __init__.py
│   ├── logger.py                 # rich logger + secret-redaction filter
│   └── helpers.py                # sigmoid, decay, kelly, market_efficiency, time utils
└── data/
    └── .gitkeep                  # trades.db, audit.jsonl, state.json, whales_manual.json at runtime
```

---

## 3. External APIs & SDKs

| Source | Purpose | Phase A auth | Phase B auth | Failure handling |
|---|---|---|---|---|
| `py-clob-client` (CLOB read) | markets, books, trades by maker, market status | API key+secret+passphrase (read-only ok); **no signing key needed** | + L1 signing key | exp. backoff; circuit breaker pauses paper streams when consecutive fails ≥ N |
| `py-clob-client` (CLOB write) | order placement, cancel | — | + L1 signing key | post-place reconcile via order status |
| `https://data-api.polymarket.com/trades?user={wallet}` | per-whale fills (corrected from PLAN v3 — `clob.polymarket.com/trades?maker_address=` returns 401 without auth; `data-api` is the public endpoint) | none (public) | none | per-wallet isolation |
| `https://gamma-api.polymarket.com/profile/{wallet}` | whale stats | none | none | cache last-known |
| `https://lb-api.polymarket.com/profit?window={window}&limit={n}` | leaderboard for whale auto-discovery (corrected from PLAN v3 — exact URL probed at module-4 build) | none (public) | none | freeze universe on failure |
| Polygonscan API | optional on-chain USDC verification | API key (optional) | API key (optional) | absent ⇒ skip + single warn |
| Polygon RPC (`web3`) | chain reads (USDC balance for Phase B) | not used | RPC URL only | rotate; halt new orders if all fail |

`utils/logger.py` redaction filter scrubs known secrets pre-emission.

---

## 4. Data flow [UPDATED]

### Phase A (build now)

```
                       ┌─────────────────────────┐
                       │   Polymarket CLOB API   │ ◄── (READ ONLY)
                       └─────────────────────────┘
                               ▲             │
                               │             ▼
  leaderboard ─┐        ┌──────┴──────────────┐
  trades?maker ┼───────►│  WhaleTracker thread│ ──► WhaleSignal queue ──┐
  profile/     ┘        │  (60 s, aiohttp)    │  (+ signal_type +       │
                        │  classify open/add/ │   detection_latency_ms) │
                        │  exit/flip)         │                          │
                        └─────────────────────┘                          │
                                                                         ▼
                        ┌─────────────────────┐         ┌──────────────────────────────┐
                        │  MarketScanner      │         │   PaperTrader (4 streams)    │
                        │  (10 min)           │ ───────►│   acts on open|add only:     │
                        │  + BookSnapshotter  │         │   • copy_whale               │
                        │  (depth 10)         │         │   • stub_blend               │
                        └─────────────────────┘         │   • null_random_side         │
                                                        │   • whale_side_random_timing │
                                                        │     (defers to schedule)     │
                                                        └─────────────┬────────────────┘
                                                                      │
                        ┌────────────────────────┐                    ▼
                        │  Pending sweep         │ ◄────────► ┌──────────────────────┐
                        │  (every 10 min, on     │   reads    │       Tracker        │
                        │  market_scan tick)     │   writes   │   SQLite + audit     │
                        │  materialises due      │ ─────────► │                      │
                        │  whale_side_random_t.  │            │                      │
                        └────────────────────────┘            └──────────┬───────────┘
                        ┌────────────────────────┐                       │
                        │  ResolutionPoller      │ ────────────────────► │
                        │  (30 min, paper-trade  │                       │
                        │  markets only)         │                       │
                        └────────────────────────┘                       ▼
                                                              ┌──────────────────────┐
                                                              │   WhalePerf          │
                                                              │   (Beta update)      │
                                                              └──────────────────────┘
                                                                         │
                                                                         ▼
                                                              ┌──────────────────────┐
                                                              │   Calibration        │
                                                              │   readiness check    │
                                                              │   (called by status) │
                                                              └──────────────────────┘
```

### Phase B (additive, later)

Phase A keeps running unchanged. New branch attaches downstream:

```
   PaperTrader (still running for ongoing comparison)
                     ─────────────────► Tracker
                                            ▲
                     ┌──────────────────────┴──────────┐
                     │  Calibration outputs (fit α,β,  │
                     │  λ, per-whale priors, weights)  │
                     │  → data/calibration.yaml        │
                     └──────────────┬──────────────────┘
                                    ▼
                            ┌───────────────┐
                            │   Strategy    │ ◄── live MarketSnapshot + WhaleSignal
                            └──────┬────────┘
                                   ▼ BetIntent
                            ┌───────────────┐
                            │   RiskManager │
                            │  (single gate)│
                            └──────┬────────┘
                                   ▼ RiskCheckResult token
                            ┌───────────────┐
                            │   Executor    │ ──► CLOB (write)
                            └───────────────┘
```

---

## 5. Module dependency graph

```
main.py
   ├── bot.client            ── py_clob_client, requests
   ├── bot.market_scanner    ── bot.client, bot.models, utils.helpers
   ├── bot.whale_tracker     ── aiohttp, bot.tracker (for signal_type lookups),
   │                            bot.models, utils.helpers, utils.logger
   ├── bot.resolution_poller ── bot.client, bot.tracker
   ├── bot.paper_trader      ── bot.tracker, bot.models, utils.helpers
   ├── bot.tracker           ── sqlite3, json
   ├── bot.whale_perf        ── bot.tracker
   ├── bot.calibration       ── bot.tracker
   ├── bot.strategy          [Phase B]   ── bot.models, utils.helpers (pure)
   ├── bot.risk              [Phase B]   ── bot.tracker
   ├── bot.executor          [Phase B]   ── bot.client, bot.risk, bot.tracker
   └── utils.logger / helpers
```

**Layering rules:**
- `utils.*` depends on nothing in `bot.*`.
- `bot.strategy` is pure — no I/O, fully unit-testable.
- `bot.executor` is the **only** caller of `client.place_order`, and **must** pass a `RiskCheckResult` token that only `RiskManager` can construct. `client.place_order` asserts `token.passed`. Structural single-gate enforcement.

---

## 6. Config & data schema [UPDATED]

### `config.yaml`

```yaml
# dry_run is hoisted to the absolute top of the file by design — it is THE
# live/paper boundary and must be visually unmissable. See config.yaml header.
dry_run: true                              # Phase A: false ⇒ refuse to start

phase: A                                   # informational; A or B

network:
  polygon_rpc_urls:                        # tried in order (Phase B uses)
    - https://polygon-rpc.com
    - https://polygon.llamarpc.com
  ctf_exchange: "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

bot:
  log_level: INFO
  data_dir: ./data
  db_path: ./data/trades.db
  audit_log_path: ./data/audit.log.jsonl
  state_path: ./data/state.json

intervals_seconds:
  whale_poll: 60
  market_scan: 600
  book_snapshot: 600                       # piggybacks on scan
  pending_sweep: 600                       # piggybacks on scan; materialises due deferred trades
  resolution_poll: 1800
  whale_discovery: 86400
  trust_score_refresh: 3600

market_filters:
  binary_only: true
  outcome_labels: ["Yes", "No"]            # case-insensitive match; widen here for Phase B
  min_volume_24h_usd: 10000
  min_liquidity_usd: 5000
  max_spread_bps: 200
  blacklist_categories: ["crypto-prices"]
  blacklist_keywords: ["BTC price", "ETH price", "SOL price"]
  whitelist_categories: []

book_snapshot:
  depth_levels: 10                         # each side
  store_as_json: true                      # full {bids:[…10], asks:[…10]} blob

whale_universe:
  top_n_from_leaderboard: 50
  min_all_time_profit_usd: 50000
  manual_seed: []                          # [{address, label, trust}]
  manual_overrides_path: ./data/whales_manual.json   # CLI add-whale writes here

paper_streams:
  copy_whale:
    enabled: true
    size_mode: proportional                # paper $ ≈ whale $ × scale, then clamped
    proportional_scale: 0.01               # 1% of whale's bet size
    min_paper_size_usd: 5
    max_paper_size_usd: 500
    acts_on_signal_types: [open, add]

  stub_blend:
    enabled: true
    alpha: 0.5
    beta: 0.0
    gamma: 0.5
    trust_prior: 0.7                       # uniform during Phase A
    size_usd: 100
    acts_on_signal_types: [open, add]

  null_random_side:
    enabled: true
    size_usd: 100
    seed_strategy: market_id               # random.seed(market_id) — deterministic across runs
    acts_on_signal_types: [open, add]

  whale_side_random_timing:
    enabled: true
    size_usd: 100
    delay_window_seconds: 86400            # uniform sample [0, 24h]
    seed_strategy: signal_id               # random.seed(signal_id) — independent per signal
    miss_tolerance_seconds: 1800           # if sweep arrives this far past scheduled_for_ts,
                                           # void with VOIDED_MISSED_WINDOW (do NOT back-fill —
                                           # stale mid corrupts the timing-null comparison)
    max_snapshot_staleness_seconds: 900    # entry mid must be from a snapshot ≤ this old at
                                           # materialisation; else void as MISSED_WINDOW
    acts_on_signal_types: [open, add]

calibration_readiness:
  min_resolved_trades: 50                  # counts only resolved_outcome IN ('YES','NO');
                                           # VOIDED_* and INVALID excluded
  min_whales_with_min_trades: 5
  min_trades_per_whale: 5
  min_calendar_days: 14

decision_matrix:
  min_winrate_gap_pp: 5.0                  # copy_whale must beat null_random_side by ≥ this
  max_p_value: 0.10                        # two-proportion z-test (Fisher's exact at small N)
  inconclusive_extension_days: 14          # extend Phase A this much if INCONCLUSIVE

whale_perf:                                # bot/whale_perf.py — per-whale Bayesian trust
  prior_alpha: 7                           # Beta(7,3) ⇒ prior mean 0.7, total weight 10
  prior_beta: 3
  manual_override_total_weight: 10         # add-whale --trust seeds α+β=10
  trust_mean_min: 0.0                      # Floor at 0.0 — auto-blacklist handles low-trust whales below blacklist_threshold
  trust_mean_max: 0.95                     # Ceiling — prevents pathological copy-sizing in Phase B
  blacklist_threshold: 0.35                # Phase A: log + candidate_blacklist flag only; NO universe removal
  attribution_strategy_id: copy_whale      # only resolved copy_whale YES/NO rows update trust

# DEPRECATED (module-9): use whale_perf: above. Kept for backward compat until Phase B ships.
trust_score:
  prior_alpha: 7
  prior_beta: 3
  manual_override_total_weight: 10
  min: 0.0
  max: 0.95

reconnect:
  initial_backoff_seconds: 2
  max_backoff_seconds: 300
  multiplier: 2.0
  circuit_breaker_consecutive_fails: 5

# ──────────────  Phase B sections (inert in Phase A)  ──────────────
strategy:                                  # [Phase B]
  min_edge_pct: 0.03
  max_bet_pct_bankroll: 0.05
  kelly_fraction: 0.25
  blend:
    alpha_max: 0.6
    beta_max: 0.5
    lambda_decay_per_min: 0.02             # OVERWRITTEN by calibration fit
    sigmoid_steepness: 3.0
    quant_score_default: 0.5
    avg_bet_lookback_days: 30

market_efficiency:                         # [Phase B]
  spread_weight: 0.4
  depth_weight: 0.3
  volume_weight: 0.3
  spread_tight_bps: 50
  spread_wide_bps: 500
  depth_full_usd: 50000
  volume_full_usd_24h: 1000000

risk:                                      # [Phase B]
  max_open_positions: 10
  max_daily_drawdown_pct: 0.05
  max_slippage_bps: 100
  duplicate_market_block: true
  bankroll_floor_usd: 100

execution:                                 # [Phase B]
  order_type: GTC
  limit_offset_bps: 5
  retry_attempts: 3
  retry_backoff_seconds: 2
```

### SQLite schema

```sql
-- whale signals captured by tracker; signal_type classified vs prior position state
CREATE TABLE whale_signals (
  id INTEGER PRIMARY KEY,
  whale_address TEXT NOT NULL,
  whale_label TEXT,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,                      -- YES | NO
  signal_type TEXT NOT NULL,               -- open | add | exit | flip
  price REAL NOT NULL,
  size_usd REAL NOT NULL,
  whale_fill_ts INTEGER NOT NULL,          -- ms epoch, from CLOB
  detected_at_ts INTEGER NOT NULL,         -- ms epoch, our clock
  detection_latency_ms INTEGER NOT NULL,   -- detected_at - whale_fill_ts
  transaction_hash TEXT UNIQUE,            -- chain tx hash; UNIQUE for idempotent
                                           -- INSERT ON CONFLICT DO NOTHING. NULL
                                           -- allowed (multiple NULLs permitted by
                                           -- SQLite UNIQUE) so synthetic test
                                           -- signals lacking a hash still insert.
  raw_json TEXT NOT NULL                   -- audit trail
);
CREATE INDEX idx_whale_signals_market ON whale_signals(market_id);
CREATE INDEX idx_whale_signals_whale  ON whale_signals(whale_address);
CREATE INDEX idx_whale_signals_type   ON whale_signals(signal_type);

-- 10-min market state samples
CREATE TABLE market_snapshots (
  id INTEGER PRIMARY KEY,
  market_id TEXT NOT NULL,
  ts INTEGER NOT NULL,                     -- ms epoch
  mid REAL NOT NULL,
  spread_bps REAL,
  volume_24h_usd REAL,
  liquidity_usd REAL,
  category TEXT,
  question TEXT
);
CREATE INDEX idx_market_snapshots_market_ts ON market_snapshots(market_id, ts);

-- full book to depth 10, JSON blob, separate table to keep main lean
CREATE TABLE book_snapshots (
  market_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  book_json TEXT NOT NULL,                 -- {"bids":[[price,size]…10], "asks":[…10]}
  PRIMARY KEY (market_id, ts)
);
-- The PK already serves DESC scans efficiently in SQLite, but declare an
-- explicit DESC index so calibration's "give me the latest book for market X"
-- queries are unambiguous and survive any future PK refactor.
CREATE INDEX idx_book_snapshots_market_ts_desc ON book_snapshots(market_id, ts DESC);

-- four parallel paper streams in one table, distinguished by strategy_id
CREATE TABLE paper_trades (
  id INTEGER PRIMARY KEY,
  strategy_id TEXT NOT NULL,               -- copy_whale | stub_blend | null_random_side | whale_side_random_timing
  triggering_signal_id INTEGER NOT NULL,   -- FK whale_signals.id
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  entry_price REAL NOT NULL,               -- mid at decision time
  size_usd REAL NOT NULL,
  decided_at_ts INTEGER NOT NULL,
  resolved_at_ts INTEGER,                  -- NULL until market resolves
  resolved_outcome TEXT,                   -- YES | NO | INVALID | VOIDED_PRE_FILL | VOIDED_MISSED_WINDOW
                                           -- (only YES/NO count toward win-rate stats)
  pnl_usd REAL,                            -- realised on resolution; 0 for VOIDED_*
  FOREIGN KEY(triggering_signal_id) REFERENCES whale_signals(id)
);
CREATE INDEX idx_paper_trades_strategy ON paper_trades(strategy_id);
CREATE INDEX idx_paper_trades_market   ON paper_trades(market_id);
CREATE INDEX idx_paper_trades_decided  ON paper_trades(decided_at_ts);

-- deferred decisions for whale_side_random_timing (24-h randomised entry)
CREATE TABLE pending_paper_trades (
  id INTEGER PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  triggering_signal_id INTEGER NOT NULL,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  size_usd REAL NOT NULL,
  scheduled_for_ts INTEGER NOT NULL,
  status TEXT NOT NULL,                    -- pending | materialised | voided
  created_at_ts INTEGER NOT NULL,
  FOREIGN KEY(triggering_signal_id) REFERENCES whale_signals(id)
);
CREATE INDEX idx_pending_status_time ON pending_paper_trades(status, scheduled_for_ts);

-- per-whale running trust scores (Beta-distribution params)
CREATE TABLE whale_trust (
  whale_address TEXT PRIMARY KEY,
  alpha REAL NOT NULL,
  beta  REAL NOT NULL,
  trust_mean REAL NOT NULL,                -- clamped to [whale_perf.trust_mean_min, trust_mean_max]
  resolved_count INTEGER NOT NULL,
  updated_at_ts INTEGER NOT NULL,
  candidate_blacklist INTEGER NOT NULL DEFAULT 0   -- 1 if trust_mean < blacklist_threshold (Phase A: flag only)
);

-- Idempotency for whale_perf: one trust update per (market_id, whale_signal_id).
CREATE TABLE processed_trust_updates (
  market_id TEXT NOT NULL,
  whale_signal_id INTEGER NOT NULL,
  processed_at_ts INTEGER NOT NULL,
  PRIMARY KEY (market_id, whale_signal_id)
);

-- resolved_markets: idempotency for the resolution poller. One row per
-- market we've learned has resolved, so we don't re-query CLOB on every
-- tick. Added in module-3 build (reverses v3 §15 Q6 "skip" — turned out
-- to be load-bearing for poller efficiency).
CREATE TABLE resolved_markets (
  market_id TEXT PRIMARY KEY,
  resolved_outcome TEXT NOT NULL,                -- YES | NO | INVALID
  resolved_at_ts INTEGER NOT NULL
);

-- live orders (Phase B only — table created in A but unused)
CREATE TABLE live_orders (
  id INTEGER PRIMARY KEY,
  clob_order_id TEXT UNIQUE,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  limit_price REAL NOT NULL,
  size_usd REAL NOT NULL,
  status TEXT NOT NULL,                    -- pending|filled|partial|cancelled|rejected
  placed_at_ts INTEGER NOT NULL,
  filled_at_ts INTEGER,
  fill_price REAL,
  risk_token_hash TEXT NOT NULL,           -- proves it passed the gate
  intent_json TEXT NOT NULL
);
```

`audit.jsonl` mirrors every state-changing event one-line-per-event. Append-only, never read by the bot at runtime — forensic.

---

## 7. `signal_type` classification [UPDATED]

**Raw-trade → (side, signal_type) mapping** (data-api.polymarket.com/trades — confirmed by probe at module-7 build):

| Trade `side` | Trade `outcome` | WhaleSignal `side` | Effect on position |
|---|---|---|---|
| BUY | Yes | YES | +shares to YES |
| SELL | Yes | YES | −shares from YES |
| BUY | No | NO | +shares to NO |
| SELL | No | NO | −shares from NO |

`shares = size_usd / price` (size_usd stored as USDC value = `trade.size × trade.price`). Trade timestamps from data-api are in **seconds**; whale_tracker multiplies by 1000 to match our ms-epoch convention.

When `WhaleTracker` receives a fill for whale W on market M with side S and size N:

1. Query `whale_signals` for prior aggregate position of W in M:
   `net_yes = sum(YES sizes) − sum(NO sizes prior YES exits) …` (running net per side).
2. Classify:
   - **open** — no prior position in M for W.
   - **add** — prior position on the **same** side as the new fill.
   - **exit** — fill is on the side that reduces an existing position (sell of held side; or buy of opposite side that doesn't exceed prior). Marked exit if the position is closed or reduced.
   - **flip** — buy on the opposite side that exceeds the prior holding (i.e. the position changes sign).
3. Persist with `signal_type`. **All four types are logged.**

Streams' `acts_on_signal_types` filters which produce paper trades. Default for all four streams: `[open, add]`. `exit` and `flip` are observation-only — Phase B can opt them in if calibration shows predictive value.

**Deduplication (two layers).**
1. **5-minute in-memory window** keyed on `(wallet, market_id, outcome, side)` inside `PollingWhaleTracker`. Drops repeat signals seen during the same trade event (e.g. a poll cycle that overlaps with the previous one). Avoids hot-path DB roundtrips for the common case.
2. **DB `UNIQUE(transaction_hash)` on `whale_signals`** as the strict idempotency guard. `Tracker.record_whale_signal` uses `INSERT ... ON CONFLICT(transaction_hash) DO NOTHING` and returns `int | None` (rowid on insert, `None` on conflict). `WhaleTracker` treats `None` as "already seen, don't emit". Survives process restarts, where the in-memory window is empty.

---

## 8. Paper streams in detail [UPDATED-v4]

For every whale signal with `signal_type ∈ {open, add}`, all four enabled streams emit one row each. **All four use the same `triggering_signal_id`** so per-signal comparison is a single SQL join.

| Stream | Direction | Entry timing | Entry price | Size |
|---|---|---|---|---|
| `copy_whale` | same as whale | immediate (current scan tick mid) | whale's own fill `price` (from signal) | `clamp(whale_size · proportional_scale, [min, max])` |
| `stub_blend` | side with higher `P_true` per stub formula (α=0.5, β=0, γ=0.5) | immediate | latest market_snapshot mid (or `1−mid` for NO side) | fixed `size_usd` |
| `null_random_side` | **random YES/NO**, `random.Random(market_id)` (deterministic across runs) | immediate | latest market_snapshot mid (or `1−mid` for NO) | fixed `size_usd` |
| `whale_side_random_timing` | same as whale | **deferred**: random delay ∈ [0, 1 h] *(Phase A.2 deviation: reduced from 24 h — see below)*, `random.Random(signal_id)`, materialised on next pending-sweep tick after `scheduled_for_ts` | mid at materialisation tick (from latest fresh `book_snapshots`) | fixed `size_usd` |

**`acts_on_signal_types` per stream** (revised at module-8 build): `copy_whale` and `whale_side_random_timing` act on `[open, add]` only. `stub_blend` and `null_random_side` act on **all four** signal types (`open, add, exit, flip`) — the null baseline must be comparable to copy_whale on every observable, and stub is a placeholder for Phase B blend output which weighs every signal type.

**Phase A.2 PLAN deviations:**

1. **Snapshot on-demand fallback** (`stub_blend` / `null_random_side`): the original design required a fresh cached `market_snapshot` (≤15 min old) at signal-consume time. In practice ~99.7% of signals had no fresh snapshot for their specific market, causing both streams to produce zero rows during Phase A. Fix: when `get_latest_market_snapshot` returns `None` or a stale row, `paper_trader` calls `client.get_order_book(market_id)`, writes the result to `book_snapshots` (so future signals for the same market benefit from the cache), and uses the computed best-bid/ask mid as the entry price. `CircuitBreakerOpen` → WARNING + skip both streams. One-sided book → INFO + skip.

2. **`delay_window_seconds` reduced from 86400 → 3600** (`whale_side_random_timing`): 77% of Phase A trades in this stream voided with `VOIDED_PRE_FILL` because whales disproportionately trade markets near resolution (hours, not days). A 24-hour random delay was longer than most market resolution horizons. Reduced to 1 hour so the fill window has a realistic chance of preceding resolution.

**`whale_side_random_timing` mechanics:**
- On signal: write `pending_paper_trades` row, status `pending`, `scheduled_for_ts = now + uniform(0, delay_window_seconds)` seeded by `signal_id`.
- Every 10 min (pending sweep): `SELECT * FROM pending_paper_trades WHERE status='pending' AND scheduled_for_ts <= now`.
- For each due row, in this order of checks:
  1. **Market already resolved** (resolution recorded before scheduled fire) → write `paper_trades` row with `resolved_outcome='VOIDED_PRE_FILL'`, `pnl_usd=0`, `entry_price=NULL`; mark pending `voided`.
  2. **Sweep is late** (`now > scheduled_for_ts + miss_tolerance_seconds` — bot was offline, scheduler delayed, etc.) → write `paper_trades` row with `resolved_outcome='VOIDED_MISSED_WINDOW'`, `pnl_usd=0`, `entry_price=NULL`; mark pending `voided`. **Never back-fill at a stale mid — it would corrupt the timing-null comparison.**
  3. **Latest snapshot is too stale** (most recent `market_snapshots.ts` for this `market_id` older than `max_snapshot_staleness_seconds`) → same as case 2, void as `VOIDED_MISSED_WINDOW`.
  4. **Otherwise** (healthy materialisation): use the latest snapshot's `mid` as `entry_price`; insert `paper_trades` row; mark pending `materialised`.
- Win-rate and P&L queries filter `WHERE resolved_outcome IN ('YES', 'NO')` — VOIDED_* and INVALID rows are auditable but excluded from stats.
- This isolates "is the whale's *timing* informative?" from "is the whale's *direction* informative?".

**Crash / restart safety.** `pending_paper_trades` is durable. On startup, the next sweep tick processes everything; rows whose window has fully passed get voided cleanly via case 2/3. There is no path that materialises a paper trade at a mid older than `max_snapshot_staleness_seconds`.

**Decision matrix (post 50+ resolved, evaluated only on `resolved_outcome IN ('YES','NO')` rows):**

`copy_whale` is the test arm; `null_random_side` is the control. Compute win rates `w_copy`, `w_null` and run a two-proportion z-test for the difference (use **Fisher's exact** if any 2×2 cell count < 5). The matrix evaluates **top-to-bottom — first match wins**, so HALT and INCONCLUSIVE both trigger before any "build B" path.

| # | Condition | Reading | Action |
|---|---|---|---|
| 1 | `w_null ≥ w_copy` | no directional edge | **HALT** — do not build B |
| 2 | `(w_copy − w_null) < min_winrate_gap_pp` **OR** `p > max_p_value` | within statistical noise; "I built it, I'm shipping it" trap | **INCONCLUSIVE** — extend Phase A by `inconclusive_extension_days`, re-evaluate. Do NOT proceed to B. |
| 3 | `w_copy > w_null` significantly **AND** `w_timing_null ≈ w_copy` (p > 0.10 between them) | direction matters, timing doesn't | build B with relaxed latency requirements; entry can lag whale fill by minutes |
| 4 | `w_copy > w_null` significantly **AND** `w_copy > w_timing_null` significantly | direction *and* timing matter | build B as specified; prioritise low-latency execution |

`calibration.py` exposes `evaluate_decision_matrix(tracker) -> DecisionVerdict` returning each stream's `(wins, losses, win_rate, n)`, the pairwise p-values, and the verdict label `HALT | INCONCLUSIVE | BUILD_B_RELAXED | BUILD_B_FULL`. `python main.py status` prints this readout alongside the readiness report so the decision is data-driven, not vibes-driven.

---

## 9. Probability formula (Phase B — for reference)

Phase A doesn't use this; `stub_blend` uses the placeholder values from §6.

```
P_true = α · P_whale + β · P_quant + γ · P_market

α = trust_score · sigmoid((bet_size / avg_bet − 1) · k) · e^(−λ · Δt)
β = quant_score · (1 − market_efficiency)
γ = 1 − α − β        # residual; clipped ≥ 0

# Clip α ≤ alpha_max, β ≤ beta_max, then renormalise so α+β+γ=1, all ≥ 0.
```

Sizing (Phase B):
```
b      = (1 / P_market) − 1
f_raw  = (b · P_true − (1 − P_true)) / b
f_frac = f_raw · kelly_fraction
bet    = min(f_frac · bankroll, max_bet_pct · bankroll)

skip if (P_true − P_market) < min_edge_pct OR f_raw ≤ 0
```

Calibration (§11) fits `λ`, per-whale `trust_score` priors, `quant_score`, and `market_efficiency` weights from Phase A's data, writing the result to `data/calibration.yaml` consumed by `strategy.py`.

---

## 10. Risk gate (Phase B only)

Not built in Phase A. R1–R9 from v1/v2 carry over verbatim. Built at build-order step 16.

In Phase A, "risk" is the `dry_run: false` refuse-to-start check at startup — that **is** the Phase A risk gate.

---

## 11. Calibration readiness gate + decision matrix [UPDATED-v4]

Implemented in `bot/calibration.py`:

```python
@dataclass
class ReadinessCheck:
    name: str
    required: float
    current: float
    passed: bool

@dataclass
class ReadinessReport:
    checks: list[ReadinessCheck]
    all_passed: bool
    summary: str               # "47/50 trades · 4/5 whales ready · 12/14 days"

def validate_calibration_readiness(tracker) -> ReadinessReport: ...
```

**Three checks (all must pass):**

| Check | Required | Computed from |
|---|---|---|
| Resolved paper-trades | ≥ 50 | `paper_trades WHERE resolved_outcome IS NOT NULL` |
| Distinct whales w/ ≥ 5 resolved trades each | ≥ 5 | join `paper_trades` ↔ `whale_signals` ↔ `whale_address` |
| Calendar days of data | ≥ 14 | `MAX(decided_at_ts) − MIN(decided_at_ts)` |

`python main.py status` calls this on every invocation and renders the summary line plus per-check breakdown. Glance at it through Phase A; you'll see the counters tick up.

**Plus the §8 decision matrix.** Even with all three readiness checks green, the verdict from `evaluate_decision_matrix()` must be `BUILD_B_RELAXED` or `BUILD_B_FULL`. `HALT` and `INCONCLUSIVE` are non-overridable in code — Phase B startup refuses if either is current.

**Two gates, both required:**
1. *Readiness gate* (this section) — proves you have **enough** data.
2. *Decision matrix* (§8) — proves the data shows **real edge** at sufficient statistical confidence (≥ 5pp gap, p ≤ 0.10).

`INCONCLUSIVE` is the row that exists specifically to defeat the "I built it, I'm shipping it" bias. If you see it, the only correct action is to keep Phase A running for another `inconclusive_extension_days` and let more data land. The infrastructure cost of another two weeks is zero; the cost of building Phase B on a coin-flip edge is large.

**Why the readiness thresholds:**
- 50 binary trials: 95% CI half-width ≈ ±14 pp around 60% — wide, but distinguishes edge from coin-flip.
- 5 whales × 5 trades: floor for per-whale priors that aren't pooled noise.
- 14 days: at least one weekly cycle (weekends differ from weekdays).

---

## 12. Failure modes [UPDATED-v4]

| Failure | Detection | Phase A response | Phase B addition |
|---|---|---|---|
| `dry_run: false` in Phase A | startup check in `main.py` | **abort** with explicit error: "Live trading not built — see PLAN.md §0" | n/a (allowed in B) |
| CLOB read timeout / 5xx | request wrapper | exp. backoff; circuit breaker pauses paper streams (still log signals) | + pause new orders |
| CLOB 401 / signature error | response code | unlikely in A (read-only) — log loud | halt new orders, require restart |
| Whale endpoint 5xx | per-wallet | isolate that whale | same |
| Leaderboard unreachable | discovery job | freeze universe + warn | same |
| Polygonscan absent / errors | enrichment call | skip + single startup warn | same |
| RPC node down | not used in A | n/a | rotate; halt orders if all fail |
| Order placement fails | n/a | n/a | poll status, reconcile, audit; no blind retry |
| DB write fails | sqlite exception | retry once → halt + audit fallback | same |
| Crash mid-flight | next-startup | restore state.json + rebuild from DB; next pending sweep classifies any overdue rows via §8 cases 1–3 (resolved → VOIDED_PRE_FILL; late → VOIDED_MISSED_WINDOW; stale snapshot → VOIDED_MISSED_WINDOW). **Never back-fill at stale mid.** | + reconcile open orders against CLOB |
| Pending paper trade overdue but market resolved before scheduled fire | sweep tick | write `paper_trades` row `resolved_outcome='VOIDED_PRE_FILL'`, `pnl_usd=0`; pending → `voided` | n/a |
| Pending paper trade missed window (bot offline / sweep delayed > `miss_tolerance_seconds`, or no fresh snapshot) | sweep tick | write `paper_trades` row `resolved_outcome='VOIDED_MISSED_WINDOW'`, `pnl_usd=0`; pending → `voided`. Stale mid would corrupt timing-null comparison — voiding is the only correct answer. | n/a |
| SIGTERM/SIGINT | handler | flush state, join threads (5 s timeout), exit 0 | + cancel open orders before flush |
| Clock skew | reconciliation tick | warn if local drift > 30 s vs CLOB ts | same |
| `signal_type` ambiguous (rare race) | classifier | log as `open` and warn | same |

---

## 13. Testing strategy [UPDATED-v4]

**Unit (offline):**
- `utils.helpers` — sigmoid, decay, Kelly, edge, `market_efficiency` — table-driven.
- `bot.whale_tracker.classify_signal_type` — synthetic per-whale fill sequences, verify open/add/exit/flip classification at each step.
- `bot.paper_trader` — four streams, fixed inputs ⇒ expected ledger rows for each `strategy_id`. Verify:
  - `null_random_side` reproducible across runs given same `market_id`
  - `whale_side_random_timing` writes `pending_paper_trades` row with seeded delay
  - sweep materialises only after `scheduled_for_ts` with a fresh snapshot
  - **void cases** each produce a `paper_trades` row with the correct `resolved_outcome` and `pnl_usd=0`:
    - market resolved before scheduled fire → `VOIDED_PRE_FILL`
    - sweep delayed past `miss_tolerance_seconds` (simulate clock skip) → `VOIDED_MISSED_WINDOW`
    - latest snapshot older than `max_snapshot_staleness_seconds` → `VOIDED_MISSED_WINDOW`
  - **never back-fills at stale mid** — assert no path inserts `entry_price` from a snapshot older than `max_snapshot_staleness_seconds`
  - win-rate aggregation queries filter `resolved_outcome IN ('YES','NO')` and exclude all VOIDED_*/INVALID rows
  - size clamps applied to `copy_whale`
- `bot.calibration` —
  - readiness: synthetic DB states; all three checks fire correctly at boundary values (49/50, 4/5, 13/14)
  - decision matrix: synthetic resolved counts —
    - `null=27/50, copy=30/50` (3pp gap, p≈0.68) ⇒ **INCONCLUSIVE**
    - `null=22/50, copy=35/50` (13pp gap, p≈0.008) ⇒ **BUILD_B_FULL** if timing-null also lags, else `BUILD_B_RELAXED`
    - `null=30/50, copy=28/50` ⇒ **HALT**
    - small-N edge (any cell < 5) ⇒ falls back to Fisher's exact, verify scipy and manual computation agree
  - matrix is evaluated top-to-bottom; HALT and INCONCLUSIVE both pre-empt any BUILD_B verdict
- `bot.whale_perf` — Beta update converges on always-right and always-wrong synthetic whales; `trust_mean` clamped to `[0.0, 0.95]`; `add-whale --trust 0.9` seeds α=9 β=1; `blacklist_threshold` flags `candidate_blacklist` but does not remove whales from universe in Phase A; idempotent per `(market_id, whale_signal_id)` via `processed_trust_updates`.
- `bot.market_scanner` — filter logic, `outcome_labels` case-insensitive match, depth-10 book parsing.
- `bot.strategy` [Phase B] — fixed inputs ⇒ expected `BetIntent` (or `None`).
- `bot.risk` [Phase B] — each of R1–R9 individually + combined scenarios.

**Integration:**
- `bot.tracker` — real SQLite in `tmp_path`; full schema; concurrent writers; state-restore after simulated crash; `pending_paper_trades` survives restart.
- `bot.client` — read-only endpoints against live CLOB (no writes). Phase B writes covered by recorded HTTP cassettes.
- **End-to-end Phase A smoke** — inject canned whale signals (incl. one open then one exit on same market) + canned market + canned book; run two scan ticks; assert:
  - `whale_signals` has both rows with correct `signal_type`s
  - four paper-trade rows for the open (one per stream); zero for the exit
  - one `pending_paper_trades` row with `scheduled_for_ts` in the future
  - after fast-forwarding the clock, sweep materialises it
  - `audit.jsonl` mirrors every event
  - `detection_latency_ms` populated

**Manual verification before A→B flip:**
1. `python main.py status` — readiness all-green.
2. Inspect: per-whale win rates, average detection latency, P&L by stream.
3. **Review `candidate_blacklist` whales** (`python main.py whales` — ⚠️ below threshold).
   Phase A never auto-removes whales from the polling universe (that would corrupt the
   observation dataset). At A→B transition, **manually** decide which flagged whales to
   exclude from the live universe after reviewing their full trade history.
4. **Decision matrix check.** Read the verdict from `evaluate_decision_matrix()`:
   - `HALT` → null wins or ties; do not build B
   - `INCONCLUSIVE` → real but underpowered; extend Phase A by `inconclusive_extension_days` and re-check
   - `BUILD_B_RELAXED` → direction matters, timing doesn't; relaxed latency targets
   - `BUILD_B_FULL` → both matter; full Phase B with low-latency execution
5. Run calibration fit, sanity-check fitted params.
6. Phase B first run with `dry_run: true` for ≥ 24 h.
7. Only then `dry_run: false` with small `bankroll_floor_usd`.

---

## 14. Build order

**Phase A (now):**
1. `utils/helpers.py` + `utils/logger.py` — pure utilities, redaction filter
2. `bot/models.py` — dataclasses
3. `bot/tracker.py` — full SQLite schema (incl. Phase B tables, unused), audit log, state restore
4. `bot/client.py` — read-only CLOB wrapper, retries, circuit breaker
5. `bot/market_scanner.py` — fetch + filter + book snapshot to depth 10
6. `bot/resolution_poller.py` — outcome polling, paper-trade close-out (paper-trade markets only). [Swapped ahead of whale_tracker at module-6 build by builder judgment: lets module-7 whale_tracker smoke produce end-to-end signal→paper→resolution lifecycles.]
7. `bot/whale_tracker.py` — abstract base + polling impl + `signal_type` classifier + `detection_latency_ms`
8. `bot/paper_trader.py` — four streams + pending-trade scheduling/sweep
9. `bot/whale_perf.py` — Bayesian Beta update on resolution
10. `bot/calibration.py` — `validate_calibration_readiness()` + `evaluate_decision_matrix()` (two-proportion z-test + Fisher's exact via `scipy.stats`); parameter-fit helpers stubbed for B
11. `main.py` — CLI, orchestrator, signal handlers, **dry_run:false refuse-to-start guard**
12. `README.md` — quickstart, A→B transition checklist
13. `config.yaml` + `.env.example` — final defaults

**Phase B (after readiness gate + decision matrix):**
14. `bot/calibration.py` — parameter-fit functions (write fitted values to `data/calibration.yaml`)
15. `bot/strategy.py` — pure blending + Kelly, reads `calibration.yaml`
16. `bot/risk.py` — `pre_trade_checks()` gate
17. `bot/executor.py` — gated order placement
18. `main.py` update — `run` command, signing-key requirement

After each Phase A step: I show the file, list exports/deps, write a smoke test you can run, then wait.

---

## 15. CLI reference (Phase A)

```
python main.py scan          # one-shot: print current filtered markets + signals
python main.py whales        # whale scorecard from observed Phase A data
python main.py whale-feed    # live tail of whale activity (incl. signal_type)
python main.py backtest      # replay paper streams; refuses if < 7 days data
python main.py status        # readiness report + per-stream P&L + decision-matrix verdict (HALT|INCONCLUSIVE|BUILD_B_*) with win rates, gaps, p-values
python main.py pnl --days 7 --by-source     # P&L by strategy_id (4 rows) + per-whale breakdown
python main.py add-whale 0x... --label x --trust 0.9   # writes data/whales_manual.json; seeds Beta(α+β=10)
# python main.py run         # Phase B only — refuses to start in Phase A
```

---

## 16. All open questions resolved [UPDATED]

v1 §13 and v2 §15 now closed:

| # | Question | Resolution |
|---|---|---|
| Whale closing trades | log all with `signal_type`; streams act on `open`+`add` only |
| Multi-outcome filter | `outcome_labels` config key, default `["Yes","No"]` case-insensitive |
| `copy_whale` size | proportional with `[5, 500]` USD clamp |
| Null baseline | `null_random_side` (seeded random direction at mid) + `whale_side_random_timing` as a fourth stream |
| `add-whale --trust` | seeds α+β=10 with mean=trust |
| Resolution poller scope | paper-trade markets only |
| Trust scoring math | Bayesian Beta(7,3) prior |
| CLOB testnet | none exists; Phase B writes covered by cassettes only |
| Bankroll source | chain at startup + after every fill; cache between |
| Dataclass home | `bot/models.py` |
| `add-whale` persistence | `data/whales_manual.json` |
| `pnl --by-source` rows | one per stream + per-whale breakdown |

No remaining open questions.

---

## 17. Non-goals

- No live trading until Phase B readiness gate **and** decision matrix pass.
- No websocket feeds (interface ready).
- No on-chain event listening for resolutions.
- No web UI / dashboard — `rich` terminal tables only.
- No multi-outcome markets in Phase A.
- No bridging / deposits / withdrawals.
- No multi-account / multi-key support.
- No on-demand date-range backtest (no historical L2 data).
- Phase A does not load or use `PRIVATE_KEY`. The signing key never enters memory in Phase A.

---

**Awaiting:** "PLAN APPROVED — START BUILDING" or further revisions.
