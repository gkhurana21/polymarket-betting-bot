"""Phase B executor: signing CLOB client + order placement + live_orders persistence.

ClobSigningClient subclasses ClobReadOnlyClient and overrides the Phase B
stubs (place_order etc.) with real signed requests via py_clob_client.

LiveTrader.consume(signal) is the top-level entry point, called from main.py
alongside PaperTrader. It:
  1. Resolves token_id from market_token_map (DB cache)
  2. Generates BetIntent via Strategy
  3. Gates through RiskGate (raises RiskVeto → logged, dropped)
  4. Deduplicates via risk_token_hash (idempotent across restarts)
  5. Places the order (or simulates it in dry-run mode)
  6. Writes to live_orders table

PRIVATE_KEY must be set in the environment for live mode. In dry-run mode
(config dry_run: true) all logic runs but no order is submitted to the CLOB.
"""

from __future__ import annotations

import os
import threading
import time
from queue import Empty, Queue
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

from bot.client import CircuitBreakerOpen, ClobReadOnlyClient
from bot.models import BetIntent, LiveOrder, WhaleSignal
from bot.risk import RiskGate, RiskVeto
from bot.strategy import Strategy
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)


class ClobSigningClient(ClobReadOnlyClient):
    """Phase B signing-capable CLOB client.

    Adds a real ClobClient with L1 (EOA) credentials loaded from the
    environment. Overrides the Phase B NotImplementedError stubs.

    Environment variables required for live mode:
      PRIVATE_KEY            — Ethereum private key (hex, with or without 0x)
      CLOB_API_KEY           — from /auth/derive-api-key
      CLOB_API_SECRET        — from /auth/derive-api-key
      CLOB_API_PASSPHRASE    — from /auth/derive-api-key

    Constructed lazily: Phase A never imports this class (executor.py is
    never imported in Phase A paths).
    """

    def __init__(self, **kwargs) -> None:
        # Build read-only base first (no key= passed, validates key=None).
        super().__init__(**kwargs)

        private_key = os.environ.get("PRIVATE_KEY", "").strip()
        if not private_key:
            raise EnvironmentError(
                "PRIVATE_KEY not set — cannot build ClobSigningClient. "
                "Set it in .env or the environment before starting Phase B."
            )

        api_key = os.environ.get("CLOB_API_KEY", "")
        api_secret = os.environ.get("CLOB_API_SECRET", "")
        api_passphrase = os.environ.get("CLOB_API_PASSPHRASE", "")

        creds: ApiCreds | None = None
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

        self._signing_sdk = ClobClient(
            host=self._clob_host,
            chain_id=POLYGON,
            key=private_key,
            creds=creds,
        )
        self._address = self._signing_sdk.get_address()
        _log.info("ClobSigningClient ready — address=%s", self._address)

    def create_and_post_order(self, order_args: OrderArgs) -> dict[str, Any]:
        """Place a signed GTC limit order. Retried through the base _call chain."""
        def _place():
            return self._signing_sdk.create_and_post_order(order_args)
        return self._call(_place)

    def cancel(self, order_id: str) -> dict[str, Any]:
        def _cancel():
            return self._signing_sdk.cancel(order_id)
        return self._call(_cancel)

    def get_open_orders(self) -> list[dict[str, Any]]:
        def _fetch():
            return self._signing_sdk.get_open_orders()
        return self._call(_fetch)


