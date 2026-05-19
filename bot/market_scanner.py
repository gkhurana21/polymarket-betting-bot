"""Market scanner. Implements PLAN.md §2 bot/market_scanner.py.

Pipeline (per scan_once tick, ~10 min in production):

    ClobReadOnlyClient.get_active_markets()       # uses sampling endpoint
        ↓
    _filter_markets()                             # pre-book filters
        ↓                                         # outcome labels, categories,
        ↓                                         # blacklist keywords, volume
    for each survivor:
        get_order_book(yes_token)                 # one HTTP per market
        _build_market_snapshot + _build_book_snapshot
        post-book filters: spread_bps, liquidity_usd, mid is defined
        Tracker.record_market_snapshot + record_book_snapshot
    →
    ScanResult                                    # counts + per-market errors

What this module does NOT own:
  * Generating signals or bet intents (paper_trader's job — module 8)
  * Deciding whether a market is worth acting on (also paper_trader)
  * Computing market_efficiency (deferred to Phase B calibration)

Field-availability findings from live API (see PLAN.md §3 corrections):
  * `get_sampling_markets` returns the dicts we want (currently liquid),
    but does NOT include `volume_24hr` or `category`. We handle gracefully:
      - volume filter: skipped when field is missing/None (treated as pass)
      - category filters: cat=None → "" → blacklist no-op; whitelist still
        drops markets without categories (caller intent: only those listed)
  * `liquidity_usd` is computed from the order book (sum price*size both
    sides). The post-book min_liquidity_usd filter uses this derived value.

Concurrency: scan_once is sequential — one market at a time, one HTTP per
book. Plays cleanly with the circuit breaker and is fast enough for the
10-minute scan interval. Don't parallelise yet.

# TODO(phase-b): consider asyncio.gather for book fetches if scan
# duration exceeds half the scan interval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dataclasses_json import DataClassJsonMixin

from bot.client import CircuitBreakerOpen, ClobReadOnlyClient
from bot.models import BookSnapshot, MarketSnapshot
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class ScanError(DataClassJsonMixin):
    """One failure during a scan. Aggregated into ScanResult.error_details."""
    market_id: str
    error_type: str
    message: str


@dataclass
class ScanResult(DataClassJsonMixin):
    """Per-tick scan summary. Returned by `scan_once`."""
    markets_fetched: int = 0
    markets_passed_filter: int = 0      # passed pre+post book filters AND were written
    market_snapshots_written: int = 0   # rows landed in market_snapshots table
    book_snapshots_written: int = 0     # rows landed in book_snapshots table
    errors: int = 0
    error_details: list[ScanError] = field(default_factory=list)


class MarketScanner:
    """Fetch + filter currently-liquid binary markets, persist snapshots.

    Stateless across ticks except for injected client/tracker. Construct
    once at startup; call scan_once() on each tick.
    """

    def __init__(
        self,
        client: ClobReadOnlyClient,
        tracker: Tracker,
        config: dict[str, Any],
    ) -> None:
        self._client = client
        self._tracker = tracker

        mf = (config.get("market_filters") or {})
        self._min_volume_24h_usd = float(mf.get("min_volume_24h_usd", 0))
        self._min_liquidity_usd = float(mf.get("min_liquidity_usd", 0))
        self._max_spread_bps = float(mf.get("max_spread_bps", float("inf")))
        self._outcome_labels: set[str] = {
            s.lower() for s in mf.get("outcome_labels", ["Yes", "No"])
        }
        self._whitelist_categories = [c.lower() for c in (mf.get("whitelist_categories") or [])]
        self._blacklist_categories = [c.lower() for c in (mf.get("blacklist_categories") or [])]
        self._blacklist_keywords = [k.lower() for k in (mf.get("blacklist_keywords") or [])]
        self._depth_levels = int((config.get("book_snapshot") or {}).get("depth_levels", 10))

    # ────────────────────────────  public  ────────────────────────────

    def scan_once(self, fetch_limit: int = 500) -> ScanResult:
        """Run one full scan tick. Catches per-market failures so a single
        bad market never crashes the loop; halts the rest of the scan if
        the circuit breaker trips during book fetches."""
        result = ScanResult()

        # Phase 1: fetch.
        try:
            raw = self._client.get_active_markets(limit=fetch_limit)
        except CircuitBreakerOpen as e:
            _log.warning("scanner: get_active_markets short-circuited: %s", e)
            result.errors += 1
            result.error_details.append(ScanError("", "CircuitBreakerOpen", str(e)[:200]))
            return result
        except Exception as e:
            _log.error("scanner: get_active_markets failed: %s", e)
            result.errors += 1
            result.error_details.append(ScanError("", type(e).__name__, str(e)[:200]))
            return result
        result.markets_fetched = len(raw)

        # Phase 2: pre-book filtering.
        pre_filtered = self._filter_markets(raw)

        # Phase 3: per-market book fetch + post-book filter + persist.
        ts = now_ms()
        for m in pre_filtered:
            mid_id = m.get("condition_id", "")
            yes_token = self._yes_token(m)
            if yes_token is None:
                _log.debug("scanner: %s has no Yes token; skipping", mid_id)
                continue
            try:
                book = self._client.get_order_book(yes_token)
            except CircuitBreakerOpen as e:
                _log.warning("scanner: CB OPEN at market %s; ending scan early", mid_id)
                result.errors += 1
                result.error_details.append(ScanError(mid_id, "CircuitBreakerOpen", str(e)[:200]))
                break
            except Exception as e:
                _log.warning("scanner: order book fetch for %s failed: %s", mid_id, e)
                result.errors += 1
                result.error_details.append(ScanError(mid_id, type(e).__name__, str(e)[:200]))
                continue

            # Build snapshot — returns None if the book is one-sided.
            ms_snap = self._build_market_snapshot(m, book, ts)
            if ms_snap is None:
                _log.debug("scanner: %s one-sided book; skipping", mid_id)
                continue

            # Post-book filters: spread + liquidity.
            if not self._passes_post_book_filters(book):
                continue

            bs_snap = self._build_book_snapshot(mid_id, book, ts)
            try:
                self._tracker.record_market_snapshot(ms_snap)
                result.market_snapshots_written += 1
                self._tracker.record_book_snapshot(bs_snap)
                result.book_snapshots_written += 1
                result.markets_passed_filter += 1
            except Exception as e:
                _log.error("scanner: persist failed for %s: %s", mid_id, e)
                result.errors += 1
                result.error_details.append(ScanError(mid_id, type(e).__name__, str(e)[:200]))

        return result

    # ──────────────────────────  filtering  ──────────────────────────

    def _filter_markets(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pre-book filtering: outcome labels, category lists, keyword
        blacklist, optional volume floor (only when data present)."""
        out: list[dict[str, Any]] = []
        for m in raw:
            outcomes = {(t.get("outcome") or "").strip().lower() for t in (m.get("tokens") or [])}
            if outcomes != self._outcome_labels:
                continue

            cat = (m.get("category") or "").strip().lower()
            if self._whitelist_categories and cat not in self._whitelist_categories:
                continue
            if cat and cat in self._blacklist_categories:
                continue

            q = (m.get("question") or "").lower()
            if any(kw in q for kw in self._blacklist_keywords):
                continue

            vol = m.get("volume_24h_usd")
            if vol is None:
                vol = m.get("volume_24hr")
            if vol is not None:
                try:
                    if float(vol) < self._min_volume_24h_usd:
                        continue
                except (TypeError, ValueError):
                    pass  # malformed → don't filter on it

            out.append(m)
        return out

    def _passes_post_book_filters(self, book: dict[str, Any]) -> bool:
        """Spread + liquidity filters applied after the book is in hand."""
        spread = self._compute_spread_bps(book)
        if spread is None or spread > self._max_spread_bps:
            return False
        if self._compute_liquidity_usd(book) < self._min_liquidity_usd:
            return False
        return True

    @staticmethod
    def _yes_token(m: dict[str, Any]) -> str | None:
        for t in (m.get("tokens") or []):
            if (t.get("outcome") or "").strip().lower() == "yes":
                return t.get("token_id")
        return None

    # ──────────────────────────  derivations  ──────────────────────────

    @staticmethod
    def _compute_mid(book: dict[str, Any]) -> float | None:
        """(best_bid + best_ask) / 2. None if either side is empty."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = max(float(b[0]) for b in bids)
            best_ask = min(float(a[0]) for a in asks)
        except (IndexError, TypeError, ValueError):
            return None
        return (best_bid + best_ask) / 2.0

    @staticmethod
    def _compute_spread_bps(book: dict[str, Any]) -> float | None:
        """(ask − bid) / mid × 10_000. None if either side is empty or mid≤0."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = max(float(b[0]) for b in bids)
            best_ask = min(float(a[0]) for a in asks)
        except (IndexError, TypeError, ValueError):
            return None
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid * 10_000.0

    @staticmethod
    def _compute_liquidity_usd(book: dict[str, Any]) -> float:
        """Sum of price*size across both sides. Total USD-equivalent depth."""
        total = 0.0
        for side in ("bids", "asks"):
            for level in (book.get(side) or []):
                try:
                    total += float(level[0]) * float(level[1])
                except (IndexError, TypeError, ValueError):
                    continue
        return total

    @staticmethod
    def _compute_implied_prob(book: dict[str, Any], outcome: str) -> float | None:
        """For binary YES/NO markets: P(YES) = mid; P(NO) = 1 − mid."""
        mid = MarketScanner._compute_mid(book)
        if mid is None:
            return None
        o = outcome.strip().lower()
        if o == "yes":
            return mid
        if o == "no":
            return 1.0 - mid
        return None

    def _extract_depth_n(self, book: dict[str, Any]) -> dict[str, list[list[float]]]:
        """Top N levels each side, sorted (bids desc by price, asks asc).
        Defensive: handles books where the API returned levels out of order.
        """
        bids = sorted(
            ([float(b[0]), float(b[1])] for b in (book.get("bids") or [])),
            key=lambda x: -x[0],
        )[: self._depth_levels]
        asks = sorted(
            ([float(a[0]), float(a[1])] for a in (book.get("asks") or [])),
            key=lambda x: x[0],
        )[: self._depth_levels]
        return {"bids": bids, "asks": asks}

    # ─────────────────────────  conversions  ─────────────────────────

    def _build_market_snapshot(
        self, raw: dict[str, Any], book: dict[str, Any], ts: int
    ) -> MarketSnapshot | None:
        """Convert raw + book → MarketSnapshot. Returns None if the book is
        one-sided (mid undefined) — caller skips."""
        mid = self._compute_mid(book)
        if mid is None:
            return None

        spread_bps = self._compute_spread_bps(book)
        liquidity = self._compute_liquidity_usd(book)
        vol_raw = raw.get("volume_24h_usd")
        if vol_raw is None:
            vol_raw = raw.get("volume_24hr")
        try:
            volume = float(vol_raw) if vol_raw is not None else None
        except (TypeError, ValueError):
            volume = None

        return MarketSnapshot(
            market_id=raw.get("condition_id", ""),
            ts=ts,
            mid=mid,
            spread_bps=spread_bps,
            volume_24h_usd=volume,
            liquidity_usd=liquidity if liquidity > 0 else None,
            category=raw.get("category"),
            question=raw.get("question"),
        )

    def _build_book_snapshot(
        self, market_id: str, book: dict[str, Any], ts: int
    ) -> BookSnapshot:
        """Top N each side → JSON string for SQLite storage."""
        depth = self._extract_depth_n(book)
        return BookSnapshot(
            market_id=market_id,
            ts=ts,
            book_json=json.dumps(depth, separators=(",", ":")),
        )
