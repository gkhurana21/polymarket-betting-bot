"""Paper trader. Implements PLAN.md §2 bot/paper_trader.py and the four-stream
fan-out + pending sweep semantics in PLAN.md §8.

Per-signal fan-out
------------------
Each `WhaleSignal` pulled from the whale_tracker queue is processed by
`consume_signal`, which produces between 0 and 4 outputs:

  * `copy_whale`              — immediate `paper_trades` row, side = whale's,
                                entry = whale's own fill price, size = clamped
                                proportional. Acts only on `[open, add]`.
  * `stub_blend`              — immediate `paper_trades` row. Side = the side
                                with higher P_true under the stub blend
                                formula. Entry = latest market_snapshot's mid
                                (or `1 − mid` for NO). Acts on ALL types.
  * `null_random_side`        — immediate `paper_trades` row. Side = 50/50
                                coinflip seeded by `market_id` (deterministic
                                across runs). Entry from latest snapshot. Acts
                                on ALL types.
  * `whale_side_random_timing`— `pending_paper_trades` row with
                                `scheduled_for_ts = now + uniform(0, 24h)`,
                                seeded by `signal_id`. Materialises later on
                                a sweep tick. Acts only on `[open, add]`.

Sweep semantics (`sweep_pending(now_ms)`)
-----------------------------------------
For each pending row with `scheduled_for_ts ≤ now_ms`, the four cases are
checked in this strict order (first match wins):

  1. **Resolved-before-fire** — `tracker.get_resolved_market(market_id)`
     returns a row → write a void `paper_trades` row with
     `resolved_outcome='VOIDED_PRE_FILL'`, `pnl_usd=0`, `entry_price=NULL`;
     mark pending `voided`.
  2. **Missed window** — `now_ms > scheduled_for_ts + miss_tolerance_ms`
     → `VOIDED_MISSED_WINDOW`. Bot was offline / sweep delayed past the
     grace period. Never back-fill at a stale mid.
  3. **Stale snapshot** — latest `book_snapshots` is missing or older than
     `max_snapshot_staleness_ms` → `VOIDED_MISSED_WINDOW`. The timing test
     is meaningless without a fresh mid.
  4. **Healthy materialisation** — write a `paper_trades` row with
     `entry_price = current YES_mid` (or `1 − mid` for NO side), mark
     pending `materialised`.

All void paths write a visible `paper_trades` row with `pnl_usd=0` and
`entry_price=NULL` so win-rate queries (which filter
`resolved_outcome IN ('YES', 'NO')`) naturally exclude voids without
needing a special-cased query.

Concurrency
-----------
`start()` spawns a daemon consumer thread that pulls from the injected
`signal_queue` (typically `whale_tracker.events`) and calls
`consume_signal` for each item. `sweep_pending` is called from the main
loop on its own schedule (every 5 min). Tracker's internal write lock
serialises every DB write across both threads.
"""

from __future__ import annotations

import json
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dataclasses_json import DataClassJsonMixin

from bot.client import ClobReadOnlyClient
from bot.models import PaperTrade, PendingPaperTrade, WhaleSignal
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)


# ─────────────────────────  result dataclasses  ─────────────────────────

@dataclass(frozen=True)
class SweepError(DataClassJsonMixin):
    pending_id: int
    error_type: str
    message: str


@dataclass
class SweepResult(DataClassJsonMixin):
    """One sweep tick's outcome."""
    due: int = 0                          # pending rows whose scheduled_for_ts ≤ now
    voided_pre_fill: int = 0
    voided_missed_window: int = 0
    materialised: int = 0
    errors: int = 0
    error_details: list[SweepError] = field(default_factory=list)


# ────────────────────────────  PaperTrader  ────────────────────────────

