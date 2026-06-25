"""SQLite + audit-log persistence layer. Implements PLAN.md §2 bot/tracker.py
and the schema in PLAN.md §6.

Threading model per build directive:
  * Single sqlite3 connection per Tracker instance, opened with
    check_same_thread=False so the main loop and the whale poller can both
    write through the same instance.
  * WAL mode + foreign keys ON + synchronous=NORMAL applied on open
    (verified in module-3 smoke).
  * Application-level threading.Lock (`_write_lock`) serialises every
    write. SQLite's WAL permits concurrent reads, but with a single shared
    connection we must serialise writes at the application level.
  * `isolation_level=None` puts the sqlite3 driver in autocommit mode and
    disables its implicit-BEGIN magic; we manage transactions explicitly
    with BEGIN / COMMIT / ROLLBACK around every write.

Audit log:
  * Append-only JSONL at config.bot.audit_log_path (default
    ./data/audit.log.jsonl). Every record_* / resolve_* / void_* /
    materialise_* / update_* method writes a corresponding event line.
  * Convention: the audit log is the source of truth; SQLite is a
    queryable index. In Phase A we write DB-then-audit (rather than
    audit-then-DB) because auto-increment ids are not known until after
    DB insert. If audit write fails after a DB commit succeeded, the
    method raises but the DB row remains — operator intervention required.
  * Sharper consequence under caller retry: a DB-success-then-audit-fail
    raise will be retried by the caller, producing a duplicate DB row
    (different rowid, same payload) plus a single successful audit line.
    The DB then has a phantom row with no audit counterpart. Acceptable
    for Phase A (audit failures should be vanishingly rare on local
    disk; observation mode tolerates duplicate noise). Resolved in
    Phase B by switching to UUID primary keys and audit-first ordering
    so retries are idempotent end-to-end.

Phase B tables (live_orders) are created via CREATE TABLE IF NOT EXISTS so
the schema is forward-compatible, but no Phase A method writes to them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bot.models import (
    BookSnapshot,
    MarketSnapshot,
    PaperTrade,
    PendingPaperTrade,
    ReadinessCheck,
    ReadinessReport,
    WhaleSignal,
)
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)


# Beta(7,3) prior per PLAN.md §6 trust_score.prior_alpha / prior_beta.
# Hardcoded here so Tracker doesn't need the full config dict at construction;
# kept in sync with config.yaml by convention. If you change one, change both.
_TRUST_PRIOR_ALPHA: float = 7.0
_TRUST_PRIOR_BETA: float = 3.0


_SCHEMA_SQL = """
-- ─────────────────────────  Phase A tables  ─────────────────────────

CREATE TABLE IF NOT EXISTS whale_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  whale_address TEXT NOT NULL,
  whale_label TEXT,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,                       -- YES | NO
  signal_type TEXT NOT NULL,                -- open | add | exit | flip
  price REAL NOT NULL,
  size_usd REAL NOT NULL,
  whale_fill_ts INTEGER NOT NULL,
  detected_at_ts INTEGER NOT NULL,
  detection_latency_ms INTEGER NOT NULL,
  transaction_hash TEXT UNIQUE,             -- chain tx hash; UNIQUE for idempotent
                                            -- ON CONFLICT(transaction_hash) DO NOTHING.
                                            -- NULL allowed (SQLite UNIQUE permits
                                            -- multiple NULLs) for synthetic signals.
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_whale_signals_market ON whale_signals(market_id);
CREATE INDEX IF NOT EXISTS idx_whale_signals_whale  ON whale_signals(whale_address);
CREATE INDEX IF NOT EXISTS idx_whale_signals_type   ON whale_signals(signal_type);

CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  mid REAL NOT NULL,
  spread_bps REAL,
  volume_24h_usd REAL,
  liquidity_usd REAL,
  category TEXT,
  question TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_ts ON market_snapshots(market_id, ts);

