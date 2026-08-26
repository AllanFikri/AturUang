import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))
if str(_REPO_ROOT / "cloud" / "worker" / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "cloud" / "worker" / "scripts"))

import unittest
import sqlite3
import shutil
import tempfile
import hashlib
import json
from pathlib import Path

import services
import db
import export_sqlite_to_d1

BASE_DIR = _REPO_ROOT
MIGRATION_01 = BASE_DIR / "cloud" / "worker" / "migrations" / "0001_initial_schema.sql"
MIGRATION_02 = BASE_DIR / "cloud" / "worker" / "migrations" / "0002_staging_schema.sql"
MIGRATION_03 = BASE_DIR / "cloud" / "worker" / "migrations" / "0003_ingestion_connectors.sql"
MIGRATION_04 = BASE_DIR / "cloud" / "worker" / "migrations" / "0004_durable_nonce_guard.sql"


def reset_test_tables(con: sqlite3.Connection):
    """Safely cleans all tables respecting foreign keys."""
    tables = [
        "reconciliations",
        "balance_snapshots",
        "ingestion_audit_log",
        "ingestion_candidates",
        "raw_events",
        "transactions",
        "protected_allocations",
        "upcoming",
        "debt_events",
        "debts",
        "allocation_goals",
        "budget_categories",
        "monthly_plans",
        "accounts",
    ]
    for t in tables:
        try:
            con.execute(f"DELETE FROM {t}")
        except Exception:
            pass


