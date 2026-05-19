"""Structured logger with secret redaction.

Implements PLAN.md §3 (auth-secret redaction), §6 (`bot.log_level`), and the
muscle-memory rule from PLAN.md §0: no module ever instantiates
`logging.getLogger` directly — they use `get_logger(name)` from this module
so the redaction mechanism is always active.

Secret registration (the canonical API):
    `register_secret(value)` is idempotent and thread-safe. Call it
    immediately after `load_dotenv()` in main.py for each sensitive env var:

        load_dotenv()
        for var in ("CLOB_API_KEY", "CLOB_API_SECRET", "PRIVATE_KEY"):
            v = os.environ.get(var)
            if v:
                register_secret(v)

    A best-effort env-name scan also runs at first `get_logger()` call as a
    convenience, but module-level `logger = get_logger(__name__)` typically
    runs BEFORE `load_dotenv()` in normal Python startup — so never rely on
    the auto-scan to catch a secret your code depends on. Explicit registration
    is the auditable, timing-independent path.

Redaction mechanism:
    Records are scrubbed at creation time via `logging.setLogRecordFactory`.
    This catches every record produced by any logger in the process — including
    third-party libraries (py-clob-client, aiohttp, web3) and logs routed to
    library-installed handlers — before any logger or handler sees the raw
    msg/args. The factory hook is global and unavoidable.

    Note on the simpler "filter on root logger" pattern: in Python's logging
    model, `Logger.callHandlers` walks up the tree calling ancestors' handlers
    but does NOT re-apply ancestors' logger-level filters. So a filter on
    root only runs for records emitted via `root.info(...)` directly, not for
    descendant-logger emissions. The factory hook is what makes redaction
    actually global. The redaction filter is also attached to the root logger
    as defense-in-depth and for verifiability.

Format: `[YYYY-MM-DD HH:MM:SS] LEVEL  module — message` (rich-rendered).
Default level INFO. Override with `LOG_LEVEL=DEBUG` (or any standard level).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable

from rich.logging import RichHandler


# Env var key patterns whose VALUES must never appear in any log output.
# Matched against env var NAMES at first get_logger() call as a convenience;
# explicit register_secret() is the canonical path.
_SECRET_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r".*KEY.*", re.IGNORECASE),
    re.compile(r".*SECRET.*", re.IGNORECASE),
    re.compile(r"PRIVATE.*", re.IGNORECASE),
)

# Env values shorter than this are not redacted — risk of false-positive
# matches on incidental short strings. Real secrets are always longer.
_MIN_SECRET_LEN = 8


class _RedactionFilter(logging.Filter):
    """Holds the live secret set and provides scrubbing.

    Mutable: `add()` is idempotent and thread-safe. The same scrubber is
    used by the LogRecord factory hook installed in `_setup()`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        # Atomic snapshot used by the scrubber; replaced wholesale on add().
        self._ordered: tuple[str, ...] = ()

    def add(self, value: str) -> None:
        if not value or len(value) < _MIN_SECRET_LEN:
            return
        with self._lock:
            if value in self._secrets:
                return
            self._secrets.add(value)
            # Longest-first to avoid partial-overlap leaks.
            self._ordered = tuple(sorted(self._secrets, key=len, reverse=True))

    def known_count(self) -> int:
        return len(self._secrets)

    def scrub(self, text: str) -> str:
        for s in self._ordered:
            if s in text:
                text = text.replace(s, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._ordered:
            return True
        if isinstance(record.msg, str):
            record.msg = self.scrub(record.msg)
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(self.scrub(a) if isinstance(a, str) else a for a in args)
        return True


# Module-level singleton — referenced by both the factory hook and the
# root-attached filter. Tests reach in to assert membership/count.
_redaction_filter = _RedactionFilter()
_initialised = False
_setup_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Add a value to the redaction set. Idempotent. Thread-safe.

    THE canonical way to declare a secret. Call once after `load_dotenv()` in
    main.py for each sensitive env var. Values shorter than 8 chars are
    silently ignored (false-positive guard against incidental short strings).
    Calling before `get_logger()` is fine — the factory will be installed
    on first `get_logger()` call and will consult the live secret set.
    """
    _redaction_filter.add(value)


def _scan_env_for_secrets() -> None:
    """Best-effort: register values whose env-var names match the patterns.

    Convenience only. Does not replace the explicit `register_secret()`
    call after `load_dotenv()`.
    """
    for name, value in os.environ.items():
        if not value:
            continue
        for pat in _SECRET_KEY_PATTERNS:
            if pat.fullmatch(name):
                _redaction_filter.add(value)
                break


def _make_scrubbing_factory(
    base: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """Wrap a LogRecord factory so every record is scrubbed at creation."""

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        if _redaction_filter._ordered:
            if isinstance(record.msg, str):
                record.msg = _redaction_filter.scrub(record.msg)
            if record.args:
                a = record.args if isinstance(record.args, tuple) else (record.args,)
                record.args = tuple(
                    _redaction_filter.scrub(x) if isinstance(x, str) else x for x in a
                )
        return record

    return factory


def _setup() -> None:
    """One-time global logging setup."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Wrap whatever factory is currently installed (typically the default).
    # This is what actually scrubs third-party library emissions.
    logging.setLogRecordFactory(_make_scrubbing_factory(logging.getLogRecordFactory()))

    rich_handler = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        markup=False,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    rich_handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))

    root = logging.getLogger()
    # Defense in depth + verifiability: attach the filter to root so direct
    # root emissions (`logging.info(...)`) are also scrubbed even if the
    # factory hook is somehow unwound by a third-party library.
    if _redaction_filter not in root.filters:
        root.addFilter(_redaction_filter)
    # Append our handler. Do NOT clear root.handlers — third-party libraries
    # may install their own and they should keep working. Their records pass
    # through the scrubbing factory before reaching any handler.
    root.addHandler(rich_handler)
    root.setLevel(level)

    # Convenience: register any secrets already present in the env at this
    # point. Caller still owns explicit register_secret() after dotenv load.
    _scan_env_for_secrets()


def get_logger(name: str) -> logging.Logger:
    """Return a logger with global redaction active.

    Single global factory. No module should call `logging.getLogger` directly —
    bypassing this function bypasses the one-time setup that installs the
    LogRecord factory hook.
    """
    global _initialised
    if not _initialised:
        with _setup_lock:
            if not _initialised:
                _setup()
                _initialised = True
    return logging.getLogger(name)
