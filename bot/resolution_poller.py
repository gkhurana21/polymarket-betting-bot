"""Resolution poller. Implements PLAN.md §2 bot/resolution_poller.py.

Scope per PLAN.md §15 Q6: this poller ONLY checks markets we have paper
trades in (`Tracker.get_unresolved_paper_trade_market_ids()`) — never the
full universe. Bounded query, sufficient for P&L close-out.

Pipeline (per poll tick, 30 min in production):

    Tracker.get_unresolved_paper_trade_market_ids()
        ↓ (one entry per still-unresolved paper-trade market)
    for each market_id:
        if Tracker.get_resolved_market(market_id) is not None:
            # already in resolved_markets cache; settle stragglers and skip API
            _settle_market(...)
            continue
        client.get_market(market_id)
        outcome = _resolution_outcome(market_dict)        # YES | NO | INVALID | None
        if outcome is None:                                # not yet resolved
            continue
        Tracker.record_resolved_market(market_id, outcome, ts)   # idempotent
        _settle_market(market_id, outcome, ts)                   # writes resolutions
    →
    ResolutionResult                                       # counts + per-market errors

Resolution detection (probed at module-6 build, see PLAN.md §3):
  * `closed: true`  → market is settled
  * Token with `winner: true`  → that token's outcome is the winning side
  * `is_50_50_outcome: true`  → INVALID (Polymarket's 50/50 fallback)
  * Multiple winners or zero winners on a closed market  → INVALID

P&L formula for binary YES/NO bets:
  shares = size_usd / entry_price
  if outcome == side  : pnl = shares * 1.0  − size_usd          # = size_usd × (1/p − 1)
  elif outcome != side: pnl = −size_usd
  elif outcome == INVALID: pnl = shares * 0.50 − size_usd       # 50/50 split

Failure handling:
  * `client.get_market` per-market non-CBO error → log + count + continue
  * `client.get_market` raising `CircuitBreakerOpen` → log + count + break
    the rest of this tick early; next tick retries from scratch
    * Per-trade `resolve_paper_trade` failure → log + count + continue

Whale trust (module 9 — PLAN deviation: hooks into this module):
  After `_settle_market` succeeds, if `whale_perf` is injected, calls
  `whale_perf.update_trust_for_resolved_market(market_id, outcome)`.
  Idempotent per (market_id, whale_signal_id) inside whale_perf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dataclasses_json import DataClassJsonMixin

from bot.client import CircuitBreakerOpen, ClobReadOnlyClient
from bot.tracker import Tracker
from bot.whale_perf import WhalePerf
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class ResolutionError(DataClassJsonMixin):
    """One failure during a poll tick."""
    market_id: str
    error_type: str
    message: str


@dataclass
class ResolutionResult(DataClassJsonMixin):
    """Per-tick poll summary returned by `poll_once`."""
    markets_checked: int = 0
    markets_resolved_this_tick: int = 0
    paper_trades_settled: int = 0
    live_orders_settled: int = 0
    trust_updates_applied: int = 0
    errors: int = 0
    error_details: list[ResolutionError] = field(default_factory=list)


class ResolutionPoller:
    """Discover resolved markets and settle paper trades against them."""

    def __init__(
        self,
        client: ClobReadOnlyClient,
        tracker: Tracker,
        config: dict[str, Any] | None = None,
        whale_perf: WhalePerf | None = None,
    ) -> None:
        # config is reserved for future tuning (e.g. partial-fill PnL adjustments).
        self._client = client
        self._tracker = tracker
        self._config = config or {}
        self._whale_perf = whale_perf

    # ────────────────────────────  public  ────────────────────────────

    def poll_once(self) -> ResolutionResult:
        """One full poll tick. Catches per-market failures so a single bad
        market never crashes the loop; halts the rest of the tick if the
        circuit breaker trips during a get_market call."""
        result = ResolutionResult()
        # Union of markets with unresolved paper trades AND unresolved live
        # orders, so live orders settle on the same tick as paper trades.
        market_ids = list(dict.fromkeys(
            self._tracker.get_unresolved_paper_trade_market_ids()
            + self._tracker.get_unresolved_live_order_market_ids()
        ))
        result.markets_checked = len(market_ids)

        for mid in market_ids:
            # 1. Already-resolved cache check — settle stragglers without re-fetch.
            cached = self._tracker.get_resolved_market(mid)
            if cached is not None:
                outcome, ts = cached
                try:
                    n = self._settle_market(mid, outcome, ts)
                    ln = self._settle_live_orders(mid, outcome, ts)
                    result.trust_updates_applied += self._run_whale_perf(mid, outcome)
                except Exception as e:
                    _log.error("res-poller: cache-path settle %s failed: %s", mid, e)
                    result.errors += 1
                    result.error_details.append(
                        ResolutionError(mid, type(e).__name__, str(e)[:200])
                    )
                    continue
                result.paper_trades_settled += n
                result.live_orders_settled += ln
                continue

            # 2. Live fetch.
            try:
                market = self._client.get_market(mid)
            except CircuitBreakerOpen as e:
                _log.warning("res-poller: CB OPEN at %s; ending tick early", mid)
                result.errors += 1
                result.error_details.append(
                    ResolutionError(mid, "CircuitBreakerOpen", str(e)[:200])
                )
                break
            except Exception as e:
                _log.warning("res-poller: get_market %s failed: %s", mid, e)
                result.errors += 1
                result.error_details.append(
                    ResolutionError(mid, type(e).__name__, str(e)[:200])
                )
                continue

            # 3. Resolution check.
            outcome = self._resolution_outcome(market)
            if outcome is None:
                continue                                # not yet resolved

            # 4. Record + settle. CLOB doesn't expose exact resolution time, so
            # we record the detection ts; PLAN.md §6 resolved_markets accepts
            # this since the column is "when we saw it resolve", not "exact chain ts".
            ts = now_ms()
            try:
                self._tracker.record_resolved_market(mid, outcome, ts)
                n = self._settle_market(mid, outcome, ts)
                ln = self._settle_live_orders(mid, outcome, ts)
                result.trust_updates_applied += self._run_whale_perf(mid, outcome)
            except Exception as e:
                _log.error("res-poller: settle %s (outcome=%s) failed: %s", mid, outcome, e)
                result.errors += 1
                result.error_details.append(
                    ResolutionError(mid, type(e).__name__, str(e)[:200])
                )
                continue
            result.markets_resolved_this_tick += 1
            result.paper_trades_settled += n
            result.live_orders_settled += ln
            _log.info(
                "res-poller: %s resolved=%s settled %d paper trades", mid, outcome, n
            )

        return result

    def _run_whale_perf(self, market_id: str, outcome: str) -> int:
        """Returns count of trust updates applied (0 if whale_perf absent)."""
        if self._whale_perf is None:
            return 0
        try:
            tr = self._whale_perf.update_trust_for_resolved_market(market_id, outcome)
            return tr.updates_applied
        except Exception as e:
            _log.error(
                "res-poller: whale_perf for %s failed (non-fatal): %s", market_id, e
            )
            return 0

    # ──────────────────────────  resolution  ──────────────────────────

    @staticmethod
    def _resolution_outcome(market: dict[str, Any]) -> str | None:
        """Returns 'YES' | 'NO' | 'INVALID' if resolved, None if not.

        Probed at module-6 build:
          * Open market: closed=False, no winners.
          * Resolved binary market: closed=True, exactly one token has
            winner=True, that token's price is 1.0, the loser's price is 0.
          * 50/50 invalid: closed=True, is_50_50_outcome=True.
          * Ambiguous closed states (no winner / multiple winners) → INVALID.
          * Sports markets (outcomes != Yes/No) → INVALID for our purposes.
        """
        if not market.get("closed"):
            return None
        if market.get("is_50_50_outcome"):
            return "INVALID"

        tokens = market.get("tokens") or []
        winners = [t for t in tokens if t.get("winner") is True]
        if len(winners) != 1:
            return "INVALID"
        outcome_label = (winners[0].get("outcome") or "").strip().lower()
        if outcome_label == "yes":
            return "YES"
        if outcome_label == "no":
            return "NO"
        return "INVALID"

    # ────────────────────────────  PnL  ────────────────────────────

    @staticmethod
    def _compute_pnl(
        side: str, entry_price: float, size_usd: float, outcome: str
    ) -> float:
        """Binary YES/NO PnL. side ∈ {YES, NO}; outcome ∈ {YES, NO, INVALID}.

        shares = size_usd / entry_price
        outcome == INVALID → each share pays $0.50 (Polymarket 50/50 split)
        outcome == side    → each share pays $1
        else               → each share pays $0
        """
        if entry_price is None or entry_price <= 0:
            return 0.0
        if size_usd is None or size_usd <= 0:
            return 0.0
        shares = size_usd / entry_price
        if outcome == "INVALID":
            return shares * 0.50 - size_usd
        if outcome == side:
            return shares * 1.0 - size_usd
        return -size_usd

    # ──────────────────────────  settlement  ──────────────────────────

    def _settle_market(self, market_id: str, outcome: str, resolved_at_ts: int) -> int:
        """Settle every unresolved paper_trade in this market. Returns the
        count of successful resolutions. VOIDED_* rows are excluded by the
        `entry_price IS NOT NULL` clause as defense-in-depth (they should
        already have a non-NULL resolved_outcome and thus be excluded by
        `resolved_outcome IS NULL`)."""
        rows = self._tracker._conn.execute(
            "SELECT id, side, entry_price, size_usd FROM paper_trades "
            "WHERE market_id = ? AND resolved_outcome IS NULL "
            "AND entry_price IS NOT NULL",
            (market_id,),
        ).fetchall()

        n = 0
        for r in rows:
            pnl = self._compute_pnl(
                side=r["side"],
                entry_price=float(r["entry_price"]),
                size_usd=float(r["size_usd"]),
                outcome=outcome,
            )
            try:
                self._tracker.resolve_paper_trade(
                    trade_id=int(r["id"]),
                    outcome=outcome,
                    pnl_usd=pnl,
                    resolved_at_ts=resolved_at_ts,
                )
                n += 1
            except Exception as e:
                _log.error(
                    "res-poller: resolve_paper_trade id=%s failed: %s", r["id"], e
                )
        return n

    def _settle_live_orders(self, market_id: str, outcome: str, resolved_at_ts: int) -> int:
        """Settle every unresolved live_order in this market and record realised
        P&L. Mirrors `_settle_market` for the Phase B live (and dry-run) book.

        Entry price is the actual fill_price when filled, else the limit_price
        (the price we committed to) — so dry-run orders, which carry no fill,
        settle against what they would have paid. Returns the count settled."""
        # Only orders that actually filled are trades. 'dry_run_unfilled' /
        # 'dry_run_nobook' rows are recorded for diagnostics but never settled,
        # otherwise we'd book P&L on orders that would never have executed.
        rows = self._tracker._conn.execute(
            "SELECT id, side, COALESCE(fill_price, limit_price) AS entry, size_usd "
            "FROM live_orders WHERE market_id = ? AND resolved_outcome IS NULL "
            "AND status NOT IN ('cancelled', 'error', 'dry_run_unfilled', 'dry_run_nobook')",
            (market_id,),
        ).fetchall()

        n = 0
        for r in rows:
            pnl = self._compute_pnl(
                side=r["side"],
                entry_price=float(r["entry"]),
                size_usd=float(r["size_usd"]),
                outcome=outcome,
            )
            try:
                self._tracker.settle_live_order(
                    order_id=int(r["id"]),
                    outcome=outcome,
                    pnl_usd=pnl,
                    resolved_at_ts=resolved_at_ts,
                )
                n += 1
            except Exception as e:
                _log.error(
                    "res-poller: settle_live_order id=%s failed: %s", r["id"], e
                )
        return n
