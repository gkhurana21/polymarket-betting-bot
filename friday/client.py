"""Read-only Polymarket CLOB wrapper. Implements PLAN.md §2 bot/client.py
and the auth/endpoint table in PLAN.md §3.

Phase A scope: every method here is read-only. The wrapped py_clob_client
ClobClient is instantiated with `key=None` so no signing-capable code path
exists. Methods that would place / cancel / post orders raise
NotImplementedError with a pointer to bot/executor.py (Phase B).

Phase B will subclass this with a signing-capable variant that overrides
the NotImplementedError stubs and adds risk-token-gated order placement.

Resilience:
  * Retry decorator (`_retried`) — exponential backoff. Retries only on
    requests.exceptions.ConnectionError, requests.exceptions.Timeout,
    requests.exceptions.HTTPError 5xx, and PolyApiException with
    status_code in [500, 600). NEVER retries 4xx — those are bugs in our
    request, not theirs.
  * Circuit breaker (`_CircuitBreaker`) — CLOSED → OPEN → HALF_OPEN state
    machine. After N consecutive logical failures (where one logical
    failure = one exhausted retry chain) the breaker opens; subsequent
    calls raise CircuitBreakerOpen immediately without touching the
    network. After `reset_seconds`, one trial call is allowed
    (HALF_OPEN); success closes it, failure re-opens.
  * Both retry and circuit-breaker thresholds are config-driven via the
    `reconnect:` block in config.yaml.

Endpoints (corrected from PLAN.md v3 at module-4 build, confirmed by probe):
  * py_clob_client (server-time / markets / market / order_book) — official SDK
  * data-api.polymarket.com/trades — per-wallet fills (public)
  * lb-api.polymarket.com/profit — leaderboard (public)
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.exceptions import PolyApiException

from friday.logger import get_logger

_log = get_logger(__name__)


# Default endpoints — can be overridden via constructor for testing.
_DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
_DEFAULT_DATA_API = "https://data-api.polymarket.com"
_DEFAULT_LB_API = "https://lb-api.polymarket.com"


class CircuitBreakerOpen(Exception):
    """Raised when a request is short-circuited because the breaker is OPEN."""


class _CircuitBreaker:
    """Three-state breaker. Thread-safe."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, fail_threshold: int, reset_seconds: float) -> None:
        self._fail_threshold = fail_threshold
        self._reset_seconds = reset_seconds
        self._fail_count = 0
        self._opened_at_ms = 0
        self._state = self.CLOSED
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._fail_count

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000

    def call(self, fn, *args, **kwargs):
        # Pre-call: gate on state.
        with self._lock:
            if self._state == self.OPEN:
                if self._now_ms() - self._opened_at_ms >= self._reset_seconds * 1000:
                    self._state = self.HALF_OPEN
                    _log.info("circuit breaker → HALF_OPEN (probing)")
                else:
                    raise CircuitBreakerOpen(
                        f"circuit breaker OPEN: {self._fail_count} consecutive "
                        f"failures; will probe in "
                        f"{(self._reset_seconds * 1000 - (self._now_ms() - self._opened_at_ms)) / 1000:.1f}s"
                    )

        # Call without holding the lock so concurrent reads aren't blocked.
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            # Only retriable (network / 5xx) errors count toward the breaker.
            # A 4xx means the API is alive and our request is malformed —
            # propagate it but treat it as a "successful round-trip" from the
            # breaker's perspective (it proves the API is responsive).
            if not _is_retriable(exc):
                with self._lock:
                    if self._state != self.CLOSED:
                        _log.info("circuit breaker → CLOSED (4xx-class response: API is up)")
                    self._state = self.CLOSED
                    self._fail_count = 0
                raise
            with self._lock:
                self._fail_count += 1
                if self._state == self.HALF_OPEN:
                    self._state = self.OPEN
                    self._opened_at_ms = self._now_ms()
                    _log.warning("circuit breaker HALF_OPEN trial failed → OPEN")
                elif self._fail_count >= self._fail_threshold:
                    self._state = self.OPEN
                    self._opened_at_ms = self._now_ms()
                    _log.warning(
                        "circuit breaker → OPEN after %d consecutive failures",
                        self._fail_count,
                    )
            raise

        # Success — reset.
        with self._lock:
            if self._state != self.CLOSED:
                _log.info("circuit breaker → CLOSED")
            self._state = self.CLOSED
            self._fail_count = 0
        return result


