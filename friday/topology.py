"""Graph topology: which components exist and how data moves between them.

Derived from the live node registry so the graph reflects what is actually
running, not a hand-drawn diagram that drifts from the code.
"""

from __future__ import annotations

from typing import Any

from friday.events import BUS, CLOB, GUARD, SCANNER, TRACKER

LAYERS = {
    "venue": 0,       # the exchange
    "select": 1,      # scanner
    "signal": 2,      # mark / toxicity
    "decide": 3,      # market maker
    "execute": 4,     # order legs
    "record": 5,      # tracker / guard
}


def build_graph(markets: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = [
        {"id": CLOB, "label": "Polymarket CLOB", "layer": "venue", "kind": "venue"},
        {"id": SCANNER, "label": "Scanner", "layer": "select", "kind": "core",
         "sub": "reward pool / depth"},
        {"id": TRACKER, "label": "Tracker", "layer": "record", "kind": "core",
         "sub": "sqlite + audit"},
        {"id": GUARD, "label": "Guard", "layer": "record", "kind": "core",
         "sub": "halts"},
    ]
    edges = [
        {"src": CLOB, "dst": SCANNER, "kind": "book"},
        {"src": TRACKER, "dst": GUARD, "kind": "check"},
    ]

    state = BUS.nodes()
    for m in markets:
        cid = m["condition_id"]
        short = (m.get("question") or cid)[:22]
        mm, mk, tx = f"mm:{cid}", f"mark:{cid}", f"tox:{cid}"
        bid, ask = f"leg:{cid}:bid", f"leg:{cid}:ask"
        nodes += [
            {"id": mk, "label": "Mark", "layer": "signal", "kind": "signal",
             "market": cid, "sub": "microprice + vol"},
            {"id": tx, "label": "Toxicity", "layer": "signal", "kind": "signal",
             "market": cid, "sub": "one-sided flow"},
            {"id": mm, "label": short, "layer": "decide", "kind": "maker",
             "market": cid, "sub": "market maker"},
            {"id": bid, "label": "bid leg", "layer": "execute", "kind": "leg",
             "market": cid, "sub": "BUY YES"},
            {"id": ask, "label": "ask leg", "layer": "execute", "kind": "leg",
             "market": cid, "sub": "BUY NO"},
        ]
        edges += [
            {"src": CLOB, "dst": mk, "kind": "book"},
            {"src": SCANNER, "dst": mm, "kind": "select"},
            {"src": mk, "dst": mm, "kind": "fair"},
            {"src": tx, "dst": mm, "kind": "tox"},
            {"src": mm, "dst": bid, "kind": "quote"},
            {"src": mm, "dst": ask, "kind": "quote"},
            {"src": bid, "dst": CLOB, "kind": "order"},
            {"src": ask, "dst": CLOB, "kind": "order"},
            {"src": mm, "dst": TRACKER, "kind": "record"},
            {"src": GUARD, "dst": mm, "kind": "halt"},
        ]

    for n in nodes:
        n["state"] = state.get(n["id"], {})
        n["depth"] = LAYERS.get(n["layer"], 0)
    return {"nodes": nodes, "edges": edges, "layers": LAYERS}
