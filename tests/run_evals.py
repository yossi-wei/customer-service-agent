"""Standalone eval runner — no pytest required.

    python3 tests/run_evals.py

Discovers every case in tests/cases/*.json, runs the agent, prints a pass/fail table, and
exits non-zero if any case fails (so it works in CI).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_loader import load_cases, run_case  # noqa: E402


def main():
    cases = load_cases()
    passed = 0
    print(f"Running {len(cases)} eval case(s)\n" + "=" * 72)
    for file_name, case in cases:
        ok, action, intent, failures = run_case(case)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {file_name} :: {case['name']:<38} -> {action} ({intent})")
        for f in failures:
            print(f"         {f}")
        passed += ok
    print("=" * 72)
    print(f"{passed}/{len(cases)} passed")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
