"""Whale performance / trust scoring. Implements PLAN.md §2 bot/whale_perf.py.

Post-resolution Bayesian Beta(α, β) updates on per-whale trust. Only resolved
`copy_whale` paper trades with `resolved_outcome IN ('YES', 'NO')` contribute.

Hook: called by `ResolutionPoller` after `_settle_market` succeeds via
`update_trust_for_resolved_market(market_id, outcome)`.

Idempotency: one update per `(market_id, whale_signal_id)` via
`processed_trust_updates` — safe on cache-path re-polls and restarts.

Phase A blacklist: `auto_blacklist_check()` flags `candidate_blacklist` and
logs WARNING; does NOT remove whales from the polling universe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dataclasses_json import DataClassJsonMixin

from bot.tracker import Tracker
from utils.helpers import clamp
from utils.logger import get_logger

_log = get_logger(__name__)


@dataclass
class TrustUpdateResult(DataClassJsonMixin):
    """Summary from one `update_trust_for_resolved_market` call."""

    market_id: str
    updates_applied: int = 0
    updates_skipped_idempotent: int = 0
    updates_skipped_invalid_outcome: int = 0
    below_threshold_wallets: list[str] = field(default_factory=list)


class WhalePerf:
    """Bayesian Beta trust updates and Phase A candidate-blacklist flags."""

    def __init__(self, tracker: Tracker, config: dict[str, Any] | None = None) -> None:
        self._tracker = tracker
        cfg = (config or {}).get("whale_perf") or {}
        self._prior_alpha = float(cfg.get("prior_alpha", 7))
        self._prior_beta = float(cfg.get("prior_beta", 3))
        self._manual_total_weight = float(cfg.get("manual_override_total_weight", 10))
        self._trust_min = float(cfg.get("trust_mean_min", 0.0))
        self._trust_max = float(cfg.get("trust_mean_max", 0.95))
        self._blacklist_threshold = float(cfg.get("blacklist_threshold", 0.35))
        self._attribution_strategy = str(
            cfg.get("attribution_strategy_id", "copy_whale")
        )

    # ───────────────────────────  public API  ───────────────────────────

    def update_trust_for_resolved_market(
        self, market_id: str, outcome: str
    ) -> TrustUpdateResult:
        """Apply Beta updates for all attributable copy_whale rows on this market.

        `outcome` is the market resolution (YES | NO | INVALID). Only YES/NO
        rows update trust; INVALID skips attribution for that market tick.
        """
        result = TrustUpdateResult(market_id=market_id)

        if outcome not in ("YES", "NO"):
            rows = self._tracker.get_copy_whale_attribution_rows(
                market_id, self._attribution_strategy
            )
            result.updates_skipped_invalid_outcome = len(rows)
            _log.debug(
                "whale_perf: market %s outcome=%s — no trust updates",
                market_id, outcome,
            )
            result.below_threshold_wallets = self.auto_blacklist_check()
            return result

        rows = self._tracker.get_copy_whale_attribution_rows(
            market_id, self._attribution_strategy
        )
        for row in rows:
            signal_id = int(row["triggering_signal_id"])
            if not self._tracker.try_record_processed_trust_update(
                market_id, signal_id
            ):
                result.updates_skipped_idempotent += 1
                continue

            wallet = row["whale_address"]
            side = row["side"]
            resolved = row["resolved_outcome"]
            won = resolved == side

            alpha, beta = self._tracker.get_whale_trust(
                wallet,
                prior_alpha=self._prior_alpha,
                prior_beta=self._prior_beta,
            )
            if won:
                alpha += 1.0
            else:
                beta += 1.0

            trust_mean = self._clamp_trust_mean(alpha, beta)
            self._tracker.update_whale_trust(
                wallet, alpha, beta, trust_mean
            )
            result.updates_applied += 1
            _log.info(
                "whale_perf: trust update %s signal=%d won=%s → α=%.1f β=%.1f mean=%.3f",
                wallet[:10], signal_id, won, alpha, beta, trust_mean,
            )

        result.below_threshold_wallets = self.auto_blacklist_check()
        return result

    def auto_blacklist_check(self) -> list[str]:
        """Phase A: flag + log only. Returns wallets below blacklist_threshold."""
        below = self._tracker.refresh_candidate_blacklist_flags(
            self._blacklist_threshold
        )
        for wallet in below:
            row = self._tracker.get_whale_trust_row(wallet)
            mean = row["trust_mean"] if row else 0.0
            _log.warning(
                "whale_perf: candidate_blacklist %s trust_mean=%.3f < %.3f "
                "(Phase A: still polled — review at A→B)",
                wallet[:10], mean, self._blacklist_threshold,
            )
        return below

    def seed_manual_trust(self, wallet: str, trust: float) -> tuple[float, float, float]:
        """Seed Beta prior for `add-whale --trust`. α+β = manual_override_total_weight."""
        trust = clamp(trust, 0.0, 1.0)
        total = self._manual_total_weight
        alpha = round(trust * total, 6)
        beta = round((1.0 - trust) * total, 6)
        trust_mean = self._clamp_trust_mean(alpha, beta)
        self._tracker.update_whale_trust(
            wallet, alpha, beta, trust_mean, candidate_blacklist=False
        )
        return alpha, beta, trust_mean

    # ───────────────────────────  internals  ───────────────────────────

    def _clamp_trust_mean(self, alpha: float, beta: float) -> float:
        denom = alpha + beta
        raw = alpha / denom if denom > 0 else 0.0
        return clamp(raw, self._trust_min, self._trust_max)

    @staticmethod
    def beta_update(
        alpha: float, beta: float, won: bool
    ) -> tuple[float, float]:
        """Pure Beta conjugate step (for tests)."""
        if won:
            return alpha + 1.0, beta
        return alpha, beta + 1.0
