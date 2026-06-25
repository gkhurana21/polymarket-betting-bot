"""Smoke test for Module 11 (main.py).

10 verifications (offline where possible):
   1. dry_run:false + phase A → SystemExit
   2. phase mismatch config vs bot.PHASE → SystemExit
   3. _load_whale_universe merges manual_seed
   4. state.json save/load merges last_seen_ts (max with DB)
   5. cmd_status renders readiness on empty DB
   6. cmd_backtest refuses when < 7 days
   7. cmd_add_whale writes whales_manual.json + seeds trust
   8. Orchestrator constructs without starting loops
   9. `run` command returns 1
  10. argparse dispatches known subcommands

Run:
    .venv/bin/python tests/smoke_module11.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from bot.models import WhaleSignal  # noqa: E402
from bot.tracker import Tracker  # noqa: E402
from bot.whale_tracker import PollingWhaleTracker  # noqa: E402
from utils.helpers import load_config  # noqa: E402


def _cfg(tmp: Path) -> dict:
    base = load_config()
    base["dry_run"] = True
    base["phase"] = "A"
    base["bot"] = dict(base.get("bot") or {})
    base["bot"]["data_dir"] = str(tmp)
    base["bot"]["db_path"] = str(tmp / "t.db")
    base["bot"]["audit_log_path"] = str(tmp / "a.jsonl")
    base["bot"]["state_path"] = str(tmp / "state.json")
    base["whale_universe"] = {
        "manual_seed": [{"address": "0xmanual01", "label": "M1", "trust": 0.8}],
        "manual_overrides_path": str(tmp / "whales_manual.json"),
        "top_n_from_leaderboard": 0,
    }
    return base


def main_smoke() -> int:
    # 1: dry_run false
    bad = load_config()
    bad = dict(bad)
    bad["dry_run"] = False
    bad["phase"] = "A"
    try:
        main._assert_phase_a_safe(bad)
        raise AssertionError("expected SystemExit for dry_run:false")
    except SystemExit:
        pass
    print("  dry_run:false refused   OK")

    # 2: phase mismatch
    bad2 = load_config()
    bad2 = dict(bad2)
    bad2["dry_run"] = True
    bad2["phase"] = "B"
    try:
        main._assert_phase_a_safe(bad2)
        raise AssertionError("expected SystemExit for phase mismatch")
    except SystemExit:
        pass
    print("  phase mismatch refused   OK")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = _cfg(tmp)

        # 3: whale universe manual seed
        client = mock.MagicMock()
        client._sdk.get_address.return_value = None
        client.get_leaderboard.side_effect = RuntimeError("offline")
        uni = main._load_whale_universe(client, cfg)
        assert any(w["address"] == "0xmanual01" for w in uni)
        print("  load_whale_universe manual_seed   OK")

        # 4: state save/load
        db_path = tmp / "t.db"
        tracker = Tracker(db_path=db_path, audit_log_path=tmp / "a.jsonl")
        sid = tracker.record_whale_signal(WhaleSignal(
            whale_address="0xstate01",
            market_id="m1",
            side="YES",
            signal_type="open",
            price=0.5,
            size_usd=10.0,
            whale_fill_ts=5000,
            detected_at_ts=6000,
            detection_latency_ms=1000,
            raw_json="{}",
            transaction_hash="tx-state",
        ))
        assert sid is not None
        wt = PollingWhaleTracker(
            client, tracker, cfg,
            whale_universe=[{"address": "0xstate01", "label": "S"}],
        )
        assert wt._last_seen_ts["0xstate01"] == 5000
        main._save_state_file(wt, tmp / "state.json")
        wt._last_seen_ts["0xstate01"] = 1000
        main._apply_state_file(wt, tmp / "state.json")
        assert wt._last_seen_ts["0xstate01"] == 5000
        tracker.close()
        print("  state.json max(DB, file) last_seen   OK")

        # 5: status on empty DB
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.cmd_status(cfg)
        out = buf.getvalue()
        assert "0/50" in out or "Readiness" in out
        print("  cmd_status readiness output   OK")

        # 6: backtest refuse
        try:
            main.cmd_backtest(cfg)
            raise AssertionError("backtest should refuse")
        except SystemExit:
            pass
        print("  backtest <7 days refused   OK")

        # 7: add-whale
        main.cmd_add_whale(cfg, "0xadd0001", "TestWhale", 0.9)
        manual = json.loads((tmp / "whales_manual.json").read_text())
        assert any(e["address"] == "0xadd0001" for e in manual)
        t2 = Tracker(db_path=db_path, audit_log_path=tmp / "a.jsonl")
        row = t2.get_whale_trust_row("0xadd0001")
        assert row is not None and abs(row["trust_mean"] - 0.9) < 1e-6
        t2.close()
        print("  add-whale json + Beta seed   OK")

        # 8: orchestrator init (mock health)
        with mock.patch.object(main.ClobReadOnlyClient, "health_check", return_value=True):
            with mock.patch.object(main, "_load_whale_universe", return_value=[]):
                orch = main.Orchestrator(cfg)
                assert orch.whale_perf is not None
                assert orch.resolution_poller._whale_perf is not None
                orch.tracker.close()
        print("  Orchestrator wiring   OK")

    # 9: run command — Phase B gates (readiness not met on empty DB → sys.exit(1))
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("dry_run: true\nphase: A\nbot:\n  data_dir: /tmp/_smoke11_run\n")
        cfg_path = f.name
    try:
        try:
            rc = main.main(["--config", cfg_path, "run"])
            assert rc == 1, f"expected 1, got {rc}"
        except SystemExit as exc:
            assert exc.code == 1, f"expected SystemExit(1), got SystemExit({exc.code})"
    finally:
        Path(cfg_path).unlink(missing_ok=True)
    print("  run command gates readiness (exits 1 on empty DB)   OK")

    # 10: parser knows serve
    p = main._build_parser()
    args = p.parse_args(["serve"])
    assert args.command == "serve"
    print("  argparse subcommands   OK")

    print("\nmodule 11 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_smoke())
