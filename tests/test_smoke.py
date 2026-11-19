"""Run every smoke script and every Friday test under pytest.

The scripts are plain programs that assert as they go and exit non-zero on failure. Each one runs in its own
process here, offline (LIVE_API_TESTS=0 skips the few checks that call Polymarket), so `pytest` and CI see them. One script needs the local
corpus and is skipped where data/ is absent.

    pytest -q                      # everything, offline
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRIDAY = ROOT / "friday"

SMOKE = sorted(p.name for p in (ROOT / "tests").glob("smoke_*.py"))
# Scripts that validate against the stored corpus, which is local only (data/ is not in the repo).
NEEDS_DATA = {"smoke_llm_forecast.py": "data/trades.db"}
FRIDAY_TESTS = sorted(p.stem for p in (FRIDAY / "tests").glob("test_*.py"))


def _run(args: list[str], cwd: Path) -> None:
    env = {**os.environ, "LIVE_API_TESTS": "0"}
    done = subprocess.run([sys.executable, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"{' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


@pytest.mark.parametrize("script", SMOKE)
def test_smoke(script: str) -> None:
    needs = NEEDS_DATA.get(script)
    if needs and not (ROOT / needs).exists():
        pytest.skip(f"needs {needs}, which is local only")
    _run([f"tests/{script}"], ROOT)


@pytest.mark.parametrize("module", FRIDAY_TESTS)
def test_friday(module: str) -> None:
    _run(["-m", f"tests.{module}"], FRIDAY)
