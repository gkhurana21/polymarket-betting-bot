"""Phase B risk gate: guards BetIntents before they reach the executor.

Four checks, evaluated in order:
  1. duplicate_market_block  — one live position per market at a time
  2. max_open_positions      — total open orders cap
  3. daily_drawdown_guard    — daily loss ceiling (from live_orders fills)
  4. max_bet_size            — single bet absolute USD cap

All checks are read-only against the DB; no state is held in memory (so a
crash + restart doesn't reset the guard). Thread-safe via DB-level reads.
"""

from __future__ import annotations

import logging
from typing import Any

from bot.models import BetIntent
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)

_MS_PER_DAY = 86_400_000


class RiskVeto(Exception):
    """Raised (not returned) when a BetIntent fails a risk check.

    Attributes:
        reason: short machine-readable code
        detail: human-readable message
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class RiskGate:
    """Stateless risk checks backed by the live DB.

    Call check(intent, tracker) before each order. Raises RiskVeto on
    failure; returns normally on pass.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        risk = config.get("risk") or {}
        self._max_open: int = int(risk.get("max_open_positions", 10))
        self._max_drawdown_pct: float = float(risk.get("max_daily_drawdown_pct", 0.05))
        self._duplicate_block: bool = bool(risk.get("duplicate_market_block", True))
        strat = config.get("strategy") or {}
        self._bankroll_usd: float = float(strat.get("bankroll_usd", 10_000.0))
        self._max_bet_pct: float = float(strat.get("max_bet_pct_bankroll", 0.05))
        self._floor_usd: float = float(risk.get("bankroll_floor_usd", 100.0))

    def check(self, intent: BetIntent, tracker: Tracker) -> None:
        """Run all risk checks. Raises RiskVeto if any check fails."""
        self._check_duplicate_market(intent, tracker)
        self._check_open_positions(tracker)
        self._check_drawdown(tracker)
        self._check_bet_size(intent)

    # ── individual checks ──────────────────────────────────────────────

    def _check_duplicate_market(self, intent: BetIntent, tracker: Tracker) -> None:
        if not self._duplicate_block:
            return
        open_count = tracker.count_open_orders_for_market(intent.market_id)
        if open_count > 0:
            raise RiskVeto(
                "DUPLICATE_MARKET",
                f"market {intent.market_id[:16]}… already has {open_count} open order(s)",
            )

    def _check_open_positions(self, tracker: Tracker) -> None:
        total_open = tracker.count_open_orders()
        if total_open >= self._max_open:
            raise RiskVeto(
                "MAX_OPEN_POSITIONS",
                f"open positions={total_open} >= max={self._max_open}",
            )

    def _check_drawdown(self, tracker: Tracker) -> None:
        since_ms = now_ms() - _MS_PER_DAY
        daily_loss = tracker.daily_realized_loss_usd(since_ms)
        max_loss = self._max_drawdown_pct * self._bankroll_usd
        if daily_loss >= max_loss:
            raise RiskVeto(
                "MAX_DAILY_DRAWDOWN",
                f"daily loss ${daily_loss:.2f} >= ceiling ${max_loss:.2f} "
                f"({self._max_drawdown_pct:.0%} of bankroll ${self._bankroll_usd:.0f})",
            )

    def _check_bet_size(self, intent: BetIntent) -> None:
        max_usd = self._max_bet_pct * self._bankroll_usd
        if intent.size_usd > max_usd:
            raise RiskVeto(
                "MAX_BET_SIZE",
                f"size ${intent.size_usd:.2f} > max ${max_usd:.2f}",
            )
        if intent.size_usd < 1.0:
            raise RiskVeto(
                "MIN_BET_SIZE",
                f"size ${intent.size_usd:.2f} below $1 minimum",
            )
