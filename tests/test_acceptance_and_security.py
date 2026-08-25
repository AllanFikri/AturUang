"""
Money Tracks V12 — Prompt 2 Acceptance & Security Test Suite
Contains Acceptance Tests (T-01, T-03, T-04, T-05, T-07, T-09, T-10)
and Reconciliation Security Tests (R-01, R-02, R-03, R-04, R-05).
Uses isolated production fixtures from db.py.
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
    dashboard,
    report_data,
    month_bounds,
    get_time_context,
    personal_expense_rows,
)


def create_isolated_fixture():
    """Creates an isolated temporary database with production schema from db.py."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    init_db(db_path)
    with db_connect(db_path) as con:
        con.execute("DELETE FROM balance_snapshots")
        con.execute("DELETE FROM transactions")
        con.execute("DELETE FROM reconciliations")
        con.execute("DELETE FROM upcoming")
        con.execute("DELETE FROM accounts")
    return db_path


def cleanup_fixture(db_path: Path):
    try:
        if db_path.exists():
            db_path.unlink(missing_ok=True)
    except Exception:
        pass


# =============================================================================
# B. Acceptance Tests (docs/money-tracks-spec.md §11)
# =============================================================================

def test_t01_basic_safe_to_spend_formula():
    """T-01: Rumus dasar Live Safe-to-Spend = Total Liquid Assets - Protected Savings - Current Commitments."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date, protected, protected_amount, active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("Cash", "Owned", 300000.0, "2026-08-23", 0, 0.0, 1)
            )
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date, protected, protected_amount, active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("BCA Tabungan", "Owned", 200000.0, "2026-08-23", 1, 150000.0, 1)
            )
            con.execute(
                "INSERT INTO upcoming(due_date, title, amount, category, status) VALUES (?, ?, ?, ?, ?)",
                ("2026-08-28", "Tagihan Listrik", 50000.0, "Utilities", "Upcoming")
            )

            dash = dashboard(con, "2026-08")
            total_bal = dash["kpis"]["totalBalance"]
            prot_sav = dash["kpis"]["protectedSavings"]
            commitments = dash["kpis"]["currentCommitments"]
            safe_to_spend = dash["kpis"]["safeToSpend"]

            assert total_bal == 500000.0, f"Expected totalBalance 500000, got {total_bal}"
            assert prot_sav == 150000.0, f"Expected protectedSavings 150000, got {prot_sav}"
            assert commitments == 50000.0, f"Expected currentCommitments 50000, got {commitments}"
            expected_sts = round(total_bal - prot_sav - commitments, 2)
            assert safe_to_spend == expected_sts, f"Expected safeToSpend {expected_sts}, got {safe_to_spend}"
            assert safe_to_spend == 300000.0
    finally:
        cleanup_fixture(db_path)


def test_t03_paying_obligation_preserves_sts():
    """T-03: Membayar kewajiban aktif tidak mengubah Safe-to-Spend (STS invariant)."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date, protected, protected_amount, active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("Cash", "Owned", 500000.0, "2026-08-23", 0, 0.0, 1)
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?, ?, ?, 'initial_anchor')",
                ("Cash", 500000.0, "2026-08-23")
            )
            cur = con.execute(
                "INSERT INTO upcoming(due_date, title, amount, category, status) VALUES (?, ?, ?, ?, ?)",
                ("2026-08-25", "Internet by.U", 70000.0, "Phone & Internet", "Upcoming")
            )
            up_id = cur.lastrowid

            dash_before = dashboard(con, "2026-08")
            sts_before = dash_before["kpis"]["safeToSpend"]
            assert sts_before == 430000.0  # 500k - 0 - 70k = 430k

            # Bayar kewajiban: Buat transaksi Expense, potong saldo akun, tandai upcoming Paid
            cur_tx = con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, category, money_context, status, budget_effect)
                   VALUES (?, ?, 'Expense', ?, ?, '', ?, ?, 'Personal', 'Confirmed', ?)""",
                ("2026-08-23", "12:00", 70000.0, "Cash", "Internet by.U", "Phone & Internet", 70000.0)
            )
            tx_id = cur_tx.lastrowid
            mutate_account_balance(con, "Cash", -70000.0, "2026-08-23")
            con.execute("UPDATE upcoming SET status='Paid', transaction_id=? WHERE id=?", (tx_id, up_id))

            dash_after = dashboard(con, "2026-08")
            sts_after = dash_after["kpis"]["safeToSpend"]
            assert sts_after == sts_before, f"STS changed from {sts_before} to {sts_after} after payment"
            assert sts_after == 430000.0
    finally:
        cleanup_fixture(db_path)


def test_t04_transfer_excluded_from_income_expense():
    """T-04: Transaksi Transfer tidak masuk agregasi pemasukan dan pengeluaran pribadi."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 500000.0, "2026-08-23")
            )
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-23")
            )
            con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, money_context, status)
                   VALUES (?, ?, 'Transfer', ?, ?, ?, ?, 'Personal', 'Confirmed')""",
                ("2026-08-23", "10:00", 50000.0, "BCA Main", "Cash", "Tarik Tunai ATM")
            )
            mutate_account_balance(con, "BCA Main", -50000.0, "2026-08-23")
            mutate_account_balance(con, "Cash", +50000.0, "2026-08-23")

            dash = dashboard(con, "2026-08")
            total_inc = dash["kpis"]["income"]
            total_exp = dash["kpis"]["spent"]
            assert total_inc == 0.0, f"Transfer counted as income in dashboard: {total_inc}"
            assert total_exp == 0.0, f"Transfer counted as expense in dashboard: {total_exp}"

            rep = report_data(con, "2026-08")
            m_report = next((m for m in rep["monthly"] if m["month"] == "2026-08"), {"income": 0.0, "expense": 0.0})
            assert m_report["income"] == 0.0, f"Transfer counted as income in report: {m_report['income']}"
            assert m_report["expense"] == 0.0, f"Transfer counted as expense in report: {m_report['expense']}"

            exp_rows = personal_expense_rows(con, "2026-08")
            assert len(exp_rows) == 0, f"Transfer found in personal expense rows: {exp_rows}"
    finally:
        cleanup_fixture(db_path)


def test_t05_passthrough_excluded_from_personal_income():
    """T-05: Titipan (Pass-through) tidak menjadi pemasukan pribadi."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 200000.0, "2026-08-23")
            )
            con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, money_context, status)
                   VALUES (?, ?, 'Income', ?, '', ?, ?, 'Pass-through', 'Confirmed')""",
                ("2026-08-23", "11:00", 100000.0, "Cash", "Titipan Pembelian Barang Teman")
            )
            mutate_account_balance(con, "Cash", +100000.0, "2026-08-23")

            dash = dashboard(con, "2026-08")
            total_inc = dash["kpis"]["income"]
            assert total_inc == 0.0, f"Pass-through money counted as personal income: {total_inc}"

            rep = report_data(con, "2026-08")
            m_report = next((m for m in rep["monthly"] if m["month"] == "2026-08"), {"income": 0.0, "expense": 0.0})
            assert m_report["income"] == 0.0, f"Pass-through counted as personal income in report: {m_report['income']}"
    finally:
        cleanup_fixture(db_path)


def test_t07_cache_verified_from_anchor_plus_mutations():
    """T-07: Saldo cache dapat diverifikasi dari snapshot anchor + mutasi transaksi."""
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
                   VALUES (?, ?, 'Income', ?, '', 'Cash', 'Gaji Lepas', '2026-08-22 10:00:00')""",
                ("2026-08-22", "10:00", 50000.0)
            )
            mutate_account_balance(con, "Cash", +50000.0, "2026-08-22")

            con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, created_at)
                   VALUES (?, ?, 'Expense', ?, 'Cash', '', 'Makan Siang', '2026-08-22 13:00:00')""",
                ("2026-08-22", "13:00", 20000.0)
            )
            mutate_account_balance(con, "Cash", -20000.0, "2026-08-22")

            recon = reconstruct_account_balance(con, "Cash")
            assert recon["status"] == "ok"
            assert recon["expected_balance"] == 130000.0
            assert recon["cached_balance"] == 130000.0
            assert recon["difference"] == 0.0

            issues = verify_balances(con)
            assert len(issues) == 0, f"Discrepancy reported: {issues}"
    finally:
        cleanup_fixture(db_path)


def test_t09_month_bounds_wib_timezone():
    """T-09: Batas bulan dan konteks waktu mengikuti zona waktu lokal WIB (Asia/Jakarta UTC+7)."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            start, end = month_bounds("2026-08")
            assert start == dt.date(2026, 8, 1)
            assert end == dt.date(2026, 8, 31)

            tctx = get_time_context(con)
            assert "WIB" in tctx["formatted_wib"] or "+07:00" in tctx["formatted_wib"] or "Asia/Jakarta" in str(tctx)
            assert tctx["days_in_month"] == 31
    finally:
        cleanup_fixture(db_path)


