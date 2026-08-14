"""Regression: expected-reward integration must be PER MARKET.

The cycles table interleaves markets. Integrating it as one timeline charges
most consecutive pairs a dt of ~0 and under-reports by roughly the market
count — which would make the guard's reward-vs-inventory halt trip early on a
business that is actually paying. Found on a real 24h run ($1.67 vs $3.92).
"""

import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday.tracker import Tracker

def t(name, cond):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")
    return cond

ok = True
DB = "/tmp/friday_integ.db"
Path(DB).unlink(missing_ok=True)
tr = Tracker(DB, "/tmp/friday_integ.jsonl")

# Two markets, interleaved, 60s apart, each expecting $2.00/day.
# 10 intervals x 60s = 600s per market = 600/86400 of a day.
# Correct total = 2 markets * $2.00 * (600/86400) = $0.0278
now = int(time.time() * 1000)
for step in range(11):
    for cid in ("0xAAA", "0xBBB"):
        ts = now - (10 - step) * 60_000
        tr._conn.execute(
            "INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) "
            "VALUES (?,?,1,2.0)", (ts, cid))
tr._conn.commit()

got = tr.expected_reward_since(0)
expected = 2 * 2.0 * (600 / 86400)
ok &= t(f"per-market integration ${got:.5f} ~= ${expected:.5f}", abs(got - expected) < 1e-6)

# The flat/naive integration would land near half of this; assert we beat it.
ok &= t("beats the naive single-timeline result", got > expected * 0.9)

# Long gaps (downtime) must not be integrated as if we were resting.
tr2 = Tracker("/tmp/friday_integ2.db", "/tmp/friday_integ2.jsonl")
tr2._conn.execute("DELETE FROM quote_cycles")
tr2._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) VALUES (?,?,1,2.0)",
                  (now - 10 * 3600_000, "0xC"))
tr2._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) VALUES (?,?,1,2.0)",
                  (now, "0xC"))
tr2._conn.commit()
ok &= t("10h gap is not counted as resting time", tr2.expected_reward_since(0) == 0.0)

# Non-quoting cycles contribute nothing.
tr3 = Tracker("/tmp/friday_integ3.db", "/tmp/friday_integ3.jsonl")
for step in range(5):
    tr3._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) VALUES (?,?,0,5.0)",
                      (now - (5 - step) * 60_000, "0xD"))
tr3._conn.commit()
ok &= t("stand-down cycles accrue nothing", tr3.expected_reward_since(0) == 0.0)

tr.close(); tr2.close(); tr3.close()
print("\ntracker integration tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
