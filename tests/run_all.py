"""
AturUang — Master Test Runner Terpusat

Mendukung dua mode:
1. Quick: python tests/run_all.py quick
2. Full : python tests/run_all.py full
"""
import glob
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = BASE_DIR / "tests"
SCRIPTS_DIR = BASE_DIR / "scripts"
RUNTIME_DIR = BASE_DIR / "runtime"
DB_PATH = RUNTIME_DIR / "money_tracks.db"

# Ensure BASE_DIR and aturuang are on path
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "aturuang"))


def get_db_hash() -> str:
    if DB_PATH.exists():
        return hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    return "NO_DB_FILE"


def run_quick_suite() -> bool:
    print("=" * 65)
    print("ATURUANG TEST RUNNER — QUICK MODE")
    print("=" * 65)

    # 1. Compile check
    print("[1/5] Checking python bytecode compilation...")
    res_compile = subprocess.run([sys.executable, "-m", "compileall", "-q", "."], cwd=str(BASE_DIR), capture_output=True, text=True)
    if res_compile.returncode != 0:
        print("FAIL: Bytecode compilation error!")
        print(res_compile.stderr)
        return False
    print("      PASS: All Python modules compiled cleanly.")

    # 2. Key unit suites
    key_tests = [
        "test_core_js.py",
        "test_verify_balances.py",
        "test_acceptance_and_security.py",
        "test_prompt11f_startup_idempotency.py",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{BASE_DIR}{os.pathsep}{BASE_DIR / 'aturuang'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    all_pass = True
    for i, kt in enumerate(key_tests, 2):
        print(f"[{i}/5] Running {kt}...")
        p = TESTS_DIR / kt
        res = subprocess.run([sys.executable, str(p)], cwd=str(BASE_DIR), env=env, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"      FAIL: {kt}")
            print(res.stderr or res.stdout)
            all_pass = False
        else:
            print(f"      PASS: {kt}")

    # 3. Smoke test
    print("[5/5] Running Smoke Test Suite...")
    smoke_script = SCRIPTS_DIR / "run_smoke_tests.py"
    if smoke_script.exists():
        res_smoke = subprocess.run([sys.executable, str(smoke_script)], cwd=str(BASE_DIR), env=env, capture_output=True, text=True)
        if res_smoke.returncode != 0:
            print("      FAIL: Smoke tests failed!")
            print(res_smoke.stderr or res_smoke.stdout)
            all_pass = False
        else:
            print("      PASS: Smoke tests passed.")

    print("=" * 65)
    if all_pass:
        print("RESULT: QUICK TEST SUITE PASSED (100% OK)!")
    else:
        print("RESULT: QUICK TEST SUITE HAD FAILURES!")
    print("=" * 65)
    return all_pass


def run_full_suite() -> bool:
    print("=" * 65)
    print("ATURUANG TEST RUNNER — FULL SUITE MODE")
    print("=" * 65)

    hash_before = get_db_hash()
    print(f"Baseline DB Hash : {hash_before}")

    test_files = sorted(glob.glob(str(TESTS_DIR / "test_*.py")))
    total_tests = len(test_files)
    print(f"Discovered {total_tests} test suites in {TESTS_DIR.name}/")
    print("-" * 65)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{BASE_DIR}{os.pathsep}{BASE_DIR / 'aturuang'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    passed_suites = []
    failed_suites = []

    start_time = time.time()
    for tf in test_files:
        name = Path(tf).name
        res = subprocess.run([sys.executable, str(tf)], cwd=str(BASE_DIR), env=env, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[FAIL] {name}")
            failed_suites.append((name, res.stderr or res.stdout))
        else:
            print(f"[PASS] {name}")
            passed_suites.append(name)

    elapsed = time.time() - start_time
    hash_after = get_db_hash()

    print("=" * 65)
    print(f"SUMMARY: {len(passed_suites)}/{total_tests} suites passed in {elapsed:.2f}s.")
    print(f"Database Hash After: {hash_after}")

    if hash_before != hash_after:
        print("CRITICAL ALERT: Database hash changed during test execution!")
        return False

    print("VERIFIED: Zero mutation confirmed on local SQLite database.")

    if failed_suites:
        print("\n--- FAILURE DETAILS ---")
        for f_name, f_err in failed_suites:
            print(f"FAIL: {f_name}")
            print(f_err[:400])
            print("-" * 40)
        return False

    print("ALL TEST SUITES PASSED CLEANLY (100%)!")
    print("=" * 65)
    return True


def main():
    mode = "quick"
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower().strip()
        if arg in ("full", "--full", "-f"):
            mode = "full"
        elif arg in ("quick", "--quick", "-q"):
            mode = "quick"
        else:
            print(f"Unknown argument '{arg}'. Usage: python tests/run_all.py [quick|full]")
            sys.exit(1)

    if mode == "quick":
        success = run_quick_suite()
    else:
        success = run_full_suite()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
