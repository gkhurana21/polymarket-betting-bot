"""Market selection for THIN-market liquidity provision.

The reward-based `Scanner` ranks by `pool / depth` — reward efficiency. That
is the right objective when income is liquidity rewards, which is what Friday
was built for and what FINDINGS.md rejected: rewards covered 17% of measured
adverse selection.

This ranks a different business. In thin markets ($200-$10k/day) the spread is
1.6c median and 15c at p95, versus ~0.1c on liquid books, so the income is the
SPREAD, not rewards. Markout measured on ~10,000 real fills is POSITIVE for the
maker (+0.179c at 1h), meaning the flow is uninformed — the opposite of what
killed the reward strategy.

  see: polymarket betting bot/research/FINDINGS_thin_markout.md

────────────────────────────────────────────────────────────────────────────
The constraint that decides this, and why it is here rather than in Scanner
────────────────────────────────────────────────────────────────────────────
A maker earns half the spread only when filled on BOTH sides. Filled once, you
are not making a market — you are holding inventory and a directional bet you
never intended.

Measured buy/sell imbalance across 83 thin markets: median 38.9%, p75 90.1%,
**p90 100%**. At p90 every trade in the market is on one side. Only 30 of 83
were round-trippable at all.

So flow balance is not a refinement here, it is the primary filter, and it does
not exist in the reward-based scanner because reward income does not require a
round trip — resting alone earns it. That difference is the whole reason this
is a separate class rather than a flag.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

TRADES = "https://data-api.polymarket.com/trades?market={cid}&limit=500"
GAMMA = "https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset={off}"


@dataclass(frozen=True)
class ThinCandidate:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str | None
    mid: float
    best_bid: float
    best_ask: float
    spread: float
    volume_24h: float
    imbalance: float          # |buys-sells| / total, 0 = perfectly balanced
    trades_seen: int
    top_depth_shares: float
    score: float

    @property
    def half_spread_available(self) -> float:
        """What we could capture per fill quoting one tick inside the touch."""
        return max(0.0, self.spread / 2.0)


def _fetch_json(url: str, timeout: float = 20.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "friday/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def flow_imbalance(cid: str, token_id: str) -> tuple[float, int]:
    """Directional imbalance of recent flow on OUR token.

    Returns (imbalance, n_trades). Imbalance 0.0 is perfectly two-sided; 1.0
    means every trade was on one side and a maker there accumulates inventory
    without ever completing a round trip.

    Only trades whose `asset` matches `token_id` are counted. data-api returns
    trades for BOTH outcome tokens of a market, and mixing them silently
    inverts the side of half the sample — that exact mistake produced a
    confident, sign-flipped markout result before it was caught.
    """
    rows = _fetch_json(TRADES.format(cid=cid))
    if not isinstance(rows, list) or not rows:
        return (1.0, 0)
    buys = sells = 0
    for t in rows:
        if str(t.get("asset") or "") != str(token_id):
            continue
        side = str(t.get("side") or "").upper()
        if side == "BUY":
            buys += 1
        elif side == "SELL":
            sells += 1
    total = buys + sells
    if total == 0:
        return (1.0, 0)
    return (abs(buys - sells) / total, total)


def fetch_open_universe(max_markets: int) -> list[dict[str, Any]]:
    """All OPEN markets from Gamma, paginated.

    NOT `client.get_active_markets()`. That calls CLOB's sampling-markets,
    which by its own docstring "returns only those currently sampled for
    liquidity rewards" -- i.e. the LIQUID markets. Wiring a thin-market
    scanner to it would have silently excluded the entire population being
    studied, the same way volume>=$10k filters scoped six earlier hypotheses
    to the liquid 3.6% of the venue.

    Gamma caps a page at 100 regardless of `limit`, so this must paginate by
    offset; asking for more in one call silently returns 100.
    """
    out: list[dict[str, Any]] = []
    off = 0
    while off < max_markets:
        page = _fetch_json(GAMMA.format(off=off))
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
        off += 100
    return out[:max_markets]


def _gamma_tokens(market: dict[str, Any]) -> tuple[str | None, str | None]:
    """Gamma encodes token ids as a JSON STRING, not a list."""
    raw = market.get("clobTokenIds")
    if not raw:
        return (None, None)
    try:
        toks = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return (None, None)
    if not isinstance(toks, list) or len(toks) != 2:
        return (None, None)
    return (str(toks[0]), str(toks[1]))


class ThinScanner:
    def __init__(self, client, config: dict[str, Any]) -> None:
        self._client = client
        sc = (config.get("thin_scanner") or {})
        self._min_vol = float(sc.get("min_volume_24h_usd", 200))
        self._max_vol = float(sc.get("max_volume_24h_usd", 10000))
        self._min_spread = float(sc.get("min_spread", 0.006))
        self._max_spread = float(sc.get("max_spread", 0.25))
        self._max_imbalance = float(sc.get("max_flow_imbalance", 0.20))
        self._min_trades = int(sc.get("min_recent_trades", 20))
        self._min_depth = float(sc.get("min_top_depth_shares", 50))
        self._top_n = int(sc.get("top_n", 4))
        self._workers = int(sc.get("book_fetch_workers", 6))
        self._max_scan = int(sc.get("max_markets_scan", 300))

    # -- scoring ------------------------------------------------------------

    def _score(self, market: dict[str, Any]) -> ThinCandidate | None:
        vol = _f(market.get("volume24hr"))
        if vol is None or not (self._min_vol <= vol < self._max_vol):
            return None
        yes, no = _gamma_tokens(market)
        if not yes:
            return None
        try:
            book = self._client.get_order_book(yes)
        except Exception:
            return None
        bids = _levels(book.get("bids"))
        asks = _levels(book.get("asks"))
        if not bids or not asks:
            return None                      # one-sided: no mid, no quoting

        best_bid = max(p for p, _ in bids)
        best_ask = min(p for p, _ in asks)
        if not (0.0 < best_bid < best_ask < 1.0):
            return None
        spread = best_ask - best_bid
        if not (self._min_spread <= spread <= self._max_spread):
            return None
        # A spread that wide with no depth behind it is a stub quote, not a
        # market. Two stub orders around an empty book fabricated a t=-3.31 in
        # the longshot study; the same shape here would fabricate an edge.
        top_depth = min(
            sum(s for p, s in bids if p >= best_bid - 0.01),
            sum(s for p, s in asks if p <= best_ask + 0.01),
        )
        if top_depth < self._min_depth:
            return None

        cid = market.get("conditionId") or market.get("condition_id") or ""
        imb, n = flow_imbalance(cid, yes)
        if n < self._min_trades or imb > self._max_imbalance:
            return None                      # cannot round-trip: see module doc

        # Opportunity ~ what we capture per fill x how much flow exists.
        # Deliberately NOT multiplied by an assumed capture rate: that number
        # is unknown, and folding a guess into the ranking hides the guess.
        shares_per_day = vol / max(0.5 * (best_bid + best_ask), 1e-6)
        return ThinCandidate(
            condition_id=cid,
            question=(market.get("question") or "")[:80],
            yes_token_id=yes, no_token_id=no,
            mid=0.5 * (best_bid + best_ask),
            best_bid=best_bid, best_ask=best_ask, spread=spread,
            volume_24h=vol, imbalance=imb, trades_seen=n,
            top_depth_shares=top_depth,
            score=(spread / 2.0) * shares_per_day,
        )

    def scan(self) -> list[ThinCandidate]:
        markets = fetch_open_universe(self._max_scan)
        out: list[ThinCandidate] = []
        with cf.ThreadPoolExecutor(max_workers=self._workers) as ex:
            for c in ex.map(self._score, markets):
                if c is not None:
                    out.append(c)
        out.sort(key=lambda c: c.score, reverse=True)
        _log.info("thin scan: %d fetched, %d qualify (balanced flow, real depth)",
                  len(markets), len(out))
        return out

    def select(self, ranked: list[ThinCandidate]) -> list[ThinCandidate]:
        return ranked[:self._top_n]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _levels(raw) -> list[tuple[float, float]]:
    """Accept both CLOB shapes: {"price","size"} dicts and [price,size] lists."""
    out = []
    for a in raw or []:
        try:
            if isinstance(a, dict):
                out.append((float(a["price"]), float(a["size"])))
            else:
                out.append((float(a[0]), float(a[1])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def _tokens(market: dict[str, Any]) -> tuple[str | None, str | None]:
    toks = market.get("tokens") or []
    yes = no = None
    for t in toks:
        out = str(t.get("outcome", "")).strip().lower()
        if out == "yes":
            yes = t.get("token_id")
        elif out == "no":
            no = t.get("token_id")
    if yes is None and len(toks) == 2:
        yes, no = toks[0].get("token_id"), toks[1].get("token_id")
    return yes, no
