"""Conservative maker-fill replay over historical order books.

THE PROBLEM THIS SOLVES

Friday can project its income (liquidity rewards accrue for RESTING, no fill
required) but has never measured its cost. Adverse selection only appears when
you get filled, and dry-run has recorded zero fills by design — the predecessor
project died of an optimistic fill assumption and I would not repeat it.

WHY SIMULATING MAKER FILLS IS SAFE WHERE TAKER FILLS WERE NOT

For a TAKER, assuming you were filled is the OPTIMISTIC error: it hands you the
trade you wanted at the price you wanted. That is exactly what turned a
measured +21.8% into -18.1% in the predecessor.

For a MAKER it is the PESSIMISTIC error. A fill is not a prize; it is someone
choosing to trade against your resting quote, handing you inventory you did not
ask for. Assuming more fills means assuming more adverse selection. The bias
runs the safe way.

THE FILL RULE

Snapshots are ~10 minutes apart, so trades between them are not observable
directly. Fills must be inferred from book state — and the inference has to be
built on the side of the book our absence does not distort.

The obvious rule is wrong. "Our bid is no longer the best bid, so it was taken"
fails because the recorded book never contained our order: once the volatility
estimate settles, Friday quotes INSIDE the observed spread, and the recorded
best bid then sits below ours in every interval regardless of what happened.
That reports a fill every tick and destroys the signal entirely.

Use the counterparty side instead. Our resting bid at B is hit only if somebody
was willing to sell at or below B — which shows up as the market's best ASK
reaching down to our price:

    best_ask(t+1) <= our_bid      =>  our bid was hit    (we bought at B)
    best_bid(t+1) >= our_ask      =>  our ask was lifted (we sold at A)

Both conditions are properties of the other side of the book, so they hold
whether or not our order was present.

DIRECTION OF ERROR, STATED PLAINLY

This detects fills where price moved through us AND STAYED — which are the
adverse ones. It misses fills where price dipped through and recovered inside
the interval, which are the FAVOURABLE ones (bought low, price came back). So
the model over-weights bad fills and under-weights good ones. Pessimistic,
which is the direction to be wrong in.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from friday.marketmaker import MarketMaker
from friday.logger import get_logger

_log = get_logger(__name__)

_MS_PER_DAY = 86_400_000


@dataclass
class Fill:
    ts: int
    side: str            # "BUY" (bid hit) | "SELL" (ask lifted)
    price: float
    size: float
    mid_after: float | None = None
    markout: float | None = None      # signed, per share, positive = good


@dataclass
class ReplayResult:
    market_id: str = ""
    snapshots: int = 0
    quoting_intervals: int = 0
    standdown_intervals: int = 0
    standdown_reasons: dict[str, int] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    reward_eligible_ms: int = 0
    adverse_selection_usd: float = 0.0
    favourable_usd: float = 0.0
    rebate_usd: float = 0.0
    final_position: float = 0.0
    inventory_mtm_usd: float = 0.0

    @property
    def n_fills(self) -> int:
        return len(self.fills)

    @property
    def net_flow_usd(self) -> float:
        """Everything except liquidity rewards, which accrue separately."""
        return (self.favourable_usd - self.adverse_selection_usd
                + self.rebate_usd + self.inventory_mtm_usd)


def _touch(book_json: str):
    try:
        b = json.loads(book_json)
        bids = sorted(((float(x[0]), float(x[1])) for x in (b.get("bids") or [])),
                      key=lambda t: -t[0])
        asks = sorted(((float(x[0]), float(x[1])) for x in (b.get("asks") or [])),
                      key=lambda t: t[0])
    except Exception:
        return None
    if not bids or not asks:
        return None
    return bids, asks


class MakerReplay:
    """Replays the quoting engine over recorded books for one market."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        rb = (config.get("rebates") or {})
        # Maker rebate: a share of the taker fee paid on the liquidity you
        # provided that got taken. Category rates 15-25%; taker fee itself is
        # C * rate * p * (1-p).
        self._rebate_share = float(rb.get("maker_rebate_share", 0.25))
        self._taker_fee_rate = float(rb.get("taker_fee_rate", 0.05))
        self._markout_lag = int(rb.get("markout_lag_snapshots", 3))

    def _rebate(self, size: float, price: float) -> float:
        if not (0.0 < price < 1.0):
            return 0.0
        taker_fee = size * self._taker_fee_rate * price * (1.0 - price)
        return taker_fee * self._rebate_share

    def replay_market(self, rows: list[sqlite3.Row], *,
                      reward_band: float = 0.045,
                      min_size: float = 200.0) -> ReplayResult:
        res = ReplayResult(market_id=rows[0]["market_id"] if rows else "")
        mm = MarketMaker(self._config, reward_band=reward_band,
                         min_size_shares=min_size,
                         condition_id=res.market_id)
        position = 0.0
        cash = 0.0
        pending: list[tuple[int, Fill]] = []          # (resolve_at_index, fill)
        mids: list[float] = []

        parsed = []
        for r in rows:
            t = _touch(r["book_json"])
            if t is not None:
                parsed.append((r["ts"], t[0], t[1]))
        res.snapshots = len(parsed)
        if len(parsed) < 2:
            return res

        for i in range(len(parsed) - 1):
            ts, bids, asks = parsed[i]
            nts, nbids, nasks = parsed[i + 1]
            mid = 0.5 * (bids[0][0] + asks[0][0])
            mids.append(mid)

            q = mm.quote(bids, asks, position, cash)
            if not q.quoting:
                res.standdown_intervals += 1
                res.standdown_reasons[q.reason] = \
                    res.standdown_reasons.get(q.reason, 0) + 1
                continue
            res.quoting_intervals += 1

            # Resting inside the reward band at min_size earns for the interval.
            if abs(q.bid_px - mid) <= reward_band and q.bid_sz >= min_size:
                res.reward_eligible_ms += (nts - ts)

            next_bid, next_ask = nbids[0][0], nasks[0][0]

            # Counterparty-side inference (see module docstring): our bid is hit
            # only when someone is willing to sell down to it.
            if q.bid_sz > 0 and next_ask <= q.bid_px + 1e-9:
                f = Fill(ts=nts, side="BUY", price=q.bid_px, size=q.bid_sz)
                position += q.bid_sz
                cash -= q.bid_sz * q.bid_px
                res.rebate_usd += self._rebate(q.bid_sz, q.bid_px)
                mm.on_fill(+q.bid_sz, q.bid_px)
                res.fills.append(f)
                pending.append((i + self._markout_lag, f))

            if q.ask_sz > 0 and next_bid >= q.ask_px - 1e-9:
                f = Fill(ts=nts, side="SELL", price=q.ask_px, size=q.ask_sz)
                position -= q.ask_sz
                cash += q.ask_sz * q.ask_px
                res.rebate_usd += self._rebate(q.ask_sz, q.ask_px)
                mm.on_fill(-q.ask_sz, q.ask_px)
                res.fills.append(f)
                pending.append((i + self._markout_lag, f))

            # Score fills whose markout horizon has arrived.
            still = []
            for due, f in pending:
                if due <= i:
                    fair = mid
                    sign = 1.0 if f.side == "BUY" else -1.0
                    f.mid_after = fair
                    f.markout = sign * (fair - f.price)
                    pnl = f.markout * f.size
                    if pnl < 0:
                        res.adverse_selection_usd += -pnl
                    else:
                        res.favourable_usd += pnl
                else:
                    still.append((due, f))
            pending = still

        # Anything unscored: mark against the last observed mid.
        if mids:
            for _due, f in pending:
                sign = 1.0 if f.side == "BUY" else -1.0
                f.mid_after = mids[-1]
                f.markout = sign * (mids[-1] - f.price)
                pnl = f.markout * f.size
                if pnl < 0:
                    res.adverse_selection_usd += -pnl
                else:
                    res.favourable_usd += pnl

        res.final_position = position
        if mids and abs(position) > 1e-9:
            res.inventory_mtm_usd = cash + position * mids[-1]
        else:
            res.inventory_mtm_usd = cash
        return res
