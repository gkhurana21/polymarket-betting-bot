"""Calibration readiness + decision matrix. Implements PLAN.md §2
bot/calibration.py and §8 / §11.

Phase A scope:
  * validate_calibration_readiness(tracker, config) → ReadinessReport
  * evaluate_decision_matrix(tracker, config) → DecisionVerdict
  * Parameter-fit helpers stubbed for Phase B (write data/calibration.yaml later)

Statistical tests:
  * Two-proportion z-test when all four cells in the 2×2 table are ≥ 5
  * Fisher's exact test (scipy.stats.fisher_exact) when any cell < 5
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from dataclasses_json import DataClassJsonMixin
from scipy.stats import fisher_exact, norm

from bot.models import ReadinessCheck, ReadinessReport
from bot.tracker import Tracker
from utils.logger import get_logger

_log = get_logger(__name__)

_STRATEGY_COPY = "copy_whale"
_STRATEGY_NULL = "null_random_side"
_STRATEGY_TIMING = "whale_side_random_timing"

_VERDICT_HALT = "HALT"
_VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
_VERDICT_RELAXED = "BUILD_B_RELAXED"
_VERDICT_FULL = "BUILD_B_FULL"


@dataclass(frozen=True)
class StreamStats(DataClassJsonMixin):
    """Win/loss counts for one paper stream."""

    strategy_id: str
    wins: int
    losses: int
    n: int
    win_rate: float


@dataclass(frozen=True)
class PairwiseTest(DataClassJsonMixin):
    """Result of comparing two streams' win rates."""

    label: str
    gap_pp: float
    p_value: float
    method: str  # "z_test" | "fisher_exact"


@dataclass(frozen=True)
class DecisionVerdict(DataClassJsonMixin):
    """Phase A→B decision matrix output per PLAN.md §8."""

    label: str
    copy: StreamStats
    null: StreamStats
    timing: StreamStats
    copy_vs_null: PairwiseTest
    copy_vs_timing: PairwiseTest
    summary: str


# ───────────────────────────  readiness  ───────────────────────────


def validate_calibration_readiness(
    tracker: Tracker,
    config: dict[str, Any] | None = None,
) -> ReadinessReport:
    """Three-check readiness gate per PLAN.md §11."""
    cfg = (config or {}).get("calibration_readiness") or {}
    min_trades = int(cfg.get("min_resolved_trades", 50))
    min_whales = int(cfg.get("min_whales_with_min_trades", 5))
    min_per_whale = int(cfg.get("min_trades_per_whale", 5))
    min_days = float(cfg.get("min_calendar_days", 14))

    resolved_count = _count_resolved_trades(tracker)
    whales_with_min = _count_whales_with_min_trades(tracker, min_per_whale)
    days = _calendar_days_span(tracker)

    checks = [
        ReadinessCheck(
            name="resolved_trades",
            required=float(min_trades),
            current=float(resolved_count),
            passed=resolved_count >= min_trades,
        ),
        ReadinessCheck(
            name="whales_with_5_trades",
            required=float(min_whales),
            current=float(whales_with_min),
            passed=whales_with_min >= min_whales,
        ),
        ReadinessCheck(
            name="calendar_days",
            required=min_days,
            current=days,
            passed=days >= min_days,
        ),
    ]
    all_passed = all(c.passed for c in checks)
    summary = (
        f"{resolved_count}/{min_trades} trades · "
        f"{whales_with_min}/{min_whales} whales ready · "
        f"{int(days)}/{int(min_days)} days"
    )
    return ReadinessReport(checks=checks, all_passed=all_passed, summary=summary)


# ─────────────────────────  decision matrix  ─────────────────────────


