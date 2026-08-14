"""Local dashboard. Pure stdlib — no new dependencies, no external assets.

Serves a single self-contained page plus a JSON state endpoint read live from
the SQLite tracker. Binds to localhost only: this exposes position and PnL
data and has no authentication, so it must not be reachable off the machine.

Display discipline mirrors postmortem.py — NET is the headline, gross reward
income is a labelled component, and expected-but-unrealised income is marked
as a projection. A dashboard that flatters you is worse than no dashboard.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from friday.events import BUS
from friday.logger import get_logger
from friday.postmortem import PostMortem
from friday.topology import build_graph
from friday.tracker import Tracker

_log = get_logger(__name__)

_STATIC = Path(__file__).parent / "static" / "dashboard.html"
_GRAPH = Path(__file__).parent / "static" / "graph.html"
_MS_PER_DAY = 86_400_000


# A market counts as CURRENTLY quoted only if its last cycle is this fresh.
# Without this the table accumulates every market ever quoted in the P&L
# window and the totals over-report badly — an early build showed 10 markets
# and $1,916 deployed when only 2 were live. Over-reporting is the exact
# failure mode this project exists to avoid, so the cutoff is deliberate.
_LIVE_WINDOW_S = 600


def build_state(tracker: Tracker, config: dict[str, Any], window_days: float = 5.0) -> dict:
    now = int(time.time() * 1000)
    since = now - int(window_days * _MS_PER_DAY)
    rep = PostMortem(tracker, config).report(window_days=window_days)

    markets = []
    for r in tracker.latest_cycle_per_market(now - _LIVE_WINDOW_S * 1000):
        age = (now - r["ts"]) / 1000.0
        markets.append({
            "condition_id": r["condition_id"],
            "question": r["question"] or r["condition_id"][:20],
            "quoting": bool(r["quoting"]),
            "reason": r["reason"],
            "bid_px": r["bid_px"], "bid_sz": r["bid_sz"],
            "ask_px": r["ask_px"], "ask_sz": r["ask_sz"],
            "mid": r["mid"], "half_spread": r["half_spread"],
            "reward_band": r["reward_band"],
            "pool": r["pool_usd_per_day"],
            "depth": r["depth_inside_band"],
            "our_shares": r["our_shares"],
            "implied_share": r["implied_share"],
            "expected": r["expected_usd_per_day"],
            "position": r["position"],
            "markout": r["markout"],
            "tox_bid": r["tox_bid"], "tox_ask": r["tox_ask"],
            "age_s": age,
            "stale": age > 180,
        })

    # Totals reflect only what is live right now, never historical residue.
    quoting = [m for m in markets if m["quoting"] and not m["stale"]]
    return {
        "ts": now,
        "dry_run": bool(config.get("dry_run", True)),
        "bankroll": config.get("bankroll_usd"),
        "window_days": window_days,
        "net": {
            "net_usd": rep.net_pnl_usd,
            "reward_realised": rep.reward_realised_usd,
            "reward_expected": rep.reward_expected_usd,
            "reward_is_projection": rep.reward_realised_usd == 0 and rep.reward_expected_usd > 0,
            "adverse_selection": rep.adverse_selection_usd,
            "inventory_mtm": rep.inventory_mtm_usd,
            "n_fills": rep.n_fills,
            "n_cycles": rep.n_cycles,
            "uptime_pct": rep.uptime_pct,
            "avg_markout": rep.avg_markout,
            "decline_reasons": rep.decline_reasons,
        },
        "markets": markets,
        "live_window_s": _LIVE_WINDOW_S,
        "totals": {
            "expected_per_day": sum(m["expected"] or 0 for m in quoting),
            "n_quoting": len(quoting),
            "n_markets": len(markets),
            "capital_deployed": sum(
                (m["bid_sz"] or 0) * (m["bid_px"] or 0)
                + (m["ask_sz"] or 0) * (1 - (m["ask_px"] or 0))
                for m in quoting),
        },
        "fills": [
            {"ts": f["ts"], "condition_id": f["condition_id"],
             "signed_size": f["signed_size"], "price": f["price"],
             "markout": f["markout_at_lag"]}
            for f in tracker.recent_fills(15)
        ],
    }


def make_handler(tracker: Tracker, config: dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html", "/graph"):
                f = _GRAPH if path == "/graph" else _STATIC
                try:
                    self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, b"page missing", "text/plain")
            elif path == "/api/graph":
                try:
                    since = int(self.path.split("since=")[1].split("&")[0]) \
                        if "since=" in self.path else 0
                except (ValueError, IndexError):
                    since = 0
                try:
                    state = build_state(tracker, config)
                    # The graph is a picture of NOW. Markets rotate on re-rank,
                    # so the 10-minute table window would draw stale makers
                    # alongside live ones and imply more concurrency than exists.
                    live = [m for m in state["markets"] if m["age_s"] <= 150]
                    seq, evs = BUS.since(since)
                    body = {"graph": build_graph(live),
                            "events": evs, "seq": seq,
                            "net": state["net"], "totals": state["totals"],
                            "dry_run": state["dry_run"], "ts": state["ts"]}
                    self._send(200, json.dumps(body).encode(), "application/json")
                except Exception as e:
                    _log.error("graph build failed: %s", e, exc_info=True)
                    self._send(500, json.dumps({"error": str(e)}).encode(),
                               "application/json")
            elif path == "/api/state":
                try:
                    state = build_state(tracker, config)
                    self._send(200, json.dumps(state).encode(), "application/json")
                except Exception as e:
                    _log.error("state build failed: %s", e, exc_info=True)
                    self._send(500, json.dumps({"error": str(e)}).encode(),
                               "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass                      # HTTP access logs would drown the trade log

    return Handler


def serve(tracker: Tracker, config: dict[str, Any], port: int = 8420) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(tracker, config))
    _log.info("dashboard on http://127.0.0.1:%d  (localhost only)", port)
    httpd.serve_forever()
