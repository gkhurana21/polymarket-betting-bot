"""Logging with secret redaction. Ported from the predecessor project.

Any value registered via register_secret is scrubbed from every log record,
including records emitted by third-party libraries. A private key that reaches
a log file is a private key that reaches a backup.
"""

from __future__ import annotations

import logging
import os
import sys

_SECRETS: set[str] = set()
_MIN_SECRET_LEN = 8


def register_secret(value: str) -> None:
    if value and len(value) >= _MIN_SECRET_LEN:
        _SECRETS.add(value)


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for s in _SECRETS:
            if s in msg:
                msg = msg.replace(s, "***REDACTED***")
                record.msg = msg
                record.args = ()
        return True


_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    h.addFilter(_RedactionFilter())
    root.addHandler(h)
    root.addFilter(_RedactionFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(name)
