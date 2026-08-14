"""In-process event bus feeding the live architecture graph.

Every edge drawn in the UI corresponds to a real message between real
components — this is instrumentation, not decoration. If an edge never
pulses, that path genuinely never fired.

Thread-safe ring buffer with monotonic sequence numbers so the UI can poll
incrementally (`?since=N`) instead of re-fetching the world.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

# Node ids are stable strings. Global singletons plus per-market instances.
CLOB = "clob"
SCANNER = "scanner"
GUARD = "guard"
TRACKER = "tracker"


def mm_node(cid: str) -> str:      return f"mm:{cid}"
def mark_node(cid: str) -> str:    return f"mark:{cid}"
def tox_node(cid: str) -> str:     return f"tox:{cid}"
def leg_node(cid: str, s: str) -> str: return f"leg:{cid}:{s}"


class EventBus:
    def __init__(self, maxlen: int = 4000) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._nodes: dict[str, dict[str, Any]] = {}

    def emit(self, src: str, dst: str, kind: str, **payload: Any) -> None:
        with self._lock:
            self._seq += 1
            self._buf.append({
                "seq": self._seq, "ts": time.time(),
                "src": src, "dst": dst, "kind": kind, "payload": payload,
            })

    def node_state(self, node: str, **state: Any) -> None:
        """Latest known state for a node (drives colour/size in the graph)."""
        with self._lock:
            self._nodes.setdefault(node, {}).update(state)

    def drop_market(self, cid: str) -> None:
        """Forget a market's nodes when it leaves the selection."""
        with self._lock:
            for n in list(self._nodes):
                if n.endswith(cid) or f":{cid}" in n:
                    self._nodes.pop(n, None)

    def since(self, seq: int, limit: int = 500) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            out = [e for e in self._buf if e["seq"] > seq][-limit:]
            return self._seq, out

    def nodes(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._nodes.items()}


BUS = EventBus()
