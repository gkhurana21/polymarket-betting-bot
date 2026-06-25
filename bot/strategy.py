"""Phase B strategy: per-whale Kelly sizing → BetIntent.

Reads calibrated win-rates from the whale_trust table (Beta posterior mean).
Applies ¼-Kelly sizing against the current mid-price. Returns None for
excluded whales (win_rate < min_whale_winrate) or when Kelly is non-positive.

Kelly formula for binary-outcome prediction market at price p:
  b = (1 / p) - 1          (decimal odds on a YES bet priced at p)
  f* = (b*win_rate - (1 - win_rate)) / b  = (win_rate - p) / (p * (1 - p))  [simplified]
  f_adjusted = kelly_fraction * f*         (¼-Kelly by default)
  size_usd = f_adjusted * bankroll_usd

Edge check: f* > 0  ⟺  win_rate > p  (we have positive edge vs market price).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
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


def _kelly_size(win_rate: float, price: float, kelly_fraction: float, bankroll_usd: float) -> float | None:
    """Quarter-Kelly position size in USD. Returns None if no edge or price invalid."""
    if not (0.0 < price < 1.0):
        return None
    b = (1.0 / price) - 1.0
    f_star = (b * win_rate - (1.0 - win_rate)) / b
    if f_star <= 0.0:
        return None
    return kelly_fraction * f_star * bankroll_usd


class Strategy:
    """Converts a WhaleSignal + trust data into a BetIntent (or None).

    Constructed once by LiveTrader; stateless between calls.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        strat = config.get("strategy") or {}
        self._kelly_fraction: float = float(strat.get("kelly_fraction", 0.25))
        self._min_edge_pct: float = float(strat.get("min_edge_pct", 0.03))
        self._max_bet_pct: float = float(strat.get("max_bet_pct_bankroll", 0.05))
        self._min_whale_winrate: float = float(strat.get("min_whale_winrate", 0.50))
        self._bankroll_usd: float = float(strat.get("bankroll_usd", 10_000.0))
        risk = config.get("risk") or {}
        self._floor_usd: float = float(risk.get("bankroll_floor_usd", 100.0))
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
        """Return a sized BetIntent, or None if the signal should be skipped."""
        addr = signal.whale_address.lower()

        if addr in _EXCLUDED_WHALES:
            _log.debug("strategy: skip excluded whale %s", addr[:12])
            return None

        # Pull win rate from posterior mean.
        if trust_row is None:
            _log.debug("strategy: no trust row for %s — skipping", addr[:12])
            return None

        win_rate: float = float(trust_row.get("trust_mean", 0.0))
        if win_rate < self._min_whale_winrate:
            _log.info(
                "strategy: skip whale %s win_rate=%.3f < min=%.3f",
                addr[:12], win_rate, self._min_whale_winrate,
            )
            return None

        price = signal.price
        size_usd = _kelly_size(win_rate, price, self._kelly_fraction, self._bankroll_usd)
        if size_usd is None:
            _log.info(
                "strategy: no edge whale=%s price=%.3f win_rate=%.3f",
                addr[:12], price, win_rate,
            )
            return None

        # Edge check: require meaningful edge beyond Kelly saying "bet".
        edge_pct = win_rate - price
        if edge_pct < self._min_edge_pct:
            _log.info(
                "strategy: edge %.3f < min %.3f — skip",
                edge_pct, self._min_edge_pct,
            )
            return None

        # Cap at max_bet_pct_bankroll.
        max_usd = self._max_bet_pct * self._bankroll_usd
        size_usd = min(size_usd, max_usd)

        # Add limit_offset_bps to skew limit price slightly towards fill.
        offset = price * self._limit_offset_bps / 10_000.0
        # Buying YES: price up slightly to improve fill probability.
        limit_price = min(round(price + offset, 4), 0.9999)

        hash_ = _risk_token_hash(signal.market_id, signal.side, addr, signal.whale_fill_ts)

        intent_data = {
            "whale_address": addr,
            "market_id": signal.market_id,
            "token_id": token_id,
            "side": signal.side,
            "strategy_id": _STRATEGY_ID,
            "limit_price": limit_price,
            "size_usd": round(size_usd, 2),
            "risk_token_hash": hash_,
            "whale_fill_ts": signal.whale_fill_ts,
            "triggering_signal_id": signal_id,
            "win_rate": win_rate,
            "edge_pct": edge_pct,
            "kelly_fraction": self._kelly_fraction,
            "bankroll_usd": self._bankroll_usd,
        }

        return BetIntent(
            whale_address=addr,
            market_id=signal.market_id,
            token_id=token_id,
            side=signal.side,
            strategy_id=_STRATEGY_ID,
            limit_price=limit_price,
            size_usd=round(size_usd, 2),
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
