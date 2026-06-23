"""Pytest entry point over the same pluggable case files.

    pytest

Each case in tests/cases/*.json becomes its own parametrized test, so failures point at the
exact case by name. Add cases by editing/adding JSON files — no test code changes needed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from eval_loader import load_cases, run_case  # noqa: E402

CASES = load_cases()


@pytest.mark.parametrize(
    "case", [c for _, c in CASES], ids=[f"{f}:{c['name']}" for f, c in CASES]
)
def test_routing(case):
    ok, action, intent, failures = run_case(case)
    assert ok, "; ".join(failures)
