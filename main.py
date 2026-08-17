#!/usr/bin/env python3
"""Friday — Polymarket liquidity-rewards market maker.

Commands:
  scan     rank markets by reward efficiency and show the selection
  quote    run ONE quote cycle and show what would rest (no orders in dry_run)
  serve    run the quoting loop continuously
  report   net-PnL post-mortem over a window
  dashboard  local web UI (localhost only) reading the live tracker

Dry-run scope, stated plainly: the paper phase measures quoting behaviour,
reward eligibility, and whether the vol-floor-vs-reward-band rule leaves any
market quotable. It does NOT produce fill P&L — simulating passive fills would
reintroduce exactly the optimistic-fill error that invalidated the predecessor
project. Fills are recorded only when live.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from friday.adapter import PolymarketAdapter
from friday.client import ClobReadOnlyClient
from friday.events import BUS, TRACKER
from friday.guard import Guard
from friday.inventory import PortfolioRisk
from friday.lock import AlreadyRunning, InstanceLock
from friday.logger import get_logger, register_secret
from friday.marketmaker import MarketMaker, Quote
from friday.postmortem import PostMortem
from friday.scanner import Scanner
from friday.tracker import Tracker

_log = get_logger("friday")
_SHUTDOWN = threading.Event()


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_client(config: dict[str, Any]):
    rc = config.get("reconnect") or {}
    kw = dict(
        retry_max_attempts=int(rc.get("retry_max_attempts", 5)),
        retry_initial_backoff_seconds=float(rc.get("initial_backoff_seconds", 2)),
        retry_max_backoff_seconds=float(rc.get("max_backoff_seconds", 300)),
        retry_multiplier=float(rc.get("multiplier", 2.0)),
        cb_fail_threshold=int(rc.get("circuit_breaker_consecutive_fails", 5)),
        cb_reset_seconds=float(rc.get("max_backoff_seconds", 300)),
    )
    if config.get("dry_run", True):
        return ClobReadOnlyClient(**kw)
    from friday.adapter import ClobSigningClient
    return ClobSigningClient(**kw)


class Engine:
    """Holds one MarketMaker and one adapter leg-pair per selected market."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.dry_run = bool(config.get("dry_run", True))
        bot = config.get("bot") or {}
        db = bot.get("db_path", "./data/friday.db")
        self.lock = InstanceLock(Path(db).parent / "friday.lock").acquire()
        self.tracker = Tracker(db, bot.get("audit_log_path", "./data/audit.jsonl"))
        self.client = build_client(config)
        self.scanner = Scanner(self.client, config)
        self.guard = Guard(config)
        self.portfolio = PortfolioRisk(config)
        self.adapter = PolymarketAdapter(self.client, dry_run=self.dry_run)
        self.makers: dict[str, MarketMaker] = {}
        self.positions: dict[str, float] = {}
        self.realised_pnl = 0.0
        q = config.get("quoting") or {}
        self.our_shares = float(q.get("size_shares", 200)) * 2   # both legs

    def _maker(self, cand) -> MarketMaker:
        mm = self.makers.get(cand.condition_id)
        if mm is None or abs(mm.band - cand.max_spread) > 1e-9:
            mm = MarketMaker(self.config, reward_band=cand.max_spread,
                             min_size_shares=cand.min_size_shares or 200,
                             condition_id=cand.condition_id)
            self.makers[cand.condition_id] = mm
        return mm

    selected_ids: list[str] = []

    def cycle(self, selected) -> list[dict[str, Any]]:
        """One quote cycle across the selected markets."""
        out = []
        deployed = 0.0                       # USD committed so far THIS cycle
        active: set[str] = set()
        for cand in selected:
            mm = self._maker(cand)
            try:
                bids, asks = self.adapter.get_book(cand.yes_token_id)
            except Exception as e:
                _log.warning("book fetch failed %s: %s", cand.condition_id[:12], e)
                continue

            pos = self.positions.get(cand.condition_id, 0.0)
            bid_tag = f"{cand.condition_id}:bid"
            ask_tag = f"{cand.condition_id}:ask"
            own_bid = own_ask = None
            rb, ra = self.adapter.resting(bid_tag), self.adapter.resting(ask_tag)
            if rb:
                own_bid = (rb.price, rb.size)
            if ra:
                own_ask = (1.0 - ra.price, ra.size)   # NO-leg back into YES terms

            q = mm.quote(bids, asks, pos, self.realised_pnl, own_bid, own_ask)

            # Portfolio gate: per-market limits cannot cap total exposure, so
            # the whole-book check runs here, before capital is committed.
            if q.quoting:
                want = PortfolioRisk.quote_notional(q.bid_px, q.bid_sz,
                                                    q.ask_px, q.ask_sz)
                pv = self.portfolio.gate(cand.condition_id, self.positions,
                                         deployed, want, active)
                if not pv.allowed:
                    _log.info("portfolio gate blocked %s: %s",
                              cand.condition_id[:12], pv.reason)
                    BUS.node_state(f"mm:{cand.condition_id}", quoting=False,
                                   reason="portfolio_cap")
                    q = Quote(reason="portfolio_cap")
                else:
                    deployed += want
                    active.add(cand.condition_id)

            if q.quoting:
                self.adapter.sync_quote(bid_tag, cand.yes_token_id, q.bid_px, q.bid_sz)
                if cand.no_token_id:
                    self.adapter.sync_quote(ask_tag, cand.no_token_id,
                                            PolymarketAdapter.ask_as_no_buy(q.ask_px),
                                            q.ask_sz)
            else:
                self.adapter.sync_quote(bid_tag, cand.yes_token_id, 0, 0)
                self.adapter.sync_quote(ask_tag, cand.no_token_id or "", 0, 0)

            half = (q.ask_px - q.bid_px) / 2 if q.quoting else None
            row = {
                "condition_id": cand.condition_id, "question": cand.question,
                "quoting": 1 if q.quoting else 0, "reason": q.reason or None,
                "bid_px": q.bid_px or None, "bid_sz": q.bid_sz or None,
                "ask_px": q.ask_px or None, "ask_sz": q.ask_sz or None,
                "mid": mm.mark.last, "vol_est": mm.mark._vol(), "half_spread": half,
                "reward_band": cand.max_spread,
                "pool_usd_per_day": cand.pool_usd_per_day,
                "depth_inside_band": cand.depth_inside_band,
                "our_shares": self.our_shares if q.quoting else 0.0,
                "implied_share": cand.implied_share(self.our_shares) if q.quoting else 0.0,
                "expected_usd_per_day": (cand.expected_daily_usd(self.our_shares)
                                         if q.quoting else 0.0),
                "position": pos, "markout": mm.markout.average(),
                "tox_bid": mm.tox_bid, "tox_ask": mm.tox_ask,
            }
            self.tracker.record_quote_cycle(row)
            BUS.emit(f"mm:{cand.condition_id}", TRACKER, "record",
                     quoting=bool(q.quoting), expected=row["expected_usd_per_day"])
            out.append(row)
        self.selected_ids = [c.condition_id for c in selected]
        self.portfolio_state = self.portfolio.snapshot(self.positions, deployed, active)
        BUS.node_state("guard", **{f"pf_{k}": v for k, v in
                                   self.portfolio_state.items()})
        return out

    def shutdown(self) -> None:
        n = self.adapter.cancel_all()
        _log.info("cancelled %d resting orders", n)
        self.tracker.close()
        self.lock.release()


