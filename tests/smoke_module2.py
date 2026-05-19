"""Smoke test for Module 2 (bot/models.py).

Run from project root:
    .venv/bin/python tests/smoke_module2.py

Exits non-zero on any failure. Designed to complete in <1 second.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import PHASE, __version__  # noqa: E402
from bot.models import (  # noqa: E402
    BookSnapshot,
    MarketSnapshot,
    PaperTrade,
    PendingPaperTrade,
    ReadinessCheck,
    ReadinessReport,
    WhaleSignal,
)


def _assert_frozen(obj, attr: str, new_value) -> None:
    try:
        setattr(obj, attr, new_value)
    except FrozenInstanceError:
        return
    raise AssertionError(f"{type(obj).__name__}.{attr} should be frozen")


def main() -> int:
    # ── package marker ─────────────────────────────────────────────────
    assert PHASE == "A", f"bot.PHASE must be 'A' in this build, got {PHASE!r}"
    assert __version__.endswith("phase-a")
    print(f"  bot.__init__: PHASE={PHASE} version={__version__}")

    # ── frozen observations ────────────────────────────────────────────
    ws = WhaleSignal(
        whale_address="0xabc123",
        market_id="m1",
        side="YES",
        signal_type="open",
        price=0.42,
        size_usd=1000.0,
        whale_fill_ts=1_700_000_001_000,
        detected_at_ts=1_700_000_002_500,
        detection_latency_ms=1500,
        raw_json='{"trade":"data"}',
        whale_label="alpha",
    )
    _assert_frozen(ws, "price", 0.99)
    _assert_frozen(ws, "side", "NO")

    ms = MarketSnapshot(market_id="m1", ts=1_700_000_000_000, mid=0.5)
    _assert_frozen(ms, "mid", 0.6)

    bs = BookSnapshot(
        market_id="m1",
        ts=1_700_000_000_000,
        book_json='{"bids":[[0.49,100]],"asks":[[0.51,100]]}',
    )
    _assert_frozen(bs, "ts", 0)
    print("  models: WhaleSignal / MarketSnapshot / BookSnapshot frozen   OK")

    # ── mutable state ──────────────────────────────────────────────────
    pt = PaperTrade(
        strategy_id="copy_whale",
        triggering_signal_id=1,
        market_id="m1",
        side="YES",
        size_usd=50.0,
        decided_at_ts=1_700_000_001_500,
        entry_price=0.45,
    )
    assert pt.resolved_outcome is None and pt.resolved_at_ts is None
    pt.resolved_at_ts = 1_700_000_900_000
    pt.resolved_outcome = "YES"
    pt.pnl_usd = 61.11
    assert pt.resolved_outcome == "YES"
    assert pt.pnl_usd == 61.11

    # void-row pattern: entry_price=None, pnl_usd=0
    void = PaperTrade(
        strategy_id="whale_side_random_timing",
        triggering_signal_id=2,
        market_id="m1",
        side="YES",
        size_usd=100.0,
        decided_at_ts=1_700_000_500_000,
        entry_price=None,
        resolved_outcome="VOIDED_MISSED_WINDOW",
        pnl_usd=0.0,
    )
    assert void.entry_price is None
    assert void.resolved_outcome == "VOIDED_MISSED_WINDOW"

    ppt = PendingPaperTrade(
        strategy_id="whale_side_random_timing",
        triggering_signal_id=1,
        market_id="m1",
        side="YES",
        size_usd=100.0,
        scheduled_for_ts=1_700_086_400_000,
        status="pending",
        created_at_ts=1_700_000_000_000,
    )
    ppt.status = "materialised"
    assert ppt.status == "materialised"
    print("  models: PaperTrade / PendingPaperTrade mutable               OK")

    # ── readiness types (frozen) ───────────────────────────────────────
    rc1 = ReadinessCheck(name="resolved_trades", required=50, current=47, passed=False)
    rc2 = ReadinessCheck(name="distinct_whales", required=5, current=5, passed=True)
    _assert_frozen(rc1, "passed", True)
    rr = ReadinessReport(
        checks=[rc1, rc2],
        all_passed=False,
        summary="47/50 trades · 5/5 whales · 12/14 days",
    )
    _assert_frozen(rr, "all_passed", True)
    _assert_frozen(rr, "summary", "")
    print("  models: ReadinessCheck / ReadinessReport frozen              OK")

    # ── JSON roundtrip via dataclasses_json ────────────────────────────
    j = ws.to_json()
    ws2 = WhaleSignal.from_json(j)
    assert ws2 == ws, f"WhaleSignal JSON roundtrip mismatch: {ws2!r} vs {ws!r}"

    j2 = pt.to_json()
    pt2 = PaperTrade.from_json(j2)
    assert pt2 == pt, f"PaperTrade JSON roundtrip mismatch: {pt2!r} vs {pt!r}"

    j3 = rr.to_json()
    rr2 = ReadinessReport.from_json(j3)
    assert rr2 == rr, f"ReadinessReport JSON roundtrip mismatch"
    print("  models: dataclasses_json roundtrip (3 types)                 OK")

    # ── type discipline: timestamps are int ────────────────────────────
    int_fields = (
        ws.whale_fill_ts, ws.detected_at_ts, ws.detection_latency_ms,
        ms.ts, bs.ts,
        pt.decided_at_ts, pt.resolved_at_ts,
        ppt.scheduled_for_ts, ppt.created_at_ts,
    )
    for v in int_fields:
        assert isinstance(v, int), f"timestamps must be int ms epoch, got {type(v)}"
    print("  models: timestamp type discipline (int ms epoch)             OK")

    # ── enum-literal sanity (no validation, just doc-spec adherence) ───
    assert pt.strategy_id in {
        "copy_whale", "stub_blend", "null_random_side", "whale_side_random_timing"
    }
    assert ws.signal_type in {"open", "add", "exit", "flip"}
    assert ws.side in {"YES", "NO"}
    assert void.resolved_outcome in {
        "YES", "NO", "INVALID", "VOIDED_PRE_FILL", "VOIDED_MISSED_WINDOW"
    }
    print("  models: literal-string fields match PLAN.md §6 enums         OK")

    print("\nmodule 2 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
