"""Dataclass models. Implements PLAN.md §2 bot/models.py.

Mutability rules per the build directive:
  * Immutable observations (frozen=True): WhaleSignal, MarketSnapshot,
    BookSnapshot, ReadinessCheck, ReadinessReport
  * Mutable state: PaperTrade (post-resolution updates of resolved_at_ts /
    resolved_outcome / pnl_usd), PendingPaperTrade (status transitions
    pending → materialised | voided during sweep)

Conventions:
  * Timestamps are int milliseconds since the Unix epoch — never datetime.
  * Side / outcome / signal_type / status are str literals; valid values
    enumerated in each field's docstring and originate in PLAN.md §6 schema.
  * Prices and USD amounts are float.
  * `id` is None pre-insert and populated by Tracker on insert.
  * No methods beyond what @dataclass and dataclasses_json provide. If
    something needs validation, a separate validate() helper goes in
    utils/helpers.py per build directive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dataclasses_json import DataClassJsonMixin


@dataclass(frozen=True)
class WhaleSignal(DataClassJsonMixin):
    """One observed whale fill, classified by signal_type.

    Implements PLAN.md §6 whale_signals schema and §7 signal_type classifier.

    side ∈ {"YES", "NO"}
    signal_type ∈ {"open", "add", "exit", "flip"}
    """
    whale_address: str
    market_id: str
    side: str
    signal_type: str
    price: float
    size_usd: float
    whale_fill_ts: int           # ms epoch from CLOB
    detected_at_ts: int          # ms epoch our clock at receipt
    detection_latency_ms: int    # detected_at_ts − whale_fill_ts
    raw_json: str                # audit trail
    whale_label: str | None = None
    transaction_hash: str | None = None  # chain tx hash; UNIQUE in DB. None on
                                         # synthetic test signals; production
                                         # whale_tracker always sets it.
    id: int | None = None        # set by Tracker post-insert


@dataclass(frozen=True)
class MarketSnapshot(DataClassJsonMixin):
    """One 10-min sample of a filtered market's state.

    Implements PLAN.md §6 market_snapshots schema.
    """
    market_id: str
    ts: int
    mid: float
    spread_bps: float | None = None
    volume_24h_usd: float | None = None
    liquidity_usd: float | None = None
    category: str | None = None
    question: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class BookSnapshot(DataClassJsonMixin):
    """Full order book to depth N at a point in time.

    Implements PLAN.md §6 book_snapshots schema (depth 10 per side per
    config). `book_json` holds the JSON string
    `{"bids":[[price,size]…N],"asks":[…N]}` stored verbatim in the DB;
    consumers parse on read.
    """
    market_id: str
    ts: int
    book_json: str


@dataclass
class PaperTrade(DataClassJsonMixin):
    """One paper-trade entry from one of the four streams.

    Implements PLAN.md §6 paper_trades schema and §8 paper streams.

    strategy_id ∈ {"copy_whale", "stub_blend", "null_random_side",
                   "whale_side_random_timing"}
    side ∈ {"YES", "NO"}
    resolved_outcome ∈ {None, "YES", "NO", "INVALID",
                        "VOIDED_PRE_FILL", "VOIDED_MISSED_WINDOW"}

    Mutable: resolved_at_ts, resolved_outcome, pnl_usd are written by the
    resolution poller / paper-trader sweep AFTER row insertion. entry_price
    is None for VOIDED_* rows per PLAN.md §8 mechanics.
    """
    strategy_id: str
    triggering_signal_id: int
    market_id: str
    side: str
    size_usd: float
    decided_at_ts: int
    entry_price: float | None = None
    resolved_at_ts: int | None = None
    resolved_outcome: str | None = None
    pnl_usd: float | None = None
    id: int | None = None


@dataclass
class PendingPaperTrade(DataClassJsonMixin):
    """Deferred decision row for the whale_side_random_timing stream.

    Implements PLAN.md §6 pending_paper_trades schema and §8 mechanics.

    status ∈ {"pending", "materialised", "voided"}
    Mutable: status transitions on each sweep tick.
    """
    strategy_id: str
    triggering_signal_id: int
    market_id: str
    side: str
    size_usd: float
    scheduled_for_ts: int
    status: str
    created_at_ts: int
    id: int | None = None


@dataclass(frozen=True)
class BetIntent(DataClassJsonMixin):
    """A sizing decision produced by strategy.py before risk gating.

    side ∈ {"YES", "NO"}
    strategy_id: always "live_copy_whale" in Phase B
    token_id: the YES/NO token to buy (resolved from market_token_map)
    limit_price: mid-price at signal time (executor may add offset_bps)
    size_usd: Kelly-sized notional in USD
    risk_token_hash: SHA-256 of (market_id, side, whale_address, whale_fill_ts)
      used to deduplicate across crashes and detect duplicate market blocks
    """
    whale_address: str
    market_id: str
    token_id: str
    side: str
    strategy_id: str
    limit_price: float
    size_usd: float
    risk_token_hash: str
    whale_fill_ts: int
    intent_json: str          # full JSON blob for audit / live_orders.intent_json
    triggering_signal_id: int
    created_at_ts: int


@dataclass
class LiveOrder(DataClassJsonMixin):
    """One live CLOB order row, mirroring the live_orders DB table.

    status ∈ {"open", "filled", "cancelled", "error"}
    Mutable: status / filled_at_ts / fill_price updated by executor on callback.
    """
    clob_order_id: str
    market_id: str
    side: str
    limit_price: float
    size_usd: float
    status: str
    placed_at_ts: int
    risk_token_hash: str
    intent_json: str
    filled_at_ts: int | None = None
    fill_price: float | None = None
    id: int | None = None


# ───────────────────────  binary complement arbitrage  ───────────────────────
#  Research-only models. A binary market's YES + NO shares must together pay
#  exactly $1 at resolution, so buying equal quantities of both for materially
#  less than $1 would lock a return independent of the outcome. These records
#  capture whether that was ever true at EXECUTABLE (ask-side) prices, with
#  enough evidence attached to review any candidate by hand later.


@dataclass(frozen=True)
class ArbitrageLeg(DataClassJsonMixin):
    """One side of a complement pair, priced by walking the ask book.

    `vwap_price` is the volume-weighted average price actually required to buy
    `shares`, not a midpoint or a top-of-book quote. `levels_used` records how
    deep the fill had to reach — a fill spanning many levels is a warning that
    the quoted edge depends on thin outer levels.

    side ∈ {"YES", "NO"}
    """
    token_id: str
    side: str
    shares: float
    vwap_price: float
    cost_usd: float
    levels_used: int


@dataclass(frozen=True)
class ArbitrageOpportunity(DataClassJsonMixin):
    """One observed complement-pair candidate, executable or rejected.

    Rejected candidates are persisted deliberately: the research question is
    how often an opportunity exists and how large it is, and that cannot be
    answered from successes alone.

    reject_reason ∈ {None, "missing_token", "one_sided_book",
                     "insufficient_yes_depth", "insufficient_no_depth",
                     "stale_book", "not_profitable", "edge_below_threshold"}
    """
    market_id: str
    question: str | None
    observed_at_ts: int
    yes_leg: ArbitrageLeg
    no_leg: ArbitrageLeg
    equal_shares: float
    gross_payout_usd: float
    gross_cost_usd: float
    estimated_fees_usd: float
    estimated_slippage_usd: float
    net_locked_pnl_usd: float
    net_edge_bps: float
    book_age_ms: int | None
    executable: bool
    reject_reason: str | None
    evidence_json: str
    id: int | None = None


@dataclass(frozen=True)
class ArbitrageScanError(DataClassJsonMixin):
    """One per-market failure during a scan tick."""
    market_id: str
    error_type: str
    message: str


@dataclass
class ArbitrageScanResult(DataClassJsonMixin):
    """Per-tick summary returned by ArbitrageScanner.scan_once."""
    markets_considered: int = 0
    markets_eligible: int = 0
    opportunities_found: int = 0
    executable_opportunities: int = 0
    errors: int = 0
    total_executable_notional_usd: float = 0.0
    error_details: list[ArbitrageScanError] = field(default_factory=list)


@dataclass(frozen=True)
class ReadinessCheck(DataClassJsonMixin):
    """One row of the calibration readiness gate.

    Implements PLAN.md §11. `current` and `required` are float for uniform
    handling whether the underlying check is a count, a ratio, or a day
    delta.
    """
    name: str
    required: float
    current: float
    passed: bool


@dataclass(frozen=True)
class ReadinessReport(DataClassJsonMixin):
    """Full calibration readiness report.

    Implements PLAN.md §11. `summary` is the one-line human-readable string
    rendered by `python main.py status`. The `checks` list is treated as
    immutable by callers; `frozen=True` prevents reassigning the field but
    Python does not deep-freeze the contents — by convention, do not mutate
    after construction.
    """
    checks: list[ReadinessCheck]
    all_passed: bool
    summary: str
