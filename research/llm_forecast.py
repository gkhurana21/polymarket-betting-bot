"""Can an LLM forecast Polymarket markets better than the market price?

RESEARCH ONLY. Places no orders. Read-only against data/trades.db plus two
outbound calls: Gamma (market metadata backfill) and the Gemini API.

────────────────────────────────────────────────────────────────────────────
The bar
────────────────────────────────────────────────────────────────────────────
Measured on 2,379 resolved markets in this corpus:

    always 0.50            Brier 0.2500
    always base rate 24.8% Brier 0.1865
    MARKET MID PRICE       Brier 0.1313   <- the number to beat

Accuracy is not edge. A model that is 75% accurate on a corpus where NO wins
75% of the time has learned nothing. Brier against the market is the only
comparison that means anything, and the market gets 29.6% skill over the base
rate. This is the fifth time this project has had to restate that principle;
see FINDINGS.md "win rate is not profit".

────────────────────────────────────────────────────────────────────────────
The trap: training contamination
────────────────────────────────────────────────────────────────────────────
This corpus resolved 2026-05-23 -> 2026-08-21. If the model's training data
includes that window, it does not forecast these questions — it REMEMBERS
them, and will post a spectacular fake edge.

That is the same failure shape as the two bugs already caught in this
repository: mid-price treated as an executable fill (+21.8% -> -18.1%), and
bid 0.001 / ask 0.998 books priced as 99.8% favourites (fake t = -3.31). Both
looked like signal and were leakage.

Three guards, in order of strength:

  1. `--probe-cutoff` empirically locates the model's knowledge boundary by
     asking about events with known dates, before any forecasting runs.
  2. Per-month cohort scoring. A model that remembers May and not August
     produces a step change in Brier at its cutoff. A genuine forecaster
     degrades smoothly. The step IS the contamination signal.
  3. The prompt asks the model to declare whether it already knows the
     outcome. Self-report is the weakest guard — it is a cross-check on 1
     and 2, never a substitute.

The market price is NEVER shown to the model. Anchoring it to the price would
measure the price, not the model.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/markets"
GEMINI = ("https://generativelanguage.googleapis.com/v1beta/models/"
          "{model}:generateContent")

# Key is read from the environment or a gitignored file — never passed as an
# argument, so it cannot land in shell history or a process listing.
KEY_ENV = "GEMINI_API_KEY"
KEY_FILE = os.path.expanduser("~/.config/gemini/key")


def load_api_key() -> str | None:
    key = os.environ.get(KEY_ENV)
    if key:
        return key.strip()
    try:
        with open(KEY_FILE) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def http_json(url: str, payload: dict | None = None,
              timeout: float = 45.0) -> dict | list | None:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "polymarket-research/1.0"},
        method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, json.JSONDecodeError, OSError):
        return None


# ── step 1: backfill question text ─────────────────────────────────────────
#  The scanner stored condition hashes, not questions: only 4 of 4,551
#  resolved markets carry text. Without the question there is nothing to
#  prompt with, so this must run before any forecasting.

def ensure_backfill_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_metadata (
            market_id   TEXT PRIMARY KEY,
            question    TEXT,
            description TEXT,
            end_date    TEXT,
            category    TEXT,
            fetched_at  INTEGER
        )""")
    conn.commit()


def backfill(db: str, limit: int, delay: float) -> None:
    """Fetch question + resolution criteria for resolved markets."""
    conn = sqlite3.connect(db)
    ensure_backfill_table(conn)
    todo = [r[0] for r in conn.execute("""
        SELECT DISTINCT r.market_id
        FROM resolved_markets r
        JOIN book_snapshots b ON b.market_id = r.market_id
        LEFT JOIN market_metadata m ON m.market_id = r.market_id
        WHERE r.resolved_outcome IN ('YES','NO') AND m.market_id IS NULL
    """).fetchall()][:limit]

    print(f"backfill: {len(todo):,} markets need metadata")
    ok = fail = 0
    for i, mid in enumerate(todo, 1):
        got = http_json(f"{GAMMA}?condition_ids={mid}")
        rec = got[0] if isinstance(got, list) and got else None
        if rec is None:
            fail += 1
        else:
            conn.execute("INSERT OR REPLACE INTO market_metadata VALUES (?,?,?,?,?,?)",
                         (mid, rec.get("question"), rec.get("description"),
                          rec.get("endDate"), rec.get("category"),
                          int(time.time() * 1000)))
            ok += 1
        if i % 25 == 0:
            conn.commit()
            print(f"  {i:,}/{len(todo):,}  ok={ok} fail={fail}", flush=True)
        time.sleep(delay)
    conn.commit()
    conn.close()
    print(f"backfill done: ok={ok} fail={fail}")


# ── step 2: ask the model ──────────────────────────────────────────────────

PROMPT = """You are forecasting a prediction-market question.

QUESTION: {question}

RESOLUTION CRITERIA: {description}

Today is {asof}. This market resolves on {end_date}.

Give the probability that this resolves YES, judged only from what was
knowable as of {asof}.

Answer with STRICT JSON and nothing else:
{{"p_yes": <float 0..1>, "known_outcome": <true|false>, "why": "<20 words>"}}

Set "known_outcome" to true if you already know how this actually resolved
from your training data, rather than forecasting it. Answer honestly: a true
value here excludes the item from scoring, it is not a penalty."""


def ask_gemini(key: str, model: str, question: str, description: str,
               asof: str, end_date: str) -> dict | None:
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(
            question=question, description=(description or "(none given)")[:1500],
            asof=asof, end_date=end_date or "(unknown)")}]}],
        # Temperature 0: we want the model's modal estimate, not a sample.
        # A sampled forecast would add variance we would then mistake for
        # model uncertainty.
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 300},
    }
    url = GEMINI.format(model=model) + f"?key={key}"
    got = http_json(url, body)
    if not got:
        return None
    try:
        txt = got["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        rec = json.loads(txt)
        p = float(rec["p_yes"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= p <= 1.0):
        return None
    return {"p_yes": p, "known_outcome": bool(rec.get("known_outcome", False)),
            "why": str(rec.get("why", ""))[:200]}


def brier(pairs: list[tuple[float, float]]) -> float:
    return statistics.fmean((p - y) ** 2 for p, y in pairs) if pairs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--model", default="gemini-2.5-pro")
    ap.add_argument("--check", action="store_true",
                    help="verify key + connectivity, make no forecasts")
    args = ap.parse_args()

    if args.check:
        key = load_api_key()
        print(f"api key   : {'found' if key else 'MISSING'}"
              f"  (env {KEY_ENV} or {KEY_FILE})")
        print(f"gamma     : {'reachable' if http_json(GAMMA + '?limit=1') else 'BLOCKED'}")
        if key:
            r = ask_gemini(key, args.model, "Will the sun rise tomorrow?",
                           "Resolves YES if the sun rises.", "2026-08-29", "2026-08-30")
            print(f"gemini    : {'reachable — p_yes=' + str(r['p_yes']) if r else 'FAILED'}")
        conn = sqlite3.connect(args.db)
        ensure_backfill_table(conn)
        n = conn.execute("SELECT COUNT(*) FROM market_metadata "
                         "WHERE question IS NOT NULL").fetchone()[0]
        print(f"questions : {n:,} backfilled")
        conn.close()
        return

    if args.backfill:
        backfill(args.db, args.limit, args.delay)
        return

    print("nothing to do; pass --check or --backfill")


if __name__ == "__main__":
    main()
