"""Opportunity funnel: scan -> filter -> rank -> gate -> (execute).

One path from "what does the venue show" to "is this a trade", with the
drop-off visible at every stage. Detection was never the hard part in this
project; knowing WHY a candidate dies was.

Every filter here encodes a mistake that already produced a confident, wrong
number:

  SPREAD      A book quoting bid 0.001 / ask 0.998 is two stub orders around
              an empty book -- executable, economically meaningless. Pricing
              those as 99.8% favourites fabricated a t = -3.31.
  DEPTH       Violations found at 32 and 5 shares were worth $0.90 and $0.19.
              An edge with no size behind it is not an edge.
  DIRECTION   "reach 2.0% or lower" contains "reach" but inverts on the tail.
              Getting this backwards reported four Fed violations that did not
              exist.
  COMPLETE    Buying 3 legs of a 6-leg exhaustive set guarantees nothing. This
              produced three separate false positives, including a +62% one.
  FEES        Charged PER LEG at 0.05*p*(1-p) while the edge is per-SET. Six
              legs of fee ate a 2.8c violation whole.
  CAPITAL     THE decisive one. A binary settles at $1, so guaranteeing $1
              back means posting ~$1. Return IS the edge -- cents on a dollar,
              unleverageable. Ranking by gross edge instead of return on
              capital is how a 0.4c "opportunity" on 6,000 shares looks
              exciting and pays 0.76% annualised.

Nothing here places an order. `decide()` returns intents; execution stays
behind executor.py and config dry_run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TAKER_FEE_RATE = 0.05


@dataclass(frozen=True)
class Leg:
    """One side of a trade, priced at an executable quote with real size."""
    token_id: str
    question: str
    side: str            # "BUY_YES" | "BUY_NO"
    price: float         # what we actually pay per share
    size_shares: float   # resting size available at that price
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Opportunity:
    """A candidate trade, whatever scanner produced it."""
    kind: str                     # "monotonicity" | "complement" | "exclusive" ...
    legs: list[Leg]
    guaranteed_payout: float      # worst-case payout per set, in $
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def shares(self) -> float:
        return min(l.size_shares for l in self.legs) if self.legs else 0.0

    @property
    def cost_per_set(self) -> float:
        return sum(l.price for l in self.legs)

    @property
    def fees_per_set(self) -> float:
        """Per LEG, not per set. This is what kills multi-leg arbitrage."""
        return sum(TAKER_FEE_RATE * l.price * (1.0 - l.price) for l in self.legs)

    @property
    def gross_per_set(self) -> float:
        return self.guaranteed_payout - self.cost_per_set

    @property
    def net_per_set(self) -> float:
        return self.gross_per_set - self.fees_per_set

    @property
    def capital(self) -> float:
        """What we must post. NOT the edge -- the whole cost of every leg."""
        return self.cost_per_set * self.shares

    @property
    def net_usd(self) -> float:
        return self.net_per_set * self.shares

    @property
    def return_on_capital(self) -> float:
        return self.net_usd / self.capital if self.capital > 0 else 0.0

    def annualised(self, days_to_resolution: float) -> float:
        """Simple, NOT compounded.

        Compounding a thin edge over a year produced +2,270,324% once. Simple
        scaling is the honest comparison against a T-bill.
        """
        if days_to_resolution <= 0:
            return float("nan")
        return self.return_on_capital * (365.0 / days_to_resolution)


@dataclass
class FunnelConfig:
    max_spread: float = 0.15
    min_shares: float = 50.0
    min_net_usd: float = 1.0
    min_return_on_capital: float = 0.0
    min_annualised: float = 0.06       # must beat a T-bill to be worth capital
    max_capital_per_trade: float = 2000.0
    max_total_capital: float = 10000.0
    require_positive_after_fees: bool = True


STAGES = ["scanned", "spread", "depth", "fees", "capital", "return", "sized"]


class Funnel:
    """Narrows candidates and records why each one died."""

    def __init__(self, cfg: FunnelConfig | None = None) -> None:
        self.cfg = cfg or FunnelConfig()
        self.counts: dict[str, int] = {s: 0 for s in STAGES}
        self.rejects: dict[str, int] = {}

    def _drop(self, reason: str) -> None:
        self.rejects[reason] = self.rejects.get(reason, 0) + 1

    def filter_one(self, o: Opportunity, days: float) -> str | None:
        """Return None if it survives, else the stage that killed it."""
        c = self.cfg
        if any(l.spread > c.max_spread for l in o.legs):
            self._drop("spread: stub book, not a price")
            return "spread"
        if o.shares < c.min_shares:
            self._drop("depth: edge with no size behind it")
            return "depth"
        if c.require_positive_after_fees and o.net_per_set <= 0:
            self._drop(f"fees: per-leg fee exceeds per-set edge ({len(o.legs)} legs)")
            return "fees"
        if o.net_usd < c.min_net_usd:
            self._drop("capital: net too small to be worth an order")
            return "capital"
        ann = o.annualised(days)
        if ann != ann or ann < c.min_annualised:
            self._drop(f"return: {ann*100:.2f}% annualised, below T-bill")
            return "return"
        return None

    def run(self, opps: list[tuple[Opportunity, float]]) -> list[Opportunity]:
        """opps = [(opportunity, days_to_resolution)]. Returns survivors,
        best return-on-capital first, capped by the capital budget."""
        self.counts["scanned"] = len(opps)
        survivors: list[tuple[Opportunity, float]] = []
        for o, days in opps:
            stage = self.filter_one(o, days)
            if stage is None:
                survivors.append((o, days))
            else:
                # everything before the killing stage counts as passed
                for s in STAGES[1:]:
                    if s == stage:
                        break
                    self.counts[s] += 1
                continue
            for s in STAGES[1:-1]:
                self.counts[s] += 1

        # Rank by RETURN ON CAPITAL, never gross edge. A 0.4c edge on 6,000
        # shares is $15 on $6,000 -- it looks big and pays 0.76% annualised.
        survivors.sort(key=lambda t: t[0].annualised(t[1]), reverse=True)

        out, spent = [], 0.0
        for o, _d in survivors:
            if o.capital > self.cfg.max_capital_per_trade:
                self._drop("capital: exceeds per-trade cap")
                continue
            if spent + o.capital > self.cfg.max_total_capital:
                self._drop("capital: portfolio budget exhausted")
                continue
            spent += o.capital
            out.append(o)
        self.counts["sized"] = len(out)
        return out

    def report(self) -> str:
        lines = ["", "── funnel ──"]
        prev = None
        for s in STAGES:
            n = self.counts[s]
            arrow = "" if prev is None else f"  ({n - prev:+d})"
            lines.append(f"  {s:<10} {n:>6}{arrow}")
            prev = n
        if self.rejects:
            lines.append("")
            lines.append("  why they died:")
            for r, n in sorted(self.rejects.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {n:>5}  {r}")
        return "\n".join(lines)