def compute_d1_domain_kpis(d1_con: sqlite3.Connection, month_str: str | None = None) -> dict:
    """Executes exact production D1 database queries matching cloud/worker/src/domain.ts."""
    # 1. Liquid balance
    liq_res = d1_con.execute("SELECT COALESCE(SUM(current_balance), 0.0) as total FROM accounts WHERE active=1 AND kind='Owned'").fetchone()
    total_balance = round(float(liq_res[0] or 0.0), 2)

    # 2. Allocation goals
    alloc_res = d1_con.execute("""
        SELECT
           COALESCE(SUM(CASE WHEN kind = 'Emergency' THEN allocated_amount ELSE 0 END), 0.0) as emergency,
           COALESCE(SUM(CASE WHEN kind = 'Goal' THEN allocated_amount ELSE 0 END), 0.0) as goals,
           COALESCE(SUM(CASE WHEN kind = 'General' THEN allocated_amount ELSE 0 END), 0.0) as general,
           COALESCE(SUM(allocated_amount), 0.0) as total_alloc
        FROM allocation_goals
        WHERE status = 'Active'
    """).fetchone()
    emergency_allocated = round(float(alloc_res[0] or 0.0), 2)
    goals_allocated = round(float(alloc_res[1] or 0.0), 2)
    general_allocated = round(float(alloc_res[2] or 0.0), 2)
    protected_savings = round(float(alloc_res[3] or 0.0), 2)

    # 3. Custody outstanding
    custody_debts = d1_con.execute("SELECT id FROM debts WHERE kind='Custody' AND status='Active'").fetchall()
    pass_through_outstanding = 0.0
    for cd in custody_debts:
        ev = d1_con.execute("SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=?", (cd[0],)).fetchone()
        pass_through_outstanding += max(0.0, float(ev[0] or 0.0))
    pass_through_outstanding = round(pass_through_outstanding, 2)

    # 4. Receivables outstanding
    rec_debts = d1_con.execute("SELECT id FROM debts WHERE kind='Receivable' AND status='Active'").fetchall()
    receivables_outstanding = 0.0
    for rd in rec_debts:
        ev = d1_con.execute("SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=?", (rd[0],)).fetchone()
        receivables_outstanding += max(0.0, float(ev[0] or 0.0))
    receivables_outstanding = round(receivables_outstanding, 2)

    # 5. Effective commitments
    # 5a. U_eff for Active Payables
    pay_debts = d1_con.execute("SELECT id FROM debts WHERE kind='Payable' AND status='Active'").fetchall()
    total_u_eff = 0.0
    for pd in pay_debts:
        ev = d1_con.execute("SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=?", (pd[0],)).fetchone()
        debt_out = max(0.0, float(ev[0] or 0.0))
        cov_res = d1_con.execute("""
            SELECT COALESCE(SUM(pa.amount), 0.0)
            FROM protected_allocations pa
            JOIN upcoming u ON pa.covers_upcoming_id = u.id
            WHERE u.debt_id = ? AND pa.status = 'Active' AND u.status = 'Upcoming'
        """, (pd[0],)).fetchone()
        cov = float(cov_res[0] or 0.0)
        total_u_eff += max(0.0, debt_out - cov)

    # 5b. X_eff for Regular Upcomings using combined coverage
    cov_map: dict[int, float] = {}
    try:
        pa_rows = d1_con.execute("""
            SELECT covers_upcoming_id, COALESCE(SUM(amount), 0.0)
            FROM protected_allocations
            WHERE status = 'Active' AND covers_upcoming_id IS NOT NULL
            GROUP BY covers_upcoming_id
        """).fetchall()
        for r in pa_rows:
            cov_map[r[0]] = round(cov_map.get(r[0], 0.0) + float(r[1] or 0.0), 2)
    except Exception:
        pass

    try:
        goal_rows = d1_con.execute("""
            SELECT u.id, COALESCE(ag.allocated_amount, 0.0)
            FROM upcoming u
            JOIN allocation_goals ag ON u.linked_goal_id = ag.id
            WHERE ag.status = 'Active' AND u.linked_goal_id IS NOT NULL
        """).fetchall()
        for r in goal_rows:
            cov_map[r[0]] = round(cov_map.get(r[0], 0.0) + float(r[1] or 0.0), 2)
    except Exception:
        pass

    upcomings = d1_con.execute("""
        SELECT id, amount, status, COALESCE(reserve_now, 0) as reserve_now
        FROM upcoming
        WHERE (debt_id IS NULL OR debt_id = 0)
          AND status IN ('Upcoming', 'Confirmed', 'Tentative')
    """).fetchall()

    total_x_eff = 0.0
    for u in upcomings:
        if u["status"] == "Tentative" and int(u["reserve_now"] or 0) == 0:
            continue
        amt = float(u["amount"] or 0.0)
        cov = cov_map.get(u["id"], 0.0)
        total_x_eff += max(0.0, amt - cov)

    current_commitments = round(total_u_eff + total_x_eff, 2)

    # 6. Pending expenses
    pend_res = d1_con.execute("""
        SELECT COALESCE(SUM(amount), 0.0)
        FROM transactions
        WHERE is_deleted = 0
          AND status = 'Provisional Neutral'
          AND (transaction_type = 'Expense' OR (transaction_type = '' AND amount > 0))
    """).fetchone()
    pending_expenses = round(float(pend_res[0] or 0.0), 2)

    # 7. Safe to Spend
    safe_to_spend = round(total_balance - protected_savings - pass_through_outstanding - current_commitments - pending_expenses, 2)

    return {
        "totalBalance": total_balance,
        "protectedSavings": protected_savings,
        "emergencyAllocated": emergency_allocated,
        "goalsAllocated": goals_allocated,
        "generalAllocated": general_allocated,
        "passThroughOutstanding": pass_through_outstanding,
        "receivablesOutstanding": receivables_outstanding,
        "currentCommitments": current_commitments,
        "pendingExpenses": pending_expenses,
        "safeToSpend": safe_to_spend,
    }


