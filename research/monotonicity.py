"""Two-leg monotonicity arbitrage: dates and thresholds.

RESEARCH ONLY. Read-only public endpoints. No orders, no keys.

────────────────────────────────────────────────────────────────────────────
Why this is not hypothesis 10 again
────────────────────────────────────────────────────────────────────────────
Exact-score and neg-risk sets died on FEE GEOMETRY, not on the absence of
violations. Gross profit there is `sum(bid) - 1` -- one quantity regardless of
leg count -- while the taker fee 0.05*p*(1-p) is charged PER LEG. Six legs of
fee is ~2.7% against observed violations of 1-3%, so the structure could not
pay no matter how large the sample.

This trade has TWO legs. The fee drag is minimal, so a violation of even a few
tenths of a cent survives.

And nothing pins these prices. Complement arb is held by split/merge, which
physically converts $1 into YES+NO. Neg-risk sets have their own conversion.
Two markets on DIFFERENT events -- "by June" and "by December" -- have no
conversion between them at all. The constraint is pure logic, enforced only by
somebody noticing, and this project has measured repeatedly that nobody watches
thin books.

────────────────────────────────────────────────────────────────────────────
The trade
────────────────────────────────────────────────────────────────────────────
If event A implies event B (happening by June implies happening by December;
BTC reaching $120k implies reaching $100k), then P(A) <= P(B) necessarily.

Buy YES on the WEAKER claim B, buy NO on the STRONGER claim A:

    A true  -> B true    : B pays 1, NO(A) pays 0   = 1
    A false, B true      : B pays 1, NO(A) pays 1   = 2
    A false, B false     : B pays 0, NO(A) pays 1   = 1

Payout is ALWAYS >= 1. Cost = ask(B) + (1 - bid(A)).

    profit >= bid(A) - ask(B)

So `bid(stronger) > ask(weaker)` is locked profit, outcome-independent.

Prices are executable top-of-book with real resting size, never midpoints, and
legs with one-sided or absurdly wide books are dropped -- a stub quote is not a
price, the lesson that fabricated a t = -3.31 earlier in this project.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import defaultdict

GAMMA = "https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset={off}"
BOOK = "https://clob.polymarket.com/book?token_id={tok}"
FEE = 0.05
MAX_SPREAD = 0.15

# Predicates whose probability DECREASES as the number rises.
UP = re.compile(r"\b(reach|reaches|above|exceed|exceeds|greater|over|hit|hits|"
                r"or more|at least)\b", re.I)
# Predicates whose probability INCREASES as the number rises.
DOWN = re.compile(r"\b(dip|dips|below|under|less than|fall|falls|drop|drops|"
                  r"or fewer|or lower|or below|or less|at most)\b", re.I)
NUM = re.compile(r"\$?(\d[\d,]*\.?\d*)\s*(k|K|m|M|B|T)?")


def get(url: str, timeout: float = 20.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def scale(v: str, suf: str | None) -> float:
    x = float(v.replace(",", ""))
    return x * {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6,
                "B": 1e9, "T": 1e12}.get(suf or "", 1.0)


def touch(tok: str):
    """(best_bid, bid_size, best_ask) or None if the book is not a price."""
    b = get(BOOK.format(tok=tok))
    if not b:
        return None
    bids, asks = b.get("bids") or [], b.get("asks") or []
    if not bids or not asks:
        return None
    try:
        bb, bsz = max((float(x["price"]), float(x["size"])) for x in bids)
        ba = min(float(x["price"]) for x in asks)
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 < bb < ba < 1.0) or (ba - bb) > MAX_SPREAD:
        return None
    return bb, bsz, ba


def first_token(m: dict) -> str | None:
    try:
        t = json.loads(m["clobTokenIds"])
        return str(t[0]) if len(t) == 2 else None
    except Exception:
        return None


def main() -> None:
    rows, off = [], 0
    while off < 8000:
        p = get(GAMMA.format(off=off))
        if not p:
            break
        rows.extend(p)
        if len(p) < 100:
            break
        off += 100
        time.sleep(0.1)
    print(f"open markets: {len(rows):,}", flush=True)

    # ---- build implication pairs -------------------------------------------
    # threshold ladders: identical wording, differing number, same end date
    thr: dict[tuple, list] = defaultdict(list)
    # date ladders: identical wording INCLUDING number, differing end date
    dat: dict[str, list] = defaultdict(list)
    for r in rows:
        q = r.get("question") or ""
        if not first_token(r):
            continue
        end = (r.get("endDate") or "")[:10]
        m = NUM.search(q)
        if m:
            thr[(NUM.sub("#", q), end)].append((scale(m.group(1), m.group(2)), r))
        dat[q].append((end, r))

    pairs = []
    for (masked, _end), legs in thr.items():
        if len(legs) < 2:
            continue
        up, down = bool(UP.search(masked)), bool(DOWN.search(masked))
        # DOWN wins when both match. "reach 2.0% OR LOWER" contains "reach"
        # (an UP word) but the "or lower" tail inverts it: reaching 1.75% or
        # lower is the STRONGER claim, not the weaker one. Treating it as UP
        # inverted the whole Fed ladder and reported four violations that do
        # not exist -- the live prices are correctly ordered there
        # (1.75% = 0.013 < 2.0% = 0.038).
        if down:
            up = False
        if not (up or down):
            continue                     # no directional predicate: skip
        legs.sort(key=lambda x: x[0])
        for i in range(len(legs) - 1):
            lo_v, lo_m = legs[i]
            hi_v, hi_m = legs[i + 1]
            # "reach $X": higher number is the STRONGER claim (less likely).
            # "dip to $X": LOWER number is the stronger claim.
            strong, weak = (hi_m, lo_m) if up else (lo_m, hi_m)
            pairs.append(("threshold", strong, weak))

    for q, legs in dat.items():
        if len(legs) < 2:
            continue
        legs.sort(key=lambda x: x[0])
        for i in range(len(legs) - 1):
            (e1, m1), (e2, m2) = legs[i], legs[i + 1]
            if not e1 or not e2 or e1 == e2:
                continue
            # earlier deadline is the STRONGER claim
            pairs.append(("date", m1, m2))

    print(f"implication pairs: {len(pairs)} "
          f"({sum(1 for k,_,_ in pairs if k=='threshold')} threshold, "
          f"{sum(1 for k,_,_ in pairs if k=='date')} date)", flush=True)

    hits, scanned = [], 0
    cache: dict[str, object] = {}
    for i, (kind, strong, weak) in enumerate(pairs, 1):
        ts, tw = first_token(strong), first_token(weak)
        if not ts or not tw:
            continue
        for t in (ts, tw):
            if t not in cache:
                cache[t] = touch(t)
                time.sleep(0.09)
        a, b = cache[ts], cache[tw]
        if a is None or b is None:
            continue
        scanned += 1
        bid_strong, size_strong, _ = a
        _, _, ask_weak = b
        gross = bid_strong - ask_weak
        if gross <= 0:
            continue
        shares = size_strong
        fees = (FEE * bid_strong * (1 - bid_strong)
                + FEE * ask_weak * (1 - ask_weak)) * shares
        net = gross * shares - fees
        hits.append((net, gross, kind, shares, fees,
                     (strong.get("question") or "")[:56],
                     (weak.get("question") or "")[:56]))
        print(f"  VIOLATION [{kind}] gross={gross*100:+.2f}c shares={shares:.0f} "
              f"net=${net:+.2f}", flush=True)
        print(f"     stronger: {(strong.get('question') or '')[:66]}", flush=True)
        print(f"     weaker  : {(weak.get('question') or '')[:66]}", flush=True)
        if i % 40 == 0:
            print(f"  ...{i}/{len(pairs)} pairs, {scanned} priced", flush=True)

    print(f"\npairs priced on both legs : {scanned}")
    print(f"monotonicity violations   : {len(hits)}")
    print(f"PROFITABLE after fees     : {sum(1 for h in hits if h[0] > 0)}")
    for h in sorted(hits, reverse=True)[:10]:
        print(f"  net=${h[0]:+8.2f} gross={h[1]*100:+6.2f}c [{h[2]}] "
              f"shares={h[3]:.0f}\n     {h[5]}\n     {h[6]}")
    print("DONE")


if __name__ == "__main__":
    main()
