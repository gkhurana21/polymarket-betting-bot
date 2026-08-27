"""Aggregate the captured order-book corpus for the research report.

Reads book_snapshots (real Polymarket books, YES token side) and produces
small summary structures suitable for embedding in a self-contained page.
Aggregates only — the raw corpus is hundreds of thousands of rows and has no
business being shipped inside an HTML file.
"""

from __future__ import annotations

import collections
import datetime
import json
from typing import Any

from bot.tracker import Tracker
from utils.logger import get_logger

_log = get_logger(__name__)


def _touch(book_json: str) -> tuple[float, float, float, float] | None:
    """(best_bid, best_ask, bid_size_at_touch, ask_size_at_touch) or None."""
    try:
        b = json.loads(book_json)
        bids, asks = b.get("bids") or [], b.get("asks") or []
        if not bids or not asks:
            return None
        bb = max((float(x[0]) for x in bids), default=0.0)
        ba = min((float(x[0]) for x in asks), default=0.0)
        bsz = sum(float(x[1]) for x in bids if abs(float(x[0]) - bb) < 1e-9)
        asz = sum(float(x[1]) for x in asks if abs(float(x[0]) - ba) < 1e-9)
    except Exception:
        return None
    if not (0 < bb < ba < 1):
        return None
    return bb, ba, bsz, asz


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def build_corpus_summary(tracker: Tracker, limit: int = 250_000,
                         max_markets: int = 250) -> dict[str, Any]:
    """Summarise the book corpus: spread shape, price structure, time trend."""
    spreads: list[float] = []
    by_price: dict[int, list[float]] = collections.defaultdict(list)
    by_day: dict[str, list[float]] = collections.defaultdict(list)
    per_market: dict[str, dict[str, Any]] = {}

    cur = tracker._conn.execute(
        "SELECT market_id, ts, book_json FROM book_snapshots "
        "ORDER BY ts DESC LIMIT ?", (limit,))
    for r in cur:
        t = _touch(r["book_json"])
        if t is None:
            continue
        bb, ba, bsz, asz = t
        sp = ba - bb
        mid = (bb + ba) / 2
        spreads.append(sp)
        by_price[min(9, int(mid * 10))].append(sp)
        day = datetime.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        by_day[day].append(sp)

        m = per_market.setdefault(r["market_id"], {
            "market_id": r["market_id"], "n": 0, "spreads": [],
            "mids": [], "depth": [], "last_ts": 0})
        m["n"] += 1
        if len(m["spreads"]) < 400:
            m["spreads"].append(sp)
            m["mids"].append(mid)
            m["depth"].append(bsz + asz)
        m["last_ts"] = max(m["last_ts"], r["ts"])

    spreads.sort()

    # Attach questions where we have them.
    questions: dict[str, str] = {}
    for row in tracker._conn.execute(
            "SELECT market_id, question FROM market_snapshots "
            "WHERE question IS NOT NULL GROUP BY market_id"):
        questions[row["market_id"]] = row["question"]

    markets = []
    for m in sorted(per_market.values(), key=lambda x: -x["n"])[:max_markets]:
        sp = sorted(m["spreads"])
        md = sorted(m["mids"])
        dp = sorted(m["depth"])
        markets.append({
            "id": m["market_id"],
            "q": (questions.get(m["market_id"]) or m["market_id"])[:70],
            "n": m["n"],
            "spread": round(_pct(sp, 0.5), 5),
            "spread_p90": round(_pct(sp, 0.9), 5),
            "mid": round(_pct(md, 0.5), 4),
            "depth": round(_pct(dp, 0.5), 1),
            "last": m["last_ts"],
        })

    # Histogram of spreads, 0–5 cents.
    bins, lo, hi = 25, 0.0, 0.05
    width = (hi - lo) / bins
    counts = [0] * bins
    for s in spreads:
        if lo <= s <= hi:
            counts[min(bins - 1, int((s - lo) / width))] += 1

    price_levels = []
    for k in range(10):
        v = sorted(by_price.get(k, []))
        if not v:
            continue
        price_levels.append({
            "lo": round(k / 10, 1), "hi": round((k + 1) / 10, 1),
            "n": len(v), "median": round(_pct(v, 0.5), 5),
            "p90": round(_pct(v, 0.9), 5),
        })

    timeline = []
    for day in sorted(by_day):
        v = sorted(by_day[day])
        if len(v) < 20:
            continue
        timeline.append({"day": day, "n": len(v),
                         "median": round(_pct(v, 0.5), 5),
                         "p90": round(_pct(v, 0.9), 5)})

    totals = tracker._conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT market_id) m FROM book_snapshots"
    ).fetchone()

    return {
        "n_books_total": totals["n"],
        "n_markets_total": totals["m"],
        "n_usable": len(spreads),
        "hist": {"lo": lo, "hi": hi, "width": width, "counts": counts},
        "pct": {q: round(_pct(spreads, q), 5)
                for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
        "tight_2tick": sum(1 for s in spreads if s <= 0.002),
        "price_levels": price_levels,
        "timeline": timeline,
        "markets": markets,
    }
