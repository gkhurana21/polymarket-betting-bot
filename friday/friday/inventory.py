"""Portfolio-level risk. The limits that span markets.

A MarketMaker is deliberately single-instrument: one mark, one inventory, one
toxicity pair. That is the right shape for quoting and the wrong shape for
RISK, because risk is a property of the whole book. Per-market limits cannot
express "cap my total exposure" — with two markets each allowed 800 shares,
nothing stops 1,600 shares of correlated exposure appearing across them.

This matters more here than in a general venue. Friday's markets are often
politically correlated (several governor races, several primary candidates),
so "unrelated markets" is an assumption the book does not honour: one news
event can move every position at once.

Caps are expressed in the units that actually bind at this size:
  * gross shares      — total |position| across markets
  * gross notional    — USD committed to resting quotes, the real capital cap
  * concurrent markets — how many books we are willing to stand in at once
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from friday.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class PortfolioVerdict:
    allowed: bool
    reason: str = ""


class PortfolioRisk:
    """Stateless gate evaluated before committing capital to a market."""

    def __init__(self, config: dict[str, Any]) -> None:
        pf = config.get("portfolio") or {}
        bankroll = float(config.get("bankroll_usd", 500))
        self._max_gross_shares = float(pf.get("max_gross_shares", 1600))
        # Default to a fraction of bankroll rather than a bare number so the
        # cap tracks the account instead of drifting out of date.
        self._max_gross_usd = float(
            pf.get("max_gross_notional_usd", round(bankroll * 0.90, 2)))
        self._max_markets = int(pf.get("max_concurrent_markets", 2))
        self._bankroll = bankroll

    # -- state ---------------------------------------------------------------

    @staticmethod
    def gross_shares(positions: dict[str, float]) -> float:
        return sum(abs(v) for v in positions.values())

    @staticmethod
    def quote_notional(bid_px: float, bid_sz: float,
                       ask_px: float, ask_sz: float) -> float:
        """USD tied up by a two-sided quote.

        Both legs are BUYs on opposite tokens: the bid costs bid_px per share,
        the ask leg is a NO buy costing (1 - ask_px) per share.
        """
        return bid_sz * bid_px + ask_sz * max(0.0, 1.0 - ask_px)

    # -- the gate ------------------------------------------------------------

    def gate(
        self,
        condition_id: str,
        positions: dict[str, float],
        deployed_usd: float,
        incoming_usd: float,
        active_markets: set[str],
    ) -> PortfolioVerdict:
        """May we commit `incoming_usd` to `condition_id` right now?"""
        if condition_id not in active_markets and len(active_markets) >= self._max_markets:
            return PortfolioVerdict(
                False,
                f"already in {len(active_markets)} markets "
                f"(max {self._max_markets})")

        gs = self.gross_shares(positions)
        if gs >= self._max_gross_shares:
            return PortfolioVerdict(
                False, f"gross {gs:.0f} shares >= max {self._max_gross_shares:.0f}")

        total = deployed_usd + incoming_usd
        if total > self._max_gross_usd:
            return PortfolioVerdict(
                False,
                f"would deploy ${total:.0f} > cap ${self._max_gross_usd:.0f} "
                f"({self._max_gross_usd / self._bankroll:.0%} of bankroll)")

        return PortfolioVerdict(True)

    def snapshot(self, positions: dict[str, float], deployed_usd: float,
                 active_markets: set[str]) -> dict[str, Any]:
        return {
            "gross_shares": self.gross_shares(positions),
            "max_gross_shares": self._max_gross_shares,
            "deployed_usd": round(deployed_usd, 2),
            "max_gross_usd": self._max_gross_usd,
            "markets": len(active_markets),
            "max_markets": self._max_markets,
            "utilisation": round(deployed_usd / self._max_gross_usd, 3)
            if self._max_gross_usd else 0.0,
        }
