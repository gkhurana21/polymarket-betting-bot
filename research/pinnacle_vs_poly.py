"""Does Polymarket's draw price agree with Pinnacle's closing line?

RESEARCH ONLY. Offline, no orders.

This is a stronger test than the homemade Poisson model that preceded it.
Rather than asking "can my 1997-vintage model beat the market" -- a question
whose answer was always likely to be no -- it asks whether Polymarket agrees
with the sharpest publicly available probability estimate in football.

Pinnacle's closing line is the standard efficiency benchmark in sports
betting: low margin, high limits, and it welcomes winning players, so its
close aggregates informed money better than any model we could fit.

  * If the two agree, Polymarket is efficient on football and the question is
    closed -- no model we build will beat a price that already equals the
    sharpest book.
  * If they systematically differ, the gap is the signal, and it is worth far
    more than anything a Poisson fit would have produced.

────────────────────────────────────────────────────────────────────────────
Honest limits, stated before the numbers
────────────────────────────────────────────────────────────────────────────
1. TIMING. PSCD is the CLOSING draw price, set at kickoff. Our Polymarket
   snapshot is earlier. So a disagreement is NOT directly tradeable -- acting
   on it live would need Pinnacle's price at OUR snapshot time, which this
   dataset does not contain. Treat any gap as a diagnostic of efficiency, not
   as a realised edge.

2. MARGIN. Bookmaker odds contain overround: 1/PSCH + 1/PSCD + 1/PSCA > 1.
   Comparing a raw 1/PSCD against a Polymarket probability would bias the
   comparison by several points, so the implied probabilities are normalised
   to sum to 1 before anything is compared.

3. SAMPLE. Free data covers top divisions only. Most of our draw markets are
   Brazilian Serie B, so ~54 club fixtures match, not the ~400 hoped for.
"""

from __future__ import annotations

import csv
import glob
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, "research")
from longshot_study import load_quotes, one_per_market, taker_fee_per_share, welch_t  # noqa: E402

