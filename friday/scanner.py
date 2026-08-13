"""Market selection by reward efficiency.

THE RANKING RULE, and why it is not pool size:

Polymarket splits each market's daily reward pool pro-rata among makers
resting inside the band. Your share is

    your_size / total_qualifying_size

so the pool is the numerator and the *resting depth inside the band* is the
denominator. Ranking by pool size alone walks straight into the most crowded
books: measured live, "Will the Democrats win the 2028 US Presidential
Election" carries a pool but 420,844 shares of resting depth, which pays a
200-share maker essentially nothing. Meanwhile a $3/day Alabama governor
market with 3,881 shares of depth pays ~10x more per dollar deployed.

Score = reward_daily_rate / resting_depth_inside_band.
"""

from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass
from typing import Any

from friday.client import ClobReadOnlyClient
from friday.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class MarketCandidate:
    """One scored market. `score` is reward-per-unit-of-competition."""
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str | None
    pool_usd_per_day: float
    max_spread: float          # PRICE units (API gives cents)
    min_size_shares: float
    depth_inside_band: float   # shares resting within max_spread of mid
    mid: float
    best_bid: float
    best_ask: float
    score: float

    def implied_share(self, our_shares: float) -> float:
        """Our pro-rata fraction of the pool if we rest `our_shares`.

        Our own size joins the denominator — a maker who ignores that
        overestimates their take, badly, in thin books.
        """
        denom = self.depth_inside_band + our_shares
        return (our_shares / denom) if denom > 0 else 0.0

    def expected_daily_usd(self, our_shares: float) -> float:
        return self.pool_usd_per_day * self.implied_share(our_shares)

    def our_book_share(self, our_shares: float) -> float:
        """Fraction of the post-quote book that would be ours."""
        denom = self.depth_inside_band + our_shares
        return (our_shares / denom) if denom > 0 else 1.0


def _tokens(market: dict[str, Any]) -> tuple[str | None, str | None]:
    yes = no = None
    for t in (market.get("tokens") or []):
        label = (t.get("outcome") or "").strip().lower()
        if label == "yes":
            yes = t.get("token_id")
        elif label == "no":
            no = t.get("token_id")
    return yes, no


def _pool_and_band(market: dict[str, Any]) -> tuple[float, float, float]:
    """Returns (pool_usd_per_day, max_spread_in_price, min_size_shares)."""
    rw = market.get("rewards") or {}
    pool = 0.0
    for r in (rw.get("rates") or []):
        try:
            pool += float(r.get("rewards_daily_rate") or 0)
        except (TypeError, ValueError):
            continue
    # API expresses max_spread in CENTS; everything downstream is in price units.
    try:
        band = float(rw.get("max_spread") or 0) / 100.0
    except (TypeError, ValueError):
        band = 0.0
    try:
        min_size = float(rw.get("min_size") or 0)
    except (TypeError, ValueError):
        min_size = 0.0
    return pool, band, min_size


def depth_inside_band(bids, asks, mid: float, band: float) -> float:
    """Total resting shares within `band` of mid, both sides.

    This is the pro-rata denominator we are competing against.
    """
    d = 0.0
    for px, sz in bids:
        if abs(px - mid) <= band:
            d += sz
    for px, sz in asks:
        if abs(px - mid) <= band:
            d += sz
    return d


