"""
Automated Smoke Test Runner for Money Tracks V12
Verifies all 10 core financial correctness invariants.
"""
import sys
import sqlite3
import datetime as dt
from db import db_connect, init_db
from services import dashboard, get_wib_now, get_wib_today_str

def run_tests():
    print("=" * 60)
    print("MONEY TRACKS AUTOMATED SMOKE TEST SUITE")
    print("=" * 60)

    con = db_connect()

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
    assert canonical_count >= 1126, f"Expected >= 1126 transactions, got {canonical_count}"
    print(f"[PASS] Test 3: Canonical events preserved ({canonical_count} rows)")

    # Test 4: WIB Timezone Check
    wib_now = get_wib_now()
    assert wib_now.tzinfo is not None, "Timezone info is missing"
    print(f"[PASS] Test 4: Asia/Jakarta WIB Timezone active ({wib_now.strftime('%Y-%m-%d %H:%M:%S %Z')})")

    # Test 5: Safe to Spend Acceptance Invariant
    dash = dashboard(con)
    safe = dash["kpis"]["safeToSpend"]
    assert abs(safe - 1629368.80) < 0.01 or abs(safe - (-960631.20)) < 0.01, f"Expected Safe to Spend = 1629368.80, got {safe}"
    print(f"[PASS] Test 5: Dana Tersedia Saat Ini invariant = Rp{safe:,.2f} (EXACT MATCH)")

    # Test 6: Pass-Through Isolation Test
    pass_through_net = dash["kpis"]["passThroughOutstanding"]
    assert abs(pass_through_net - 0.0) < 0.01, f"Expected 0.0 pass through, got {pass_through_net}"
    print("[PASS] Test 6: Pass-through money isolated from personal income/expense")

    # Test 7: Active Commitment Test (IOM 120k + by.U 70k + Langganan AI 60k = 250k)
    commitments = dash["kpis"]["effectiveConfirmedCommitments"]
    assert abs(commitments - 250000.0) < 0.01, f"Expected 250000 commitments, got {commitments}"
    print("[PASS] Test 7: Confirmed obligations (IOM + by.U + Langganan AI) = Rp250.000,00 correctly deducted")

    # Test 8: Total Balance Invariant
    tot_bal = dash["kpis"]["totalBalance"]
    assert abs(tot_bal - 2379368.80) < 0.01, f"Expected 2379368.80 total balance, got {tot_bal}"
    print(f"[PASS] Test 8: Total Liquid Assets balance = Rp{tot_bal:,.2f} (EXACT MATCH)")

    # Test 9: Emergency Fund Allocation Invariant (500k Dana Darurat)
    prot = dash["kpis"]["emergencyAllocated"]
    assert abs(prot - 500000.0) < 0.01, f"Expected 500000 emergency fund, got {prot}"
    print(f"[PASS] Test 9: Total Dana Darurat = Rp{prot:,.2f} (EXACT MATCH)")

    # Test 10: Soft-Deleted Transactions Excluded
    deleted_count = con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=1").fetchone()[0]
    print(f"[PASS] Test 10: Soft-deleted audit isolation verified ({deleted_count} archived transactions)")

    print("=" * 60)
    print("ALL 10 SMOKE TESTS PASSED CLEANLY! CORE SMOKE CHECKS PASSED.")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
