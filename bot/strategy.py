"""Phase B strategy: proportional copy-whale sizing → BetIntent.

This mirrors the `copy_whale` paper stream — the ONLY model validated in
Phase A (58.9% win rate, BUILD_B_FULL verdict). It does NOT use Kelly on
the whale trust score: an earlier trust-Kelly draft produced max-size bets
on deep-longshot markets (e.g. $500 on a 0.2¢ market) because it treated a
whale's *average* win rate as the per-market win probability. Proportional
sizing has no such failure mode — a whale's $X longshot bet simply becomes
~1% of $X, clamped.

Sizing (matches paper_streams.copy_whale):
    scaled = whale_size_usd × proportional_scale          (default 1%)
    size   = clamp(scaled, [min_bet_usd, max_bet_usd])
             then hard-capped at max_bet_pct_bankroll × bankroll
    side   = mirror the whale's side
    entry  = the whale's own fill price (+ a small limit offset)

The whale trust score is used only to EXCLUDE underperformers
(win_rate < min_whale_winrate, e.g. LEEEROYJENKINS at 41.7%); it never
sizes the bet.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bot.models import BetIntent, WhaleSignal
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)

_STRATEGY_ID = "live_copy_whale"
_EXCLUDED_WHALES: set[str] = set()


def _risk_token_hash(market_id: str, side: str, whale_address: str, whale_fill_ts: int) -> str:
    """Stable dedup key, safe to store in DB."""
    raw = f"{market_id}|{side}|{whale_address}|{whale_fill_ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Strategy:
    """Converts a WhaleSignal + trust data into a BetIntent (or None).

    Constructed once by LiveTrader; stateless between calls.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        strat = config.get("strategy") or {}
        self._scale: float = float(strat.get("proportional_scale", 0.01))
        self._min_bet: float = float(strat.get("min_bet_usd", 5.0))
        self._max_bet: float = float(strat.get("max_bet_usd", 500.0))
        self._max_bet_pct: float = float(strat.get("max_bet_pct_bankroll", 0.05))
        self._min_whale_winrate: float = float(strat.get("min_whale_winrate", 0.50))
        self._bankroll_usd: float = float(strat.get("bankroll_usd", 10_000.0))
        # Optional price band — None disables. Excludes deep longshots /
        # near-certainties if set, e.g. [0.05, 0.95].
        band = strat.get("price_band")
        self._price_band: tuple[float, float] | None = (
            (float(band[0]), float(band[1])) if band else None
        )
        exec_cfg = config.get("execution") or {}
        self._limit_offset_bps: float = float(exec_cfg.get("limit_offset_bps", 5))

    def generate(
        self,
        signal: WhaleSignal,
        trust_row: dict[str, Any] | None,
        token_id: str,
        *,
        signal_id: int,
    ) -> BetIntent | None:
        """Return a proportionally-sized BetIntent, or None to skip."""
        addr = signal.whale_address.lower()

        if addr in _EXCLUDED_WHALES:
            _log.debug("strategy: skip excluded whale %s", addr[:12])
            return None

        # Trust gate: exclude underperforming whales. A missing trust row means
        # the whale has no resolved history yet — skip until we can judge it.
        if trust_row is None:
            _log.debug("strategy: no trust row for %s — skipping", addr[:12])
            return None
        win_rate = float(trust_row.get("trust_mean", 0.0))
        if win_rate < self._min_whale_winrate:
            _log.info(
                "strategy: skip whale %s win_rate=%.3f < min=%.3f",
                addr[:12], win_rate, self._min_whale_winrate,
            )
            return None

        price = signal.price
        if not (0.0 < price < 1.0):
            _log.debug("strategy: bad price %.4f for %s — skip", price, addr[:12])
            return None

        # Optional price band: skip longshots / near-certainties.
        if self._price_band is not None:
            lo, hi = self._price_band
            if not (lo <= price <= hi):
                _log.info(
                    "strategy: price %.4f outside band [%.2f, %.2f] — skip",
                    price, lo, hi,
                )
                return None

        # Proportional sizing — the validated copy_whale model.
        scaled = signal.size_usd * self._scale
        if scaled <= 0:
            return None
        size_usd = max(self._min_bet, min(self._max_bet, scaled))
        # Hard ceiling: never exceed max_bet_pct of bankroll.
        size_usd = min(size_usd, self._max_bet_pct * self._bankroll_usd)
        size_usd = round(size_usd, 2)
        if size_usd < 1.0:
            return None

        # Mirror the whale: enter at their fill price, nudged by limit offset
        # to improve fill probability when we BUY the chosen outcome token.
        offset = price * self._limit_offset_bps / 10_000.0
        limit_price = min(round(price + offset, 4), 0.9999)

        hash_ = _risk_token_hash(signal.market_id, signal.side, addr, signal.whale_fill_ts)

        intent_data = {
            "whale_address": addr,
            "market_id": signal.market_id,
            "token_id": token_id,
            "side": signal.side,
            "strategy_id": _STRATEGY_ID,
            "limit_price": limit_price,
            "size_usd": size_usd,
            "risk_token_hash": hash_,
            "whale_fill_ts": signal.whale_fill_ts,
            "triggering_signal_id": signal_id,
            "win_rate": win_rate,
            "whale_size_usd": signal.size_usd,
            "proportional_scale": self._scale,
            "bankroll_usd": self._bankroll_usd,
        }

        return BetIntent(
            whale_address=addr,
            market_id=signal.market_id,
            token_id=token_id,
            side=signal.side,
            strategy_id=_STRATEGY_ID,
            limit_price=limit_price,
            size_usd=size_usd,
            risk_token_hash=hash_,
            whale_fill_ts=signal.whale_fill_ts,
            intent_json=json.dumps(intent_data),
            triggering_signal_id=signal_id,
            created_at_ts=now_ms(),
        )


def exclude_whale(address: str) -> None:
    """Runtime exclusion — add a whale address to the skip list."""
    _EXCLUDED_WHALES.add(address.lower())
    _log.info("strategy: whale %s added to exclusion list", address[:12])
