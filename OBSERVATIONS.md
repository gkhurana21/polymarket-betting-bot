# Observations log

Running journal of daemon health, anomalies, and items to revisit. Append-only;
do not edit prior entries.

## Day 1 — 2026-05-22 — Launch

Daemon live under tmux session `polybot`. 50 whales loaded from leaderboard,
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
