"""
Master Test Runner for Prompt 2
Runs:
1. Prompt 1b & 1c Suite (A-I, J1-J5)
2. Prompt 2 Section A: core.js Pure Calculations
3. Prompt 2 Section B: Acceptance Tests (T-01..T-10)
4. Prompt 2 Section C: Security Tests (R-01..R-05)
"""
import sys
import subprocess
import test_verify_balances
import test_acceptance_and_security
from test_core_js import run_node_core_tests


def main():
    print("=" * 75)
    print("MONEY TRACKS V12 — PROMPT 2 MASTER TEST RUNNER")
    print("=" * 75)

    print("\n--- 1. PROMPT 1B & 1C TEST SUITE (A-I, J1-J5) ---")
    test_verify_balances.run_all_tests()

    print("\n--- 2. PROMPT 2 SECTION A: CORE.JS PURE CALCULATIONS ---")
    core_results = run_node_core_tests()
    for r in core_results:
        status = "[PASS]" if r["passed"] else "[FAIL]"
        err = f" -> {r.get('error')}" if not r["passed"] else ""
        print(f"{status} {r['name']}{err}")

    print("\n--- 3. PROMPT 2 SECTION B & C: ACCEPTANCE & SECURITY SUITE ---")
    passed, failed = test_acceptance_and_security.run_all()

    print("\n" + "=" * 75)
    print(f"SUMMARY: {len(passed)} acceptance/security tests passed, {len(failed)} tests failed.")
    print("=" * 75)


if __name__ == "__main__":
    main()
