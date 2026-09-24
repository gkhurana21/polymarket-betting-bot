"""Track how long monotonicity violations PERSIST.

RESEARCH ONLY. Read-only public endpoints, no orders, no keys.
Appends to data/mono_monitor.db; never touches trades.db.

────────────────────────────────────────────────────────────────────────────
Why persistence is the only variable left
────────────────────────────────────────────────────────────────────────────
Hypothesis 11 found REAL violations with real depth (Bitcoin $180k at 0.019
against $170k at 0.012, on 6,000 shares) and rejected them at 1.17%
annualised. That rejection rested on one assumption I never questioned:
holding to resolution.

    return = edge / capital,  annualised over the HOLD

Capital is ~$1 per set and cannot be levered -- that is settled. Edge is the
violation size, cents on a dollar -- that is settled. The hold is the only
term never measured.

    0.25% over 4 months  ->  0.76% annualised
    0.25% over 3 days    ->  ~30% annualised

Same edge, same capital. If violations CORRECT rather than sitting until
resolution, you exit and redeploy, and the whole rejection is wrong.

This cannot be answered from the stored corpus: only 2 of 4,487 backfilled
questions are directional, because threshold ladders resolve in late
2026/2027 and none have resolved yet. So it must be collected forward.

────────────────────────────────────────────────────────────────────────────
What honest persistence data requires
────────────────────────────────────────────────────────────────────────────
Snapshot the same pairs repeatedly and record whether each violation is still
open. A violation seen once is noise; one still open at the next tick is a
data point; one open across many ticks is a finding.

Every tick stores ALL scanned pairs, not just violating ones -- otherwise
there is no denominator and no way to distinguish "violations persist" from
"we only ever look when one exists".
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict

GAMMA = "https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset={off}"
BOOK = "https://clob.polymarket.com/book?token_id={tok}"
DB = "data/mono_monitor.db"
FEE = 0.05
MAX_SPREAD = 0.15

UP = re.compile(r"\b(reach|reaches|above|exceed|exceeds|greater|over|hit|hits|"
                r"or more|at least)\b", re.I)
DOWN = re.compile(r"\b(dip|dips|below|under|less than|fall|falls|drop|drops|"
                  r"or fewer|or lower|or below|or less|at most)\b", re.I)
NUM = re.compile(r"\$?(\d[\d,]*\.?\d*)\s*(k|K|m|M|B|T)?")


def get(url, timeout=20.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def scale(v, suf):
    return float(v.replace(",", "")) * {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6,
                                        "B": 1e9, "T": 1e12}.get(suf or "", 1.0)


def touch(tok):
    b = get(BOOK.format(tok=tok))
    if not b:
        return None
    bids, asks = b.get("bids") or [], b.get("asks") or []
    if not bids or not asks:
        return None
    try:
        bb, bsz = max((float(x["price"]), float(x["size"])) for x in bids)
        ba, asz = min((float(x["price"]), float(x["size"])) for x in asks)
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 < bb < ba < 1.0) or (ba - bb) > MAX_SPREAD:
        return None
    return bb, bsz, ba, asz


def tok0(m):
    try:
        t = json.loads(m["clobTokenIds"])
        return str(t[0]) if len(t) == 2 else None
    except Exception:
        return None


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS obs (
        ts INTEGER, pair_key TEXT, kind TEXT,
        strong_id TEXT, weak_id TEXT, strong_q TEXT, weak_q TEXT,
        bid_strong REAL, ask_weak REAL, size_strong REAL,
        gross REAL, net_usd REAL, violating INTEGER,
        PRIMARY KEY (ts, pair_key))""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_pair ON obs(pair_key, ts)")
    conn.commit()


def build_pairs(rows):
    thr = defaultdict(list)
    for r in rows:
        q = r.get("question") or ""
        if not tok0(r):
            continue
        m = NUM.search(q)
        if not m:
            continue
        masked = NUM.sub("#", q)
        up, down = bool(UP.search(masked)), bool(DOWN.search(masked))
        if down:
            up = False          # "reach X or lower" is a DOWN ladder
        if not (up or down):
            continue
        thr[(masked, (r.get("endDate") or "")[:10], up)].append(
            (scale(m.group(1), m.group(2)), r))
    pairs = []
    for (masked, _e, up), legs in thr.items():
        if len(legs) < 2:
            continue
        legs.sort(key=lambda x: x[0])
        for i in range(len(legs) - 1):
            lo, hi = legs[i][1], legs[i + 1][1]
            strong, weak = (hi, lo) if up else (lo, hi)
            pairs.append(("threshold", strong, weak))
    return pairs


def main():
    rows, off = [], 0
    while off < 8000:
        p = get(GAMMA.format(off=off))
        if not p:
            break
        rows.extend(p)
        if len(p) < 100:
            break
        off += 100
        time.sleep(0.1)
    pairs = build_pairs(rows)
    print(f"open markets {len(rows):,} | implication pairs {len(pairs)}", flush=True)

    conn = sqlite3.connect(DB, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure(conn)
    now = int(time.time() * 1000)
    cache, stored, viol = {}, 0, 0
    for kind, strong, weak in pairs:
        ts_, tw = tok0(strong), tok0(weak)
        if not ts_ or not tw:
            continue
        for t in (ts_, tw):
            if t not in cache:
                cache[t] = touch(t)
                time.sleep(0.09)
        a, b = cache[ts_], cache[tw]
        if a is None or b is None:
            continue
        bid_s, size_s, _, _ = a
        _, _, ask_w, _ = b
        gross = bid_s - ask_w
        fees = (FEE * bid_s * (1 - bid_s) + FEE * ask_w * (1 - ask_w)) * size_s
        net = gross * size_s - fees
        key = f"{strong.get('id')}|{weak.get('id')}"
        # store EVERY pair, not only violations -- without the denominator
        # there is no way to tell persistence from selective looking
        conn.execute("INSERT OR REPLACE INTO obs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (now, key, kind, str(strong.get("id")), str(weak.get("id")),
                      (strong.get("question") or "")[:90],
                      (weak.get("question") or "")[:90],
                      bid_s, ask_w, size_s, gross, net, 1 if gross > 0 else 0))
        stored += 1
        if gross > 0:
            viol += 1
    conn.commit()

    ticks = conn.execute("SELECT COUNT(DISTINCT ts) FROM obs").fetchone()[0]
    print(f"tick stored: {stored} pairs, {viol} violating", flush=True)
    print(f"total ticks in db: {ticks}")
    if ticks >= 2:
        print("\n── persistence across ticks ──")
        rowsx = conn.execute("""
            SELECT pair_key, COUNT(*) n, SUM(violating) v,
                   MIN(ts) t0, MAX(ts) t1, MAX(strong_q)
            FROM obs GROUP BY pair_key HAVING v > 0 ORDER BY v DESC LIMIT 15
        """).fetchall()
        print(f"{'seen':>5} {'violating':>10} {'span(h)':>9}  market")
        for _k, n, v, t0, t1, q in rowsx:
            print(f"{n:>5} {v:>10} {(t1-t0)/3.6e6:>8.1f}h  {(q or '')[:52]}")
    else:
        print("\nfirst tick recorded. Run again later to measure persistence.")
    conn.close()


if __name__ == "__main__":
    main()
