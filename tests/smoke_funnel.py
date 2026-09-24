"""Replay every false positive this project produced through the funnel.

A filter chain is only worth having if it catches the specific mistakes that
already happened. Each case below is a real candidate that looked good at the
time, with the numbers actually observed.
"""
import sys
sys.path.insert(0, ".")
from bot.funnel import Funnel, FunnelConfig, Leg, Opportunity   # noqa: E402


def leg(price, size, bid=None, ask=None, side="BUY_YES", q=""):
    return Leg(token_id="t", question=q, side=side, price=price,
               size_shares=size, bid=bid if bid is not None else price - 0.001,
               ask=ask if ask is not None else price)


CASES = []

# 1. Stub book: bid 0.001 / ask 0.998 priced as a 99.8% favourite -> t = -3.31
CASES.append(("stub book (fabricated t=-3.31)", "spread",
              Opportunity("complement",
                          [leg(0.998, 500, bid=0.001, ask=0.998)], 1.0), 30))

# 2. Exact-score, as OBSERVED: 32 shares. Fails depth first -- and the real
#    Belgium vs Senegal candidate failed on BOTH depth and fees.
CASES.append(("exact-score as observed (32 shares)", "depth",
              Opportunity("exclusive",
                          [leg(0.9, 32, bid=0.895, ask=0.9, side="BUY_NO")
                           for _ in range(6)], 5.0), 60))

# 2b. Same trade with depth removed as a variable, to isolate FEE geometry:
#     6 legs of NO at p~0.9 pays at most 5 of 6, and the per-leg fee
#     (0.05*0.9*0.1 = 0.45c x 6) exceeds any realistic per-set edge.
CASES.append(("exact-score, ample depth, fee geometry", "fees",
              Opportunity("exclusive",
                          [leg(0.9, 900, bid=0.895, ask=0.9, side="BUY_NO")
                           for _ in range(6)], 5.0), 60))

# 3. Monotonicity Bitcoin: real, 6,000 shares, $15 net -- on $6,000 capital
#    held 4 months. 0.76% annualised. Ranked by gross edge this looks huge.
CASES.append(("Bitcoin $180k/$170k (real, 0.76% ann)", "return",
              Opportunity("monotonicity",
                          [leg(0.016, 6000), leg(0.981, 6000, side="BUY_NO")],
                          1.0), 120))

# 4. Trump approval: +19c gross and 53% annualised -- on 15 shares, $2.67.
CASES.append(("Trump approval, 15 shares", "depth",
              Opportunity("monotonicity",
                          [leg(0.30, 15), leg(0.51, 15, side="BUY_NO")],
                          1.0), 120))

# 5. Thin-market pair worth $0.19 total.
CASES.append(("neg-risk subset artifact, $0.19", "depth",
              Opportunity("exclusive",
                          [leg(0.33, 5) for _ in range(3)], 1.0), 90))

# 6. A trade that SHOULD survive: decent edge, real depth, short horizon.
CASES.append(("genuine 3c edge, 800 shares, 21d", None,
              Opportunity("monotonicity",
                          [leg(0.40, 800), leg(0.57, 800, side="BUY_NO")],
                          1.0), 21))


def run() -> int:
    f = Funnel(FunnelConfig())
    fails = 0
    print(f"{'verdict':<8} {'expected':<9} {'got':<9} {'ann%':>8} {'net$':>9}  case")
    for name, expect, opp, days in CASES:
        got = f.filter_one(opp, days)
        ok = (got == expect)
        fails += (not ok)
        print(f"{'PASS' if ok else 'FAIL':<8} {str(expect):<9} {str(got):<9} "
              f"{opp.annualised(days)*100:>7.2f}% ${opp.net_usd:>8.2f}  {name}")
    print(f"\n{'ALL PASS' if not fails else str(fails)+' FAILURES'}")

    # end-to-end with ranking + capital budget
    f2 = Funnel(FunnelConfig())
    kept = f2.run([(o, d) for _n, _e, o, d in CASES])
    print(f2.report())
    print(f"\nsurvivors: {len(kept)}")
    for o in kept:
        print(f"  ${o.net_usd:>8.2f} on ${o.capital:>9,.2f} capital  "
              f"{o.return_on_capital*100:.2f}% ROC")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