CREATE TABLE IF NOT EXISTS book_snapshots (
  market_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  book_json TEXT NOT NULL,                  -- {"bids":[[p,s]…N],"asks":[…N]}
  PRIMARY KEY (market_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_book_snapshots_market_ts_desc
    ON book_snapshots(market_id, ts DESC);

CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL,                -- copy_whale | stub_blend | null_random_side | whale_side_random_timing
  triggering_signal_id INTEGER NOT NULL,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,                       -- YES | NO
  entry_price REAL,                         -- NULL for VOIDED_* rows
  size_usd REAL NOT NULL,
  decided_at_ts INTEGER NOT NULL,
  resolved_at_ts INTEGER,
  resolved_outcome TEXT,                    -- YES | NO | INVALID | VOIDED_PRE_FILL | VOIDED_MISSED_WINDOW
  pnl_usd REAL,                             -- 0 for VOIDED_*
  FOREIGN KEY(triggering_signal_id) REFERENCES whale_signals(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy ON paper_trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_market   ON paper_trades(market_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_decided  ON paper_trades(decided_at_ts);
CREATE INDEX IF NOT EXISTS idx_paper_trades_unresolved
    ON paper_trades(market_id) WHERE resolved_outcome IS NULL;

CREATE TABLE IF NOT EXISTS pending_paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL,
  triggering_signal_id INTEGER NOT NULL,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  size_usd REAL NOT NULL,
  scheduled_for_ts INTEGER NOT NULL,
  status TEXT NOT NULL,                     -- pending | materialised | voided
  created_at_ts INTEGER NOT NULL,
  FOREIGN KEY(triggering_signal_id) REFERENCES whale_signals(id)
);
CREATE INDEX IF NOT EXISTS idx_pending_status_time
    ON pending_paper_trades(status, scheduled_for_ts);

CREATE TABLE IF NOT EXISTS whale_trust (
  whale_address TEXT PRIMARY KEY,
  alpha REAL NOT NULL,
  beta  REAL NOT NULL,
  trust_mean REAL NOT NULL,
  resolved_count INTEGER NOT NULL,
  updated_at_ts INTEGER NOT NULL,
  candidate_blacklist INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS processed_trust_updates (
  market_id TEXT NOT NULL,
  whale_signal_id INTEGER NOT NULL,
  processed_at_ts INTEGER NOT NULL,
  PRIMARY KEY (market_id, whale_signal_id)
);

-- resolved_markets: ResolutionPoller idempotency. One row per market we've
-- learned is settled — avoids re-querying CLOB on every poll tick.
CREATE TABLE IF NOT EXISTS resolved_markets (
  market_id TEXT PRIMARY KEY,
  resolved_outcome TEXT NOT NULL,           -- YES | NO | INVALID
  resolved_at_ts INTEGER NOT NULL
);

-- market_token_map: condition_id → YES token_id lookup. Populated by
-- MarketScanner on each scan tick for every market that passes book
-- filters. Consumed by paper_trader's on-demand book fetch fallback so
-- it calls get_order_book with the correct token_id rather than the
-- condition_id (which returns 404).
-- PLAN deviation (Phase A.2.1): original Fix 1 (Phase A.2) passed
-- condition_id to get_order_book, causing 821 silent 404 failures that
-- blocked stub_blend and null_random_side entirely.
CREATE TABLE IF NOT EXISTS market_token_map (
  condition_id   TEXT PRIMARY KEY,
  yes_token_id   TEXT NOT NULL,
  updated_at_ts  INTEGER NOT NULL
);

-- ─────────────────────────  Phase B tables  ─────────────────────────
-- Created so the schema is forward-compatible. NO Phase A method writes
-- to these. bot/executor.py (Phase B) will be the only writer.

CREATE TABLE IF NOT EXISTS live_orders (    -- Phase B
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clob_order_id TEXT UNIQUE,
  market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  limit_price REAL NOT NULL,
  size_usd REAL NOT NULL,
  status TEXT NOT NULL,
  placed_at_ts INTEGER NOT NULL,
  filled_at_ts INTEGER,
  fill_price REAL,
  risk_token_hash TEXT NOT NULL,
  intent_json TEXT NOT NULL
);
"""


class Tracker:
    """SQLite + audit-log persistence. Single connection, lock-serialised writes."""

    def __init__(
        self,
        db_path: str | Path,
        audit_log_path: str | Path | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        if audit_log_path is None:
            self._audit_path = self._db_path.parent / "audit.log.jsonl"
        else:
            self._audit_path = Path(audit_log_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,            # autocommit; we manage tx explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()
        self._audit_lock = threading.Lock()

        self._apply_pragmas()
        self._create_schema()
        self._migrate_schema()

    # ───────────────────────  schema / pragmas  ───────────────────────

    def _apply_pragmas(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def _create_schema(self) -> None:
        with self._write_lock:
            self._conn.executescript(_SCHEMA_SQL)

    def _migrate_schema(self) -> None:
        """Additive migrations for DBs created before module 9."""
        with self._write_lock:
            cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(whale_trust)")
            }
            if cols and "candidate_blacklist" not in cols:
                self._conn.execute(
                    "ALTER TABLE whale_trust ADD COLUMN "
                    "candidate_blacklist INTEGER NOT NULL DEFAULT 0"
                )
            # Phase B migration: add no_token_id to market_token_map.
            mtm_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(market_token_map)")
            }
            if mtm_cols and "no_token_id" not in mtm_cols:
                self._conn.execute(
                    "ALTER TABLE market_token_map ADD COLUMN no_token_id TEXT"
                )

    # ─────────────────────────────  audit  ─────────────────────────────

    def _audit_write(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one JSONL line to the audit log. See module docstring for
        the DB-then-audit ordering rationale."""
        line = json.dumps(
            {"ts": now_ms(), "event_type": event_type, "payload": payload},
            separators=(",", ":"),
            default=str,
        )
        with self._audit_lock:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ──────────────────────────  whale signals  ──────────────────────────

    def record_whale_signal(self, signal: WhaleSignal) -> int | None:
        """Insert one whale signal. Idempotent on `transaction_hash` via
        `ON CONFLICT(transaction_hash) DO NOTHING`. Returns the assigned
        rowid on insert, or `None` if a signal with the same
        transaction_hash already existed (caller should skip downstream
        emission). NULL transaction_hash never conflicts.
        """
        # TODO(phase-b): UUID PK + audit-first ordering for idempotent retry.
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO whale_signals "
                    "(whale_address, whale_label, market_id, side, signal_type, "
                    " price, size_usd, whale_fill_ts, detected_at_ts, "
                    " detection_latency_ms, transaction_hash, raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(transaction_hash) DO NOTHING",
                    (
                        signal.whale_address, signal.whale_label, signal.market_id,
                        signal.side, signal.signal_type, signal.price, signal.size_usd,
                        signal.whale_fill_ts, signal.detected_at_ts,
                        signal.detection_latency_ms, signal.transaction_hash,
                        signal.raw_json,
                    ),
                )
                inserted = cur.rowcount == 1
                rowid = int(cur.lastrowid) if inserted else None
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        if not inserted:
            return None
        payload = asdict(signal)
        payload["id"] = rowid
        self._audit_write("whale_signal", payload)
        return rowid

    def get_whale_signals_for_wallet(self, wallet: str) -> list[WhaleSignal]:
        """Return every whale signal for `wallet`, ordered chronologically
        by `whale_fill_ts ASC`. Used by `WhaleTracker.__init__` to replay
        per-(wallet, conditionId) position state from history."""
        cur = self._conn.execute(
            "SELECT id, whale_address, whale_label, market_id, side, signal_type, "
            "price, size_usd, whale_fill_ts, detected_at_ts, detection_latency_ms, "
            "transaction_hash, raw_json "
            "FROM whale_signals WHERE whale_address = ? "
            "ORDER BY whale_fill_ts ASC",
            (wallet,),
        )
        return [
            WhaleSignal(
                whale_address=r["whale_address"],
                market_id=r["market_id"],
                side=r["side"],
                signal_type=r["signal_type"],
                price=r["price"],
                size_usd=r["size_usd"],
                whale_fill_ts=r["whale_fill_ts"],
                detected_at_ts=r["detected_at_ts"],
                detection_latency_ms=r["detection_latency_ms"],
                raw_json=r["raw_json"],
                whale_label=r["whale_label"],
                transaction_hash=r["transaction_hash"],
                id=r["id"],
            )
            for r in cur.fetchall()
        ]

    # ───────────────────────  market & book snapshots  ───────────────────────

    def record_market_snapshot(self, snapshot: MarketSnapshot) -> int:
        # TODO(phase-b): UUID PK + audit-first ordering for idempotent retry.
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO market_snapshots "
                    "(market_id, ts, mid, spread_bps, volume_24h_usd, liquidity_usd, "
                    " category, question) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        snapshot.market_id, snapshot.ts, snapshot.mid, snapshot.spread_bps,
                        snapshot.volume_24h_usd, snapshot.liquidity_usd,
                        snapshot.category, snapshot.question,
                    ),
                )
                rowid = int(cur.lastrowid)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        payload = asdict(snapshot)
        payload["id"] = rowid
        self._audit_write("market_snapshot", payload)
        return rowid

    def record_book_snapshot(self, snapshot: BookSnapshot) -> int:
        """Insert one book snapshot. Composite PK is (market_id, ts) — there
        is no auto-increment id; we return `ts` as the natural identifier.
        Duplicate (market_id, ts) is silently ignored (INSERT OR IGNORE).
        """
        # TODO(phase-b): UUID PK + audit-first ordering for idempotent retry.
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO book_snapshots "
                    "(market_id, ts, book_json) VALUES (?,?,?)",
                    (snapshot.market_id, snapshot.ts, snapshot.book_json),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("book_snapshot", asdict(snapshot))
        return snapshot.ts

    # ──────────────────────────  paper trades  ──────────────────────────

    def record_paper_trade(self, trade: PaperTrade) -> int:
        # TODO(phase-b): UUID PK + audit-first ordering for idempotent retry.
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO paper_trades "
                    "(strategy_id, triggering_signal_id, market_id, side, entry_price, "
                    " size_usd, decided_at_ts, resolved_at_ts, resolved_outcome, pnl_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        trade.strategy_id, trade.triggering_signal_id, trade.market_id,
                        trade.side, trade.entry_price, trade.size_usd, trade.decided_at_ts,
                        trade.resolved_at_ts, trade.resolved_outcome, trade.pnl_usd,
                    ),
                )
                rowid = int(cur.lastrowid)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        payload = asdict(trade)
        payload["id"] = rowid
        self._audit_write("paper_trade", payload)
        return rowid

    def record_pending_paper_trade(self, pending: PendingPaperTrade) -> int:
        # TODO(phase-b): UUID PK + audit-first ordering for idempotent retry.
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO pending_paper_trades "
                    "(strategy_id, triggering_signal_id, market_id, side, size_usd, "
                    " scheduled_for_ts, status, created_at_ts) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        pending.strategy_id, pending.triggering_signal_id,
                        pending.market_id, pending.side, pending.size_usd,
                        pending.scheduled_for_ts, pending.status, pending.created_at_ts,
                    ),
                )
                rowid = int(cur.lastrowid)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        payload = asdict(pending)
        payload["id"] = rowid
        self._audit_write("pending_paper_trade", payload)
        return rowid

    # ────────────────────  resolutions / voids / fills  ────────────────────

    def resolve_paper_trade(
        self, trade_id: int, outcome: str, pnl_usd: float, resolved_at_ts: int
    ) -> None:
        """Update the three resolution columns in-place. Caller (resolution
        poller) computes pnl_usd from entry_price + outcome + size."""
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "UPDATE paper_trades SET resolved_at_ts=?, resolved_outcome=?, "
                    "pnl_usd=? WHERE id=?",
                    (resolved_at_ts, outcome, pnl_usd, trade_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(
                        f"resolve_paper_trade: id={trade_id} not found "
                        f"(rowcount={cur.rowcount})"
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("resolve_paper_trade", {
            "trade_id": trade_id, "outcome": outcome,
            "pnl_usd": pnl_usd, "resolved_at_ts": resolved_at_ts,
        })

    def void_pending_paper_trade(self, pending_id: int, reason: str) -> int:
        """Per PLAN.md §8: voids write a visible paper_trades row with
        pnl_usd=0 and entry_price=NULL, and mark the pending row 'voided'.
        Excluded from win-rate queries by the resolved_outcome IN ('YES','NO')
        filter. Returns the id of the new paper_trades row.
        """
        if reason not in ("VOIDED_PRE_FILL", "VOIDED_MISSED_WINDOW"):
            raise ValueError(
                f"reason must be VOIDED_PRE_FILL or VOIDED_MISSED_WINDOW; got {reason!r}"
            )
        ts = now_ms()
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "SELECT strategy_id, triggering_signal_id, market_id, side, "
                    "size_usd, status FROM pending_paper_trades WHERE id=?",
                    (pending_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"pending_paper_trades id={pending_id} not found")
                if row["status"] != "pending":
                    raise ValueError(
                        f"pending_paper_trades id={pending_id} status="
                        f"{row['status']!r} (must be 'pending')"
                    )
                cur = self._conn.execute(
                    "INSERT INTO paper_trades "
                    "(strategy_id, triggering_signal_id, market_id, side, entry_price, "
                    " size_usd, decided_at_ts, resolved_at_ts, resolved_outcome, pnl_usd) "
                    "VALUES (?,?,?,?,NULL,?,?,?,?,0.0)",
                    (
                        row["strategy_id"], row["triggering_signal_id"],
                        row["market_id"], row["side"], row["size_usd"],
                        ts, ts, reason,
                    ),
                )
                paper_trade_id = int(cur.lastrowid)
                self._conn.execute(
                    "UPDATE pending_paper_trades SET status='voided' WHERE id=?",
                    (pending_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("void_pending_paper_trade", {
            "pending_id": pending_id, "paper_trade_id": paper_trade_id,
            "reason": reason, "ts": ts,
        })
        return paper_trade_id

    def materialise_pending_paper_trade(
        self, pending_id: int, entry_price: float
    ) -> int:
        """Healthy materialisation per PLAN.md §8 case 4: insert a
        paper_trades row with the snapshot mid as entry_price; mark the
        pending row 'materialised'. Counterpart to void_pending_paper_trade.
        """
        ts = now_ms()
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "SELECT strategy_id, triggering_signal_id, market_id, side, "
                    "size_usd, status FROM pending_paper_trades WHERE id=?",
                    (pending_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"pending_paper_trades id={pending_id} not found")
                if row["status"] != "pending":
                    raise ValueError(
                        f"pending_paper_trades id={pending_id} status="
                        f"{row['status']!r} (must be 'pending')"
                    )
                cur = self._conn.execute(
                    "INSERT INTO paper_trades "
                    "(strategy_id, triggering_signal_id, market_id, side, "
                    " entry_price, size_usd, decided_at_ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        row["strategy_id"], row["triggering_signal_id"],
                        row["market_id"], row["side"],
                        entry_price, row["size_usd"], ts,
                    ),
                )
                paper_trade_id = int(cur.lastrowid)
                self._conn.execute(
                    "UPDATE pending_paper_trades SET status='materialised' WHERE id=?",
                    (pending_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("materialise_pending_paper_trade", {
            "pending_id": pending_id, "paper_trade_id": paper_trade_id,
            "entry_price": entry_price, "ts": ts,
        })
        return paper_trade_id

    # ─────────────────────────  queries  ─────────────────────────

    def get_pending_paper_trades_due(self, now_ms_value: int) -> list[PendingPaperTrade]:
        """Return pending rows whose scheduled_for_ts <= now_ms_value.
        Materialised and voided rows are excluded.
        """
        cur = self._conn.execute(
            "SELECT id, strategy_id, triggering_signal_id, market_id, side, "
            "size_usd, scheduled_for_ts, status, created_at_ts "
            "FROM pending_paper_trades "
            "WHERE status='pending' AND scheduled_for_ts <= ? "
            "ORDER BY scheduled_for_ts ASC",
            (now_ms_value,),
        )
        return [
            PendingPaperTrade(
                strategy_id=r["strategy_id"],
                triggering_signal_id=r["triggering_signal_id"],
                market_id=r["market_id"],
                side=r["side"],
                size_usd=r["size_usd"],
                scheduled_for_ts=r["scheduled_for_ts"],
                status=r["status"],
                created_at_ts=r["created_at_ts"],
                id=r["id"],
            )
            for r in cur.fetchall()
        ]

    def get_latest_market_snapshot(self, market_id: str) -> MarketSnapshot | None:
        """Most recent `market_snapshots` row for this market, or None.
        Used by paper_trader's stub_blend / null_random_side at consume time
        to obtain a "current" mid for synthetic paper-trade entry pricing.
        """
        cur = self._conn.execute(
            "SELECT id, market_id, ts, mid, spread_bps, volume_24h_usd, "
            "liquidity_usd, category, question FROM market_snapshots "
            "WHERE market_id = ? ORDER BY ts DESC LIMIT 1",
            (market_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return MarketSnapshot(
            market_id=row["market_id"], ts=int(row["ts"]), mid=float(row["mid"]),
            spread_bps=row["spread_bps"], volume_24h_usd=row["volume_24h_usd"],
            liquidity_usd=row["liquidity_usd"], category=row["category"],
            question=row["question"], id=int(row["id"]),
        )

    def get_latest_book_snapshot(self, market_id: str) -> BookSnapshot | None:
        """Most recent `book_snapshots` row for this market, or None.
        Used by paper_trader's pending sweep to obtain a fresh mid for the
        whale_side_random_timing materialisation."""
        cur = self._conn.execute(
            "SELECT market_id, ts, book_json FROM book_snapshots "
            "WHERE market_id = ? ORDER BY ts DESC LIMIT 1",
            (market_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return BookSnapshot(
            market_id=row["market_id"], ts=int(row["ts"]),
            book_json=row["book_json"],
        )

    def get_unresolved_paper_trade_market_ids(self) -> list[str]:
        """Distinct market_ids that have at least one paper_trades row with
        resolved_outcome IS NULL. Used by ResolutionPoller — scope is
        paper-trade markets only per PLAN.md §15 Q6.
        """
        cur = self._conn.execute(
            "SELECT DISTINCT market_id FROM paper_trades WHERE resolved_outcome IS NULL"
        )
        return [r["market_id"] for r in cur.fetchall()]

    # ─────────────────────────  whale trust  ─────────────────────────

    def update_whale_trust(
        self,
        wallet: str,
        alpha: float,
        beta: float,
        trust_mean: float,
        *,
        candidate_blacklist: bool | None = None,
    ) -> None:
        """Upsert whale trust. Increments resolved_count by 1 on each call.
        Caller (whale_perf.py) computes (alpha, beta, trust_mean) after Beta
        update and clamping.
        """
        ts = now_ms()
        cb = candidate_blacklist
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                if cb is None:
                    self._conn.execute(
                        "INSERT INTO whale_trust "
                        "(whale_address, alpha, beta, trust_mean, resolved_count, "
                        " updated_at_ts, candidate_blacklist) "
                        "VALUES (?,?,?,?,1,?,0) "
                        "ON CONFLICT(whale_address) DO UPDATE SET "
                        "  alpha=excluded.alpha, "
                        "  beta=excluded.beta, "
                        "  trust_mean=excluded.trust_mean, "
                        "  resolved_count=resolved_count+1, "
                        "  updated_at_ts=excluded.updated_at_ts",
                        (wallet, alpha, beta, trust_mean, ts),
                    )
                else:
                    flag = 1 if cb else 0
                    self._conn.execute(
                        "INSERT INTO whale_trust "
                        "(whale_address, alpha, beta, trust_mean, resolved_count, "
                        " updated_at_ts, candidate_blacklist) "
                        "VALUES (?,?,?,?,1,?,?) "
                        "ON CONFLICT(whale_address) DO UPDATE SET "
                        "  alpha=excluded.alpha, "
                        "  beta=excluded.beta, "
                        "  trust_mean=excluded.trust_mean, "
                        "  resolved_count=resolved_count+1, "
                        "  updated_at_ts=excluded.updated_at_ts, "
                        "  candidate_blacklist=excluded.candidate_blacklist",
                        (wallet, alpha, beta, trust_mean, ts, flag),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("whale_trust_update", {
            "wallet": wallet, "alpha": alpha, "beta": beta,
            "trust_mean": trust_mean, "ts": ts,
            "candidate_blacklist": cb,
        })

    def get_whale_trust(
        self,
        wallet: str,
        *,
        prior_alpha: float | None = None,
        prior_beta: float | None = None,
    ) -> tuple[float, float]:
        """Return (alpha, beta). Defaults to Beta(7, 3) prior if unseen."""
        pa = prior_alpha if prior_alpha is not None else _TRUST_PRIOR_ALPHA
        pb = prior_beta if prior_beta is not None else _TRUST_PRIOR_BETA
        cur = self._conn.execute(
            "SELECT alpha, beta FROM whale_trust WHERE whale_address=?",
            (wallet,),
        )
        row = cur.fetchone()
        if row is None:
            return (pa, pb)
        return (float(row["alpha"]), float(row["beta"]))

    def get_whale_trust_row(self, wallet: str) -> dict[str, Any] | None:
        """Full whale_trust row for CLI / whale_perf, or None if unseen."""
        cur = self._conn.execute(
            "SELECT whale_address, alpha, beta, trust_mean, resolved_count, "
            "updated_at_ts, candidate_blacklist FROM whale_trust "
            "WHERE whale_address=?",
            (wallet,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def list_whale_trust_rows(self) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT whale_address, alpha, beta, trust_mean, resolved_count, "
            "updated_at_ts, candidate_blacklist FROM whale_trust "
            "ORDER BY trust_mean DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def refresh_candidate_blacklist_flags(self, threshold: float) -> list[str]:
        """Set candidate_blacklist from trust_mean vs threshold. Returns wallets
        currently below threshold (for logging). Phase A: flag only."""
        below: list[str] = []
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "SELECT whale_address, trust_mean FROM whale_trust"
                )
                for row in cur.fetchall():
                    wallet = row["whale_address"]
                    flagged = float(row["trust_mean"]) < threshold
                    if flagged:
                        below.append(wallet)
                    self._conn.execute(
                        "UPDATE whale_trust SET candidate_blacklist=? "
                        "WHERE whale_address=?",
                        (1 if flagged else 0, wallet),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("candidate_blacklist_refresh", {
            "threshold": threshold, "below_threshold": below,
        })
        return below

    def try_record_processed_trust_update(
        self, market_id: str, whale_signal_id: int
    ) -> bool:
        """Idempotent guard for whale_perf. Returns True if this pair was newly
        recorded (caller should apply Beta update), False if already processed."""
        ts = now_ms()
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO processed_trust_updates "
                    "(market_id, whale_signal_id, processed_at_ts) VALUES (?,?,?)",
                    (market_id, whale_signal_id, ts),
                )
                inserted = cur.rowcount == 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        if inserted:
            self._audit_write("processed_trust_update", {
                "market_id": market_id,
                "whale_signal_id": whale_signal_id,
                "processed_at_ts": ts,
            })
        return inserted

    def get_copy_whale_attribution_rows(
        self, market_id: str, strategy_id: str = "copy_whale"
    ) -> list[dict[str, Any]]:
        """Resolved copy_whale rows on a market for whale_perf attribution.
        Only YES/NO outcomes; VOIDED_* and INVALID excluded."""
        cur = self._conn.execute(
            "SELECT pt.triggering_signal_id, pt.side, pt.resolved_outcome, "
            "ws.whale_address "
            "FROM paper_trades pt "
            "JOIN whale_signals ws ON ws.id = pt.triggering_signal_id "
            "WHERE pt.market_id = ? AND pt.strategy_id = ? "
            "AND pt.resolved_outcome IN ('YES', 'NO') "
            "AND pt.entry_price IS NOT NULL",
            (market_id, strategy_id),
        )
        return [dict(r) for r in cur.fetchall()]

    # ──────────────────────────  market token map  ──────────────────────────

    def record_market_token(
        self, condition_id: str, yes_token_id: str, no_token_id: str | None = None
    ) -> None:
        """Upsert YES (and optionally NO) token_id for a condition_id.

        Not audit-logged: this table is a fast-path reference updated every
        scan cycle; auditing each upsert would bloat audit.jsonl with noise.
        """
        ts = now_ms()
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT INTO market_token_map "
                    "(condition_id, yes_token_id, no_token_id, updated_at_ts) VALUES (?,?,?,?) "
                    "ON CONFLICT(condition_id) DO UPDATE SET "
                    "  yes_token_id=excluded.yes_token_id, "
                    "  no_token_id=COALESCE(excluded.no_token_id, no_token_id), "
                    "  updated_at_ts=excluded.updated_at_ts",
                    (condition_id, yes_token_id, no_token_id, ts),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_yes_token(self, condition_id: str) -> str | None:
        """Return the YES token_id for a condition_id, or None if not yet scanned."""
        cur = self._conn.execute(
            "SELECT yes_token_id FROM market_token_map WHERE condition_id=?",
            (condition_id,),
        )
        row = cur.fetchone()
        return row["yes_token_id"] if row is not None else None

    def get_yes_token_id(self, condition_id: str) -> str | None:
        """Alias for get_yes_token; preferred name in Phase B executor."""
        return self.get_yes_token(condition_id)

    def get_no_token_id(self, condition_id: str) -> str | None:
        """Return the NO token_id for a condition_id, or None if not yet scanned."""
        cur = self._conn.execute(
            "SELECT no_token_id FROM market_token_map WHERE condition_id=?",
            (condition_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["no_token_id"]

    # ─────────────────────────  resolved markets  ─────────────────────────

    def record_resolved_market(
        self, market_id: str, outcome: str, resolved_at_ts: int
    ) -> None:
        """Idempotent insert into resolved_markets. Used by ResolutionPoller
        to skip markets we've already learned are settled."""
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO resolved_markets "
                    "(market_id, resolved_outcome, resolved_at_ts) VALUES (?,?,?)",
                    (market_id, outcome, resolved_at_ts),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("resolved_market", {
            "market_id": market_id, "outcome": outcome,
            "resolved_at_ts": resolved_at_ts,
        })

    def get_resolved_market(self, market_id: str) -> tuple[str, int] | None:
        cur = self._conn.execute(
            "SELECT resolved_outcome, resolved_at_ts FROM resolved_markets "
            "WHERE market_id=?",
            (market_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return (row["resolved_outcome"], int(row["resolved_at_ts"]))

    # ───────────────────────  calibration readiness  ───────────────────────

    def get_calibration_readiness(
        self, config: dict[str, Any] | None = None
    ) -> ReadinessReport:
        """Delegates to bot.calibration.validate_calibration_readiness."""
        from bot.calibration import validate_calibration_readiness

        return validate_calibration_readiness(self, config)

    def get_recent_whale_signals(
        self, limit: int = 50, since_ts_ms: int | None = None
    ) -> list[WhaleSignal]:
        """Most recent whale signals for whale-feed / status displays."""
        if since_ts_ms is not None:
            cur = self._conn.execute(
                "SELECT id, whale_address, whale_label, market_id, side, signal_type, "
                "price, size_usd, whale_fill_ts, detected_at_ts, detection_latency_ms, "
                "transaction_hash, raw_json "
                "FROM whale_signals WHERE detected_at_ts >= ? "
                "ORDER BY detected_at_ts DESC LIMIT ?",
                (since_ts_ms, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT id, whale_address, whale_label, market_id, side, signal_type, "
                "price, size_usd, whale_fill_ts, detected_at_ts, detection_latency_ms, "
                "transaction_hash, raw_json "
                "FROM whale_signals ORDER BY detected_at_ts DESC LIMIT ?",
                (limit,),
            )
        return [
            WhaleSignal(
                whale_address=r["whale_address"],
                market_id=r["market_id"],
                side=r["side"],
                signal_type=r["signal_type"],
                price=float(r["price"]),
                size_usd=float(r["size_usd"]),
                whale_fill_ts=int(r["whale_fill_ts"]),
                detected_at_ts=int(r["detected_at_ts"]),
                detection_latency_ms=int(r["detection_latency_ms"]),
                raw_json=r["raw_json"],
                whale_label=r["whale_label"],
                transaction_hash=r["transaction_hash"],
                id=int(r["id"]),
            )
            for r in cur.fetchall()
        ]

    def get_pnl_by_strategy(self, since_ts_ms: int) -> list[dict[str, Any]]:
        """Aggregate realised PnL by strategy_id since `since_ts_ms`."""
        cur = self._conn.execute(
            """
            SELECT strategy_id,
                   COUNT(*) AS n,
                   SUM(CASE WHEN side = resolved_outcome THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl_usd), 0) AS pnl_usd
            FROM paper_trades
            WHERE resolved_outcome IN ('YES', 'NO')
              AND decided_at_ts >= ?
            GROUP BY strategy_id
            ORDER BY strategy_id
            """,
            (since_ts_ms,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_pnl_by_whale(self, since_ts_ms: int) -> list[dict[str, Any]]:
        """Per-whale PnL from copy_whale resolved trades since `since_ts_ms`."""
        cur = self._conn.execute(
            """
            SELECT ws.whale_address,
                   ws.whale_label,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pt.side = pt.resolved_outcome THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pt.pnl_usd), 0) AS pnl_usd
            FROM paper_trades pt
            JOIN whale_signals ws ON ws.id = pt.triggering_signal_id
            WHERE pt.strategy_id = 'copy_whale'
              AND pt.resolved_outcome IN ('YES', 'NO')
              AND pt.decided_at_ts >= ?
            GROUP BY ws.whale_address
            ORDER BY pnl_usd DESC
            """,
            (since_ts_ms,),
        )
        return [dict(r) for r in cur.fetchall()]

    def resolved_calendar_days(self) -> float:
        """Span in days of resolved YES/NO paper trades (for backtest gate)."""
        cur = self._conn.execute(
            """
            SELECT MIN(decided_at_ts) AS lo, MAX(decided_at_ts) AS hi
            FROM paper_trades
            WHERE resolved_outcome IN ('YES', 'NO')
            """
        )
        row = cur.fetchone()
        if row["lo"] is None:
            return 0.0
        return (row["hi"] - row["lo"]) / 1000.0 / 86400.0

    # ─────────────────────────  Phase B: live_orders  ─────────────────────────

    def insert_live_order(self, order: "LiveOrder") -> int:
        """Insert a new live_orders row. Returns the row id."""
        from bot.models import LiveOrder  # local import avoids circular at module level
        ts = now_ms()
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO live_orders "
                    "(clob_order_id, market_id, side, limit_price, size_usd, status, "
                    " placed_at_ts, filled_at_ts, fill_price, risk_token_hash, intent_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        order.clob_order_id,
                        order.market_id,
                        order.side,
                        order.limit_price,
                        order.size_usd,
                        order.status,
                        order.placed_at_ts,
                        order.filled_at_ts,
                        order.fill_price,
                        order.risk_token_hash,
                        order.intent_json,
                    ),
                )
                row_id = cur.lastrowid
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._audit_write("live_order_placed", {
            "clob_order_id": order.clob_order_id,
            "market_id": order.market_id,
            "side": order.side,
            "size_usd": order.size_usd,
            "status": order.status,
        })
        return row_id

    def live_order_exists(self, risk_token_hash: str) -> bool:
        """True if any live_order row with this hash already exists."""
        cur = self._conn.execute(
            "SELECT 1 FROM live_orders WHERE risk_token_hash=? LIMIT 1",
            (risk_token_hash,),
        )
        return cur.fetchone() is not None

    def count_open_orders(self) -> int:
        """Total live_orders rows with status='open'."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM live_orders WHERE status='open'"
        )
        return int(cur.fetchone()[0])

    def count_open_orders_for_market(self, market_id: str) -> int:
        """Open orders for a specific market (duplicate-market block)."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM live_orders WHERE market_id=? AND status='open'",
            (market_id,),
        )
        return int(cur.fetchone()[0])

    def daily_realized_loss_usd(self, since_ms: int) -> float:
        """Total USD committed in live_orders placed since since_ms.

        Conservative: counts all placed orders (open + filled + dry_run) as
        "at risk" for the daily drawdown ceiling. Filled wins will offset this
        at resolution time, but we can't know that until the market resolves.
        """
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(size_usd), 0.0) FROM live_orders "
            "WHERE placed_at_ts >= ? AND status NOT IN ('cancelled', 'error')",
            (since_ms,),
        )
        return float(cur.fetchone()[0])

    def update_live_order_status(
        self,
        clob_order_id: str,
        status: str,
        *,
        fill_price: float | None = None,
        filled_at_ts: int | None = None,
    ) -> None:
        """Update status (and optionally fill details) on a live_order row."""
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "UPDATE live_orders SET status=?, fill_price=?, filled_at_ts=? "
                    "WHERE clob_order_id=?",
                    (status, fill_price, filled_at_ts, clob_order_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ─────────────────────────  shutdown  ─────────────────────────

    def close(self) -> None:
        """Close the SQLite connection. Idempotent — safe to call multiple times."""
        with self._write_lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass
