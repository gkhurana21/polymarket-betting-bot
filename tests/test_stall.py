"""Stall detection: a dashboard that reports healthy while the loop is dead is
worse than one that is down. This is the failure that went unnoticed for 7 days."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday import dashboard
from friday.tracker import Tracker

def t(n,c): print(f"  {'OK  ' if c else 'FAIL'} {n}"); return c
ok=True
CFG={"dry_run":True,"bankroll_usd":500,"safety":{},"guard":{}}

# No cycles at all -> stalled, not "healthy with zero activity".
DB="/tmp/friday_stall.db"; Path(DB).unlink(missing_ok=True)
tr=Tracker(DB,"/tmp/friday_stall.jsonl")
st=dashboard.build_state(tr,CFG)
ok&=t("empty db reports stalled", st["loop"]["stalled"] is True)
ok&=t("empty db age is None (never ran), not 0", st["loop"]["age_s"] is None)

# A fresh cycle clears the stall.
now=int(time.time()*1000)
tr._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) "
                 "VALUES (?,?,1,2.0)",(now,"0xA")); tr._conn.commit()
st=dashboard.build_state(tr,CFG)
ok&=t("recent cycle -> not stalled", st["loop"]["stalled"] is False)
ok&=t(f"age reported ({st['loop']['age_s']:.0f}s)", st["loop"]["age_s"] < 5)

# An old cycle re-raises the stall — this is the 7-day case.
tr._conn.execute("DELETE FROM quote_cycles")
tr._conn.execute("INSERT INTO quote_cycles (ts,condition_id,quoting,expected_usd_per_day) "
                 "VALUES (?,?,1,2.0)",(now-7*86400*1000,"0xA")); tr._conn.commit()
st=dashboard.build_state(tr,CFG)
ok&=t("7-day-old cycle reports stalled", st["loop"]["stalled"] is True)
ok&=t(f"age ~7d ({st['loop']['age_s']/86400:.1f}d)", st["loop"]["age_s"]>6*86400)

# Thread liveness is surfaced separately from cycle freshness.
dashboard.LOOP_ALIVE["alive"]=False
ok&=t("dead thread reported", dashboard.build_state(tr,CFG)["loop"]["alive"] is False)
dashboard.LOOP_ALIVE["alive"]=None
tr.close()
print("\nstall tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
