"""Smoke test for Module 1 (utils/helpers.py + utils/logger.py).

Run from project root:
    .venv/bin/python tests/smoke_module1.py

Exits non-zero on any failure. Designed to complete in <1 second.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

# Ensure project root on path so `utils.*` imports work when running directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.helpers import clamp, load_config, ms_to_iso, now_ms  # noqa: E402
from utils.logger import (  # noqa: E402
    _RedactionFilter,
    _redaction_filter,
    get_logger,
    register_secret,
)


def main() -> int:
    # ── helpers ─────────────────────────────────────────────────────────
    cfg = load_config("config.yaml")
    assert cfg["dry_run"] is True, "config.yaml must have dry_run: true in Phase A"
    assert cfg["phase"] == "A", "phase must be 'A'"

    assert clamp(1500, 5, 500) == 500, "clamp upper bound failed"
    assert clamp(2, 5, 500) == 5, "clamp lower bound failed"
    assert clamp(100, 5, 500) == 100, "clamp pass-through failed"
    try:
        clamp(1, 10, 5)
    except ValueError:
        pass
    else:
        raise AssertionError("clamp should reject lo > hi")

    ts = now_ms()
    assert isinstance(ts, int) and ts > 1_700_000_000_000
    iso = ms_to_iso(ts)
    assert iso.startswith("20") and "T" in iso and iso.endswith("+00:00"), iso
    print(f"  helpers: load_config / now_ms / ms_to_iso / clamp     OK  (now={iso})")

    # ── logger basic emission (also triggers _setup) ───────────────────
    log = get_logger("smoke.module1")
    log.info("logger initialised — this line should appear above with rich format")
    print("  logger: get_logger + info emission                     OK")

    # ── register_secret: idempotency + min-length floor ─────────────────
    before = _redaction_filter.known_count()
    register_secret("supersecret_value_xyz12345")
    register_secret("supersecret_value_xyz12345")          # idempotent
    register_secret("short")                                # below floor → ignored
    assert _redaction_filter.known_count() == before + 1, (
        "register_secret must be idempotent and must ignore values below "
        "the min-length floor"
    )
    print("  logger: register_secret idempotent + min-length floor  OK")

    # ── filter attached to ROOT (per build directive) ───────────────────
    root = logging.getLogger()
    assert _redaction_filter in root.filters, (
        "redaction filter must be attached to the root logger, not just a handler"
    )
    print("  logger: redaction filter attached to root              OK")

    # ── factory hook scrubs third-party-style descendant emissions ──────
    # Set propagate=False so the record CANNOT flow up to root's filters or
    # handlers. The only way it gets scrubbed is via setLogRecordFactory.
    # This is the test that the user's "filter on root" prescription alone
    # would NOT pass — proving the factory hook is doing the real work.
    buf = io.StringIO()
    capture = logging.StreamHandler(buf)
    capture.setFormatter(logging.Formatter("%(message)s"))
    third_party = logging.getLogger("py_clob_client.fake_module")
    third_party.addHandler(capture)
    third_party.propagate = False
    try:
        third_party.info("leaked supersecret_value_xyz12345 in url")
    finally:
        third_party.removeHandler(capture)
        third_party.propagate = True
    captured = buf.getvalue()
    assert "[REDACTED]" in captured, (
        f"third-party descendant emission must be scrubbed via factory hook; "
        f"got {captured!r}"
    )
    assert "supersecret_value_xyz12345" not in captured, (
        f"raw secret leaked through factory: {captured!r}"
    )
    print("  logger: third-party descendant emission scrubbed       OK")

    # ── direct _RedactionFilter behaviour (unit-level) ──────────────────
    f = _RedactionFilter()
    f.add("another_secret_8chars_long_xyz")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="p", lineno=0,
        msg="leaked another_secret_8chars_long_xyz", args=(), exc_info=None,
    )
    f.filter(rec)
    assert "[REDACTED]" in rec.msg and "another_secret" not in rec.msg
    print("  logger: _RedactionFilter direct scrubbing              OK")

    print("\nmodule 1 smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
