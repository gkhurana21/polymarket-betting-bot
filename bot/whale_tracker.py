"""Whale tracker. Implements PLAN.md §2 bot/whale_tracker.py and the
signal_type classifier in PLAN.md §7.

Architecture
------------
`AbstractWhaleTracker` defines the seam for the future websocket variant.
`PollingWhaleTracker` is the Phase A concrete implementation: fetches each
tracked wallet's recent fills via `client.get_trades_for_wallet` on a
configurable interval, classifies each new trade against an in-memory
position cache, persists via `Tracker.record_whale_signal`, and emits a
`WhaleSignal` onto a bounded queue for the paper_trader to consume.

Position-state replay (init-time)
---------------------------------
`__init__` runs synchronously before the tracker becomes usable. For every
wallet in the tracked universe it calls
`Tracker.get_whale_signals_for_wallet(wallet)` (chronological asc) and
replays each signal into the position cache:

    cache[(wallet, market_id)] = {"yes_shares": float, "no_shares": float}

  * `signal_type ∈ {open, add, flip}` adds shares to the signal's side.
  * `signal_type == "exit"` subtracts shares.

Cold-start whales (no prior signals in DB) start with empty position state;
their first observed trade classifies as `open` by construction. This
intentionally ignores any history older than what we have on disk — true
historical positions for newly-discovered whales are unrecoverable from
the public API. Acceptable Phase A imperfection; documented for Phase B.

Floating-point residuals
------------------------
After a complete exit, replay-derived shares can carry sub-1e-6 residual
due to per-trade FP rounding. The classifier treats `|shares| < 1e-6` as
zero so the next BUY on a fully-exited side classifies as `open`, not `add`.

Dedup (two layers)
------------------
1. In-memory 5-minute window keyed on `(wallet, market_id, outcome, side)`.
   Drops repeat signals during the same trade event without DB roundtrips.
2. DB `UNIQUE(transaction_hash)` on `whale_signals`. `record_whale_signal`
   uses `ON CONFLICT(transaction_hash) DO NOTHING`; returns `None` on dup
   and the tracker skips emission. Survives process restarts.

Read-only enforcement
---------------------
At `__init__`, asserts `client._sdk.get_address() is None` — same runtime
introspection pattern as module 4. The whale tracker must never receive a
signing-capable client.

Threading
---------
`start()` spawns a daemon `Thread(name="whale-tracker")` that loops
`poll_once()` with `stop_event.wait(interval)` for interruptible sleep.
`stop()` signals the event and joins with timeout. `poll_once()` is
sync-callable from tests without involving the thread.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from bot.client import CircuitBreakerOpen, ClobReadOnlyClient
from bot.models import WhaleSignal
from bot.tracker import Tracker
from utils.helpers import now_ms
from utils.logger import get_logger

_log = get_logger(__name__)

# Treat |shares| < this as zero — handles FP residuals from per-trade rounding.
_SHARES_TOLERANCE = 1e-6


@dataclass(frozen=True)
class PollCycleResult:
    """Summary of one whale_tracker poll_once() round."""

    emitted: int
    skipped_stale: int
    elapsed_ms: int


# ════════════════════════════  classifier (pure)  ════════════════════════════

def classify_signal_type(
    *,
    yes_shares_prior: float,
    no_shares_prior: float,
    trade_outcome: str,
    trade_side: str,
    trade_shares: float,
) -> str:
    """Implements PLAN.md §7. Returns one of {open, add, exit, flip}.

    `trade_outcome ∈ {Yes, No, ...}` (case-insensitive)
    `trade_side    ∈ {BUY, SELL}` (case-insensitive)
    `trade_shares` is the absolute share count of the new trade.
    """
    # FP residual handling: treat near-zero priors as zero.
    yes_p = 0.0 if abs(yes_shares_prior) < _SHARES_TOLERANCE else yes_shares_prior
    no_p = 0.0 if abs(no_shares_prior) < _SHARES_TOLERANCE else no_shares_prior

    side_u = (trade_side or "").upper()
    outcome_l = (trade_outcome or "").strip().lower()

    has_prior = yes_p > 0 or no_p > 0
    if not has_prior:
        return "open"

    if side_u == "SELL":
        # Selling shares of `trade_outcome` reduces that side's holdings.
        return "exit"

    # BUY case
    if outcome_l == "yes":
        if yes_p > 0:
            return "add"
        # No YES prior, only NO. Buying YES.
        if trade_shares > no_p:
            return "flip"
        return "add"
    if outcome_l == "no":
        if no_p > 0:
            return "add"
        if trade_shares > yes_p:
            return "flip"
        return "add"
    # Unknown outcome label (sports etc.) — treat as new exposure.
    return "open"


# ════════════════════════════  abstract base  ════════════════════════════

class AbstractWhaleTracker(ABC):
    """Seam for whale-event sources. Phase A: PollingWhaleTracker.
    Phase B (or later): WebsocketWhaleTracker, etc."""

    @abstractmethod
    def stream_events(self) -> Iterator[WhaleSignal]:
        """Yield WhaleSignal objects as they arrive."""

    @abstractmethod
    def start(self) -> None:
        """Begin pushing events into the stream."""

    @abstractmethod
    def stop(self) -> None:
        """Signal a clean shutdown and join any background workers."""


# ════════════════════════════  polling impl  ════════════════════════════

class PollingWhaleTracker(AbstractWhaleTracker):
    """Concrete Phase A implementation. See module docstring."""

    def __init__(
        self,
        client: ClobReadOnlyClient,
        tracker: Tracker,
        config: dict[str, Any],
        whale_universe: list[dict[str, Any]] | None = None,
    ) -> None:
        # ── read-only enforcement (module-4 pattern) ──
        if client is not None:
            try:
                sdk_addr = client._sdk.get_address()
            except Exception:
                sdk_addr = None
            if sdk_addr is not None:
                raise RuntimeError(
                    f"PollingWhaleTracker requires a read-only client; "
                    f"got SDK with signer address={sdk_addr!r}"
                )

        self._client = client
        self._tracker = tracker

        cfg_int = config.get("intervals_seconds") or {}
        self._poll_interval_seconds = float(cfg_int.get("whale_poll", 60.0))

        cfg_wt = config.get("whale_tracker") or {}
        self._queue_max = int(cfg_wt.get("queue_max", 10000))
        self._dedup_window_ms = int(cfg_wt.get("dedup_window_ms", 5 * 60 * 1000))
        self._page_size = int(cfg_wt.get("page_size", 100))
        self._max_pages = int(cfg_wt.get("max_pages_per_wallet", 20))
        self._latency_window_size = int(cfg_wt.get("latency_window", 1000))
        self._latency_log_every = int(cfg_wt.get("latency_log_every_polls", 5))
        # Stale-signal guard: detection_latency_ms above this threshold means
        # we recorded the trade to DB but skip enqueuing it. Prevents the
        # cold-start case (new whale's 100 most recent trades stretch back
        # months) from flooding paper_trader with stale would-have-paper-trades.
        self._max_signal_age_ms = int(cfg_wt.get("max_signal_age_ms", 60_000))

        self._whale_universe: list[dict[str, Any]] = list(whale_universe or [])

        # Per-(wallet, market_id) live position state.
        self._position_cache: dict[tuple[str, str], dict[str, float]] = {}
        # 5-min in-memory dedup. key → last_seen_trade_ts_ms.
        self._dedup: dict[tuple[str, str, str, str], int] = {}
        # Per-wallet last seen trade ts (ms). Seeded from DB; used to skip stale fetches.
        self._last_seen_ts: dict[str, int] = {}
        # Bounded ring of detection latencies (ms) for percentile logging.
        self._recent_latencies: deque[int] = deque(maxlen=self._latency_window_size)
        self._poll_count = 0

        self._queue: queue.Queue[WhaleSignal] = queue.Queue(maxsize=self._queue_max)

        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

        # Init: replay positions + seed last_seen_ts. Both read-only DB ops.
        self._replay_positions()
        self._seed_last_seen_ts()

    # ───────────────────  init helpers (synchronous)  ───────────────────

    def _replay_positions(self) -> None:
        """Rebuild position cache from on-disk whale_signals history."""
        for w in self._whale_universe:
            wallet = w["address"]
            signals = self._tracker.get_whale_signals_for_wallet(wallet)
            prev_ts = 0
            for s in signals:
                # Defensive: warn but process if DB returned out-of-order rows.
                if s.whale_fill_ts < prev_ts:
                    _log.warning(
                        "whale_tracker: out-of-order whale_signals for %s "
                        "(ts %d after %d); replaying anyway",
                        wallet, s.whale_fill_ts, prev_ts,
                    )
                prev_ts = max(prev_ts, s.whale_fill_ts)
                self._apply_signal_to_cache(s)

    def _apply_signal_to_cache(self, signal: WhaleSignal) -> None:
        """Mutate position cache from one historical or freshly-emitted signal."""
        if signal.price <= 0:
            return                                 # divide-by-zero guard
        key = (signal.whale_address, signal.market_id)
        slot = self._position_cache.setdefault(key, {"yes_shares": 0.0, "no_shares": 0.0})
        shares = signal.size_usd / signal.price
        sign = -1.0 if signal.signal_type == "exit" else 1.0
        if signal.side.upper() == "YES":
            slot["yes_shares"] += sign * shares
        elif signal.side.upper() == "NO":
            slot["no_shares"] += sign * shares

    def _seed_last_seen_ts(self) -> None:
        """For each tracked wallet, last_seen_ts = MAX(whale_fill_ts) from DB."""
        for w in self._whale_universe:
            wallet = w["address"]
            row = self._tracker._conn.execute(
                "SELECT MAX(whale_fill_ts) AS m FROM whale_signals WHERE whale_address=?",
                (wallet,),
            ).fetchone()
            self._last_seen_ts[wallet] = int(row["m"]) if row["m"] is not None else 0

    # ───────────────────────  public ABC interface  ───────────────────────

    @property
    def events(self) -> queue.Queue[WhaleSignal]:
        """Direct queue access for paper_trader and tests."""
        return self._queue

    def stream_events(self, timeout: float = 1.0) -> Iterator[WhaleSignal]:
        """Yield signals as they arrive. Blocks up to `timeout` per get;
        returns when stop() is called and the queue has been drained."""
        while True:
            if (
                self._stop_event is not None
                and self._stop_event.is_set()
                and self._queue.empty()
            ):
                return
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                continue

    def start(self) -> None:
        """Spawn the daemon poll thread. No-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            _log.debug("whale_tracker.start: already running")
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="whale-tracker"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and join. Logs warning if join times out."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                _log.warning(
                    "whale_tracker thread did not stop within %.1fs", timeout
                )
        self._thread = None
        self._stop_event = None

    # ───────────────────────────  poll loop  ───────────────────────────

    def _run_loop(self) -> None:
        """Thread target."""
        while not self._stop_event.is_set():
            try:
                result = self.poll_once()
                _log.info(
                    "whale_poll cycle complete — %d signals emitted, "
                    "%d skipped (stale), %dms",
                    result.emitted, result.skipped_stale, result.elapsed_ms,
                )
            except Exception as e:
                _log.error("whale_tracker poll failed: %s", e, exc_info=True)
            self._stop_event.wait(timeout=self._poll_interval_seconds)

    def poll_once(self) -> PollCycleResult:
        """One synchronous polling round across all tracked wallets."""
        t0 = time.perf_counter()
        self._poll_count += 1
        emitted = 0
        skipped_stale = 0
        for w in self._whale_universe:
            try:
                e, s = self._poll_one_wallet(w)
                emitted += e
                skipped_stale += s
            except CircuitBreakerOpen as e:
                _log.warning(
                    "whale_tracker: CB OPEN polling %s; aborting this poll round",
                    w.get("address"),
                )
                _log.debug("CB detail: %s", e)
                break
            except Exception as e:
                _log.warning(
                    "whale_tracker: poll for %s failed: %s",
                    w.get("address"), e,
                )
        if self._poll_count % self._latency_log_every == 0 and self._recent_latencies:
            self._log_latency_percentiles()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return PollCycleResult(
            emitted=emitted, skipped_stale=skipped_stale, elapsed_ms=elapsed_ms,
        )

    def _poll_one_wallet(self, whale: dict[str, Any]) -> tuple[int, int]:
        """Returns (emitted_to_queue, skipped_stale)."""
        wallet = whale["address"]
        last_seen = self._last_seen_ts.get(wallet, 0)
        new_trades = self._fetch_new_trades(wallet, last_seen)
        if not new_trades:
            return 0, 0
        # API returns newest-first; replay oldest-to-newest so position state
        # evolves correctly across a sequence of trades within one poll.
        new_trades.sort(key=lambda t: int(t.get("timestamp", 0)))

        emitted = 0
        skipped_stale = 0
        max_seen = last_seen
        for trade in new_trades:
            sig, was_stale = self._build_and_emit(whale, trade)
            ts = int(trade.get("timestamp", 0))
            if ts > max_seen:
                max_seen = ts
            if was_stale:
                skipped_stale += 1
            elif sig is not None:
                emitted += 1
        self._last_seen_ts[wallet] = max_seen
        return emitted, skipped_stale

    def _fetch_new_trades(self, wallet: str, after_ts: int) -> list[dict[str, Any]]:
        """Page through trades, stopping when we hit a page where some rows
        are older than `after_ts` (deeper pages are guaranteed older — API
        returns newest-first)."""
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self._max_pages):
            page = self._client.get_trades_for_wallet(
                wallet, after_ts=None,
                limit=self._page_size, offset=offset,
            )
            if not page:
                break
            new_on_page = [t for t in page if int(t.get("timestamp", 0)) > after_ts]
            out.extend(new_on_page)
            if len(new_on_page) < len(page):
                break                              # crossed the cutoff
            if len(page) < self._page_size:
                break                              # no more pages
            offset += self._page_size
        return out

    # ─────────────────────  build / dedup / classify / emit  ─────────────────────

    def _build_and_emit(
        self, whale: dict[str, Any], trade: dict[str, Any]
    ) -> tuple[WhaleSignal | None, bool]:
        """Returns (emitted_signal_or_none, skipped_as_stale)."""
        outcome_raw = (trade.get("outcome") or "").strip()
        outcome_l = outcome_raw.lower()
        if outcome_l not in ("yes", "no"):
            return None, False                            # non-binary outcome — skip

        side_yesno = "YES" if outcome_l == "yes" else "NO"
        condition_id = trade.get("conditionId") or ""
        if not condition_id:
            return None, False

        whale_fill_ts = int(trade.get("timestamp", 0))   # already ms post-client
        if whale_fill_ts <= 0:
            return None, False
        detected_at_ts = now_ms()
        latency_ms = max(0, detected_at_ts - whale_fill_ts)

        wallet = whale["address"]

        # Layer 1 dedup: 5-min in-memory window.
        if self._is_recent_duplicate(wallet, condition_id, outcome_l, side_yesno, whale_fill_ts):
            _log.debug(
                "whale_tracker: 5-min dedup'd (%s %s %s %s)",
                wallet[:10], condition_id[:10], outcome_l, side_yesno,
            )
            return None, False

        # Classification against current position cache.
        slot = self._position_cache.get(
            (wallet, condition_id), {"yes_shares": 0.0, "no_shares": 0.0}
        )
        try:
            trade_shares = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
        except (TypeError, ValueError):
            return None, False
        if price <= 0 or trade_shares <= 0:
            return None, False

        signal_type = classify_signal_type(
            yes_shares_prior=slot["yes_shares"],
            no_shares_prior=slot["no_shares"],
            trade_outcome=outcome_l,
            trade_side=str(trade.get("side", "")).upper(),
            trade_shares=trade_shares,
        )
        if signal_type == "flip":
            _log.warning(
                "whale_tracker: flip signal for %s on %s "
                "(yes_p=%.2f, no_p=%.2f, new=%s%s × %.2f) — "
                "rare/exotic, may indicate hedge or misclassification",
                wallet[:10], condition_id[:10],
                slot["yes_shares"], slot["no_shares"],
                str(trade.get("side")).upper(), side_yesno, trade_shares,
            )

        size_usd = trade_shares * price
        sig = WhaleSignal(
            whale_address=wallet,
            market_id=condition_id,
            side=side_yesno,
            signal_type=signal_type,
            price=price,
            size_usd=size_usd,
            whale_fill_ts=whale_fill_ts,
            detected_at_ts=detected_at_ts,
            detection_latency_ms=latency_ms,
            raw_json=json.dumps(trade, separators=(",", ":"), default=str),
            whale_label=whale.get("label"),
            transaction_hash=trade.get("transactionHash"),
        )

        # Layer 2 dedup: DB-level UNIQUE on transaction_hash.
        rowid = self._tracker.record_whale_signal(sig)
        if rowid is None:
            _log.debug(
                "whale_tracker: DB-level dedup'd tx=%s",
                (sig.transaction_hash or "")[:16],
            )
            return None, False

        # Apply to position cache regardless of staleness — it's a real trade
        # that genuinely happened, so the cache must reflect it.
        self._apply_signal_to_cache(sig)
        # Track latency for percentile logging across all observed trades,
        # including stale ones (so the operator can see if a whale's trades
        # are arriving late on average).
        self._recent_latencies.append(latency_ms)

        # Stale-signal guard: trade is real and recorded, but paper_trader
        # would paper-trade at stale prices if we enqueued. Skip enqueue and
        # the 5-min dedup mark (we never emitted, so don't pollute dedup state
        # — a fresh signal for the same key arriving later should still emit).
        if latency_ms > self._max_signal_age_ms:
            _log.debug(
                "whale_tracker: stale signal not enqueued "
                "(wallet=%s market=%s age=%dms > max=%dms) — recorded to DB only",
                wallet[:10], condition_id[:10], latency_ms, self._max_signal_age_ms,
            )
            return None, True

        self._mark_dedup(wallet, condition_id, outcome_l, side_yesno, whale_fill_ts)
        emitted = replace(sig, id=rowid)
        self._enqueue(emitted)
        return emitted, False

    def _is_recent_duplicate(
        self, wallet: str, market_id: str, outcome: str, side: str, ts_ms: int
    ) -> bool:
        self._evict_old_dedup(ts_ms)
        key = (wallet, market_id, outcome.lower(), side.upper())
        last = self._dedup.get(key)
        return last is not None and (ts_ms - last) < self._dedup_window_ms

    def _mark_dedup(
        self, wallet: str, market_id: str, outcome: str, side: str, ts_ms: int
    ) -> None:
        self._dedup[(wallet, market_id, outcome.lower(), side.upper())] = ts_ms

    def _evict_old_dedup(self, now_ts_ms: int) -> None:
        cutoff = now_ts_ms - self._dedup_window_ms
        # Iterate snapshot of items to allow safe deletion.
        for k, v in list(self._dedup.items()):
            if v < cutoff:
                del self._dedup[k]

    def _enqueue(self, signal: WhaleSignal) -> None:
        """Bounded queue. On overflow, drop the OLDEST unconsumed signal
        and push the new one — better than blocking the poller or OOM."""
        try:
            self._queue.put_nowait(signal)
            return
        except queue.Full:
            pass
        # Drop oldest, retry. If still full (consumer races), give up.
        try:
            dropped = self._queue.get_nowait()
            _log.warning(
                "whale_tracker: queue overflow at maxsize=%d; "
                "dropped oldest signal (id=%s, market=%s)",
                self._queue_max, dropped.id, dropped.market_id[:10],
            )
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(signal)
        except queue.Full:
            _log.warning("whale_tracker: queue still full after eviction; signal lost")

    def _log_latency_percentiles(self) -> None:
        if not self._recent_latencies:
            return
        s = sorted(self._recent_latencies)
        n = len(s)
        p50 = s[n // 2]
        p95 = s[min(n - 1, (n * 95) // 100)]
        p99 = s[min(n - 1, (n * 99) // 100)]
        _log.info(
            "whale_tracker: detection latency p50=%dms p95=%dms p99=%dms (n=%d)",
            p50, p95, p99, n,
        )
