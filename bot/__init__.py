"""bot package — Polymarket betting bot. Implements PLAN.md §2 bot/.

Exposes the build-phase constant. main.py asserts this matches the runtime
config.yaml `phase` field at startup; mismatch is a hard fail.
"""

__version__ = "0.1.0-phase-a"
PHASE = "A"
