"""Favourite-longshot bias study over the recorded book corpus.

RESEARCH ONLY. Reads data/trades.db. Places no orders, opens no network
connections, writes nothing back to the trading tables.

────────────────────────────────────────────────────────────────────────────
The hypothesis
────────────────────────────────────────────────────────────────────────────
In betting and prediction markets, low-probability outcomes ("longshots")
persistently trade ABOVE their realised frequency and favourites BELOW it.
If that holds on Polymarket, then buying the favourite side at the ask and
holding to resolution has positive expectancy — with no information edge
required.

This matters because it is the first hypothesis in this project whose failure
mode is not already established:

  * whale copying died because information is priced before we can act.
    Irrelevant here: this edge needs no information.
  * complement arbitrage died on the mint floor (split/merge pins YES+NO to
    $1). Irrelevant here: we buy ONE side, so nothing pins the price.
  * market making died on adverse selection. Irrelevant here: we are the
    taker, deliberately.

And Polymarket's own fee curve, fee = C x rate x p x (1-p), is MINIMISED at
the price extremes — exactly where longshot bias lives. Every prior
hypothesis died on costs; this one is structurally cheap.

────────────────────────────────────────────────────────────────────────────
Method (and the traps it is built to avoid)
────────────────────────────────────────────────────────────────────────────
1. EXECUTABLE PRICES ONLY. We buy at the ask, walking real book depth. Using
   the midpoint is what made whale-copying look like +21.8% when it was
   -18.1%. The YES book is recorded; the NO book is derived by complement:

       ask_NO = 1 - bid_YES        bid_NO = 1 - ask_YES

   which is exactly the relation the complement-arb study measured live
   (ask_YES + ask_NO = 1.001, one tick wide).

2. RESOLUTION BUFFER. `resolved_at_ts` is when our poller DETECTED the
   resolution, not when it happened. The poll interval is 1800s, so a
   snapshot shortly before that timestamp may already be post-outcome. We
   drop everything inside `--buffer-hours` (default 6h, 12x the poll gap).
   Without this, prices that have already converged to 0/1 manufacture a
   spectacular fake edge.

3. ONE TRADE PER MARKET. 36,739 snapshots over 4,552 markets are NOT
   independent observations. The headline test takes a single snapshot per
   market, so each market contributes exactly one outcome. Pooling all
   snapshots inflates the t-statistic by roughly sqrt(8).

4. OUT-OF-SAMPLE SPLIT by resolution date. The mid-band copy edge looked like
   +21.8% in sample and +7.5% out of it; the gap is the overfitting tax. Any
   result here is reported both ways or not at all.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

# Polymarket taker fee. Peaks at p=0.5, vanishes at the extremes.
# Common-case category rate; Crypto is 0.07, Geopolitics 0.
TAKER_FEE_RATE = 0.05

MS_PER_HOUR = 3_600_000.0


def taker_fee_per_share(price: float, rate: float = TAKER_FEE_RATE) -> float:
    """fee = rate x p x (1-p), charged per share to takers only."""
    if not (0.0 < price < 1.0):
        return 0.0
    return rate * price * (1.0 - price)


@dataclass(frozen=True)
class Quote:
    """One executable observation of a market at a point in time."""
    market_id: str
    ts: int
    horizon_h: float
    resolved_yes: bool
    ask_yes: float          # cost to BUY one YES share
    bid_yes: float          # proceeds to SELL one YES share
    ask_no: float           # cost to BUY one NO share  = 1 - bid_yes
    depth_yes: float        # shares available at best ask
    depth_no: float         # shares available at best NO ask (= best YES bid)


def parse_book(raw: str) -> tuple[float, float, float, float] | None:
    """Return (best_bid, bid_size, best_ask, ask_size) or None if one-sided.

    A one-sided book has no executable price on the missing side. The
    mm-harness learned this the hard way — a one-sided book produced an
    infinite quote. We drop rather than extrapolate.
    """
    try:
        book = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    try:
        best_bid, bid_sz = max(((float(p), float(s)) for p, s in bids),
                               key=lambda t: t[0])
        best_ask, ask_sz = min(((float(p), float(s)) for p, s in asks),
                               key=lambda t: t[0])
    except (TypeError, ValueError):
        return None
    if not (0.0 < best_bid < best_ask < 1.0):
        return None      # crossed, degenerate, or at a boundary
    return best_bid, bid_sz, best_ask, ask_sz


def load_quotes(db_path: str, buffer_hours: float,
                min_horizon_h: float, max_horizon_h: float,
                max_spread: float = 0.05) -> list[Quote]:
    """Load executable observations for resolved markets.

    `max_spread` is not cosmetic. A book quoting bid 0.001 / ask 0.998 has a
    technically executable ask but no economic price — it is one stub order on
    each side of an empty book. Pricing those as 99.8% favourites put 8 markets
    that resolved NO into the 0.98-1.00 bucket and fabricated a t = -3.31.
    Only 2.2% of markets are wider than 10c, but they cluster in exactly the
    extreme buckets this study is about.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT b.market_id, b.ts, b.book_json,
               r.resolved_outcome, r.resolved_at_ts
        FROM book_snapshots b
        JOIN resolved_markets r ON r.market_id = b.market_id
        WHERE r.resolved_outcome IN ('YES','NO')
    """).fetchall()
    conn.close()

    out: list[Quote] = []
    for row in rows:
        horizon_h = (row["resolved_at_ts"] - row["ts"]) / MS_PER_HOUR
        if horizon_h < buffer_hours:
            continue                      # inside the detection-lag shadow
        if not (min_horizon_h <= horizon_h <= max_horizon_h):
            continue
        parsed = parse_book(row["book_json"])
        if parsed is None:
            continue
        best_bid, bid_sz, best_ask, ask_sz = parsed
        if best_ask - best_bid > max_spread:
            continue                      # no economic price; see docstring
        out.append(Quote(
            market_id=row["market_id"],
            ts=row["ts"],
            horizon_h=horizon_h,
            resolved_yes=(row["resolved_outcome"] == "YES"),
            ask_yes=best_ask,
            bid_yes=best_bid,
            ask_no=1.0 - best_bid,
            depth_yes=ask_sz,
            depth_no=bid_sz,
        ))
    return out


def one_per_market(quotes: list[Quote]) -> list[Quote]:
    """Keep the EARLIEST qualifying snapshot per market.

    Earliest, not random: it is the one a live bot could actually have acted
    on first, and it is furthest from the resolution shadow.
    """
    best: dict[str, Quote] = {}
    for q in quotes:
        cur = best.get(q.market_id)
        if cur is None or q.ts < cur.ts:
            best[q.market_id] = q
    return sorted(best.values(), key=lambda q: q.ts)


def welch_t(sample: list[float]) -> tuple[float, float, float]:
    """Return (mean, stderr, t) for a one-sample test against zero."""
    n = len(sample)
    if n < 2:
        return (0.0, 0.0, 0.0)
    mean = statistics.fmean(sample)
    sd = statistics.stdev(sample)
    se = sd / math.sqrt(n)
    return (mean, se, (mean / se) if se > 0 else 0.0)


# ── calibration: does price predict frequency? ──────────────────────────────

BUCKETS = [(0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
           (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80),
           (0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 1.00)]


def calibration_table(quotes: list[Quote]) -> list[dict]:
    """For each ask-price bucket: how often did YES actually happen?

    Priced on the YES ASK — the price a buyer actually pays. If realised
    frequency exceeds the ask, buying YES in that bucket is +EV before fees.
    """
    rows = []
    for lo, hi in BUCKETS:
        sel = [q for q in quotes if lo <= q.ask_yes < hi]
        if not sel:
            continue
        n = len(sel)
        mean_ask = statistics.fmean(q.ask_yes for q in sel)
        freq = sum(1 for q in sel if q.resolved_yes) / n
        # EV per share of buying YES at ask, net of the taker fee.
        pnl = [(1.0 if q.resolved_yes else 0.0) - q.ask_yes
               - taker_fee_per_share(q.ask_yes) for q in sel]
        mean, se, t = welch_t(pnl)
        rows.append(dict(lo=lo, hi=hi, n=n, mean_ask=mean_ask, freq=freq,
                         edge=freq - mean_ask, ev_net=mean, se=se, t=t))
    return rows


# ── strategy: buy one side, hold to resolution ─────────────────────────────

def evaluate_side(quotes: list[Quote], side: str,
                  lo: float, hi: float) -> dict | None:
    """P&L per $1 staked of buying `side` at the ask, when that ask is in
    [lo, hi), held to resolution. Fees charged once, on entry."""
    trades = []
    for q in quotes:
        price = q.ask_yes if side == "YES" else q.ask_no
        if not (lo <= price < hi):
            continue
        won = q.resolved_yes if side == "YES" else (not q.resolved_yes)
        payoff = 1.0 if won else 0.0
        fee = taker_fee_per_share(price)
        # Return on capital deployed: capital at risk is the ask itself.
        trades.append((payoff - price - fee) / price)
    if len(trades) < 30:
        return None
    mean, se, t = welch_t(trades)
    wins = sum(1 for r in trades if r > 0)
    return dict(side=side, lo=lo, hi=hi, n=len(trades), roi=mean,
                se=se, t=t, winrate=wins / len(trades))


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--buffer-hours", type=float, default=6.0,
                    help="drop snapshots this close to detected resolution")
    ap.add_argument("--min-horizon", type=float, default=0.0)
    ap.add_argument("--max-horizon", type=float, default=1e9)
    ap.add_argument("--max-spread", type=float, default=0.05,
                    help="drop books wider than this (no economic price)")
    ap.add_argument("--all-snapshots", action="store_true",
                    help="pool every snapshot (inflates t; shape only)")
    args = ap.parse_args()

    raw = load_quotes(args.db, args.buffer_hours,
                      args.min_horizon, args.max_horizon, args.max_spread)
    quotes = raw if args.all_snapshots else one_per_market(raw)

    print(f"corpus         : {len(raw):,} usable snapshots"
          f"  /  {len({q.market_id for q in raw}):,} markets")
    print(f"sample         : {len(quotes):,} observations"
          f"  ({'all snapshots' if args.all_snapshots else 'one per market'})")
    if not quotes:
        print("no data after filters")
        return
    print(f"buffer         : {args.buffer_hours}h before detected resolution")
    print(f"max spread     : {args.max_spread:.3f}")
    print(f"horizon        : {min(q.horizon_h for q in quotes):.1f}h "
          f"to {max(q.horizon_h for q in quotes):.1f}h")
    base = sum(1 for q in quotes if q.resolved_yes) / len(quotes)
    print(f"base rate YES  : {base * 100:.1f}%")

    print("\n── Calibration: YES ask price vs realised YES frequency ──")
    print(f"{'ask bucket':>13} {'n':>6} {'mean ask':>9} {'realised':>9} "
          f"{'edge':>8} {'EV/share':>10} {'t':>7}")
    for r in calibration_table(quotes):
        print(f"{r['lo']:.2f}-{r['hi']:.2f}".rjust(13)
              + f" {r['n']:>6,} {r['mean_ask']:>9.3f} {r['freq']:>9.3f}"
              + f" {r['edge']:>+8.3f} {r['ev_net']:>+10.4f} {r['t']:>7.2f}")

    print("\n── Buying one side at the ask, held to resolution ──")
    print(f"{'side':>5} {'ask band':>13} {'n':>6} {'win%':>7} "
          f"{'ROI/trade':>10} {'t':>7}")
    for side in ("YES", "NO"):
        for lo, hi in BUCKETS:
            r = evaluate_side(quotes, side, lo, hi)
            if r is None:
                continue
            print(f"{side:>5} {f'{lo:.2f}-{hi:.2f}':>13} {r['n']:>6,} "
                  f"{r['winrate'] * 100:>6.1f}% {fmt_pct(r['roi']):>10} "
                  f"{r['t']:>7.2f}")


    oos_report(quotes)
    horizon_report(quotes)




# ────────────────────────────────────────────────────────────────────────────
#  Out-of-sample validation and horizon adjustment
#
#  Two disciplines this project paid to learn:
#
#  1. IN-SAMPLE RESULTS DECAY. The mid-band copy_whale edge measured +21.8%
#     in sample and +7.5% out of it. Any bucket found by scanning 21 of them
#     is a candidate, not a finding, until it survives a split it was not
#     chosen on.
#
#  2. ROI IS NOT A RETURN. Cross-venue arbitrage showed +3.16c net and died
#     at 9.9% annualised because capital sat locked until resolution. A trade
#     paying +2.8% over a 40-day hold is not comparable to one paying +2.8%
#     over two days.
# ────────────────────────────────────────────────────────────────────────────

def annualise(roi: float, hold_hours: float) -> float:
    """DELIBERATELY NOT COMPOUNDED. Returns simple turnover-scaled return.

    An earlier version compounded (1+roi)**(8760/hold) and printed
    +2,270,324% for a bucket whose t-statistic was 0.91. Compounding a mean
    that cannot be distinguished from zero manufactures a number with no
    meaning, and it assumes an unbroken supply of identical opportunities
    plus instant redeployment of the full stake. Simple scaling is the
    honest comparison against the 9.9% that ended the cross-venue study.
    """
    if hold_hours <= 0:
        return float("nan")
    return roi * (8760.0 / hold_hours)


def split_by_date(quotes: list[Quote]) -> tuple[list[Quote], list[Quote]]:
    """Chronological split on snapshot time. Train = older half."""
    ordered = sorted(quotes, key=lambda q: q.ts)
    cut = len(ordered) // 2
    return ordered[:cut], ordered[cut:]


def oos_report(quotes: list[Quote]) -> None:
    train, test = split_by_date(quotes)
    print(f"\n── Out-of-sample split (chronological) ──")
    print(f"train: {len(train):,} markets  |  test: {len(test):,} markets")
    print(f"{'side':>5} {'ask band':>13} "
          f"{'IS n':>6} {'IS ROI':>9} {'IS t':>6}   "
          f"{'OOS n':>6} {'OOS ROI':>9} {'OOS t':>6}")
    for side in ("YES", "NO"):
        for lo, hi in BUCKETS:
            a = evaluate_side(train, side, lo, hi)
            b = evaluate_side(test, side, lo, hi)
            if a is None or b is None:
                continue
            print(f"{side:>5} {f'{lo:.2f}-{hi:.2f}':>13} "
                  f"{a['n']:>6,} {fmt_pct(a['roi']):>9} {a['t']:>6.2f}   "
                  f"{b['n']:>6,} {fmt_pct(b['roi']):>9} {b['t']:>6.2f}")


def horizon_report(quotes: list[Quote]) -> None:
    """Per-trade ROI is meaningless without the hold it was earned over."""
    print(f"\n── Horizon-adjusted return, buying NO at the ask ──")
    print(f"{'ask band':>13} {'n':>6} {'ROI':>9} {'t':>6} "
          f"{'med hold':>10} {'annualised':>11}")
    for lo, hi in BUCKETS:
        r = evaluate_side(quotes, "NO", lo, hi)
        if r is None:
            continue
        sel = [q for q in quotes if lo <= q.ask_no < hi]
        med = statistics.median(q.horizon_h for q in sel)
        ann = annualise(r["roi"], med)
        ann_s = "n/a" if ann != ann else fmt_pct(ann)
        print(f"{f'{lo:.2f}-{hi:.2f}':>13} {r['n']:>6,} "
              f"{fmt_pct(r['roi']):>9} {r['t']:>6.2f} "
              f"{med:>9.1f}h {ann_s:>11}")


if __name__ == "__main__":
    main()
