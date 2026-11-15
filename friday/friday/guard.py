"""Kill switches and halts.

The economics here are thin and asymmetric: at $500 the gross reward income is
a couple of dollars a day, while one adverse fill of 5c on 200 shares costs
$10 — several days of earnings. Net profitability is therefore decided by
these controls, not by reward capture. They are not a safety wrapper around
the strategy; they ARE the strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from friday.events import BUS, GUARD, TRACKER
from friday.logger import get_logger
from friday.tracker import Tracker

_log = get_logger(__name__)

_MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""


class Guard:
    """Portfolio-level halts, evaluated before any quoting."""

    def __init__(self, config: dict[str, Any]) -> None:
        saf = config.get("safety") or {}
        g = config.get("guard") or {}
        self._kill_loss = float(saf.get("kill_loss_usd", 40.0))
        self._window_days = int(saf.get("reward_vs_inventory_window_days", 5))
        self._news_halt = bool(g.get("news_halt_enabled", False))
        self._halted = False
        self._halt_reason = ""

    def latch(self, reason: str) -> None:
        if not self._halted:
            _log.error("GUARD HALT: %s", reason)
            BUS.emit(GUARD, "broadcast", "halt", reason=reason)
        BUS.node_state(GUARD, halted=True, reason=reason)
        self._halted = True
        self._halt_reason = reason

    @property
    def halted(self) -> bool:
        return self._halted

    def check(self, tracker: Tracker, realised_pnl_usd: float) -> GuardVerdict:
        """Evaluate every halt condition. Latching is permanent for the
        process — a halt means a human looks at it, not that we wait for the
        number to drift back."""
        if self._halted:
            return GuardVerdict(False, self._halt_reason)

        if realised_pnl_usd <= -self._kill_loss:
            self.latch(f"realised loss ${-realised_pnl_usd:.2f} >= kill ${self._kill_loss:.2f}")
            return GuardVerdict(False, self._halt_reason)

        BUS.emit(TRACKER, GUARD, "check", pnl=round(realised_pnl_usd, 2))
        BUS.node_state(GUARD, halted=False, pnl=round(realised_pnl_usd, 2))
        v = self.check_reward_vs_inventory(tracker)
        if not v.allowed:
            self.latch(v.reason)
        return v

    def check_reward_vs_inventory(self, tracker: Tracker) -> GuardVerdict:
        """The operating rule: if inventory-driven losses exceed reward
        earnings over the rolling window, stop and re-evaluate.

        This compares NET against GROSS deliberately. Rewards are the only
        thing paying us; inventory is the only thing that can take it away. If
        the second is bigger than the first, the business does not work, and
        no amount of further running changes that.
        """
        since = int(time.time() * 1000) - self._window_days * _MS_PER_DAY
        rewards = tracker.realised_reward_since(since)
        if rewards <= 0:
            rewards = tracker.expected_reward_since(since)
        inv_loss = _inventory_loss_usd(tracker, since)
        if inv_loss > rewards and inv_loss > 0:
            return GuardVerdict(
                False,
                f"{self._window_days}d inventory loss ${inv_loss:.2f} exceeds "
                f"reward earnings ${rewards:.2f}",
            )
        return GuardVerdict(True)


def _inventory_loss_usd(tracker: Tracker, since_ms: int) -> float:
    """Loss attributable to being filled, measured by markout.

    A fill's markout is where fair value went afterwards, signed by our
    direction. Summing the negative markouts over the window gives the cost of
    being picked off, in dollars, which is exactly the quantity that has to
    stay below reward income.
    """
    loss = 0.0
    for f in tracker.fills_since(since_ms):
        m = f["markout_at_lag"]
        if m is None:
            continue
        pnl = m * abs(f["signed_size"])
        if pnl < 0:
            loss += -pnl
    return loss
