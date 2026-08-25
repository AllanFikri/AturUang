"""
Money Tracks V12 — Audit & Verification Test Suite for Prompt 1b & 1c
Tests A through I, and J1 through J5 using isolated production fixtures.
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import os
import tempfile
import sqlite3
import datetime as dt
from pathlib import Path

from db import init_db, db_connect
from services import (
    reconstruct_account_balance,
    verify_balances,
    mutate_account_balance,
    reconcile_account_balance,
    save_account_balance,
)


def create_isolated_fixture():
    """Creates an isolated temporary database with production schema from db.py."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    init_db(db_path)
    # Clear seeded data to give each test fixture a clean slate with exact production schema
    with db_connect(db_path) as con:
        con.execute("DELETE FROM balance_snapshots")
        con.execute("DELETE FROM transactions")
        con.execute("DELETE FROM reconciliations")
        con.execute("DELETE FROM accounts")
    return db_path


def cleanup_fixture(db_path: Path):
    """Cleans up temporary database file."""
    try:
        if db_path.exists():
            db_path.unlink(missing_ok=True)
    except Exception:
        pass


def test_a_correct_cache():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            res = verify_balances(con)
            assert len(res) == 0, f"Test A failed: {res}"
            print("[PASS] Test A: Saldo cache benar -> verify_balances tidak melaporkan selisih.")
    finally:
        cleanup_fixture(db_path)


def test_b_tampered_cache():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            con.execute("UPDATE accounts SET current_balance=110000.0 WHERE name='Cash'")
            res = verify_balances(con)
            assert len(res) == 1, f"Test B failed: {res}"
            assert res[0]["status"] == "discrepancy"
            assert res[0]["difference"] == -10000.0
            print("[PASS] Test B: current_balance diubah +Rp10.000 -> selisih Rp10.000 terdeteksi.")
    finally:
        cleanup_fixture(db_path)


def test_c_account_without_snapshot():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("GoPay", "Owned", 50000.0, "2026-08-22")
            )
            res = verify_balances(con)
            gopay_issue = next((x for x in res if x["account_name"] == "GoPay"), None)
            assert gopay_issue is not None, "Test C failed: GoPay should be reported"
            assert gopay_issue["status"] == "unverifiable"
            assert gopay_issue["expected_balance"] is None
            print("[PASS] Test C: Akun tanpa snapshot -> status unverifiable (tidak otomatis dianggap sehat).")
    finally:
        cleanup_fixture(db_path)


def test_d_same_date_transaction():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("2026-08-22", "10:00", "Income", 25000.0, "", "Cash", "Topup", "2026-08-22 00:05:00")
            )
            mutate_account_balance(con, "Cash", +25000.0, "2026-08-22")
            res = [x for x in verify_balances(con) if x["account_name"] == "Cash"]
            assert len(res) == 0, f"Test D failed: {res}"
            print("[PASS] Test D: Transaksi pada tanggal sama dengan snapshot tidak hilang dan tidak terhitung ganda.")
    finally:
        cleanup_fixture(db_path)


def test_e_reconciliation_mutation():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            reconcile_account_balance(con, "Cash", 120000.0, "Audit fisik kas")
            res = [x for x in verify_balances(con) if x["account_name"] == "Cash"]
            assert len(res) == 0, f"Test E failed: {res}"
            print("[PASS] Test E: Rekonsiliasi [Rekonsiliasi] dan snapshot tidak membuat mutasi terhitung dua kali.")
    finally:
        cleanup_fixture(db_path)


def test_f_tolerance():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            # Selisih 0.004 tidak membuat penyesuaian
            rec_small = reconcile_account_balance(con, "Cash", 100000.004, "Diff 0.004")
            assert rec_small["adjustment_tx_id"] is None, f"Test F1 failed: {rec_small}"
            # Selisih 0.005 membuat penyesuaian
            rec_tol = reconcile_account_balance(con, "Cash", 100000.005, "Diff 0.005")
            assert rec_tol["adjustment_tx_id"] is not None, f"Test F2 failed: {rec_tol}"
            print("[PASS] Test F: Selisih 0.004 tidak membuat transaksi penyesuaian; selisih 0.005 membuat penyesuaian.")
    finally:
        cleanup_fixture(db_path)


