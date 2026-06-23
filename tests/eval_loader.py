"""Shared helpers for the eval harness.

The harness is intentionally pluggable: every `*.json` file in tests/cases/ is auto-discovered.
To add coverage, just drop a new file with the same shape — no code changes needed.

Case file shape:
    {"cases": [
        {"name": "...", "email": {"from","subject","body"}, "expect": {"action","intent"?}}
    ]}

Evals are forced into deterministic FALLBACK mode (no API key) so results are reproducible in
CI and don't depend on a live model. To eval the LLM path instead, run the agent directly.
"""
import json
import os
import sys
from pathlib import Path

CASES_DIR = Path(__file__).parent / "cases"
ROOT = Path(__file__).parent.parent


def _force_fallback():
    os.environ.pop("ANTHROPIC_API_KEY", None)  # deterministic, no network
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def load_cases():
    """Return a flat list of (file_name, case_dict), discovered from tests/cases/*.json."""
    out = []
    for path in sorted(CASES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        for case in data.get("cases", []):
            out.append((path.name, case))
    return out


def run_case(case):
    """Triage one case, return (passed: bool, actual_action, actual_intent, failures: list)."""
    _force_fallback()
    import agent  # imported after fallback is forced
    t = agent.triage_email({**case["email"], "id": case["name"]})
    expect = case["expect"]
    failures = []
    if t.action != expect["action"]:
        failures.append(f"action: expected {expect['action']}, got {t.action}")
    if "intent" in expect and t.intent != expect["intent"]:
        failures.append(f"intent: expected {expect['intent']}, got {t.intent}")
    return (not failures, t.action, t.intent, failures)
