"""Smoke test for Module 6 (bot/resolution_poller.py).

(Module 6 in the swapped build order — resolution_poller first, whale_tracker
next; see PLAN.md §14.)

Verifications:
  1. _resolution_outcome unit:
     - active market (closed=False) → None
     - YES-resolved → "YES"
     - NO-resolved → "NO"
     - is_50_50_outcome → "INVALID"
     - no winner / multi-winner → "INVALID"
     - non-Yes/No outcome (sports) → "INVALID"
  2. _compute_pnl unit: YES win, NO win, INVALID, wrong side, edge cases
  3. End-to-end settlement via mocked client:
     - Insert whale_signal + paper_trades for a market
     - Mock client returns closed YES-winner
     - poll_once settles all open trades with correct PnL
  4. Idempotency: re-poll → markets_resolved_this_tick == 0, no re-settlement
  5. Already-resolved cache path: pre-populate resolved_markets, run poll
     with a "broken" client (raises if get_market is called); straggler
     paper_trade gets settled from cache without API call
  6. Edge: get_market raises CircuitBreakerOpen mid-poll → tick halts
     gracefully, partial counts, error in result

Run from project root:
    .venv/bin/python tests/smoke_module6.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.client import CircuitBreakerOpen  # noqa: E402
from bot.models import PaperTrade, WhaleSignal  # noqa: E402
from bot.resolution_poller import ResolutionPoller  # noqa: E402
from bot.tracker import Tracker  # noqa: E402


# ═════════════════════════  fixtures  ═════════════════════════

def _market_active(cid="m_active"):
    return {
        "condition_id": cid,
        "closed": False,
        "archived": False,
        "is_50_50_outcome": False,
        "tokens": [
            {"token_id": "t_yes", "outcome": "Yes", "winner": False, "price": 0.5},
            {"token_id": "t_no",  "outcome": "No",  "winner": False, "price": 0.5},
        ],
    }


def _market_yes_won(cid="m_yes"):
    return {
        "condition_id": cid,
        "closed": True,
        "archived": False,
        "is_50_50_outcome": False,
        "tokens": [
            {"token_id": "t_yes", "outcome": "Yes", "winner": True,  "price": 1.0},
            {"token_id": "t_no",  "outcome": "No",  "winner": False, "price": 0.0},
        ],
    }


def _market_no_won(cid="m_no"):
    return {
        "condition_id": cid,
        "closed": True,
        "archived": False,
        "is_50_50_outcome": False,
        "tokens": [
            {"token_id": "t_yes", "outcome": "Yes", "winner": False, "price": 0.0},
            {"token_id": "t_no",  "outcome": "No",  "winner": True,  "price": 1.0},
        ],
    }


def _market_invalid_5050(cid="m_5050"):
    return {
        "condition_id": cid,
        "closed": True,
        "archived": False,
        "is_50_50_outcome": True,
        "tokens": [
            {"token_id": "t_yes", "outcome": "Yes", "winner": False, "price": 0.5},
            {"token_id": "t_no",  "outcome": "No",  "winner": False, "price": 0.5},
        ],
    }


def _market_invalid_no_winner(cid="m_nowin"):
    return {
        "condition_id": cid,
        "closed": True,
        "archived": False,
        "is_50_50_outcome": False,
        "tokens": [
            {"token_id": "t_yes", "outcome": "Yes", "winner": False, "price": 0.0},
            {"token_id": "t_no",  "outcome": "No",  "winner": False, "price": 0.0},
        ],
    }


def _market_invalid_sports(cid="m_sports"):
    return {
        "condition_id": cid,
        "closed": True,
        "archived": False,
        "is_50_50_outcome": False,
        "tokens": [
            {"token_id": "t_a", "outcome": "Team A", "winner": True,  "price": 1.0},
            {"token_id": "t_b", "outcome": "Team B", "winner": False, "price": 0.0},
        ],
    }


class _FakeClient:
    """Minimal client stub for ResolutionPoller. Maps cid → market dict.
    Optionally raises CircuitBreakerOpen after N calls."""

    def __init__(self, markets: dict[str, dict], cbo_after: int | None = None):
        self._markets = markets
        self._cbo_after = cbo_after
        self.get_market_calls = 0

    def get_market(self, condition_id: str):
        self.get_market_calls += 1
        if self._cbo_after is not None and self.get_market_calls > self._cbo_after:
            raise CircuitBreakerOpen("forced for test")
        if condition_id not in self._markets:
            raise ValueError(f"unknown market in fake client: {condition_id}")
        return self._markets[condition_id]


def _seed_signal(tracker: Tracker, whale="0xW1", market="m_yes") -> int:
    """Insert one whale signal so paper_trades has a valid FK to attach to."""
    s = WhaleSignal(
        whale_address=whale,
        market_id=market,
        side="YES",
        signal_type="open",
        price=0.42,
        size_usd=100.0,
        whale_fill_ts=1_700_000_000_000,
        detected_at_ts=1_700_000_001_000,
        detection_latency_ms=1000,
        raw_json="{}",
    )
    return tracker.record_whale_signal(s)


def _seed_paper_trade(
    tracker: Tracker, *,
    signal_id: int, market: str, side: str,
    entry_price: float, size_usd: float, strategy_id: str = "copy_whale",
) -> int:
    pt = PaperTrade(
        strategy_id=strategy_id,
        triggering_signal_id=signal_id,
        market_id=market,
        side=side,
        size_usd=size_usd,
        decided_at_ts=1_700_000_001_500,
        entry_price=entry_price,
    )
    return tracker.record_paper_trade(pt)


# ═════════════════════════════════════════════════════════════════════════


def main() -> int:
    # ── 1: _resolution_outcome unit ────────────────────────────────────
    rp_cls = ResolutionPoller
    assert rp_cls._resolution_outcome(_market_active()) is None
    assert rp_cls._resolution_outcome(_market_yes_won()) == "YES"
    assert rp_cls._resolution_outcome(_market_no_won()) == "NO"
    assert rp_cls._resolution_outcome(_market_invalid_5050()) == "INVALID"
    assert rp_cls._resolution_outcome(_market_invalid_no_winner()) == "INVALID"
    assert rp_cls._resolution_outcome(_market_invalid_sports()) == "INVALID"
    # multi-winner → INVALID
    multi_win = _market_yes_won()
    multi_win["tokens"][1]["winner"] = True
    assert rp_cls._resolution_outcome(multi_win) == "INVALID"
    print("  _resolution_outcome: 7 cases (active/YES/NO/5050/no-win/sports/multi)  OK")

    # ── 2: _compute_pnl unit ───────────────────────────────────────────
    # Bought YES at 0.40 with $100 → 250 shares.  YES wins → 250×$1 − $100 = +$150.
    assert rp_cls._compute_pnl("YES", 0.40, 100.0, "YES") == 150.0
    # NO wins on a YES bet → lose $100.
    assert rp_cls._compute_pnl("YES", 0.40, 100.0, "NO") == -100.0
    # NO bought at 0.60 with $60 → 100 shares.  NO wins → $40 profit.
    assert abs(rp_cls._compute_pnl("NO", 0.60, 60.0, "NO") - 40.0) < 1e-9
    # YES bet on INVALID market → 250 shares × $0.50 − $100 = +$25.
    assert abs(rp_cls._compute_pnl("YES", 0.40, 100.0, "INVALID") - 25.0) < 1e-9
    # INVALID at price=0.50 (entry mid) → 200 shares × $0.50 − $100 = $0.
    assert rp_cls._compute_pnl("YES", 0.50, 100.0, "INVALID") == 0.0
    # Edge: zero/None entry_price → 0 (defensive).
    assert rp_cls._compute_pnl("YES", 0.0, 100.0, "YES") == 0.0
    print("  _compute_pnl: YES-win/NO-win/wrong-side/INVALID/edge   OK")

    # ── 3: end-to-end settlement via mocked client ─────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = Tracker(db_path=Path(td) / "t.db", audit_log_path=Path(td) / "a.jsonl")

        # One whale signal, three paper-trade rows in market m_yes (YES will win).
        sid_yes = _seed_signal(tk, whale="0xWA", market="m_yes")
        pt_copy_yes = _seed_paper_trade(
            tk, signal_id=sid_yes, market="m_yes", side="YES",
            entry_price=0.40, size_usd=100.0, strategy_id="copy_whale",
        )
        pt_stub_yes = _seed_paper_trade(
            tk, signal_id=sid_yes, market="m_yes", side="YES",
            entry_price=0.40, size_usd=100.0, strategy_id="stub_blend",
        )
        # Null-side bet on the wrong outcome — should land at −$100.
        pt_null_no = _seed_paper_trade(
            tk, signal_id=sid_yes, market="m_yes", side="NO",
            entry_price=0.60, size_usd=100.0, strategy_id="null_random_side",
        )
        # And a separate market that resolves NO-wins.
        sid_no = _seed_signal(tk, whale="0xWB", market="m_no")
        pt_no_win = _seed_paper_trade(
            tk, signal_id=sid_no, market="m_no", side="NO",
            entry_price=0.30, size_usd=60.0, strategy_id="copy_whale",
        )
        # And one in a still-active market — must NOT be touched.
        sid_act = _seed_signal(tk, whale="0xWC", market="m_active")
        pt_active = _seed_paper_trade(
            tk, signal_id=sid_act, market="m_active", side="YES",
            entry_price=0.50, size_usd=100.0, strategy_id="copy_whale",
        )

        markets = {
            "m_yes":    _market_yes_won("m_yes"),
            "m_no":     _market_no_won("m_no"),
            "m_active": _market_active("m_active"),
        }
        fc = _FakeClient(markets)
        rp = ResolutionPoller(client=fc, tracker=tk)
        r = rp.poll_once()

        assert r.markets_checked == 3, f"expected 3 markets checked, got {r.markets_checked}"
        assert r.markets_resolved_this_tick == 2, r.markets_resolved_this_tick
        assert r.paper_trades_settled == 4, r.paper_trades_settled
        assert r.errors == 0, r.error_details

        def _pt(pid: int):
            return tk._conn.execute(
                "SELECT side, entry_price, resolved_outcome, pnl_usd FROM paper_trades WHERE id=?",
                (pid,),
            ).fetchone()

        # m_yes: YES wins.
        row = _pt(pt_copy_yes)
        assert row["resolved_outcome"] == "YES"
        assert abs(row["pnl_usd"] - 150.0) < 1e-9, row["pnl_usd"]
        row = _pt(pt_stub_yes)
        assert row["resolved_outcome"] == "YES" and abs(row["pnl_usd"] - 150.0) < 1e-9
        row = _pt(pt_null_no)        # bet NO at 0.60, YES won → −$100
        assert row["resolved_outcome"] == "YES"
        assert row["pnl_usd"] == -100.0
        # m_no: NO wins. Bet NO at 0.30 → 200 shares × $1 − $60 = $140.
        row = _pt(pt_no_win)
        assert row["resolved_outcome"] == "NO"
        assert abs(row["pnl_usd"] - 140.0) < 1e-9, row["pnl_usd"]
        # m_active: untouched.
        row = _pt(pt_active)
        assert row["resolved_outcome"] is None and row["pnl_usd"] is None

        # resolved_markets cache populated for m_yes and m_no, not m_active.
        assert tk.get_resolved_market("m_yes") is not None
        assert tk.get_resolved_market("m_no") is not None
        assert tk.get_resolved_market("m_active") is None
        print(f"  e2e settle: 4 trades resolved (PnL +150,+150,−100,+140), "
              f"active untouched   OK")

        # ── 4: idempotency — re-poll, no further work ─────────────────
        get_calls_before = fc.get_market_calls
        r2 = rp.poll_once()
        assert r2.markets_resolved_this_tick == 0, r2.markets_resolved_this_tick
        assert r2.paper_trades_settled == 0, r2.paper_trades_settled
        # Only the still-active m_active is checked; cached m_yes/m_no skip the API.
        active_only_call = fc.get_market_calls - get_calls_before
        assert active_only_call == 1, (
            f"second tick should hit API only for m_active (1 call), got {active_only_call}"
        )
        print(f"  idempotency: re-poll → 0 resolved, 0 settled, "
              f"only m_active queried   OK")

        # ── 5: cache-path settlement of a straggler ───────────────────
        # Insert a NEW paper_trade for the already-resolved m_yes (e.g.
        # whale_side_random_timing materialised after resolution).
        pt_straggler = _seed_paper_trade(
            tk, signal_id=sid_yes, market="m_yes", side="YES",
            entry_price=0.20, size_usd=20.0,
            strategy_id="whale_side_random_timing",
        )
        # Use a strict client that REFUSES to be called for cached markets.
        class _StrictClient:
            def __init__(self, markets):
                self._markets = markets
                self.calls = []
            def get_market(self, mid):
                self.calls.append(mid)
                if mid in ("m_yes", "m_no"):
                    raise AssertionError(
                        f"cache-path violation: get_market called for cached {mid}"
                    )
                return self._markets[mid]
        sc = _StrictClient(markets)
        rp_strict = ResolutionPoller(client=sc, tracker=tk)
        r3 = rp_strict.poll_once()
        assert r3.errors == 0, r3.error_details
        assert r3.paper_trades_settled == 1, r3.paper_trades_settled
        assert sc.calls == ["m_active"], f"expected only m_active, got {sc.calls}"
        # Straggler resolved correctly.
        row = _pt(pt_straggler)
        assert row["resolved_outcome"] == "YES"
        # 20 / 0.20 = 100 shares × $1 − $20 = $80 profit.
        assert abs(row["pnl_usd"] - 80.0) < 1e-9, row["pnl_usd"]
        print(f"  cache-path: straggler settled from resolved_markets, "
              f"API NOT touched for cached markets   OK")

        tk.close()

    # ── 6: CBO mid-poll halts gracefully ───────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tk = Tracker(db_path=Path(td) / "t.db", audit_log_path=Path(td) / "a.jsonl")
        # Three pending markets, CBO fires on the 2nd get_market call.
        sid = _seed_signal(tk, whale="0xWX", market="m1")
        for cid in ("m1", "m2", "m3"):
            sid_i = _seed_signal(tk, whale="0xWX", market=cid)
            _seed_paper_trade(
                tk, signal_id=sid_i, market=cid, side="YES",
                entry_price=0.5, size_usd=100.0,
            )
        markets = {"m1": _market_yes_won("m1"), "m2": _market_yes_won("m2"),
                   "m3": _market_yes_won("m3")}
        fc = _FakeClient(markets, cbo_after=1)
        rp = ResolutionPoller(client=fc, tracker=tk)
        r = rp.poll_once()

        # m1 succeeds; m2 raises CBO → break. m3 not touched.
        # markets_checked = total candidates, NOT successful. errors = 1 (the CBO).
        assert r.markets_checked == 3, r.markets_checked
        assert r.markets_resolved_this_tick == 1, r.markets_resolved_this_tick
        assert r.paper_trades_settled == 1, r.paper_trades_settled
        assert r.errors == 1
        assert any(e.error_type == "CircuitBreakerOpen" for e in r.error_details), \
            r.error_details
        assert fc.get_market_calls == 2, (
            f"expected 2 API calls before halt, got {fc.get_market_calls}"
        )
        # m3 still has unresolved paper_trades — next tick should retry.
        unresolved = tk.get_unresolved_paper_trade_market_ids()
        assert "m3" in unresolved, unresolved
        # m2 also still unresolved (CBO interrupted before settlement).
        assert "m2" in unresolved, unresolved
        # m1 fully settled.
        assert "m1" not in unresolved
        tk.close()
    print(f"  CBO mid-poll: 1 settled, 2 untouched, halted at 2 API calls   OK")

    print("\nmodule 6 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
