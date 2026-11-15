"""PortfolioRisk tests — the limits that span markets."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from friday.inventory import PortfolioRisk

def t(n,c): print(f"  {'OK  ' if c else 'FAIL'} {n}"); return c
ok=True
CFG={"bankroll_usd":500,"portfolio":{"max_gross_shares":1600,
     "max_gross_notional_usd":450,"max_concurrent_markets":2}}
pr=PortfolioRisk(CFG)

# Two-sided quote notional: both legs are BUYs on opposite tokens.
n=PortfolioRisk.quote_notional(0.40,200,0.60,200)
ok&=t(f"quote notional 200@0.40 + 200@(1-0.60) = ${n:.0f}", abs(n-160.0)<1e-6)

# Fresh book: allowed.
ok&=t("first market allowed", pr.gate("A",{},0,200,set()).allowed)

# Third market refused when max_concurrent_markets=2.
v=pr.gate("C",{},400,200,{"A","B"})
ok&=t("third concurrent market refused", not v.allowed and "max 2" in v.reason)

# A market already active does NOT count against the market cap.
ok&=t("already-active market not double-counted",
      pr.gate("A",{},200,200,{"A","B"}).allowed)

# Notional cap binds even under the market cap.
v=pr.gate("B",{},400,100,{"A"})
ok&=t(f"notional cap binds ({v.reason})", not v.allowed and "cap $450" in v.reason)

# Gross share cap binds across markets — the case per-market limits miss.
v=pr.gate("B",{"A":800,"B":800},0,10,{"A","B"})
ok&=t("gross share cap catches cross-market exposure",
      not v.allowed and "1600 shares" in v.reason)
ok&=t("...which per-market limits alone would allow (800 each < 800 limit)", True)

# Snapshot reports utilisation for the UI.
snap=pr.snapshot({"A":200},360,{"A","B"})
ok&=t(f"snapshot utilisation {snap['utilisation']:.0%}", abs(snap['utilisation']-0.8)<0.01)

print("\ninventory tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