def test_g_production_endpoint_reconciliation():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            save_account_balance(con, {
                "name": "BCA Main",
                "kind": "Owned",
                "current_balance": 100000.0,
                "balance_date": "2026-08-22",
                "active": True
            })

            tx_count_before = con.execute("SELECT COUNT(*) FROM transactions WHERE description LIKE '%[Rekonsiliasi]%'").fetchone()[0]
            rec_log_before = con.execute("SELECT COUNT(*) FROM reconciliations WHERE account_name='BCA Main'").fetchone()[0]

            res = save_account_balance(con, {
                "name": "BCA Main",
                "kind": "Owned",
                "current_balance": 150000.0,
                "balance_date": "2026-08-22",
                "active": True
            })
            assert res.get("status") == "success", f"Test G failed on save_account_balance response: {res}"

            current_bal_after = float(con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()[0])
            assert current_bal_after == 150000.0, f"Test G failed: current_balance should be 150000.0, got {current_bal_after}"

            tx_count_after = con.execute("SELECT COUNT(*) FROM transactions WHERE description LIKE '%[Rekonsiliasi]%'").fetchone()[0]
            assert tx_count_after == tx_count_before + 1, f"Test G failed: Expected exactly 1 reconciliation transaction, found {tx_count_after - tx_count_before}"

            rec_log_after = con.execute("SELECT COUNT(*) FROM reconciliations WHERE account_name='BCA Main'").fetchone()[0]
            assert rec_log_after == rec_log_before + 1, f"Test G failed: Expected exactly 1 reconciliation audit log, found {rec_log_after - rec_log_before}"

            print("[PASS] Test G: Jalur produksi save_account_balance mengubah saldo akun lama lewat tepat satu transaksi [Rekonsiliasi] dan audit log.")
    finally:
        cleanup_fixture(db_path)


def test_h_verify_balances_read_only():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 999999.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            issues = verify_balances(con)
            assert len(issues) > 0, "Test H failed: Discrepancy should be reported"
            bal_after = float(con.execute("SELECT current_balance FROM accounts WHERE name='Cash'").fetchone()[0])
            assert bal_after == 999999.0, f"Test H failed: verify_balances modified database from 999999.0 to {bal_after}"
            print("[PASS] Test H: verify_balances hanya melaporkan dan tidak pernah mengubah saldo database secara otomatis.")
    finally:
        cleanup_fixture(db_path)


def test_i_collision_and_ambiguous_ordering():
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("2026-08-22", "00:00", "Income", 50000.0, "", "Cash", "Collision Tx", "2026-08-22 00:00:00")
            )

            res = verify_balances(con)
            cash_issue = next((x for x in res if x["account_name"] == "Cash"), None)
            assert cash_issue is not None, "Test I failed: Collision should be detected"
            assert cash_issue["status"] == "unverifiable", f"Test I failed: Status should be unverifiable, got {cash_issue['status']}"
            assert "urutan transaksi dan snapshot pada waktu anchor tidak dapat dipastikan" in cash_issue["reason"]
            print("[PASS] Test I: Collision timestamp transaksi & snapshot pada waktu anchor ditandai unverifiable.")
    finally:
        cleanup_fixture(db_path)


# -----------------------------------------------------------------------------
# Test Suite Prompt 1c: J1 - J5
# -----------------------------------------------------------------------------

