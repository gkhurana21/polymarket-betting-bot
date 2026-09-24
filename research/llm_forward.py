"""Forward forecasting test: LLM WITH live search vs the market price.

RESEARCH ONLY. No orders. Writes data/llm_forward.db only.

────────────────────────────────────────────────────────────────────────────
What this tests that the earlier study could not
────────────────────────────────────────────────────────────────────────────
`llm_forecast.py` scored gemini-3.1-pro at Brier 0.1571 against the market's
0.1368 -- 14.9% worse -- and the cause was plain: a January 2025 training
cutoff forecasting May-August 2026 events against a market holding current
information. That result rejects LLM forecasting WITHOUT retrieval. It says
nothing about a model that can read today's news, and that could not be tested
on a historical corpus because we cannot retroactively hand a model 2026 news.

Forward markets can test it. Google Search grounding is verified working on
this key: asked for today's date and current Senate leadership, the model
returned both correctly -- information 20 months past its cutoff.

────────────────────────────────────────────────────────────────────────────
Design
────────────────────────────────────────────────────────────────────────────
* Markets resolving within ~60 days, priced 0.05-0.95. Near-certain markets
  are excluded: predicting them correctly demonstrates nothing, and they would
  flatter the Brier score of any forecaster.
* The MARKET PRICE IS NEVER SHOWN. Anchoring the model to the price measures
  the price, not the model.
* Prediction and price are both stored NOW, before resolution, so the
  comparison is locked in and cannot be selected after the fact.
* Contamination is structurally impossible here -- these events have not
  happened yet. That removes the weakest part of the historical study, where
  memorisation had to be detected rather than ruled out.

Scoring waits for resolution. A model that beats the market's Brier on the
same items has the first genuine informational edge in this project; one that
does not closes the retrieval question for good.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

MODEL = "gemini-3.1-pro-preview"
URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       f"{MODEL}:generateContent")
KEY_FILE = os.path.expanduser("~/.config/gemini/key")
DB = "data/llm_forward.db"

PROMPT = """You are forecasting a prediction-market question. Search the web
for current information before answering.

QUESTION: {q}

RESOLUTION CRITERIA: {desc}

Today is {today}. This market resolves on {end}.

Give the probability this resolves YES. Use what you can find TODAY -- recent
news, polls, reporting. Be calibrated, not cautious: if the evidence points
strongly one way, say so.

Reply with STRICT JSON only:
{{"p_yes": <float 0..1>, "key_fact": "<the single most decisive thing you
found, 25 words max>"}}"""


def load_key() -> str | None:
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    try:
        return open(KEY_FILE).read().strip() or None
    except OSError:
        return None


def ask(key: str, q: str, desc: str, end: str) -> dict | None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(
            q=q, desc=(desc or "(none)")[:900], today=today, end=end[:10])}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2000},
    }
    req = urllib.request.Request(
        f"{URL}?key={key}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        cand = d["candidates"][0]
        parts = cand["content"]["parts"]
        txt = next(p["text"] for p in parts if isinstance(p.get("text"), str))
        grounded = bool(cand.get("groundingMetadata"))
    except Exception:
        return None
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    # the model sometimes wraps JSON in prose; take the first {...} block
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        txt = txt[i:j + 1]
    try:
        rec = json.loads(txt)
        p = float(rec["p_yes"])
    except Exception:
        return None
    if not (0.0 <= p <= 1.0):
        return None
    return {"p_yes": p, "key_fact": str(rec.get("key_fact", ""))[:240],
            "grounded": grounded}


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fwd (
        market_id TEXT PRIMARY KEY, question TEXT, end_date TEXT,
        asked_at INTEGER, p_model REAL, p_market REAL, grounded INTEGER,
        key_fact TEXT, resolved_outcome TEXT, resolved_at INTEGER)""")
    conn.commit()


def main():
    key = load_key()
    if not key:
        print("no API key"); return
    items = json.load(open("/tmp/near_markets.json"))
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure(conn)
    done = {r[0] for r in conn.execute("SELECT market_id FROM fwd").fetchall()}
    todo = [it for it in items if str(it["id"]) not in done]
    print(f"forecasting {len(todo)} markets with live search", flush=True)
    ok = bad = ung = 0
    for i, it in enumerate(todo, 1):
        r = ask(key, it["q"], it.get("desc", ""), it["end"])
        if r is None:
            bad += 1
        else:
            if not r["grounded"]:
                ung += 1
            conn.execute(
                "INSERT OR REPLACE INTO fwd VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                (str(it["id"]), it["q"][:200], it["end"][:10],
                 int(time.time() * 1000), r["p_yes"], it["price"],
                 1 if r["grounded"] else 0, r["key_fact"]))
            conn.commit()
            ok += 1
            d = r["p_yes"] - it["price"]
            if abs(d) >= 0.15:
                print(f"  DISAGREE {d:+.2f}  model {r['p_yes']:.2f} vs "
                      f"market {it['price']:.2f}  {it['q'][:46]}", flush=True)
                print(f"     {r['key_fact'][:96]}", flush=True)
        if i % 10 == 0:
            print(f"  {i}/{len(todo)} ok={ok} bad={bad} ungrounded={ung}", flush=True)
        time.sleep(0.4)
    print(f"\nstored {ok}, failed {bad}, ungrounded {ung}")
    n = conn.execute("SELECT COUNT(*) FROM fwd").fetchone()[0]
    print(f"total forecasts on file: {n}")
    print("Score with --score once these resolve.")
    conn.close()


if __name__ == "__main__":
    main()
