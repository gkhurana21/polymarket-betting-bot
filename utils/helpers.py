"""Pure helper functions. Implements PLAN.md §2 utils/helpers.py.

Phase A scope (YAGNI): config loading, ms-epoch time helpers, value clamp.
Formula helpers (sigmoid, decay, kelly, market_efficiency) listed in PLAN.md
§2 are deferred to Phase B build — their consumers do not exist yet.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Load and return the YAML config as a dict.

    Implements PLAN.md §6 ("config.yaml is the single source of all tunable
    parameters; zero hardcoded values in code"). Caller treats the result as
    read-only — no module mutates the returned dict.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found at {p.resolve()}")
    with p.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"config at {p} did not parse to a dict (got {type(cfg).__name__})")
    return cfg


def now_ms() -> int:
    """Current wall-clock time as integer milliseconds since the Unix epoch.

    Used wherever a timestamp is recorded — see PLAN.md §6 schema columns
    `whale_signals.detected_at_ts`, `market_snapshots.ts`, etc. Uses
    `time.time_ns()` for monotonic-grade precision rather than `time.time()`.
    """
    return time.time_ns() // 1_000_000


def ms_to_iso(ts_ms: int) -> str:
    """Format an ms-epoch timestamp as ISO-8601 UTC for human-readable logs.

    Inverse of `now_ms()` for display purposes only — never round-trip through
    this for storage; always store the raw integer ms value.
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp `value` into the inclusive range [lo, hi].

    Used by `paper_trader.copy_whale` to enforce `min_paper_size_usd` and
    `max_paper_size_usd` per PLAN.md §6 paper_streams.copy_whale and §8
    paper-stream sizing rules.
    """
    if lo > hi:
        raise ValueError(f"clamp: lo ({lo}) must be <= hi ({hi})")
    return max(lo, min(hi, value))