def test_j1_reconcile_when_ledger_drifts_and_actual_matches_cache():
    """J1: E=100.000, C=150.000, A=150.000 -> buat adjustment +50.000 -> akhir E=C=A=150.000."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            # Baseline anchor snapshot 100.000
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 150000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("BCA Main", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )

            # Sebelum rekonsiliasi: E=100.000, C=150.000
            recon_before = reconstruct_account_balance(con, "BCA Main")
            assert recon_before["expected_balance"] == 100000.0
            assert recon_before["cached_balance"] == 150000.0

            # Eksekusi rekonsiliasi dengan actual A=150.000
            res = reconcile_account_balance(con, "BCA Main", 150000.0, "Koreksi fisik selisih awal")
            assert res["action"] == "ledger_adjusted"
            assert res["difference"] == 50000.0
            assert res["adjustment_tx_id"] is not None

            # Setelah rekonsiliasi: E=C=A=150.000
            recon_after = reconstruct_account_balance(con, "BCA Main")
            assert recon_after["expected_balance"] == 150000.0
            assert recon_after["cached_balance"] == 150000.0
            assert recon_after["difference"] == 0.0

            # verify_balances tidak melaporkan discrepancy
            issues = [x for x in verify_balances(con) if x["account_name"] == "BCA Main"]
            assert len(issues) == 0, f"Test J1 failed: Issues reported {issues}"
            print("[PASS] Test J1: E=100k, C=150k, A=150k -> adjustment +50k -> akhir E=C=A=150k dan tidak ada discrepancy.")
    finally:
        cleanup_fixture(db_path)


def test_j2_reconcile_when_cache_drifted_but_ledger_matches_actual():
    """J2: E=100.000, C=150.000, A=100.000 -> tidak buat tx keuangan -> cache dibangun ulang ke 100k -> audit perbaikan cache tercatat."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            # Baseline anchor snapshot 100.000
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 150000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("BCA Main", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )

            tx_count_before = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

            # Rekonsiliasi dengan actual A=100.000 (sama dengan E)
            res = reconcile_account_balance(con, "BCA Main", 100000.0, "Sinkronisasi cache dengan fisik 100k")
            assert res["action"] == "cache_repaired"
            assert res["adjustment_tx_id"] is None

            # Tidak ada transaksi keuangan baru dibuat
            tx_count_after = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            assert tx_count_after == tx_count_before, "Test J2 failed: No financial tx should be created"

            # Cache di-set menjadi 100.000
            current_bal = float(con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()[0])
            assert current_bal == 100000.0

            # Audit perbaikan cache tercatat di reconciliations
            rec_log = con.execute("SELECT * FROM reconciliations WHERE account_name='BCA Main' ORDER BY id DESC LIMIT 1").fetchone()
            assert rec_log is not None
            assert "Perbaikan cache saldo" in rec_log["notes"]

            # Akhir: E=C=A=100.000
            recon_after = reconstruct_account_balance(con, "BCA Main")
            assert recon_after["expected_balance"] == 100000.0
            assert recon_after["cached_balance"] == 100000.0
            assert recon_after["difference"] == 0.0
            print("[PASS] Test J2: E=100k, C=150k, A=100k -> tanpa tx keuangan -> cache diperbaiki ke 100k -> audit tercatat.")
    finally:
        cleanup_fixture(db_path)


def test_j3_reconcile_when_both_ledger_and_cache_differ_from_actual():
    """J3: E=100.000, C=150.000, A=130.000 -> adjustment ledger +30.000 -> cache akhir 130.000 -> akhir E=C=A=130.000."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 150000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("BCA Main", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )

            res = reconcile_account_balance(con, "BCA Main", 130000.0, "Penyesuaian ke saldo fisik 130k")
            assert res["action"] == "ledger_adjusted"
            assert res["difference"] == 30000.0
            assert res["adjustment_tx_id"] is not None

            # Transaksi dibuat sebesar +30.000
            tx = con.execute("SELECT * FROM transactions WHERE id=?", (res["adjustment_tx_id"],)).fetchone()
            assert tx["amount"] == 30000.0
            assert tx["transaction_type"] == "Adjustment"
            assert tx["account_to"] == "BCA Main"

            # Cache di-set langsung menjadi 130.000 (bukan 150k + 30k = 180k)
            current_bal = float(con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()[0])
            assert current_bal == 130000.0

            # Akhir: E=C=A=130.000
            recon_after = reconstruct_account_balance(con, "BCA Main")
            assert recon_after["expected_balance"] == 130000.0
            assert recon_after["cached_balance"] == 130000.0
            assert recon_after["difference"] == 0.0
            print("[PASS] Test J3: E=100k, C=150k, A=130k -> adjustment +30k -> cache akhir 130k -> akhir E=C=A=130k.")
    finally:
        cleanup_fixture(db_path)


def test_j4_reconcile_unverifiable_account():
    """J4: Akun tanpa anchor tepercaya ditolak rekonsiliasi biasa; izinkan penetapan anchor eksplisit tanpa fake tx."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Unknown Account", "Owned", 50000.0, "2026-08-22")
            )

            # Rekonsiliasi biasa tanpa force_anchor ditolak
            rejected = False
            try:
                reconcile_account_balance(con, "Unknown Account", 60000.0, "Audit fisik", force_anchor=False)
            except ValueError as err:
                rejected = True
                assert "belum memiliki anchor saldo" in str(err)
            assert rejected, "Test J4 failed: Reconciliation without anchor should be rejected"

            # Tidak ada transaksi keuangan palsu yang dibuat
            tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            assert tx_count == 0

            # Penetapan anchor eksplisit dengan force_anchor=True
            res = reconcile_account_balance(con, "Unknown Account", 60000.0, "Inisialisasi anchor fisik", force_anchor=True)
            assert res["action"] == "anchor_established"
            assert res["adjustment_tx_id"] is None

            # Tidak ada transaksi keuangan palsu
            assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

            # Snapshot baseline sekarang ada
            snap = con.execute("SELECT * FROM balance_snapshots WHERE account_name='Unknown Account'").fetchone()
            assert snap is not None
            assert snap["balance"] == 60000.0

            # Status rekonstruksi sekarang menjadi OK
            recon = reconstruct_account_balance(con, "Unknown Account")
            assert recon["status"] == "ok"
            assert recon["expected_balance"] == 60000.0
            assert recon["cached_balance"] == 60000.0
            print("[PASS] Test J4: Rekonsiliasi akun tanpa anchor ditolak biasa; penetapan anchor eksplisit berhasil tanpa tx palsu.")
    finally:
        cleanup_fixture(db_path)


