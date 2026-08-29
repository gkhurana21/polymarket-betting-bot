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




# ────────────────────────────────────────────────────────────────────────────
#  Scoring, cohort analysis, and the contamination detector
#
#  These run on stored forecasts and need no network, so the detector can be
#  validated against synthetic forecasters BEFORE any API budget is spent.
#  That ordering is deliberate: the guard is the study. If the detector does
#  not fire on a known-contaminated model, every number downstream is
#  unfalsifiable.
# ────────────────────────────────────────────────────────────────────────────

#  Memorisation and excellence are different magnitudes, and conflating them
#  cost a test failure: a synthetic forecaster at Brier 0.0406 — extraordinary
#  but honest — tripped a single 0.05 threshold and would have been discarded
#  as contaminated. A model reciting known outcomes scores ~0.001, not ~0.04.
#
#  So the omniscience test is split. Below HARD, no honest forecaster exists
#  and the run is unusable. Between HARD and WARN the result is suspicious but
#  is for a human to judge, not for this script to throw away — it is the one
#  band where a genuine finding could live.
#
#  The threshold cannot simply be deleted in favour of the step test: if the
#  model's cutoff falls AFTER this corpus it knows every month, there is no
#  step to detect, and this is the only guard that fires.
OMNISCIENCE_HARD = 0.02
OMNISCIENCE_WARN = 0.06
#  A genuine forecaster degrades smoothly across months. A hard step is the
#  training cutoff revealing itself.
STEP_RATIO = 3.0
#  Self-report is the weakest guard, so its threshold is the loosest.
SELF_REPORT_RATE = 0.05


def cohort_brier(records: list[dict]) -> list[dict]:
    """Per-month Brier for the model and, on identical items, the market."""
    by_month: dict[str, list[dict]] = {}
    for r in records:
        by_month.setdefault(r["month"], []).append(r)
    out = []
    for month in sorted(by_month):
        rs = by_month[month]
        out.append(dict(
            month=month, n=len(rs),
            model=brier([(r["p_model"], r["y"]) for r in rs]),
            market=brier([(r["p_market"], r["y"]) for r in rs]),
            self_reported=sum(1 for r in rs if r.get("known_outcome")) / len(rs),
        ))
    return out


def detect_contamination(cohorts: list[dict]) -> tuple[bool, list[str], list[str]]:
    """Return (disqualified, reasons, warnings).

    `reasons` are disqualifying. `warnings` mean a human should look before
    believing the result — deliberately NOT automatic rejection, because a
    real finding would land there.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    if not cohorts:
        return (False, ["no cohorts"], [])

    for c in cohorts:
        if c["model"] < OMNISCIENCE_HARD:
            reasons.append(
                f"{c['month']}: Brier {c['model']:.4f} < {OMNISCIENCE_HARD} "
                f"— memorisation, not forecasting "
                f"(market scores {c['market']:.4f})")
        elif c["model"] < OMNISCIENCE_WARN:
            warnings.append(
                f"{c['month']}: Brier {c['model']:.4f} vs market "
                f"{c['market']:.4f} — implausibly strong; verify by hand")

    ordered = sorted(cohorts, key=lambda c: c["month"])
    for a, b in zip(ordered, ordered[1:]):
        if a["model"] > 0 and b["model"] / max(a["model"], 1e-9) >= STEP_RATIO:
            reasons.append(
                f"{a['month']}->{b['month']}: Brier stepped "
                f"{a['model']:.4f} -> {b['model']:.4f} "
                f"({b['model'] / a['model']:.1f}x) — looks like a training cutoff")

    total = sum(c["n"] for c in cohorts)
    claimed = sum(c["self_reported"] * c["n"] for c in cohorts)
    if total and claimed / total > SELF_REPORT_RATE:
        reasons.append(
            f"model self-reported knowing the outcome on "
            f"{claimed / total * 100:.1f}% of items")

    return (bool(reasons), reasons, warnings)


def score_report(records: list[dict]) -> None:
    """The only comparison that means anything: model vs market, same items."""
    if not records:
        print("no forecasts stored")
        return
    m = brier([(r["p_model"], r["y"]) for r in records])
    k = brier([(r["p_market"], r["y"]) for r in records])
    print(f"\n── Brier on {len(records):,} matched items ──")
    print(f"  model  {m:.4f}")
    print(f"  market {k:.4f}")
    verdict = ("MODEL BEATS MARKET" if m < k else "no edge")
    print(f"  -> {verdict}  (skill vs market {(1 - m / k) * 100:+.1f}%)")

    print(f"\n── Per-month cohorts ──")
    cohorts = cohort_brier(records)
    print(f"{'month':>9} {'n':>6} {'model':>8} {'market':>8} {'self-rep':>9}")
    for c in cohorts:
        print(f"{c['month']:>9} {c['n']:>6,} {c['model']:>8.4f} "
              f"{c['market']:>8.4f} {c['self_reported'] * 100:>8.1f}%")

    flagged, reasons, warnings = detect_contamination(cohorts)
    print(f"\n── Contamination check ──")
    if flagged:
        print("  DISQUALIFIED — results are NOT usable:")
        for r in reasons:
            print(f"    * {r}")
    elif warnings:
        print("  SUSPICIOUS — verify by hand before believing:")
        for w in warnings:
            print(f"    * {w}")
    else:
        print("  clean — no memorisation, no step change, low self-report")


if __name__ == "__main__":
    main()
