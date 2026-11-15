"""SQLite persistence + audit log.

Schema is deliberately measurement-first: every quote cycle records the
reward-economics inputs (pool, depth, our size, implied share, expected
accrual) so the scale-up decision is answered from logged reality rather than
a model. Per the operating rule, measurement happens ALONGSIDE revenue.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from friday.logger import get_logger

_log = get_logger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;

-- One row per quote cycle per market. The reward-economics dataset.
CREATE TABLE IF NOT EXISTS quote_cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  condition_id TEXT NOT NULL,
  question TEXT,
  quoting INTEGER NOT NULL,          -- 0/1
  reason TEXT,                       -- why we declined, when quoting=0
  bid_px REAL, bid_sz REAL,
  ask_px REAL, ask_sz REAL,
  mid REAL, vol_est REAL, half_spread REAL,
  reward_band REAL,
  pool_usd_per_day REAL,
  depth_inside_band REAL,            -- pro-rata denominator (excl. us)
  our_shares REAL,
  implied_share REAL,                -- our_shares / (depth + our_shares)
  expected_usd_per_day REAL,
  position REAL,
  markout REAL,
  tox_bid REAL, tox_ask REAL
);
CREATE INDEX IF NOT EXISTS idx_qc_ts ON quote_cycles(ts);
CREATE INDEX IF NOT EXISTS idx_qc_market ON quote_cycles(condition_id, ts);

-- Fills. signed_size > 0 means our bid was hit (we bought).
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  condition_id TEXT NOT NULL,
  token_id TEXT,
  signed_size REAL NOT NULL,
  price REAL NOT NULL,
  order_id TEXT,
  markout_at_lag REAL                -- filled in later by postmortem
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

-- Realised reward credits, so expected can be scored against actual.
CREATE TABLE IF NOT EXISTS reward_credits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  condition_id TEXT NOT NULL,
  usd REAL NOT NULL,
  day TEXT NOT NULL,
  UNIQUE(condition_id, day)
);
"""


class Tracker:
    def __init__(self, db_path: str | Path, audit_log_path: str | Path | None = None) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._audit = Path(audit_log_path) if audit_log_path else self._path.parent / "audit.jsonl"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def _audit_write(self, event: str, payload: dict[str, Any]) -> None:
        try:
            with self._audit.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": self._now(), "event": event,
                                    "payload": payload}, separators=(",", ":")) + "\n")
        except OSError as e:
            _log.warning("audit write failed: %s", e)

    # -- writes -------------------------------------------------------------

    def record_quote_cycle(self, row: dict[str, Any]) -> int:
        cols = ("condition_id", "question", "quoting", "reason", "bid_px", "bid_sz",
                "ask_px", "ask_sz", "mid", "vol_est", "half_spread", "reward_band",
                "pool_usd_per_day", "depth_inside_band", "our_shares", "implied_share",
                "expected_usd_per_day", "position", "markout", "tox_bid", "tox_ask")
        vals = [self._now()] + [row.get(c) for c in cols]
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO quote_cycles (ts,{','.join(cols)}) "
                f"VALUES ({','.join('?' * (len(cols) + 1))})", vals)
            self._conn.commit()
            return cur.lastrowid

    def record_fill(self, condition_id: str, token_id: str, signed_size: float,
                    price: float, order_id: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO fills (ts,condition_id,token_id,signed_size,price,order_id) "
                "VALUES (?,?,?,?,?,?)",
                (self._now(), condition_id, token_id, signed_size, price, order_id))
            self._conn.commit()
        self._audit_write("fill", {"condition_id": condition_id,
                                   "signed_size": signed_size, "price": price})
        return cur.lastrowid

    def record_reward(self, condition_id: str, usd: float, day: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reward_credits (ts,condition_id,usd,day) VALUES (?,?,?,?) "
                "ON CONFLICT(condition_id,day) DO UPDATE SET usd=excluded.usd",
                (self._now(), condition_id, usd, day))
            self._conn.commit()

    # -- reads --------------------------------------------------------------

    def expected_reward_since(self, since_ms: int) -> float:
        """Integrate expected accrual over logged cycles, PER MARKET.

        Grouping by market is not cosmetic. The cycles table interleaves
        markets — with two live markets the rows run (m1@t, m2@t, m1@t+30,
        m2@t+30, ...) — so integrating the table as one timeline charges most
        pairs a dt of ~0 and under-reports by roughly the market count. That
        number feeds the guard's reward-vs-inventory halt, and under-reporting
        income there makes the guard trip early on a business that is actually
        paying. Measured on a 24h run: 1.67 flat vs 2.75 grouped.
        """
        cur = self._conn.execute(
            "SELECT condition_id, ts, expected_usd_per_day FROM quote_cycles "
            "WHERE ts >= ? AND quoting = 1 ORDER BY condition_id, ts", (since_ms,))
        by_market: dict[str, list[tuple[int, float]]] = {}
        for r in cur.fetchall():
            by_market.setdefault(r["condition_id"], []).append(
                (r["ts"], r["expected_usd_per_day"] or 0.0))

        total = 0.0
        for rows in by_market.values():
            for i in range(1, len(rows)):
                dt_ms = rows[i][0] - rows[i - 1][0]
                if 0 < dt_ms <= 3600_000:      # ignore gaps > 1h (downtime)
                    total += rows[i - 1][1] * (dt_ms / 86_400_000.0)
        return total

    def realised_reward_since(self, since_ms: int) -> float:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(usd),0.0) s FROM reward_credits WHERE ts >= ?",
            (since_ms,))
        return float(cur.fetchone()["s"])

    def fills_since(self, since_ms: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM fills WHERE ts >= ? ORDER BY ts", (since_ms,)).fetchall()

    def last_cycle_ts(self) -> int | None:
        r = self._conn.execute("SELECT MAX(ts) m FROM quote_cycles").fetchone()
        return int(r["m"]) if r and r["m"] else None

    def latest_cycle_per_market(self, since_ms: int) -> list[sqlite3.Row]:
        """Most recent cycle for each market seen in the window."""
        return self._conn.execute(
            "SELECT * FROM quote_cycles WHERE id IN "
            "  (SELECT MAX(id) FROM quote_cycles WHERE ts >= ? GROUP BY condition_id) "
            "ORDER BY expected_usd_per_day DESC", (since_ms,)).fetchall()

    def recent_fills(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()

    def cycles_since(self, since_ms: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM quote_cycles WHERE ts >= ? ORDER BY ts", (since_ms,)).fetchall()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass
