"""Smoke test for Module 7 (bot/whale_tracker.py).

Verifications (12) + a cross-module lifecycle integration:

   1. AbstractWhaleTracker cannot be instantiated directly
   2. PollingWhaleTracker instantiates with empty universe → empty caches
   3. Position replay: 3 chronological seeded signals → cache reflects final state
   4. Classifier: open / add / exit / flip from synthetic priors
   5. FP residual: 1e-9 prior treated as zero — next BUY → "open"
   6. Cold-start wallet: trade for untracked wallet classifies as "open"
   7. Queue overflow: maxsize reached → oldest dropped, warning logged
   8. Detection latency: trade ts = now-5s → latency ≈ 5000 ms
   9. Dedup: same (wallet, market, outcome, side) within 5 min emits once
  10. Threading: start() launches thread, stop() joins within timeout
  11. Pagination: 250 trades across 3 fake pages → all 250 ingested
  12. (skippable) Live integration: track Theo4 for one poll cycle

  +  Lifecycle:  signal → paper_trade → record_resolved_market →
                 resolution_poller.poll_once → resolved with correct PnL

Run:
    .venv/bin/python tests/smoke_module7.py
    LIVE_API_TESTS=0 .venv/bin/python tests/smoke_module7.py   # skip test 12
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.client import ClobReadOnlyClient  # noqa: E402
from bot.models import PaperTrade, WhaleSignal  # noqa: E402
from bot.resolution_poller import ResolutionPoller  # noqa: E402
from bot.tracker import Tracker  # noqa: E402
from bot.whale_tracker import (  # noqa: E402
    AbstractWhaleTracker,
    PollingWhaleTracker,
    classify_signal_type,
)
from utils.helpers import now_ms  # noqa: E402

LIVE = os.environ.get("LIVE_API_TESTS", "1") == "1"


# ════════════════════════════  fakes  ════════════════════════════

class _NullSDK:
    """Stand-in for py_clob_client.ClobClient — only get_address is exercised."""
    def get_address(self):
        return None


class _FakeClient:
    """Minimal ClobReadOnlyClient stand-in. Returns canned trade pages.

    `pages` is a dict {wallet → [page1, page2, ...]} where each page is a
    list of trade dicts. The fake yields page[offset/page_size] when called.
    """

    def __init__(self, pages: dict[str, list[list[dict]]]):
        self._sdk = _NullSDK()
        self._pages = pages
        self.calls: list[tuple[str, int, int]] = []

    def get_trades_for_wallet(self, wallet, after_ts=None, limit=100, offset=0):
        self.calls.append((wallet, limit, offset))
        wallet_pages = self._pages.get(wallet, [])
        idx = offset // max(1, limit)
        if idx >= len(wallet_pages):
            return []
        return list(wallet_pages[idx])


def _trade(
    *, ts_ms: int, side: str, outcome: str, price: float, size: float,
    cid: str = "0xCID", txhash: str | None = None, wallet: str = "0xWHALE",
) -> dict:
    return {
        "proxyWallet": wallet,
        "side": side,
        "outcome": outcome,
        "conditionId": cid,
        "asset": cid + "-tok",
        "price": price,
        "size": size,
        "timestamp": ts_ms,                # post-client convention: ms
        "transactionHash": txhash if txhash is not None
                           else f"0x{ts_ms:x}{side}{outcome}",
        "title": "Test market",
        "name": "TestWhale",
        "pseudonym": "Test-Pseudo",
    }


def _basic_config(poll_interval=60.0, queue_max=10000, dedup_ms=5*60*1000):
    return {
        "intervals_seconds": {"whale_poll": poll_interval},
        "whale_tracker": {
            "queue_max": queue_max,
            "dedup_window_ms": dedup_ms,
            "page_size": 100,
            "max_pages_per_wallet": 20,
            "latency_window": 1000,
            "latency_log_every_polls": 5,
        },
    }


def _fresh_tracker(td: Path) -> Tracker:
    return Tracker(db_path=td / "t.db", audit_log_path=td / "a.jsonl")


# ════════════════════════════════════════════════════════════════════════


def main() -> int:
    # ── 1: ABC cannot be instantiated ────────────────────────────────
    try:
        AbstractWhaleTracker()  # type: ignore[abstract]
    except TypeError:
        pass
    else:
        raise AssertionError("AbstractWhaleTracker must not be instantiable")
    print("  ABC: AbstractWhaleTracker direct instantiation rejected           OK")

    # ── 2: empty universe → empty caches ─────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        wt = PollingWhaleTracker(
            client=_FakeClient({}), tracker=tk,
            config=_basic_config(), whale_universe=[],
        )
        assert wt._position_cache == {}
        assert wt._last_seen_ts == {}
        assert wt._queue.qsize() == 0
        tk.close()
    print("  empty universe: position cache, last_seen, queue all empty        OK")

    # ── 3: position replay from seeded history ───────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # Three chronological signals on same (wallet, market):
        #   t=1000  open YES @ 0.40, $40    →  100 YES shares
        #   t=2000  add  YES @ 0.50, $50    →  +100 YES shares = 200 total
        #   t=3000  exit YES @ 0.55, $55    →  −100 YES shares = 100 remaining
        for sig in [
            WhaleSignal(
                whale_address="0xRPL", market_id="m_rpl", side="YES",
                signal_type="open", price=0.40, size_usd=40.0,
                whale_fill_ts=1000, detected_at_ts=1500, detection_latency_ms=500,
                raw_json="{}", transaction_hash="0xrpl1",
            ),
            WhaleSignal(
                whale_address="0xRPL", market_id="m_rpl", side="YES",
                signal_type="add", price=0.50, size_usd=50.0,
                whale_fill_ts=2000, detected_at_ts=2500, detection_latency_ms=500,
                raw_json="{}", transaction_hash="0xrpl2",
            ),
            WhaleSignal(
                whale_address="0xRPL", market_id="m_rpl", side="YES",
                signal_type="exit", price=0.55, size_usd=55.0,
                whale_fill_ts=3000, detected_at_ts=3500, detection_latency_ms=500,
                raw_json="{}", transaction_hash="0xrpl3",
            ),
        ]:
            assert tk.record_whale_signal(sig) is not None
        wt = PollingWhaleTracker(
            client=_FakeClient({}), tracker=tk, config=_basic_config(),
            whale_universe=[{"address": "0xRPL", "label": "Repl"}],
        )
        slot = wt._position_cache[("0xRPL", "m_rpl")]
        # +100 +100 −100 = 100 YES shares.
        assert abs(slot["yes_shares"] - 100.0) < 1e-9, slot
        assert slot["no_shares"] == 0.0
        assert wt._last_seen_ts["0xRPL"] == 3000
        tk.close()
    print(f"  replay: 3 chronological signals → yes_shares=100, last_seen=3000  OK")

    # ── 4: classifier — open / add / exit / flip ─────────────────────
    # open: no prior anywhere
    assert classify_signal_type(
        yes_shares_prior=0.0, no_shares_prior=0.0,
        trade_outcome="Yes", trade_side="BUY", trade_shares=50,
    ) == "open"
    # add: prior YES, BUY YES
    assert classify_signal_type(
        yes_shares_prior=100.0, no_shares_prior=0.0,
        trade_outcome="Yes", trade_side="BUY", trade_shares=50,
    ) == "add"
    # exit: SELL on a side they own
    assert classify_signal_type(
        yes_shares_prior=100.0, no_shares_prior=0.0,
        trade_outcome="Yes", trade_side="SELL", trade_shares=50,
    ) == "exit"
    # flip: prior NO, BUY YES with shares > prior NO
    assert classify_signal_type(
        yes_shares_prior=0.0, no_shares_prior=50.0,
        trade_outcome="Yes", trade_side="BUY", trade_shares=200,
    ) == "flip"
    # add (not flip): prior NO, BUY YES with shares < prior NO
    assert classify_signal_type(
        yes_shares_prior=0.0, no_shares_prior=200.0,
        trade_outcome="Yes", trade_side="BUY", trade_shares=50,
    ) == "add"
    # symmetric: prior YES, BUY NO with shares > prior → flip
    assert classify_signal_type(
        yes_shares_prior=50.0, no_shares_prior=0.0,
        trade_outcome="No", trade_side="BUY", trade_shares=200,
    ) == "flip"
    # exit on NO side
    assert classify_signal_type(
        yes_shares_prior=0.0, no_shares_prior=80.0,
        trade_outcome="No", trade_side="SELL", trade_shares=30,
    ) == "exit"
    print("  classifier: open / add / exit / flip across 7 cases               OK")

    # ── 5: floating-point residual treated as zero ────────────────────
    # 1e-9 < tolerance → has_prior False → "open"
    assert classify_signal_type(
        yes_shares_prior=1e-9, no_shares_prior=-1e-10,
        trade_outcome="Yes", trade_side="BUY", trade_shares=10,
    ) == "open"
    print("  classifier: |1e-9| residual collapses to zero                     OK")

    # ── 6: cold-start wallet → "open" via empty cache lookup ─────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # Universe is empty; trade arrives for an "untracked" wallet.
        # We exercise classify directly with the same lookup pattern poll uses.
        wt = PollingWhaleTracker(
            client=_FakeClient({}), tracker=tk,
            config=_basic_config(), whale_universe=[],
        )
        slot = wt._position_cache.get(
            ("0xUNTRACKED", "m_x"), {"yes_shares": 0.0, "no_shares": 0.0}
        )
        s = classify_signal_type(
            yes_shares_prior=slot["yes_shares"],
            no_shares_prior=slot["no_shares"],
            trade_outcome="Yes", trade_side="BUY", trade_shares=100,
        )
        assert s == "open"
        tk.close()
    print("  cold-start wallet: empty cache lookup → 'open'                    OK")

    # ── 7: queue overflow → oldest dropped, signal still lands ───────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        wt = PollingWhaleTracker(
            client=_FakeClient({}), tracker=tk,
            config=_basic_config(queue_max=3), whale_universe=[],
        )
        # Pre-fill the queue past capacity by direct enqueue.
        for i in range(3):
            sig = WhaleSignal(
                whale_address="0xQ", market_id="mq", side="YES", signal_type="open",
                price=0.5, size_usd=10.0,
                whale_fill_ts=1_000_000 + i, detected_at_ts=1_000_001 + i,
                detection_latency_ms=1, raw_json="{}",
                transaction_hash=f"0xq{i}", id=i + 1,
            )
            wt._enqueue(sig)
        assert wt._queue.qsize() == 3
        # Push a 4th — oldest (id=1) is dropped, 4th lands.
        sig4 = WhaleSignal(
            whale_address="0xQ", market_id="mq", side="YES", signal_type="add",
            price=0.5, size_usd=10.0,
            whale_fill_ts=2_000_000, detected_at_ts=2_000_001,
            detection_latency_ms=1, raw_json="{}",
            transaction_hash="0xq3", id=4,
        )
        wt._enqueue(sig4)
        assert wt._queue.qsize() == 3
        ids_in_queue = []
        while not wt._queue.empty():
            ids_in_queue.append(wt._queue.get_nowait().id)
        # Oldest (1) was dropped; 2,3,4 remain in FIFO.
        assert ids_in_queue == [2, 3, 4], ids_in_queue
        tk.close()
    print("  queue overflow: oldest dropped, remaining = [2,3,4]               OK")

    # ── 8: detection latency ≈ ts age ────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        whale = {"address": "0xLAT", "label": "Lat"}
        five_seconds_ago_ms = now_ms() - 5000
        page1 = [_trade(
            ts_ms=five_seconds_ago_ms, side="BUY", outcome="Yes",
            price=0.50, size=20.0, cid="0xLATM", txhash="0xLATtx",
            wallet="0xLAT",
        )]
        wt = PollingWhaleTracker(
            client=_FakeClient({"0xLAT": [page1]}), tracker=tk,
            config=_basic_config(), whale_universe=[whale],
        )
        emitted = wt.poll_once().emitted
        assert emitted == 1
        sig = wt._queue.get_nowait()
        # Allow up to 1500ms slop for test execution time.
        assert 4500 <= sig.detection_latency_ms <= 6500, sig.detection_latency_ms
        tk.close()
    print(f"  latency: trade ts=now-5s → detection_latency_ms in [4500,6500]    OK")

    # ── 9: 5-min in-memory dedup ─────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        whale = {"address": "0xDED", "label": "Ded"}
        # Two trades, same (market, outcome, side), 20 s apart — second is dup'd.
        # Use distinct transaction hashes so DB-level dedup doesn't save us;
        # in-memory layer must catch it.  Both within the staleness cutoff.
        t1 = _trade(ts_ms=now_ms() - 30_000, side="BUY", outcome="Yes",
                    price=0.5, size=10.0, cid="0xDM", txhash="0xtx_a", wallet="0xDED")
        t2 = _trade(ts_ms=now_ms() - 10_000, side="BUY", outcome="Yes",
                    price=0.5, size=10.0, cid="0xDM", txhash="0xtx_b", wallet="0xDED")
        page1 = [t2, t1]                              # newest-first like the real API
        wt = PollingWhaleTracker(
            client=_FakeClient({"0xDED": [page1]}), tracker=tk,
            config=_basic_config(), whale_universe=[whale],
        )
        n = wt.poll_once().emitted
        # Replayed oldest-first: t1 emits ('open'); t2 hits 5-min dedup → no emit.
        assert n == 1, f"expected 1 emission (second dedup'd), got {n}"
        assert wt._queue.qsize() == 1
        tk.close()
    print("  dedup: same (wallet,market,outcome,side) within 5min → 1 emission OK")

    # ── 10: start/stop threading lifecycle ──────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # Tight poll interval so the loop runs ≥ once before we stop.
        cfg = _basic_config(poll_interval=0.05)
        wt = PollingWhaleTracker(
            client=_FakeClient({}), tracker=tk,
            config=cfg, whale_universe=[],
        )
        wt.start()
        assert wt._thread is not None and wt._thread.is_alive()
        time.sleep(0.15)                              # let it loop a few times
        t0 = time.monotonic()
        wt.stop(timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"stop took {elapsed:.2f}s — should join promptly"
        assert wt._thread is None
        tk.close()
    print(f"  threading: start/stop joined cleanly in {elapsed*1000:.0f}ms            OK")

    # ── 11: pagination across 3 pages → 250 trades ──────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        whale = {"address": "0xPAG"}
        # Use fresh timestamps (within last few seconds) so the staleness guard
        # doesn't drop them. 250 distinct markets so in-memory dedup doesn't
        # collapse to 1 emission. Three pages: 100 + 100 + 50.
        base_ts = now_ms() - 250                       # ascending in last 0.25s
        all_trades = [
            _trade(
                ts_ms=base_ts + i, side="BUY", outcome="Yes",
                price=0.5, size=10.0,
                cid=f"0xMK{i:04d}", txhash=f"0xtx_pag_{i}", wallet="0xPAG",
            )
            for i in range(250)
        ]
        # API returns newest-first; reverse before paginating into pages.
        sorted_desc = sorted(all_trades, key=lambda t: -t["timestamp"])
        page_size = 100
        pages = [sorted_desc[i:i + page_size] for i in range(0, 250, page_size)]
        assert [len(p) for p in pages] == [100, 100, 50]
        fc = _FakeClient({"0xPAG": pages})
        # Disable staleness cutoff for this test — its concern is pagination.
        cfg_pag = _basic_config()
        cfg_pag["whale_tracker"]["max_signal_age_ms"] = 10 ** 12
        wt = PollingWhaleTracker(
            client=fc, tracker=tk, config=cfg_pag,
            whale_universe=[whale],
        )
        emitted = wt.poll_once().emitted
        assert emitted == 250, f"expected 250, got {emitted}"
        # 3 paginated calls: offsets 0, 100, 200.
        offsets = [c[2] for c in fc.calls]
        assert offsets == [0, 100, 200], offsets
        tk.close()
    print(f"  pagination: 250 trades across 3 pages (offsets {offsets})         OK")

    # ── 12: live integration (skippable) ────────────────────────────
    if LIVE:
        with tempfile.TemporaryDirectory() as td:
            tk = _fresh_tracker(Path(td))
            client = ClobReadOnlyClient(
                retry_max_attempts=3,
                retry_initial_backoff_seconds=0.001,
                retry_max_backoff_seconds=0.01,
                cb_fail_threshold=20,
                cb_reset_seconds=60.0,
                sleep_fn=lambda _s: None,
            )
            # Theo4 from the leaderboard probe.
            whale = {
                "address": "0x56687bf447db6ffa42ffe2204a05edaa20f55839",
                "label": "Theo4",
            }
            # First poll — last_seen=0, fetch up to 100 most recent trades.
            # Disable staleness cutoff: Theo4's recent trades may be older
            # than 60 s; the live test's purpose is end-to-end pipeline
            # validation, not the cutoff (covered by verification 14).
            cfg_live = _basic_config()
            cfg_live["whale_tracker"]["max_signal_age_ms"] = 10 ** 12
            wt = PollingWhaleTracker(
                client=client, tracker=tk,
                config=cfg_live, whale_universe=[whale],
            )
            n = wt.poll_once().emitted
            assert n >= 1, f"expected ≥1 live signal for Theo4, got {n}"
            sig = wt._queue.get_nowait()
            assert sig.whale_address == whale["address"]
            assert sig.side in ("YES", "NO")
            assert sig.signal_type in ("open", "add", "exit", "flip")
            assert sig.detection_latency_ms >= 0
            assert sig.transaction_hash and sig.transaction_hash.startswith("0x")
            tk.close()
            print(f"  live: 1st emitted → {sig.side}/{sig.signal_type}, "
                  f"latency={sig.detection_latency_ms}ms, "
                  f"tx={sig.transaction_hash[:14]}…  OK")
    else:
        print("  live integration: SKIPPED (LIVE_API_TESTS=0)")

    # ── 14: stale signal recorded to DB but NOT enqueued ─────────────
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        cfg = _basic_config()
        cfg["whale_tracker"]["max_signal_age_ms"] = 60_000           # 1 minute
        whale = {"address": "0xSTALE", "label": "Stale"}
        # Two trades: one stale (120 s old) and one fresh (5 s old).
        # Both must land in DB; only the fresh one in the queue.
        stale_ts = now_ms() - 120_000
        fresh_ts = now_ms() - 5_000
        page1 = [
            _trade(ts_ms=fresh_ts, side="BUY", outcome="Yes", price=0.5, size=10.0,
                   cid="0xSM1", txhash="0xSTALE_fresh", wallet="0xSTALE"),
            _trade(ts_ms=stale_ts, side="BUY", outcome="No",  price=0.5, size=10.0,
                   cid="0xSM2", txhash="0xSTALE_stale", wallet="0xSTALE"),
        ]
        wt = PollingWhaleTracker(
            client=_FakeClient({"0xSTALE": [page1]}), tracker=tk,
            config=cfg, whale_universe=[whale],
        )
        emitted = wt.poll_once().emitted
        assert emitted == 1, f"only fresh signal should emit, got {emitted}"
        # DB has BOTH rows (recording happens before the staleness check).
        db_count = tk._conn.execute(
            "SELECT COUNT(*) FROM whale_signals WHERE whale_address=?", ("0xSTALE",)
        ).fetchone()[0]
        assert db_count == 2, f"both rows must be in DB, got {db_count}"
        # Queue has exactly one — the fresh one.
        assert wt._queue.qsize() == 1
        sig = wt._queue.get_nowait()
        assert sig.market_id == "0xSM1", f"queued signal should be the fresh one, got {sig.market_id}"
        tk.close()
    print("  stale signal: 2 to DB / 1 to queue (stale dropped at boundary)   OK")

    # ════════════ lifecycle integration: signal → paper → resolution ════════════
    with tempfile.TemporaryDirectory() as td:
        tk = _fresh_tracker(Path(td))
        # 1. Seed a synthetic whale signal.
        ws = WhaleSignal(
            whale_address="0xLIFE", market_id="m_life", side="YES",
            signal_type="open", price=0.40, size_usd=80.0,
            whale_fill_ts=now_ms() - 3600_000,
            detected_at_ts=now_ms() - 3599_000,
            detection_latency_ms=1000, raw_json="{}",
            transaction_hash="0xlifecycle_tx",
        )
        sid = tk.record_whale_signal(ws)
        assert sid is not None

        # 2. Paper trader stand-in: bet YES at $0.40 with $80 → 200 shares.
        pt = PaperTrade(
            strategy_id="copy_whale", triggering_signal_id=sid,
            market_id="m_life", side="YES", entry_price=0.40, size_usd=80.0,
            decided_at_ts=now_ms() - 3590_000,
        )
        pt_id = tk.record_paper_trade(pt)

        # 3. Pre-cache the resolved market (YES wins) — same as resolution
        #    poller would have done after fetching the closed market.
        tk.record_resolved_market("m_life", "YES", now_ms())

        # 4. Run the resolution poller. With m_life cached, it skips the API
        #    and settles the paper trade from the cache.
        class _StrictNoCallClient:
            def get_market(self, mid):
                raise AssertionError(f"resolution_poller hit API for cached {mid}")
        rp = ResolutionPoller(client=_StrictNoCallClient(), tracker=tk)
        r = rp.poll_once()
        assert r.markets_checked == 1
        assert r.paper_trades_settled == 1
        assert r.errors == 0

        # 5. Verify settlement: bet YES, YES won → +shares×$1 − cost.
        #    200 shares × $1 − $80 = +$120.
        row = tk._conn.execute(
            "SELECT resolved_outcome, pnl_usd FROM paper_trades WHERE id=?",
            (pt_id,),
        ).fetchone()
        assert row["resolved_outcome"] == "YES", row["resolved_outcome"]
        assert abs(row["pnl_usd"] - 120.0) < 1e-9, row["pnl_usd"]
        tk.close()
    print("  lifecycle: signal → paper_trade → resolved → PnL +$120         OK")

    print("\nmodule 7 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
