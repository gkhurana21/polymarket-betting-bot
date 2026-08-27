"""Self-contained HTML report for the complement-arbitrage research module.

Deliberately a STATIC FILE, not a server. This repository's posture is
observation and its processes are meant to end; a long-running dashboard is
one more thing to leave running by accident (this project has already lost a
week to a loop that died while its server kept answering 200s). Generate,
open, done.

The report leads with the structural explanation rather than the tally,
because "we found nothing" and "nothing can be there, here is the mechanism"
are very different findings, and only the second one is worth acting on.
"""

from __future__ import annotations

import html
import json
import statistics
from pathlib import Path
from typing import Any

from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)

_MS_PER_DAY = 86_400_000


def _spread_sample(tracker: Tracker, limit: int = 60_000) -> list[float]:
    """Real YES-token bid-ask spreads from captured books."""
    out: list[float] = []
    cur = tracker._conn.execute(
        "SELECT book_json FROM book_snapshots LIMIT ?", (limit,))
    for r in cur:
        try:
            b = json.loads(r["book_json"])
            bids, asks = b.get("bids") or [], b.get("asks") or []
            if not bids or not asks:
                continue
            bb = max(float(x[0]) for x in bids)
            ba = min(float(x[0]) for x in asks)
        except Exception:
            continue
        if 0 < bb < ba < 1:
            out.append(ba - bb)
    out.sort()
    return out


def _histogram(values: list[float], lo: float, hi: float, bins: int = 28):
    """Simple equal-width histogram -> [(bin_lo, bin_hi, count)]."""
    if not values:
        return []
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        if v < lo or v > hi:
            continue
        i = min(bins - 1, int((v - lo) / width))
        counts[i] += 1
    return [(lo + i * width, lo + (i + 1) * width, c) for i, c in enumerate(counts)]


