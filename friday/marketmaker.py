"""Quoting engine. Ported from ~/trading/mm-harness/bot.py.

That file's design notes were measured against a mock exchange, not guessed,
and the findings carry over to this venue. The ones that shaped this port:

  * The MARK is the whole game. Handing strategies a good fair value made them
    profitable; deriving it badly flipped every one to a loss.
  * A one-sided book must NOT move the mark. Jumping to the single price
    showing overflowed the vol estimate and produced an infinite quote price.
  * Half spread must clear ~vol_floor_mult * sigma * (latency + 1).
  * The one-sided toxicity guard was the only defense that helped in every
    regime and every latency tested.
  * Re-quoting the same price must be a no-op (handled in adapter.py).

WHAT IS NEW HERE, and it is the most important rule in Friday:

  Polymarket pays liquidity rewards only while our quote rests within
  `max_spread` of mid. That turns the reward band into a HARD CAP on the half
  spread — and it collides head-on with the volatility floor above. The vol
  floor says "quote this wide or get run over"; the band says "quote wider
  than this and earn nothing".

  When the floor exceeds the band the two are irreconcilable, and the correct
  action is to NOT QUOTE THAT MARKET. Quoting inside the band anyway would
  mean knowingly standing at a spread the volatility does not support, in
  exchange for a few dollars a day of rewards. That trade is how a market
  maker dies.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from friday.events import BUS, CLOB
from friday.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class Quote:
    """A two-sided quote. Size 0 on a side means do not rest there.

    `reason` is populated when we decline to quote, and is logged so the
    post-mortem can attribute downtime to a cause rather than a silence.
    """
    bid_px: float = 0.0
    bid_sz: float = 0.0
    ask_px: float = 0.0
    ask_sz: float = 0.0
    reason: str = ""

    @property
    def quoting(self) -> bool:
        return self.bid_sz > 0 or self.ask_sz > 0


class MarkEstimator:
    """Book -> fair value + volatility estimate.

    Deliberately conservative: it would rather hold a slightly stale mark than
    chase a thin or one-sided book. Chasing is how you crash.
    """

    def __init__(self, cfg: dict[str, Any], tick: float) -> None:
        self.cfg = cfg
        self.tick = tick
        self.last: float | None = None
        self.moves: deque[float] = deque(maxlen=int(cfg.get("vol_window", 30)))

    def update(self, bids, asks, own_bid=None, own_ask=None):
        """bids/asks are [(price, size), ...] best first. Returns (fair, vol)."""
        if self.cfg.get("exclude_self_from_mark", True):
            bids = self._strip(bids, own_bid)
            asks = self._strip(asks, own_ask)

        fair = self._microprice(bids, asks)
        if fair is None:
            return self.last, self._vol()

        if self.last is not None:
            move = fair - self.last
            # One bad print must not poison the vol estimate for a whole window.
            if abs(move) <= float(self.cfg.get("max_move_ticks", 50)) * self.tick:
                self.moves.append(move)
            else:
                return self.last, self._vol()      # reject the print entirely
        self.last = fair
        return fair, self._vol()

    @staticmethod
    def _strip(levels, own):
        """Remove our own resting size. Building a mark from a book containing
        our own quotes is a feedback loop."""
        if not own:
            return levels
        own_px, own_sz = own
        out = []
        for px, sz in levels:
            if abs(px - own_px) < 1e-9:
                sz -= own_sz
                if sz <= 0:
                    continue
            out.append((px, sz))
        return out

    def _microprice(self, bids, asks):
        """mark_mode "micro" size-weights toward the thin side; "mid" uses
        prices only. A one-sided or empty book HOLDS the last mark — never
        jump to the one price showing.
        """
        if bids and asks:
            bpx, bsz = bids[0]
            apx, asz = asks[0]
            if self.cfg.get("mark_mode", "micro") == "mid":
                return 0.5 * (bpx + apx)
            total = bsz + asz
            if total <= 0:
                return 0.5 * (bpx + apx)
            return (bpx * asz + apx * bsz) / total
        return self.last

    def _vol(self) -> float:
        if len(self.moves) < 5:
            return float(self.cfg.get("vol_guess", 0.002))
        n = len(self.moves)
        mean = sum(self.moves) / n
        var = sum((m - mean) ** 2 for m in self.moves) / n
        v = math.sqrt(var)
        # Clamp so downstream arithmetic can never explode.
        return min(max(v, self.tick * 0.1),
                   float(self.cfg.get("max_move_ticks", 50)) * self.tick)


class MarkoutTracker:
    """Scores each fill `lag` ticks later against where fair actually went.

    Negative average markout means we are quoting too tight for this
    environment, whatever the cause. This is the single best live health
    signal — watch it, not fill count.
    """

    def __init__(self, lag: int = 10, window: int = 200) -> None:
        self.lag = lag
        self.pending: list[tuple[int, float, float]] = []
        self.marks: deque[float] = deque(maxlen=window)

    def record(self, tick: int, signed_size: float, price: float) -> None:
        self.pending.append((tick + self.lag, signed_size, price))

    def resolve(self, tick: int, fair: float | None) -> None:
        keep = []
        for due, signed, price in self.pending:
            if due <= tick and fair is not None:
                self.marks.append(signed * (fair - price) / abs(signed))
            elif due > tick:
                keep.append((due, signed, price))
        self.pending = keep

    def average(self) -> float | None:
        return sum(self.marks) / len(self.marks) if self.marks else None


class MarketMaker:
    """One market, one inventory, one toxicity pair.

    Risk that spans markets belongs in inventory.PortfolioRisk, not here — a
    per-market limit cannot express "cap my total exposure". That gate runs in
    the engine before capital is committed to any market.
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        reward_band: float,
        min_size_shares: float,
        condition_id: str = "",
    ) -> None:
        q = config.get("quoting") or {}
        mk = config.get("mark") or {}
        inv = config.get("inventory") or {}
        tox = config.get("toxicity") or {}
        saf = config.get("safety") or {}

        self.condition_id = condition_id
        self.tick = float(q.get("tick", 0.001))
        self.band = float(reward_band)
        # Never quote below the reward floor: sub-min_size earns nothing while
        # still carrying full inventory risk.
        self.min_size = float(min_size_shares or q.get("min_size_shares", 200))
        self.size = float(q.get("size_shares", 200))

        self._cfg_half = float(q.get("half_spread", 0.008))
        self._vol_floor_mult = float(q.get("vol_floor_mult", 2.5))
        self._min_half = float(q.get("min_half", 0.001))
        self._max_half = float(q.get("max_half", 0.05))
        self._latency = int(q.get("latency_ticks", 1))

        self._skew = float(inv.get("skew", 5e-6))
        self._soft_inv = float(inv.get("soft_inventory_shares", 400))
        self._pos_limit = float(inv.get("position_limit_shares", 800))
        self._cushion = float(inv.get("inventory_cushion", 0.85))

        self._tox_decay = float(tox.get("tox_decay", 0.85))
        self._tox_widen = float(tox.get("tox_widen", 5e-6))
        self._tox_shrink = float(tox.get("tox_shrink", 0.25))

        self._max_loss = float(saf.get("max_loss_usd", 15.0))
        self._kill_loss = float(saf.get("kill_loss_usd", 40.0))

        self.mark = MarkEstimator(mk, self.tick)
        self.markout = MarkoutTracker(int(mk.get("markout_lag", 10)))
        self.tox_bid = 0.0
        self.tox_ask = 0.0
        self.tick_index = 0
        self.peak_pnl = 0.0
        self.killed = False

    # -- risk ---------------------------------------------------------------

    def effective_limit(self) -> float:
        return self._pos_limit * self._cushion

    def check_safety(self, pnl: float) -> float:
        """Size multiplier: 1.0 normal, 0.5 wounded, 0.0 dead."""
        self.peak_pnl = max(self.peak_pnl, pnl)
        dd = self.peak_pnl - pnl
        if self.killed or dd >= self._kill_loss:
            if not self.killed:
                _log.error("mm %s: KILLED on drawdown $%.2f", self.condition_id[:12], dd)
            self.killed = True
            return 0.0
        if dd >= self._max_loss:
            return 0.5
        return 1.0

    # -- spread -------------------------------------------------------------

    def half_spread(self, vol_est: float) -> float | None:
        """Returns the half spread, or None if this market cannot be quoted
        safely inside its reward band.

        THE central rule. The vol floor and the reward band are in direct
        tension; when the floor wins, the market is too volatile to earn in
        and we stand down rather than quote a spread the volatility does not
        support.
        """
        floor = self._vol_floor_mult * vol_est * (self._latency + 1)
        if floor > self.band:
            return None
        half = max(self._cfg_half, floor, self._min_half)
        return min(half, self._max_half, self.band)

    # -- fills --------------------------------------------------------------

    def on_fill(self, signed_size: float, price: float) -> None:
        """signed_size > 0 means our bid was hit (we bought)."""
        self.markout.record(self.tick_index, signed_size, price)
        if signed_size > 0:
            self.tox_bid += abs(signed_size)
        else:
            self.tox_ask += abs(signed_size)

    # -- the quote ----------------------------------------------------------

    def quote(self, bids, asks, position: float, pnl: float,
              own_bid=None, own_ask=None) -> Quote:
        self.tick_index += 1

        fair, vol_est = self.mark.update(bids, asks, own_bid, own_ask)
        cid = self.condition_id
        BUS.emit(CLOB, f"mark:{cid}", "book",
                 bids=len(bids), asks=len(asks))
        BUS.node_state(f"mark:{cid}", fair=fair, vol=round(vol_est, 5))
        if fair is None:
            BUS.node_state(f"mm:{cid}", quoting=False, reason="no_mark")
            return Quote(reason="no_mark")          # never guess a mark
        BUS.emit(f"mark:{cid}", f"mm:{cid}", "fair",
                 fair=round(fair, 4), vol=round(vol_est, 5))
        self.markout.resolve(self.tick_index, fair)

        scale = self.check_safety(pnl)
        if scale == 0.0:
            BUS.node_state(f"mm:{cid}", quoting=False, reason="killed", killed=True)
            return Quote(reason="killed")

        self.tox_bid *= self._tox_decay
        self.tox_ask *= self._tox_decay

        half = self.half_spread(vol_est)
        if half is None:
            BUS.node_state(f"mm:{cid}", quoting=False, reason="vol_exceeds_reward_band")
            BUS.emit(f"mark:{cid}", f"mm:{cid}", "standdown",
                     vol=round(vol_est, 5), band=self.band)
            return Quote(reason="vol_exceeds_reward_band")

        # Widen if our own fills are proving us wrong.
        avg_markout = self.markout.average()
        if avg_markout is not None and avg_markout < 0:
            half = min(half * 1.5, self.band)

        center = fair - position * self._skew

        net = self.tox_bid - self.tox_ask
        BUS.node_state(f"tox:{cid}", net=round(net, 1),
                       tox_bid=round(self.tox_bid, 1), tox_ask=round(self.tox_ask, 1))
        if abs(net) > 1e-6:
            BUS.emit(f"tox:{cid}", f"mm:{cid}", "tox", net=round(net, 1))
        bid_push = max(0.0, net) * self._tox_widen
        ask_push = max(0.0, -net) * self._tox_widen

        bid_px = _floor_tick(center - half - bid_push, self.tick)
        ask_px = _ceil_tick(center + half + ask_push, self.tick)
        if ask_px <= bid_px:
            ask_px = bid_px + self.tick

        # Prices must stay in (0, 1) — this venue is a probability, not a price.
        bid_px = max(self.tick, min(bid_px, 1.0 - self.tick))
        ask_px = max(self.tick, min(ask_px, 1.0 - self.tick))

        base = self.size * scale
        bid_sz = ask_sz = base

        # Inventory: soft refusal above, hard stop at the cushion.
        limit = self.effective_limit()
        if position >= self._soft_inv:
            bid_sz = 0.0
        if position <= -self._soft_inv:
            ask_sz = 0.0
        bid_sz = max(0.0, min(bid_sz, limit - position))
        ask_sz = max(0.0, min(ask_sz, limit + position))

        # Toxicity: shrink the side being picked off.
        if bid_sz:
            bid_sz = bid_sz / (1 + self._tox_shrink * max(0.0, net))
        if ask_sz:
            ask_sz = ask_sz / (1 + self._tox_shrink * max(0.0, -net))

        # Reward floor: a quote below min_size earns nothing but still carries
        # full inventory risk. Pull it rather than rest a size that cannot pay.
        if bid_sz < self.min_size:
            bid_sz = 0.0
        if ask_sz < self.min_size:
            ask_sz = 0.0

        if bid_sz == 0.0 and ask_sz == 0.0:
            BUS.node_state(f"mm:{cid}", quoting=False, reason="size_below_reward_floor")
            return Quote(reason="size_below_reward_floor")

        BUS.node_state(f"mm:{cid}", quoting=True, reason=None,
                       half=round(half, 4), band=self.band,
                       band_use=round(half / self.band, 3) if self.band else 0,
                       position=position, markout=self.markout.average())
        return Quote(bid_px=bid_px, bid_sz=bid_sz,
                     ask_px=ask_px, ask_sz=ask_sz)

    def health(self) -> dict[str, Any]:
        return {
            "tick": self.tick_index,
            "markout": self.markout.average(),
            "tox_bid": round(self.tox_bid, 2),
            "tox_ask": round(self.tox_ask, 2),
            "killed": self.killed,
        }


def _floor_tick(px: float, tick: float) -> float:
    if not math.isfinite(px):
        raise ValueError(f"non finite price {px}: check the mark")
    return math.floor(px / tick + 1e-9) * tick


def _ceil_tick(px: float, tick: float) -> float:
    if not math.isfinite(px):
        raise ValueError(f"non finite price {px}: check the mark")
    return math.ceil(px / tick - 1e-9) * tick
