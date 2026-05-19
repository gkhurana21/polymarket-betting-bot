"""Smoke test for Module 10 (bot/calibration.py).

12 verifications:
   1. Readiness: empty DB → all checks fail
   2. Readiness boundary: 49/50 trades fails, 50/50 passes
   3. Readiness boundary: 4/5 whales fails, 5/5 passes
   4. Readiness boundary: 13/14 days fails, 14/14 passes
   5. compare_proportions: large-N uses z_test
   6. compare_proportions: small-N uses fisher_exact
   7. HALT: null=30/50, copy=28/50
   8. INCONCLUSIVE: null=27/50, copy=30/50 (3pp gap, high p)
   9. BUILD_B_RELAXED: copy beats null; timing ≈ copy
  10. BUILD_B_FULL: copy beats null and timing
  11. Matrix precedence: overlapping row conditions → highest-precedence verdict
  12. Fisher boundary: min cell=4 → fisher_exact; min cell=5 → z_test (method on result)
  13. Tracker.get_calibration_readiness delegates to calibration module

Run:
    .venv/bin/python tests/smoke_module10.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.calibration import (  # noqa: E402
    PairwiseTest,
    StreamStats,
    _decide_verdict,
    compare_proportions,
    evaluate_decision_matrix,
    validate_calibration_readiness,
)
from bot.models import PaperTrade, WhaleSignal  # noqa: E402
from bot.tracker import Tracker  # noqa: E402

_CFG: dict = {
    "calibration_readiness": {
        "min_resolved_trades": 50,
        "min_whales_with_min_trades": 5,
        "min_trades_per_whale": 5,
        "min_calendar_days": 14,
    },
    "decision_matrix": {
        "min_winrate_gap_pp": 5.0,
        "max_p_value": 0.10,
    },
}

_MS_PER_DAY = 86_400_000


def _tracker(tmp: str) -> Tracker:
    return Tracker(db_path=Path(tmp) / "t.db", audit_log_path=Path(tmp) / "a.jsonl")


_counter = 0


def _seed_resolved(
    tk: Tracker,
    *,
    strategy_id: str,
    wins: int,
    losses: int,
    whale: str = "0xWHALE",
    base_ts: int = 1_700_000_000_000,
    day_offset: int = 0,
) -> None:
    """Insert wins+losses resolved paper_trades for one strategy."""
    global _counter
    ts = base_ts + day_offset * _MS_PER_DAY
    for i in range(wins + losses):
        _counter += 1
        win = i < wins
        side = "YES"
        outcome = "YES" if win else "NO"
        market = f"m_{strategy_id}_{_counter}"
        sig = WhaleSignal(
            whale_address=whale,
            market_id=market,
            side=side,
            signal_type="open",
            price=0.5,
            size_usd=100.0,
            whale_fill_ts=ts,
            detected_at_ts=ts + 1000,
            detection_latency_ms=1000,
            raw_json="{}",
            transaction_hash=f"tx-{_counter}",
        )
        sid = tk.record_whale_signal(sig)
        assert sid is not None
        pid = tk.record_paper_trade(PaperTrade(
            strategy_id=strategy_id,
            triggering_signal_id=sid,
            market_id=market,
            side=side,
            size_usd=100.0,
            decided_at_ts=ts + 2000,
            entry_price=0.5,
        ))
        pnl = 100.0 if win else -100.0
        tk.resolve_paper_trade(pid, outcome, pnl, ts + 3000)


def _seed_whales_with_trades(
    tk: Tracker,
    n_whales: int,
    trades_each: int,
    *,
    base_ts: int,
    whale_offset: int = 0,
) -> None:
    for w in range(n_whales):
        whale = f"0xWHALE{whale_offset + w:04d}"
        _seed_resolved(
            tk, strategy_id="copy_whale", wins=trades_each, losses=0,
            whale=whale, base_ts=base_ts, day_offset=w,
        )


def main() -> int:
    # 12: Fisher boundary — min cell 4 vs 5, method reported on PairwiseTest
    _, method_cell4 = compare_proportions(4, 9, 4, 9)   # cells 4,5,4,5 → min=4
    assert method_cell4 == "fisher_exact"
    _, method_cell5 = compare_proportions(5, 10, 5, 10)  # cells all 5 → min=5
    assert method_cell5 == "z_test"
    # integration: evaluate_decision_matrix exposes .method on PairwiseTest
    with tempfile.TemporaryDirectory() as td:
        tk = _tracker(td + "_fisher")
        _seed_resolved(tk, strategy_id="copy_whale", wins=4, losses=5)
        _seed_resolved(tk, strategy_id="null_random_side", wins=3, losses=6)
        _seed_resolved(tk, strategy_id="whale_side_random_timing", wins=4, losses=5)
        v_small = evaluate_decision_matrix(tk, _CFG)
        assert v_small.copy_vs_null.method == "fisher_exact", v_small.copy_vs_null
        tk.close()
    print("  Fisher cell=4 vs cell=5 + method on verdict   OK")

    # 11: precedence with simultaneous row conditions (crafted stats)
    timing = StreamStats("whale_side_random_timing", 33, 17, 50, 0.66)
    cn_sig = PairwiseTest("copy_vs_null", 26.0, 0.001, "z_test")
    ct_similar = PairwiseTest("copy_vs_timing", 4.0, 0.50, "z_test")
    ct_sig = PairwiseTest("copy_vs_timing", 12.0, 0.02, "z_test")

    # HALT (row 1) beats INCONCLUSIVE (row 2): null≥copy even though gap would be noise
    copy_h = StreamStats("copy_whale", 25, 25, 50, 0.50)
    null_h = StreamStats("null_random_side", 26, 24, 50, 0.52)
    cn_noise = PairwiseTest("copy_vs_null", -2.0, 0.85, "z_test")
    label_h, _ = _decide_verdict(
        copy_h, null_h, timing, cn_noise, ct_similar, 5.0, 0.10,
    )
    assert label_h == "HALT"

    # INCONCLUSIVE (row 2) beats BUILD_B_RELAXED (row 3): copy>null but not significant
    copy_i = StreamStats("copy_whale", 28, 22, 50, 0.56)
    null_i = StreamStats("null_random_side", 27, 23, 50, 0.54)
    cn_inc = PairwiseTest("copy_vs_null", 2.0, 0.84, "z_test")
    label_i, _ = _decide_verdict(
        copy_i, null_i, timing, cn_inc, ct_similar, 5.0, 0.10,
    )
    assert label_i == "INCONCLUSIVE"

    # BUILD_B_RELAXED (row 3) beats BUILD_B_FULL (row 4): both copy>null significant,
    # but timing ≈ copy (p>0.10) — must NOT return FULL
    copy_r = StreamStats("copy_whale", 35, 15, 50, 0.70)
    null_r = StreamStats("null_random_side", 22, 28, 50, 0.44)
    label_r, _ = _decide_verdict(
        copy_r, null_r, timing, cn_sig, ct_similar, 5.0, 0.10,
    )
    assert label_r == "BUILD_B_RELAXED"

    # BUILD_B_FULL (row 4) when copy>null AND copy>timing both significant
    label_f, _ = _decide_verdict(
        copy_r, null_r, timing, cn_sig, ct_sig, 5.0, 0.10,
    )
    assert label_f == "BUILD_B_FULL"
    print("  matrix precedence: 4 overlapping scenarios   OK")

    with tempfile.TemporaryDirectory() as td:
        tk = _tracker(td)

        # 1: empty DB
        rep = validate_calibration_readiness(tk, _CFG)
        assert not rep.all_passed
        assert rep.checks[0].current == 0.0
        print("  readiness empty DB → fail   OK")

        # 2: 49 vs 50 trades
        _seed_resolved(tk, strategy_id="copy_whale", wins=49, losses=0)
        rep49 = validate_calibration_readiness(tk, _CFG)
        assert not rep49.checks[0].passed
        _seed_resolved(tk, strategy_id="copy_whale", wins=1, losses=0)
        rep50 = validate_calibration_readiness(tk, _CFG)
        assert rep50.checks[0].passed
        print("  readiness 49/50 fail, 50/50 pass   OK")

        # 3: whales boundary (fresh DB)
        tk.close()
        tk = _tracker(td + "_w")
        base = 1_700_000_000_000
        _seed_whales_with_trades(tk, 4, 5, base_ts=base)
        rep4 = validate_calibration_readiness(tk, _CFG)
        assert not rep4.checks[1].passed
        _seed_whales_with_trades(tk, 1, 5, base_ts=base + 5 * _MS_PER_DAY, whale_offset=4)
        rep5 = validate_calibration_readiness(tk, _CFG)
        assert rep5.checks[1].passed
        print("  readiness 4/5 whales fail, 5/5 pass   OK")

        # 4: calendar days boundary
        tk.close()
        tk = _tracker(td + "_d")
        span_13 = int(13 * _MS_PER_DAY)
        _seed_resolved(
            tk, strategy_id="copy_whale", wins=50, losses=0,
            base_ts=1_700_000_000_000,
        )
        # Stretch last trade to exactly 13 days span
        tk._conn.execute(
            "UPDATE paper_trades SET decided_at_ts = decided_at_ts + ? "
            "WHERE id = (SELECT MAX(id) FROM paper_trades)",
            (span_13,),
        )
        rep13 = validate_calibration_readiness(tk, _CFG)
        assert not rep13.checks[2].passed, rep13.checks[2].current
        tk._conn.execute(
            "UPDATE paper_trades SET decided_at_ts = decided_at_ts + ? "
            "WHERE id = (SELECT MAX(id) FROM paper_trades)",
            (_MS_PER_DAY,),
        )
        rep14 = validate_calibration_readiness(tk, _CFG)
        assert rep14.checks[2].passed
        print("  readiness 13/14 days fail, 14/14 pass   OK")

        # 7: HALT
        tk.close()
        tk = _tracker(td + "_halt")
        _seed_resolved(tk, strategy_id="copy_whale", wins=28, losses=22)
        _seed_resolved(tk, strategy_id="null_random_side", wins=30, losses=20)
        _seed_resolved(tk, strategy_id="whale_side_random_timing", wins=25, losses=25)
        v = evaluate_decision_matrix(tk, _CFG)
        assert v.label == "HALT", v.summary
        print(f"  HALT verdict: {v.summary[:60]}…   OK")

        # 8: INCONCLUSIVE (small gap and/or high p — PLAN example ~3–6pp at 50n)
        tk.close()
        tk = _tracker(td + "_inc")
        _seed_resolved(tk, strategy_id="copy_whale", wins=28, losses=22)
        _seed_resolved(tk, strategy_id="null_random_side", wins=27, losses=23)
        _seed_resolved(tk, strategy_id="whale_side_random_timing", wins=28, losses=22)
        v = evaluate_decision_matrix(tk, _CFG)
        assert v.label == "INCONCLUSIVE", v.summary
        assert v.copy_vs_null.gap_pp < 5.0, v.copy_vs_null.gap_pp
        assert v.copy_vs_null.p_value > 0.10, v.copy_vs_null.p_value
        print(f"  INCONCLUSIVE: gap={v.copy_vs_null.gap_pp:.1f}pp "
              f"p={v.copy_vs_null.p_value:.3f}   OK")

        # 9: BUILD_B_RELAXED — copy >> null, timing ≈ copy
        tk.close()
        tk = _tracker(td + "_rel")
        _seed_resolved(tk, strategy_id="copy_whale", wins=35, losses=15)
        _seed_resolved(tk, strategy_id="null_random_side", wins=22, losses=28)
        _seed_resolved(tk, strategy_id="whale_side_random_timing", wins=33, losses=17)
        v = evaluate_decision_matrix(tk, _CFG)
        assert v.label == "BUILD_B_RELAXED", v.summary
        assert v.copy_vs_timing.p_value > 0.10
        print(f"  BUILD_B_RELAXED   OK")

        # 10: BUILD_B_FULL
        tk.close()
        tk = _tracker(td + "_full")
        _seed_resolved(tk, strategy_id="copy_whale", wins=38, losses=12)
        _seed_resolved(tk, strategy_id="null_random_side", wins=22, losses=28)
        _seed_resolved(tk, strategy_id="whale_side_random_timing", wins=24, losses=26)
        v = evaluate_decision_matrix(tk, _CFG)
        assert v.label == "BUILD_B_FULL", v.summary
        print(f"  BUILD_B_FULL   OK")

        # 13: tracker delegation
        tk.close()
        tk = _tracker(td + "_del")
        rep_del = tk.get_calibration_readiness(_CFG)
        assert rep_del.summary.startswith("0/50")
        print("  tracker delegates readiness   OK")

        tk.close()

    print("\nmodule 10 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