class Scanner:
    def __init__(self, client: ClobReadOnlyClient, config: dict[str, Any]) -> None:
        self._client = client
        sc = config.get("scanner") or {}
        self._max_scan = int(sc.get("max_markets_scan", 500))
        self._min_pool = float(sc.get("min_pool_usd_per_day", 1.0))
        self._workers = int(sc.get("book_fetch_workers", 8))
        self._top_n = int(sc.get("top_n", 2))
        self._hysteresis = float(sc.get("hysteresis_margin", 0.25))
        self._min_depth = float(sc.get("min_depth_shares", 100))
        self._max_book_share = float(sc.get("max_our_book_share", 0.60))

    # -- scoring ------------------------------------------------------------

    def _score_market(self, market: dict[str, Any]) -> MarketCandidate | None:
        pool, band, min_size = _pool_and_band(market)
        if pool < self._min_pool or band <= 0:
            return None
        yes, no = _tokens(market)
        if not yes:
            return None
        try:
            book = self._client.get_order_book(yes)
        except Exception as e:
            _log.debug("scanner: book fetch failed for %s: %s",
                       market.get("condition_id", "")[:16], e)
            return None

        bids = [(float(b[0]), float(b[1])) for b in (book.get("bids") or [])]
        asks = [(float(a[0]), float(a[1])) for a in (book.get("asks") or [])]
        if not bids or not asks:
            return None                       # one-sided: no mid, no quoting

        best_bid = max(p for p, _ in bids)
        best_ask = min(p for p, _ in asks)
        mid = 0.5 * (best_bid + best_ask)
        depth = depth_inside_band(bids, asks, mid, band)
        if depth <= 0:
            return None

        return MarketCandidate(
            condition_id=market.get("condition_id", ""),
            question=(market.get("question") or "")[:80],
            yes_token_id=yes,
            no_token_id=no,
            pool_usd_per_day=pool,
            max_spread=band,
            min_size_shares=min_size,
            depth_inside_band=depth,
            mid=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            score=pool / depth,
        )

    def scan(self) -> list[MarketCandidate]:
        """All scoreable markets, best reward-efficiency first."""
        markets = self._client.get_active_markets(limit=self._max_scan)
        out: list[MarketCandidate] = []
        with cf.ThreadPoolExecutor(max_workers=self._workers) as ex:
            for c in ex.map(self._score_market, markets):
                if c is not None:
                    out.append(c)
        out.sort(key=lambda c: c.score, reverse=True)
        _log.info("scanner: %d markets fetched, %d scoreable", len(markets), len(out))
        return out

    # -- selection ----------------------------------------------------------

    def viable(self, cand: MarketCandidate, our_shares: float) -> bool:
        """Reject markets where our own quote would BE the book.

        Reward efficiency and safety pull in opposite directions: the thinnest
        books pay the most per share precisely because nobody else will stand
        there. Measured live, a $2/day market showed 22 shares of resting
        depth — posting 400 shares makes us 95% of the book, so every fill we
        get is someone hitting the only bid available, and there is no other
        liquidity to exit against. That is adverse selection by construction,
        and no toxicity parameter rescues it.

        Two gates: an absolute depth floor, and a cap on how much of the
        resulting book is ours.
        """
        if cand.depth_inside_band < self._min_depth:
            return False
        return cand.our_book_share(our_shares) <= self._max_book_share

    def select(
        self,
        ranked: list[MarketCandidate],
        our_shares: float,
        incumbents: set[str] | None = None,
    ) -> list[MarketCandidate]:
        """Top N, with hysteresis so we do not churn queue position.

        An incumbent keeps its slot unless a challenger beats it by
        `hysteresis_margin`. Re-quoting a different market forfeits queue
        position, and queue position is what earns the reward — so a marginal
        ranking change must not be allowed to trigger a move.
        """
        ranked = [c for c in ranked if self.viable(c, our_shares)]
        if not ranked:
            return []
        if not incumbents:
            return ranked[: self._top_n]

        chosen = [c for c in ranked if c.condition_id in incumbents][: self._top_n]
        chosen_ids = {c.condition_id for c in chosen}
        for cand in ranked:
            if len(chosen) >= self._top_n:
                break
            if cand.condition_id in chosen_ids:
                continue
            chosen.append(cand)
            chosen_ids.add(cand.condition_id)

        # Displacement pass: challenger must clear the bar by the margin.
        if len(ranked) > self._top_n:
            for cand in ranked[: self._top_n * 3]:
                if cand.condition_id in chosen_ids:
                    continue
                weakest = min(chosen, key=lambda c: c.score)
                if cand.score > weakest.score * (1.0 + self._hysteresis):
                    _log.info(
                        "scanner: displacing %s (score %.3f) with %s (score %.3f)",
                        weakest.question[:32], weakest.score,
                        cand.question[:32], cand.score,
                    )
                    chosen.remove(weakest)
                    chosen_ids.discard(weakest.condition_id)
                    chosen.append(cand)
                    chosen_ids.add(cand.condition_id)
        return chosen[: self._top_n]
