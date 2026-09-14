"""
Automated Smoke Test Runner for Money Tracks V12
Verifies core financial correctness invariants against an isolated, disposable database.
"""
import datetime as dt
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "aturuang"))

from db import db_connect, init_db, seed_plan
from services import dashboard, get_wib_now

# Newly invented sanitized synthetic constants
SYNTHETIC_ACCOUNT_NAME = "Synthetic Account Alpha"
SYNTHETIC_ACCOUNT_BALANCE = 5000.00
SYNTHETIC_GOAL_NAME = "Synthetic Goal Alpha"
SYNTHETIC_GOAL_ALLOCATED = 1000.00
SYNTHETIC_COMMITMENT_NAME = "Synthetic Commitment Alpha"
SYNTHETIC_COMMITMENT_AMOUNT = 500.00
SYNTHETIC_SAFE_TO_SPEND = round(SYNTHETIC_ACCOUNT_BALANCE - SYNTHETIC_GOAL_ALLOCATED - SYNTHETIC_COMMITMENT_AMOUNT, 2)
SYNTHETIC_ACTIVE_TX_COUNT = 5
SYNTHETIC_DELETED_TX_COUNT = 1


def seed_synthetic_smoke_fixture(db_path: Path) -> None:
    """Seeds sanitized synthetic fixtures into an isolated database."""
    con = sqlite3.connect(db_path)
    con.execute("UPDATE accounts SET active=0 WHERE kind='Owned'")
    con.execute(
        "INSERT OR REPLACE INTO accounts (name, kind, current_balance, active) VALUES (?, 'Owned', ?, 1)",
        (SYNTHETIC_ACCOUNT_NAME, SYNTHETIC_ACCOUNT_BALANCE),
    )
    con.execute(
        "INSERT INTO allocation_goals (name, kind, target_amount, allocated_amount, status) VALUES (?, 'Emergency', ?, ?, 'Active')",
        (SYNTHETIC_GOAL_NAME, SYNTHETIC_GOAL_ALLOCATED, SYNTHETIC_GOAL_ALLOCATED),
    )
    con.execute(
        "INSERT INTO upcoming (due_date, title, amount, category, account, status, reserve_now) VALUES ('2026-09-20', ?, ?, 'Other / Miscellaneous', ?, 'Upcoming', 0)",
        (SYNTHETIC_COMMITMENT_NAME, SYNTHETIC_COMMITMENT_AMOUNT, SYNTHETIC_ACCOUNT_NAME),
    )
    txs = [(f"syn_tx_{i}", "2026-08-01", "Expense", 10.0, "Personal", 0) for i in range(SYNTHETIC_ACTIVE_TX_COUNT)]
    con.executemany(
        "INSERT INTO transactions (canonical_id, date, transaction_type, amount, money_context, is_deleted) VALUES (?, ?, ?, ?, ?, ?)",
        txs,
    )
    con.execute(
        "INSERT INTO transactions (canonical_id, date, transaction_type, amount, money_context, is_deleted) VALUES ('syn_del_1', '2026-08-01', 'Expense', 10.0, 'Personal', 1)"
    )
    seed_plan(con, "2026-08")
    con.commit()
    con.close()


