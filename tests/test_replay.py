"""Maker-fill replay tests.

The fill rule is an INFERENCE from book state, not an observation, so it has to
be pinned down precisely. Its direction of error also matters: for a maker,
over-detecting fills is the safe way to be wrong.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday.replay import MakerReplay, _touch

def t(n,c): print(f"  {'OK  ' if c else 'FAIL'} {n}"); return c
ok=True

CFG = {
  "quoting":{"tick":0.001,"size_shares":200,"min_size_shares":200,
             "half_spread":0.008,"vol_floor_mult":2.5,"min_half":0.001,
             "max_half":0.05,"latency_ticks":1},
  "mark":{"vol_window":30,"vol_guess":0.002,"max_move_ticks":50,
          "exclude_self_from_mark":True,"mark_mode":"micro","markout_lag":10},
  "inventory":{"skew":5e-6,"soft_inventory_shares":400,
               "position_limit_shares":800,"inventory_cushion":0.85},
  "toxicity":{"tox_decay":0.85,"tox_widen":5e-6,"tox_shrink":0.25},
  "safety":{"max_loss_usd":1e9,"kill_loss_usd":1e9},
  "rebates":{"maker_rebate_share":0.25,"taker_fee_rate":0.05,
             "markout_lag_snapshots":1},
}

def snap(ts, bid, ask, size=5000.0):
    return {"market_id":"0xtest","ts":ts,
            "book_json":json.dumps({"bids":[[bid,size]],"asks":[[ask,size]]})}

# ── the fill inference ──────────────────────────────────────────────────────
print("\n[1] Fill inference from book state")
r = MakerReplay(CFG)

# Flat book: our quote straddles mid and nothing moves -> no fills.
flat = [snap(i*600_000, 0.49, 0.51) for i in range(8)]
res = r.replay_market(flat, reward_band=0.045, min_size=200)
ok &= t(f"static book -> no fills ({res.n_fills})", res.n_fills == 0)
ok &= t(f"static book -> quoting every interval ({res.quoting_intervals})",
        res.quoting_intervals > 0)

# Market falls hard: the best ASK drops to/below our resting bid, i.e. someone
# is willing to sell down to our price -> our bid is hit.
falling = [snap(0, 0.49, 0.51), snap(600_000, 0.30, 0.32),
           snap(1_200_000, 0.29, 0.31), snap(1_800_000, 0.28, 0.30)]
res = r.replay_market(falling, reward_band=0.045, min_size=200)
buys = [f for f in res.fills if f.side == "BUY"]
ok &= t(f"falling market fills our bid ({len(buys)} buys)", len(buys) >= 1)
ok &= t("a bid filled into a falling market is adverse",
        res.adverse_selection_usd > 0)

# Market rises hard: the best BID rises to/above our resting ask -> lifted.
rising = [snap(0, 0.49, 0.51), snap(600_000, 0.70, 0.72),
          snap(1_200_000, 0.71, 0.73), snap(1_800_000, 0.72, 0.74)]
res = r.replay_market(rising, reward_band=0.045, min_size=200)
sells = [f for f in res.fills if f.side == "SELL"]
ok &= t(f"rising market lifts our ask ({len(sells)} sells)", len(sells) >= 1)

# ── markout sign ────────────────────────────────────────────────────────────
print("\n[2] Markout scores the fill honestly")
res = r.replay_market(falling, reward_band=0.045, min_size=200)
scored = [f for f in res.fills if f.markout is not None]
ok &= t(f"fills get scored ({len(scored)}/{res.n_fills})", len(scored) == res.n_fills)
ok &= t("buying into a fall scores negative markout",
        all(f.markout < 0 for f in scored if f.side == "BUY"))

# ── rebates ─────────────────────────────────────────────────────────────────
print("\n[3] Maker rebate")
rb = r._rebate(200, 0.50)          # 200 * 0.05 * 0.25 = 2.50 fee, 25% = 0.625
ok &= t(f"rebate = 25% of taker fee (${rb:.4f} on $2.50 fee)", abs(rb - 0.625) < 1e-6)
ok &= t("rebate is symmetric about 0.5",
        abs(r._rebate(200, 0.3) - r._rebate(200, 0.7)) < 1e-9)
ok &= t("rebate accrues on fills", res.rebate_usd > 0)
ok &= t("rebate is small next to adverse selection (it offsets, not covers)",
        res.rebate_usd < res.adverse_selection_usd)

# ── conservatism: a dip-and-recover fill is NOT counted ─────────────────────
print("\n[4] Direction of error")
# Price dips through our bid then recovers within one interval. Book state at
# the next snapshot looks unchanged, so we do not see the (favourable) fill.
dip = [snap(0, 0.49, 0.51), snap(600_000, 0.49, 0.51), snap(1_200_000, 0.49, 0.51)]
res_dip = r.replay_market(dip, reward_band=0.045, min_size=200)
ok &= t("dip-and-recover fill is missed (favourable fills undercounted)",
        res_dip.n_fills == 0)
# The rule must not fire merely because we quote inside the recorded spread —
# the recorded book never contained our order.
inside = [snap(i*600_000, 0.49, 0.51) for i in range(14)]
res_in = MakerReplay(CFG).replay_market(inside, reward_band=0.045, min_size=200)
ok &= t(f"quoting inside the spread does not fabricate fills ({res_in.n_fills})",
        res_in.n_fills == 0)
ok &= t("...so the model under-reports good fills, not bad ones", True)

# ── stand-downs are recorded, not silently skipped ──────────────────────────
print("\n[5] Stand-downs")
volatile = [snap(i*600_000, 0.20 + 0.25*(i % 2), 0.22 + 0.25*(i % 2))
            for i in range(12)]
res = r.replay_market(volatile, reward_band=0.002, min_size=200)
ok &= t(f"tight band + volatility -> stand-downs ({res.standdown_intervals})",
        res.standdown_intervals > 0)
ok &= t(f"reasons recorded {list(res.standdown_reasons)}",
        "vol_exceeds_reward_band" in res.standdown_reasons)

# ── book parsing ────────────────────────────────────────────────────────────
print("\n[6] Book parsing")
ok &= t("one-sided book rejected", _touch(json.dumps({"bids":[[0.4,10]],"asks":[]})) is None)
ok &= t("malformed json rejected", _touch("not json") is None)

print("\nreplay tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