FIXTURE = re.compile(r"Will (.+?) vs\.? (.+?) end in a draw", re.I)
DATE_TOLERANCE_DAYS = 3


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|ec|sc|ac|cf|ca|cr|aa|afc|cd|club|de|do|da|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def parse_date(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def load_fixtures() -> dict[tuple[str, str], list[dict]]:
    """Index football-data rows by (home, away) normalised names."""
    idx: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in glob.glob("/tmp/fd_*.csv"):
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            h, a = r.get("Home"), r.get("Away")
            d = parse_date(r.get("Date", ""))
            if not h or not a or d is None:
                continue
            try:
                hg, ag = int(r["HG"]), int(r["AG"])
            except (KeyError, TypeError, ValueError):
                continue
            def f(k):
                try:
                    v = float(r.get(k) or 0)
                    return v if v > 1.0 else None
                except ValueError:
                    return None
            # Pinnacle (PSC*) is absent from recent seasons; the closing
            # market AVERAGE (AvgC*) is present throughout and is an equally
            # valid sharp benchmark -- arguably better, being a consensus
            # rather than one book. Prefer Pinnacle, fall back to the average.
            ph = f("PSCH") or f("AvgCH")
            pd_ = f("PSCD") or f("AvgCD")
            pa = f("PSCA") or f("AvgCA")
            src = "pinnacle" if f("PSCD") else ("avg" if f("AvgCD") else None)
            idx[(norm(h), norm(a))].append(
                {"date": d, "hg": hg, "ag": ag,
                 "ph": ph, "pd": pd_, "pa": pa, "src": src})
    return idx


def main() -> None:
    idx = load_fixtures()
    print(f"football-data fixtures indexed: {sum(len(v) for v in idx.values()):,}")

    quotes = {q.market_id: q for q in
              one_per_market(load_quotes("data/trades.db", 6.0, 0.0, 1e9, 0.05))}
    conn = sqlite3.connect("data/trades.db")
    rows = conn.execute("""
        SELECT m.market_id, m.question, m.end_date, r.resolved_outcome
        FROM market_metadata m JOIN resolved_markets r ON r.market_id = m.market_id
        WHERE r.resolved_outcome IN ('YES','NO') AND lower(m.question) LIKE '%draw%'
    """).fetchall()
    conn.close()

    paired, no_odds, outcome_mismatch = [], 0, 0
    for mid, q, end, outcome in rows:
        fx = FIXTURE.search(q or "")
        quote = quotes.get(mid)
        d = parse_date((end or "")[:10])
        if not fx or quote is None or d is None:
            continue
        key = (norm(fx.group(1)), norm(fx.group(2)))
        best = None
        for cand in idx.get(key, []):
            if abs((cand["date"] - d).days) <= DATE_TOLERANCE_DAYS:
                best = cand
                break
        if best is None:
            continue
        # Verify the joined row agrees with how the market actually resolved.
        # A name+date match that disagrees on the outcome is the wrong fixture,
        # and silently accepting it would attach another match's odds to this
        # market -- the same undetectable poisoning the Gamma condition_id bug
        # would have caused.
        actually_drew = (best["hg"] == best["ag"])
        if actually_drew != (outcome == "YES"):
            outcome_mismatch += 1
            continue
        if not (best["ph"] and best["pd"] and best["pa"]):
            no_odds += 1
            continue
        # de-vig: normalise the three implied probabilities to sum to 1
        raw = [1/best["ph"], 1/best["pd"], 1/best["pa"]]
        pin_draw = raw[1] / sum(raw)
        paired.append((pin_draw, quote, outcome == "YES", best.get("src")))

    print(f"markets joined to a fixture with odds : {len(paired)}")
    print(f"  rejected, outcome disagreed         : {outcome_mismatch}")
    print(f"  rejected, no Pinnacle odds          : {no_odds}")
    if len(paired) < 25:
        print("\nsample too small to conclude"); return

    from collections import Counter
    print("  odds source:", dict(Counter(s for *_, s in paired)))
    ys = [1.0 if w else 0.0 for _, _, w, _ in paired]
    mid = [(q.ask_yes + q.bid_yes) / 2 for _, q, _, _ in paired]
    pin = [p for p, _, _, _ in paired]
    br = lambda ps: statistics.fmean((p - y) ** 2 for p, y in zip(ps, ys))
    base = statistics.fmean(ys)
    print(f"\nrealised draw rate {base:.3f}   n={len(paired)}")
    print(f"{'forecaster':>22} {'Brier':>8}")
    print(f"{'always base rate':>22} {br([base]*len(ys)):>8.4f}")
    print(f"{'Polymarket mid':>22} {br(mid):>8.4f}")
    print(f"{'bookmaker close':>22} {br(pin):>8.4f}")

    diffs = [p - m for p, m in zip(pin, mid)]
    print(f"\nBookmaker minus Polymarket: mean {statistics.fmean(diffs)*100:+.2f}c  "
          f"median {statistics.median(diffs)*100:+.2f}c  "
          f"stdev {statistics.stdev(diffs)*100:.2f}c")

    print("\nBuy the draw on Polymarket when the book says it is cheap:")
    print(f"{'edge >':>8} {'bets':>6} {'win%':>7} {'ROI':>9} {'t':>7}")
    for thr in (0.02, 0.04, 0.06):
        r = [((1.0 if w else 0.0) - q.ask_yes - taker_fee_per_share(q.ask_yes)) / q.ask_yes
             for p, q, w, _ in paired if p - q.ask_yes > thr]
        if len(r) < 10:
            print(f"{thr:>8.2f} {len(r):>6} {'-':>7} {'too few':>9} {'-':>7}")
            continue
        m, se, t = welch_t(r)
        print(f"{thr:>8.2f} {len(r):>6} {sum(1 for x in r if x>0)/len(r)*100:>6.1f}% "
              f"{m*100:>+8.2f}% {t:>7.2f}")


if __name__ == "__main__":
    main()