def evaluate_decision_matrix(
    tracker: Tracker,
    config: dict[str, Any] | None = None,
) -> DecisionVerdict:
    """Top-to-bottom decision matrix per PLAN.md §8."""
    dm = (config or {}).get("decision_matrix") or {}
    min_gap_pp = float(dm.get("min_winrate_gap_pp", 5.0))
    max_p = float(dm.get("max_p_value", 0.10))

    copy = _stream_stats(tracker, _STRATEGY_COPY)
    null = _stream_stats(tracker, _STRATEGY_NULL)
    timing = _stream_stats(tracker, _STRATEGY_TIMING)

    copy_vs_null = _pairwise_test("copy_vs_null", copy, null)
    copy_vs_timing = _pairwise_test("copy_vs_timing", copy, timing)

    label, summary = _decide_verdict(
        copy, null, timing, copy_vs_null, copy_vs_timing, min_gap_pp, max_p,
    )

    return DecisionVerdict(
        label=label,
        copy=copy,
        null=null,
        timing=timing,
        copy_vs_null=copy_vs_null,
        copy_vs_timing=copy_vs_timing,
        summary=summary,
    )


def fit_calibration_parameters(tracker: Tracker, config: dict[str, Any] | None = None) -> None:
    """Phase B stub — parameter fit writes data/calibration.yaml."""
    raise NotImplementedError(
        "fit_calibration_parameters: Phase B only; gated by readiness + decision matrix"
    )


# ───────────────────────────  DB queries  ───────────────────────────


def _count_resolved_trades(tracker: Tracker) -> int:
    cur = tracker._conn.execute(
        "SELECT COUNT(*) AS n FROM paper_trades "
        "WHERE resolved_outcome IN ('YES', 'NO')"
    )
    return int(cur.fetchone()["n"])


