"""Score the forward forecasts in data/llm_forward.db against the market.

RESEARCH ONLY. No orders. Writes only `resolved_outcome` / `resolved_at` in
data/llm_forward.db.

Every rule here is fixed by research/PREREG_llm_forward.md, committed before
the primary set began resolving. If this file and the pre-registration ever
disagree, the pre-registration wins and this file has a bug.

    python research/score_forward.py --resolve   # fetch outcomes from Gamma
    python research/score_forward.py --score     # the pre-registered report
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from llm_forecast import brier

DB = "data/llm_forward.db"
GAMMA = "https://gamma-api.polymarket.com/markets/"

REGISTERED = "2026-10-08"          # rows ending before this are excluded
SEED = 20261008
N_BOOT = 10_000

# Forecasts whose key fact visibly quotes the market. Frozen at registration.
ODDS_RE = re.compile(r"prediction market|kalshi|polymarket|odds|betting", re.I)

# Clusters resample whole. The midterm rows share one national swing, so they
# are one cluster, not 112 independent draws.
_CLUSTERS = {
    "midterms": {"562830", "562831", "562794", "634974", "634976", "634892",
                 "562828", "634893", "907687", "635016", "634879", "635012",
                 "634878", "907688", "907690"},
    "nobel": {"637031", "637022", "637025"},
    "israel_pm": {"682708", "682705", "682706"},
    "bernie": {"593972", "593974"},
    "brazil": {"601819", "601826"},               # excluded; kept so it maps
}
_SINGLETONS = {"776464", "668591"}


def cluster_of(market_id: str, question: str) -> str:
    if "House seat" in question:
        return "midterms"
    for name, ids in _CLUSTERS.items():
        if market_id in ids:
            return name
    if market_id in _SINGLETONS:
        return market_id
    # Every row was assigned at registration. An unknown id means the data
    # changed underneath the study -- stop rather than invent a cluster.
    raise ValueError(f"market {market_id} has no pre-registered cluster")


# ── resolution ──────────────────────────────────────────────────────────────

def parse_resolution(m: dict, requested_id: str) -> tuple[str, str]:
    """Return (status, detail); status is YES, NO, VOID, UNRESOLVED or ERROR."""
    if str(m.get("id")) != str(requested_id):
        return "ERROR", f"asked for {requested_id}, Gamma returned {m.get('id')}"
    if not m.get("closed") or m.get("umaResolutionStatus") != "resolved":
        return "UNRESOLVED", f"closed={m.get('closed')} uma={m.get('umaResolutionStatus')}"
    try:
        outcomes = json.loads(m["outcomes"])
        prices = json.loads(m["outcomePrices"])
        yes = prices[outcomes.index("Yes")]
        no = prices[outcomes.index("No")]
    except (KeyError, ValueError, TypeError, IndexError) as e:
        return "ERROR", f"unparseable outcomes: {e}"
    if (yes, no) == ("1", "0"):
        return "YES", ""
    if (yes, no) == ("0", "1"):
        return "NO", ""
    return "VOID", f"final prices yes={yes} no={no}"


def _closed_ms(m: dict) -> int | None:
    t = m.get("closedTime")
    if not t:
        return None
    t = re.sub(r"([+-]\d\d)$", r"\1:00", t.strip())   # Gamma writes "+00"
    try:
        return int(datetime.fromisoformat(t).timestamp() * 1000)
    except ValueError:
        return None


def fetch_market(market_id: str) -> dict | None:
    req = urllib.request.Request(GAMMA + market_id,
                                 headers={"User-Agent": "polybot-research/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def resolve(db: str) -> None:
    conn = sqlite3.connect(db, timeout=30.0)
    rows = conn.execute("SELECT market_id FROM fwd WHERE resolved_outcome IS NULL "
                        "ORDER BY end_date").fetchall()
    print(f"checking {len(rows)} unresolved rows")
    counts: dict[str, int] = {}
    for (mid,) in rows:
        m = fetch_market(mid)
        status, detail = ("ERROR", "fetch failed") if m is None else parse_resolution(m, mid)
        counts[status] = counts.get(status, 0) + 1
        if status in ("YES", "NO", "VOID"):
            # Resolution is final; a resolved row is never fetched again.
            conn.execute("UPDATE fwd SET resolved_outcome=?, resolved_at=? "
                         "WHERE market_id=?", (status, _closed_ms(m), mid))
            conn.commit()
        if status in ("ERROR", "VOID"):
            print(f"  {status:<10} {mid}  {detail}")
        time.sleep(0.2)
    conn.close()
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))


# ── statistics ──────────────────────────────────────────────────────────────

def paired_d(p_model: float, p_market: float, y: float) -> float:
    """Negative means the model was closer to the outcome than the market."""
    return (p_model - y) ** 2 - (p_market - y) ** 2


def log_loss(p: float, y: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def cluster_bootstrap(groups: list[list[float]], n: int = N_BOOT,
                      seed: int = SEED) -> tuple[float, float]:
    """95% percentile CI of the pooled mean, resampling whole clusters."""
    rng = random.Random(seed)
    k = len(groups)
    stats = []
    for _ in range(n):
        tot = cnt = 0
        for _ in range(k):
            g = groups[rng.randrange(k)]
            tot += sum(g)
            cnt += len(g)
        stats.append(tot / cnt)
    return _percentile(stats, 0.025), _percentile(stats, 0.975)


def iid_bootstrap(ds: list[float], n: int = N_BOOT, seed: int = SEED) -> tuple[float, float]:
    return cluster_bootstrap([[d] for d in ds], n, seed)


# ── report ──────────────────────────────────────────────────────────────────

def load(db: str) -> list[dict]:
    # A plain connection, SELECT only: a read-only URI cannot open a WAL
    # database whose -shm file is absent, and immutable=1 would skip the WAL.
    conn = sqlite3.connect(db, timeout=30.0)
    rows = conn.execute("SELECT market_id, question, end_date, p_model, p_market, "
                        "key_fact, resolved_outcome FROM fwd").fetchall()
    conn.close()
    out = []
    for mid, q, end, pm, pk, fact, res in rows:
        out.append(dict(id=mid, question=q, end=end, p_model=pm, p_market=pk,
                        cites_odds=bool(ODDS_RE.search(fact or "")),
                        cluster=cluster_of(mid, q), outcome=res,
                        excluded=end < REGISTERED))
    return out


def _summary(name: str, rows: list[dict]) -> float | None:
    if not rows:
        print(f"  {name:<28} n=0")
        return None
    ds = [paired_d(r["p_model"], r["p_market"], r["y"]) for r in rows]
    d = sum(ds) / len(ds)
    bm = brier([(r["p_model"], r["y"]) for r in rows])
    bk = brier([(r["p_market"], r["y"]) for r in rows])
    print(f"  {name:<28} n={len(rows):<4} D={d:+.4f}  "
          f"Brier model {bm:.4f} market {bk:.4f}")
    return d


def score(db: str) -> None:
    rows = load(db)
    excluded = [r for r in rows if r["excluded"]]
    primary = [r for r in rows if not r["excluded"]]
    scored = [dict(r, y=1.0 if r["outcome"] == "YES" else 0.0)
              for r in primary if r["outcome"] in ("YES", "NO")]
    pending = [r for r in primary if r["outcome"] not in ("YES", "NO")]

    print(f"\n── Population (pre-registered {REGISTERED}) ──")
    print(f"  excluded, ended before registration: {len(excluded)} "
          f"({', '.join(r['id'] for r in excluded)})")
    print(f"  primary set: {len(primary)}  scored: {len(scored)}  "
          f"not scored: {len(pending)}")
    for r in pending:
        print(f"    {r['outcome'] or 'unresolved':<10} {r['id']}  {r['end']}  "
              f"{r['question'][:60]}")
    if not scored:
        print("\nnothing resolved yet")
        return

    print("\n── Primary: D = mean[(p_model−y)² − (p_market−y)²], negative = model better ──")
    d_all = _summary("primary set", scored)
    groups: dict[str, list[float]] = {}
    for r in scored:
        groups.setdefault(r["cluster"], []).append(
            paired_d(r["p_model"], r["p_market"], r["y"]))
    lo, hi = cluster_bootstrap(list(groups.values()))
    print(f"  cluster bootstrap 95% CI ({len(groups)} clusters, {N_BOOT:,} draws, "
          f"seed {SEED}): [{lo:+.4f}, {hi:+.4f}]")
    for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"    {name:<12} n={len(g):<4} D={sum(g) / len(g):+.4f}")

    print("\n── Pre-registered subgroup: key fact does not cite odds ──")
    d_sub = _summary("no odds citation", [r for r in scored if not r["cites_odds"]])
    _summary("cites odds", [r for r in scored if r["cites_odds"]])

    print("\n── Reported, not decisive ──")
    ilo, ihi = iid_bootstrap([paired_d(r["p_model"], r["p_market"], r["y"])
                              for r in scored])
    print(f"  iid bootstrap 95% CI (anti-conservative): [{ilo:+.4f}, {ihi:+.4f}]")
    lm = sum(log_loss(r["p_model"], r["y"]) for r in scored) / len(scored)
    lk = sum(log_loss(r["p_market"], r["y"]) for r in scored) / len(scored)
    print(f"  log-loss  model {lm:.4f}  market {lk:.4f}")
    _summary("resolved Oct 10 / Oct 27", [r for r in scored if r["end"] < "2026-11-01"])

    demonstrated = hi < 0 and d_sub is not None and d_sub < 0
    print("\n── Verdict ──")
    if len(pending):
        print(f"  PROVISIONAL: {len(pending)} primary rows not yet scored")
    print("  RETRIEVAL EDGE DEMONSTRATED" if demonstrated else "  not demonstrated")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()
    if args.resolve:
        resolve(args.db)
    if args.score:
        score(args.db)
    if not (args.resolve or args.score):
        ap.print_help()


if __name__ == "__main__":
    main()
