"""Interactive HTML research report for the complement-arbitrage module.

A self-contained static file, not a server: this repository's processes are
meant to end, and a long-running dashboard is one more thing to leave running
by accident. All data is aggregated and embedded at generation time, so the
page is fully interactive with no backend and no network.

The report leads with the MECHANISM rather than the tally. "We found nothing"
and "nothing can be there, and here is why" are different findings, and only
the second one is worth acting on.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from bot.arb_data import build_corpus_summary
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)

_MS_PER_DAY = 86_400_000


def build_report(tracker: Tracker, config: dict[str, Any], days: int = 7,
                 corpus_limit: int = 250_000) -> str:
    since = now_ms() - days * _MS_PER_DAY
    obs = tracker.get_arbitrage_observations(since)
    top = tracker.get_top_arbitrage(since, limit=25)
    cfg = (config or {}).get("arbitrage_scanner") or {}

    corpus = build_corpus_summary(tracker, limit=corpus_limit)

    execs = [o for o in obs if o["executable"]]
    reasons: dict[str, int] = {}
    for o in obs:
        if not o["executable"]:
            k = o["reject_reason"] or "unknown"
            reasons[k] = reasons.get(k, 0) + 1
    edges = sorted(o["net_edge_bps"] for o in obs) if obs else []

    payload = {
        "corpus": corpus,
        "obs": {
            "n": len(obs), "executable": len(execs),
            "median_edge": edges[len(edges) // 2] if edges else 0,
            "best_edge": edges[-1] if edges else 0,
            "reasons": reasons,
            "top": [{
                "q": (o["question"] or o["market_id"])[:64],
                "shares": o["equal_shares"], "cost": o["gross_cost_usd"],
                "pnl": o["net_locked_pnl_usd"], "bps": o["net_edge_bps"],
                "exec": bool(o["executable"]), "reason": o["reject_reason"],
            } for o in top],
        },
        "cost_model": {
            "fee_bps": cfg.get("estimated_fee_bps_per_leg", 100),
            "slip_bps": cfg.get("slippage_buffer_bps_per_leg", 50),
            "min_edge_bps": cfg.get("min_net_edge_bps", 100),
            "max_notional": cfg.get("max_notional_usd", 25),
            "max_age_s": cfg.get("max_book_age_seconds", 10),
        },
        "days": days,
    }

    return _TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))


def write_report(tracker: Tracker, config: dict[str, Any], out: Path,
                 days: int = 7) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(tracker, config, days), encoding="utf-8")
    _log.info("arb report written → %s", out)
    return out


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Complement arbitrage — research explorer</title>
<style>
 :root{--bg:#0b0f14;--panel:#141a22;--panel2:#1a212b;--line:#252d38;--fg:#e6edf3;
  --dim:#8b949e;--dimmer:#5f6873;--pos:#3fb950;--neg:#f85149;--warn:#d29922;
  --accent:#58a6ff;--accent2:#a371f7;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
 .wrap{max-width:1120px;margin:0 auto;padding:30px 22px 80px}
 h1{font-size:22px;margin:0 0 3px} h1 span{color:var(--dimmer);font-weight:400}
 .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--dim);
  margin:30px 0 10px;font-weight:600;display:flex;align-items:center;gap:10px}
 h2 .hint{font:10px var(--mono);color:var(--dimmer);text-transform:none;letter-spacing:0}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
 details.mech{background:#1b1708;border:1px solid #5a4708;border-radius:10px;padding:0}
 details.mech summary{padding:15px 20px;cursor:pointer;color:var(--warn);
  font-weight:600;list-style:none;user-select:none}
 details.mech summary::-webkit-details-marker{display:none}
 details.mech summary:before{content:"▸ ";transition:.2s}
 details.mech[open] summary:before{content:"▾ "}
 details.mech .body{padding:0 20px 18px;font:13px/1.75 var(--mono);color:var(--dim);white-space:pre-wrap}
 details.mech b{color:var(--fg)}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px}
 .stat{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:13px 15px}
 .stat .k{font:10px var(--mono);color:var(--dimmer);text-transform:uppercase;letter-spacing:.6px}
 .stat .v{font:19px var(--mono);margin-top:5px}
 .stat .s{font:11px var(--mono);color:var(--dimmer);margin-top:2px}
 svg{display:block;width:100%;overflow:visible}
 #hist{height:200px} #levels{height:210px} #timeline{height:190px}
 /* 250 market rows rendered flat made the page 12,000px tall and pushed every
    other panel off-screen. Scroll the table, not the document. */
 .scroller{max-height:430px;overflow-y:auto}
 .scroller thead th{position:sticky;top:0;background:var(--panel);z-index:2}
 .scroller::-webkit-scrollbar{width:10px}
 .scroller::-webkit-scrollbar-thumb{background:#2b3543;border-radius:5px}
 .scroller::-webkit-scrollbar-track{background:transparent}
 .bar{cursor:pointer;transition:opacity .12s}
 .bar:hover{opacity:.75}
 .bar.sel rect{stroke:#fff;stroke-width:1.5}
 .axis text{font:10px var(--mono);fill:var(--dimmer)}
 .axis line{stroke:var(--line)}
 .gl{stroke:#1e242c;stroke-dasharray:2 3}
 #tip{position:fixed;pointer-events:none;background:#0b0f14;border:1px solid var(--line);
  border-radius:6px;padding:7px 10px;font:11px var(--mono);color:var(--fg);
  box-shadow:0 6px 20px rgba(0,0,0,.55);opacity:0;transition:opacity .1s;z-index:50;white-space:pre}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th{text-align:left;font:10px var(--mono);text-transform:uppercase;letter-spacing:.6px;
  color:var(--dimmer);padding:9px 10px;border-bottom:1px solid var(--line);
  cursor:pointer;user-select:none;white-space:nowrap}
 th:hover{color:var(--fg)} th.up:after{content:" ▲"} th.down:after{content:" ▼"}
 td{padding:8px 10px;border-bottom:1px solid #1a2029}
 tr:last-child td{border-bottom:none} tbody tr:hover td{background:var(--panel2)}
 .num{font:12px var(--mono);text-align:right;white-space:nowrap}
 th.num{text-align:right} .pos{color:var(--pos)} .neg{color:var(--neg)}
 .q{max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tag{font:10px var(--mono);padding:2px 7px;border-radius:4px;border:1px solid var(--line);color:var(--dim)}
 .tag-ok{font:10px var(--mono);padding:2px 7px;border-radius:4px;border:1px solid #1d3d24;
  background:#0d1f13;color:var(--pos)}
 .empty{padding:28px;text-align:center;color:var(--dimmer)}
 input[type=search]{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
  color:var(--fg);padding:8px 11px;font:12px var(--mono);width:260px;outline:none}
 input[type=search]:focus{border-color:var(--accent)}
 .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
 .chip{font:11px var(--mono);padding:5px 10px;border-radius:99px;border:1px solid var(--accent);
  background:#0f2033;color:var(--accent);cursor:pointer}
 .chip:hover{background:#16304d}
 code{font:12px var(--mono);background:#1a2029;padding:1px 6px;border-radius:3px;color:var(--dim)}
 .foot{margin-top:34px;color:var(--dimmer);font-size:12px;line-height:1.85}
 .warn{color:var(--warn)}
</style></head><body><div class="wrap">

<h1>Complement arbitrage <span>· research explorer</span></h1>
<div class="sub" id="subtitle">Observation only — this module has never placed an order.</div>

<details class="mech" open>
<summary>Why there is a floor, and why it is not competition</summary>
<div class="body">Polymarket's CTF lets anyone <b>split $1 USDC → 1 YES + 1 NO</b>, and <b>merge 1 YES + 1 NO → $1</b>.
A pair is therefore <b>always</b> obtainable for exactly $1.

Buying the pair on-book for less than $1 would be an arbitrage against the mint mechanism
itself — capturable instantly by anyone, with no view on the outcome. So the pair ask-sum
is floored at ~$1.00 <b>structurally. It is the contract, not the crowd.</b>

Measured live across 116 liquid markets: ask(YES)+ask(NO) = <b>1.0010</b> — $1.00 plus exactly
one <b>minimum_tick_size</b>.

And VWAP over depth is by construction ≥ best ask, so walking deeper only raises the cost:
<b>a market that fails at the touch cannot pass at depth.</b></div>
</details>

<h2>The captured corpus <span class="hint">real Polymarket books, YES side</span></h2>
<div class="grid" id="corpusStats"></div>

<h2>Spread distribution <span class="hint">click a bar to filter markets · hover for counts</span></h2>
<div class="card"><svg id="hist" height="200"></svg></div>

<h2>Spread by price level <span class="hint">click to filter · where does the book widen?</span></h2>
<div class="card"><svg id="levels" height="210"></svg></div>

<h2>Spread over time <span class="hint">daily median and p90</span></h2>
<div class="card"><svg id="timeline" height="190"></svg></div>

<h2>Market explorer</h2>
<div class="toolbar">
  <input type="search" id="search" placeholder="filter markets…">
  <span id="filterChip"></span>
  <span style="margin-left:auto;color:var(--dimmer);font:11px var(--mono)" id="rowCount"></span>
</div>
<div class="card scroller" style="padding:0"><table id="mkt">
<thead><tr>
  <th data-k="q">market</th>
  <th class="num" data-k="n">books</th>
  <th class="num" data-k="mid">median mid</th>
  <th class="num" data-k="spread">median spread</th>
  <th class="num" data-k="spread_p90">p90 spread</th>
  <th class="num" data-k="depth">touch depth</th>
</tr></thead><tbody></tbody></table></div>

<h2>Scanner observations <span class="hint">populated by <code>main.py arb-scan</code></span></h2>
<div class="grid" id="obsStats"></div>
<div class="card scroller" style="padding:0;margin-top:10px;max-height:320px"><table id="obsTbl">
<thead><tr><th>market</th><th class="num">shares</th><th class="num">cost</th>
<th class="num">net p&amp;l</th><th class="num">edge bps</th><th>status</th></tr></thead>
<tbody></tbody></table></div>
<div class="card" style="margin-top:10px" id="reasonsCard"></div>

<h2>Cost model in force</h2>
<div class="card" id="costModel"></div>

<div class="foot">
Settlement is <b>assumed</b> conventional ($1 per YES+NO pair). <code>neg_risk</code> multi-outcome
markets and 50/50-invalid resolutions can break that identity — <code>evidence_json</code> stores full
market metadata for hand review.<br>
Two-leg execution risk is <b>not modelled</b>: filling one leg and missing the other converts a
"locked" pair into an outright directional position.<br>
<b class="warn">A positive observation is evidence, not permission to trade.</b>
</div>
</div>
<div id="tip"></div>

<script>
const D = __DATA__;
const C = D.corpus;
const $ = s => document.querySelector(s);
const fmt = n => n.toLocaleString();
const cents = v => (v*100).toFixed(2)+"c";
const tip = $("#tip");
function showTip(e, text){ tip.textContent = text; tip.style.opacity = 1;
  const pad = 14;
  tip.style.left = Math.min(innerWidth - tip.offsetWidth - 8, e.clientX + pad) + "px";
  tip.style.top  = Math.max(8, e.clientY - tip.offsetHeight - pad) + "px"; }
function hideTip(){ tip.style.opacity = 0; }

const state = { level: null, search: "", sort: { k: "n", dir: -1 } };

// ── stat cards ──────────────────────────────────────────────────────────────
$("#subtitle").textContent =
  `Observation only — this module has never placed an order. `
  + `Corpus: ${fmt(C.n_usable)} two-sided books from ${fmt(C.n_books_total)} captured `
  + `across ${fmt(C.n_markets_total)} markets. Scanner window: last ${D.days} days.`;

$("#corpusStats").innerHTML = [
  ["two-sided books", fmt(C.n_usable), "usable of "+fmt(C.n_books_total)],
  ["median spread", cents(C.pct["0.5"]), "p90 "+cents(C.pct["0.9"])],
  ["tightest decile", cents(C.pct["0.1"]), "p10"],
  ["quoting ≤2 ticks", fmt(C.tight_2tick),
    (C.tight_2tick/C.n_usable*100).toFixed(0)+"% of books"],
].map(([k,v,s]) =>
  `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div>
   <div class="s">${s}</div></div>`).join("");

$("#obsStats").innerHTML = [
  ["observations", fmt(D.obs.n), ""],
  ["executable", fmt(D.obs.executable), D.obs.n ? (D.obs.executable/D.obs.n*100).toFixed(1)+"%" : "—"],
  ["median net edge", D.obs.median_edge.toFixed(0)+" bps", ""],
  ["best net edge", D.obs.best_edge.toFixed(0)+" bps", ""],
].map(([k,v,s]) =>
  `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div>
   <div class="s">${s}</div></div>`).join("");

const cm = D.cost_model;
$("#costModel").innerHTML = `<table>
 <tr><td>fee per leg <b class="warn">(ASSUMPTION)</b></td><td class="num">${cm.fee_bps} bps</td></tr>
 <tr><td>slippage buffer per leg</td><td class="num">${cm.slip_bps} bps</td></tr>
 <tr><td>min net edge to call executable</td><td class="num">${cm.min_edge_bps} bps</td></tr>
 <tr><td>max notional modelled</td><td class="num">$${cm.max_notional}</td></tr>
 <tr><td>max book age</td><td class="num">${cm.max_age_s}s</td></tr></table>
 <div style="margin-top:12px;font-size:12px" class="warn">The fee rate is a guess. Live fill
 records carry <b>no fee field</b>, and <code>maker_base_fee</code>/<code>taker_base_fee</code>
 read <code>1000</code> in undocumented units. Until that is pinned down, every net figure
 here inherits the error.</div>`;

const rk = Object.entries(D.obs.reasons).sort((a,b)=>b[1]-a[1]);
$("#reasonsCard").innerHTML = rk.length
  ? `<div style="font:10px var(--mono);color:var(--dimmer);text-transform:uppercase;
      letter-spacing:.6px;margin-bottom:8px">why candidates were rejected</div>`
    + `<table>${rk.map(([k,v])=>`<tr><td>${k}</td><td class="num">${fmt(v)}</td>
       <td class="num">${(v/D.obs.n*100).toFixed(1)}%</td></tr>`).join("")}</table>`
  : `<div class="empty">No scanner observations in this window — run
     <code>python main.py arb-scan</code>. The corpus panels above are independent of it.</div>`;

$("#obsTbl").querySelector("tbody").innerHTML = D.obs.top.length
  ? D.obs.top.map(o=>`<tr><td class="q">${o.q.replace(/[<>&]/g,"")}</td>
      <td class="num">${o.shares.toFixed(1)}</td>
      <td class="num">$${o.cost.toFixed(2)}</td>
      <td class="num ${o.pnl>0?"pos":"neg"}">$${o.pnl.toFixed(4)}</td>
      <td class="num ${o.bps>0?"pos":"neg"}">${o.bps.toFixed(0)}</td>
      <td>${o.exec?'<span class="tag-ok">executable</span>'
                  :'<span class="tag">'+(o.reason||"")+'</span>'}</td></tr>`).join("")
  : `<tr><td colspan="6" class="empty">No observations yet</td></tr>`;

// ── chart helpers ───────────────────────────────────────────────────────────
const NS = "http://www.w3.org/2000/svg";
function el(n, attrs, parent){ const e = document.createElementNS(NS, n);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e); return e; }
function clear(s){ while (s.firstChild) s.removeChild(s.firstChild); }

// ── spread histogram ────────────────────────────────────────────────────────
function drawHist(){
  const svg = $("#hist"); clear(svg);
  const W = svg.clientWidth || 900, H = 200, PAD = {l:46,r:12,t:12,b:26};
  const h = C.hist, max = Math.max(...h.counts) || 1;
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  for (let i=0;i<=4;i++){
    const y = PAD.t + ih*(i/4);
    el("line",{x1:PAD.l,x2:W-PAD.r,y1:y,y2:y,class:"gl"},svg);
    el("text",{x:PAD.l-8,y:y+3,"text-anchor":"end",class:"axis"},svg)
      .textContent = fmt(Math.round(max*(1-i/4)));
  }
  const bw = iw/h.counts.length;
  h.counts.forEach((c,i)=>{
    const bh = (c/max)*ih, x = PAD.l+i*bw, y = PAD.t+ih-bh;
    const g = el("g",{class:"bar"},svg);
    el("rect",{x:x+1,y,width:Math.max(1,bw-2),height:Math.max(1,bh),
      fill:"url(#gr)",rx:2},g);
    const a=(h.lo+i*h.width)*100, b=(h.lo+(i+1)*h.width)*100;
    g.addEventListener("mousemove",e=>showTip(e,
      `${a.toFixed(1)}–${b.toFixed(1)} cents\n${fmt(c)} books  (${(c/C.n_usable*100).toFixed(1)}%)`));
    g.addEventListener("mouseleave",hideTip);
  });
  const defs = el("defs",{},svg);
  const gr = el("linearGradient",{id:"gr",x1:0,y1:0,x2:0,y2:1},defs);
  el("stop",{offset:"0%","stop-color":"#58a6ff"},gr);
  el("stop",{offset:"100%","stop-color":"#1f4f80"},gr);
  for (let i=0;i<=5;i++){
    const x = PAD.l + iw*(i/5);
    el("text",{x,y:H-8,"text-anchor":"middle",class:"axis"},svg).textContent = i+"c";
  }
}

// ── spread by price level ───────────────────────────────────────────────────
function drawLevels(){
  const svg = $("#levels"); clear(svg);
  const W = svg.clientWidth || 900, H = 210, PAD = {l:46,r:12,t:12,b:34};
  const L = C.price_levels;
  const max = Math.max(...L.map(d=>d.p90)) || 0.01;
  const iw = W-PAD.l-PAD.r, ih = H-PAD.t-PAD.b, bw = iw/L.length;
  for (let i=0;i<=4;i++){
    const y = PAD.t+ih*(i/4);
    el("line",{x1:PAD.l,x2:W-PAD.r,y1:y,y2:y,class:"gl"},svg);
    el("text",{x:PAD.l-8,y:y+3,"text-anchor":"end",class:"axis"},svg)
      .textContent = ((max*(1-i/4))*100).toFixed(1)+"c";
  }
  L.forEach((d,i)=>{
    const x = PAD.l+i*bw;
    const hm = (d.median/max)*ih, hp = (d.p90/max)*ih;
    const g = el("g",{class:"bar"+(state.level===i?" sel":"")},svg);
    el("rect",{x:x+3,y:PAD.t+ih-hp,width:Math.max(1,bw-6),height:Math.max(1,hp),
      fill:"#2d3a4d",rx:2},g);
    el("rect",{x:x+3,y:PAD.t+ih-hm,width:Math.max(1,bw-6),height:Math.max(1,hm),
      fill:state.level===i?"#a371f7":"#58a6ff",rx:2},g);
    el("text",{x:x+bw/2,y:H-18,"text-anchor":"middle",class:"axis"},svg)
      .textContent = d.lo.toFixed(1);
    g.addEventListener("mousemove",e=>showTip(e,
      `price ${d.lo.toFixed(1)}–${d.hi.toFixed(1)}\nmedian ${cents(d.median)}   p90 ${cents(d.p90)}\n${fmt(d.n)} books`));
    g.addEventListener("mouseleave",hideTip);
    g.addEventListener("click",()=>{ state.level = state.level===i?null:i;
      drawLevels(); renderMarkets(); });
  });
  el("text",{x:PAD.l+iw/2,y:H-2,"text-anchor":"middle",class:"axis"},svg)
    .textContent = "YES mid price";
}

// ── timeline ────────────────────────────────────────────────────────────────
function drawTimeline(){
  const svg = $("#timeline"); clear(svg);
  const W = svg.clientWidth || 900, H = 190, PAD = {l:46,r:12,t:12,b:28};
  const T = C.timeline;
  if (!T.length){ el("text",{x:W/2,y:H/2,"text-anchor":"middle",class:"axis"},svg)
    .textContent = "no timeline data"; return; }
  const max = Math.max(...T.map(d=>d.p90)) || 0.01;
  const iw = W-PAD.l-PAD.r, ih = H-PAD.t-PAD.b;
  const X = i => PAD.l + iw*(i/Math.max(1,T.length-1));
  const Y = v => PAD.t + ih*(1 - v/max);
  for (let i=0;i<=4;i++){
    const y = PAD.t+ih*(i/4);
    el("line",{x1:PAD.l,x2:W-PAD.r,y1:y,y2:y,class:"gl"},svg);
    el("text",{x:PAD.l-8,y:y+3,"text-anchor":"end",class:"axis"},svg)
      .textContent = ((max*(1-i/4))*100).toFixed(1)+"c";
  }
  const line = k => T.map((d,i)=>`${i?"L":"M"}${X(i)},${Y(d[k])}`).join(" ");
  el("path",{d:line("p90"),fill:"none",stroke:"#2d3a4d","stroke-width":1.5},svg);
  el("path",{d:line("median"),fill:"none",stroke:"#58a6ff","stroke-width":2},svg);
  T.forEach((d,i)=>{
    const g = el("g",{},svg);
    el("rect",{x:X(i)-iw/T.length/2,y:PAD.t,width:iw/T.length,height:ih,
      fill:"transparent"},g);
    el("circle",{cx:X(i),cy:Y(d.median),r:2.5,fill:"#58a6ff"},g);
    g.addEventListener("mousemove",e=>showTip(e,
      `${d.day}\nmedian ${cents(d.median)}   p90 ${cents(d.p90)}\n${fmt(d.n)} books`));
    g.addEventListener("mouseleave",hideTip);
  });
  [0, T.length-1].forEach(i=>
    el("text",{x:X(i),y:H-8,"text-anchor":i?"end":"start",class:"axis"},svg)
      .textContent = T[i].day.slice(5));
}

// ── market table ────────────────────────────────────────────────────────────
function renderMarkets(){
  const tb = $("#mkt tbody");
  let rows = C.markets.slice();
  if (state.level !== null){
    const L = C.price_levels[state.level];
    rows = rows.filter(m => m.mid >= L.lo && m.mid < L.hi);
  }
  if (state.search){
    const s = state.search.toLowerCase();
    rows = rows.filter(m => m.q.toLowerCase().includes(s) || m.id.toLowerCase().includes(s));
  }
  const {k, dir} = state.sort;
  rows.sort((a,b)=> (typeof a[k]==="string"
    ? a[k].localeCompare(b[k]) : a[k]-b[k]) * dir);

  tb.innerHTML = rows.length ? rows.map(m=>`<tr>
     <td class="q" title="${m.id}">${m.q.replace(/[<>&]/g,"")}</td>
     <td class="num">${fmt(m.n)}</td>
     <td class="num">${m.mid.toFixed(3)}</td>
     <td class="num">${cents(m.spread)}</td>
     <td class="num">${cents(m.spread_p90)}</td>
     <td class="num">${fmt(Math.round(m.depth))}</td></tr>`).join("")
   : `<tr><td colspan="6" class="empty">no markets match</td></tr>`;

  $("#rowCount").textContent = `${rows.length} of ${C.markets.length} markets`;
  const chip = $("#filterChip");
  if (state.level !== null){
    const L = C.price_levels[state.level];
    chip.innerHTML = `<span class="chip" id="clearF">price ${L.lo.toFixed(1)}–${L.hi.toFixed(1)} ✕</span>`;
    $("#clearF").onclick = ()=>{ state.level=null; drawLevels(); renderMarkets(); };
  } else chip.innerHTML = "";

  document.querySelectorAll("#mkt th").forEach(th=>{
    th.className = th.classList.contains("num") ? "num" : "";
    if (th.dataset.k === k) th.classList.add(dir>0?"up":"down");
  });
}

document.querySelectorAll("#mkt th").forEach(th=>{
  th.addEventListener("click", ()=>{
    const k = th.dataset.k;
    state.sort = (state.sort.k===k) ? {k, dir:-state.sort.dir} : {k, dir:-1};
    renderMarkets();
  });
});
$("#search").addEventListener("input", e=>{ state.search = e.target.value; renderMarkets(); });

function drawAll(){ drawHist(); drawLevels(); drawTimeline(); }
addEventListener("resize", drawAll);
drawAll(); renderMarkets();
</script>
</body></html>"""
