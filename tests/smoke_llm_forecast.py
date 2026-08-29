"""Validate the contamination detector BEFORE spending API budget.

The detector is the study. If it fails to fire on a model that memorised the
outcomes, every downstream number is unfalsifiable — the same way mid-price
fills and stub-order books produced confident, meaningless results earlier in
this repository.

Equally important is the reverse: a detector that flags a genuinely good
forecaster is useless, because we would throw away the one result we want.
Both directions are asserted here against REAL corpus outcomes and REAL market
prices, with only the model's probabilities synthesised.
"""

import random
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "research")
from llm_forecast import cohort_brier, detect_contamination, brier   # noqa: E402
from longshot_study import load_quotes, one_per_market               # noqa: E402


def corpus() -> list[dict]:
    q = one_per_market(load_quotes("data/trades.db", 6.0, 0.0, 1e9, 0.05))
    conn = sqlite3.connect("data/trades.db")
    res = dict(conn.execute(
        "SELECT market_id, resolved_at_ts FROM resolved_markets").fetchall())
    conn.close()
    out = []
    for x in q:
        ts = res.get(x.market_id)
        if ts is None:
            continue
        month = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m")
        out.append(dict(month=month, y=1.0 if x.resolved_yes else 0.0,
                        p_market=(x.ask_yes + x.bid_yes) / 2))
    return out


def clamp(p): return min(max(p, 0.001), 0.999)


def run() -> int:
    rows = corpus()
    months = sorted({r["month"] for r in rows})
    print(f"corpus: {len(rows):,} markets across {months}")
    assert len(rows) > 500, "corpus too small to test with"
    assert len(months) >= 3, "need >=3 months to test a step change"
    rng = random.Random(11)
    fails = 0

    def check(name: str, records: list[dict], expect_flag: bool) -> None:
        nonlocal fails
        cohorts = cohort_brier(records)
        flagged, reasons, warns = detect_contamination(cohorts)
        b_model = brier([(r["p_model"], r["y"]) for r in records])
        b_mkt = brier([(r["p_market"], r["y"]) for r in records])
        ok = (flagged == expect_flag)
        print(f"\n[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"       model Brier {b_model:.4f} vs market {b_mkt:.4f}"
              f" | flagged={flagged} expected={expect_flag}")
        for r in reasons:
            print(f"       - DISQUALIFY: {r}")
        for w in warns:
            print(f"       - warn: {w}")
        if not ok:
            fails += 1

    # 1. Contaminated: memorised everything before a cutoff, guesses after.
    cutoff = months[len(months) // 2]
    rec = []
    for r in rows:
        if r["month"] < cutoff:
            p = 0.98 * r["y"] + 0.01               # remembers the answer
        else:
            p = clamp(r["p_market"] + rng.gauss(0, 0.05))
        rec.append({**r, "p_model": p, "known_outcome": False})
    check(f"contaminated model (cutoff {cutoff}), self-report SUPPRESSED",
          rec, expect_flag=True)

    # 2. Honest but mediocre: market price plus noise. Must NOT flag.
    rec = [{**r, "p_model": clamp(r["p_market"] + rng.gauss(0, 0.08)),
            "known_outcome": False} for r in rows]
    check("honest noisy forecaster (slightly worse than market)",
          rec, expect_flag=False)

    # 3. Honest AND genuinely good — the result we are hunting. A detector
    #    that flags this would discard the finding, so this must stay clean.
    rec = [{**r, "p_model": clamp(r["p_market"] + 0.25 * (r["y"] - r["p_market"])
                                  + rng.gauss(0, 0.03)),
            "known_outcome": False} for r in rows]
    check("genuinely skilled forecaster (~25% toward truth)",
          rec, expect_flag=False)

    # 4. Honest, and better than the market by a LOT but not omniscient.
    #    Upper bound of what we would still believe.
    rec = [{**r, "p_model": clamp(r["p_market"] + 0.45 * (r["y"] - r["p_market"])
                                  + rng.gauss(0, 0.03)),
            "known_outcome": False} for r in rows]
    check("strongly skilled forecaster (~45% toward truth) — warn, not reject",
          rec, expect_flag=False)

    # 4b. Uniformly contaminated: knows EVERY month, so there is no step
    #     change to detect. Only the memorisation threshold catches this, which
    #     is why it cannot be removed in favour of the step test.
    rec = [{**r, "p_model": 0.99 * r["y"] + 0.005, "known_outcome": False}
           for r in rows]
    check("uniformly contaminated (cutoff after corpus, NO step change)",
          rec, expect_flag=True)

    # 5. Self-report alone must be sufficient, even with ordinary Brier.
    rec = [{**r, "p_model": clamp(r["p_market"] + rng.gauss(0, 0.08)),
            "known_outcome": (i % 5 == 0)} for i, r in enumerate(rows)]
    check("honest Brier but admits knowing 20% of outcomes",
          rec, expect_flag=True)

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