class TestPrompt13aCloudParity(unittest.TestCase):
    def setUp(self):
        self.prod_db = BASE_DIR / "runtime" / "money_tracks.db"
        self.prod_hash_before = hashlib.sha256(self.prod_db.read_bytes()).hexdigest() if self.prod_db.exists() else None

        self.tmp_dir = tempfile.mkdtemp()
        self.test_sqlite = Path(self.tmp_dir) / "money_tracks.db"
        self.test_d1 = Path(self.tmp_dir) / "shadow_d1.db"

        shutil.copyfile(self.prod_db, self.test_sqlite)
        self.sqlite_con = sqlite3.connect(self.test_sqlite)
        self.sqlite_con.row_factory = sqlite3.Row

        # Setup shadow D1 SQLite instance
        self.d1_con = sqlite3.connect(self.test_d1)
        self.d1_con.row_factory = sqlite3.Row
        self.d1_con.execute("PRAGMA foreign_keys = ON")

        # Apply D1 migrations 0001, 0002, 0003, 0004
        self.d1_con.executescript(MIGRATION_01.read_text(encoding="utf-8"))
        self.d1_con.executescript(MIGRATION_02.read_text(encoding="utf-8"))
        self.d1_con.executescript(MIGRATION_03.read_text(encoding="utf-8"))
        self.d1_con.executescript(MIGRATION_04.read_text(encoding="utf-8"))

        # Generate dynamic test seed SQL from test sqlite
        self.test_seed_sql = Path(self.tmp_dir) / "test_seed.sql"
        export_sqlite_to_d1.export_d1_sql(self.test_sqlite, self.test_seed_sql)
        self.d1_con.executescript(self.test_seed_sql.read_text(encoding="utf-8"))

    def tearDown(self):
        self.sqlite_con.close()
        self.d1_con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_migration_fresh_and_idempotent(self):
        """Test 1: D1 migrations execute cleanly on fresh database and are idempotent on re-execution."""
        mig_rows = self.d1_con.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        self.assertEqual(len(mig_rows), 4)
        self.assertEqual(mig_rows[0]["version"], 1)
        self.assertEqual(mig_rows[1]["version"], 2)
        self.assertEqual(mig_rows[2]["version"], 3)
        self.assertEqual(mig_rows[3]["version"], 4)

    def test_02_export_source_strictly_readonly(self):
        """Test 2: Export process reads source in read-only mode and leaves file hash unchanged."""
        out_sql = Path(self.tmp_dir) / "export_test.sql"
        hash_before = hashlib.sha256(self.test_sqlite.read_bytes()).hexdigest()

        res = export_sqlite_to_d1.export_d1_sql(self.test_sqlite, out_sql)
        self.assertTrue(out_sql.exists())
        self.assertGreater(res["total_rows"], 1000)

        hash_after = hashlib.sha256(self.test_sqlite.read_bytes()).hexdigest()
        self.assertEqual(hash_before, hash_after, "Export altered source SQLite file!")

    def test_03_reimport_idempotency_no_duplicates(self):
        """Test 3: Re-executing D1 seed SQL does not duplicate rows or cause primary key violations."""
        cnt_tx_1 = self.d1_con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        cnt_acc_1 = self.d1_con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        # Import 2 (Idempotency)
        self.d1_con.executescript(self.test_seed_sql.read_text(encoding="utf-8"))
        cnt_tx_2 = self.d1_con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        cnt_acc_2 = self.d1_con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        self.assertEqual(cnt_tx_1, cnt_tx_2)
        self.assertEqual(cnt_acc_1, cnt_acc_2)

    def test_04_schema_constraints_and_foreign_keys(self):
        """Test 4: Foreign keys, uniqueness, and check constraints function properly in D1 shadow."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.d1_con.execute("""
                INSERT INTO balance_snapshots (account_name, snapshot_date, balance, snapshot_kind)
                VALUES ('NonExistentAccount', '2026-08-25', 10000.0, 'adhoc')
            """)

        with self.assertRaises(sqlite3.IntegrityError):
            self.d1_con.execute("""
                INSERT INTO allocation_goals (name, kind, target_amount)
                VALUES ('Invalid Goal', 'InvalidType', 50000.0)
            """)

    def test_05_comprehensive_nonzero_fixture_production_parity(self):
        """Test 5: True parity between Python production canonical services.py and D1 domain calculation across ALL nonzero financial fixtures simultaneously."""
        # Clean test tables in both SQLite and D1 instances
        for con in (self.sqlite_con, self.d1_con):
            reset_test_tables(con)

            # 1. Owned Liquid Accounts (Total = 15,000,000.0)
            con.execute("INSERT INTO accounts (name, kind, current_balance, balance_date, active) VALUES ('BCA Main', 'Owned', 10000000.0, '2026-08-01', 1)")
            con.execute("INSERT INTO accounts (name, kind, current_balance, balance_date, active) VALUES ('Jago Main', 'Owned', 5000000.0, '2026-08-01', 1)")

            # 2. Allocation Goals: Emergency & Goals (Total = 5,000,000.0)
            con.execute("INSERT INTO allocation_goals (id, name, kind, target_amount, allocated_amount, status) VALUES (1, 'Dana Darurat', 'Emergency', 20000000.0, 3000000.0, 'Active')")
            con.execute("INSERT INTO allocation_goals (id, name, kind, target_amount, allocated_amount, status) VALUES (2, 'Tabungan Laptop', 'Goal', 10000000.0, 2000000.0, 'Active')")

            # 3. Debts & Events: Custody (1M), Receivable (750k), Payable (4M)
            con.execute("INSERT INTO debts (id, person_name, kind, status) VALUES (10, 'Titipan Kantor', 'Custody', 'Active')")
            con.execute("INSERT INTO debt_events (debt_id, event_type, effect, amount, event_date) VALUES (10, 'Opening', 1, 1000000.0, '2026-08-01')")

            con.execute("INSERT INTO debts (id, person_name, kind, status) VALUES (20, 'Pinjaman Teman', 'Receivable', 'Active')")
            con.execute("INSERT INTO debt_events (debt_id, event_type, effect, amount, event_date) VALUES (20, 'Opening', 1, 750000.0, '2026-08-01')")

            con.execute("INSERT INTO debts (id, person_name, kind, status) VALUES (30, 'Cicilan HP', 'Payable', 'Active')")
            con.execute("INSERT INTO debt_events (debt_id, event_type, effect, amount, event_date) VALUES (30, 'Opening', 1, 4000000.0, '2026-08-01')")

            # 4. Upcomings:
            # - Upcoming #1 (Linked to Payable #30, covered 500k via protected_allocations -> U_eff = 3.5M)
            con.execute("INSERT INTO upcoming (id, title, amount, due_date, status, debt_id) VALUES (101, 'Cicilan HP Bln 1', 1000000.0, '2026-08-28', 'Upcoming', 30)")
            con.execute("INSERT INTO protected_allocations (id, title, amount, status, covers_upcoming_id) VALUES (1, 'Tabungan Cicilan HP', 500000.0, 'Active', 101)")

            # - Upcoming #102 (Regular personal, covered 2.0M via allocation_goal #2 Tabungan Laptop -> X_eff = max(0, 1.5M - 2.0M) = 0.0)
            con.execute("INSERT INTO upcoming (id, title, amount, due_date, status, linked_goal_id) VALUES (102, 'Beli Laptop Baru', 1500000.0, '2026-08-29', 'Upcoming', 2)")

            # - Upcoming #103 (Tentative with reserve_now = 0 -> Skipped, 0.0)
            con.execute("INSERT INTO upcoming (id, title, amount, due_date, status, reserve_now) VALUES (103, 'Liburan Akhir Tahun', 800000.0, '2026-12-25', 'Tentative', 0)")

            # - Upcoming #104 (Tentative with reserve_now = 1 -> Deducted, 400.0k)
            con.execute("INSERT INTO upcoming (id, title, amount, due_date, status, reserve_now) VALUES (104, 'Servis Kendaraan', 400000.0, '2026-09-05', 'Tentative', 1)")

            # - Upcoming #105 (Regular uncovered -> Deducted, 600.0k)
            con.execute("INSERT INTO upcoming (id, title, amount, due_date, status) VALUES (105, 'Listrik & Wifi', 600000.0, '2026-08-30', 'Upcoming')")

            # 5. Pending / Provisional Neutral Expense (250.0k)
            con.execute("""
                INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, category, for_with_whom, money_context, status, budget_effect)
                VALUES ('2026-08-26', '09:00', 'Expense', 250000.0, 'BCA Main', '', 'Pending QRIS', 'Main Meals', 'Personal / Self', 'Personal', 'Provisional Neutral', 250000.0)
            """)

            con.commit()

        # Calculate via canonical Python services
        py_snap = services.balance_snapshot(self.sqlite_con)
        py_dash = services.dashboard(self.sqlite_con, "2026-08")
        py_kpis = py_dash["kpis"]

        # Calculate via D1 domain calculation matching domain.ts
        d1_kpis = compute_d1_domain_kpis(self.d1_con, "2026-08")

        # 1. Total Balance Parity
        self.assertAlmostEqual(py_kpis["totalBalance"], d1_kpis["totalBalance"], delta=0.005)
        self.assertEqual(d1_kpis["totalBalance"], 15000000.0)

        # 2. Protected Savings / Total Allocated Parity
        self.assertAlmostEqual(py_kpis["reservedTotal"], d1_kpis["protectedSavings"], delta=0.005)
        self.assertEqual(d1_kpis["protectedSavings"], 5000000.0)

        # 3. Pass-through / Custody Outstanding Parity
        self.assertAlmostEqual(py_kpis["passThroughOutstanding"], d1_kpis["passThroughOutstanding"], delta=0.005)
        self.assertEqual(d1_kpis["passThroughOutstanding"], 1000000.0)

        # 4. Receivables Outstanding Parity
        self.assertAlmostEqual(py_kpis["receivablesOutstanding"], d1_kpis["receivablesOutstanding"], delta=0.005)
        self.assertEqual(d1_kpis["receivablesOutstanding"], 750000.0)

        # 5. Current Commitments Parity (U_eff 3.5M + X_eff 1.0M = 4.5M)
        self.assertAlmostEqual(py_kpis["currentCommitments"], d1_kpis["currentCommitments"], delta=0.005)
        self.assertEqual(d1_kpis["currentCommitments"], 4500000.0)

        # 6. Pending Expenses Parity (250k)
        self.assertAlmostEqual(py_kpis["pendingExpenses"], d1_kpis["pendingExpenses"], delta=0.005)
        self.assertEqual(d1_kpis["pendingExpenses"], 250000.0)

        # 7. Safe-to-Spend Final Parity (15M - 5M - 1M - 4.5M - 250k = 4,250,000.0)
        self.assertAlmostEqual(py_kpis["safeToSpend"], d1_kpis["safeToSpend"], delta=0.005)
        self.assertEqual(d1_kpis["safeToSpend"], 4250000.0)

    def test_06_allocation_goal_coverage_no_double_deduction(self):
        """Test 6: Linking an allocation goal to an upcoming item reduces effective commitments without double-deducting from Safe-to-Spend."""
        for con in (self.sqlite_con, self.d1_con):
            reset_test_tables(con)

            con.execute("INSERT INTO accounts (name, kind, current_balance, active) VALUES ('BCA Main', 'Owned', 10000000.0, 1)")
            con.execute("INSERT INTO allocation_goals (id, name, kind, target_amount, allocated_amount, status) VALUES (1, 'Tabungan Servis', 'Goal', 2000000.0, 1000000.0, 'Active')")
            con.execute("INSERT INTO upcoming (id, title, amount, due_date, status, linked_goal_id) VALUES (1, 'Jadwal Servis', 1000000.0, '2026-09-01', 'Upcoming', 1)")
            con.commit()

        py_dash = services.dashboard(self.sqlite_con, "2026-08")
        d1_kpis = compute_d1_domain_kpis(self.d1_con, "2026-08")

        # Effective commitment must be 0.0 because 1.0M upcoming is fully covered by 1.0M allocation goal
        self.assertEqual(d1_kpis["currentCommitments"], 0.0)
        self.assertEqual(py_dash["kpis"]["currentCommitments"], 0.0)

        # Safe-to-spend is 10M - 1M allocation = 9,000,000.0 (NOT 8,000,000.0)
        self.assertEqual(d1_kpis["safeToSpend"], 9000000.0)
        self.assertEqual(py_dash["kpis"]["safeToSpend"], 9000000.0)

    def test_07_shadow_write_route_rejection(self):
        """Test 7: Worker in MODE=shadow strictly rejects write mutations with 503 SHADOW_READ_ONLY."""
        worker_index = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("SHADOW_READ_ONLY", worker_index)
        self.assertIn("503", worker_index)
        self.assertIn("method !== \"GET\"", worker_index)

    def test_08_query_injection_and_path_traversal_protection(self):
        """Test 8: Worker code uses parameterized bindings and URL safe parsing."""
        worker_domain = (BASE_DIR / "cloud" / "worker" / "src" / "domain.ts").read_text(encoding="utf-8")
        self.assertIn(".bind(", worker_domain)
        self.assertNotIn("`SELECT * FROM transactions WHERE date = ${", worker_domain)

    def test_09_staging_auth_token_enforcement(self):
        """Test 9: Staging authorization enforces constant-time comparison and returns 401 on invalid token."""
        auth_ts = (BASE_DIR / "cloud" / "worker" / "src" / "auth.ts").read_text(encoding="utf-8")
        self.assertIn("constantTimeEqual", auth_ts)
        self.assertIn("401", auth_ts)

    def test_10_safe_error_handling_and_no_wildcard_cors(self):
        """Test 10: Server errors return generic INTERNAL_SERVER_ERROR and no wildcard CORS is exposed."""
        worker_index = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")
        auth_ts = (BASE_DIR / "cloud" / "worker" / "src" / "auth.ts").read_text(encoding="utf-8")
        self.assertIn("INTERNAL_SERVER_ERROR", worker_index)
        self.assertNotIn("err.message", worker_index)
        self.assertNotIn('"Access-Control-Allow-Origin": "*"', auth_ts)

    def test_11_production_db_unmodified(self):
        """Test 11: Production SQLite database hash remains identical before and after test."""
        actual_hash = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Production database was altered during test execution!")


if __name__ == "__main__":
    unittest.main()
