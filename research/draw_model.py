"""Can a Poisson goal model beat Polymarket's price on football draws?

RESEARCH ONLY. Offline against resolved markets. No orders, no network at
scoring time.

────────────────────────────────────────────────────────────────────────────
Why draws, and why this is not the test that already failed
────────────────────────────────────────────────────────────────────────────
The cheap version was tested first and REJECTED: draws are not systematically
underpriced. Across 298 resolved draw markets the mean ask was 0.291 against a
realised 0.285 — an edge of -0.006, the same half-spread that shows up in every
other market type. The famous "draw bias" is not present at executable prices.

This asks the harder question the calibration test cannot answer: even if the
price is right ON AVERAGE, can a model identify WHICH matches are mispriced?
Those are different claims. A market can be perfectly calibrated in aggregate
and still be beatable match by match.

The prior is poor and worth stating up front. Polymarket's football prices
track sportsbook odds, and sportsbook football is among the most efficient
betting markets in existence — a published 1997-vintage model is being run
against people who have refined exactly this for twenty-five years. If this
fails it fails on COMPETITION, not on structure like the eight before it.

────────────────────────────────────────────────────────────────────────────
Model
────────────────────────────────────────────────────────────────────────────
Standard Poisson ratings (the Dixon-Coles backbone without the low-score
correction, which needs an optimiser we have no numpy for):

    lambda_home = avg_home_goals x attack[home] x defence[away]
    lambda_away = avg_away_goals x attack[away] x defence[home]
    P(draw)     = sum_k Poisson(k; lambda_home) x Poisson(k; lambda_away)

Ratings are exponentially time-weighted: a match `d` days old carries weight
0.5 ** (d / half_life). International form from 2008 should not count equally
with last year's.

NO LOOK-AHEAD. For a fixture on date D the ratings are fit strictly on matches
played BEFORE D. This is the discipline that matters most here: including the
match being predicted, or any match after it, would manufacture an edge the
same way training contamination would have manufactured one for the LLM.
"""

from __future__ import annotations

import csv
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, "research")
from longshot_study import load_quotes, one_per_market, taker_fee_per_share, welch_t  # noqa: E402

HALF_LIFE_DAYS = 730.0      # two years
MIN_MATCHES = 12            # per team, before we will rate it
MAX_GOALS = 10
FIXTURE = re.compile(r"Will (.+?) vs\.? (.+?) end in a draw", re.I)


def load_intl(path: str) -> list[dict]:
    out = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            try:
                y, m, d = (int(x) for x in r["date"].split("-"))
                out.append({"date": date(y, m, d), "h": r["home_team"],
                            "a": r["away_team"], "hs": int(r["home_score"]),
                            "as": int(r["away_score"]),
                            "neutral": r.get("neutral", "").upper() == "TRUE"})
            except (ValueError, KeyError):
                continue
    out.sort(key=lambda r: r["date"])
    return out


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


class Ratings:
    """Time-weighted attack/defence built ONLY from matches before `asof`."""

    def __init__(self, history: list[dict], asof: date) -> None:
        gf = defaultdict(float); ga = defaultdict(float); w = defaultdict(float)
        tot_h = tot_a = tot_w = 0.0
        for m in history:
            if m["date"] >= asof:
                break                       # strict: no look-ahead
            age = (asof - m["date"]).days
            wt = 0.5 ** (age / HALF_LIFE_DAYS)
            if wt < 1e-4:
                continue
            gf[m["h"]] += wt * m["hs"]; ga[m["h"]] += wt * m["as"]; w[m["h"]] += wt
            gf[m["a"]] += wt * m["as"]; ga[m["a"]] += wt * m["hs"]; w[m["a"]] += wt
            tot_h += wt * m["hs"]; tot_a += wt * m["as"]; tot_w += wt
        self.avg_home = tot_h / tot_w if tot_w else 1.4
        self.avg_away = tot_a / tot_w if tot_w else 1.1
        avg_all = (self.avg_home + self.avg_away) / 2 or 1.0
        self.attack, self.defence, self.n = {}, {}, {}
        for t, ww in w.items():
            if ww <= 0:
                continue
            self.attack[t] = (gf[t] / ww) / avg_all
            self.defence[t] = (ga[t] / ww) / avg_all
            self.n[t] = ww

    def p_draw(self, home: str, away: str, neutral: bool) -> float | None:
        if (self.n.get(home, 0) < MIN_MATCHES
                or self.n.get(away, 0) < MIN_MATCHES):
            return None
        # On neutral ground (World Cup) the home advantage does not apply.
        base_h = self.avg_home if not neutral else (self.avg_home + self.avg_away) / 2
        base_a = self.avg_away if not neutral else (self.avg_home + self.avg_away) / 2
        lh = max(base_h * self.attack[home] * self.defence[away], 1e-6)
        la = max(base_a * self.attack[away] * self.defence[home], 1e-6)
        return sum(poisson_pmf(k, lh) * poisson_pmf(k, la)
                   for k in range(MAX_GOALS + 1))


