"""LLM adjudication of cross-venue market pairs.

The token matcher cannot tell whether two questions ask the same thing. Its
worst failure paired

    Polymarket: "Will the Democrats WIN the Pennsylvania governor race?"  0.95
    Kalshi:     "CLOSEST Governor race? Pennsylvania"                     0.001

Same year, same state, same nouns, opposite meaning — and a fabricated +0.937
"arbitrage". A hand-built PREDICATE set cut 177 candidates to 27, but that is
a keyword patch on a semantic problem: it cannot catch a mismatch phrased with
words nobody thought to enumerate.

Judging whether two resolution criteria describe the same event is pure text
comprehension. It needs NO current information, so a model with a January 2025
cutoff is not handicapped here at all — unlike forecasting 2026 outcomes.

THE FAILURE MODE THIS GUARDS AGAINST
A judge that says SAME too readily is worse than no judge: it launders a bad
pair into a confident one, and the loss shows up only at resolution, when the
two venues pay differently and the "hedge" was never a hedge. So:

  * The prompt demands NOT_SAME on any doubt, and asks for the specific
    difference rather than a verdict alone.
  * Prices are never shown. A judge told one leg is 0.95 and the other 0.001
    can infer it is looking at an arbitrage and rationalise agreement.
  * validate() runs a labelled set FIRST, including the real false positive
    above. A judge that cannot reject that pair is not used.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

MODEL = "gemini-3.1-pro-preview"
URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       f"{MODEL}:generateContent")
KEY_FILE = os.path.expanduser("~/.config/gemini/key")

PROMPT = """Two prediction markets from different exchanges. Decide whether
they resolve identically — same event, same threshold, same time window, same
side.

MARKET A ({venue_a}): {qa}
MARKET B ({venue_b}): {qb}

A trader will hold these as opposite legs of a hedge. If they differ in ANY
respect — different subject, different comparison, one asks "wins" and the
other "is closest", different dates, different thresholds, different
resolution source — the hedge silently fails and the trader loses money.

Answer NOT_SAME unless you are confident they are interchangeable. Being
wrong in the SAME direction is far more costly than being wrong in the
NOT_SAME direction.

Reply with STRICT JSON only:
{{"same": <true|false>, "confidence": <0..1>, "difference": "<the specific
difference, or 'none' if identical>"}}"""


def load_key() -> str | None:
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    try:
        return open(KEY_FILE).read().strip() or None
    except OSError:
        return None


def judge(key: str, qa: str, qb: str,
          venue_a: str = "Polymarket", venue_b: str = "Kalshi") -> dict | None:
    body = {"contents": [{"parts": [{"text": PROMPT.format(
        qa=qa, qb=qb, venue_a=venue_a, venue_b=venue_b)}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1200}}
    req = urllib.request.Request(
        f"{URL}?key={key}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read())
        txt = next(p["text"] for p in d["candidates"][0]["content"]["parts"]
                   if isinstance(p.get("text"), str))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            KeyError, IndexError, TypeError, StopIteration,
            json.JSONDecodeError, OSError):
        return None
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        rec = json.loads(txt)
        return {"same": bool(rec["same"]),
                "confidence": float(rec.get("confidence", 0.0)),
                "difference": str(rec.get("difference", ""))[:200]}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


#  Labelled set. The first case is the real false positive that motivated
#  this module; the rest probe the distinctions that matter for a hedge.
CASES = [
    (False, "Will the Democrats win the Pennsylvania governor race?",
            "Closest Governor race? Pennsylvania"),
    (False, "Will Bitcoin reach $150,000 by December 31, 2026?",
            "Will Bitcoin reach $200,000 by December 31, 2026?"),
    (False, "Will Trump win the 2028 Republican nomination?",
            "Will Trump win the 2028 presidential election?"),
    (False, "Will the Fed cut rates in September 2026?",
            "Will the Fed cut rates in December 2026?"),
    (False, "Will Israel and Hamas agree to a ceasefire before July 2026?",
            "Will Israel and Hezbollah agree to a ceasefire before July 2026?"),
    (True,  "Will the Republicans win the 2026 Georgia Senate race?",
            "Who will win the Georgia Senate election in 2026? Republican"),
    (True,  "Will SpaceX launch Starship successfully in Q3 2026?",
            "Starship successful launch in Q3 2026?"),
    (True,  "Will inflation (CPI YoY) exceed 3% in June 2026?",
            "June 2026 CPI year-over-year above 3%?"),
]


def validate(key: str) -> bool:
    print(f"validating judge on {len(CASES)} labelled pairs "
          f"({sum(1 for c in CASES if c[0])} same / "
          f"{sum(1 for c in CASES if not c[0])} not-same)\n")
    wrong_same = wrong_notsame = err = 0
    for expect, qa, qb in CASES:
        r = judge(key, qa, qb)
        if r is None:
            print(f"  [ERR ] {qa[:46]}")
            err += 1
            continue
        ok = (r["same"] == expect)
        if not ok:
            if r["same"]:
                wrong_same += 1        # the dangerous direction
            else:
                wrong_notsame += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] want={'SAME' if expect else 'NOT_SAME':8} "
              f"got={'SAME' if r['same'] else 'NOT_SAME':8} "
              f"conf={r['confidence']:.2f}")
        print(f"         A: {qa[:66]}")
        print(f"         B: {qb[:66]}")
        if r["difference"] and r["difference"] != "none":
            print(f"         -> {r['difference'][:88]}")
        time.sleep(0.3)

    print(f"\n  false SAME     : {wrong_same}  (dangerous — launders a bad pair)")
    print(f"  false NOT_SAME : {wrong_notsame}  (costs opportunity only)")
    print(f"  errors         : {err}")
    # A single false SAME disqualifies: that is the error that loses money.
    good = (wrong_same == 0 and err == 0)
    print(f"\n  judge is {'USABLE' if good else 'NOT USABLE'}")
    return good


if __name__ == "__main__":
    k = load_key()
    if not k:
        print("no API key"); sys.exit(1)
    sys.exit(0 if validate(k) else 1)