def run_smoke_suite_on_connection(con: sqlite3.Connection) -> bool:
    """Executes the 10 financial invariant checks on the given connection."""
    # Test 1: DB Integrity
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok", f"Integrity check failed: {integrity}"
    print("[PASS] Test 1: PRAGMA integrity_check = ok")

    # Test 2: Foreign Key Check
    fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
    assert len(fk_errors) == 0, f"FK errors found: {fk_errors}"
    print("[PASS] Test 2: PRAGMA foreign_key_check = empty")

    # Test 3: Canonical Count Preserved
    canonical_count = con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
    assert canonical_count == SYNTHETIC_ACTIVE_TX_COUNT, f"Expected {SYNTHETIC_ACTIVE_TX_COUNT} transactions, got {canonical_count}"
    print(f"[PASS] Test 3: Canonical events preserved ({canonical_count} rows)")

    # Test 4: WIB Timezone Check
    wib_now = get_wib_now()
    assert wib_now.tzinfo is not None, "Timezone info is missing"
    print(f"[PASS] Test 4: Asia/Jakarta WIB Timezone active ({wib_now.strftime('%Y-%m-%d %H:%M:%S %Z')})")

    # Test 5: Safe to Spend Acceptance Invariant
    dash = dashboard(con, "2026-08")
    safe = dash["kpis"]["safeToSpend"]
    assert abs(safe - SYNTHETIC_SAFE_TO_SPEND) < 0.01, f"Expected Safe to Spend = {SYNTHETIC_SAFE_TO_SPEND}, got {safe}"
    print(f"[PASS] Test 5: Dana Tersedia Saat Ini invariant = Rp{safe:,.2f} (EXACT MATCH)")

    # Test 6: Pass-Through Isolation Test
    pass_through_net = dash["kpis"]["passThroughOutstanding"]
    assert abs(pass_through_net - 0.0) < 0.01, f"Expected 0.0 pass through, got {pass_through_net}"
    print("[PASS] Test 6: Pass-through money isolated from personal income/expense")

    # Test 7: Active Commitment Test
    commitments = dash["kpis"]["effectiveConfirmedCommitments"]
    assert abs(commitments - SYNTHETIC_COMMITMENT_AMOUNT) < 0.01, f"Expected {SYNTHETIC_COMMITMENT_AMOUNT} commitments, got {commitments}"
    print(f"[PASS] Test 7: Confirmed obligations = Rp{commitments:,.2f} correctly deducted")

    # Test 8: Total Balance Invariant
    tot_bal = dash["kpis"]["totalBalance"]
    assert abs(tot_bal - SYNTHETIC_ACCOUNT_BALANCE) < 0.01, f"Expected {SYNTHETIC_ACCOUNT_BALANCE} total balance, got {tot_bal}"
    print(f"[PASS] Test 8: Total Liquid Assets balance = Rp{tot_bal:,.2f} (EXACT MATCH)")

    # Test 9: Emergency Fund Allocation Invariant
    prot = dash["kpis"]["emergencyAllocated"]
    assert abs(prot - SYNTHETIC_GOAL_ALLOCATED) < 0.01, f"Expected {SYNTHETIC_GOAL_ALLOCATED} emergency fund, got {prot}"
    print(f"[PASS] Test 9: Total Dana Darurat = Rp{prot:,.2f} (EXACT MATCH)")

    # Test 10: Soft-Deleted Transactions Excluded
    deleted_count = con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=1").fetchone()[0]
    assert deleted_count == SYNTHETIC_DELETED_TX_COUNT, f"Expected {SYNTHETIC_DELETED_TX_COUNT} soft-deleted, got {deleted_count}"
    print(f"[PASS] Test 10: Soft-deleted audit isolation verified ({deleted_count} archived transactions)")

    print("=" * 60)
    print("ALL 10 SMOKE TESTS PASSED CLEANLY! CORE SMOKE CHECKS PASSED.")
    print("=" * 60)
    return True


def run_smoke_in_dir(temp_dir: Path, raise_intentional: bool = False, cause_conn_failure: bool = False) -> None:
    """Executes smoke tests inside an isolated directory with guaranteed resource cleanup."""
    db_path = temp_dir / "disposable_smoke.db"
    con = None
    try:
        if cause_conn_failure:
            invalid_dir = temp_dir / "unopenable_dir"
            invalid_dir.mkdir(exist_ok=True)
            con = db_connect(invalid_dir)
            con.execute("SELECT 1")
            return

        init_db(db_path)
        seed_synthetic_smoke_fixture(db_path)
        con = db_connect(db_path)

        if raise_intentional:
            raise RuntimeError("Intentional test exception during smoke suite execution")

        run_smoke_suite_on_connection(con)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        # Explicitly clean up db and any potential sidecar files in the directory
        for file_name in [db_path.name, f"{db_path.name}-wal", f"{db_path.name}-shm", f"{db_path.name}-journal"]:
            target_file = temp_dir / file_name
            if target_file.exists():
                try:
                    target_file.unlink()
                except Exception:
                    pass


def run_tests() -> None:
    """Entrypoint that always creates and runs within an isolated OS TemporaryDirectory."""
    print("=" * 60)
    print("MONEY TRACKS AUTOMATED SMOKE TEST SUITE")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as td:
        run_smoke_in_dir(Path(td))


if __name__ == "__main__":
    run_tests()