def main() -> None:
    hist = load_intl("/tmp/intl.csv")
    countries = {m["h"] for m in hist} | {m["a"] for m in hist}
    print(f"history: {len(hist):,} international matches, {len(countries)} teams")

    quotes = {q.market_id: q for q in
              one_per_market(load_quotes("data/trades.db", 6.0, 0.0, 1e9, 0.05))}
    conn = sqlite3.connect("data/trades.db")
    rows = conn.execute("""
        SELECT m.market_id, m.question, m.end_date, r.resolved_outcome
        FROM market_metadata m JOIN resolved_markets r ON r.market_id = m.market_id
        WHERE r.resolved_outcome IN ('YES','NO') AND lower(m.question) LIKE '%draw%'
    """).fetchall()
    conn.close()

    cases = []
    for mid, q, end, outcome in rows:
        fx = FIXTURE.search(q or "")
        quote = quotes.get(mid)
        if not fx or quote is None or not end:
            continue
        h, a = fx.group(1).strip(), fx.group(2).strip()
        if h not in countries or a not in countries:
            continue
        try:
            y, mo, d = (int(x) for x in end[:10].split("-"))
            asof = date(y, mo, d)
        except ValueError:
            continue
        cases.append((mid, h, a, asof, quote, outcome == "YES"))
    print(f"international draw markets with executable books: {len(cases)}")
    if len(cases) < 30:
        print("sample too small to conclude"); return

    preds = []
    for mid, h, a, asof, quote, won in cases:
        r = Ratings(hist, asof)
        p = r.p_draw(h, a, neutral=True)   # these are tournament fixtures
        if p is None:
            continue
        preds.append((p, quote, won, h, a))
    print(f"rated (both teams have >= {MIN_MATCHES} weighted matches): {len(preds)}\n")
    if len(preds) < 30:
        print("too few rated"); return

    ys = [1.0 if w else 0.0 for _, _, w, _, _ in preds]
    mkt = [(q.ask_yes + q.bid_yes) / 2 for _, q, _, _, _ in preds]
    mdl = [p for p, _, _, _, _ in preds]
    br = lambda ps: statistics.fmean((p - y) ** 2 for p, y in zip(ps, ys))
    base = statistics.fmean(ys)

    print(f"realised draw rate: {base:.3f}   n={len(preds)}")
    print(f"{'forecaster':>22} {'Brier':>8}")
    print(f"{'always base rate':>22} {br([base]*len(ys)):>8.4f}")
    print(f"{'MARKET MID':>22} {br(mkt):>8.4f}")
    print(f"{'Poisson model':>22} {br(mdl):>8.4f}")
    better = br(mdl) < br(mkt)
    print(f"  -> model {'BEATS' if better else 'loses to'} market "
          f"({(1 - br(mdl)/br(mkt))*100:+.1f}% skill)")

    print("\nBetting only where the model disagrees with the executable ask:")
    print(f"{'threshold':>10} {'bets':>6} {'win%':>7} {'ROI':>9} {'t':>7}")
    for thr in (0.03, 0.05, 0.08, 0.12):
        r = []
        for p, q, won, _, _ in preds:
            if p > q.ask_yes + thr:                 # model says draw is cheap
                r.append(((1.0 if won else 0.0) - q.ask_yes
                          - taker_fee_per_share(q.ask_yes)) / q.ask_yes)
            elif p < q.ask_no + thr - 1.0 + 1.0 and p + thr < (1.0 - q.ask_no):
                pass                                 # no-draw side priced worse
        if len(r) < 10:
            print(f"{thr:>10.2f} {len(r):>6} {'-':>7} {'too few':>9} {'-':>7}")
            continue
        m, se, t = welch_t(r)
        wins = sum(1 for x in r if x > 0)
        print(f"{thr:>10.2f} {len(r):>6} {wins/len(r)*100:>6.1f}% "
              f"{m*100:>+8.2f}% {t:>7.2f}")


if __name__ == "__main__":
    main()
