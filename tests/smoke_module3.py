"""Smoke test for Module 3 (bot/tracker.py).

Covers all ten verifications from the build directive:
  1. WAL mode active
  2. Foreign keys ON
  3. All Phase A tables exist (+ Phase B stubs)
  4. WhaleSignal round-trip: insert → query → fields match
  5. Pending due query: past returns, future does not
  6. Void writes a visible paper_trades row with pnl_usd=0
  7. Whale trust defaults to (7, 3) for unseen wallet
  8. Concurrent write safety: 2 threads × 50 inserts, all 100 land
  9. Audit log: every insert produces a parseable JSONL line + DB parity
 10. Readiness on empty DB returns three failing checks (0/50, 0/5, 0/14)

Run from project root:
    .venv/bin/python tests/smoke_module3.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import PendingPaperTrade, WhaleSignal  # noqa: E402
from bot.tracker import Tracker  # noqa: E402
from utils.helpers import now_ms  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        db_path = td_path / "trades.db"
        audit_path = td_path / "audit.log.jsonl"
        t = Tracker(db_path=db_path, audit_log_path=audit_path)

        # ── 1+2+3: pragmas + table set ─────────────────────────────────
        jm = t._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert jm.lower() == "wal", f"journal_mode must be 'wal', got {jm!r}"
        fk = t._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, f"foreign_keys must be 1 (ON), got {fk}"
        sync = t._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert sync == 1, f"synchronous must be 1 (NORMAL), got {sync}"
        print(f"  pragmas: journal_mode={jm} foreign_keys={fk} synchronous={sync}     OK")

        expected_tables = {
            "whale_signals", "market_snapshots", "book_snapshots",
            "paper_trades", "pending_paper_trades", "whale_trust",
            "resolved_markets", "live_orders",
        }
        cur = t._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {r[0] for r in cur.fetchall()}
        missing = expected_tables - tables
        assert not missing, f"missing tables: {missing}"
        print(f"  schema: all {len(expected_tables)} tables present "
              f"({len(tables - expected_tables)} extra)                       OK")

        # ── 4: WhaleSignal round-trip ──────────────────────────────────
        ws = WhaleSignal(
            whale_address="0xWHALE001",
            market_id="m_test",
            side="YES",
            signal_type="open",
            price=0.42,
            size_usd=2500.0,
            whale_fill_ts=1_700_000_001_000,
            detected_at_ts=1_700_000_002_500,
            detection_latency_ms=1500,
            raw_json='{"trade":"data"}',
            whale_label="alpha",
        )
        sid = t.record_whale_signal(ws)
        assert isinstance(sid, int) and sid > 0
        row = t._conn.execute(
            "SELECT whale_address, whale_label, market_id, side, signal_type, "
            "price, size_usd, whale_fill_ts, detected_at_ts, detection_latency_ms, "
            "raw_json FROM whale_signals WHERE id=?", (sid,)
        ).fetchone()
        for k in (
            "whale_address", "whale_label", "market_id", "side", "signal_type",
            "price", "size_usd", "whale_fill_ts", "detected_at_ts",
            "detection_latency_ms", "raw_json",
        ):
            assert row[k] == getattr(ws, k), f"field {k} mismatch: db={row[k]!r} ws={getattr(ws, k)!r}"
        print(f"  round-trip: whale_signal id={sid}, all 11 fields match            OK")

        # ── 5: pending due query ───────────────────────────────────────
        n = now_ms()
        ppt_past = PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=sid,
            market_id="m_test",
            side="YES",
            size_usd=100.0,
            scheduled_for_ts=n - 60_000,           # 1 min ago
            status="pending",
            created_at_ts=n - 120_000,
        )
        ppt_future = PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=sid,
            market_id="m_test",
            side="YES",
            size_usd=100.0,
            scheduled_for_ts=n + 3_600_000,        # 1 h from now
            status="pending",
            created_at_ts=n,
        )
        past_id = t.record_pending_paper_trade(ppt_past)
        future_id = t.record_pending_paper_trade(ppt_future)
        due_ids = {p.id for p in t.get_pending_paper_trades_due(now_ms())}
        assert past_id in due_ids, f"past pending id={past_id} should be due"
        assert future_id not in due_ids, f"future pending id={future_id} should NOT be due"
        print(f"  pending due query: past={past_id} in, future={future_id} out         OK")

        # ── 6: void writes visible paper_trades row ────────────────────
        void_paper_id = t.void_pending_paper_trade(past_id, "VOIDED_PRE_FILL")
        row = t._conn.execute(
            "SELECT entry_price, pnl_usd, resolved_outcome, resolved_at_ts "
            "FROM paper_trades WHERE id=?", (void_paper_id,)
        ).fetchone()
        assert row["entry_price"] is None, f"void entry_price must be NULL, got {row['entry_price']}"
        assert row["pnl_usd"] == 0.0, f"void pnl_usd must be 0, got {row['pnl_usd']}"
        assert row["resolved_outcome"] == "VOIDED_PRE_FILL"
        assert row["resolved_at_ts"] is not None
        # pending row marked voided
        s = t._conn.execute(
            "SELECT status FROM pending_paper_trades WHERE id=?", (past_id,)
        ).fetchone()["status"]
        assert s == "voided"
        # voided pending no longer "due"
        assert past_id not in {p.id for p in t.get_pending_paper_trades_due(now_ms())}
        # second void on same row is a hard error
        try:
            t.void_pending_paper_trade(past_id, "VOIDED_PRE_FILL")
        except ValueError:
            pass
        else:
            raise AssertionError("void of already-voided row must raise")
        # invalid reason rejected
        try:
            t.void_pending_paper_trade(future_id, "BOGUS_REASON")
        except ValueError:
            pass
        else:
            raise AssertionError("void with invalid reason must raise")
        print(f"  void: paper_trades id={void_paper_id} entry=NULL pnl=0 status=voided   OK")

        # ── 7: whale trust default ─────────────────────────────────────
        a, b = t.get_whale_trust("0xUNSEEN_WALLET")
        assert (a, b) == (7.0, 3.0), f"unseen trust must default to (7,3), got ({a},{b})"
        t.update_whale_trust("0xWHALE001", alpha=8.5, beta=2.5, trust_mean=8.5 / 11.0)
        a2, b2 = t.get_whale_trust("0xWHALE001")
        assert (a2, b2) == (8.5, 2.5)
        # trust_mean computed correctly
        tm = t._conn.execute(
            "SELECT trust_mean, resolved_count FROM whale_trust WHERE whale_address=?",
            ("0xWHALE001",),
        ).fetchone()
        assert abs(tm["trust_mean"] - (8.5 / 11.0)) < 1e-9
        assert tm["resolved_count"] == 1
        # second update increments resolved_count
        t.update_whale_trust("0xWHALE001", alpha=9.0, beta=3.0, trust_mean=9.0 / 12.0)
        rc = t._conn.execute(
            "SELECT resolved_count FROM whale_trust WHERE whale_address=?",
            ("0xWHALE001",),
        ).fetchone()["resolved_count"]
        assert rc == 2
        print("  whale_trust: default (7,3), update + resolved_count++              OK")

        # ── 10: readiness on empty-of-resolved DB ──────────────────────
        # We've written one whale_signal, two pending, one VOIDED paper_trade,
        # but ZERO YES/NO-resolved trades. All three checks must read 0.
        rep = t.get_calibration_readiness()
        names = {c.name: c for c in rep.checks}
        assert names["resolved_trades"].current == 0.0
        assert names["whales_with_5_trades"].current == 0.0
        assert names["calendar_days"].current == 0.0
        for c in rep.checks:
            assert not c.passed
        assert rep.all_passed is False
        assert "0/50 trades" in rep.summary
        assert "0/5 whales" in rep.summary
        assert "0/14 days" in rep.summary
        print(f"  readiness: empty DB → 3 failing, summary='{rep.summary}'      OK")

        # ── 8: concurrent writes ───────────────────────────────────────
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                for _ in range(50):
                    sig = WhaleSignal(
                        whale_address=f"0xT{tid:02d}",
                        market_id=f"m_concurrent_{tid}",
                        side="YES",
                        signal_type="open",
                        price=0.5,
                        size_usd=100.0,
                        whale_fill_ts=now_ms(),
                        detected_at_ts=now_ms(),
                        detection_latency_ms=0,
                        raw_json="{}",
                    )
                    t.record_whale_signal(sig)
            except Exception as e:
                errors.append(e)

        before = t._conn.execute("SELECT COUNT(*) FROM whale_signals").fetchone()[0]
        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise AssertionError(f"concurrent write errors: {errors}")
        after = t._conn.execute("SELECT COUNT(*) FROM whale_signals").fetchone()[0]
        landed = after - before
        assert landed == 100, f"expected 100 new rows from concurrent writes, got {landed}"
        print(f"  concurrent: 2 threads × 50 inserts → {landed} rows landed clean    OK")

        # ── (extra) book_snapshots dedup behaviour ─────────────────────
        from bot.models import BookSnapshot
        bs_ts = 1_700_000_500_000
        b1 = BookSnapshot(market_id="m_book", ts=bs_ts, book_json='{"bids":[[0.49,100]],"asks":[[0.51,100]]}')
        b2 = BookSnapshot(market_id="m_book", ts=bs_ts, book_json='{"bids":[[0.40,999]],"asks":[[0.60,999]]}')
        t.record_book_snapshot(b1)
        t.record_book_snapshot(b2)  # same (market_id, ts) — INSERT OR IGNORE no-ops
        cnt = t._conn.execute(
            "SELECT COUNT(*) FROM book_snapshots WHERE market_id=? AND ts=?",
            ("m_book", bs_ts),
        ).fetchone()[0]
        assert cnt == 1, f"duplicate (market_id, ts) must yield 1 row, got {cnt}"
        # First write wins (the IGNORE keeps the original)
        kept = t._conn.execute(
            "SELECT book_json FROM book_snapshots WHERE market_id=? AND ts=?",
            ("m_book", bs_ts),
        ).fetchone()[0]
        assert kept == b1.book_json, "INSERT OR IGNORE must preserve the first write"
        print("  book_snapshots: duplicate (market_id, ts) → 1 row, first wins   OK")

        # ── (extra) UNIQUE(transaction_hash) idempotency + replay query ───
        ws_hash = WhaleSignal(
            whale_address="0xWHALE_HASH",
            market_id="m_dedup",
            side="YES",
            signal_type="open",
            price=0.5,
            size_usd=50.0,
            whale_fill_ts=1_700_000_010_000,
            detected_at_ts=1_700_000_011_000,
            detection_latency_ms=1000,
            raw_json="{}",
            transaction_hash="0xtx_uniqueA",
        )
        first_id = t.record_whale_signal(ws_hash)
        assert isinstance(first_id, int) and first_id > 0
        # Same hash → ON CONFLICT DO NOTHING → returns None, no audit duplicate.
        dup_id = t.record_whale_signal(ws_hash)
        assert dup_id is None, f"duplicate insert must return None, got {dup_id!r}"
        # NULL transaction_hash never conflicts (multiple NULLs allowed).
        ws_null1 = WhaleSignal(
            whale_address="0xWHALE_NULL", market_id="m_null", side="NO",
            signal_type="open", price=0.5, size_usd=10.0,
            whale_fill_ts=1_700_000_020_000, detected_at_ts=1_700_000_021_000,
            detection_latency_ms=1000, raw_json="{}",
        )
        ws_null2 = WhaleSignal(
            whale_address="0xWHALE_NULL", market_id="m_null", side="NO",
            signal_type="add", price=0.5, size_usd=10.0,
            whale_fill_ts=1_700_000_022_000, detected_at_ts=1_700_000_023_000,
            detection_latency_ms=1000, raw_json="{}",
        )
        assert t.record_whale_signal(ws_null1) is not None
        assert t.record_whale_signal(ws_null2) is not None
        # Replay query returns chronological order.
        replay = t.get_whale_signals_for_wallet("0xWHALE_NULL")
        assert len(replay) == 2
        assert replay[0].whale_fill_ts < replay[1].whale_fill_ts
        assert replay[0].signal_type == "open" and replay[1].signal_type == "add"
        replay_hash = t.get_whale_signals_for_wallet("0xWHALE_HASH")
        assert len(replay_hash) == 1
        assert replay_hash[0].transaction_hash == "0xtx_uniqueA"
        print("  whale_signals: UNIQUE(transaction_hash) + get_whale_signals_for_wallet  OK")

        # ── 9: audit log parity ─────────────────────────────────────────
        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        for line in lines:
            assert isinstance(line, dict)
            assert {"ts", "event_type", "payload"} <= line.keys(), f"malformed: {line}"
            assert isinstance(line["payload"], dict)
        events = {l["event_type"] for l in lines}
        required_events = {
            "whale_signal", "pending_paper_trade",
            "void_pending_paper_trade", "whale_trust_update",
        }
        missing_evt = required_events - events
        assert not missing_evt, f"missing audit events: {missing_evt}"
        whale_audit_count = sum(1 for l in lines if l["event_type"] == "whale_signal")
        whale_db_count = t._conn.execute("SELECT COUNT(*) FROM whale_signals").fetchone()[0]
        assert whale_audit_count == whale_db_count, (
            f"audit/db parity: audit has {whale_audit_count} whale_signal events, "
            f"DB has {whale_db_count} rows"
        )
        # field-level parity on the original signal
        original = next(
            l for l in lines
            if l["event_type"] == "whale_signal" and l["payload"].get("id") == sid
        )
        assert original["payload"]["whale_address"] == ws.whale_address
        assert original["payload"]["side"] == ws.side
        assert original["payload"]["price"] == ws.price
        assert original["payload"]["detection_latency_ms"] == ws.detection_latency_ms
        print(f"  audit log: {len(lines)} JSONL lines, "
              f"whale parity {whale_audit_count}/{whale_db_count}                  OK")

        t.close()

    print("\nmodule 3 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
