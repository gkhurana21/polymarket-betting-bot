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