def _is_retriable(exc: BaseException) -> bool:
    """True for transient/network errors. Never True for 4xx — those are
    our request bugs and must not be retried."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        sc = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        return 500 <= sc < 600
    if isinstance(exc, PolyApiException):
        sc = getattr(exc, "status_code", 0) or 0
        return 500 <= sc < 600
    return False


class ClobReadOnlyClient:
    """Phase A read-only Polymarket CLOB wrapper.

    Construct with `key=None` (enforced internally). Every call passes
    through the circuit breaker + retry chain. Read-only by construction:
    signing-capable methods raise NotImplementedError.

    Phase B will subclass this with a signing-capable variant; do not
    weaken the read-only stubs here.
    """

    def __init__(
        self,
        *,
        clob_host: str = _DEFAULT_CLOB_HOST,
        data_api_host: str = _DEFAULT_DATA_API,
        lb_api_host: str = _DEFAULT_LB_API,
        chain_id: int = POLYGON,
        # retry config
        retry_max_attempts: int = 5,
        retry_initial_backoff_seconds: float = 1.0,
        retry_max_backoff_seconds: float = 60.0,
        retry_multiplier: float = 2.0,
        # circuit-breaker config
        cb_fail_threshold: int = 5,
        cb_reset_seconds: float = 300.0,
        # injectables for testing
        http_session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self._clob_host = clob_host
        self._data_api_host = data_api_host
        self._lb_api_host = lb_api_host
        self._sdk = ClobClient(host=clob_host, chain_id=chain_id, key=None)
        # Sanity-check that the SDK is in read-only mode (no signer attached).
        # If this fails, py-clob-client behaviour changed; fail loud.
        addr = None
        try:
            addr = self._sdk.get_address()
        except Exception:
            addr = None
        if addr is not None:
            raise RuntimeError(
                f"ClobReadOnlyClient: SDK was constructed with a signer (address={addr!r}); "
                f"Phase A must be key=None"
            )

        self._retry_max = retry_max_attempts
        self._retry_initial = retry_initial_backoff_seconds
        self._retry_max_backoff = retry_max_backoff_seconds
        self._retry_multiplier = retry_multiplier
        self._cb = _CircuitBreaker(cb_fail_threshold, cb_reset_seconds)
        self._session = http_session if http_session is not None else requests.Session()
        self._sleep = sleep_fn

    # ───────────────────────  retry + breaker chain  ───────────────────────

    def _retried(self, fn, *args, **kwargs):
        attempt = 0
        backoff = self._retry_initial
        while True:
            attempt += 1
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if not _is_retriable(exc) or attempt >= self._retry_max:
                    raise
                _log.warning(
                    "retriable error in %s (attempt %d/%d): %s",
                    getattr(fn, "__name__", "fn"), attempt, self._retry_max, exc,
                )
                self._sleep(backoff)
                backoff = min(backoff * self._retry_multiplier, self._retry_max_backoff)

    def _call(self, fn, *args, **kwargs):
        """Every public read method routes through here."""
        return self._cb.call(self._retried, fn, *args, **kwargs)

    # ──────────────────────────  health  ──────────────────────────

    def health_check(self) -> bool:
        """Cheap aliveness probe via get_server_time. Bot startup must
        refuse to proceed if this returns False."""
        try:
            t = self._call(self._sdk.get_server_time)
            _log.info("CLOB health_check: server_time=%s OK", t)
            return True
        except Exception as e:
            _log.error("CLOB health_check FAILED: %s", e)
            return False

    # ──────────────────────────  markets  ──────────────────────────

    def get_active_markets(self, limit: int = 500) -> list[dict[str, Any]]:
        """Currently-liquid markets via the CLOB `sampling-markets` endpoint.

        Filters: active && !closed && !archived && accepting_orders &&
        enable_order_book. The plain `get_markets` endpoint paginates from
        the oldest first and returns many markets whose books have been
        torn down — sampling-markets returns only those currently sampled
        for liquidity rewards, which are guaranteed to have populated books.
        Scanner converts to MarketSnapshot per PLAN.md §6.
        """
        out: list[dict[str, Any]] = []
        page = self._call(self._sdk.get_sampling_markets)
        data = page.get("data", []) if isinstance(page, dict) else []
        for m in data:
            if (
                m.get("active")
                and not m.get("closed")
                and not m.get("archived")
                and m.get("accepting_orders")
                and m.get("enable_order_book")
            ):
                out.append(m)
                if len(out) >= limit:
                    break
        return out

    def get_market(self, condition_id: str) -> dict[str, Any]:
        """Single market lookup by condition_id. Used by ResolutionPoller."""
        return self._call(self._sdk.get_market, condition_id)

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Full book for a token. Returns {bids: [[price,size]…], asks: […]}.
        Polymarket binary markets have YES and NO tokens; the scanner
        chooses which side to snapshot per PLAN.md §6.
        """
        raw = self._call(self._sdk.get_order_book, token_id)
        # py_clob_client returns an OrderBookSummary object; normalise to dict.
        bids = getattr(raw, "bids", None)
        asks = getattr(raw, "asks", None)
        if bids is None and isinstance(raw, dict):
            bids = raw.get("bids", [])
        if asks is None and isinstance(raw, dict):
            asks = raw.get("asks", [])
        return {
            "bids": [[float(b.price), float(b.size)] if hasattr(b, "price") else list(b) for b in (bids or [])],
            "asks": [[float(a.price), float(a.size)] if hasattr(a, "price") else list(a) for a in (asks or [])],
        }

    # ───────────────────────  whale data (REST)  ───────────────────────

    def get_trades_for_wallet(
        self, wallet: str, after_ts: int | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Per-wallet fills via data-api.polymarket.com/trades?user=...

        **Timestamp normalisation (single-point-of-truth).** data-api returns
        each trade's `timestamp` in UNIX seconds; this method converts it to
        ms-epoch in-place before returning, so all downstream consumers
        (whale_tracker, audit log, classifier) see a uniform unit matching
        the rest of the codebase (PLAN.md §6 schema is ms throughout).
        This is the ONLY place the seconds→ms conversion happens.

        `after_ts` is ms epoch; rows whose normalised `timestamp` is below
        this cutoff are filtered locally (the public endpoint's time-range
        params are not stable).
        """
        params: dict[str, Any] = {"user": wallet, "limit": limit, "offset": offset}
        url = f"{self._data_api_host}/trades"

        def _fetch() -> list[dict[str, Any]]:
            r = self._session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json() or []

        rows = self._call(_fetch)
        # Normalise: seconds → ms. Boundary conversion, in-place.
        for row in rows:
            ts = row.get("timestamp")
            if ts is None:
                continue
            try:
                row["timestamp"] = int(float(ts)) * 1000
            except (TypeError, ValueError):
                continue
        if after_ts is not None:
            rows = [t for t in rows if int(t.get("timestamp", 0)) >= after_ts]
        return rows

    def get_leaderboard(
        self, window: str = "all", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Top profit traders. window ∈ {1d, 1w, 1m, all}."""
        params = {"window": window, "limit": limit}
        url = f"{self._lb_api_host}/profit"

        def _fetch() -> list[dict[str, Any]]:
            r = self._session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json() or []

        return self._call(_fetch)

    # ──────────────────────  read-only enforcement  ──────────────────────

    @staticmethod
    def _phase_b_only(method_name: str):
        raise NotImplementedError(
            f"{method_name}: Phase A read-only client; use bot.executor in Phase B"
        )

    def place_order(self, *_a, **_kw):
        self._phase_b_only("place_order")

    def post_order(self, *_a, **_kw):
        self._phase_b_only("post_order")

    def post_orders(self, *_a, **_kw):
        self._phase_b_only("post_orders")

    def create_order(self, *_a, **_kw):
        self._phase_b_only("create_order")

    def create_market_order(self, *_a, **_kw):
        self._phase_b_only("create_market_order")

    def create_and_post_order(self, *_a, **_kw):
        self._phase_b_only("create_and_post_order")

    def cancel(self, *_a, **_kw):
        self._phase_b_only("cancel")

    def cancel_all(self, *_a, **_kw):
        self._phase_b_only("cancel_all")

    def cancel_orders(self, *_a, **_kw):
        self._phase_b_only("cancel_orders")

    def cancel_market_orders(self, *_a, **_kw):
        self._phase_b_only("cancel_market_orders")