def test_j5_atomic_reconciliation_rollback():
    """J5: Seluruh perubahan rekonsiliasi atomik. Jika terjadi kegagalan, semua data di-rollback."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 150000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("BCA Main", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            con.commit()

        # Uji transaksi atomik dalam blok with db_connect
        with db_connect(db_path) as con:
            failed = False
            try:
                # Simulasikan operasi rekonsiliasi yang gagal di tengah proses
                con.execute("BEGIN TRANSACTION")
                reconcile_account_balance(con, "BCA Main", 200000.0, "Test atomic failure")
                # Paksa exception sebelum commit
                raise RuntimeError("Simulated crash during reconciliation")
            except RuntimeError:
                con.rollback()
                failed = True
            assert failed, "Test J5 failed: Exception should be caught"

        # Periksa bahwa semua tabel kembali ke state awal
        with db_connect(db_path) as con:
            tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            assert tx_count == 0, f"Expected 0 transactions, found {tx_count}"

            rec_count = con.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0]
            assert rec_count == 0, f"Expected 0 reconciliations, found {rec_count}"

            snap_count = con.execute("SELECT COUNT(*) FROM balance_snapshots WHERE account_name='BCA Main'").fetchone()[0]
            assert snap_count == 1, f"Expected 1 initial snapshot, found {snap_count}"

            cur_bal = float(con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()[0])
            assert cur_bal == 150000.0, f"Expected current_balance 150000.0, found {cur_bal}"

            print("[PASS] Test J5: Atomisitas rekonsiliasi terbukti; rollback membatalkan transaksi, cache, snapshot, dan log.")
    finally:
        cleanup_fixture(db_path)


def run_all_tests():
    print("=" * 70)
    print("TEST SUITE: PROMPT 1B & 1C BACKEND CORRECTION & VERIFICATION")
    print("=" * 70)
    test_a_correct_cache()
    test_b_tampered_cache()
    test_c_account_without_snapshot()
    test_d_same_date_transaction()
    test_e_reconciliation_mutation()
    test_f_tolerance()
    test_g_production_endpoint_reconciliation()
    test_h_verify_balances_read_only()
    test_i_collision_and_ambiguous_ordering()
    print("-" * 70)
    print("RUNNING DRIFT & RECONCILIATION TEST SUITE (J1 - J5):")
    print("-" * 70)
    test_j1_reconcile_when_ledger_drifts_and_actual_matches_cache()
    test_j2_reconcile_when_cache_drifted_but_ledger_matches_actual()
    test_j3_reconcile_when_both_ledger_and_cache_differ_from_actual()
    test_j4_reconcile_unverifiable_account()
    test_j5_atomic_reconciliation_rollback()
    print("=" * 70)
    print("ALL 14 TEST FIXTURES (A - I, J1 - J5) PASSED CLEANLY!")
    print("=" * 70)


if __name__ == "__main__":
    run_all_tests()
