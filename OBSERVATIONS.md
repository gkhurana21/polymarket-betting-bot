# Observations log

Running journal of daemon health, anomalies, and items to revisit. Append-only;
do not edit prior entries.

## Day 1 — 2026-05-22 — Launch

Daemon live. 50 whales loaded from leaderboard,
11,366 signals ingested in first cycle (historical backfill, not steady rate),
35/50 whales active. proxyWallet fix confirmed working. Commit 7a0c7fa, tag
phase-a-launch-v2.

### FLAG: High volume of flip-signal warnings during initial replay
Example: yes_p=0.00, no_p=40800.00, new=BUYYES × 100900.00
The values look like raw share sizes, not normalized prices/probabilities.
Possible the signal_type classifier is misreading share quantity as price.
This matters because signal_type (open/add/exit/flip) determines which signals
the copy_whale and whale_side_random_timing streams act on — misclassified
flips could silently drop signals those streams should act on, or vice versa.

ACTION: Revisit day 3-4 once steady-state live flow is visible (separate from
one-time historical replay). DO NOT fix mid-run unless it's actively corrupting
the dataset. First determine: is this replay-only noise that disappears at
steady state, or is it firing on live signals too? Only then decide whether a
restart + fix is warranted.

---

## Phase A.3 deferral — 2026-06-03

### BUG: Auto-discovery never re-runs after startup

`_load_whale_universe()` in `main.py` is called once in `__init__` and never
again. The whale universe is frozen at daemon startup time. PLAN.md §6 specifies
`whale_discovery: 86400` as a periodic interval but this was never implemented
in `run_forever` — no `last_discovery` timer, no periodic leaderboard refresh.

Consequence: any wallet that rises through the Polymarket leaderboard after a
daemon startup is permanently invisible until the next restart.

Discovery: a wallet ranked 28 on the all-time leaderboard was not in the initial
tracked universe.
Manually added via `python main.py add-whale` on 2026-06-03 — written to
`data/whales_manual.json`, will auto-activate on next daemon restart.

### Decision: Do NOT restart to activate the new wallet mid-extension

Marginal benefit (~2-4% dataset contribution from one new whale) does not
justify disrupting 36+ hours of clean in-memory runtime state. It
will join the universe on whatever restart next legitimately occurs (Phase A.3
fix, Phase B prep, or any other planned restart).

### ACTION (Phase A.3 — next planned restart boundary)

Fix `run_forever` to add a `last_discovery` timer and re-run
`_load_whale_universe` on the `whale_discovery` interval, merging new wallets
into `_whale_universe` without dropping existing tracked ones. Safe to implement
between extension boundaries. Do NOT fix during active data collection.
