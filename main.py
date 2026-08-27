#!/usr/bin/env python3
"""CLI entrypoint and Phase A daemon orchestrator. Implements PLAN.md §2 main.py
and §15 CLI reference.

Phase A commands: scan, whales, whale-feed, status, pnl, add-whale, backtest, serve
Phase B `run` is intentionally absent — refuses if invoked.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

import bot
from bot import PHASE as BOT_PHASE
from bot.arbitrage_scanner import ArbitrageScanner
from bot.calibration import evaluate_decision_matrix, validate_calibration_readiness
from bot.client import ClobReadOnlyClient
from bot.market_scanner import MarketScanner
from bot.paper_trader import PaperTrader
from bot.resolution_poller import ResolutionPoller
from bot.tracker import Tracker
from bot.whale_perf import WhalePerf
from bot.whale_tracker import PollingWhaleTracker
from utils.helpers import load_config, now_ms
from utils.logger import get_logger, register_secret

_log = get_logger(__name__)
_console = Console()

_SHUTDOWN = threading.Event()
_ORCHESTRATOR: "Orchestrator | None" = None

_MIN_BACKTEST_DAYS = 7


# ───────────────────────────  startup guards  ───────────────────────────


def _assert_phase_a_safe(config: dict[str, Any]) -> None:
    """PLAN.md §0 / §10: refuse live trading in Phase A."""
    if not config.get("dry_run", True):
        _console.print(
            "[bold red]FATAL:[/] dry_run is false but Phase A does not support live trading.\n"
            "Live orders require Phase B (readiness gate + decision matrix). "
            "See PLAN.md §0 and §10.\n"
            "Set dry_run: true in config.yaml."
        )
        sys.exit(1)

    cfg_phase = str(config.get("phase", "A")).upper()
    if cfg_phase != BOT_PHASE:
        _console.print(
            f"[bold red]FATAL:[/] config phase={cfg_phase!r} does not match "
            f"bot.PHASE={BOT_PHASE!r}"
        )
        sys.exit(1)


def _build_client(config: dict[str, Any]) -> ClobReadOnlyClient:
    rc = config.get("reconnect") or {}
    return ClobReadOnlyClient(
        retry_max_attempts=int(rc.get("retry_max_attempts", 5)),
        retry_initial_backoff_seconds=float(rc.get("initial_backoff_seconds", 2)),
        retry_max_backoff_seconds=float(rc.get("max_backoff_seconds", 300)),
        retry_multiplier=float(rc.get("multiplier", 2.0)),
        cb_fail_threshold=int(rc.get("circuit_breaker_consecutive_fails", 5)),
        cb_reset_seconds=float(rc.get("max_backoff_seconds", 300)),
    )


def _paths(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    bot_cfg = config.get("bot") or {}
    data_dir = Path(bot_cfg.get("data_dir", "./data"))
    db_path = Path(bot_cfg.get("db_path", data_dir / "trades.db"))
    audit_path = Path(bot_cfg.get("audit_log_path", data_dir / "audit.log.jsonl"))
    state_path = Path(bot_cfg.get("state_path", data_dir / "state.json"))
    return db_path, audit_path, state_path


def _load_whale_universe(
    client: ClobReadOnlyClient, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Merge manual_seed, whales_manual.json, and leaderboard (best-effort)."""
    wu = config.get("whale_universe") or {}
    by_addr: dict[str, dict[str, Any]] = {}

    def _add(entry: dict[str, Any]) -> None:
        addr = (entry.get("address") or "").strip().lower()
        if not addr:
            return
        by_addr[addr] = {
            "address": addr,
            "label": entry.get("label") or addr[:10],
            "trust": float(entry.get("trust", 0.7)),
        }

    for e in wu.get("manual_seed") or []:
        _add(e)

    manual_path = Path(wu.get("manual_overrides_path", "./data/whales_manual.json"))
    if manual_path.exists():
        try:
            data = json.loads(manual_path.read_text(encoding="utf-8"))
            for e in data if isinstance(data, list) else []:
                _add(e)
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("could not load %s: %s", manual_path, e)

    try:
        top_n = int(wu.get("top_n_from_leaderboard", 50))
        min_profit = float(wu.get("min_all_time_profit_usd", 50_000))
        lb = client.get_leaderboard(window="all", limit=top_n)
        for row in lb:
            profit = float(row.get("amount") or row.get("profit") or 0)
            if profit < min_profit:
                continue
            # API returns proxyWallet; fall back to address/user for schema resilience.
            addr = (row.get("proxyWallet") or row.get("address") or row.get("user") or "").strip().lower()
            if addr and addr not in by_addr:
                by_addr[addr] = {
                    "address": addr,
                    "label": row.get("name") or row.get("pseudonym") or addr[:10],
                    "trust": 0.7,
                }
    except Exception as e:
        _log.warning("leaderboard fetch failed; using manual universe only: %s", e)

    return list(by_addr.values())


