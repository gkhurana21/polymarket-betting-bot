"""Reward reconciliation tests.

The distinction that matters: "captured nothing" and "never measured capture"
are different claims. Conflating them turns a projection into a result.
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday.rewards import Rewards
from friday.tracker import Tracker

def t(n,c): print(f"  {'OK  ' if c else 'FAIL'} {n}"); return c
ok=True
DB="/tmp/friday_rw.db"; Path(DB).unlink(missing_ok=True)
tr=Tracker(DB,"/tmp/friday_rw.jsonl"); r=Rewards(tr)
now=int(time.time()*1000)

# Log some expected accrual: one market, 10 x 60s at $2/day.
for step in range(11):
    tr._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) "
                     "VALUES (?,?,1,2.0)", (now-(10-step)*60_000,"0xA"))
tr._conn.commit()

rec=r.reconcile(window_days=7)
ok&=t(f"expected accrued ${rec.expected_usd:.5f}", rec.expected_usd>0)
ok&=t("no credits -> capture_ratio is None, NOT 0", rec.capture_ratio is None)
ok&=t("no credits -> has_realised False", not rec.has_realised)
ok&=t("render warns loudly with no realised data", "NO REALISED DATA" in rec.render())

# Now import a realised credit.
r.record_credit("0xA", rec.expected_usd, "2026-08-16")
rec2=r.reconcile(window_days=7)
ok&=t("credit recorded", rec2.has_realised and rec2.realised_usd>0)
ok&=t(f"capture ratio computed ({rec2.capture_ratio:.0%})",
      rec2.capture_ratio is not None and abs(rec2.capture_ratio-1.0)<0.01)
ok&=t("full capture does not trigger the shortfall warning",
      "well below projection" not in rec2.render())

# Under-capture must be called out.
tr2=Tracker("/tmp/friday_rw2.db","/tmp/friday_rw2.jsonl"); r2=Rewards(tr2)
for step in range(11):
    tr2._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) "
                      "VALUES (?,?,1,10.0)", (now-(10-step)*60_000,"0xB"))
tr2._conn.commit()
exp=r2.reconcile(7).expected_usd
r2.record_credit("0xB", exp*0.3, "2026-08-16")
rec3=r2.reconcile(7)
ok&=t(f"under-capture flagged ({rec3.capture_ratio:.0%})",
      "well below projection" in rec3.render())

# Same market+day twice must upsert, not double count.
r.record_credit("0xA", 5.0, "2026-08-16")
r.record_credit("0xA", 5.0, "2026-08-16")
ok&=t("credit for same market+day upserts", abs(r.reconcile(7).realised_usd-5.0)<1e-6)

tr.close(); tr2.close()
print("\nrewards tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
