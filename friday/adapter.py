"""The only component that touches the exchange API.

TWO-SIDED QUOTING ON A BINARY VENUE

Polymarket has no short sale. Resting an ask on the YES token requires already
holding YES. A maker starting flat therefore quotes both sides as BUYS on
opposite tokens:

    our bid  ->  BUY  YES token  @ bid_px
    our ask  ->  BUY  NO  token  @ (1 - ask_px)

Buying NO at (1 - a) is economically identical to selling YES at a, and it
needs no inventory to initiate. This is also why quoting both sides at
min_size costs ~$200 regardless of price:
    200*bid_px + 200*(1 - ask_px) ~= 200.

QUEUE POSITION IS THE PRODUCT

mm-harness bot.py:17 — "Re-quoting the same price MUST be a no op. Without an
atomic replace, cancel-and-resubmit throws away queue position every single
tick." On this venue queue position is not a nicety, it is literally the thing
that earns the reward, because rewards accrue to resting size. `sync_quote`
therefore diffs against what is already resting and does nothing when the
target is unchanged.

CORRECTNESS LESSONS ALREADY PAID FOR (predecessor project, FINDINGS.md):
  * OrderArgs.side is BUY/SELL — never "YES"/"NO".
  * OrderArgs.size is SHARES, not USD.
  * OrderArgs has no order_type kwarg; create_and_post_order defaults to GTC.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

from friday.client import ClobReadOnlyClient
from friday.logger import get_logger, register_secret

_log = get_logger(__name__)


@dataclass
class RestingOrder:
    order_id: str
    token_id: str
    price: float
    size: float


class ClobSigningClient(ClobReadOnlyClient):
    """Signing-capable client. Only constructed when dry_run is false."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        pk = os.environ.get("PRIVATE_KEY", "").strip()
        if not pk:
            raise EnvironmentError(
                "PRIVATE_KEY not set — cannot build ClobSigningClient. "
                "Friday runs dry_run: true without it."
            )
        register_secret(pk)
        creds = None
        k, s, p = (os.environ.get("CLOB_API_KEY", ""),
                   os.environ.get("CLOB_API_SECRET", ""),
                   os.environ.get("CLOB_API_PASSPHRASE", ""))
        if k and s and p:
            for v in (k, s, p):
                register_secret(v)
            creds = ApiCreds(api_key=k, api_secret=s, api_passphrase=p)
        self._signing_sdk = ClobClient(host=self._clob_host, chain_id=POLYGON,
                                       key=pk, creds=creds)
        self.address = self._signing_sdk.get_address()
        _log.info("ClobSigningClient ready — address=%s", self.address)

    def post_order(self, args: OrderArgs) -> dict[str, Any]:
        return self._call(self._signing_sdk.create_and_post_order, args)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._call(self._signing_sdk.cancel, order_id)

    def open_orders(self) -> list[dict[str, Any]]:
        return self._call(self._signing_sdk.get_open_orders)


class PolymarketAdapter:
    """Quote placement with atomic-replace semantics."""

    def __init__(self, client: ClobReadOnlyClient, *, dry_run: bool = True) -> None:
        self._client = client
        self._dry_run = dry_run
        # (token_id, tag) -> RestingOrder ; tag distinguishes our two legs
        self._resting: dict[str, RestingOrder] = {}
        self.replaced = 0
        self.noops = 0

    # -- reads --------------------------------------------------------------

    def get_book(self, token_id: str):
        """Returns (bids, asks) as [(price, size)...] best-first."""
        b = self._client.get_order_book(token_id)
        bids = sorted(((float(x[0]), float(x[1])) for x in (b.get("bids") or [])),
                      key=lambda t: -t[0])
        asks = sorted(((float(x[0]), float(x[1])) for x in (b.get("asks") or [])),
                      key=lambda t: t[0])
        return bids, asks

    # -- quoting ------------------------------------------------------------

    @staticmethod
    def ask_as_no_buy(ask_px: float) -> float:
        """Our YES-ask expressed as the NO-token buy price."""
        return round(1.0 - ask_px, 4)

    def sync_quote(self, tag: str, token_id: str, price: float, size: float) -> str:
        """Bring one resting leg in line with the target.

        Returns "noop" | "placed" | "replaced" | "cancelled" | "skipped".
        A no-op is the common and desirable case: it preserves queue position,
        which is what actually earns the reward.
        """
        cur = self._resting.get(tag)

        if size <= 0:
            if cur is not None:
                self._cancel(tag, cur)
                return "cancelled"
            return "noop"

        if cur is not None and abs(cur.price - price) < 1e-9 and abs(cur.size - size) < 1e-9:
            self.noops += 1
            return "noop"                     # identical: do NOT touch it

        if cur is not None:
            self._cancel(tag, cur)
            self.replaced += 1
            action = "replaced"
        else:
            action = "placed"

        oid = self._place(token_id, price, size)
        if oid is None:
            return "skipped"
        self._resting[tag] = RestingOrder(oid, token_id, price, size)
        return action

    def _place(self, token_id: str, price: float, size: float) -> str | None:
        if self._dry_run:
            _log.info("DRY-RUN place: BUY token=%s… %.0f sh @ %.4f",
                      token_id[:12], size, price)
            return f"dry-{token_id[:10]}-{price:.4f}"
        if not isinstance(self._client, ClobSigningClient):
            _log.error("dry_run false but client is not signing-capable — refusing")
            return None
        args = OrderArgs(token_id=token_id, price=round(price, 4),
                         size=round(size, 2), side=BUY)
        try:
            resp = self._client.post_order(args)
        except Exception as e:
            _log.error("order placement failed: %s", e)
            return None
        oid = resp.get("orderID") or resp.get("id") or resp.get("order_id")
        if not oid:
            _log.error("CLOB returned no order id: %r", resp)
            return None
        _log.info("placed BUY token=%s… %.0f sh @ %.4f id=%s",
                  token_id[:12], size, price, str(oid)[:14])
        return str(oid)

    def _cancel(self, tag: str, order: RestingOrder) -> None:
        if not self._dry_run and isinstance(self._client, ClobSigningClient):
            try:
                self._client.cancel_order(order.order_id)
            except Exception as e:
                _log.error("cancel failed for %s: %s", order.order_id[:14], e)
                return
        self._resting.pop(tag, None)

    def cancel_all(self) -> int:
        n = 0
        for tag, order in list(self._resting.items()):
            self._cancel(tag, order)
            n += 1
        return n

    def resting(self, tag: str) -> RestingOrder | None:
        return self._resting.get(tag)

    def stats(self) -> dict[str, int]:
        return {"resting": len(self._resting), "replaced": self.replaced,
                "noops": self.noops}