def test_t10_float_rounding_and_tolerance():
    """T-10: Nominal REAL dibulatkan dua desimal dan toleransi 0.005 dipatuhi."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-23")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("Cash", 100000.0, "2026-08-23", "2026-08-23 00:00:00")
            )

            # Selisih 0.0049 diabaikan
            res1 = reconcile_account_balance(con, "Cash", 100000.0049, "Diff 0.0049")
            assert res1["adjustment_tx_id"] is None

            # Selisih 0.005 diproses
            res2 = reconcile_account_balance(con, "Cash", 100000.005, "Diff 0.005")
            assert res2["adjustment_tx_id"] is not None
    finally:
        cleanup_fixture(db_path)


# =============================================================================
# C. Reconciliation Security Tests
# =============================================================================

def test_r01_reconciliation_does_not_pollute_reports():
    """R-01: Rekonsiliasi saldo tidak merusak laporan (income, expense, budget_effect, savings_rate)."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            # Setup Account & Base Plan
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 100000.0, "2026-08-23")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("BCA Main", 100000.0, "2026-08-23", "2026-08-23 00:00:00")
            )
            con.execute(
                "INSERT INTO monthly_plans(month, guaranteed_income, expected_additional_income, savings_rate) VALUES (?, ?, ?, ?)",
                ("2026-08", 5000000.0, 1000000.0, 0.20)
            )

            # Laporan awal sebelum rekonsiliasi
            dash_before = dashboard(con, "2026-08")
            inc_before = dash_before["kpis"]["income"]
            spent_before = dash_before["budget_status"]["spent"] if "budget_status" in dash_before else dash_before["kpis"]["spent"]
            plan_before = dash_before["kpis"]["savingsTarget"]
            sts_before = dash_before["kpis"]["safeToSpend"]
            assert sts_before == 100000.0

            # 1. Rekonsiliasi Arah Positif: A = 150.000 (Adjustment +50.000)
            res_pos = reconcile_account_balance(con, "BCA Main", 150000.0, "Selisih fisik positif")
            assert res_pos["action"] == "ledger_adjusted"
            assert res_pos["difference"] == 50000.0
            assert res_pos["adjustment_tx_id"] is not None

            # Assert Transaksi
            tx_row = con.execute("SELECT * FROM transactions WHERE id=?", (res_pos["adjustment_tx_id"],)).fetchone()
            assert tx_row["transaction_type"] == "Adjustment"
            assert tx_row["amount"] == 50000.0
            assert tx_row["account_to"] == "BCA Main"
            assert tx_row["account_from"] == ""
            assert tx_row["subtype"] == "Balance Reconciliation"
            assert float(tx_row["budget_effect"]) == 0.0

            # Assert Saldo & STS
            cur_bal = float(con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()[0])
            assert cur_bal == 150000.0
            recon_pos = reconstruct_account_balance(con, "BCA Main")
            assert recon_pos["expected_balance"] == 150000.0
            assert recon_pos["cached_balance"] == 150000.0
            assert len(verify_balances(con)) == 0

            # Assert Laporan & Anggaran: Pemasukan pribadi TIDAK boleh bertambah karena ini transaksi penyesuaian
            dash_pos = dashboard(con, "2026-08")
            assert dash_pos["kpis"]["income"] == inc_before, f"Personal income polluted: was {inc_before}, became {dash_pos['kpis']['income']}"
            spent_pos = dash_pos["budget_status"]["spent"] if "budget_status" in dash_pos else dash_pos["kpis"]["spent"]
            assert spent_pos == spent_before
            assert dash_pos["kpis"]["savingsTarget"] == plan_before
            assert dash_pos["kpis"]["safeToSpend"] == 150000.0

            # 2. Rekonsiliasi Arah Negatif: A = 130.000 (Adjustment -20.000)
            res_neg = reconcile_account_balance(con, "BCA Main", 130000.0, "Selisih fisik negatif")
            assert res_neg["action"] == "ledger_adjusted"
            assert res_neg["difference"] == -20000.0

            tx_neg = con.execute("SELECT * FROM transactions WHERE id=?", (res_neg["adjustment_tx_id"],)).fetchone()
            assert tx_neg["transaction_type"] == "Adjustment"
            assert tx_neg["amount"] == 20000.0
            assert tx_neg["account_from"] == "BCA Main"
            assert tx_neg["account_to"] == ""
            assert tx_neg["subtype"] == "Balance Reconciliation"
            assert float(tx_neg["budget_effect"]) == 0.0

            # Assert Laporan & STS
            dash_neg = dashboard(con, "2026-08")
            assert dash_neg["kpis"]["income"] == inc_before
            spent_neg = dash_neg["budget_status"]["spent"] if "budget_status" in dash_neg else dash_neg["kpis"]["spent"]
            assert spent_neg == spent_before
            assert dash_neg["kpis"]["savingsTarget"] == plan_before
            assert dash_neg["kpis"]["safeToSpend"] == 130000.0
            assert len(verify_balances(con)) == 0
    finally:
        cleanup_fixture(db_path)


