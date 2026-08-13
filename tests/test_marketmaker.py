"""Tests for the quoting engine. Focused on the rules that lose money when wrong."""

import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.marketmaker import MarketMaker, MarkEstimator, _floor_tick, _ceil_tick

BASE = {
    "quoting": {"tick": 0.001, "size_shares": 200, "min_size_shares": 200,
                "half_spread": 0.008, "vol_floor_mult": 2.5, "min_half": 0.001,
                "max_half": 0.05, "latency_ticks": 1},
    "mark": {"vol_window": 30, "vol_guess": 0.002, "max_move_ticks": 50,
             "exclude_self_from_mark": True, "mark_mode": "micro", "markout_lag": 10},
    "inventory": {"skew": 5e-6, "soft_inventory_shares": 400,
                  "position_limit_shares": 800, "inventory_cushion": 0.85},
    "toxicity": {"tox_decay": 0.85, "tox_widen": 5e-6, "tox_shrink": 0.25},
    "safety": {"max_loss_usd": 15.0, "kill_loss_usd": 40.0},
}

def mm(band=0.045, **over):
    cfg = {k: dict(v) for k, v in BASE.items()}
    for section, kv in over.items():
        cfg[section].update(kv)
    return MarketMaker(cfg, reward_band=band, min_size_shares=200, condition_id="0xtest")

BOOK_B = [(0.49, 5000.0), (0.489, 3000.0)]
BOOK_A = [(0.51, 5000.0), (0.511, 3000.0)]

def t(name, cond):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")
    return cond

ok = True

# 1. THE central rule: vol floor above the reward band -> refuse to quote.
m = mm(band=0.002, mark={"vol_guess": 0.01})        # floor = 2.5*0.01*2 = 0.05 >> 0.002
q = m.quote(BOOK_B, BOOK_A, 0, 0)
ok &= t("vol floor > reward band -> refuses to quote", not q.quoting and q.reason == "vol_exceeds_reward_band")

# ...and the same market with a wide band DOES quote.
m = mm(band=0.06, mark={"vol_guess": 0.01})
q = m.quote(BOOK_B, BOOK_A, 0, 0)
ok &= t("same vol, wide band -> quotes", q.quoting)

# 2. Half spread never exceeds the reward band (or rewards are forfeited).
m = mm(band=0.01, mark={"vol_guess": 0.0005})
q = m.quote(BOOK_B, BOOK_A, 0, 0)
half = (q.ask_px - q.bid_px) / 2
ok &= t(f"half spread {half:.4f} <= band 0.010", half <= 0.010 + 1e-9)

# 3. One-sided book must not produce a non-finite price, and must hold the mark.
m = mm()
m.quote(BOOK_B, BOOK_A, 0, 0)                        # seed a mark
before = m.mark.last
q = m.quote(BOOK_B, [], 0, 0)                        # asks vanish
ok &= t("one-sided book holds mark (no jump)", abs(m.mark.last - before) < 1e-9)
ok &= t("one-sided book yields finite prices", all(math.isfinite(x) for x in (q.bid_px, q.ask_px)))

# 4. Toxicity: repeated bid hits must push the bid down and shrink it.
m = mm()
q0 = m.quote(BOOK_B, BOOK_A, 0, 0)
for _ in range(6):
    m.on_fill(+200, 0.49)                            # our bid keeps getting hit
q1 = m.quote(BOOK_B, BOOK_A, 0, 0)
ok &= t(f"toxic bid flow pushes bid down ({q0.bid_px:.4f} -> {q1.bid_px:.4f})", q1.bid_px < q0.bid_px)

# 5. Sub-min_size quote is pulled, not rested (it would earn zero reward).
m = mm(quoting={"size_shares": 210}, toxicity={"tox_shrink": 5.0})
for _ in range(10):
    m.on_fill(+200, 0.49)
q = m.quote(BOOK_B, BOOK_A, 0, 0)
ok &= t("shrunk-below-min_size bid is pulled to 0", q.bid_sz == 0.0)

# 6. Inventory: long past soft limit stops bidding, keeps offering.
m = mm()
q = m.quote(BOOK_B, BOOK_A, 500, 0)                  # soft_inventory = 400
ok &= t("long past soft inventory -> no bid", q.bid_sz == 0.0)
ok &= t("long past soft inventory -> still offers", q.ask_sz > 0.0)

# 7. Kill switch latches.
m = mm()
m.quote(BOOK_B, BOOK_A, 0, 100.0)                    # peak pnl 100
q = m.quote(BOOK_B, BOOK_A, 0, 40.0)                 # drawdown 60 > kill 40
ok &= t("kill switch stops quoting", not q.quoting and q.reason == "killed")
q = m.quote(BOOK_B, BOOK_A, 0, 100.0)                # recovery must NOT un-kill
ok &= t("kill switch latches (no revival)", not q.quoting)

# 8. Prices stay inside (0,1) — this venue is a probability.
m = mm(band=0.05)
q = m.quote([(0.002, 5000.0)], [(0.004, 5000.0)], 0, 0)
ok &= t(f"prices clamped into (0,1): bid={q.bid_px:.4f}", 0.0 < q.bid_px < 1.0 and 0.0 < q.ask_px < 1.0)

# 9. Tick helpers reject non-finite input rather than emitting garbage.
try:
    _floor_tick(float("inf"), 0.001); ok &= t("floor_tick rejects inf", False)
except ValueError:
    ok &= t("floor_tick rejects inf", True)

print("\nmarketmaker tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
