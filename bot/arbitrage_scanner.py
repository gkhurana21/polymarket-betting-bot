"""Binary complement arbitrage scanner. OBSERVATION ONLY — never trades.

THE HYPOTHESIS

One YES share and one NO share of the same binary market must together pay
exactly $1.00 at resolution, whichever way it settles. So if equal quantities
of both can be bought for materially less than $1.00 per pair — after fees,
slippage and a safety buffer — the difference is locked in regardless of the
outcome.

WHY THIS MODULE MEASURES RATHER THAN TRADES

The claim above is arithmetic; whether it is ever TRUE at prices you can
actually hit is an empirical question, and the honest answer needs frequency,
capacity and duration, not a single lucky snapshot. This module produces that
evidence and nothing else. It does not import bot.executor, and there is no
code path here that creates, signs, posts or cancels an order.

EXECUTABLE PRICES ONLY

Every calculation walks the ASK side. A midpoint or last-traded price is not a
price you can buy at, and using one is the single easiest way to manufacture an
edge that evaporates on contact. Depth is honoured: the quantity is capped by
the weaker of the two books, and VWAP is the true volume-weighted cost of
filling that quantity, not the best ask repeated.

A structural note worth stating plainly, because it sets expectations: VWAP
over multiple levels is by construction >= the best ask. So if the best asks
already sum to more than $1.00, no deeper fill can rescue it — walking further
into the book only raises the cost. A market that fails at the touch cannot
pass at depth. Expect most observations to be rejections; that IS the finding.

COST MODEL

    gross_payout   = equal_shares * 1.00
    gross_cost     = yes_cost + no_cost                    (both at ask VWAP)
    fees           = gross_cost * fee_bps_per_leg   * 2 / 10_000
    slippage       = gross_cost * slip_bps_per_leg  * 2 / 10_000
    net_locked_pnl = gross_payout - gross_cost - fees - slippage
    net_edge_bps   = net_locked_pnl / gross_cost * 10_000

Fee and slippage rates are CONFIGURED ASSUMPTIONS, not observed values — see
README for what needs validating before any of this is trusted.
"""

from __future__ import annotations

import json
import time
from typing import Any

from bot.client import CircuitBreakerOpen, ClobReadOnlyClient
from bot.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ArbitrageScanError,
    ArbitrageScanResult,
)
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)

# Payout of one YES+NO pair at resolution. Structural, not a tunable.
_PAIR_PAYOUT_USD = 1.0

_REASON_MISSING_TOKEN = "missing_token"
_REASON_ONE_SIDED = "one_sided_book"
_REASON_YES_DEPTH = "insufficient_yes_depth"
_REASON_NO_DEPTH = "insufficient_no_depth"
_REASON_STALE = "stale_book"
_REASON_UNPROFITABLE = "not_profitable"
_REASON_BELOW_THRESHOLD = "edge_below_threshold"


# ─────────────────────────────  book maths  ─────────────────────────────


def normalise_asks(book: dict[str, Any] | None) -> list[tuple[float, float]]:
    """Ask levels as [(price, size)…] sorted cheapest first.

    Malformed levels are dropped rather than raising: a single bad level in
    one market must not abort a scan across hundreds.
    """
    if not book:
        return []
    out: list[tuple[float, float]] = []
    for lvl in (book.get("asks") or []):
        try:
            px, sz = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px > 0 and sz > 0:
            out.append((px, sz))
    out.sort(key=lambda t: t[0])
    return out


def ask_capacity(asks: list[tuple[float, float]]) -> float:
    """Total shares buyable across all ask levels."""
    return sum(sz for _, sz in asks)


def vwap_buy(asks: list[tuple[float, float]], shares: float
             ) -> tuple[float, float, int]:
    """Cost of buying `shares` by walking asks cheapest-first.

    Returns (cost_usd, vwap_price, levels_used). Raises ValueError if the book
    cannot supply the quantity — callers must size against `ask_capacity`
    first, because silently returning a partial fill would overstate capacity,
    which is exactly the error this module exists to avoid.
    """
    if shares <= 0:
        return 0.0, 0.0, 0
    remaining = shares
    cost = 0.0
    levels = 0
    for px, sz in asks:
        if remaining <= 1e-12:
            break
        take = min(sz, remaining)
        cost += take * px
        remaining -= take
        levels += 1
    # Tolerance must scale with size. Summing level sizes accumulates float
    # error proportional to the magnitude, so a fixed 1e-9 fails on large
    # books: a live scan raised "short by 0.000000" on a 3,193,778-share fill
    # where the true shortfall was ~1e-8 — real error, invisible at 6dp, and
    # not a genuine depth problem.
    if remaining > max(1e-9, shares * 1e-12):
        raise ValueError(
            f"book cannot supply {shares} shares (short by {remaining:.3e})")
    return cost, cost / shares, levels


