"""Stress-test the thin-market liquidity result. RESEARCH ONLY, no orders.

`thin_markout.py` found positive maker markout (+0.179c) and a headline of
+0.979c net per fill. That headline was not trustworthy as stated: it paired a
median spread taken from ONE sample of markets with markout from a DIFFERENT
sample. This re-measures each market's own spread against its own markout, and
attacks the result three ways:

  1. PAIRED per-market net, so no pooled average can be carried by outliers.
     -> 81/83 markets positive, median +0.100c (not +0.979c).

  2. ONE-SIDED FLOW. A maker earns half the spread only if filled on BOTH
     sides; filled once, you hold inventory and a directional bet instead.
     -> imbalance median 38.9%, p90 100%. Only 30/83 round-trippable.
     This is the load-bearing assumption and it fails in 64% of markets.

  3. CAPTURE RATE swept across 5/10/20% rather than assuming one number, so
     the dependence on a quantity we do not know is visible.
     -> $1.37 per market-day median at 10%.

A positive result deserves harder scrutiny than a negative one: a wrong
negative costs an opportunity, a wrong positive costs money.
"""
import json, urllib.request, statistics, time
def get(u,t=25):
    try:
        with urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"r/1.0"}),timeout=t) as r:
            return json.loads(r.read())
    except Exception: return None
def f(v):
    try: return float(v)
    except: return None
def lvl(a):
    return (float(a["price"]),float(a["size"])) if isinstance(a,dict) else (float(a[0]),float(a[1]))

uni=json.load(open('/tmp/universe.json'))
thin=[r for r in uni if r.get('clobTokenIds') and 200<=(f(r.get('volume24hr')) or 0)<10000]
thin.sort(key=lambda r:-(f(r.get('volume24hr')) or 0))

per_market=[]
for m in thin[:90]:
    try: tok=json.loads(m['clobTokenIds'])[0]
    except Exception: continue
    bk=get(f"https://clob.polymarket.com/book?token_id={tok}"); time.sleep(0.1)
    if not bk: continue
    bids=bk.get('bids') or []; asks=bk.get('asks') or []
    if not bids or not asks: continue
    bb=max(lvl(a)[0] for a in bids); ba=min(lvl(a)[0] for a in asks)
    if not (0<bb<ba<1): continue
    spread=ba-bb
    tr=get(f"https://data-api.polymarket.com/trades?market={m.get('conditionId')}&limit=500"); time.sleep(0.1)
    h=get(f"https://clob.polymarket.com/prices-history?market={tok}&interval=1m&fidelity=60"); time.sleep(0.1)
    if not isinstance(tr,list) or not tr: continue
    ser=(h or {}).get('history') or []
    if len(ser)<24: continue
    ser.sort(key=lambda p:p.get('t',0))
    mks=[]; buys=0; sells=0
    for t in tr:
        try:
            p=float(t['price']); ts=int(t['timestamp']); side=str(t['side']).upper()
        except Exception: continue
        if str(t.get('asset') or '')!=str(tok): continue
        if not (0<p<1): continue
        if side=='BUY': buys+=1
        else: sells+=1
        fut=None
        for q in ser:
            if q.get('t',0)>=ts+3600:
                if q['t']-ts<=3*3600: fut=float(q['p'])
                break
        if fut is None: continue
        mks.append((p-fut) if side=='BUY' else (fut-p))
    if len(mks)<20: continue
    tot=buys+sells
    per_market.append(dict(q=m.get('question','')[:44], spread=spread,
        markout=statistics.fmean(mks), n=len(mks),
        imbalance=abs(buys-sells)/max(tot,1), vol=f(m.get('volume24hr')) or 0))

print(f"{len(per_market)} markets with measured spread AND >=20 markout points\n")

print("(1) PAIRED per-market: does the market's OWN spread cover its OWN markout?")
nets=[]
for r in per_market:
    net = 0.5*r['spread'] + r['markout']
    nets.append(net); r['net']=net
nets.sort()
print(f"    net per fill across markets (c):")
for lab,q in (("p10",.10),("p25",.25),("median",.5),("p75",.75),("p90",.90)):
    print(f"       {lab:>7} {nets[int(len(nets)*q)]*100:>+8.3f}c")
pos=sum(1 for n in nets if n>0)
print(f"    markets with positive net: {pos}/{len(nets)} ({pos/len(nets)*100:.0f}%)")

print("\n(2) ONE-SIDED FLOW: half-spread is only earned if you trade BOTH sides.")
print("    If flow is one-directional you accumulate inventory instead.")
imb=sorted(r['imbalance'] for r in per_market)
print(f"    buy/sell imbalance |buys-sells|/total:")
for lab,q in (("median",.5),("p75",.75),("p90",.90)):
    print(f"       {lab:>7} {imb[int(len(imb)*q)]*100:>7.1f}%")
bal=[r for r in per_market if r['imbalance']<0.20]
print(f"    markets with <20% imbalance (round-trippable): {len(bal)}/{len(per_market)}")
if bal:
    bn=[r['net'] for r in bal]
    print(f"    their net per fill: median {statistics.median(bn)*100:+.3f}c")

print("\n(3) REALISTIC INCOME: net/fill x fills you could actually capture.")
for cap in (0.05, 0.10, 0.20):
    daily=[]
    for r in per_market:
        if r['net']<=0: continue
        shares_day = r['vol']/0.5
        daily.append(r['net']*shares_day*cap)
    if daily:
        daily.sort()
        print(f"    at {cap*100:>2.0f}% of flow captured: median ${daily[len(daily)//2]:.2f}/market-day, "
              f"total ${sum(daily):.2f}/day across all")
json.dump(per_market, open('/tmp/permarket.json','w'))
