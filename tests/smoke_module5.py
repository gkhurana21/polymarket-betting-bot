"""Smoke test for Module 5 (bot/market_scanner.py).

Covers all nine verifications from the build directive:
  1. _compute_mid: synthetic + empty-side
  2. _compute_spread_bps: synthetic
  3. _compute_implied_prob: YES + NO branches
  4. _extract_depth_n: 25-level book truncated to 10 each side, ordered
  5. _filter_markets: drops below volume / non-Yes/No outcomes / blacklist
                      category / blacklist keyword; keeps a clean market
  6. _build_market_snapshot + _build_book_snapshot dataclass JSON roundtrip
  7. scan_once integration vs live CLOB: ≥ 1 market passes, DB row deltas
                                          match ScanResult counts
  8. Edge: bids-only book → market skipped without error
  9. Edge: get_order_book raises CircuitBreakerOpen mid-scan → scan ends
           gracefully, partial counts, error recorded

Run from project root:
    .venv/bin/python tests/smoke_module5.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.client import CircuitBreakerOpen, ClobReadOnlyClient  # noqa: E402
from bot.market_scanner import MarketScanner, ScanResult  # noqa: E402
from bot.models import BookSnapshot, MarketSnapshot  # noqa: E402
from bot.tracker import Tracker  # noqa: E402


# ═════════════════════════  helpers / fixtures  ═════════════════════════

_CFG_BASE: dict = {
    "market_filters": {
        "min_volume_24h_usd": 0,
        "min_liquidity_usd": 0,
        "max_spread_bps": 10_000,
        "outcome_labels": ["Yes", "No"],
        "whitelist_categories": [],
        "blacklist_categories": ["crypto-prices"],
        "blacklist_keywords": ["btc price"],
    },
    "book_snapshot": {"depth_levels": 10},
}


def _market(
    *,
    condition_id: str = "0xCID",
    outcomes=("Yes", "No"),
    category=None,
    question="Some question",
    volume=None,
) -> dict:
    return {
        "condition_id": condition_id,
        "active": True,
        "closed": False,
        "archived": False,
        "accepting_orders": True,
        "enable_order_book": True,
        "category": category,
        "question": question,
        "volume_24h_usd": volume,
        "tokens": [
            {"token_id": f"{condition_id}-tok-{o}", "outcome": o} for o in outcomes
        ],
    }


def _book(bids, asks):
    return {"bids": list(bids), "asks": list(asks)}


# ═════════════════════════════════════════════════════════════════════════


LIVE = os.environ.get("LIVE_API_TESTS", "1") == "1"   # 0 skips check 7, the only one that calls Polymarket


def main() -> int:
    scanner = MarketScanner(client=None, tracker=None, config=_CFG_BASE)  # type: ignore[arg-type]

    # ── 1: _compute_mid ────────────────────────────────────────────────
    assert scanner._compute_mid(_book([[0.49, 100]], [[0.51, 100]])) == 0.50
    assert scanner._compute_mid(_book([[0.40, 50], [0.45, 100]], [[0.50, 80]])) == 0.475
    assert scanner._compute_mid(_book([], [[0.51, 100]])) is None
    assert scanner._compute_mid(_book([[0.49, 100]], [])) is None
    assert scanner._compute_mid(_book([], [])) is None
    print("  _compute_mid: synthetic + both empty-side cases               OK")

    # ── 2: _compute_spread_bps ─────────────────────────────────────────
    # 0.51 − 0.49 = 0.02; mid = 0.50; (0.02 / 0.50) × 10_000 = 400 bps.
    # Use tolerance — IEEE-754 makes 0.51-0.49 ≠ exactly 0.02.
    sb = scanner._compute_spread_bps(_book([[0.49, 1]], [[0.51, 1]]))
    assert sb is not None and abs(sb - 400.0) < 1e-6, sb
    assert scanner._compute_spread_bps(_book([], [[0.5, 1]])) is None
    print(f"  _compute_spread_bps: {sb:.4f} bps for 2¢ on $0.50 mid          OK")

    # ── 3: _compute_implied_prob ───────────────────────────────────────
    bk = _book([[0.49, 1]], [[0.51, 1]])
    assert scanner._compute_implied_prob(bk, "Yes") == 0.50
    assert scanner._compute_implied_prob(bk, "No") == 0.50
    assert abs(scanner._compute_implied_prob(_book([[0.59, 1]], [[0.61, 1]]), "Yes") - 0.60) < 1e-9
    assert abs(scanner._compute_implied_prob(_book([[0.59, 1]], [[0.61, 1]]), "No") - 0.40) < 1e-9
    print("  _compute_implied_prob: YES + NO both correct                  OK")

    # ── 4: _extract_depth_n ────────────────────────────────────────────
    # 25 levels each side, intentionally shuffled order
    rng_bids = [[0.50 - i * 0.001, 100 + i] for i in range(25)]
    rng_asks = [[0.51 + i * 0.001, 100 + i] for i in range(25)]
    # shuffle: reverse bids and asks to verify sort happens
    shuffled = _book(list(reversed(rng_bids)), list(reversed(rng_asks)))
    out = scanner._extract_depth_n(shuffled)
    assert len(out["bids"]) == 10 and len(out["asks"]) == 10
    # bids descending — best (highest) first
    assert out["bids"][0][0] == 0.50
    assert out["bids"][1][0] == 0.499
    assert all(out["bids"][i][0] >= out["bids"][i + 1][0] for i in range(9))
    # asks ascending — best (lowest) first
    assert out["asks"][0][0] == 0.51
    assert out["asks"][1][0] == 0.511
    assert all(out["asks"][i][0] <= out["asks"][i + 1][0] for i in range(9))
    print("  _extract_depth_n: 25→10 each side, sorted best-first          OK")

    # ── 5: _filter_markets ─────────────────────────────────────────────
    cfg_strict = {
        "market_filters": {
            "min_volume_24h_usd": 10_000,
            "min_liquidity_usd": 0,
            "max_spread_bps": 10_000,
            "outcome_labels": ["Yes", "No"],
            "whitelist_categories": [],
            "blacklist_categories": ["crypto-prices"],
            "blacklist_keywords": ["btc price"],
        },
        "book_snapshot": {"depth_levels": 10},
    }
    s2 = MarketScanner(client=None, tracker=None, config=cfg_strict)  # type: ignore[arg-type]
    raw = [
        _market(condition_id="m_clean", volume=50_000),                     # KEEP
        _market(condition_id="m_lowvol", volume=100),                        # drop: volume
        _market(condition_id="m_threeway",                                   # drop: outcomes
                outcomes=("Team A", "Team B", "Team C")),
        _market(condition_id="m_crypto", volume=50_000,                      # drop: category
                category="Crypto-Prices"),
        _market(condition_id="m_btc", volume=50_000,                         # drop: keyword
                question="Will BTC price exceed 100k?"),
    ]
    kept = s2._filter_markets(raw)
    kept_ids = {m["condition_id"] for m in kept}
    assert kept_ids == {"m_clean"}, f"expected only m_clean, got {kept_ids}"
    print(f"  _filter_markets: {len(raw)} → {len(kept)}; only m_clean survives "
          f"(vol/outcome/cat/keyword drops)  OK")

    # ── 6: _build_*_snapshot + JSON roundtrip ───────────────────────────
    m = _market(condition_id="m_round", volume=12345.0, category="Politics")
    bk = _book([[0.49, 100], [0.48, 200]], [[0.51, 100], [0.52, 200]])
    ms_snap = scanner._build_market_snapshot(m, bk, ts=1_700_000_000_000)
    assert ms_snap is not None
    assert abs(ms_snap.mid - 0.5) < 1e-9
    assert ms_snap.spread_bps is not None and abs(ms_snap.spread_bps - 400.0) < 1e-6
    assert ms_snap.volume_24h_usd == 12345.0
    assert ms_snap.category == "Politics"
    assert ms_snap.market_id == "m_round"
    j = ms_snap.to_json()
    ms_back = MarketSnapshot.from_json(j)
    assert ms_back == ms_snap
    bs_snap = scanner._build_book_snapshot("m_round", bk, ts=1_700_000_000_000)
    parsed = json.loads(bs_snap.book_json)
    assert "bids" in parsed and "asks" in parsed
    assert len(parsed["bids"]) == 2 and len(parsed["asks"]) == 2
    bs_back = BookSnapshot.from_json(bs_snap.to_json())
    assert bs_back == bs_snap
    # one-sided book → returns None
    assert scanner._build_market_snapshot(m, _book([[0.5, 1]], []), ts=1) is None
    print("  _build_*_snapshot: dataclasses + JSON roundtrip + one-sided=None  OK")

    # ── 8: edge — bids-only market is skipped during scan_once ──────────
    class _Fake1:
        def get_active_markets(self, limit):
            return [_market(condition_id="m_oneside", volume=99999)]
        def get_order_book(self, token_id):
            return _book([[0.49, 100]], [])  # bids only

    with tempfile.TemporaryDirectory() as td:
        tk = Tracker(db_path=Path(td) / "t.db", audit_log_path=Path(td) / "a.jsonl")
        sc = MarketScanner(client=_Fake1(), tracker=tk, config=_CFG_BASE)  # type: ignore[arg-type]
        r = sc.scan_once()
        assert r.markets_fetched == 1
        assert r.markets_passed_filter == 0
        assert r.market_snapshots_written == 0
        assert r.book_snapshots_written == 0
        assert r.errors == 0
        # No rows landed
        n_ms = tk._conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        n_bs = tk._conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        assert n_ms == 0 and n_bs == 0
        tk.close()
    print("  edge: bids-only book → skipped without error                  OK")

    # ── 9: edge — CB OPENs mid-scan → scan halts cleanly ────────────────
    class _FakeCB:
        def __init__(self):
            self._book_calls = 0
        def get_active_markets(self, limit):
            return [
                _market(condition_id=f"m_cb_{i}", volume=99999) for i in range(5)
            ]
        def get_order_book(self, token_id):
            self._book_calls += 1
            if self._book_calls > 2:
                raise CircuitBreakerOpen("forced CBO for test")
            return _book([[0.49, 100]], [[0.51, 100]])

    with tempfile.TemporaryDirectory() as td:
        tk = Tracker(db_path=Path(td) / "t.db", audit_log_path=Path(td) / "a.jsonl")
        fc = _FakeCB()
        sc = MarketScanner(client=fc, tracker=tk, config=_CFG_BASE)  # type: ignore[arg-type]
        r = sc.scan_once()
        # 5 markets fetched; first 2 books succeed; 3rd raises CBO; loop breaks.
        assert r.markets_fetched == 5
        assert fc._book_calls == 3, f"expected 3 book calls, got {fc._book_calls}"
        assert r.markets_passed_filter == 2
        assert r.market_snapshots_written == 2
        assert r.book_snapshots_written == 2
        assert r.errors == 1
        assert any(e.error_type == "CircuitBreakerOpen" for e in r.error_details)
        # Two rows actually landed.
        n_ms = tk._conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        n_bs = tk._conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        assert n_ms == 2 and n_bs == 2
        tk.close()
    print("  edge: CBO mid-scan → 2 written, scan halted, error recorded   OK")

    # ── 7: live integration ────────────────────────────────────────────
    if LIVE:
        with tempfile.TemporaryDirectory() as td:
            tk = Tracker(db_path=Path(td) / "t.db", audit_log_path=Path(td) / "a.jsonl")
            live = ClobReadOnlyClient(
                retry_max_attempts=3,
                retry_initial_backoff_seconds=0.001,
                retry_max_backoff_seconds=0.01,
                cb_fail_threshold=20,                  # generous for live integration
                cb_reset_seconds=60.0,
                sleep_fn=lambda _s: None,
            )
            cfg_live = {
                "market_filters": {
                    "min_volume_24h_usd": 0,           # live data has no volume field
                    "min_liquidity_usd": 0,
                    "max_spread_bps": 5_000,           # 50% — generous for sampling markets
                    "outcome_labels": ["Yes", "No"],
                    "whitelist_categories": [],
                    "blacklist_categories": [],
                    "blacklist_keywords": [],
                },
                "book_snapshot": {"depth_levels": 10},
            }
            sc = MarketScanner(client=live, tracker=tk, config=cfg_live)
            # Cap fetch to keep the smoke fast — first 8 sampling markets.
            r = sc.scan_once(fetch_limit=8)
            n_ms = tk._conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
            n_bs = tk._conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            assert r.market_snapshots_written == n_ms, (
                f"ScanResult.market_snapshots_written={r.market_snapshots_written} "
                f"!= DB count {n_ms}"
            )
            assert r.book_snapshots_written == n_bs
            assert n_ms >= 1, "expected at least 1 market to pass filters and persist"
            # Spot-check that one persisted row has plausible mid in (0, 1).
            row = tk._conn.execute(
                "SELECT mid, spread_bps, liquidity_usd, question FROM market_snapshots LIMIT 1"
            ).fetchone()
            assert 0 < row["mid"] < 1, f"mid out of range: {row['mid']}"
            assert row["spread_bps"] is None or row["spread_bps"] >= 0
            tk.close()
            print(f"  live integration: fetched={r.markets_fetched} "
                  f"passed={r.markets_passed_filter} ms_rows={n_ms} bs_rows={n_bs} "
                  f"errors={r.errors}  OK")
    else:
        print("  live integration: SKIPPED (LIVE_API_TESTS=0)")

    print("\nmodule 5 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