def max_pair_shares(
    yes_asks: list[tuple[float, float]],
    no_asks: list[tuple[float, float]],
    budget_usd: float,
) -> float:
    """Largest equal share count buyable on BOTH books within `budget_usd`.

    Capacity is the weaker book — claiming more would be inventing liquidity.
    Within that ceiling, pair cost rises monotonically with quantity, so a
    bisection converges on the largest affordable size.
    """
    ceiling = min(ask_capacity(yes_asks), ask_capacity(no_asks))
    if ceiling <= 0:
        return 0.0

    def pair_cost(n: float) -> float:
        return vwap_buy(yes_asks, n)[0] + vwap_buy(no_asks, n)[0]

    if pair_cost(ceiling) <= budget_usd:
        return ceiling

    lo, hi = 0.0, ceiling
    for _ in range(60):                     # ~1e-18 relative precision
        mid = (lo + hi) / 2
        if pair_cost(mid) <= budget_usd:
            lo = mid
        else:
            hi = mid
    return lo


# ─────────────────────────────  the scanner  ─────────────────────────────


class ArbitrageScanner:
    """Observation-only complement-pair scanner.

    `evaluate` is a pure function of (market, books, clock) so the whole cost
    model is testable offline with no network.
    """

    def __init__(
        self,
        client: ClobReadOnlyClient,
        tracker: Tracker,
        config: dict[str, Any],
    ) -> None:
        self._client = client
        self._tracker = tracker
        cfg = (config or {}).get("arbitrage_scanner") or {}
        self.enabled = bool(cfg.get("enabled", True))
        self._scan_limit = int(cfg.get("scan_limit", 250))
        self._max_notional = float(cfg.get("max_notional_usd", 25))
        self._min_shares = float(cfg.get("min_shares", 5))
        self._min_edge_bps = float(cfg.get("min_net_edge_bps", 100))
        self._fee_bps = float(cfg.get("estimated_fee_bps_per_leg", 100))
        self._slip_bps = float(cfg.get("slippage_buffer_bps_per_leg", 50))
        self._max_book_age_ms = int(float(cfg.get("max_book_age_seconds", 10)) * 1000)
        self._require_binary = bool(cfg.get("require_binary_yes_no", True))
        self._exclude_closed = bool(cfg.get("exclude_closed_or_resolved", True))
        # Pacing between book fetches. A full pass needs two books per market;
        # firing 500 requests flat out drew 118 rate-limit failures on the
        # first live scan, while a paced probe at ~2.3 req/s took 24 of 24
        # cleanly. Politeness here buys complete data, which is the point.
        self._request_delay = float(cfg.get("request_delay_seconds", 0.12))

    # -- eligibility --------------------------------------------------------

    @staticmethod
    def _token(market: dict[str, Any], outcome: str) -> str | None:
        for t in (market.get("tokens") or []):
            if (t.get("outcome") or "").strip().lower() == outcome:
                tid = t.get("token_id")
                return str(tid) if tid else None
        return None

    def is_eligible(self, market: dict[str, Any]) -> bool:
        """Active, open, order-booked, and genuinely binary YES/NO."""
        if self._exclude_closed:
            if market.get("closed") or market.get("archived"):
                return False
            if not market.get("active", True):
                return False
            if not market.get("accepting_orders", True):
                return False
            if not market.get("enable_order_book", True):
                return False
        if self._require_binary:
            outcomes = {(t.get("outcome") or "").strip().lower()
                        for t in (market.get("tokens") or [])}
            if outcomes != {"yes", "no"}:
                return False
        return True

    # -- evaluation (pure) --------------------------------------------------

    def evaluate(
        self,
        market: dict[str, Any],
        yes_book: dict[str, Any] | None,
        no_book: dict[str, Any] | None,
        *,
        observed_at_ts: int | None = None,
        book_age_ms: int | None = None,
    ) -> ArbitrageOpportunity:
        """Score one market. Always returns a record — rejections included."""
        ts = observed_at_ts if observed_at_ts is not None else now_ms()
        market_id = str(market.get("condition_id") or "")
        question = market.get("question")
        yes_tok = self._token(market, "yes")
        no_tok = self._token(market, "no")

        def record(reason: str | None, yes_leg: ArbitrageLeg, no_leg: ArbitrageLeg,
                   shares: float, cost: float, fees: float, slip: float,
                   pnl: float, bps: float, executable: bool,
                   extra: dict[str, Any] | None = None) -> ArbitrageOpportunity:
            evidence = {
                "market": {
                    "condition_id": market_id,
                    "question": question,
                    "closed": market.get("closed"),
                    "active": market.get("active"),
                    "accepting_orders": market.get("accepting_orders"),
                    "enable_order_book": market.get("enable_order_book"),
                    "neg_risk": market.get("neg_risk"),
                    "end_date_iso": market.get("end_date_iso"),
                    "minimum_tick_size": market.get("minimum_tick_size"),
                    "minimum_order_size": market.get("minimum_order_size"),
                    "tokens": market.get("tokens"),
                },
                "yes_asks": normalise_asks(yes_book)[:20],
                "no_asks": normalise_asks(no_book)[:20],
                "cost_model": {
                    "fee_bps_per_leg": self._fee_bps,
                    "slippage_bps_per_leg": self._slip_bps,
                    "max_notional_usd": self._max_notional,
                    "min_shares": self._min_shares,
                    "min_net_edge_bps": self._min_edge_bps,
                    "max_book_age_ms": self._max_book_age_ms,
                },
                "book_age_ms": book_age_ms,
                # Settlement is ASSUMED conventional ($1 per YES+NO pair).
                # Kept explicit so any candidate can be reviewed by hand before
                # anyone acts on it — see README.
                "assumed_pair_payout_usd": _PAIR_PAYOUT_USD,
            }
            if extra:
                evidence.update(extra)
            return ArbitrageOpportunity(
                market_id=market_id, question=question, observed_at_ts=ts,
                yes_leg=yes_leg, no_leg=no_leg, equal_shares=shares,
                gross_payout_usd=shares * _PAIR_PAYOUT_USD,
                gross_cost_usd=cost, estimated_fees_usd=fees,
                estimated_slippage_usd=slip, net_locked_pnl_usd=pnl,
                net_edge_bps=bps, book_age_ms=book_age_ms,
                executable=executable, reject_reason=reason,
                evidence_json=json.dumps(evidence, separators=(",", ":"),
                                         default=str),
            )

        def empty_leg(tok: str | None, side: str) -> ArbitrageLeg:
            return ArbitrageLeg(token_id=tok or "", side=side, shares=0.0,
                                vwap_price=0.0, cost_usd=0.0, levels_used=0)

        if not yes_tok or not no_tok:
            return record(_REASON_MISSING_TOKEN, empty_leg(yes_tok, "YES"),
                          empty_leg(no_tok, "NO"), 0, 0, 0, 0, 0, 0, False)

        yes_asks = normalise_asks(yes_book)
        no_asks = normalise_asks(no_book)
        if not yes_asks or not no_asks:
            # Nothing offered on one side: we cannot buy it at any price.
            return record(_REASON_ONE_SIDED, empty_leg(yes_tok, "YES"),
                          empty_leg(no_tok, "NO"), 0, 0, 0, 0, 0, 0, False)

        yes_cap, no_cap = ask_capacity(yes_asks), ask_capacity(no_asks)
        if yes_cap < self._min_shares:
            return record(_REASON_YES_DEPTH, empty_leg(yes_tok, "YES"),
                          empty_leg(no_tok, "NO"), 0, 0, 0, 0, 0, 0, False,
                          {"yes_capacity": yes_cap, "no_capacity": no_cap})
        if no_cap < self._min_shares:
            return record(_REASON_NO_DEPTH, empty_leg(yes_tok, "YES"),
                          empty_leg(no_tok, "NO"), 0, 0, 0, 0, 0, 0, False,
                          {"yes_capacity": yes_cap, "no_capacity": no_cap})

        shares = max_pair_shares(yes_asks, no_asks, self._max_notional)
        if shares < self._min_shares:
            # Budget, not the book, is the binding constraint here; report the
            # weaker side so the reason stays actionable.
            reason = _REASON_YES_DEPTH if yes_cap <= no_cap else _REASON_NO_DEPTH
            return record(reason, empty_leg(yes_tok, "YES"),
                          empty_leg(no_tok, "NO"), 0, 0, 0, 0, 0, 0, False,
                          {"yes_capacity": yes_cap, "no_capacity": no_cap,
                           "affordable_shares": shares})

        yes_cost, yes_vwap, yes_levels = vwap_buy(yes_asks, shares)
        no_cost, no_vwap, no_levels = vwap_buy(no_asks, shares)
        yes_leg = ArbitrageLeg(yes_tok, "YES", shares, yes_vwap, yes_cost, yes_levels)
        no_leg = ArbitrageLeg(no_tok, "NO", shares, no_vwap, no_cost, no_levels)

        gross_cost = yes_cost + no_cost
        fees = gross_cost * self._fee_bps * 2 / 10_000
        slippage = gross_cost * self._slip_bps * 2 / 10_000
        payout = shares * _PAIR_PAYOUT_USD
        pnl = payout - gross_cost - fees - slippage
        bps = (pnl / gross_cost * 10_000) if gross_cost > 0 else 0.0

        # Staleness is checked AFTER pricing so the record still carries the
        # numbers — a stale candidate we would have taken is itself evidence.
        if book_age_ms is not None and book_age_ms > self._max_book_age_ms:
            return record(_REASON_STALE, yes_leg, no_leg, shares, gross_cost,
                          fees, slippage, pnl, bps, False)
        if pnl <= 0:
            return record(_REASON_UNPROFITABLE, yes_leg, no_leg, shares,
                          gross_cost, fees, slippage, pnl, bps, False)
        if bps < self._min_edge_bps:
            return record(_REASON_BELOW_THRESHOLD, yes_leg, no_leg, shares,
                          gross_cost, fees, slippage, pnl, bps, False)

        return record(None, yes_leg, no_leg, shares, gross_cost, fees,
                      slippage, pnl, bps, True)

    # -- scan ---------------------------------------------------------------

    def scan_once(self) -> ArbitrageScanResult:
        """One observation pass. Read-only against Polymarket throughout."""
        result = ArbitrageScanResult()
        if not self.enabled:
            _log.info("arb-scan: disabled in config; nothing scanned")
            return result

        try:
            markets = self._client.get_active_markets(limit=self._scan_limit)
        except Exception as e:
            _log.error("arb-scan: market fetch failed: %s", e)
            result.errors += 1
            result.error_details.append(
                ArbitrageScanError("", type(e).__name__, str(e)[:200]))
            return result

        result.markets_considered = len(markets)

        for m in markets:
            mid = str(m.get("condition_id") or "")
            if not self.is_eligible(m):
                continue
            result.markets_eligible += 1

            yes_tok = self._token(m, "yes")
            no_tok = self._token(m, "no")
            yes_book = no_book = None
            fetch_started = now_ms()
            try:
                if yes_tok:
                    yes_book = self._client.get_order_book(yes_tok)
                if self._request_delay:
                    time.sleep(self._request_delay)
                if no_tok:
                    no_book = self._client.get_order_book(no_tok)
                if self._request_delay:
                    time.sleep(self._request_delay)
            except CircuitBreakerOpen as e:
                _log.warning("arb-scan: circuit breaker open at %s; ending scan", mid[:16])
                result.errors += 1
                result.error_details.append(
                    ArbitrageScanError(mid, "CircuitBreakerOpen", str(e)[:200]))
                break
            except Exception as e:
                _log.warning("arb-scan: book fetch failed for %s: %s", mid[:16], e)
                result.errors += 1
                result.error_details.append(
                    ArbitrageScanError(mid, type(e).__name__, str(e)[:200]))
                continue

            # Age is measured from the FIRST book fetch: the real risk in a
            # two-leg trade is that leg one has moved by the time leg two is
            # priced, so the gap between reads is the number that matters.
            observed = now_ms()
            age = observed - fetch_started

            try:
                opp = self.evaluate(m, yes_book, no_book,
                                    observed_at_ts=observed, book_age_ms=age)
            except Exception as e:
                _log.error("arb-scan: evaluate failed for %s: %s", mid[:16], e,
                           exc_info=True)
                result.errors += 1
                result.error_details.append(
                    ArbitrageScanError(mid, type(e).__name__, str(e)[:200]))
                continue

            result.opportunities_found += 1
            if opp.executable:
                result.executable_opportunities += 1
                result.total_executable_notional_usd += opp.gross_cost_usd
                _log.info(
                    "arb-scan: EXECUTABLE %s shares=%.2f cost=$%.2f "
                    "pnl=$%.4f edge=%.0fbps — %s",
                    mid[:16], opp.equal_shares, opp.gross_cost_usd,
                    opp.net_locked_pnl_usd, opp.net_edge_bps,
                    (opp.question or "")[:50])

            try:
                self._tracker.record_arbitrage_opportunity(opp)
            except Exception as e:
                _log.error("arb-scan: persist failed for %s: %s", mid[:16], e)
                result.errors += 1
                result.error_details.append(
                    ArbitrageScanError(mid, type(e).__name__, str(e)[:200]))

        _log.info(
            "arb-scan complete — considered=%d eligible=%d observed=%d "
            "executable=%d notional=$%.2f errors=%d",
            result.markets_considered, result.markets_eligible,
            result.opportunities_found, result.executable_opportunities,
            result.total_executable_notional_usd, result.errors)
        return result
