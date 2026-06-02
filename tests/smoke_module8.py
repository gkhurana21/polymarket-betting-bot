"""Smoke test for Module 8 (bot/paper_trader.py).

13 verifications covering the four-stream fan-out + sweep semantics:

   1. Signal with open type → 3 immediate paper_trades + 1 pending row
   2. copy_whale skips exit signals (no row)
   3. stub_blend + null_random_side produce rows for exit signals
   4. null_random_side deterministic: same market_id → same side across re-runs
   5. null_random_side balanced across 1000 markets (rough 50/50)
   6. copy_whale clamps oversize bets to max_paper_size_usd
   7. copy_whale clamps tiny bets up to min_paper_size_usd
   8. Sweep case 1: resolved-before-fire → VOIDED_PRE_FILL
   9. Sweep case 2: missed-window → VOIDED_MISSED_WINDOW
  10. Sweep case 3: stale / missing snapshot → VOIDED_MISSED_WINDOW
  11. Sweep case 4: healthy → MATERIALISED with entry = mid
  12. Sweep precedence: resolved + missed simultaneously → VOIDED_PRE_FILL
  13. Concurrent consume: two signals processed simultaneously, all rows land

Run:
    .venv/bin/python tests/smoke_module8.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import BookSnapshot, MarketSnapshot, PaperTrade, PendingPaperTrade, WhaleSignal  # noqa: E402
from bot.paper_trader import PaperTrader  # noqa: E402
from bot.tracker import Tracker  # noqa: E402
from utils.helpers import now_ms  # noqa: E402


# ══════════════════════════  fixtures / helpers  ══════════════════════════

_FULL_CFG: dict = {
    "paper_streams": {
        "copy_whale": {
            "enabled": True,
            "proportional_scale": 0.01,
            "min_paper_size_usd": 5.0,
            "max_paper_size_usd": 500.0,
            "acts_on_signal_types": ["open", "add"],
        },
        "stub_blend": {
            "enabled": True,
            "alpha": 0.5, "beta": 0.0, "gamma": 0.5,
            "trust_prior": 0.7,
            "size_usd": 100.0,
            "acts_on_signal_types": ["open", "add", "exit", "flip"],
        },
        "null_random_side": {
            "enabled": True,
            "size_usd": 100.0,
            "seed_strategy": "market_id",
            "acts_on_signal_types": ["open", "add", "exit", "flip"],
        },
        "whale_side_random_timing": {
            "enabled": True,
            "size_usd": 100.0,
            "delay_window_seconds": 86_400.0,
            "seed_strategy": "signal_id",
            "miss_tolerance_seconds": 1800.0,
            "max_snapshot_staleness_seconds": 900.0,
            "acts_on_signal_types": ["open", "add"],
        },
    },
    "paper_trader": {"snapshot_max_staleness_seconds": 900.0},
}


def _fresh_tracker(td: Path) -> Tracker:
    return Tracker(db_path=td / "t.db", audit_log_path=td / "a.jsonl")


def _seed_whale_signal(
    tracker: Tracker, *,
    wallet: str = "0xW", market: str = "m_t", side: str = "YES",
    signal_type: str = "open", price: float = 0.40, size_usd: float = 100.0,
    txhash: str = "0xtx_a",
) -> WhaleSignal:
    sig = WhaleSignal(
        whale_address=wallet, market_id=market, side=side,
        signal_type=signal_type, price=price, size_usd=size_usd,
        whale_fill_ts=now_ms() - 5_000, detected_at_ts=now_ms() - 4_000,
        detection_latency_ms=1_000, raw_json="{}", transaction_hash=txhash,
    )
    rowid = tracker.record_whale_signal(sig)
    assert rowid is not None
    # WhaleSignal is frozen; return a fresh instance with id set.
    from dataclasses import replace
    return replace(sig, id=rowid)


def _seed_market_snapshot(
    tracker: Tracker, *, market: str = "m_t", mid: float = 0.60,
    age_ms: int = 60_000,
) -> int:
    return tracker.record_market_snapshot(MarketSnapshot(
        market_id=market, ts=now_ms() - age_ms, mid=mid,
        spread_bps=400.0, volume_24h_usd=None, liquidity_usd=None,
        category=None, question="Test",
    ))


def _seed_book_snapshot(
    tracker: Tracker, *, market: str = "m_t",
    best_bid: float = 0.59, best_ask: float = 0.61,
    age_ms: int = 60_000,
) -> int:
    bk = {"bids": [[best_bid, 100]], "asks": [[best_ask, 100]]}
    return tracker.record_book_snapshot(BookSnapshot(
        market_id=market, ts=now_ms() - age_ms,
        book_json=json.dumps(bk, separators=(",", ":")),
    ))


# ════════════════════════════════════════════════════════════════════════


def main() -> int:
    # ── 1: open signal + all 4 streams enabled → 3 paper + 1 pending ──
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        _seed_market_snapshot(tk, market="m1", mid=0.55, age_ms=60_000)
        sig = _seed_whale_signal(tk, market="m1", side="YES",
                                 signal_type="open", price=0.50, size_usd=1000.0,
                                 txhash="0xtx_1")
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        out = pt.consume_signal(sig)
        assert len(out) == 3, f"expected 3 immediate trades, got {len(out)}"
        strategies = {t.strategy_id for t in out}
        assert strategies == {"copy_whale", "stub_blend", "null_random_side"}
        # Pending row should exist for whale_side_random_timing.
        pend = tk._conn.execute(
            "SELECT strategy_id, side, status FROM pending_paper_trades "
            "WHERE triggering_signal_id=?", (sig.id,),
        ).fetchall()
        assert len(pend) == 1
        assert pend[0]["strategy_id"] == "whale_side_random_timing"
        assert pend[0]["side"] == "YES"
        assert pend[0]["status"] == "pending"
        tk.close()
    print("  test 1: open signal → 3 paper_trades + 1 pending row             OK")

    # ── 2 + 3: signal_type=exit dispatch ─────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        _seed_market_snapshot(tk, market="m2", mid=0.60, age_ms=60_000)
        sig = _seed_whale_signal(
            tk, market="m2", side="YES", signal_type="exit",
            price=0.55, size_usd=200.0, txhash="0xtx_2",
        )
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        # copy_whale acts only on [open, add] → skipped
        # whale_side_random_timing acts only on [open, add] → skipped
        # stub_blend + null_random_side ignore signal_type → both fire
        assert "copy_whale" not in strategies, strategies
        assert strategies == {"stub_blend", "null_random_side"}
        assert len(out) == 2
        # No pending row (whale_side_random_timing skipped).
        n_pending = tk._conn.execute(
            "SELECT COUNT(*) FROM pending_paper_trades"
        ).fetchone()[0]
        assert n_pending == 0
        tk.close()
    print("  tests 2+3: exit signal → no copy_whale / no pending, "
          "stub+null fire        OK")

    # ── 4: null_random_side deterministic on market_id ────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        sides = set()
        for run in range(3):
            sig = WhaleSignal(
                whale_address="0xW", market_id="m_repro_001", side="YES",
                signal_type="open", price=0.5, size_usd=10.0,
                whale_fill_ts=run, detected_at_ts=run, detection_latency_ms=0,
                raw_json="{}", transaction_hash=f"0xtx_repro_{run}",
                id=run + 1,
            )
            built = pt._build_null_random_side(sig, mid=0.5)
            sides.add(built.side)
        assert len(sides) == 1, f"null_random_side must be deterministic, got {sides}"
        tk.close()
    print(f"  test 4: null_random_side deterministic across 3 runs ({sides.pop()})  OK")

    # ── 5: null_random_side balanced across 1000 markets ──────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        yes_count = 0
        for i in range(1000):
            sig = WhaleSignal(
                whale_address="0xW", market_id=f"m_dist_{i:04d}", side="YES",
                signal_type="open", price=0.5, size_usd=10.0,
                whale_fill_ts=0, detected_at_ts=0, detection_latency_ms=0,
                raw_json="{}", transaction_hash=f"0xtx_dist_{i}",
                id=i + 1,
            )
            if pt._build_null_random_side(sig, mid=0.5).side == "YES":
                yes_count += 1
        # χ² at df=1, p=0.01 → critical = 6.63; |Δ| ≤ √(6.63·500) ≈ 58
        assert 442 <= yes_count <= 558, f"yes_count={yes_count} outside [442, 558]"
        tk.close()
    print(f"  test 5: null_random_side balance 1000 markets → "
          f"{yes_count} YES / {1000 - yes_count} NO  OK")

    # ── 6: copy_whale clamps oversize ────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        _seed_market_snapshot(tk, market="m6", mid=0.5, age_ms=60_000)
        # Whale bets $1M → scale 0.01 = $10k → clamped to max $500.
        sig = _seed_whale_signal(tk, market="m6", size_usd=1_000_000.0,
                                 signal_type="open", txhash="0xtx_6")
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        out = pt.consume_signal(sig)
        cw = next(t for t in out if t.strategy_id == "copy_whale")
        assert cw.size_usd == 500.0, f"expected $500 clamp, got ${cw.size_usd}"
        tk.close()
    print("  test 6: copy_whale clamp at max_paper_size_usd=$500              OK")

    # ── 7: copy_whale clamps tiny ────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        _seed_market_snapshot(tk, market="m7", mid=0.5, age_ms=60_000)
        # Whale bets $1 × 0.01 = $0.01 → clamped UP to min $5.
        sig = _seed_whale_signal(tk, market="m7", size_usd=1.0,
                                 signal_type="open", txhash="0xtx_7")
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        out = pt.consume_signal(sig)
        cw = next(t for t in out if t.strategy_id == "copy_whale")
        assert cw.size_usd == 5.0, f"expected $5 min clamp, got ${cw.size_usd}"
        tk.close()
    print("  test 7: copy_whale clamp at min_paper_size_usd=$5                OK")

    # ── 8: sweep case 1 — resolved before fire ────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        sig = _seed_whale_signal(tk, market="m8", txhash="0xtx_8")
        # Pending with scheduled_for_ts in the recent past — would be due.
        tk.record_pending_paper_trade(PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=int(sig.id),                     # type: ignore[arg-type]
            market_id="m8", side="YES", size_usd=100.0,
            scheduled_for_ts=now_ms() - 60_000,
            status="pending", created_at_ts=now_ms() - 120_000,
        ))
        # Mark market resolved BEFORE the scheduled fire.
        tk.record_resolved_market("m8", "YES", now_ms() - 30_000)
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        r = pt.sweep_pending(now_ms())
        assert r.due == 1
        assert r.voided_pre_fill == 1
        assert r.voided_missed_window == 0
        assert r.materialised == 0
        # The visible paper_trades row.
        row = tk._conn.execute(
            "SELECT resolved_outcome, pnl_usd, entry_price FROM paper_trades "
            "WHERE strategy_id='whale_side_random_timing'"
        ).fetchone()
        assert row["resolved_outcome"] == "VOIDED_PRE_FILL"
        assert row["pnl_usd"] == 0.0
        assert row["entry_price"] is None
        tk.close()
    print("  test 8: sweep case 1 → VOIDED_PRE_FILL                          OK")

    # ── 9: sweep case 2 — missed window ───────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        sig = _seed_whale_signal(tk, market="m9", txhash="0xtx_9")
        # Scheduled 1 hour ago; miss_tolerance is 30 min → past tolerance.
        tk.record_pending_paper_trade(PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=int(sig.id),                     # type: ignore[arg-type]
            market_id="m9", side="YES", size_usd=100.0,
            scheduled_for_ts=now_ms() - 3_600_000,
            status="pending", created_at_ts=now_ms() - 3_700_000,
        ))
        # Provide a fresh book so case 3 wouldn't fire — proves case 2 caught it.
        _seed_book_snapshot(tk, market="m9", age_ms=60_000)
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        r = pt.sweep_pending(now_ms())
        assert r.voided_missed_window == 1, r
        assert r.voided_pre_fill == 0 and r.materialised == 0
        row = tk._conn.execute(
            "SELECT resolved_outcome FROM paper_trades "
            "WHERE strategy_id='whale_side_random_timing'"
        ).fetchone()
        assert row["resolved_outcome"] == "VOIDED_MISSED_WINDOW"
        tk.close()
    print("  test 9: sweep case 2 → VOIDED_MISSED_WINDOW (missed_tolerance)  OK")

    # ── 10: sweep case 3 — stale / missing snapshot ──────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        sig = _seed_whale_signal(tk, market="m10", txhash="0xtx_10")
        # Healthy timing: scheduled 60 s ago, well within 30-min tolerance.
        tk.record_pending_paper_trade(PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=int(sig.id),                     # type: ignore[arg-type]
            market_id="m10", side="YES", size_usd=100.0,
            scheduled_for_ts=now_ms() - 60_000,
            status="pending", created_at_ts=now_ms() - 120_000,
        ))
        # NO book snapshot at all for m10. Case 3 must fire.
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        r = pt.sweep_pending(now_ms())
        assert r.voided_missed_window == 1, r
        # Sub-case: snapshot exists but is stale (> 15 min).
        sig2 = _seed_whale_signal(tk, market="m10b", txhash="0xtx_10b")
        tk.record_pending_paper_trade(PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=int(sig2.id),                    # type: ignore[arg-type]
            market_id="m10b", side="YES", size_usd=100.0,
            scheduled_for_ts=now_ms() - 60_000,
            status="pending", created_at_ts=now_ms() - 120_000,
        ))
        _seed_book_snapshot(tk, market="m10b", age_ms=30 * 60_000)   # 30 min old
        r = pt.sweep_pending(now_ms())
        assert r.voided_missed_window == 1, r
        tk.close()
    print("  test 10: sweep case 3 → VOIDED_MISSED_WINDOW (stale/missing)    OK")

    # ── 11: sweep case 4 — healthy materialisation ───────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        sig = _seed_whale_signal(tk, market="m11", side="NO", txhash="0xtx_11")
        tk.record_pending_paper_trade(PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=int(sig.id),                     # type: ignore[arg-type]
            market_id="m11", side="NO", size_usd=100.0,
            scheduled_for_ts=now_ms() - 60_000,
            status="pending", created_at_ts=now_ms() - 120_000,
        ))
        # Fresh book → mid = (0.59 + 0.61) / 2 = 0.60. NO entry = 1 − 0.60 = 0.40.
        _seed_book_snapshot(tk, market="m11", best_bid=0.59, best_ask=0.61, age_ms=60_000)
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        r = pt.sweep_pending(now_ms())
        assert r.materialised == 1, r
        assert r.voided_missed_window == 0 and r.voided_pre_fill == 0
        row = tk._conn.execute(
            "SELECT side, entry_price, resolved_outcome FROM paper_trades "
            "WHERE strategy_id='whale_side_random_timing'"
        ).fetchone()
        assert row["side"] == "NO"
        assert math.isclose(row["entry_price"], 0.40, abs_tol=1e-9), row["entry_price"]
        assert row["resolved_outcome"] is None
        # Pending row marked materialised.
        s = tk._conn.execute(
            "SELECT status FROM pending_paper_trades"
        ).fetchone()["status"]
        assert s == "materialised"
        tk.close()
    print("  test 11: sweep case 4 → MATERIALISED with entry=1−mid=$0.40     OK")

    # ── 12: sweep precedence — resolved beats missed-window ───────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        sig = _seed_whale_signal(tk, market="m12", txhash="0xtx_12")
        # BOTH conditions: market resolved AND past miss_tolerance.
        tk.record_pending_paper_trade(PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=int(sig.id),                     # type: ignore[arg-type]
            market_id="m12", side="YES", size_usd=100.0,
            scheduled_for_ts=now_ms() - 3_600_000,
            status="pending", created_at_ts=now_ms() - 3_700_000,
        ))
        tk.record_resolved_market("m12", "YES", now_ms() - 1_800_000)
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        r = pt.sweep_pending(now_ms())
        # Precedence: resolved-before-fire (case 1) wins, not missed (case 2).
        assert r.voided_pre_fill == 1, r
        assert r.voided_missed_window == 0, r
        row = tk._conn.execute(
            "SELECT resolved_outcome FROM paper_trades "
            "WHERE strategy_id='whale_side_random_timing'"
        ).fetchone()
        assert row["resolved_outcome"] == "VOIDED_PRE_FILL"
        tk.close()
    print("  test 12: precedence resolved > missed → VOIDED_PRE_FILL         OK")

    # ── 13: concurrent consume of two signals ────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        _seed_market_snapshot(tk, market="m13a", mid=0.5, age_ms=60_000)
        _seed_market_snapshot(tk, market="m13b", mid=0.5, age_ms=60_000)
        s1 = _seed_whale_signal(tk, market="m13a", side="YES",
                                signal_type="open", price=0.5, size_usd=200.0,
                                txhash="0xtx_13a")
        s2 = _seed_whale_signal(tk, market="m13b", side="NO",
                                signal_type="open", price=0.5, size_usd=200.0,
                                txhash="0xtx_13b")
        pt = PaperTrader(tracker=tk, client=None, config=_FULL_CFG)
        results: list[list[PaperTrade]] = [[], []]
        errors: list[Exception] = []

        def worker(idx: int, sig: WhaleSignal) -> None:
            try:
                results[idx] = pt.consume_signal(sig)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker, args=(0, s1))
        t2 = threading.Thread(target=worker, args=(1, s2))
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert not errors, f"concurrent errors: {errors}"
        # Each signal: 3 immediate trades (copy + stub + null).
        assert len(results[0]) == 3 and len(results[1]) == 3
        # Total: 6 paper_trades + 2 pending_paper_trades.
        n_pt = tk._conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        n_pp = tk._conn.execute("SELECT COUNT(*) FROM pending_paper_trades").fetchone()[0]
        assert n_pt == 6, f"expected 6 paper_trades, got {n_pt}"
        assert n_pp == 2, f"expected 2 pending_paper_trades, got {n_pp}"
        tk.close()
    print("  test 13: concurrent consume → 6 paper_trades + 2 pending        OK")

    # ── 14: on-demand fallback — live book used when no snapshot exists ──
    class _MockClient:
        def get_order_book(self, market_id: str) -> dict:
            return {"bids": [[0.44, 100]], "asks": [[0.46, 100]]}

    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # Seed the token mapping so the fallback can call get_order_book
        # with the correct token_id (not the condition_id).
        tk.record_market_token("m14", "0xYES_m14")
        sig = _seed_whale_signal(tk, market="m14", side="YES",
                                 signal_type="open", price=0.45, size_usd=200.0,
                                 txhash="0xtx_14")
        # No market_snapshot seeded — fallback must fire via mock client.
        pt = PaperTrader(tracker=tk, client=_MockClient(), config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        # stub_blend + null_random_side must fire via the live-book fallback.
        assert "stub_blend" in strategies, f"stub_blend missing: {strategies}"
        assert "null_random_side" in strategies, f"null_random_side missing: {strategies}"
        # copy_whale fires on open regardless; whale_side_random_timing goes to pending.
        assert "copy_whale" in strategies
        assert len(out) == 3
        # book_snapshot row must have been written before mid computation.
        snap_row = tk._conn.execute(
            "SELECT book_json FROM book_snapshots WHERE market_id='m14'"
        ).fetchone()
        assert snap_row is not None, "book_snapshot not persisted by fallback"
        assert '"bids"' in snap_row["book_json"]
        # entry price must be mid = (0.44 + 0.46) / 2 = 0.45
        sb = next(t for t in out if t.strategy_id == "stub_blend")
        assert math.isclose(sb.entry_price, 0.45, abs_tol=1e-9) or \
               math.isclose(sb.entry_price, 1 - 0.45, abs_tol=1e-9), \
               f"unexpected stub_blend entry_price: {sb.entry_price}"
        tk.close()
    print("  test 14: on-demand book fallback → stub+null fire, "
          "book_snapshot written  OK")

    # ── 15: on-demand fallback — CircuitBreakerOpen skips streams ────
    from bot.client import CircuitBreakerOpen

    class _BrokenClient:
        def get_order_book(self, market_id: str) -> dict:
            raise CircuitBreakerOpen("circuit open")

    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # Seed the token mapping so the fallback reaches the client call
        # and exercises the CircuitBreakerOpen branch specifically.
        tk.record_market_token("m15", "0xYES_m15")
        sig = _seed_whale_signal(tk, market="m15", side="YES",
                                 signal_type="open", price=0.5, size_usd=100.0,
                                 txhash="0xtx_15")
        # No snapshot; client raises CircuitBreakerOpen.
        pt = PaperTrader(tracker=tk, client=_BrokenClient(), config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        # copy_whale still fires (doesn't need a snapshot).
        assert "copy_whale" in strategies
        # stub_blend + null_random_side must be absent.
        assert "stub_blend" not in strategies, strategies
        assert "null_random_side" not in strategies, strategies
        assert len(out) == 1
        tk.close()
    print("  test 15: CircuitBreakerOpen → stub+null skipped, "
          "copy_whale unaffected  OK")

    # ── 16: on-demand fallback with correct token mapping → stub+null fire ─
    class _TokenAwareClient:
        """Returns a valid two-sided book when called with the YES token_id."""
        def get_order_book(self, token_id: str) -> dict:
            assert token_id == "0xYES_TOKEN", f"wrong token: {token_id}"
            return {"bids": [[0.44, 100]], "asks": [[0.46, 100]]}

    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # Seed the token mapping the way the scanner would.
        tk.record_market_token("0xCOND_M16", "0xYES_TOKEN")
        sig = _seed_whale_signal(tk, market="0xCOND_M16", side="YES",
                                 signal_type="open", price=0.45, size_usd=200.0,
                                 txhash="0xtx_16")
        pt = PaperTrader(tracker=tk, client=_TokenAwareClient(), config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        assert "stub_blend" in strategies, f"stub_blend missing: {strategies}"
        assert "null_random_side" in strategies, f"null_random_side missing: {strategies}"
        assert "copy_whale" in strategies
        assert len(out) == 3
        # book_snapshot persisted with condition_id as market_id.
        snap = tk._conn.execute(
            "SELECT book_json FROM book_snapshots WHERE market_id='0xCOND_M16'"
        ).fetchone()
        assert snap is not None, "book_snapshot not written"
        assert '"bids"' in snap["book_json"]
        # pending row for whale_side_random_timing.
        n_pend = tk._conn.execute(
            "SELECT COUNT(*) FROM pending_paper_trades"
        ).fetchone()[0]
        assert n_pend == 1
        tk.close()
    print("  test 16: token mapping → correct token_id used, "
          "stub+null fire        OK")

    # ── 17: unknown condition_id → None gracefully, copy+wt still fire ──
    class _NeverCalledClient:
        def get_order_book(self, token_id: str) -> dict:
            raise AssertionError("get_order_book must not be called for unknown market")

    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # No market_token_map entry for this market.
        sig = _seed_whale_signal(tk, market="0xUNKNOWN_M17", side="YES",
                                 signal_type="open", price=0.50, size_usd=100.0,
                                 txhash="0xtx_17")
        pt = PaperTrader(tracker=tk, client=_NeverCalledClient(), config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        # copy_whale fires regardless of snap.
        assert "copy_whale" in strategies, strategies
        # stub+null skip because no token mapping → no snap.
        assert "stub_blend" not in strategies, strategies
        assert "null_random_side" not in strategies, strategies
        # whale_side_random_timing must still fire (it doesn't need snap).
        n_pend = tk._conn.execute(
            "SELECT COUNT(*) FROM pending_paper_trades"
        ).fetchone()[0]
        assert n_pend == 1, (
            f"whale_side_random_timing must still produce a pending row "
            f"even when snap is None; got {n_pend}"
        )
        # No book_snapshot written (client never called).
        n_bs = tk._conn.execute(
            "SELECT COUNT(*) FROM book_snapshots"
        ).fetchone()[0]
        assert n_bs == 0
        tk.close()
    print("  test 17: unknown condition_id → stub+null skip, "
          "copy+wt unaffected    OK")

    # ── 18: token known but circuit breaker open → WARNING branch, copy+wt OK
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        tk.record_market_token("0xCOND_M18", "0xYES_TOKEN_18")
        sig = _seed_whale_signal(tk, market="0xCOND_M18", side="YES",
                                 signal_type="open", price=0.50, size_usd=100.0,
                                 txhash="0xtx_18")

        class _CBOpenClient:
            def get_order_book(self, token_id: str) -> dict:
                raise CircuitBreakerOpen("test circuit open")

        pt = PaperTrader(tracker=tk, client=_CBOpenClient(), config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        assert "copy_whale" in strategies
        assert "stub_blend" not in strategies, strategies
        assert "null_random_side" not in strategies, strategies
        n_pend = tk._conn.execute(
            "SELECT COUNT(*) FROM pending_paper_trades"
        ).fetchone()[0]
        assert n_pend == 1
        tk.close()
    print("  test 18: circuit breaker open → WARNING branch, "
          "copy+wt unaffected     OK")

    # ── 19: on-demand get_market fallback for markets outside scanner ────
    # No token map entry; client must be called for get_market + get_order_book.
    class _FullFallbackClient:
        def get_market(self, condition_id: str) -> dict:
            assert condition_id == "0xCOND_M19", f"wrong cond: {condition_id}"
            return {"tokens": [
                {"outcome": "No",  "token_id": "0xNO_M19"},
                {"outcome": "Yes", "token_id": "0xYES_M19"},
            ]}

        def get_order_book(self, token_id: str) -> dict:
            assert token_id == "0xYES_M19", f"wrong token: {token_id}"
            return {"bids": [[0.62, 100]], "asks": [[0.64, 100]]}

    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # No token map entry for m19 — forces get_market() fallback path.
        sig = _seed_whale_signal(tk, market="0xCOND_M19", side="YES",
                                 signal_type="open", price=0.63, size_usd=300.0,
                                 txhash="0xtx_19")
        pt = PaperTrader(tracker=tk, client=_FullFallbackClient(), config=_FULL_CFG)
        out = pt.consume_signal(sig)
        strategies = {t.strategy_id for t in out}
        assert "stub_blend" in strategies, f"stub_blend missing: {strategies}"
        assert "null_random_side" in strategies, f"null_random_side missing: {strategies}"
        assert "copy_whale" in strategies
        # Token must be cached to map after the call.
        cached = tk.get_yes_token("0xCOND_M19")
        assert cached == "0xYES_M19", f"expected cached token, got {cached!r}"
        # Second signal for same market must NOT call get_market again.
        class _MapOnlyClient(_FullFallbackClient):
            def get_market(self, condition_id: str) -> dict:
                raise AssertionError("get_market must not be called — token is cached")
        sig2 = _seed_whale_signal(tk, market="0xCOND_M19", side="NO",
                                  signal_type="add", price=0.37, size_usd=100.0,
                                  txhash="0xtx_19b")
        pt2 = PaperTrader(tracker=tk, client=_MapOnlyClient(), config=_FULL_CFG)
        out2 = pt2.consume_signal(sig2)
        assert "stub_blend" in {t.strategy_id for t in out2}
        tk.close()
    print("  test 19: get_market fallback fires stub+null, token cached, "
          "2nd signal uses map  OK")

    print("\nmodule 8 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