class LiveTrader:
    """Consumes WhaleSignal events from the shared queue, applies strategy +
    risk gating, and places live orders (or dry-run logs).

    Runs in its own daemon thread (same pattern as PaperTrader). Shares the
    same Tracker + signal queue as PaperTrader so both streams record in
    parallel.
    """

    def __init__(
        self,
        tracker: Tracker,
        client: ClobReadOnlyClient,
        config: dict[str, Any],
        *,
        signal_queue: Queue,
    ) -> None:
        self._tracker = tracker
        self._client = client
        self._config = config
        self._queue = signal_queue
        self._dry_run: bool = bool(config.get("dry_run", True))
        self._strategy = Strategy(config)
        self._risk = RiskGate(config)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        exec_cfg = config.get("execution") or {}
        self._order_type = exec_cfg.get("order_type", "GTC")
        self._retry_attempts = int(exec_cfg.get("retry_attempts", 3))

        # Which whale signal types this strategy acts on. The validated edge
        # (mid-band + "open" signals, +38% out-of-sample) is open-only;
        # "add" signals were ~flat (+1.6% OOS). Config-overridable.
        strat_cfg = config.get("strategy") or {}
        self._acts_on: tuple[str, ...] = tuple(
            strat_cfg.get("acts_on_signal_types", ["open"])
        )

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._consume_loop, name="live-trader", daemon=True
        )
        self._thread.start()
        _log.info("LiveTrader started (dry_run=%s)", self._dry_run)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    # ── internal ──────────────────────────────────────────────────────

    def _consume_loop(self) -> None:
        while not self._stop.is_set():
            try:
                signal: WhaleSignal = self._queue.get(timeout=1.0)
            except Empty:
                continue
            except (KeyboardInterrupt, SystemExit):
                raise
            try:
                self._consume(signal)
            except Exception as exc:
                _log.error("live_trader consume error: %s", exc, exc_info=True)

    def _consume(self, signal: WhaleSignal) -> None:
        if signal.signal_type not in self._acts_on:
            return

        # Resolve the outcome token we need to BUY (YES token for a YES bet,
        # NO token for a NO bet), falling back to an on-demand market fetch.
        token_id = self._resolve_token(signal.market_id, signal.side)
        if not token_id:
            _log.debug(
                "live_trader: no %s token for %s — skip",
                signal.side, signal.market_id[:16],
            )
            return

        signal_id = signal.id or 0

        # Get trust row for this whale.
        trust_row = self._tracker.get_whale_trust_row(signal.whale_address)

        intent = self._strategy.generate(signal, trust_row, token_id, signal_id=signal_id)
        if intent is None:
            return

        # Dedup: skip if we've already placed an order with this hash.
        if self._tracker.live_order_exists(intent.risk_token_hash):
            _log.debug("live_trader: duplicate risk_token_hash %s — skip", intent.risk_token_hash[:12])
            return

        # Risk gate.
        try:
            self._risk.check(intent, self._tracker)
        except RiskVeto as veto:
            _log.info("live_trader: risk veto [%s] %s", veto.reason, veto.detail)
            return

        # Place order (or simulate in dry-run).
        self._place(intent)

    def _resolve_token(self, market_id: str, side: str) -> str | None:
        """Return the token_id to BUY for `side`, or None if unresolvable.

        Primary source is market_token_map (populated by the scanner). Whales
        routinely open positions in markets the scanner has not catalogued yet
        — and before this fallback existed the NO side was effectively dead
        (no_token_id was never populated for live signal markets, so every
        NO-side signal was silently dropped). On a miss we fetch the market
        once, cache BOTH tokens, and proceed. Never raises.
        """
        want_no = side == "NO"
        cached = (
            self._tracker.get_no_token_id(market_id)
            if want_no
            else self._tracker.get_yes_token_id(market_id)
        )
        if cached:
            return cached

        try:
            market = self._client.get_market(market_id)
        except CircuitBreakerOpen:
            _log.warning("live_trader: circuit breaker open on get_market %s", market_id[:16])
            return None
        except Exception as e:
            _log.warning("live_trader: get_market failed for %s (%s)", market_id[:16], e)
            return None

        yes_tok = no_tok = None
        for t in (market.get("tokens") or []):
            label = (t.get("outcome") or "").strip().lower()
            if label == "yes":
                yes_tok = t.get("token_id")
            elif label == "no":
                no_tok = t.get("token_id")

        # Cache whatever we learned — non-fatal on failure.
        if yes_tok or no_tok:
            try:
                if yes_tok:
                    self._tracker.record_market_token(market_id, yes_tok, no_tok)
            except Exception as e:
                _log.warning("live_trader: caching tokens for %s failed: %s", market_id[:16], e)

        return no_tok if want_no else yes_tok

    def _place(self, intent: BetIntent) -> None:
        placed_at = now_ms()

        if self._dry_run:
            shares = round(intent.size_usd / intent.limit_price, 2) if intent.limit_price else 0.0
            _log.info(
                "live_trader DRY-RUN: would BUY %s token $%.2f = %.2f shares @ %.4f market=%s",
                intent.side, intent.size_usd, shares,
                intent.limit_price, intent.market_id[:16],
            )
            order = LiveOrder(
                clob_order_id=f"dry-{intent.risk_token_hash[:12]}",
                market_id=intent.market_id,
                side=intent.side,
                limit_price=intent.limit_price,
                size_usd=intent.size_usd,
                status="dry_run",
                placed_at_ts=placed_at,
                risk_token_hash=intent.risk_token_hash,
                intent_json=intent.intent_json,
            )
            self._tracker.insert_live_order(order)
            return

        # Live path — requires ClobSigningClient.
        if not isinstance(self._client, ClobSigningClient):
            _log.error(
                "live_trader: dry_run=false but client is not ClobSigningClient — "
                "refusing to place order. Restart with a signing client."
            )
            return

        # Polymarket order semantics:
        #   - token_id selects the outcome (YES token vs NO token); intent.token_id
        #     already resolved the correct one in _consume.
        #   - side is BUY/SELL, NOT YES/NO. We always BUY the chosen outcome token
        #     (copying a whale's directional bet = buying that side's shares).
        #   - size is the number of SHARES, not USD. shares = usd / price.
        shares = round(intent.size_usd / intent.limit_price, 2)
        order_args = OrderArgs(
            token_id=intent.token_id,
            price=intent.limit_price,
            size=shares,
            side=BUY,
        )

        clob_order_id: str | None = None
        last_exc: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                resp = self._client.create_and_post_order(order_args)
                clob_order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
                if not clob_order_id:
                    raise ValueError(f"CLOB returned no order ID: {resp!r}")
                break
            except Exception as exc:
                last_exc = exc
                _log.warning(
                    "live_trader: order attempt %d/%d failed: %s",
                    attempt, self._retry_attempts, exc,
                )
                if attempt < self._retry_attempts:
                    time.sleep(2 ** (attempt - 1))

        if clob_order_id is None:
            _log.error(
                "live_trader: all %d attempts failed for %s — order not placed: %s",
                self._retry_attempts, intent.market_id[:16], last_exc,
            )
            return

        order = LiveOrder(
            clob_order_id=clob_order_id,
            market_id=intent.market_id,
            side=intent.side,
            limit_price=intent.limit_price,
            size_usd=intent.size_usd,
            status="open",
            placed_at_ts=placed_at,
            risk_token_hash=intent.risk_token_hash,
            intent_json=intent.intent_json,
        )
        self._tracker.insert_live_order(order)
        _log.info(
            "live_trader: placed order=%s %s %s $%.2f @ %.4f",
            clob_order_id[:16], intent.side, intent.market_id[:16],
            intent.size_usd, intent.limit_price,
        )
