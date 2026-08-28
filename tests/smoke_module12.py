"""Smoke test for Module 12 (bot/arbitrage_scanner.py).

Binary complement arbitrage, observation-only. Every test is deterministic and
offline — no network, synthetic books, temporary SQLite.

Covers:
   1. VWAP uses best asks and walks multiple depth levels
   2. Equal-share capacity is limited by the weaker book
   3. A midpoint-only "opportunity" that fails at asks is rejected
   4. Fees + slippage turn a gross-positive candidate into a rejection
   5. A valid synthetic opportunity is executable with correct P&L / bps
   6. missing token, one-sided book, stale book, thin depth reject explicitly
   7. SQLite insert + exactly one audit JSONL event (and dedup)
   8. Scanner integration with a fake read-only client + temp DB
   9. The module never touches executor / order-placement APIs

Run:
    .venv/bin/python tests/smoke_module12.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.arbitrage_scanner import (  # noqa: E402
    ArbitrageScanner, ask_capacity, max_pair_shares, normalise_asks, vwap_buy,
)
from bot.tracker import Tracker  # noqa: E402
from utils.helpers import now_ms  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> bool:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")
    if not cond:
        _FAILS.append(name)
    return cond


def book(asks, bids=None):
    return {"asks": [[p, s] for p, s in asks],
            "bids": [[p, s] for p, s in (bids or [])]}


def market(mid="0xtest", yes="tok_yes", no="tok_no", **over):
    m = {"condition_id": mid, "question": "Test market?",
         "active": True, "closed": False, "archived": False,
         "accepting_orders": True, "enable_order_book": True,
         "tokens": [{"outcome": "Yes", "token_id": yes},
                    {"outcome": "No", "token_id": no}]}
    m.update(over)
    return m


CFG = {"arbitrage_scanner": {
    "enabled": True, "scan_limit": 50, "max_notional_usd": 25, "min_shares": 5,
    "min_net_edge_bps": 100, "estimated_fee_bps_per_leg": 100,
    "slippage_buffer_bps_per_leg": 50, "max_book_age_seconds": 10,
    "require_binary_yes_no": True, "exclude_closed_or_resolved": True}}


def cfg(**over):
    c = {"arbitrage_scanner": dict(CFG["arbitrage_scanner"])}
    c["arbitrage_scanner"].update(over)
    return c


def scanner(tracker=None, **over):
    return ArbitrageScanner(client=None, tracker=tracker, config=cfg(**over))


# ── 1. VWAP walks the ask book cheapest-first ────────────────────────────────
print("\n[1] VWAP over depth")
asks = [(0.40, 10.0), (0.42, 20.0), (0.50, 5.0)]
cost, vwap, levels = vwap_buy(asks, 10)
check("single level: 10 @ 0.40 -> $4.00", abs(cost - 4.0) < 1e-9 and levels == 1)
cost, vwap, levels = vwap_buy(asks, 25)
# 10*0.40 + 15*0.42 = 4.00 + 6.30 = 10.30 -> vwap 0.412
check(f"two levels: 25 shares -> ${cost:.2f}, vwap {vwap:.4f}, levels {levels}",
      abs(cost - 10.30) < 1e-9 and abs(vwap - 0.412) < 1e-9 and levels == 2)
check("vwap over depth exceeds best ask (structural)", vwap > asks[0][0])
check("unsorted asks are sorted cheapest-first",
      normalise_asks({"asks": [[0.9, 1], [0.1, 1]]})[0][0] == 0.1)
try:
    vwap_buy(asks, 999)
    check("over-capacity raises rather than silently partial-filling", False)
except ValueError:
    check("over-capacity raises rather than silently partial-filling", True)

# Regression: tolerance must scale with size. A live scan raised
# "short by 0.000000" on a 3.19M-share fill where float error exceeded a
# fixed 1e-9 epsilon — a real failure that looked like zero.
big = [(0.40, 2_000_000.0), (0.41, 1_500_000.0)]
n_big = max_pair_shares(big, [(0.55, 4_000_000.0)], budget_usd=3_000_000)
try:
    cost, vwap, lv = vwap_buy(big, n_big)
    check(f"large-book fill does not false-fail ({n_big:,.0f} shares)", cost > 0)
except ValueError as e:
    check(f"large-book fill does not false-fail — raised {e}", False)

# ── 2. Capacity limited by the weaker book ───────────────────────────────────
print("\n[2] Equal-share capacity")
yes_asks = [(0.40, 100.0)]
no_asks = [(0.45, 7.0)]
check("capacity is the weaker book (7, not 100)",
      min(ask_capacity(yes_asks), ask_capacity(no_asks)) == 7.0)
n = max_pair_shares(yes_asks, no_asks, budget_usd=1000)
check(f"max_pair_shares caps at weaker book ({n:.1f})", abs(n - 7.0) < 1e-6)
n = max_pair_shares([(0.40, 1000.0)], [(0.45, 1000.0)], budget_usd=8.5)
check(f"budget binds when depth is ample ({n:.2f} shares ~ $8.50/0.85)",
      abs(n - 10.0) < 1e-6)

# ── 3. Midpoint mirage rejected at asks ──────────────────────────────────────
print("\n[3] Midpoint-only 'opportunity' is rejected")
# Midpoints: YES (0.30+0.44)/2=0.37, NO (0.40+0.54)/2=0.47 -> 0.84 "profit".
# Asks: 0.44 + 0.54 = 0.98 gross, which fees+slippage erase.
mid_trap = scanner().evaluate(
    market(),
    book(asks=[(0.44, 100.0)], bids=[(0.30, 100.0)]),
    book(asks=[(0.54, 100.0)], bids=[(0.40, 100.0)]),
    observed_at_ts=now_ms(), book_age_ms=10)
check("ask-side pricing used, not midpoint",
      abs(mid_trap.yes_leg.vwap_price - 0.44) < 1e-9)
check(f"midpoint mirage rejected ({mid_trap.reject_reason})",
      not mid_trap.executable and mid_trap.reject_reason in
      ("not_profitable", "edge_below_threshold"))

# ── 4. Fees + slippage flip a gross-positive candidate ───────────────────────
print("\n[4] Cost model bites")
# 0.485 + 0.485 = 0.97/pair -> gross +3%. Fees 2% + slippage 1% of cost = ~2.9%.
thin = scanner().evaluate(
    market(), book([(0.485, 100.0)]), book([(0.485, 100.0)]),
    observed_at_ts=now_ms(), book_age_ms=10)
gross_margin = thin.gross_payout_usd - thin.gross_cost_usd
check(f"gross is positive (${gross_margin:+.4f})", gross_margin > 0)
check(f"costs applied (fees ${thin.estimated_fees_usd:.4f} + "
      f"slip ${thin.estimated_slippage_usd:.4f})",
      thin.estimated_fees_usd > 0 and thin.estimated_slippage_usd > 0)
check(f"net rejected after costs ({thin.reject_reason})", not thin.executable)

# ── 4b. Fee formula matches Polymarket's published table ────────────────────
print("\n[4b] Documented fee formula")
_s = scanner(taker_fee_rate=0.07)                       # Crypto category rate
for _p, _want in ((0.01, 0.07), (0.05, 0.33), (0.50, 1.75), (0.95, 0.33)):
    _got = _s._taker_fee_usd(100, _p)
    check(f"100 shares @ {_p:.2f} -> ${_got:.2f} (docs ${_want:.2f})",
          abs(_got - _want) < 0.02)
check("fee is symmetric about p=0.5",
      abs(_s._taker_fee_usd(100, 0.30) - _s._taker_fee_usd(100, 0.70)) < 1e-9)
check("fee is zero at the boundaries",
      _s._taker_fee_usd(100, 0.0) == 0.0 and _s._taker_fee_usd(100, 1.0) == 0.0)
check("geopolitics rate 0 -> no fee",
      scanner(taker_fee_rate=0.0)._taker_fee_usd(100, 0.5) == 0.0)
# Both legs of a pair cost the same fee, whatever the price — p(1-p) is
# symmetric, so buying YES at p and NO at (1-p) charges each leg identically.
check("both complement legs incur equal fees",
      abs(_s._taker_fee_usd(100, 0.30) - _s._taker_fee_usd(100, 0.70)) < 1e-9)

# ── 5. A genuine opportunity is executable with correct arithmetic ───────────
print("\n[5] Valid synthetic opportunity")
opp = scanner().evaluate(
    market(), book([(0.40, 50.0)]), book([(0.45, 50.0)]),
    observed_at_ts=now_ms(), book_age_ms=10)
# budget 25 / 0.85 per pair = 29.41 shares
exp_shares = 25.0 / 0.85
exp_cost = exp_shares * 0.85
# Polymarket's documented taker fee: C * rate * p * (1-p) per leg, both legs
# are takers. Verified against the published fee table in test [4b].
RATE = 0.05
exp_fees = (exp_shares * RATE * 0.40 * 0.60) + (exp_shares * RATE * 0.45 * 0.55)
exp_slip = exp_cost * 50 * 2 / 10_000
exp_pnl = exp_shares - exp_cost - exp_fees - exp_slip
exp_bps = exp_pnl / exp_cost * 10_000
check(f"executable ({opp.equal_shares:.2f} shares)", opp.executable)
check("reject_reason is None when executable", opp.reject_reason is None)
check(f"shares match budget ({opp.equal_shares:.4f} vs {exp_shares:.4f})",
      abs(opp.equal_shares - exp_shares) < 1e-6)
check(f"gross cost ${opp.gross_cost_usd:.4f}", abs(opp.gross_cost_usd - exp_cost) < 1e-6)
check(f"net p&l ${opp.net_locked_pnl_usd:.4f}", abs(opp.net_locked_pnl_usd - exp_pnl) < 1e-6)
check(f"net edge {opp.net_edge_bps:.1f} bps", abs(opp.net_edge_bps - exp_bps) < 1e-6)
check("payout is shares x $1.00", abs(opp.gross_payout_usd - opp.equal_shares) < 1e-9)
check("legs record levels used", opp.yes_leg.levels_used == 1 and opp.no_leg.levels_used == 1)
ev = json.loads(opp.evidence_json)
check("evidence carries market metadata + question",
      ev["market"]["question"] == "Test market?" and "tokens" in ev["market"])
check("evidence records the assumed $1 pair payout",
      ev["assumed_pair_payout_usd"] == 1.0)
check("evidence records the cost model used",
      ev["cost_model"]["fee_bps_per_leg"] == 100)

# ── 6. Explicit rejection reasons ────────────────────────────────────────────
print("\n[6] Rejections are explicit")
no_tok = scanner().evaluate(
    {"condition_id": "0xa", "tokens": [{"outcome": "Yes", "token_id": "y"}]},
    book([(0.4, 10)]), book([(0.4, 10)]), observed_at_ts=now_ms(), book_age_ms=1)
check("missing token -> missing_token", no_tok.reject_reason == "missing_token")

one_sided = scanner().evaluate(
    market(), book(asks=[], bids=[(0.40, 100.0)]), book([(0.45, 100.0)]),
    observed_at_ts=now_ms(), book_age_ms=1)
check("no asks on a side -> one_sided_book", one_sided.reject_reason == "one_sided_book")

stale = scanner().evaluate(
    market(), book([(0.40, 50.0)]), book([(0.45, 50.0)]),
    observed_at_ts=now_ms(), book_age_ms=60_000)
check("old book -> stale_book", stale.reject_reason == "stale_book")
check("stale record still carries the pricing it would have taken",
      stale.equal_shares > 0 and stale.net_locked_pnl_usd > 0)

thin_yes = scanner().evaluate(
    market(), book([(0.40, 2.0)]), book([(0.45, 100.0)]),
    observed_at_ts=now_ms(), book_age_ms=1)
check("thin YES -> insufficient_yes_depth",
      thin_yes.reject_reason == "insufficient_yes_depth")
thin_no = scanner().evaluate(
    market(), book([(0.40, 100.0)]), book([(0.45, 2.0)]),
    observed_at_ts=now_ms(), book_age_ms=1)
check("thin NO -> insufficient_no_depth",
      thin_no.reject_reason == "insufficient_no_depth")
check("closed market is ineligible", not scanner().is_eligible(market(closed=True)))
check("non-binary market is ineligible", not scanner().is_eligible(
    market(tokens=[{"outcome": "A", "token_id": "1"},
                   {"outcome": "B", "token_id": "2"}])))

# ── 7. Persistence + audit ───────────────────────────────────────────────────
print("\n[7] SQLite + audit")
tmp = Path(tempfile.mkdtemp())
tr = Tracker(db_path=tmp / "t.db", audit_log_path=tmp / "a.jsonl")
rid = tr.record_arbitrage_opportunity(opp)
check("insert returns a row id", isinstance(rid, int) and rid > 0)
rows = tr.get_arbitrage_observations(0)
check("row is readable back", len(rows) == 1 and rows[0]["executable"] == 1)
check("evidence_json round-trips", json.loads(rows[0]["evidence_json"])["market"]
      ["condition_id"] == "0xtest")
events = [json.loads(x) for x in (tmp / "a.jsonl").read_text().splitlines() if x.strip()]
arb_events = [e for e in events if e["event_type"] == "arbitrage_opportunity"]
check("exactly one audit event per inserted row", len(arb_events) == 1)
check("audit event carries the verdict",
      arb_events[0]["payload"]["executable"] is True)

dup = tr.record_arbitrage_opportunity(opp)          # same market + same ts
check("duplicate observation returns None", dup is None)
check("duplicate does not add a row", len(tr.get_arbitrage_observations(0)) == 1)
events = [json.loads(x) for x in (tmp / "a.jsonl").read_text().splitlines() if x.strip()]
check("duplicate emits no second audit event",
      len([e for e in events if e["event_type"] == "arbitrage_opportunity"]) == 1)

rejected = scanner().evaluate(market(mid="0xb"), book([(0.485, 100.0)]),
                              book([(0.485, 100.0)]),
                              observed_at_ts=now_ms() + 1, book_age_ms=5)
tr.record_arbitrage_opportunity(rejected)
check("rejected candidates are persisted too",
      len(tr.get_arbitrage_observations(0)) == 2)
check("executable_only filter works",
      len(tr.get_arbitrage_observations(0, executable_only=True)) == 1)
check("top-N orders executable first",
      tr.get_top_arbitrage(0, limit=5)[0]["executable"] == 1)

# ── 8. Scanner integration with a fake client ────────────────────────────────
print("\n[8] scan_once integration (offline)")


class FakeClient:
    """Read-only stand-in. Any order method would be an AttributeError."""

    def __init__(self):
        self.book_calls = 0

    def get_active_markets(self, limit=500):
        return [
            market(mid="0xgood", yes="gy", no="gn"),
            market(mid="0xbad", yes="by", no="bn"),
            market(mid="0xclosed", closed=True),
            market(mid="0xnonbinary",
                   tokens=[{"outcome": "A", "token_id": "1"},
                           {"outcome": "B", "token_id": "2"}]),
        ]

    def get_order_book(self, token_id):
        self.book_calls += 1
        return {"gy": book([(0.40, 50.0)]), "gn": book([(0.45, 50.0)]),
                "by": book([(0.60, 50.0)]), "bn": book([(0.55, 50.0)])
                }.get(token_id, book([]))


fc = FakeClient()
tr2 = Tracker(db_path=tmp / "t2.db", audit_log_path=tmp / "a2.jsonl")
res = scanner(tracker=tr2)
res._client = fc
r = res.scan_once()
check(f"considered all markets ({r.markets_considered})", r.markets_considered == 4)
check(f"eligibility filtered closed + non-binary ({r.markets_eligible})",
      r.markets_eligible == 2)
check(f"both eligible markets observed ({r.opportunities_found})",
      r.opportunities_found == 2)
check(f"one executable ({r.executable_opportunities})", r.executable_opportunities == 1)
check("executable notional recorded", r.total_executable_notional_usd > 0)
check("no errors", r.errors == 0)
check("all observations persisted", len(tr2.get_arbitrage_observations(0)) == 2)
check("ineligible markets never had books fetched (4 calls, not 8)",
      fc.book_calls == 4)

disabled = scanner(tracker=tr2, enabled=False)
disabled._client = fc
check("disabled scanner is a no-op", disabled.scan_once().markets_considered == 0)

tr.close()
tr2.close()

# ── 9. Observation-only guarantee ────────────────────────────────────────────
print("\n[9] No execution paths")
check("bot.executor not imported by this module", "bot.executor" not in sys.modules)

# Structural, not a substring grep: the docstring legitimately *mentions*
# bot.executor to state that it is never imported, and a prose mention is not
# an execution path. Parse the AST and inspect real imports and call sites.
import ast  # noqa: E402

tree = ast.parse((Path(__file__).resolve().parent.parent /
                  "bot" / "arbitrage_scanner.py").read_text())

imported: set[str] = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imported.update(a.name for a in node.names)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            imported.add(node.module)
check(f"no executor import (imports: {sorted(imported)})",
      not any("executor" in m for m in imported))

FORBIDDEN = {"place_order", "post_order", "post_orders", "create_order",
             "create_market_order", "create_and_post_order", "cancel",
             "cancel_order", "cancel_all", "cancel_orders",
             "cancel_market_orders", "sign", "sync_quote"}
called: set[str] = set()
attrs: set[str] = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            called.add(f.id)
        elif isinstance(f, ast.Attribute):
            called.add(f.attr)
    elif isinstance(node, ast.Attribute):
        attrs.add(node.attr)
bad_calls = FORBIDDEN & called
bad_attrs = FORBIDDEN & attrs
check(f"no order-placement calls (offenders: {sorted(bad_calls) or 'none'})",
      not bad_calls)
check(f"no order-placement attribute access (offenders: {sorted(bad_attrs) or 'none'})",
      not bad_attrs)

# The client surface actually used must be read-only.
client_methods = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Attribute)
                  and n.func.value.attr == "_client"}
check(f"client calls are read-only only ({sorted(client_methods)})",
      client_methods <= {"get_active_markets", "get_order_book", "get_market"})

print()
if _FAILS:
    print(f"module 12 smoke: {len(_FAILS)} FAILURE(S)")
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("module 12 smoke: OK")
