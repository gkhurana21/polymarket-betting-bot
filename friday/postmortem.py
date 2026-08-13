"""Net-PnL reporting. Never gross.

The predecessor project's central failure was measuring with a broken ruler:
an optimistic fill assumption manufactured a +21.8% edge that became -18.1%
once real prices were used. The defence is to make the honest number the
DEFAULT and the flattering one unavailable by accident.

So `net_pnl` here is:

    realised reward income
  - cost of adverse selection (negative markout on fills)
  - mark-to-market on residual inventory

Gross reward income is reported only as a labelled component, never as the
headline.
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
class Report:
    window_days: float
    reward_realised_usd: float = 0.0
    reward_expected_usd: float = 0.0
    adverse_selection_usd: float = 0.0     # positive number = cost
    inventory_mtm_usd: float = 0.0
    n_fills: int = 0
    n_cycles: int = 0
    n_quoting_cycles: int = 0
    uptime_pct: float = 0.0
    avg_markout: float | None = None
    decline_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def net_pnl_usd(self) -> float:
        """The only number that decides anything."""
        return (self.reward_realised_usd
                - self.adverse_selection_usd
                + self.inventory_mtm_usd)

    def render(self) -> str:
        rw = self.reward_realised_usd or self.reward_expected_usd
        tag = "realised" if self.reward_realised_usd else "expected"
        lines = [
            f"Friday — net PnL over {self.window_days:.1f}d",
            "",
            f"  reward income ({tag})     ${rw:+.2f}",
            f"  adverse selection         ${-self.adverse_selection_usd:+.2f}",
            f"  inventory mark-to-market  ${self.inventory_mtm_usd:+.2f}",
            f"  {'-' * 38}",
            f"  NET                       ${self.net_pnl_usd:+.2f}",
            "",
            f"  fills={self.n_fills}  cycles={self.n_cycles}  "
            f"uptime={self.uptime_pct:.0f}%",
        ]
        if self.avg_markout is not None:
            health = "healthy" if self.avg_markout >= 0 else "QUOTING TOO TIGHT"
            lines.append(f"  avg markout={self.avg_markout:+.5f}  ({health})")
        if self.decline_reasons:
            lines.append("  declined: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self.decline_reasons.items(),
                                              key=lambda kv: -kv[1])))
        if self.reward_realised_usd == 0 and self.reward_expected_usd > 0:
            lines.append("")
            lines.append("  NOTE: reward income is EXPECTED, not realised. Until "
                         "credits are\n        confirmed on-chain this net figure "
                         "is a projection.")
        return "\n".join(lines)


class PostMortem:
    def __init__(self, tracker: Tracker, config: dict[str, Any]) -> None:
        self._tracker = tracker
        self._cfg = config

    def report(self, window_days: float = 5.0) -> Report:
        since = int(time.time() * 1000) - int(window_days * _MS_PER_DAY)
        cycles = self._tracker.cycles_since(since)
        fills = self._tracker.fills_since(since)

        rep = Report(window_days=window_days)
        rep.n_cycles = len(cycles)
        rep.n_quoting_cycles = sum(1 for c in cycles if c["quoting"])
        rep.uptime_pct = (100.0 * rep.n_quoting_cycles / rep.n_cycles) if cycles else 0.0
        rep.n_fills = len(fills)
        rep.reward_realised_usd = self._tracker.realised_reward_since(since)
        rep.reward_expected_usd = self._tracker.expected_reward_since(since)

        for c in cycles:
            if not c["quoting"] and c["reason"]:
                rep.decline_reasons[c["reason"]] = rep.decline_reasons.get(c["reason"], 0) + 1

        marks = [f["markout_at_lag"] for f in fills if f["markout_at_lag"] is not None]
        if marks:
            rep.avg_markout = sum(marks) / len(marks)

        cost = 0.0
        for f in fills:
            m = f["markout_at_lag"]
            if m is None:
                continue
            pnl = m * abs(f["signed_size"])
            if pnl < 0:
                cost += -pnl
        rep.adverse_selection_usd = cost

        rep.inventory_mtm_usd = self._inventory_mtm(fills, cycles)
        return rep

    @staticmethod
    def _inventory_mtm(fills, cycles) -> float:
        """Mark residual position against the latest mid per market."""
        pos: dict[str, float] = {}
        cost: dict[str, float] = {}
        for f in fills:
            cid = f["condition_id"]
            pos[cid] = pos.get(cid, 0.0) + f["signed_size"]
            cost[cid] = cost.get(cid, 0.0) + f["signed_size"] * f["price"]
        last_mid: dict[str, float] = {}
        for c in cycles:
            if c["mid"] is not None:
                last_mid[c["condition_id"]] = c["mid"]
        total = 0.0
        for cid, p in pos.items():
            mid = last_mid.get(cid)
            if mid is None or abs(p) < 1e-9:
                continue
            total += p * mid - cost.get(cid, 0.0)
        return total