def test_r02_force_anchor_on_snapshot_collision():
    """R-02: force_anchor=True pada akun dengan snapshot collision harus memperbarui status menjadi ok."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            # Akun memiliki snapshot legacy lama
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("Cash", "Owned", 100000.0, "2026-08-22")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'legacy')",
                ("Cash", 100000.0, "2026-08-22", "2026-08-22 00:00:00")
            )
            legacy_snap_id = con.execute("SELECT id FROM balance_snapshots WHERE account_name='Cash'").fetchone()[0]

            # Transaksi collision dengan created_at PERSIS SAMA
            con.execute(
                """INSERT INTO transactions(date, time, transaction_type, amount, account_from, account_to, description, created_at)
                   VALUES (?, ?, 'Income', 50000.0, '', 'Cash', 'Collision Tx', '2026-08-22 00:00:00')""",
                ("2026-08-22", "00:00")
            )

            # Status awal harus unverifiable karena legacy snapshot collision
            recon_before = reconstruct_account_balance(con, "Cash")
            assert recon_before["status"] == "unverifiable"
            assert "urutan transaksi dan snapshot pada waktu anchor tidak dapat dipastikan" in recon_before["reason"]

            # Pengguna menetapkan explicit trusted anchor dengan force_anchor=True (A = 150.000)
            res = reconcile_account_balance(con, "Cash", 150000.0, "Penetapan ulang anchor terpercaya", force_anchor=True)
            assert res["action"] == "anchor_established"

            # Snapshot ambigu lama tidak dihapus (tetap ada 2 snapshot)
            snaps = con.execute("SELECT id, balance, snapshot_kind FROM balance_snapshots WHERE account_name='Cash' ORDER BY id ASC").fetchall()
            assert len(snaps) == 2, f"Expected 2 snapshots preserved, found {len(snaps)}"
            assert snaps[0]["id"] == legacy_snap_id
            assert snaps[0]["snapshot_kind"] == "legacy"
            assert snaps[1]["snapshot_kind"] == "manual_anchor"
            manual_anchor_id = snaps[1]["id"]

            # Rekonstruksi setelahnya diharapkan status ok dan memilih manual_anchor
            recon_after = reconstruct_account_balance(con, "Cash")
            assert recon_after["status"] == "ok", f"Expected status 'ok', got '{recon_after['status']}' ({recon_after.get('reason')})"
            assert recon_after["expected_balance"] == 150000.0
            assert recon_after["cached_balance"] == 150000.0
            assert recon_after["difference"] == 0.0

            # E = C = A
            current_bal = float(con.execute("SELECT current_balance FROM accounts WHERE name='Cash'").fetchone()[0])
            assert recon_after["expected_balance"] == current_bal == 150000.0

        # Restart / init_db ulang tidak mengubah pemilihan anchor
        init_db(db_path)
        with db_connect(db_path) as con:
            recon_restart = reconstruct_account_balance(con, "Cash")
            assert recon_restart["status"] == "ok"
            assert recon_restart["expected_balance"] == 150000.0
            assert recon_restart["cached_balance"] == 150000.0
    finally:
        cleanup_fixture(db_path)


def test_r02b_migration_and_rollback_on_legacy_schema():
    """R-02b: Migrasi skema balance_snapshots pra-migrasi menandai snapshot lama 'legacy' dan rollback bekerja."""
    from db import rollback_balance_snapshots_migration

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    try:
        # Buat database dengan skema PRA-MIGRASI (tanpa snapshot_kind)
        with sqlite3.connect(db_path) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, current_balance REAL, balance_date TEXT, kind TEXT NOT NULL DEFAULT 'Owned', protected INTEGER NOT NULL DEFAULT 0, protected_amount REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '')")
            con.execute("CREATE TABLE balance_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, account_name TEXT NOT NULL, balance REAL NOT NULL, snapshot_date TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(account_name) REFERENCES accounts(name))")
            con.execute("INSERT INTO accounts(name, current_balance, balance_date) VALUES ('BCA Legacy', 500000.0, '2026-08-20')")
            con.execute("INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at) VALUES ('BCA Legacy', 500000.0, '2026-08-20', '2026-08-20 10:00:00')")
            con.commit()

        # Jalankan init_db untuk memicu migrasi
        init_db(db_path)

        with db_connect(db_path) as con:
            # Periksa kolom snapshot_kind ditambahkan dan data lama bernilai 'legacy'
            snap = con.execute("SELECT * FROM balance_snapshots WHERE account_name='BCA Legacy'").fetchone()
            assert snap["snapshot_kind"] == "legacy"
            assert float(snap["balance"]) == 500000.0
            assert snap["snapshot_date"] == "2026-08-20"

            # Rekonstruksi pada legacy data tetap berjalan
            recon = reconstruct_account_balance(con, "BCA Legacy")
            assert recon["status"] == "ok"
            assert recon["expected_balance"] == 500000.0

        # Uji rollback migrasi
        rollback_balance_snapshots_migration(db_path)
        with sqlite3.connect(db_path) as con:
            cols = [r[1] for r in con.execute("PRAGMA table_info(balance_snapshots)").fetchall()]
            assert "snapshot_kind" not in cols, "snapshot_kind column should be removed on rollback"
            count = con.execute("SELECT COUNT(*) FROM balance_snapshots WHERE account_name='BCA Legacy'").fetchone()[0]
            assert count == 1, "Original snapshot record must be preserved on rollback"
    finally:
        cleanup_fixture(db_path)


def test_r03_establish_anchor_for_account_without_snapshot():
    """R-03: Penetapan anchor akun tanpa snapshot tidak membuat Income/Expense palsu, membuat audit log, status ok."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("GoPay", "Owned", 50000.0, "2026-08-23")
            )
            # Tanpa snapshot -> status unverifiable
            assert reconstruct_account_balance(con, "GoPay")["status"] == "unverifiable"

            # Panggil force_anchor=True
            res = reconcile_account_balance(con, "GoPay", 75000.0, "Inisialisasi anchor GoPay", force_anchor=True)
            assert res["action"] == "anchor_established"

            # Tidak ada transaksi Income/Expense
            tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            assert tx_count == 0, f"Expected 0 transactions, found {tx_count}"

            # Audit log tercatat
            rec = con.execute("SELECT * FROM reconciliations WHERE account_name='GoPay'").fetchone()
            assert rec is not None
            assert "Penetapan anchor" in rec["notes"]

            # Rekonstruksi setelahnya status ok
            recon = reconstruct_account_balance(con, "GoPay")
            assert recon["status"] == "ok"
            assert recon["expected_balance"] == 75000.0
            assert recon["cached_balance"] == 75000.0
    finally:
        cleanup_fixture(db_path)


