"""Is providing liquidity in THIN Polymarket markets profitable?

RESEARCH ONLY. Public endpoints, no orders, no keys.

────────────────────────────────────────────────────────────────────────────
Why this is not a rerun of Friday
────────────────────────────────────────────────────────────────────────────
Friday tested market making on LIQUID markets, where the spread is ~0.1c so
there is nothing to capture and the only income is liquidity rewards. Those
rewards covered 17% of measured adverse selection, and it was rejected.

Thin markets are a different object: median spread 1.6c, p95 15c -- 16x to
150x wider. The income is the spread itself, not rewards.

The default explanation for a wide spread is that it PRICES adverse-selection
risk: nobody quotes tight on "Will Treg Taylor advance from the 2026 Alaska
primary" because the person lifting your offer may know something. Wide spread
and toxic flow are usually the same fact. This measures whether that is true
here rather than assuming it either way.

────────────────────────────────────────────────────────────────────────────
Method: markout on real fills
────────────────────────────────────────────────────────────────────────────
For every public trade, the taker's side tells us what the MAKER did:

    taker BUY  at P  ->  maker SOLD   at P  ->  maker gains if price FALLS
    taker SELL at P  ->  maker BOUGHT at P  ->  maker gains if price RISES

So maker markout per share at horizon h:

    taker BUY :  P - price(t+h)
    taker SELL:  price(t+h) - P

Negative mean markout = the flow is toxic; the maker is picked off. The
strategy is viable only if captured half-spread EXCEEDS the adverse markout.
Reporting markout alone would be the same error as reporting a 59% win rate
without the P&L.

No fills are simulated and no fill rate is assumed -- these are trades that
actually happened, against a maker who actually existed.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request

DATA_API = "https://data-api.polymarket.com/trades?market={cid}&limit=500"
HIST = ("https://clob.polymarket.com/prices-history"
        "?market={tok}&interval=1m&fidelity=60")
HORIZONS_H = (1, 6, 24)


def get(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def price_at(hist: list[dict], ts: int) -> float | None:
    """Nearest recorded price at or after ts, within 3 hours.

    Refusing to extrapolate matters: an interpolated price beyond the series
    would silently invent the very number the study is measuring.
    """
    best = None
    for pt in hist:
        t = pt.get("t")
        if t is None or t < ts:
            continue
        if t - ts > 3 * 3600:
            break
        best = pt.get("p")
        break
    return float(best) if best is not None else None


def analyse(markets: list[dict], max_markets: int, delay: float) -> None:
    rows = []
    done = 0
    for m in markets[:max_markets]:
        cid = m.get("conditionId")
        try:
            tok = json.loads(m["clobTokenIds"])[0]
        except Exception:
            continue
        trades = get(DATA_API.format(cid=cid)); time.sleep(delay)
        hist = get(HIST.format(tok=tok)); time.sleep(delay)
        if not isinstance(trades, list) or not trades:
            continue
        series = (hist or {}).get("history") or []
        if len(series) < 24:
            continue
        series.sort(key=lambda p: p.get("t", 0))
        done += 1
        for tr in trades:
            try:
                p = float(tr["price"]); ts = int(tr["timestamp"])
                side = str(tr["side"]).upper()
                size = float(tr.get("size") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            # data-api returns trades for BOTH outcome tokens of the market,
            # tagged by `asset`. The price history is for ONE token. Scoring a
            # NO-token trade at 0.999 against the YES token's 0.001 history
            # manufactured a +-100c markout on 76% of fills and produced an
            # entirely fictitious result. Keep only trades on OUR token.
            if str(tr.get("asset") or "") != str(tok):
                continue
            if not (0.0 < p < 1.0) or size <= 0:
                continue
            for h in HORIZONS_H:
                fut = price_at(series, ts + h * 3600)
                if fut is None:
                    continue
                # maker took the OPPOSITE side of the taker
                mk = (p - fut) if side == "BUY" else (fut - p)
                rows.append((h, mk, size, m.get("question", "")[:40]))
        if done % 10 == 0:
            print(f"  {done} markets, {len(rows):,} markout points", flush=True)

    print(f"\nmarkets with usable trades+history: {done}")
    if not rows:
        print("no markout points"); return

    print(f"\n{'horizon':>8} {'fills':>8} {'mean markout':>14} "
          f"{'median':>10} {'share-wtd':>11}")
    for h in HORIZONS_H:
        sel = [(mk, sz) for hh, mk, sz, _ in rows if hh == h]
        if len(sel) < 30:
            continue
        mks = [m for m, _ in sel]
        wt = sum(m * s for m, s in sel) / max(sum(s for _, s in sel), 1e-9)
        print(f"{h:>7}h {len(sel):>8,} {statistics.fmean(mks) * 100:>13.3f}c "
              f"{statistics.median(mks) * 100:>9.3f}c {wt * 100:>10.3f}c")

    print("\nA maker earns roughly HALF the quoted spread per fill.")
    print("Thin-market median spread is 1.60c -> ~0.80c earned per fill.")
    for h in HORIZONS_H:
        sel = [mk for hh, mk, _, _ in rows if hh == h]
        if len(sel) < 30:
            continue
        adverse = statistics.fmean(sel)
        print(f"  at {h:>2}h: {0.80 + adverse * 100:+.3f}c net per fill "
              f"(0.80c spread {adverse * 100:+.3f}c markout)")


if __name__ == "__main__":
    universe = json.load(open("/tmp/universe.json"))

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    thin = [r for r in universe if r.get("clobTokenIds")
            and 200 <= (f(r.get("volume24hr")) or 0) < 10000]
    thin.sort(key=lambda r: -(f(r.get("volume24hr")) or 0))
    print(f"thin markets available: {len(thin)}")
    analyse(thin, max_markets=int(sys.argv[1]) if len(sys.argv) > 1 else 80,
            delay=0.12)
