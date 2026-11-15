"""Reward economics: expected accrual, realised credits, and the gap between.

WHY THIS MODULE EXISTS

The whole scale-up decision rests on one comparison: does the reward income we
PROJECT actually arrive? Until this was written, `reward_credits` existed as a
table that nothing ever wrote to, so every report showed expected income with
realised silently zero. That is the same failure the predecessor project died
of — a number that flatters because the honest half was never collected.

Expected accrual is computed from our own logged cycles and is therefore
always available. Realised income is not: Polymarket credits liquidity rewards
on its own schedule, and reading them back requires an authenticated endpoint
Friday cannot reach in dry-run with no key. Rather than invent a number, this
module keeps the two strictly separate and makes the reconciliation gap
explicit — including "we have no realised data at all", which is a finding
about our measurement, not about the business.

Realised credits can be imported once available:
    main.py rewards --import <condition_id> <usd> <YYYY-MM-DD>
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from friday.logger import get_logger
from friday.tracker import Tracker

_log = get_logger(__name__)

_MS_PER_DAY = 86_400_000


@dataclass
class Reconciliation:
    window_days: float
    expected_usd: float = 0.0
    realised_usd: float = 0.0
    days_with_credits: int = 0
    per_market: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def has_realised(self) -> bool:
        return self.days_with_credits > 0

    @property
    def capture_ratio(self) -> float | None:
        """Realised / expected. None while no realised data exists.

        Returning None rather than 0.0 is deliberate: "we captured nothing" and
        "we have not measured capture" are different claims, and conflating
        them is how a projection quietly becomes a result.
        """
        if not self.has_realised or self.expected_usd <= 0:
            return None
        return self.realised_usd / self.expected_usd

    def render(self) -> str:
        lines = [f"Reward reconciliation — {self.window_days:.1f}d",
                 "",
                 f"  expected (from our cycles)   ${self.expected_usd:.2f}",
                 f"  realised (credited)          ${self.realised_usd:.2f}"]
        cr = self.capture_ratio
        if cr is None:
            lines += ["",
                      "  NO REALISED DATA. Expected accrual is a projection from our own",
                      "  resting size and the advertised pool — it has never been checked",
                      "  against an actual credit. Do not treat it as income.",
                      "  Import credits with:  main.py rewards --import <cid> <usd> <day>"]
        else:
            lines.append(f"  capture ratio                {cr:.0%}")
            if cr < 0.7:
                lines += ["", "  Capture is well below projection. Either our resting size is not",
                          "  qualifying (check min_size and the reward band) or the pool is",
                          "  split more widely than the book depth implied."]
        return "\n".join(lines)


class Rewards:
    def __init__(self, tracker: Tracker) -> None:
        self._t = tracker

    def expected_since(self, since_ms: int) -> float:
        return self._t.expected_reward_since(since_ms)

    def record_credit(self, condition_id: str, usd: float, day: str) -> None:
        """Record a realised reward credit for one market on one UTC day."""
        self._t.record_reward(condition_id, usd, day)
        _log.info("recorded realised reward $%.2f for %s on %s",
                  usd, condition_id[:14], day)

    def reconcile(self, window_days: float = 7.0) -> Reconciliation:
        since = int(time.time() * 1000) - int(window_days * _MS_PER_DAY)
        rec = Reconciliation(window_days=window_days)
        rec.expected_usd = self.expected_since(since)
        rec.realised_usd = self._t.realised_reward_since(since)
        rows = self._t._conn.execute(
            "SELECT condition_id, COUNT(DISTINCT day) d, COALESCE(SUM(usd),0) s "
            "FROM reward_credits WHERE ts >= ? GROUP BY condition_id", (since,)
        ).fetchall()
        rec.days_with_credits = sum(int(r["d"]) for r in rows)
        for r in rows:
            rec.per_market[r["condition_id"]] = {"realised": float(r["s"]),
                                                 "days": int(r["d"])}
        return rec
