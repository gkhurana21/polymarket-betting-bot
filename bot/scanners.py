"""Scanner adapters: every detector emits the same Opportunity shape.

Four strategies, four payout structures, one type. The payout bound is the
part that differs and the part that must be right -- get it wrong and the
funnel is filtering fiction:

  complement    buy YES + NO of ONE market      -> pays exactly $1
  monotonicity  A implies B: buy YES(B), NO(A)  -> pays AT LEAST $1
  exclusive     buy NO on n exclusive outcomes  -> pays AT LEAST n-1
  negrisk       buy YES on a COMPLETE exclusive set -> pays exactly $1

The exclusive bound holds for any SUBSET (at most one of n legs resolves YES,
so at least n-1 pay). The negrisk bound does NOT -- buying part of an
exhaustive set guarantees nothing, because the winner may be a leg you
skipped. That asymmetry produced three separate false positives here,
including a retracted +62%, so completeness is verified for negrisk and not
assumed.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

from bot.funnel import Leg, Opportunity

GAMMA = "https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset={off}"
EVENT = "https://gamma-api.polymarket.com/events?slug={slug}"
BOOK = "https://clob.polymarket.com/book?token_id={tok}"

UP = re.compile(r"\b(reach|reaches|above|exceed|exceeds|greater|over|hit|hits|"
                r"or more|at least)\b", re.I)
DOWN = re.compile(r"\b(dip|dips|below|under|less than|fall|falls|drop|drops|"
                  r"or fewer|or lower|or below|or less|at most)\b", re.I)
NUM = re.compile(r"\$?(\d[\d,]*\.?\d*)\s*(k|K|m|M|B|T)?")


def _get(url: str, timeout: float = 20.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "funnel/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _scale(v: str, suf: str | None) -> float:
    return float(v.replace(",", "")) * {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6,
                                        "B": 1e9, "T": 1e12}.get(suf or "", 1.0)


def fetch_universe(max_markets: int = 8000) -> list[dict]:
    """Open markets from Gamma.

    NOT CLOB sampling-markets: that returns only reward-sampled (i.e. liquid)
    markets, which silently excludes most of the venue.
    """
    rows, off = [], 0
    while off < max_markets:
        p = _get(GAMMA.format(off=off))
        if not p:
            break
        rows.extend(p)
        if len(p) < 100:
            break
        off += 100
        time.sleep(0.1)
    return rows


class BookCache:
    """One fetch per token per pass, with pacing. 118 rate-limit failures
    were once caused by firing requests flat out."""

    def __init__(self, delay: float = 0.09, max_spread: float = 0.15) -> None:
        self._c: dict[str, tuple | None] = {}
        self._delay = delay
        self._max_spread = max_spread
        self.fetches = 0

    def touch(self, tok: str):
        """(bid, bid_size, ask, ask_size) or None if it is not a price."""
        if tok in self._c:
            return self._c[tok]
        b = _get(BOOK.format(tok=tok))
        self.fetches += 1
        time.sleep(self._delay)
        out = None
        if b:
            bids, asks = b.get("bids") or [], b.get("asks") or []
            if bids and asks:
                try:
                    bb, bsz = max((float(x["price"]), float(x["size"])) for x in bids)
                    ba, asz = min((float(x["price"]), float(x["size"])) for x in asks)
                    if 0.0 < bb < ba < 1.0 and (ba - bb) <= self._max_spread:
                        out = (bb, bsz, ba, asz)
                except (KeyError, TypeError, ValueError):
                    out = None
        self._c[tok] = out
        return out


def _tokens(m: dict) -> tuple[str | None, str | None]:
    """(yes, no). outcomes[0] == 'Yes' verified across 400/400 markets."""
    try:
        t = json.loads(m["clobTokenIds"])
        return (str(t[0]), str(t[1])) if len(t) == 2 else (None, None)
    except Exception:
        return (None, None)


def _days(m: dict) -> float:
    try:
        d = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
        return max((d - datetime.now(timezone.utc)).days, 1.0)
    except Exception:
        return 120.0


# ── complement: YES + NO of one market, pays exactly $1 ────────────────────

def scan_complement(rows, cache) -> list[tuple[Opportunity, float]]:
    out = []
    for m in rows:
        yes, no = _tokens(m)
        if not yes or not no:
            continue
        a, b = cache.touch(yes), cache.touch(no)
        if not a or not b:
            continue
        q = (m.get("question") or "")[:80]
        legs = [Leg(yes, q, "BUY_YES", a[2], a[3], a[0], a[2]),
                Leg(no, q, "BUY_NO", b[2], b[3], b[0], b[2])]
        out.append((Opportunity("complement", legs, 1.0,
                                note="YES+NO settles at exactly $1"), _days(m)))
    return out


# ── monotonicity: A implies B, pays at least $1 ────────────────────────────

def scan_monotonicity(rows, cache) -> list[tuple[Opportunity, float]]:
    thr = defaultdict(list)
    for m in rows:
        q = m.get("question") or ""
        if not _tokens(m)[0]:
            continue
        g = NUM.search(q)
        if not g:
            continue
        masked = NUM.sub("#", q)
        up, down = bool(UP.search(masked)), bool(DOWN.search(masked))
        if down:
            up = False          # "reach X or lower" inverts on the tail
        if not (up or down):
            continue
        thr[(masked, (m.get("endDate") or "")[:10], up)].append(
            (_scale(g.group(1), g.group(2)), m))

    out = []
    for (_mask, _end, up), legs in thr.items():
        if len(legs) < 2:
            continue
        legs.sort(key=lambda x: x[0])

        # Direction is INFERRED FROM THE LADDER'S OWN PRICES, not from the
        # wording. A regex cannot know which way the underlying is moving:
        # "Will approval hit 30%?" vs "hit 35%?" reads as an UP ladder, but if
        # approval is currently above both and falling, reaching 30% requires
        # passing through 35%, so 30% is the STRONGER claim. Reading it the
        # wrong way turned a correctly-priced pair (0.07 vs 0.70) into a
        # spurious +63c "violation" -- the single survivor of a 345-candidate
        # live run.
        #
        # A ladder of 3+ legs reveals its own direction: prices fall as the
        # claim gets stronger. Fit that slope and use it. Two-leg ladders have
        # no slope to fit and are skipped rather than guessed.
        if len(legs) < 3:
            continue
        marks = []
        for _v, mm in legs:
            yt, _ = _tokens(mm)
            t = cache.touch(yt) if yt else None
            if t:
                marks.append((_v, 0.5 * (t[0] + t[2])))
        if len(marks) < 3:
            continue
        # Require GLOBAL monotonicity, not a local slope. "Hit X%" for a
        # two-sided quantity peaks near the current value and decays both
        # ways -- the live Trump-approval ladder runs 0.014, 0.05, 0.08, 0.30
        # at 35%, then 0.042, 0.013 above it. Those claims are NOT nested:
        # hitting 44% (rising) does not require hitting 35% (falling), so no
        # implication exists and no arbitrage is possible. A local slope-fit
        # found one side of that peak and produced a spurious survivor twice.
        #
        # A genuinely nested ladder is monotone across EVERY leg. Anything
        # else is a different question wearing the same wording.
        ps = [m[1] for m in marks]
        mono_up = all(b >= a for a, b in zip(ps, ps[1:]))
        mono_dn = all(b <= a for a, b in zip(ps, ps[1:]))
        if mono_up == mono_dn:
            continue                      # flat or non-monotone: not a ladder
        # price falls as the number rises => higher number is the STRONGER claim
        up = mono_dn
        for i in range(len(legs) - 1):
            lo, hi = legs[i][1], legs[i + 1][1]
            strong, weak = (hi, lo) if up else (lo, hi)
            ys, _ = _tokens(strong)
            yw, _ = _tokens(weak)
            if not ys or not yw:
                continue
            a, b = cache.touch(ys), cache.touch(yw)
            if not a or not b:
                continue
            # buy YES(weak) at its ask, buy NO(strong) at 1 - bid(strong)
            no_px = 1.0 - a[0]
            legs_o = [
                Leg(yw, (weak.get("question") or "")[:80], "BUY_YES",
                    b[2], b[3], b[0], b[2]),
                Leg(ys, (strong.get("question") or "")[:80], "BUY_NO",
                    no_px, a[1], a[0], a[2]),
            ]
            out.append((Opportunity("monotonicity", legs_o, 1.0,
                                    note="stronger claim implies weaker"),
                        _days(weak)))
    return out


# ── exclusive: buy NO on n mutually exclusive legs, pays at least n-1 ──────

def scan_exclusive(rows, cache, min_legs: int = 5) -> list[tuple[Opportunity, float]]:
    SC = re.compile(r"Exact Score:\s*(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+?)\?")
    fx = defaultdict(list)
    for m in rows:
        g = SC.search(m.get("question") or "")
        if g and _tokens(m)[0]:
            fx[(g.group(1).strip(), g.group(4).strip())].append(m)

    out = []
    for _k, legs in fx.items():
        if len(legs) < min_legs:
            continue
        o_legs = []
        for m in legs:
            yes, _ = _tokens(m)
            t = cache.touch(yes)
            if not t:
                continue
            o_legs.append(Leg(yes, (m.get("question") or "")[:80], "BUY_NO",
                              1.0 - t[0], t[1], t[0], t[2]))
        if len(o_legs) < min_legs:
            continue
        # at most one of n resolves YES -> at least n-1 legs pay $1.
        # This bound holds for ANY subset, so incomplete coverage is safe here.
        out.append((Opportunity("exclusive", o_legs, float(len(o_legs) - 1),
                                note="at most one scoreline can win"),
                    _days(legs[0])))
    return out


def scan_all(rows=None, delay: float = 0.09):
    rows = rows if rows is not None else fetch_universe()
    cache = BookCache(delay=delay)
    opps = []
    opps += scan_monotonicity(rows, cache)
    opps += scan_exclusive(rows, cache)
    opps += scan_complement(rows, cache)
    return rows, cache, opps
