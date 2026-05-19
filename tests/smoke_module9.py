"""Smoke test for Module 9 (bot/whale_perf.py).

16 verifications:
   1. beta_update helper: win → α+1
   2. beta_update helper: loss → β+1
   3. cold-start win → Beta(8, 3) from prior (7, 3)
   4. cold-start loss → Beta(7, 4) from prior (7, 3)
   5. mixed 3W/2L → Beta(10, 5)
   6. manual seed --trust 0.9 then 1 win → Beta(10, 1)
   7. trust_mean can go below 0.1 (no sentimental 0.1 floor)
   8. trust_mean capped at 0.95
   9. stub_blend resolution does not update trust
  10. INVALID market outcome → no trust updates
  11. idempotency: second call → 0 updates AND (α, β) unchanged
  12. candidate_blacklist SET when below threshold
  13. candidate_blacklist UNSET when trust recovers above threshold
  14. processed_trust_updates row exists
  15. resolution_poller hook
  16. two signals same market → two Beta steps

Run:
    .venv/bin/python tests/smoke_module9.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import PaperTrade, WhaleSignal  # noqa: E402
from bot.resolution_poller import ResolutionPoller  # noqa: E402
from bot.tracker import Tracker  # noqa: E402
from bot.whale_perf import WhalePerf  # noqa: E402


_CFG: dict = {
    "whale_perf": {
        "prior_alpha": 7,
        "prior_beta": 3,
        "manual_override_total_weight": 10,
        "trust_mean_min": 0.0,
        "trust_mean_max": 0.95,
        "blacklist_threshold": 0.35,
        "attribution_strategy_id": "copy_whale",
    }
}


def _tracker(tmp: str) -> Tracker:
    return Tracker(db_path=Path(tmp) / "t.db", audit_log_path=Path(tmp) / "a.jsonl")


_signal_counter = 0


def _signal(tk: Tracker, *, whale: str, market: str, side: str = "YES") -> int:
    global _signal_counter
    _signal_counter += 1
    sid = tk.record_whale_signal(WhaleSignal(
        whale_address=whale,
        market_id=market,
        side=side,
        signal_type="open",
        price=0.5,
        size_usd=100.0,
        whale_fill_ts=1_700_000_000_000,
        detected_at_ts=1_700_000_001_000,
        detection_latency_ms=1000,
        raw_json="{}",
        transaction_hash=f"tx-{_signal_counter}-{whale}-{market}",
    ))
    assert sid is not None
    return sid


def _paper(
    tk: Tracker, *, sid: int, market: str, side: str, strategy: str,
    entry: float = 0.5,
) -> int:
    return tk.record_paper_trade(PaperTrade(
        strategy_id=strategy,
        triggering_signal_id=sid,
        market_id=market,
        side=side,
        size_usd=100.0,
        decided_at_ts=1_700_000_001_500,
        entry_price=entry,
    ))


def _resolve_copy(tk: Tracker, pt_id: int, outcome: str, side: str) -> None:
    pnl = 100.0 if outcome == side else -100.0
    tk.resolve_paper_trade(pt_id, outcome, pnl, 1_700_000_099_000)


def _resolve_market(
    wp: WhalePerf,
    tk: Tracker,
    *,
    whale: str,
    market: str,
    side: str,
    market_outcome: str,
) -> tuple[float, float]:
    """Resolve one copy_whale row and apply trust update. Returns (α, β)."""
    sid = _signal(tk, whale=whale, market=market, side=side)
    pid = _paper(tk, sid=sid, market=market, side=side, strategy="copy_whale")
    _resolve_copy(tk, pid, market_outcome, side)
    wp.update_trust_for_resolved_market(market, market_outcome)
    return tk.get_whale_trust(whale)


def main() -> int:
    wp_cls = WhalePerf

    # 1–2: pure beta_update
    a, b = wp_cls.beta_update(7.0, 3.0, True)
    assert a == 8.0 and b == 3.0
    a, b = wp_cls.beta_update(7.0, 3.0, False)
    assert a == 7.0 and b == 4.0
    print("  beta_update win/loss   OK")

    with tempfile.TemporaryDirectory() as td:
        tk = _tracker(td)
        wp = WhalePerf(tk, _CFG)

        # 3: cold-start win → Beta(8, 3)
        whale_cw = "0xCOLDWIN000000000000000000000000001"
        ab = _resolve_market(
            wp, tk, whale=whale_cw, market="m_cold_win",
            side="YES", market_outcome="YES",
        )
        assert ab == (8.0, 3.0), ab
        print("  cold-start win → Beta(8,3)   OK")

        # 4: cold-start loss → Beta(7, 4)
        whale_cl = "0xCOLDLOSS00000000000000000000000001"
        ab = _resolve_market(
            wp, tk, whale=whale_cl, market="m_cold_loss",
            side="YES", market_outcome="NO",
        )
        assert ab == (7.0, 4.0), ab
        print("  cold-start loss → Beta(7,4)   OK")

        # 5: mixed 3W/2L → Beta(10, 5)
        whale_mix = "0xMIXED00000000000000000000000000001"
        for i, win in enumerate([True, True, True, False, False]):
            m = f"m_mix_{i}"
            _resolve_market(
                wp, tk, whale=whale_mix, market=m,
                side="YES", market_outcome="YES" if win else "NO",
            )
        ab = tk.get_whale_trust(whale_mix)
        assert ab == (10.0, 5.0), ab
        print("  mixed 3W/2L → Beta(10,5)   OK")

        # 6: manual seed 0.9 + 1 win → Beta(10, 1)
        whale_ms = "0xMANSEED000000000000000000000000001"
        wp.seed_manual_trust(whale_ms, 0.9)
        ab = _resolve_market(
            wp, tk, whale=whale_ms, market="m_man_win",
            side="YES", market_outcome="YES",
        )
        assert ab == (10.0, 1.0), ab
        print("  seed 0.9 + 1 win → Beta(10,1)   OK")

        # 7: floor 0.0 — manual seed at 0 and post-loss mean below old 0.1 floor
        whale_bad = "0xBAD0000000000000000000000000000001"
        _, _, m0 = wp.seed_manual_trust(whale_bad, 0.0)
        assert m0 == 0.0
        for i in range(40):
            m = f"m_loss_{i}"
            sid = _signal(tk, whale=whale_bad, market=m)
            pid = _paper(tk, sid=sid, market=m, side="YES", strategy="copy_whale")
            _resolve_copy(tk, pid, "NO", "YES")
            wp.update_trust_for_resolved_market(m, "NO")
        row = tk.get_whale_trust_row(whale_bad)
        assert row is not None and row["trust_mean"] < 0.1, row
        print("  trust_mean can go below 0.1 (no sentimental floor)   OK")

        # 8: ceiling 0.95
        whale_good = "0xGOOD00000000000000000000000000001"
        for i in range(50):
            m = f"m_win_{i}"
            sid = _signal(tk, whale=whale_good, market=m)
            pid = _paper(tk, sid=sid, market=m, side="YES", strategy="copy_whale")
            _resolve_copy(tk, pid, "YES", "YES")
            wp.update_trust_for_resolved_market(m, "YES")
        row = tk.get_whale_trust_row(whale_good)
        assert row is not None and row["trust_mean"] == 0.95, row
        print("  trust_mean ceiling 0.95   OK")

        # 9: stub_blend does not update trust
        whale_stub = "0xSTUB00000000000000000000000000001"
        m5 = "m_stub_only"
        sid5 = _signal(tk, whale=whale_stub, market=m5)
        pid_stub = _paper(tk, sid=sid5, market=m5, side="YES", strategy="stub_blend")
        _resolve_copy(tk, pid_stub, "YES", "YES")
        r5 = wp.update_trust_for_resolved_market(m5, "YES")
        assert r5.updates_applied == 0
        assert tk.get_whale_trust_row(whale_stub) is None
        print("  stub_blend skipped for trust   OK")

        # 10: INVALID outcome
        whale_inv = "0xINV0000000000000000000000000000001"
        m6 = "m_invalid"
        sid6 = _signal(tk, whale=whale_inv, market=m6)
        pid6 = _paper(tk, sid=sid6, market=m6, side="YES", strategy="copy_whale")
        tk.resolve_paper_trade(pid6, "INVALID", 0.0, 1_700_000_099_000)
        r6 = wp.update_trust_for_resolved_market(m6, "INVALID")
        assert r6.updates_applied == 0
        assert tk.get_whale_trust_row(whale_inv) is None
        print("  INVALID outcome → no trust update   OK")

        # 11: idempotency — full posterior (α, β) unchanged on re-call
        whale_idem = "0xIDEM00000000000000000000000000001"
        m7 = "m_idem"
        sid7 = _signal(tk, whale=whale_idem, market=m7)
        pid7 = _paper(tk, sid=sid7, market=m7, side="YES", strategy="copy_whale")
        _resolve_copy(tk, pid7, "YES", "YES")
        r7a = wp.update_trust_for_resolved_market(m7, "YES")
        assert r7a.updates_applied == 1
        posterior1 = tk.get_whale_trust(whale_idem)
        r7b = wp.update_trust_for_resolved_market(m7, "YES")
        assert r7b.updates_applied == 0
        assert r7b.updates_skipped_idempotent == 1
        posterior2 = tk.get_whale_trust(whale_idem)
        assert posterior1 == posterior2, (posterior1, posterior2)
        print("  idempotent: posterior unchanged on re-call   OK")

        # 12–13: candidate_blacklist SET below threshold, UNSET on recovery
        whale_bl = "0xBL00000000000000000000000000000001"
        wp.seed_manual_trust(whale_bl, 0.2)
        below = wp.auto_blacklist_check()
        assert whale_bl in below
        assert tk.get_whale_trust_row(whale_bl)["candidate_blacklist"] == 1
        print("  candidate_blacklist SET below 0.35   OK")
        whale_rec = "0xRECOV000000000000000000000000001"
        wp.seed_manual_trust(whale_rec, 0.34)
        wp.auto_blacklist_check()
        assert tk.get_whale_trust_row(whale_rec)["candidate_blacklist"] == 1
        _resolve_market(
            wp, tk, whale=whale_rec, market="m_rec_win",
            side="YES", market_outcome="YES",
        )
        wp.auto_blacklist_check()
        row_rec = tk.get_whale_trust_row(whale_rec)
        assert row_rec["trust_mean"] >= 0.35, row_rec
        assert row_rec["candidate_blacklist"] == 0, row_rec
        print("  candidate_blacklist UNSET on recovery above 0.35   OK")

        # 14: processed_trust_updates row
        n10 = tk._conn.execute(
            "SELECT COUNT(*) FROM processed_trust_updates"
        ).fetchone()[0]
        assert n10 >= 1
        print(f"  processed_trust_updates rows={n10}   OK")

        # 16: two signals same market → two updates
        whale_two = "0xTWO0000000000000000000000000000001"
        m12 = "m_two_sig"
        s12a = _signal(tk, whale=whale_two, market=m12, side="YES")
        s12b = _signal(tk, whale=whale_two, market=m12, side="YES")
        p12a = _paper(tk, sid=s12a, market=m12, side="YES", strategy="copy_whale")
        p12b = _paper(tk, sid=s12b, market=m12, side="YES", strategy="copy_whale")
        _resolve_copy(tk, p12a, "YES", "YES")
        _resolve_copy(tk, p12b, "YES", "YES")
        r12 = wp.update_trust_for_resolved_market(m12, "YES")
        assert r12.updates_applied == 2
        alpha12, _ = tk.get_whale_trust(whale_two, prior_alpha=7, prior_beta=3)
        assert alpha12 == 9.0  # 7+1+1
        print("  two signals → two Beta steps   OK")

        tk.close()

    # 15: resolution_poller hook
    with tempfile.TemporaryDirectory() as td:
        tk = _tracker(td)
        wp = WhalePerf(tk, _CFG)
        whale_hook = "0xHOOK00000000000000000000000000001"
        m11 = "m_hook"
        sid11 = _signal(tk, whale=whale_hook, market=m11)
        pid11 = _paper(tk, sid=sid11, market=m11, side="YES", strategy="copy_whale")

        class _FakeClient:
            def get_market(self, mid):
                return {
                    "closed": True,
                    "is_50_50_outcome": False,
                    "tokens": [
                        {"outcome": "Yes", "winner": True, "price": 1.0},
                        {"outcome": "No", "winner": False, "price": 0.0},
                    ],
                }

        rp = ResolutionPoller(_FakeClient(), tk, whale_perf=wp)
        rp.poll_once()
        row = tk.get_whale_trust_row(whale_hook)
        assert row is not None, "hook should have updated trust"
        assert row["alpha"] == 8.0
        print("  resolution_poller → whale_perf hook   OK")
        tk.close()

    print("\nmodule 9 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