# ─────────────────────────────  commands  ─────────────────────────────

def cmd_scan(config):
    client = ClobReadOnlyClient()
    sc = Scanner(client, config)
    ranked = sc.scan()
    our = float((config.get("quoting") or {}).get("size_shares", 200)) * 2
    print(f"\n{'score':>7} {'pool/d':>7} {'depth':>9} {'band':>6} {'ourshr':>7} {'exp$/d':>7}  market")
    for c in ranked[:15]:
        mark = "" if sc.viable(c, our) else "  [rejected: too thin]"
        print(f"{c.score:>7.3f} {c.pool_usd_per_day:>7.0f} {c.depth_inside_band:>9.0f} "
              f"{c.max_spread*100:>5.1f}c {c.our_book_share(our)*100:>6.0f}% "
              f"{c.expected_daily_usd(our):>7.2f}  {c.question[:34]}{mark}")
    sel = sc.select(ranked, our)
    total = sum(c.expected_daily_usd(our) for c in sel)
    print(f"\nSELECTED {len(sel)}:")
    for c in sel:
        print(f"  {c.question[:56]}")
        print(f"    pool=${c.pool_usd_per_day:.0f}/d depth={c.depth_inside_band:,.0f}sh "
              f"our_share={c.implied_share(our)*100:.0f}% -> ${c.expected_daily_usd(our):.2f}/day")
    print(f"\n  expected GROSS reward: ${total:.2f}/day")
    print("  (gross — adverse selection is not in this number)")


def cmd_quote(config):
    eng = Engine(config)
    try:
        ranked = eng.scanner.scan()
        sel = eng.scanner.select(ranked, eng.our_shares)
        rows = eng.cycle(sel)
        print()
        for r in rows:
            if r["quoting"]:
                print(f"  QUOTE {r['question'][:44]}")
                print(f"    bid {r['bid_sz']:.0f} @ {r['bid_px']:.4f}   "
                      f"ask {r['ask_sz']:.0f} @ {r['ask_px']:.4f}   "
                      f"half={r['half_spread']:.4f} band={r['reward_band']:.3f}")
                print(f"    expected ${r['expected_usd_per_day']:.2f}/day")
            else:
                print(f"  STAND DOWN {r['question'][:40]} — {r['reason']}")
        print(f"\n  adapter: {eng.adapter.stats()}")
    finally:
        eng.shutdown()


