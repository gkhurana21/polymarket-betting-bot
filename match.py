"""Match markets across venues, then test for a genuine cross-venue spread.

MATCHING IS THE HARD PART, NOT PRICING.

Cross-venue prediction arbitrage usually dies here rather than on price. Two
markets can carry near-identical titles and resolve on different criteria — a
different source, a different cutoff date, a different tie rule. If they do,
what looked like a locked pair is an outright directional bet on a technicality,
and you discover it at settlement.

So this matcher is deliberately strict and biased toward rejecting. Anything it
does surface is a CANDIDATE for human review, never an executable pair:

  * numbers and years must match exactly — "before 2027" != "before 2028",
    "$100k" != "$150k". These are the most common silent mismatch.
  * strong token overlap on content words after stopword removal.
  * the arb test itself is direction-aware.

THE ARB CONDITION

To hedge across venues you buy YES on one and NO on the other. Buying NO at
price q is buying at (1 - bid_YES) on that venue, so:

    cost = ask_YES(A) + (1 - bid_YES(B))
    profit  <=>  cost < 1  <=>  ask_YES(A) < bid_YES(B)

i.e. one venue's best ask is strictly below the other's best bid. No mint
mechanism links the two books, so nothing structurally forbids it.
"""
from __future__ import annotations
import json, re, sys
from dataclasses import dataclass

STOP = set("""a an the is are was were be been being will would can could may
might do does did to of in on at by for with from as that this these those it
its and or not no if then than there here who whom which what when where how
before after during until between above below over under more most less least
any all both each few other some such only own same so too very just now
about into through above out up down off again further once he she they them
his her their you your we our us i me my""".split())

NUM = re.compile(r"\d+(?:[.,]\d+)?")

# Words that CARRY THE CLAIM rather than name the subject. If one side has one
# of these and the other does not, the two questions are asking different
# things however much their nouns overlap.
#
# This exists because the first run matched "Will the Democrats WIN the
# Pennsylvania governor race in 2026?" (0.95) against "CLOSEST Governor race in
# 2026? Pennsylvania" (0.001) and reported a +0.937 "arbitrage". Same year,
# same state, same nouns, completely different question. Token overlap alone
# cannot see the difference; the predicate has to be compared explicitly.
PREDICATE = {
    "win", "wins", "won", "lose", "loses", "closest", "narrowest", "widest",
    "highest", "lowest", "most", "least", "first", "last", "above", "below",
    "over", "under", "before", "after", "reach", "reaches", "exceed",
    "exceeds", "hit", "hits", "drop", "drops", "rise", "rises", "fall",
    "falls", "resign", "resigns", "out", "leave", "leaves", "nominee",
    "nomination", "elected", "control", "controls", "majority", "flip",
    "flips", "yes", "no",
}
WORD = re.compile(r"[a-z0-9$%.]+")


def norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[^a-z0-9$%.\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> set[str]:
    return {w for w in WORD.findall(norm(s)) if w not in STOP and len(w) > 1}


def numbers(s: str) -> set[str]:
    """Numeric fingerprint. Commas stripped so 100,000 == 100000."""
    return {n.replace(",", "") for n in NUM.findall(norm(s))}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Pair:
    pm_q: str
    kl_q: str
    pm_bid: float
    pm_ask: float
    kl_bid: float
    kl_ask: float
    sim: float
    pm_id: str
    kl_ticker: str

    @property
    def spread_pm_to_kl(self) -> float:
        """Buy YES on Polymarket, NO on Kalshi. Positive = locked profit."""
        return self.kl_bid - self.pm_ask

    @property
    def spread_kl_to_pm(self) -> float:
        """Buy YES on Kalshi, NO on Polymarket."""
        return self.pm_bid - self.kl_ask

    @property
    def best_spread(self) -> float:
        return max(self.spread_pm_to_kl, self.spread_kl_to_pm)


def _f(v):
    try:
        f = float(v)
        return f if 0.0 < f < 1.0 else None
    except (TypeError, ValueError):
        return None


def load():
    kl = json.load(open("kalshi.json"))
    pm = json.load(open("polymarket.json"))
    k_rows = []
    for e in kl:
        for m in e.get("markets") or []:
            title = f"{e.get('title','')} {m.get('subtitle') or m.get('yes_sub_title') or ''}".strip()
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if bid is None or ask is None:
                continue
            k_rows.append({"q": title, "bid": bid, "ask": ask,
                           "ticker": m.get("ticker", ""),
                           "tok": tokens(title), "num": numbers(title)})
    p_rows = []
    for m in pm:
        q = m.get("question") or ""
        bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
        if bid is None or ask is None:
            continue
        p_rows.append({"q": q, "bid": bid, "ask": ask,
                       "id": m.get("conditionId", ""),
                       "tok": tokens(q), "num": numbers(q)})
    return p_rows, k_rows


def match(p_rows, k_rows, min_sim=0.70) -> list[Pair]:
    out = []
    for p in p_rows:
        if len(p["tok"]) < 3:
            continue
        best = None
        for k in k_rows:
            # Numeric fingerprints must agree exactly. "before 2027" vs
            # "before 2028" is the single most common silent mismatch, and no
            # amount of word overlap should be allowed to paper over it.
            if p["num"] != k["num"]:
                continue
            # The claim words must agree. Subject overlap is not enough.
            if (p["tok"] & PREDICATE) != (k["tok"] & PREDICATE):
                continue
            s = jaccard(p["tok"], k["tok"])
            if s >= min_sim and (best is None or s > best[0]):
                best = (s, k)
        if best:
            s, k = best
            out.append(Pair(p["q"], k["q"], p["bid"], p["ask"],
                            k["bid"], k["ask"], s, p["id"], k["ticker"]))
    return out


if __name__ == "__main__":
    p_rows, k_rows = load()
    print(f"priced markets — polymarket {len(p_rows)}, kalshi {len(k_rows)}")
    loose = match(p_rows, k_rows, min_sim=0.55)
    pairs = match(p_rows, k_rows, min_sim=0.70)
    print(f"candidates at sim>=0.55 with predicate agreement: {len(loose)}")
    print(f"candidates at sim>=0.70 with predicate agreement: {len(pairs)}\n")
    if not pairs:
        sys.exit(0)
    pairs.sort(key=lambda x: -x.best_spread)
    print(f"{'spread':>8} {'sim':>5}  {'PM bid/ask':>13} {'KL bid/ask':>13}  market")
    for pr in pairs[:20]:
        print(f"{pr.best_spread:>+8.3f} {pr.sim:>5.2f}  "
              f"{pr.pm_bid:>6.3f}/{pr.pm_ask:<6.3f} {pr.kl_bid:>6.3f}/{pr.kl_ask:<6.3f}")
        print(f"{'':>8}  PM: {pr.pm_q[:74]}")
        print(f"{'':>8}  KL: {pr.kl_q[:74]}")
    pos = [x for x in pairs if x.best_spread > 0]
    print(f"\npairs with a positive cross-venue spread: {len(pos)} / {len(pairs)}")