def _apply_state_file(whale_tracker: PollingWhaleTracker, state_path: Path) -> None:
    """Resume: max(DB last_seen, state.json last_seen) per wallet."""
    if not state_path.exists():
        return
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("state file unreadable %s: %s", state_path, e)
        return
    for wallet, ts in (data.get("last_seen_ts") or {}).items():
        prev = whale_tracker._last_seen_ts.get(wallet, 0)
        whale_tracker._last_seen_ts[wallet] = max(prev, int(ts))
    _log.info(
        "resumed last_seen_ts for %d wallets from %s",
        len(data.get("last_seen_ts") or {}), state_path,
    )


def _save_state_file(whale_tracker: PollingWhaleTracker, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_seen_ts": whale_tracker._last_seen_ts,
        "saved_at_ms": now_ms(),
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log.info("wrote runtime state → %s", state_path)


# ───────────────────────────  orchestrator  ───────────────────────────


class Orchestrator:
    """Phase A daemon: whale poller + scan/sweep + resolution timers."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        db_path, audit_path, state_path = _paths(config)
        self.state_path = state_path

        self.client = _build_client(config)
        self.tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
        self.whale_perf = WhalePerf(self.tracker, config)

        universe = _load_whale_universe(self.client, config)
        self.whale_tracker = PollingWhaleTracker(
            self.client, self.tracker, config, whale_universe=universe,
        )
        _apply_state_file(self.whale_tracker, state_path)

        self.market_scanner = MarketScanner(self.client, self.tracker, config)
        self.paper_trader = PaperTrader(
            self.tracker,
            self.client,
            config,
            signal_queue=self.whale_tracker.events,
        )
        self.resolution_poller = ResolutionPoller(
            self.client,
            self.tracker,
            config,
            whale_perf=self.whale_perf,
        )

        intervals = config.get("intervals_seconds") or {}
        self._scan_interval = float(intervals.get("market_scan", 600))
        self._res_interval = float(intervals.get("resolution_poll", 1800))

    def run_forever(self) -> None:
        if not self.client.health_check():
            _console.print("[red]CLOB health_check failed — refusing to start daemon[/]")
            sys.exit(1)

        _console.print(
            f"[green]Phase A daemon starting[/] — "
            f"{len(self.whale_tracker._whale_universe)} whales, "
            f"scan every {self._scan_interval:.0f}s, "
            f"resolution every {self._res_interval:.0f}s"
        )

        self.whale_tracker.start()
        self.paper_trader.start()

        last_scan = 0.0
        last_res = 0.0

        while not _SHUTDOWN.is_set():
            now = time.monotonic()
            if now - last_scan >= self._scan_interval:
                try:
                    t0 = time.perf_counter()
                    sr = self.market_scanner.scan_once()
                    self.paper_trader.sweep_pending()
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    _log.info(
                        "market_scan cycle complete — %d markets scanned, "
                        "%d filtered in, %dms",
                        sr.markets_fetched,
                        sr.markets_passed_filter,
                        elapsed,
                    )
                except Exception as e:
                    _log.error("scan/sweep tick failed: %s", e, exc_info=True)
                last_scan = now

            if now - last_res >= self._res_interval:
                try:
                    t0 = time.perf_counter()
                    rr = self.resolution_poller.poll_once()
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    _log.info(
                        "resolution_poll cycle complete — %d markets resolved, "
                        "%d trust updates, %dms",
                        rr.markets_resolved_this_tick,
                        rr.trust_updates_applied,
                        elapsed,
                    )
                except Exception as e:
                    _log.error("resolution tick failed: %s", e, exc_info=True)
                last_res = now

            _SHUTDOWN.wait(1.0)

        self.shutdown()

    def shutdown(self) -> None:
        _console.print("[yellow]Shutting down…[/]")
        self.whale_tracker.stop()
        self.paper_trader.stop()
        _save_state_file(self.whale_tracker, self.state_path)
        self.tracker.close()
        _console.print("[green]Shutdown complete[/]")


def _handle_signal(signum: int, _frame) -> None:
    _log.info("received signal %s — initiating shutdown", signum)
    _SHUTDOWN.set()
    if _ORCHESTRATOR is not None:
        pass  # run_forever loop observes _SHUTDOWN


# ───────────────────────────  CLI commands  ───────────────────────────


def cmd_scan(config: dict[str, Any]) -> None:
    client = _build_client(config)
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    try:
        scanner = MarketScanner(client, tracker, config)
        result = scanner.scan_once()
        _console.print(
            f"Scan complete: fetched={result.markets_fetched} "
            f"passed={result.markets_passed_filter} "
            f"snapshots={result.market_snapshots_written} "
            f"books={result.book_snapshots_written} errors={result.errors}"
        )
    finally:
        tracker.close()


def cmd_whales(config: dict[str, Any]) -> None:
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    threshold = float((config.get("whale_perf") or {}).get("blacklist_threshold", 0.35))
    try:
        rows = tracker.list_whale_trust_rows()
        if not rows:
            _console.print("No whale trust rows yet.")
            return
        table = Table(title="Whale trust scores")
        table.add_column("Wallet")
        table.add_column("Trust", justify="right")
        table.add_column("α", justify="right")
        table.add_column("β", justify="right")
        table.add_column("Resolved", justify="right")
        table.add_column("Flag")
        for r in rows:
            flag = ""
            if r["candidate_blacklist"]:
                flag = "⚠️ below threshold"
            elif r["trust_mean"] < threshold:
                flag = "⚠️ below threshold"
            table.add_row(
                r["whale_address"][:16] + "…",
                f"{r['trust_mean']:.3f}",
                f"{r['alpha']:.1f}",
                f"{r['beta']:.1f}",
                str(r["resolved_count"]),
                flag,
            )
        _console.print(table)
    finally:
        tracker.close()


def cmd_whale_feed(config: dict[str, Any], *, follow: bool, limit: int) -> None:
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    seen: set[int] = set()
    try:
        while True:
            signals = tracker.get_recent_whale_signals(limit=limit)
            for s in reversed(signals):
                if s.id in seen:
                    continue
                seen.add(s.id)
                _console.print(
                    f"[cyan]{s.signal_type:5}[/] {s.whale_address[:10]}… "
                    f"{s.side} ${s.size_usd:.0f} @ {s.price:.3f} "
                    f"lat={s.detection_latency_ms}ms market={s.market_id[:12]}…"
                )
            if not follow:
                break
            time.sleep(5.0)
    except KeyboardInterrupt:
        pass
    finally:
        tracker.close()


def cmd_status(config: dict[str, Any]) -> None:
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    try:
        rep = validate_calibration_readiness(tracker, config)
        _console.print(f"\n[bold]Readiness:[/] {rep.summary}")
        for c in rep.checks:
            mark = "[green]✓[/]" if c.passed else "[red]✗[/]"
            _console.print(
                f"  {mark} {c.name}: {c.current:.0f}/{c.required:.0f}"
                if c.name != "calendar_days"
                else f"  {mark} {c.name}: {c.current:.1f}/{c.required:.0f} days"
            )

        if rep.all_passed:
            verdict = evaluate_decision_matrix(tracker, config)
            _console.print(f"\n[bold]Decision matrix:[/] [cyan]{verdict.label}[/]")
            _console.print(f"  {verdict.summary}")
            _console.print(
                f"  copy {verdict.copy.wins}/{verdict.copy.n} "
                f"({verdict.copy.win_rate:.1%}) vs "
                f"null {verdict.null.wins}/{verdict.null.n} "
                f"({verdict.null.win_rate:.1%}) "
                f"[{verdict.copy_vs_null.method} p={verdict.copy_vs_null.p_value:.3f}]"
            )
        else:
            _console.print(
                "\n[dim]Decision matrix: deferred until readiness gate is all-green[/]"
            )
    finally:
        tracker.close()


def cmd_pnl(config: dict[str, Any], days: int, by_source: bool) -> None:
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    since = now_ms() - days * 86_400_000
    try:
        if by_source:
            rows = tracker.get_pnl_by_strategy(since)
            table = Table(title=f"P&L by stream (last {days}d)")
            table.add_column("strategy_id")
            table.add_column("n", justify="right")
            table.add_column("wins", justify="right")
            table.add_column("win%", justify="right")
            table.add_column("pnl_usd", justify="right")
            for r in rows:
                n = int(r["n"])
                wins = int(r["wins"])
                wr = wins / n if n else 0.0
                table.add_row(
                    r["strategy_id"], str(n), str(wins),
                    f"{wr:.1%}", f"${float(r['pnl_usd']):,.2f}",
                )
            _console.print(table)

        whale_rows = tracker.get_pnl_by_whale(since)
        wt = Table(title=f"P&L by whale — copy_whale (last {days}d)")
        wt.add_column("whale")
        wt.add_column("n", justify="right")
        wt.add_column("wins", justify="right")
        wt.add_column("pnl_usd", justify="right")
        for r in whale_rows:
            label = r["whale_label"] or r["whale_address"][:12]
            wt.add_row(label, str(r["n"]), str(r["wins"]), f"${float(r['pnl_usd']):,.2f}")
        _console.print(wt)
    finally:
        tracker.close()


def cmd_add_whale(
    config: dict[str, Any], address: str, label: str | None, trust: float
) -> None:
    wu = config.get("whale_universe") or {}
    manual_path = Path(wu.get("manual_overrides_path", "./data/whales_manual.json"))
    manual_path.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    if manual_path.exists():
        entries = json.loads(manual_path.read_text(encoding="utf-8"))

    addr = address.strip().lower()
    entry = {"address": addr, "label": label or addr[:10], "trust": trust}
    entries = [e for e in entries if e.get("address") != addr] + [entry]
    manual_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    try:
        wp = WhalePerf(tracker, config)
        a, b, m = wp.seed_manual_trust(addr, trust)
        _console.print(
            f"Added whale {addr} label={entry['label']!r} "
            f"trust={trust:.2f} → Beta({a:.0f},{b:.0f}) mean={m:.3f}"
        )
        _console.print(f"Wrote {manual_path}")
    finally:
        tracker.close()


def cmd_backtest(config: dict[str, Any]) -> None:
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    try:
        days = tracker.resolved_calendar_days()
        if days < _MIN_BACKTEST_DAYS:
            _console.print(
                f"[red]Backtest refused:[/] only {days:.1f} days of resolved data "
                f"(need ≥ {_MIN_BACKTEST_DAYS}). Keep Phase A running."
            )
            sys.exit(1)
        _console.print(f"Backtest window: {days:.1f} calendar days of resolved trades")
        rows = tracker.get_pnl_by_strategy(0)
        table = Table(title="Stream P&L (all time)")
        table.add_column("strategy")
        table.add_column("n", justify="right")
        table.add_column("win%", justify="right")
        table.add_column("pnl", justify="right")
        for r in rows:
            n = int(r["n"])
            wr = int(r["wins"]) / n if n else 0.0
            table.add_row(
                r["strategy_id"], str(n), f"{wr:.1%}", f"${float(r['pnl_usd']):,.2f}"
            )
        _console.print(table)
        if validate_calibration_readiness(tracker, config).all_passed:
            v = evaluate_decision_matrix(tracker, config)
            _console.print(f"\nDecision matrix: [cyan]{v.label}[/] — {v.summary}")
    finally:
        tracker.close()


def cmd_arb_scan(config: dict[str, Any]) -> None:
    """One observation pass of the binary complement arbitrage scanner.

    Read-only against Polymarket. Records evidence; never trades.
    """
    client = _build_client(config)
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    try:
        scanner = ArbitrageScanner(client, tracker, config)
        r = scanner.scan_once()
        _console.print(
            f"\n[bold]Complement arb scan[/] — "
            f"considered={r.markets_considered} eligible={r.markets_eligible} "
            f"observed={r.opportunities_found} "
            f"[cyan]executable={r.executable_opportunities}[/] "
            f"notional=${r.total_executable_notional_usd:,.2f} "
            f"errors={r.errors}"
        )
        if r.executable_opportunities == 0:
            _console.print(
                "[dim]No executable complement arbitrage this pass. Expected: "
                "VWAP over depth is always ≥ best ask, so a market that fails "
                "at the touch cannot pass deeper.[/]"
            )
        _console.print("[dim]Observation only — nothing was traded.[/]")
        for e in r.error_details[:5]:
            _console.print(f"  [red]{e.error_type}[/] {e.market_id[:16]} {e.message[:70]}")
    finally:
        tracker.close()


def cmd_arb_report(config: dict[str, Any], days: int, out: str | None) -> None:
    """Render a self-contained HTML research report."""
    from bot.arb_report import write_report
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    try:
        target = Path(out) if out else Path(db_path).parent / "arb_report.html"
        written = write_report(tracker, config, target, days=days)
        _console.print(f"\nReport written → [cyan]{written}[/]")
        _console.print(f"  open with: [dim]open '{written}'[/]")
    finally:
        tracker.close()


def cmd_arb_status(config: dict[str, Any], days: int) -> None:
    """Summarise recorded complement-arb observations."""
    db_path, audit_path, _ = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)
    since = now_ms() - days * 86_400_000
    try:
        rows = tracker.get_arbitrage_observations(since)
        if not rows:
            _console.print(f"No arbitrage observations in the last {days}d. "
                           "Run [cyan]arb-scan[/] first.")
            return

        execs = [r for r in rows if r["executable"]]
        edges = sorted(r["net_edge_bps"] for r in rows)
        median = edges[len(edges) // 2]
        notional = sum(r["gross_cost_usd"] for r in execs)

        _console.print(f"\n[bold]Complement arb observations — last {days}d[/]")
        _console.print(f"  observations        {len(rows)}")
        _console.print(f"  executable          {len(execs)}"
                       f"  ({len(execs) / len(rows):.1%})")
        _console.print(f"  median net edge     {median:,.0f} bps")
        _console.print(f"  max net edge        {edges[-1]:,.0f} bps")
        _console.print(f"  executable notional ${notional:,.2f}")

        reasons: dict[str, int] = {}
        for r in rows:
            if not r["executable"]:
                reasons[r["reject_reason"] or "unknown"] = \
                    reasons.get(r["reject_reason"] or "unknown", 0) + 1
        if reasons:
            _console.print("\n[bold]Why candidates were rejected[/]")
            for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
                _console.print(f"  {k:26} {v}")

        table = Table(title="Top 10 candidates by net edge")
        table.add_column("market")
        table.add_column("observed")
        table.add_column("shares", justify="right")
        table.add_column("net p&l", justify="right")
        table.add_column("edge bps", justify="right")
        table.add_column("status")
        for r in tracker.get_top_arbitrage(since, limit=10):
            ts = datetime.datetime.fromtimestamp(
                r["observed_at_ts"] / 1000).strftime("%m-%d %H:%M")
            status = "[green]executable[/]" if r["executable"] \
                else f"[dim]{r['reject_reason']}[/]"
            table.add_row(
                (r["question"] or r["market_id"])[:34],
                ts, f"{r['equal_shares']:.1f}",
                f"${r['net_locked_pnl_usd']:+.4f}",
                f"{r['net_edge_bps']:+,.0f}", status,
            )
        _console.print(table)
        _console.print(
            "[yellow]A positive observation is evidence, not permission to "
            "trade.[/] Fee and settlement assumptions are unvalidated — see README."
        )
    finally:
        tracker.close()


def cmd_serve(config: dict[str, Any]) -> None:
    global _ORCHESTRATOR
    _ORCHESTRATOR = Orchestrator(config)
    _ORCHESTRATOR.run_forever()


def cmd_run(config: dict[str, Any]) -> None:
    """Phase B live-trading orchestrator.

    Runs alongside the full Phase A stack (paper streams, resolution poller)
    so both live and paper trades are recorded in parallel for ongoing
    calibration. LiveTrader reads from the same whale_tracker.events queue.

    Guards:
      1. Readiness gate must be all-green.
      2. Decision matrix must be BUILD_B_RELAXED or BUILD_B_FULL.
      3. PRIVATE_KEY must be set (unless dry_run: true — then paper mode).
    """
    from bot.executor import ClobSigningClient, LiveTrader
    from bot.calibration import evaluate_decision_matrix, validate_calibration_readiness

    db_path, audit_path, state_path = _paths(config)
    tracker = Tracker(db_path=db_path, audit_log_path=audit_path)

    rep = validate_calibration_readiness(tracker, config)
    if not rep.all_passed:
        _console.print(f"[red]refused:[/] readiness gate not met — {rep.summary}")
        tracker.close()
        sys.exit(1)

    verdict = evaluate_decision_matrix(tracker, config)
    if verdict.label not in ("BUILD_B_RELAXED", "BUILD_B_FULL"):
        _console.print(
            f"[red]refused:[/] decision matrix verdict={verdict.label!r}. "
            "Need BUILD_B_RELAXED or BUILD_B_FULL to proceed."
        )
        tracker.close()
        sys.exit(1)

    dry_run = bool(config.get("dry_run", True))
    private_key = os.environ.get("PRIVATE_KEY", "").strip()

    if not dry_run and not private_key:
        _console.print(
            "[red]refused:[/] dry_run=false but PRIVATE_KEY is not set. "
            "Set PRIVATE_KEY in .env or the environment, or set dry_run: true "
            "in config.yaml for a paper-only run of Phase B logic."
        )
        tracker.close()
        sys.exit(1)

    if dry_run:
        client = _build_client(config)
        _console.print("[yellow]Phase B dry-run[/] — live_orders will be written with status=dry_run")
    else:
        rc = config.get("reconnect") or {}
        client = ClobSigningClient(
            retry_max_attempts=int(rc.get("retry_max_attempts", 5)),
            retry_initial_backoff_seconds=float(rc.get("initial_backoff_seconds", 2)),
            retry_max_backoff_seconds=float(rc.get("max_backoff_seconds", 300)),
            retry_multiplier=float(rc.get("multiplier", 2.0)),
            cb_fail_threshold=int(rc.get("circuit_breaker_consecutive_fails", 5)),
            cb_reset_seconds=float(rc.get("max_backoff_seconds", 300)),
        )

    tracker_b = tracker

    universe = _load_whale_universe(client, config)
    whale_tracker = PollingWhaleTracker(client, tracker_b, config, whale_universe=universe)
    _apply_state_file(whale_tracker, state_path)

    market_scanner = MarketScanner(client, tracker_b, config)
    paper_trader = PaperTrader(tracker_b, client, config, signal_queue=whale_tracker.events)
    live_trader = LiveTrader(tracker_b, client, config, signal_queue=whale_tracker.events)
    resolution_poller = ResolutionPoller(
        client, tracker_b, config, whale_perf=WhalePerf(tracker_b, config)
    )

    intervals = config.get("intervals_seconds") or {}
    scan_interval = float(intervals.get("market_scan", 600))
    res_interval = float(intervals.get("resolution_poll", 1800))

    if not client.health_check():
        _console.print("[red]CLOB health_check failed — refusing to start[/]")
        tracker_b.close()
        sys.exit(1)

    _console.print(
        f"[green]Phase B starting[/] (verdict={verdict.label}, dry_run={dry_run}) — "
        f"{len(universe)} whales"
    )

    whale_tracker.start()
    paper_trader.start()
    live_trader.start()

    last_scan = 0.0
    last_res = 0.0

    try:
        while not _SHUTDOWN.is_set():
            now = time.monotonic()
            if now - last_scan >= scan_interval:
                try:
                    market_scanner.scan_once()
                    paper_trader.sweep_pending()
                except Exception as e:
                    _log.error("Phase B scan/sweep tick failed: %s", e, exc_info=True)
                last_scan = now
            if now - last_res >= res_interval:
                try:
                    resolution_poller.poll_once()
                except Exception as e:
                    _log.error("Phase B resolution tick failed: %s", e, exc_info=True)
                last_res = now
            _SHUTDOWN.wait(1.0)
    finally:
        whale_tracker.stop()
        paper_trader.stop()
        live_trader.stop()
        _save_state_file(whale_tracker, state_path)
        tracker_b.close()
        _console.print("[green]Phase B shutdown complete[/]")


# ───────────────────────────  argparse  ───────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Polymarket betting bot — Phase A observation mode",
    )
    p.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="One-shot market scan + book snapshots")
    sub.add_parser("whales", help="Whale trust scorecard")
    wf = sub.add_parser("whale-feed", help="Tail whale signals")
    wf.add_argument("--follow", action="store_true", help="Poll continuously")
    wf.add_argument("--limit", type=int, default=20)

    sub.add_parser("status", help="Readiness + decision matrix")
    pnl = sub.add_parser("pnl", help="P&L summary")
    pnl.add_argument("--days", type=int, default=7)
    pnl.add_argument("--by-source", action="store_true", default=True)

    aw = sub.add_parser("add-whale", help="Add manual whale override")
    aw.add_argument("address")
    aw.add_argument("--label", default=None)
    aw.add_argument("--trust", type=float, default=0.9)

    sub.add_parser("backtest", help="Replay paper-stream stats (≥7 days required)")

    sub.add_parser("arb-scan", help="One binary-complement arbitrage observation pass (read-only)")
    arb = sub.add_parser("arb-status", help="Summarise recorded arbitrage observations")
    arb.add_argument("--days", type=int, default=7)
    arep = sub.add_parser("arb-report", help="Render the HTML research report")
    arep.add_argument("--days", type=int, default=7)
    arep.add_argument("--out", default=None)
    sub.add_parser("serve", help="Run Phase A daemon (all background loops)")

    run = sub.add_parser("run", help="[Phase B] Live trading")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    for var in ("CLOB_API_KEY", "CLOB_API_SECRET", "CLOB_API_PASSPHRASE", "PRIVATE_KEY"):
        v = os.environ.get(var)
        if v:
            register_secret(v)

    args = _build_parser().parse_args(argv)

    config = load_config(args.config)

    if args.command == "run":
        # Phase B: skip the Phase A dry_run/phase guard — `run` has its own guards
        # inside cmd_run (readiness gate + decision matrix + PRIVATE_KEY check).
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        log_level = (config.get("bot") or {}).get("log_level", "INFO")
        os.environ.setdefault("LOG_LEVEL", log_level)
        _log.info("polymarket-bot %s phase=B command=run", bot.__version__)
        cmd_run(config)
        return 0

    _assert_phase_a_safe(config)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log_level = (config.get("bot") or {}).get("log_level", "INFO")
    # Env var takes precedence; setdefault only writes if LOG_LEVEL is not already set.
    os.environ.setdefault("LOG_LEVEL", log_level)
    _log.info("polymarket-bot %s phase=%s command=%s", bot.__version__, BOT_PHASE, args.command)

    handlers = {
        "scan": lambda: cmd_scan(config),
        "whales": lambda: cmd_whales(config),
        "whale-feed": lambda: cmd_whale_feed(
            config, follow=args.follow, limit=args.limit
        ),
        "status": lambda: cmd_status(config),
        "pnl": lambda: cmd_pnl(config, days=args.days, by_source=args.by_source),
        "add-whale": lambda: cmd_add_whale(
            config, args.address, args.label, args.trust
        ),
        "backtest": lambda: cmd_backtest(config),
        "arb-scan": lambda: cmd_arb_scan(config),
        "arb-status": lambda: cmd_arb_status(config, days=args.days),
        "arb-report": lambda: cmd_arb_report(config, days=args.days,
                                             out=getattr(args, "out", None)),
        "serve": lambda: cmd_serve(config),
        "run": lambda: cmd_run(config),
    }
    handlers[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