def cmd_serve(config):
    eng = Engine(config)
    interval = float((config.get("scanner") or {}).get("rerank_interval_seconds", 900))
    poll = 30.0
    _log.info("Friday starting — dry_run=%s, top_n=%s",
              eng.dry_run, (config.get("scanner") or {}).get("top_n"))
    ranked = eng.scanner.scan()
    selected = eng.scanner.select(ranked, eng.our_shares)
    last_rank = time.monotonic()
    try:
        while not _SHUTDOWN.is_set():
            v = eng.guard.check(eng.tracker, eng.realised_pnl)
            if not v.allowed:
                _log.error("halted: %s — cancelling and stopping", v.reason)
                break
            if time.monotonic() - last_rank >= interval:
                incumbents = {c.condition_id for c in selected}
                ranked = eng.scanner.scan()
                selected = eng.scanner.select(ranked, eng.our_shares, incumbents)
                last_rank = time.monotonic()
            try:
                eng.cycle(selected)
            except Exception as e:
                _log.error("cycle failed: %s", e, exc_info=True)
            _SHUTDOWN.wait(poll)
    finally:
        eng.shutdown()


def cmd_rewards(config, imp, days: float):
    from friday.rewards import Rewards
    bot = config.get("bot") or {}
    tr = Tracker(bot.get("db_path", "./data/friday.db"))
    try:
        r = Rewards(tr)
        if imp:
            cid, usd, day = imp
            r.record_credit(cid, float(usd), day)
            print(f"recorded ${float(usd):.2f} for {cid[:16]}… on {day}")
        print()
        print(r.reconcile(window_days=days).render())
    finally:
        tr.close()


def cmd_dashboard(config, port: int, with_loop: bool):
    """Serve the UI. Optionally run the quoting loop in the same process so a
    single command gives a live view."""
    from friday import dashboard
    bot = config.get("bot") or {}
    tr = Tracker(bot.get("db_path", "./data/friday.db"))
    if with_loop:
        threading.Thread(target=cmd_serve, args=(config,), daemon=True).start()
        _log.info("quoting loop started alongside dashboard")
    try:
        dashboard.serve(tr, config, port=port)
    except KeyboardInterrupt:
        pass
    finally:
        _SHUTDOWN.set()
        tr.close()


def cmd_report(config, days: float):
    bot = config.get("bot") or {}
    tr = Tracker(bot.get("db_path", "./data/friday.db"))
    try:
        print()
        print(PostMortem(tr, config).report(window_days=days).render())
    finally:
        tr.close()


def _sig(_s, _f):
    _log.info("shutdown signal")
    _SHUTDOWN.set()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Friday — Polymarket liquidity-rewards MM")
    p.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="rank markets by reward efficiency")
    sub.add_parser("quote", help="one quote cycle")
    sub.add_parser("serve", help="continuous quoting loop")
    rp = sub.add_parser("report", help="net-PnL post-mortem")
    rp.add_argument("--days", type=float, default=5.0)
    rw = sub.add_parser("rewards", help="expected vs realised reward reconciliation")
    rw.add_argument("--import", dest="imp", nargs=3,
                    metavar=("CONDITION_ID", "USD", "YYYY-MM-DD"),
                    help="record a realised reward credit")
    rw.add_argument("--days", type=float, default=7.0)
    dp = sub.add_parser("dashboard", help="local web UI (localhost only)")
    dp.add_argument("--port", type=int, default=8420)
    dp.add_argument("--with-loop", action="store_true",
                    help="also run the quoting loop in this process")
    args = p.parse_args(argv)

    for var in ("PRIVATE_KEY", "CLOB_API_KEY", "CLOB_API_SECRET", "CLOB_API_PASSPHRASE"):
        v = os.environ.get(var)
        if v:
            register_secret(v)

    config = load_config(args.config)
    os.environ.setdefault("LOG_LEVEL", (config.get("bot") or {}).get("log_level", "INFO"))

    if not config.get("dry_run", True) and not os.environ.get("PRIVATE_KEY"):
        print("refused: dry_run is false but PRIVATE_KEY is not set.", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        return _dispatch(args, config)
    except AlreadyRunning as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1


def _dispatch(args, config) -> int:
    {"scan": lambda: cmd_scan(config),
     "quote": lambda: cmd_quote(config),
     "serve": lambda: cmd_serve(config),
     "report": lambda: cmd_report(config, args.days),
     "rewards": lambda: cmd_rewards(config, getattr(args, "imp", None), args.days),
     "dashboard": lambda: cmd_dashboard(config, args.port,
                                        getattr(args, "with_loop", False)),
     }[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
