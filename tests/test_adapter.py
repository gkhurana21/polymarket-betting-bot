"""Adapter tests. Queue position is what earns the reward, so the no-op path
matters more than the place path."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.adapter import PolymarketAdapter
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY

def t(name, cond):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")
    return cond

ok = True
a = PolymarketAdapter(client=None, dry_run=True)

# Identical re-quote must NOT touch the resting order.
r1 = a.sync_quote("bid", "tok1", 0.490, 200)
r2 = a.sync_quote("bid", "tok1", 0.490, 200)
r3 = a.sync_quote("bid", "tok1", 0.490, 200)
ok &= t("first sync places", r1 == "placed")
ok &= t("identical re-quote is a no-op (queue preserved)", r2 == "noop" and r3 == "noop")
ok &= t("no-ops counted, nothing replaced", a.stats()["noops"] == 2 and a.stats()["replaced"] == 0)

# A real price change must replace.
r4 = a.sync_quote("bid", "tok1", 0.489, 200)
ok &= t("price change triggers replace", r4 == "replaced" and a.stats()["replaced"] == 1)

# Size change must replace too (resting size drives reward accrual).
r5 = a.sync_quote("bid", "tok1", 0.489, 400)
ok &= t("size change triggers replace", r5 == "replaced")

# Zero size cancels, and cancelling twice is a no-op not an error.
r6 = a.sync_quote("bid", "tok1", 0.489, 0)
r7 = a.sync_quote("bid", "tok1", 0.489, 0)
ok &= t("zero size cancels", r6 == "cancelled" and a.resting("bid") is None)
ok &= t("cancel when already flat is a no-op", r7 == "noop")

# The two legs are tracked independently.
a.sync_quote("bid", "yes_tok", 0.49, 200)
a.sync_quote("ask", "no_tok", 0.49, 200)
ok &= t("both legs rest independently", a.resting("bid") is not None and a.resting("ask") is not None)
ok &= t("cancel_all clears both", a.cancel_all() == 2 and a.stats()["resting"] == 0)

# The YES-ask -> NO-buy conversion.
ok &= t("ask 0.51 maps to NO buy at 0.49", abs(PolymarketAdapter.ask_as_no_buy(0.51) - 0.49) < 1e-9)

# OrderArgs contract: side is BUY/SELL and size is SHARES, never USD.
# (Both were real bugs in the predecessor project.)
args = OrderArgs(token_id="t", price=0.49, size=200.0, side=BUY)
ok &= t("OrderArgs side is BUY constant, not 'YES'", args.side == "BUY")
ok &= t("OrderArgs size is share count", args.size == 200.0)
ok &= t("OrderArgs has no order_type kwarg", not hasattr(args, "order_type"))

print("\nadapter tests:", "OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
