"""Exhaustive neg-risk groups, with COMPLETE membership verified.

The previous version grouped by negRiskMarketID across the open-markets
listing and reported a +62% "arbitrage" on the Nobel Peace Prize group. It had
summed 20 of the event's 71 legs. Buying a SUBSET of an exhaustive set
guarantees nothing -- the winner is probably one of the legs you skipped, and
the payout is zero.

The exact-score test was immune to this because its payout bound (at least n-1
of n legs pay) holds for any subset. This test has no such protection: it
relies on the set being COMPLETE, so completeness must be verified, not
assumed.

Groups are therefore built from /events, which knows true membership, and any
event whose fetched leg count disagrees with its market list is DISCARDED
rather than scored.
"""
import json, urllib.request, time, collections
def get(u,t=25):
    try:
        with urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"r/1.0"}),timeout=t) as r:
            return json.loads(r.read())
    except Exception: return None

rows=[];off=0
while off<8000:
    d=get(f"https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset={off}")
    if not d: break
    rows.extend(d)
    if len(d)<100: break
    off+=100; time.sleep(0.1)
print(f"open markets: {len(rows):,}", flush=True)

slugs=set()
for r in rows:
    if not (r.get('negRiskMarketID') or r.get('negRiskRequestID')): continue
    for e in (r.get('events') or []):
        if e.get('slug'): slugs.add(e['slug'])
print(f"neg-risk event slugs: {len(slugs):,}", flush=True)

FEE=0.05
def best_ask(tok):
    b=get(f"https://clob.polymarket.com/book?token_id={tok}")
    if not b: return None
    out=[]
    for x in (b.get('asks') or []):
        try:
            out.append((float(x['price']),float(x['size'])) if isinstance(x,dict)
                       else (float(x[0]),float(x[1])))
        except Exception: pass
    return min(out) if out else None

results=[]; skipped=0
for i,slug in enumerate(sorted(slugs),1):   # all events, not the first 70
    ev=get(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not (isinstance(ev,list) and ev): skipped+=1; continue
    mks=ev[0].get('markets') or []
    live=[m for m in mks if not m.get('closed') and m.get('clobTokenIds')]
    # COMPLETENESS GATE: every live leg must price, or we do not score the set
    if len(live)<3: skipped+=1; continue
    tot=fee=0.0; sizes=[]; ok=True
    for m in live:
        try: tok=json.loads(m['clobTokenIds'])[0]
        except Exception: ok=False; break
        a=best_ask(tok); time.sleep(0.05)
        if a is None: ok=False; break
        p,s=a
        tot+=p; fee+=FEE*p*(1-p); sizes.append(s)
    if not ok:
        skipped+=1; continue                # incomplete -> never scored
    results.append((tot,fee,len(live),len(mks),min(sizes),ev[0].get('title','')[:46]))
    if i%15==0: print(f"  {i}/{min(len(slugs),70)} events", flush=True)

results.sort()
print(f"\n{'sum ask':>9} {'fees':>8} {'net':>9} {'live':>5} {'total':>6} {'shares':>8}  event")
for tot,fee,nl,nt,sz,title in results[:18]:
    print(f"{tot:>9.4f} {fee:>8.4f} {1-tot-fee:>+9.4f} {nl:>5} {nt:>6} {sz:>8.0f}  {title}")
print(f"\nevents scored (complete sets) : {len(results)}")
print(f"events skipped (incomplete)   : {skipped}")
print(f"sum below $1.00               : {sum(1 for r in results if r[0]<1.0)}")
print(f"PROFITABLE after per-leg fees : {sum(1 for r in results if 1-r[0]-r[1]>0)}")
print("DONE")