class PaperTrader:
    """Fan-out signal consumer + pending materialisation sweep."""

    def __init__(
        self,
        tracker: Tracker,
        client: ClobReadOnlyClient | None,
        config: dict[str, Any],
        signal_queue: queue.Queue[WhaleSignal] | None = None,
    ) -> None:
        self._tracker = tracker
        self._client = client
        self._signal_queue = signal_queue

        # ── per-stream config (PLAN.md §6 paper_streams) ──
        streams = (config.get("paper_streams") or {})
        cw = streams.get("copy_whale") or {}
        self._cw_enabled = bool(cw.get("enabled", True))
        self._cw_scale = float(cw.get("proportional_scale", 0.01))
        self._cw_min = float(cw.get("min_paper_size_usd", 5.0))
        self._cw_max = float(cw.get("max_paper_size_usd", 500.0))
        self._cw_acts = {s.lower() for s in (cw.get("acts_on_signal_types") or ["open", "add"])}

        sb = streams.get("stub_blend") or {}
        self._sb_enabled = bool(sb.get("enabled", True))
        self._sb_alpha = float(sb.get("alpha", 0.5))
        self._sb_beta = float(sb.get("beta", 0.0))           # reserved for future
        self._sb_gamma = float(sb.get("gamma", 0.5))
        self._sb_trust_prior = float(sb.get("trust_prior", 0.7))  # reserved
        self._sb_size_usd = float(sb.get("size_usd", 100.0))
        self._sb_acts = {s.lower() for s in (sb.get("acts_on_signal_types")
                                              or ["open", "add", "exit", "flip"])}

        nr = streams.get("null_random_side") or {}
        self._nr_enabled = bool(nr.get("enabled", True))
        self._nr_size_usd = float(nr.get("size_usd", 100.0))
        self._nr_acts = {s.lower() for s in (nr.get("acts_on_signal_types")
                                              or ["open", "add", "exit", "flip"])}

        wt = streams.get("whale_side_random_timing") or {}
        self._wt_enabled = bool(wt.get("enabled", True))
        self._wt_size_usd = float(wt.get("size_usd", 100.0))
        self._wt_delay_window_seconds = float(wt.get("delay_window_seconds", 86_400.0))
        self._wt_miss_tolerance_ms = int(
            float(wt.get("miss_tolerance_seconds", 1800.0)) * 1000
        )
        self._wt_max_snap_staleness_ms = int(
            float(wt.get("max_snapshot_staleness_seconds", 900.0)) * 1000
        )
        self._wt_acts = {s.lower() for s in (wt.get("acts_on_signal_types")
                                              or ["open", "add"])}

        # ── consume-time snapshot freshness (stub_blend / null_random_side) ──
        ptc = config.get("paper_trader") or {}
        self._consume_snap_max_age_ms = int(
            float(ptc.get("snapshot_max_staleness_seconds", 900.0)) * 1000
        )

        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

        pt_cfg = config.get("paper_trader") or {}
        self._summary_every_n = int(pt_cfg.get("consumer_summary_every_n", 50))
        self._summary_interval_s = float(
            pt_cfg.get("consumer_summary_interval_seconds", 300)
        )
        self._summary_lock = threading.Lock()
        self._since_summary = {"signals": 0, "trades": 0, "pending": 0}
        self._last_summary_mono = time.monotonic()

    # ────────────────────────  consume (per signal)  ────────────────────────

    def consume_signal(self, signal: WhaleSignal) -> list[PaperTrade]:
        """Fan a single signal out to all enabled streams. Returns the
        immediate `PaperTrade` objects (with id populated). The deferred
        `whale_side_random_timing` row goes to `pending_paper_trades`
        instead — visible to tests via DB query, not in this return."""
        if signal.id is None:
            raise ValueError(
                "consume_signal requires a persisted signal (id must be set); "
                "whale_tracker should always pass through record_whale_signal first"
            )

        emitted: list[PaperTrade] = []
        sig_type = (signal.signal_type or "").lower()

        # ── stream 1: copy_whale ──
        if self._cw_enabled and sig_type in self._cw_acts:
            pt = self._build_copy_whale(signal)
            if pt is not None:
                pid = self._tracker.record_paper_trade(pt)
                pt.id = pid
                emitted.append(pt)

        # ── streams 2 + 3: need latest market_snapshot for mid ──
        snap = None
        if (self._sb_enabled and sig_type in self._sb_acts) or (
            self._nr_enabled and sig_type in self._nr_acts
        ):
            snap = self._tracker.get_latest_market_snapshot(signal.market_id)
            if snap is not None:
                age_ms = now_ms() - snap.ts
                if age_ms > self._consume_snap_max_age_ms:
                    _log.debug(
                        "paper_trader: market_snapshot for %s is %d ms old "
                        "(> %d) — stub_blend / null_random_side skipped",
                        signal.market_id[:10], age_ms, self._consume_snap_max_age_ms,
                    )
                    snap = None

        if self._sb_enabled and sig_type in self._sb_acts and snap is not None:
            pt = self._build_stub_blend(signal, snap.mid)
            pid = self._tracker.record_paper_trade(pt)
            pt.id = pid
            emitted.append(pt)

        if self._nr_enabled and sig_type in self._nr_acts and snap is not None:
            pt = self._build_null_random_side(signal, snap.mid)
            pid = self._tracker.record_paper_trade(pt)
            pt.id = pid
            emitted.append(pt)

        # ── stream 4: whale_side_random_timing → pending only ──
        pending_created = 0
        if self._wt_enabled and sig_type in self._wt_acts:
            ppt = self._build_whale_side_random_timing(signal)
            self._tracker.record_pending_paper_trade(ppt)
            pending_created = 1

        _log.debug(
            "paper_consumer: signal id=%s type=%s whale=%s market=%s → "
            "%d immediate trades, %d pending",
            signal.id, signal.signal_type, signal.whale_address[:10],
            signal.market_id[:12], len(emitted), pending_created,
        )
        self._record_consumer_stats(len(emitted), pending_created)
        return emitted

    # ─────────────────────────  stream builders  ─────────────────────────

    def _build_copy_whale(self, signal: WhaleSignal) -> PaperTrade | None:
        """Size = clamp(whale_size × scale, [min, max]). Skip if scaled
        size is non-positive (e.g. whale size was zero — shouldn't happen
        but defensive)."""
        scaled = signal.size_usd * self._cw_scale
        if scaled <= 0:
            return None
        size = max(self._cw_min, min(self._cw_max, scaled))
        return PaperTrade(
            strategy_id="copy_whale",
            triggering_signal_id=signal.id,                      # type: ignore[arg-type]
            market_id=signal.market_id,
            side=signal.side,                                    # mirror whale
            size_usd=size,
            decided_at_ts=now_ms(),
            entry_price=signal.price,                            # whale's own fill
        )

    def _build_stub_blend(self, signal: WhaleSignal, mid: float) -> PaperTrade:
        """Stub formula: P_true = α·P_whale + γ·P_market. β is configured 0.
        Choose the side with P_true > 0.5; entry = mid (YES) or 1−mid (NO).
        """
        p_whale_yes = 1.0 if signal.side == "YES" else 0.0
        p_true_yes = self._sb_alpha * p_whale_yes + self._sb_gamma * mid
        side = "YES" if p_true_yes > 0.5 else "NO"
        entry = mid if side == "YES" else (1.0 - mid)
        return PaperTrade(
            strategy_id="stub_blend",
            triggering_signal_id=signal.id,                      # type: ignore[arg-type]
            market_id=signal.market_id,
            side=side,
            size_usd=self._sb_size_usd,
            decided_at_ts=now_ms(),
            entry_price=entry,
        )

    def _build_null_random_side(
        self, signal: WhaleSignal, mid: float
    ) -> PaperTrade:
        """Deterministic 50/50 seeded by market_id. Critical: seed is
        market_id (not signal_id) so re-runs of the bot produce the same
        null-side choice per market — calibration reproducibility relies
        on this. `random.Random(str)` uses SHA-512 internally, so it's
        deterministic across processes regardless of PYTHONHASHSEED."""
        rng = random.Random(signal.market_id)
        side = "YES" if rng.random() < 0.5 else "NO"
        entry = mid if side == "YES" else (1.0 - mid)
        return PaperTrade(
            strategy_id="null_random_side",
            triggering_signal_id=signal.id,                      # type: ignore[arg-type]
            market_id=signal.market_id,
            side=side,
            size_usd=self._nr_size_usd,
            decided_at_ts=now_ms(),
            entry_price=entry,
        )

    def _build_whale_side_random_timing(
        self, signal: WhaleSignal
    ) -> PendingPaperTrade:
        """Random delay ∈ [0, delay_window_seconds] seeded by signal_id."""
        rng = random.Random(signal.id)
        delay_seconds = rng.uniform(0.0, self._wt_delay_window_seconds)
        ts = now_ms()
        return PendingPaperTrade(
            strategy_id="whale_side_random_timing",
            triggering_signal_id=signal.id,                      # type: ignore[arg-type]
            market_id=signal.market_id,
            side=signal.side,                                    # mirror whale
            size_usd=self._wt_size_usd,
            scheduled_for_ts=ts + int(delay_seconds * 1000),
            status="pending",
            created_at_ts=ts,
        )

    # ─────────────────────────────  sweep  ─────────────────────────────

    def sweep_pending(self, now_ms_value: int | None = None) -> SweepResult:
        """One sweep tick. Four cases in strict order: resolved → missed →
        stale → healthy. First match wins (resolved-before-fire takes
        precedence over both missed-window and stale-snapshot)."""
        if now_ms_value is None:
            now_ms_value = now_ms()

        result = SweepResult()
        due = self._tracker.get_pending_paper_trades_due(now_ms_value)
        result.due = len(due)

        for p in due:
            try:
                action = self._process_pending(p, now_ms_value)
            except Exception as e:
                _log.error("paper_trader: sweep failed for pending id=%s: %s",
                           p.id, e, exc_info=True)
                result.errors += 1
                result.error_details.append(
                    SweepError(int(p.id or -1), type(e).__name__, str(e)[:200])
                )
                continue
            if action == "VOIDED_PRE_FILL":
                result.voided_pre_fill += 1
            elif action == "VOIDED_MISSED_WINDOW":
                result.voided_missed_window += 1
            elif action == "MATERIALISED":
                result.materialised += 1
        return result

    def _process_pending(self, p: PendingPaperTrade, now_ms_value: int) -> str:
        """Strict precedence: resolved > missed > stale > healthy."""
        # Case 1: resolved before fire.
        if self._tracker.get_resolved_market(p.market_id) is not None:
            self._tracker.void_pending_paper_trade(int(p.id), "VOIDED_PRE_FILL")  # type: ignore[arg-type]
            return "VOIDED_PRE_FILL"

        # Case 2: missed window.
        if now_ms_value > p.scheduled_for_ts + self._wt_miss_tolerance_ms:
            self._tracker.void_pending_paper_trade(int(p.id), "VOIDED_MISSED_WINDOW")  # type: ignore[arg-type]
            return "VOIDED_MISSED_WINDOW"

        # Case 3: stale snapshot — we won't materialise at a stale mid.
        snap = self._tracker.get_latest_book_snapshot(p.market_id)
        if snap is None or (now_ms_value - snap.ts) > self._wt_max_snap_staleness_ms:
            self._tracker.void_pending_paper_trade(int(p.id), "VOIDED_MISSED_WINDOW")  # type: ignore[arg-type]
            return "VOIDED_MISSED_WINDOW"

        # Case 4: healthy materialisation.
        mid = self._compute_mid_from_book_json(snap.book_json)
        if mid is None:
            # one-sided book → indistinguishable from stale for our purposes
            self._tracker.void_pending_paper_trade(int(p.id), "VOIDED_MISSED_WINDOW")  # type: ignore[arg-type]
            return "VOIDED_MISSED_WINDOW"

        entry = mid if p.side == "YES" else (1.0 - mid)
        self._tracker.materialise_pending_paper_trade(int(p.id), entry)  # type: ignore[arg-type]
        return "MATERIALISED"

    @staticmethod
    def _compute_mid_from_book_json(book_json: str) -> float | None:
        """Parse a stored book snapshot and return (best_bid + best_ask)/2.
        Returns None if either side is empty or parsing fails."""
        try:
            book = json.loads(book_json)
        except (TypeError, ValueError):
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = max(float(b[0]) for b in bids)
            best_ask = min(float(a[0]) for a in asks)
        except (IndexError, TypeError, ValueError):
            return None
        return (best_bid + best_ask) / 2.0

    # ─────────────────────────  consumer thread  ─────────────────────────

    def start(self) -> None:
        """Spawn the daemon consumer thread (no-op if already running)."""
        if self._signal_queue is None:
            raise RuntimeError(
                "PaperTrader.start: signal_queue not provided to __init__"
            )
        if self._thread is not None and self._thread.is_alive():
            _log.debug("paper_trader.start: already running")
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._consume_loop, daemon=True, name="paper-trader"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and join."""
        if self._stop_event is not None:
            self._stop_event.set()
        self._maybe_emit_consumer_summary()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                _log.warning(
                    "paper_trader thread did not stop within %.1fs", timeout
                )
        self._thread = None
        self._stop_event = None

    def _record_consumer_stats(self, trades: int, pending: int) -> None:
        with self._summary_lock:
            self._since_summary["signals"] += 1
            self._since_summary["trades"] += trades
            self._since_summary["pending"] += pending
            should_log = (
                self._since_summary["signals"] >= self._summary_every_n
                or time.monotonic() - self._last_summary_mono >= self._summary_interval_s
            )
            if should_log:
                _log.info(
                    "paper_consumer summary — %d signals consumed, "
                    "%d paper trades created, %d pending",
                    self._since_summary["signals"],
                    self._since_summary["trades"],
                    self._since_summary["pending"],
                )
                self._since_summary = {"signals": 0, "trades": 0, "pending": 0}
                self._last_summary_mono = time.monotonic()

    def _consume_loop(self) -> None:
        """Thread target. Pulls signals from the queue and dispatches."""
        assert self._signal_queue is not None
        while not self._stop_event.is_set():
            try:
                sig = self._signal_queue.get(timeout=1.0)
            except queue.Empty:
                self._maybe_emit_consumer_summary(force_interval_only=True)
                continue
            try:
                self.consume_signal(sig)
            except Exception as e:
                _log.error(
                    "paper_trader: consume_signal failed for sig id=%s: %s",
                    sig.id, e, exc_info=True,
                )

    def _maybe_emit_consumer_summary(self, *, force_interval_only: bool = False) -> None:
        with self._summary_lock:
            if self._since_summary["signals"] == 0:
                return
            if force_interval_only:
                if time.monotonic() - self._last_summary_mono < self._summary_interval_s:
                    return
            _log.info(
                "paper_consumer summary — %d signals consumed, "
                "%d paper trades created, %d pending",
                self._since_summary["signals"],
                self._since_summary["trades"],
                self._since_summary["pending"],
            )
            self._since_summary = {"signals": 0, "trades": 0, "pending": 0}
            self._last_summary_mono = time.monotonic()
