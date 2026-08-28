"""Quantify the cross-venue spread on HIGH-CONFIDENCE matches only.

Only near-identical token sets are trusted. Everything looser is reported as
needing human review, because the failure mode is silent: a pair that resolves
on different criteria is not a hedge, it is two directional bets that happen to
look like one.
"""
from __future__ import annotations
from match import load, match

p_rows, k_rows = load()
pairs = match(p_rows, k_rows, min_sim=0.55)

exact = [p for p in pairs if p.sim >= 0.99]
high  = [p for p in pairs if 0.85 <= p.sim < 0.99]
mid   = [p for p in pairs if 0.70 <= p.sim < 0.85]

print(f"identical token sets (sim = 1.00): {len(exact)}")
print(f"high confidence   (0.85-0.99):     {len(high)}")
print(f"needs review      (0.70-0.85):     {len(mid)}")
print()

trusted = exact + high
if not trusted:
    print("no high-confidence pairs")
    raise SystemExit

print("=== HIGH-CONFIDENCE PAIRS ===")
print(f"{'sim':>5} {'spread':>8} {'PM bid/ask':>14} {'KL bid/ask':>14}  question")
for p in sorted(trusted, key=lambda x: -x.best_spread):
    print(f"{p.sim:>5.2f} {p.best_spread:>+8.3f}  {p.pm_bid:>6.3f}/{p.pm_ask:<6.3f} "
          f"{p.kl_bid:>6.3f}/{p.kl_ask:<6.3f}  {p.pm_q[:52]}")

pos = [p for p in trusted if p.best_spread > 0]
print()
print(f"positive gross spread: {len(pos)} / {len(trusted)}")
if pos:
    sp = sorted(p.best_spread for p in pos)
    print(f"  spread cents: min {sp[0]*100:.1f}  median {sp[len(sp)//2]*100:.1f}  max {sp[-1]*100:.1f}")

# Net of fees. Polymarket taker fee = C*rate*p*(1-p). Kalshi charges a taker
# fee too, commonly quoted as 0.07 * C * p * (1-p), plus maker fees on some
# products. Use documented-ish rates and be explicit that both are assumptions.
PM_RATE, KL_RATE = 0.05, 0.07
print()
print(f"=== NET OF FEES (PM rate {PM_RATE}, KL rate {KL_RATE}, per share) ===")
survivors = []
for p in sorted(trusted, key=lambda x: -x.best_spread):
    if p.spread_kl_to_pm >= p.spread_pm_to_kl:
        buy_p, sell_p, direction = p.kl_ask, p.pm_bid, "buy KL / short PM"
        fee = KL_RATE*p.kl_ask*(1-p.kl_ask) + PM_RATE*p.pm_bid*(1-p.pm_bid)
    else:
        buy_p, sell_p, direction = p.pm_ask, p.kl_bid, "buy PM / short KL"
        fee = PM_RATE*p.pm_ask*(1-p.pm_ask) + KL_RATE*p.kl_bid*(1-p.kl_bid)
    net = p.best_spread - fee
    if p.best_spread > 0:
        print(f"  gross {p.best_spread*100:>5.1f}c  fee {fee*100:>5.1f}c  "
              f"NET {net*100:>+6.1f}c  {direction}  {p.pm_q[:40]}")
        if net > 0:
            survivors.append((net, p))
print()
print(f"pairs still positive AFTER fees: {len(survivors)} / {len(trusted)}")
if survivors:
    for net, p in sorted(survivors, reverse=True)[:5]:
        print(f"   {net*100:+.2f}c  {p.pm_q[:60]}")