def test_r04_atomicity_of_reconciliation_workflow():
    """R-04: Atomisitas jalur rekonsiliasi. Rollback membatalkan semua mutasi jika terjadi kegagalan."""
    db_path = create_isolated_fixture()
    try:
        with db_connect(db_path) as con:
            con.execute(
                "INSERT INTO accounts(name, kind, current_balance, balance_date) VALUES (?, ?, ?, ?)",
                ("BCA Main", "Owned", 100000.0, "2026-08-23")
            )
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES (?, ?, ?, ?, 'initial_anchor')",
                ("BCA Main", 100000.0, "2026-08-23", "2026-08-23 00:00:00")
            )
            con.commit()

        with db_connect(db_path) as con:
            failed = False
            try:
                con.execute("BEGIN TRANSACTION")
                reconcile_account_balance(con, "BCA Main", 200000.0, "Atomic test")
                raise RuntimeError("Simulated crash in reconciliation workflow")
            except RuntimeError:
                con.rollback()
                failed = True
            assert failed

        with db_connect(db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 1
            assert float(con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()[0]) == 100000.0
    finally:
        cleanup_fixture(db_path)


def test_r05_backup_before_production_reconcile():
    """R-05: Audit pemanggilan backup_db() sebelum rekonsiliasi pada route /api/reconcile."""
    with open("money_tracks_server.py", "r", encoding="utf-8") as f:
        content = f.read()

    rec_pos = content.find('if path == "/api/reconcile":')
    assert rec_pos != -1, "Route /api/reconcile not found in money_tracks_server.py"
    rec_block = content[rec_pos:rec_pos + 400]
    
    assert "backup_db()" in rec_block, "backup_db() is not called in /api/reconcile route"
    backup_pos = rec_block.find("backup_db()")
    service_pos = rec_block.find("reconcile_account_balance(")
    assert backup_pos < service_pos, "backup_db() must be called BEFORE reconcile_account_balance"


# =============================================================================
# Runner
# =============================================================================

def run_all():
    tests = [
        ("T-01 Rumus dasar STS", test_t01_basic_safe_to_spend_formula),
        ("T-03 Membayar kewajiban tidak mengubah STS", test_t03_paying_obligation_preserves_sts),
        ("T-04 Transfer dikecualikan dari pemasukan/pengeluaran", test_t04_transfer_excluded_from_income_expense),
        ("T-05 Titipan dikecualikan dari pemasukan pribadi", test_t05_passthrough_excluded_from_personal_income),
        ("T-07 Saldo cache diverifikasi dari anchor + mutasi", test_t07_cache_verified_from_anchor_plus_mutations),
        ("T-09 Batas bulan mengikuti WIB", test_t09_month_bounds_wib_timezone),
        ("T-10 Toleransi selisih float 0.005", test_t10_float_rounding_and_tolerance),
        ("R-01 Rekonsiliasi tidak merusak laporan", test_r01_reconciliation_does_not_pollute_reports),
        ("R-02 force_anchor pada snapshot collision", test_r02_force_anchor_on_snapshot_collision),
        ("R-02b Migrasi dan rollback snapshot_kind", test_r02b_migration_and_rollback_on_legacy_schema),
        ("R-03 Penetapan anchor akun tanpa snapshot", test_r03_establish_anchor_for_account_without_snapshot),
        ("R-04 Atomisitas rekonsiliasi produksi", test_r04_atomicity_of_reconciliation_workflow),
        ("R-05 Backup sebelum rekonsiliasi", test_r05_backup_before_production_reconcile),
    ]

    passed = []
    failed = []

    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"[PASS] {name}")
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[FAIL] {name} -> {e}")

    return passed, failed


if __name__ == "__main__":
    run_all()