def _count_whales_with_min_trades(tracker: Tracker, min_trades: int) -> int:
    cur = tracker._conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT ws.whale_address
            FROM paper_trades pt
            JOIN whale_signals ws ON pt.triggering_signal_id = ws.id
            WHERE pt.resolved_outcome IN ('YES', 'NO')
            GROUP BY ws.whale_address
            HAVING COUNT(*) >= ?
        )
        """,
        (min_trades,),
    )
    return int(cur.fetchone()["n"])


def _calendar_days_span(tracker: Tracker) -> float:
    cur = tracker._conn.execute(
        """
        SELECT MIN(decided_at_ts) AS lo, MAX(decided_at_ts) AS hi
        FROM paper_trades
        WHERE resolved_outcome IN ('YES', 'NO')
        """
    )
    row = cur.fetchone()
    if row["lo"] is None:
        return 0.0
    return (row["hi"] - row["lo"]) / 1000.0 / 86400.0


def _stream_stats(tracker: Tracker, strategy_id: str) -> StreamStats:
    cur = tracker._conn.execute(
        """
        SELECT
          SUM(CASE WHEN side = resolved_outcome THEN 1 ELSE 0 END) AS wins,
          COUNT(*) AS n
        FROM paper_trades
        WHERE strategy_id = ? AND resolved_outcome IN ('YES', 'NO')
        """,
        (strategy_id,),
    )
    row = cur.fetchone()
    wins = int(row["wins"] or 0)
    n = int(row["n"] or 0)
    losses = n - wins
    win_rate = wins / n if n > 0 else 0.0
    return StreamStats(
        strategy_id=strategy_id,
        wins=wins,
        losses=losses,
        n=n,
        win_rate=win_rate,
    )


# ───────────────────────────  statistics  ───────────────────────────


def _pairwise_test(label: str, a: StreamStats, b: StreamStats) -> PairwiseTest:
    """Compare stream `a` vs `b` (a is test arm, b is control)."""
    gap_pp = (a.win_rate - b.win_rate) * 100.0
    p_value, method = compare_proportions(a.wins, a.n, b.wins, b.n)
    return PairwiseTest(label=label, gap_pp=gap_pp, p_value=p_value, method=method)


def compare_proportions(
    wins_a: int, n_a: int, wins_b: int, n_b: int
) -> tuple[float, str]:
    """Two-sided p-value for difference in proportions.

    Uses Fisher's exact when any 2×2 cell < 5, else two-proportion z-test.
    Returns (p_value, method_name).
    """
    if n_a == 0 or n_b == 0:
        return 1.0, "z_test"

    losses_a = n_a - wins_a
    losses_b = n_b - wins_b
    table = [[wins_a, losses_a], [wins_b, losses_b]]

    if min(wins_a, losses_a, wins_b, losses_b) < 5:
        _, p = fisher_exact(table)
        return float(p), "fisher_exact"

    p_value = _two_proportion_z_pvalue(wins_a, n_a, wins_b, n_b)
    return p_value, "z_test"


def _two_proportion_z_pvalue(wins_a: int, n_a: int, wins_b: int, n_b: int) -> float:
    p_a = wins_a / n_a
    p_b = wins_b / n_b
    p_pool = (wins_a + wins_b) / (n_a + n_b)
    if p_pool <= 0.0 or p_pool >= 1.0:
        return 1.0
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n_a + 1.0 / n_b))
    if se == 0.0:
        return 1.0
    z = (p_a - p_b) / se
    return float(2.0 * (1.0 - norm.cdf(abs(z))))


def _significant(test: PairwiseTest, min_gap_pp: float, max_p: float) -> bool:
    return test.gap_pp >= min_gap_pp and test.p_value <= max_p


def _decide_verdict(
    copy: StreamStats,
    null: StreamStats,
    timing: StreamStats,
    copy_vs_null: PairwiseTest,
    copy_vs_timing: PairwiseTest,
    min_gap_pp: float,
    max_p: float,
) -> tuple[str, str]:
    """Evaluate matrix top-to-bottom; first match wins."""
    w_copy = copy.win_rate
    w_null = null.win_rate

    # Row 1: HALT
    if w_null >= w_copy:
        summary = (
            f"HALT: null win rate {w_null:.1%} >= copy {w_copy:.1%} "
            f"(gap {copy_vs_null.gap_pp:+.1f}pp, p={copy_vs_null.p_value:.3f})"
        )
        return _VERDICT_HALT, summary

    # Row 2: INCONCLUSIVE
    if copy_vs_null.gap_pp < min_gap_pp or copy_vs_null.p_value > max_p:
        summary = (
            f"INCONCLUSIVE: copy−null gap {copy_vs_null.gap_pp:+.1f}pp "
            f"(need ≥{min_gap_pp}), p={copy_vs_null.p_value:.3f} (need ≤{max_p})"
        )
        return _VERDICT_INCONCLUSIVE, summary

    # Rows 3–4 require copy significantly beats null (passed row 2)
    copy_beats_timing = _significant(copy_vs_timing, min_gap_pp, max_p)
    timing_similar_copy = copy_vs_timing.p_value > max_p

    # Row 3: direction matters, timing doesn't
    if timing_similar_copy:
        summary = (
            f"BUILD_B_RELAXED: copy beats null (gap {copy_vs_null.gap_pp:+.1f}pp, "
            f"p={copy_vs_null.p_value:.3f}); timing ≈ copy "
            f"(p={copy_vs_timing.p_value:.3f} > {max_p})"
        )
        return _VERDICT_RELAXED, summary

    # Row 4: direction and timing both matter
    if copy_beats_timing:
        summary = (
            f"BUILD_B_FULL: copy beats null (gap {copy_vs_null.gap_pp:+.1f}pp, "
            f"p={copy_vs_null.p_value:.3f}) and copy beats timing "
            f"(gap {copy_vs_timing.gap_pp:+.1f}pp, p={copy_vs_timing.p_value:.3f})"
        )
        return _VERDICT_FULL, summary

    # Fallback: significant vs null but timing comparison inconclusive
    summary = (
        f"INCONCLUSIVE: copy beats null but copy vs timing unresolved "
        f"(gap {copy_vs_timing.gap_pp:+.1f}pp, p={copy_vs_timing.p_value:.3f})"
    )
    return _VERDICT_INCONCLUSIVE, summary