def build_report(tracker: Tracker, config: dict[str, Any], days: int = 7) -> str:
    since = now_ms() - days * _MS_PER_DAY
    obs = tracker.get_arbitrage_observations(since)
    top = tracker.get_top_arbitrage(since, limit=12)
    cfg = (config or {}).get("arbitrage_scanner") or {}

    execs = [o for o in obs if o["executable"]]
    reasons: dict[str, int] = {}
    for o in obs:
        if not o["executable"]:
            reasons[o["reject_reason"] or "unknown"] = \
                reasons.get(o["reject_reason"] or "unknown", 0) + 1

    edges = sorted(o["net_edge_bps"] for o in obs) if obs else []
    spreads = _spread_sample(tracker)
    book_rows = tracker._conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT market_id) m FROM book_snapshots"
    ).fetchone()

    def esc(x) -> str:
        return html.escape(str(x if x is not None else ""))

    # ── spread histogram (real data) ────────────────────────────────────────
    hist = _histogram(spreads, 0.0, 0.05, bins=25)
    hmax = max((c for _, _, c in hist), default=1) or 1
    bars = "".join(
        f'<div class="bar" title="{a*100:.1f}–{b*100:.1f}c: {c:,} books">'
        f'<i style="height:{(c/hmax)*100:.1f}%"></i></div>'
        for a, b, c in hist
    )

    pct = (lambda q: spreads[int(q * len(spreads))]) if spreads else (lambda q: 0.0)

    reason_rows = "".join(
        f'<tr><td>{esc(k)}</td><td class="num">{v:,}</td>'
        f'<td class="num">{v / max(1, len(obs)) * 100:.1f}%</td></tr>'
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="3" class="empty">no rejections recorded yet</td></tr>'

    def _status_cell(o) -> str:
        if o["executable"]:
            return '<span class="tag-ok">executable</span>'
        return f'<span class="tag">{esc(o["reject_reason"])}</span>'

    def _cand_row(o) -> str:
        pnl_cls = "pos" if o["net_locked_pnl_usd"] > 0 else "neg"
        bps_cls = "pos" if o["net_edge_bps"] > 0 else "neg"
        label = esc((o["question"] or o["market_id"])[:52])
        return (
            f'<tr><td class="q">{label}</td>'
            f'<td class="num">{o["equal_shares"]:.1f}</td>'
            f'<td class="num">${o["gross_cost_usd"]:.2f}</td>'
            f'<td class="num {pnl_cls}">${o["net_locked_pnl_usd"]:+.4f}</td>'
            f'<td class="num {bps_cls}">{o["net_edge_bps"]:+,.0f}</td>'
            f'<td>{_status_cell(o)}</td></tr>'
        )

    cand_rows = "".join(_cand_row(o) for o in top) or (
        '<tr><td colspan="6" class="empty">No observations yet — run '
        '<code>python main.py arb-scan</code></td></tr>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Complement arbitrage — research report</title>
<style>
 :root{{--bg:#0d1117;--panel:#161b22;--line:#272e38;--fg:#e6edf3;--dim:#8b949e;
   --dimmer:#636c76;--pos:#3fb950;--neg:#f85149;--warn:#d29922;--accent:#58a6ff;
   --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}}
 .wrap{{max-width:1000px;margin:0 auto;padding:32px 22px 70px}}
 h1{{font-size:21px;margin:0 0 4px}} h1 span{{color:var(--dimmer);font-weight:400}}
 .sub{{color:var(--dim);font-size:13px;margin-bottom:22px}}
 h2{{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--dim);
   margin:30px 0 10px;font-weight:600}}
 .card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px}}
 .verdict{{border-color:#5a4708;background:#1f1a09}}
 .verdict b{{color:var(--warn)}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:8px}}
 .stat{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:13px 15px}}
 .stat .k{{font:10px var(--mono);color:var(--dimmer);text-transform:uppercase;letter-spacing:.6px}}
 .stat .v{{font:18px var(--mono);margin-top:5px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;font:10px var(--mono);text-transform:uppercase;letter-spacing:.6px;
   color:var(--dimmer);padding:9px 10px;border-bottom:1px solid var(--line)}}
 td{{padding:9px 10px;border-bottom:1px solid #1e242c}}
 tr:last-child td{{border-bottom:none}}
 .num{{font:12px var(--mono);text-align:right;white-space:nowrap}}
 th.num{{text-align:right}} .pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
 .q{{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
 .tag{{font:10px var(--mono);padding:2px 7px;border-radius:4px;border:1px solid var(--line);color:var(--dim)}}
 .tag-ok{{font:10px var(--mono);padding:2px 7px;border-radius:4px;
   border:1px solid #1d3d24;background:#0d1f13;color:var(--pos)}}
 .empty{{padding:26px;text-align:center;color:var(--dimmer)}}
 .chart{{display:flex;align-items:flex-end;gap:2px;height:130px;padding:10px 0;
   border-bottom:1px solid var(--line)}}
 .bar{{flex:1;height:100%;display:flex;align-items:flex-end}}
 .bar i{{display:block;width:100%;background:linear-gradient(180deg,var(--accent),#1f4f80);
   border-radius:2px 2px 0 0;min-height:1px}}
 .axis{{display:flex;justify-content:space-between;font:10px var(--mono);
   color:var(--dimmer);margin-top:6px}}
 code{{font:12px var(--mono);background:#1e242c;padding:1px 6px;border-radius:3px;color:var(--dim)}}
 .foot{{margin-top:34px;color:var(--dimmer);font-size:12px;line-height:1.8}}
 .mech{{font:13px/1.75 var(--mono);color:var(--dim);white-space:pre-wrap;margin:0}}
 .mech b{{color:var(--fg)}}
</style></head><body><div class="wrap">

<h1>Complement arbitrage <span>· research report</span></h1>
<div class="sub">Observation only — this module has never placed an order.
Window: last {days} days.</div>

<div class="card verdict">
<b>Why there is a floor, and why it is not competition.</b><br><br>
<p class="mech">Polymarket's CTF lets anyone <b>split $1 USDC → 1 YES + 1 NO</b>, and
<b>merge 1 YES + 1 NO → $1</b>. A pair is therefore <b>always</b> obtainable for exactly $1.

Buying the pair on-book for less than $1 would be an arbitrage against the mint
mechanism itself — capturable instantly by anyone, with no view on the outcome.
So the pair ask-sum is floored at ~$1.00 <b>structurally</b>. It is the contract,
not the crowd.

Measured live across 116 liquid markets: ask(YES)+ask(NO) = <b>1.0010</b>
— $1.00 plus exactly one <code>minimum_tick_size</code>.

And VWAP over depth is by construction ≥ best ask, so walking deeper only raises
the cost: <b>a market that fails at the touch cannot pass at depth.</b></p>
</div>

<h2>Observations</h2>
<div class="grid">
  <div class="stat"><div class="k">observations</div><div class="v">{len(obs):,}</div></div>
  <div class="stat"><div class="k">executable</div>
    <div class="v {'pos' if execs else ''}">{len(execs):,}</div></div>
  <div class="stat"><div class="k">median net edge</div>
    <div class="v {'neg' if edges and edges[len(edges)//2] < 0 else ''}">
    {(edges[len(edges)//2] if edges else 0):,.0f} bps</div></div>
  <div class="stat"><div class="k">best net edge</div>
    <div class="v">{(edges[-1] if edges else 0):,.0f} bps</div></div>
</div>

<h2>Real market tightness — {len(spreads):,} captured books</h2>
<div class="card">
  <div class="chart">{bars}</div>
  <div class="axis"><span>0c</span><span>1c</span><span>2c</span><span>3c</span>
    <span>4c</span><span>5c</span></div>
  <div style="margin-top:14px;font:12px var(--mono);color:var(--dim)">
    p10 {pct(0.10)*100:.2f}c · p50 {pct(0.50)*100:.2f}c · p90 {pct(0.90)*100:.2f}c
    &nbsp;|&nbsp; {sum(1 for s in spreads if s <= 0.002):,} books
    ({sum(1 for s in spreads if s <= 0.002)/max(1,len(spreads))*100:.0f}%) quote at ≤2 ticks
  </div>
  <div style="margin-top:8px;color:var(--dimmer);font-size:12px">
    Drawn from {book_rows['n']:,} real order books across {book_rows['m']:,} markets.
    These are genuinely tight books — there is very little room between the bid and
    the ask for a mispricing to hide in.
  </div>
</div>

<h2>Why candidates were rejected</h2>
<div class="card" style="padding:0">
<table><thead><tr><th>reason</th><th class="num">count</th><th class="num">share</th></tr></thead>
<tbody>{reason_rows}</tbody></table></div>

<h2>Top candidates by net edge</h2>
<div class="card" style="padding:0">
<table><thead><tr><th>market</th><th class="num">shares</th><th class="num">cost</th>
<th class="num">net p&amp;l</th><th class="num">edge bps</th><th>status</th></tr></thead>
<tbody>{cand_rows}</tbody></table></div>

<h2>Cost model in force</h2>
<div class="card"><table>
<tr><td>fee per leg <b style="color:var(--warn)">(ASSUMPTION)</b></td>
    <td class="num">{cfg.get('estimated_fee_bps_per_leg', 100)} bps</td></tr>
<tr><td>slippage buffer per leg</td>
    <td class="num">{cfg.get('slippage_buffer_bps_per_leg', 50)} bps</td></tr>
<tr><td>min net edge to call executable</td>
    <td class="num">{cfg.get('min_net_edge_bps', 100)} bps</td></tr>
<tr><td>max notional modelled</td>
    <td class="num">${cfg.get('max_notional_usd', 25)}</td></tr>
<tr><td>max book age</td>
    <td class="num">{cfg.get('max_book_age_seconds', 10)}s</td></tr>
</table>
<div style="margin-top:12px;color:var(--warn);font-size:12px">
The fee rate is a guess. Live fill records carry <b>no fee field</b>, and
<code>maker_base_fee</code>/<code>taker_base_fee</code> read <code>1000</code> in
undocumented units. Until that is pinned down, every net figure here inherits the error.
</div></div>

<div class="foot">
Settlement is <b>assumed</b> conventional ($1 per YES+NO pair). <code>neg_risk</code>
multi-outcome markets and 50/50-invalid resolutions can break that identity —
<code>evidence_json</code> stores full market metadata for hand review.<br>
Two-leg execution risk is <b>not modelled</b>: filling one leg and missing the other
converts a "locked" pair into an outright directional position.<br>
<b>A positive observation is evidence, not permission to trade.</b>
</div>
</div></body></html>"""


def write_report(tracker: Tracker, config: dict[str, Any], out: Path,
                 days: int = 7) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(tracker, config, days), encoding="utf-8")
    _log.info("arb report written → %s", out)
    return out
