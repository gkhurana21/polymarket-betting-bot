"""Guard + post-mortem tests. These decide whether we keep trading, so the
arithmetic has to be right and the headline number has to be NET."""

import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.guard import Guard
from friday.postmortem import PostMortem
from friday.tracker import Tracker

def t(name, cond):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")
    return cond

ok = True
DB = "/tmp/friday_guard_test.db"
Path(DB).unlink(missing_ok=True)
tr = Tracker(DB, "/tmp/friday_guard_test.jsonl")
cfg = {"safety": {"kill_loss_usd": 40.0, "reward_vs_inventory_window_days": 5},
       "guard": {"news_halt_enabled": False}}

# Healthy: no fills, nothing to halt on.
g = Guard(cfg)
ok &= t("clean state allows quoting", g.check(tr, 0.0).allowed)

# Kill switch on realised loss.
g2 = Guard(cfg)
ok &= t("realised loss past kill halts", not g2.check(tr, -45.0).allowed)
ok &= t("halt latches after recovery", not g2.check(tr, 0.0).allowed)

# The operating rule: inventory loss exceeding reward income halts.
tr.record_reward("0xa", 2.0, "2026-08-13")
now = int(time.time() * 1000)
for _ in range(3):
    fid = tr.record_fill("0xa", "tok", 200, 0.50)
    tr._conn.execute("UPDATE fills SET markout_at_lag=? WHERE id=?", (-0.02, fid))
tr._conn.commit()
# 3 fills x 200 shares x -0.02 = -$12 against $2 of rewards
g3 = Guard(cfg)
v = g3.check_reward_vs_inventory(tr)
ok &= t("inventory loss > reward income halts", not v.allowed)
ok &= t("halt reason names both numbers", "12.00" in v.reason and "2.00" in v.reason)

# ...and the reverse: small inventory loss under good rewards does not halt.
Path("/tmp/friday_ok.db").unlink(missing_ok=True)
tr2 = Tracker("/tmp/friday_ok.db", "/tmp/friday_ok.jsonl")
tr2.record_reward("0xb", 20.0, "2026-08-13")
fid = tr2.record_fill("0xb", "tok", 200, 0.50)
tr2._conn.execute("UPDATE fills SET markout_at_lag=? WHERE id=?", (-0.01, fid))
tr2._conn.commit()
ok &= t("small loss under good rewards continues", Guard(cfg).check_reward_vs_inventory(tr2).allowed)

# Post-mortem must report NET, and net must subtract adverse selection.
rep = PostMortem(tr, cfg).report(window_days=5)
ok &= t(f"adverse selection counted (${rep.adverse_selection_usd:.2f})",
        abs(rep.adverse_selection_usd - 12.0) < 1e-6)
ok &= t(f"net is below gross reward ({rep.net_pnl_usd:+.2f} < {rep.reward_realised_usd:+.2f})",
        rep.net_pnl_usd < rep.reward_realised_usd)
ok &= t("negative markout flagged as quoting too tight",
        "QUOTING TOO TIGHT" in rep.render())

# Expected-vs-realised must be labelled, never silently blended.
Path("/tmp/friday_exp.db").unlink(missing_ok=True)
tr3 = Tracker("/tmp/friday_exp.db", "/tmp/friday_exp.jsonl")
for i in range(3):
    tr3.record_quote_cycle({"condition_id": "0xc", "quoting": 1,
                            "expected_usd_per_day": 2.0, "mid": 0.5})
    time.sleep(0.01)
r3 = PostMortem(tr3, cfg).report(window_days=5)
ok &= t("expected reward flagged as projection, not realised",
        "EXPECTED, not realised" in r3.render())

tr.close(); tr2.close(); tr3.close()
print("\nguard/report tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
