"""Pull open markets from both venues. Public endpoints only, no auth."""
from __future__ import annotations
import json, time, urllib.request, urllib.parse

UA = {"User-Agent": "research/1.0"}

def _get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

# ── Kalshi ──────────────────────────────────────────────────────────────────
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

def kalshi_events(max_pages: int = 40) -> list[dict]:
    """Open events with nested markets. Skips auto-generated parlay shards,
    which flood the flat /markets listing with zero-liquidity combinations."""
    out, cursor = [], None
    for _ in range(max_pages):
        u = f"{KALSHI}/events?limit=200&status=open&with_nested_markets=true"
        if cursor:
            u += "&cursor=" + cursor
        try:
            d = _get(u)
        except Exception as e:
            print(f"  kalshi page failed: {e}")
            break
        for e in d.get("events") or []:
            mk = [m for m in (e.get("markets") or [])
                  if "MVECROSS" not in str(m.get("ticker", ""))]
            if mk:
                e["markets"] = mk
                out.append(e)
        cursor = d.get("cursor")
        if not cursor:
            break
        time.sleep(0.15)
    return out

# ── Polymarket ──────────────────────────────────────────────────────────────
GAMMA = "https://gamma-api.polymarket.com"

def polymarket_markets(limit: int = 1200) -> list[dict]:
    """Active, non-closed markets via the Gamma API, which carries the
    question text and resolution description the CLOB endpoint omits."""
    out, offset = [], 0
    while len(out) < limit:
        # Gamma silently caps page size at 100 regardless of the limit asked
        # for, so request exactly that and page with offset.
        u = (f"{GAMMA}/markets?closed=false&active=true&archived=false"
             f"&limit=100&offset={offset}")
        try:
            d = _get(u)
        except Exception as e:
            print(f"  polymarket page failed: {e}")
            break
        if not d:
            break
        out.extend(d)
        offset += 100
        if len(d) < 100:
            break
        time.sleep(0.15)
    return out[:limit]

if __name__ == "__main__":
    k = kalshi_events()
    km = sum(len(e.get("markets") or []) for e in k)
    print(f"kalshi:     {len(k)} events / {km} markets")
    p = polymarket_markets()
    print(f"polymarket: {len(p)} markets")
    json.dump(k, open("kalshi.json", "w"))
    json.dump(p, open("polymarket.json", "w"))
    if p:
        print("\npolymarket sample fields:", sorted(p[0].keys())[:16])
