"""Smoke test for Module 4 (bot/client.py).

Covers all seven verifications from the build directive:
  1. health_check() returns True against live CLOB
  2. get_active_markets(limit=5) returns 5 dicts with expected fields
  3. get_order_book(token) returns {bids, asks}
  4. get_leaderboard(limit=10) returns 10 entries
  5. Retry: ConnectionError × 2, success on 3rd → exactly 3 attempts
  6. Circuit breaker: 5 failures → 6th raises CircuitBreakerOpen w/o calling fn
  7. Read-only: place_* etc. raise NotImplementedError

Verifications 1-4 hit the live network and are skippable. Check 4 can no longer
pass: Polymarket retired the leaderboard endpoint (HTTP 410 since late 2026).

Run from project root:
    .venv/bin/python tests/smoke_module4.py                     # with the live checks
    LIVE_API_TESTS=0 .venv/bin/python tests/smoke_module4.py   # offline: checks 5-7 only
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.client import (  # noqa: E402
    CircuitBreakerOpen,
    ClobReadOnlyClient,
    _CircuitBreaker,
    _is_retriable,
)


LIVE = os.environ.get("LIVE_API_TESTS", "1") == "1"


def main() -> int:
    # Build a client with quick retry/CB params so we can run fast.
    client = ClobReadOnlyClient(
        retry_max_attempts=5,
        retry_initial_backoff_seconds=0.001,
        retry_max_backoff_seconds=0.01,
        cb_fail_threshold=5,
        cb_reset_seconds=60.0,
        sleep_fn=lambda _s: None,  # no real sleeps in smoke
    )

    if LIVE:
        # ── 1: health_check (live network) ─────────────────────────────────
        ok = client.health_check()
        assert ok is True, "health_check failed against live CLOB — see logs above"
        print("  health_check: live CLOB OK                                    OK")

        # ── 2: get_active_markets ──────────────────────────────────────────
        ms = client.get_active_markets(limit=5)
        assert isinstance(ms, list), f"expected list, got {type(ms)}"
        assert len(ms) == 5, f"expected exactly 5 active markets, got {len(ms)}"
        required_fields = {"condition_id", "question", "tokens", "active"}
        for m in ms:
            missing = required_fields - m.keys()
            assert not missing, f"market missing fields: {missing}; got {sorted(m.keys())[:10]}"
            assert m["active"] is True
            assert m.get("closed") is False
            # tokens is a list of {token_id, outcome, ...}
            assert isinstance(m["tokens"], list) and len(m["tokens"]) >= 2
        print(f"  get_active_markets(5): {len(ms)} markets, fields present     OK")

        # ── 3: get_order_book for one of those active markets ──────────────
        # Find a token whose book actually exists by trying each market in turn.
        book = None
        chosen = None
        for m in ms:
            for tok in m["tokens"]:
                try:
                    b = client.get_order_book(tok["token_id"])
                    if b["bids"] or b["asks"]:
                        book = b
                        chosen = (m["condition_id"][:10] + "…", tok["outcome"])
                        break
                except Exception:
                    continue
            if book is not None:
                break
        assert book is not None, "no active market in the top 5 had a populated book"
        assert "bids" in book and "asks" in book
        assert isinstance(book["bids"], list) and isinstance(book["asks"], list)
        print(f"  get_order_book: {chosen[0]} {chosen[1]}: "
              f"{len(book['bids'])} bids / {len(book['asks'])} asks            OK")

        # ── 4: get_leaderboard ─────────────────────────────────────────────
        lb = client.get_leaderboard(window="all", limit=10)
        assert isinstance(lb, list), f"expected list, got {type(lb)}"
        assert len(lb) == 10, f"expected 10 leaderboard entries, got {len(lb)}"
        for entry in lb:
            # Probed fields: proxyWallet, amount, pseudonym/name
            assert "proxyWallet" in entry, f"missing proxyWallet: {entry.keys()}"
            assert "amount" in entry, f"missing amount: {entry.keys()}"
        print(f"  get_leaderboard(10): {len(lb)} entries; top profit ${lb[0]['amount']:,.0f}  OK")
    else:
        print("  live checks 1-4: SKIPPED (LIVE_API_TESTS=0)")

    # ── 5: retry — ConnectionError × 2, success on 3rd, exactly 3 attempts ──
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.exceptions.ConnectionError("simulated")
        return "ok"

    result = client._retried(flaky)
    assert result == "ok", f"expected 'ok', got {result!r}"
    assert attempts["n"] == 3, f"expected exactly 3 attempts, got {attempts['n']}"
    print(f"  retry: 2× ConnectionError + 1 success → {attempts['n']} attempts        OK")

    # 4xx must NOT retry — one attempt then raise
    attempts_4xx = {"n": 0}

    def four_oh_four():
        attempts_4xx["n"] += 1
        resp = requests.Response()
        resp.status_code = 404
        raise requests.exceptions.HTTPError("not found", response=resp)

    try:
        client._retried(four_oh_four)
    except requests.exceptions.HTTPError:
        pass
    else:
        raise AssertionError("4xx should propagate without retry")
    assert attempts_4xx["n"] == 1, f"4xx should not retry; got {attempts_4xx['n']} attempts"
    print(f"  retry: 4xx propagates immediately ({attempts_4xx['n']} attempt)         OK")

    # ── 6: circuit breaker — 5 fails → 6th raises CircuitBreakerOpen ───
    cb = _CircuitBreaker(fail_threshold=5, reset_seconds=300.0)
    fail_calls = {"n": 0}

    def boom():
        fail_calls["n"] += 1
        raise requests.exceptions.ConnectionError("boom")

    for i in range(5):
        try:
            cb.call(boom)
        except requests.exceptions.ConnectionError:
            pass
    assert cb.state == "OPEN", f"expected OPEN, got {cb.state}"
    assert fail_calls["n"] == 5, f"expected 5 fn invocations, got {fail_calls['n']}"

    # 6th call: raises CircuitBreakerOpen WITHOUT invoking the function
    before_n = fail_calls["n"]
    try:
        cb.call(boom)
    except CircuitBreakerOpen:
        pass
    else:
        raise AssertionError("6th call should raise CircuitBreakerOpen")
    assert fail_calls["n"] == before_n, (
        f"function must NOT be called when CB is OPEN; "
        f"calls before={before_n} after={fail_calls['n']}"
    )
    print(f"  circuit breaker: 5 fails → OPEN, 6th short-circuits "
          f"({fail_calls['n']} fn invocations)  OK")

    # ── 4b: timestamp seconds → ms conversion at client boundary ──────
    # data-api returns trade.timestamp in UNIX seconds; client.get_trades_for_wallet
    # must convert to ms once at the boundary so no downstream code thinks in seconds.
    from unittest.mock import MagicMock
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [
        {"timestamp": 1731489409, "side": "BUY", "outcome": "Yes",
         "conditionId": "0xABC", "price": 0.5, "size": 100,
         "transactionHash": "0xtest1", "proxyWallet": "0xWHALE"},
        {"timestamp": 1731489500, "side": "SELL", "outcome": "No",
         "conditionId": "0xABC", "price": 0.45, "size": 50,
         "transactionHash": "0xtest2", "proxyWallet": "0xWHALE"},
    ]
    mock_session.get.return_value = mock_resp
    c2 = ClobReadOnlyClient(
        retry_max_attempts=1,
        retry_initial_backoff_seconds=0.001,
        retry_max_backoff_seconds=0.01,
        cb_fail_threshold=10,
        cb_reset_seconds=60.0,
        sleep_fn=lambda _s: None,
        http_session=mock_session,
    )
    rows = c2.get_trades_for_wallet("0xWHALE")
    assert len(rows) == 2
    # 1731489409 s × 1000 = 1731489409000 ms
    assert rows[0]["timestamp"] == 1731489409 * 1000, f"ts not converted: {rows[0]['timestamp']}"
    assert rows[1]["timestamp"] == 1731489500 * 1000
    # `after_ts` filter operates in ms post-conversion
    rows_filtered = c2.get_trades_for_wallet("0xWHALE", after_ts=1731489450 * 1000)
    # mock returns same payload; after re-conversion, filter keeps only ts >= cutoff
    # but each call mutates the same dict refs — refetch with fresh mock to be safe
    mock_resp.json.return_value = [
        {"timestamp": 1731489409, "side": "BUY", "outcome": "Yes",
         "conditionId": "0xABC", "price": 0.5, "size": 100,
         "transactionHash": "0xtest1", "proxyWallet": "0xWHALE"},
        {"timestamp": 1731489500, "side": "SELL", "outcome": "No",
         "conditionId": "0xABC", "price": 0.45, "size": 50,
         "transactionHash": "0xtest2", "proxyWallet": "0xWHALE"},
    ]
    rows_filtered = c2.get_trades_for_wallet("0xWHALE", after_ts=1731489450 * 1000)
    assert len(rows_filtered) == 1, f"expected 1 row after filter, got {len(rows_filtered)}"
    assert rows_filtered[0]["timestamp"] == 1731489500 * 1000
    print("  get_trades_for_wallet: timestamp s→ms conversion + after_ts filter   OK")

    # ── 7: read-only enforcement ───────────────────────────────────────
    blocked = (
        "place_order", "post_order", "post_orders",
        "create_order", "create_market_order", "create_and_post_order",
        "cancel", "cancel_all", "cancel_orders", "cancel_market_orders",
    )
    for name in blocked:
        method = getattr(client, name)
        try:
            method()
        except NotImplementedError as e:
            assert "Phase A read-only" in str(e), f"unexpected message for {name}: {e}"
        else:
            raise AssertionError(f"{name} should raise NotImplementedError")
    print(f"  read-only: {len(blocked)} signing-capable methods all raise         OK")

    print("\nmodule 4 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
