"""Do mutually exclusive Polymarket outcomes ever sum above $1?

RESEARCH ONLY. Offline against data/trades.db. No orders, no network.

────────────────────────────────────────────────────────────────────────────
Why this is not the complement-arb test again
────────────────────────────────────────────────────────────────────────────
Complement arbitrage (YES+NO within ONE market) is closed: 0 crossings in 908
markets, both directions, floored at exactly one tick around $1. But that floor
is MECHANICAL -- split/merge physically converts $1 into 1 YES + 1 NO, so the
constraint enforces itself with nobody watching.

"Exact Score: X a-b" markets are different. Sixteen separate binary markets on
one fixture, at most one of which can resolve YES. Nothing converts between
them. The constraint is arithmetic, not mechanical, so it holds only if
somebody is enforcing it -- and this project has already measured that nobody
is watching thin books.

────────────────────────────────────────────────────────────────────────────
The trade
────────────────────────────────────────────────────────────────────────────
Buy NO on n mutually exclusive legs. At most one can resolve YES, so at least
n-1 pay $1.

    cost   = sum(1 - bid_i)  =  n - sum(bid_i)      [ask_NO = 1 - bid_YES]
    payout >= n - 1
    profit >= sum(bid_i) - 1

So `sum of YES bids > 1` is a guaranteed profit, and the bound holds for ANY
SUBSET of the legs -- missing legs only make the payout better, never worse.
That matters because our book coverage is incomplete, and a test that needed
all 16 legs would be untestable here.

Prices are executable bids walked as real resting size, never midpoints, and
legs whose book is one-sided or absurdly wide are dropped: a stub quote is not
a price, a lesson that cost this project a fabricated t = -3.31.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict

SCORE = re.compile(r"Exact Score:\s*(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+?)\?")
MAX_SPREAD = 0.20          # generous: these are genuinely wide books
BUCKET_MS = 20 * 60 * 1000  # align snapshots taken within 20 minutes


def touch(raw: str):
    """Best bid / best ask from a stored book, or None if unusable."""
    try:
        b = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    bids, asks = b.get("bids") or [], b.get("asks") or []
    if not bids or not asks:
        return None
    try:
        bb = max(float(p) for p, _ in bids)
        ba = min(float(p) for p, _ in asks)
    except (TypeError, ValueError):
        return None
    if not (0.0 < bb < ba < 1.0) or (ba - bb) > MAX_SPREAD:
        return None
    return bb, ba


def main() -> None:
    conn = sqlite3.connect("data/trades.db")
    meta = conn.execute(
        "SELECT market_id, question, end_date FROM market_metadata "
        "WHERE question LIKE 'Exact Score%'").fetchall()

    fixtures: dict[tuple, list[str]] = defaultdict(list)
    for mid, q, ed in meta:
        m = SCORE.search(q or "")
        if m:
            fixtures[(m.group(1).strip(), m.group(4).strip(),
                      (ed or "")[:10])].append(mid)
    print(f"exact-score markets: {len(meta):,}   fixtures: {len(fixtures):,}")

    sums, best = [], []
    fixtures_tested = 0
    for key, mids in fixtures.items():
        if len(mids) < 6:
            continue
        qm = ",".join("?" * len(mids))
        rows = conn.execute(
            f"SELECT market_id, ts, book_json FROM book_snapshots "
            f"WHERE market_id IN ({qm})", mids).fetchall()
        # align legs observed within the same 20-minute window
        by_bucket: dict[int, dict[str, tuple]] = defaultdict(dict)
        for mid, ts, raw in rows:
            t = touch(raw)
            if t:
                by_bucket[ts // BUCKET_MS][mid] = t
        tested_here = False
        for bucket, legs in by_bucket.items():
            if len(legs) < 6:
                continue          # too few legs for the bound to be meaningful
            s = sum(bb for bb, _ in legs.values())
            sums.append((s, len(legs), key))
            tested_here = True
            if s > 1.0:
                best.append((s, len(legs), key, bucket))
        if tested_here:
            fixtures_tested += 1
    conn.close()

    print(f"fixtures with >=6 priced legs in one window: {fixtures_tested}")
    print(f"observations (fixture x time window)       : {len(sums):,}")
    if not sums:
        print("no observations"); return

    vals = sorted(s for s, _, _ in sums)
    print(f"\nsum of YES bids across mutually exclusive legs:")
    for lab, q in (("min", 0.0), ("p05", .05), ("p25", .25), ("median", .5),
                   ("p75", .75), ("p95", .95)):
        print(f"   {lab:>7} {vals[min(int(len(vals)*q), len(vals)-1)]:.4f}")
    print(f"   {'max':>7} {vals[-1]:.4f}")
    print(f"   {'mean':>7} {statistics.fmean(vals):.4f}")

    print(f"\nobservations with sum > 1.00 (guaranteed profit): {len(best)}")
    for s, n, key, _ in sorted(best, reverse=True)[:8]:
        print(f"   sum={s:.4f}  legs={n:>2}  profit=${s-1:.4f}/set  "
              f"{key[0][:22]} vs {key[1][:22]}")


if __name__ == "__main__":
    main()
