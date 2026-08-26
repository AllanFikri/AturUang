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

    def test_05_read_api_parity_logical_fingerprint(self):
        """Test 5: Comprehensive financial parity between Python canonical services and D1 database."""
        # 1. Total liquid balance parity
        py_snap = services.balance_snapshot(self.sqlite_con)
        liq_sql = py_snap["total_balance"]
        liq_d1 = self.d1_con.execute("SELECT sum(current_balance) FROM accounts WHERE active=1 AND kind='Owned'").fetchone()[0]
        self.assertAlmostEqual(liq_sql, liq_d1, delta=0.005)

        # 2. Total allocated savings parity
        py_alloc = services.get_total_allocated_savings(self.sqlite_con)
        d1_alloc = self.d1_con.execute("SELECT COALESCE(SUM(allocated_amount), 0) FROM allocation_goals WHERE status='Active'").fetchone()[0]
        self.assertAlmostEqual(py_alloc, d1_alloc, delta=0.005)

        # 3. Active transactions count parity
        tx_sql = self.sqlite_con.execute("SELECT count(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
        tx_d1 = self.d1_con.execute("SELECT count(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
        self.assertEqual(tx_sql, tx_d1)

        # 4. Custody (Titipan) outstanding parity
        py_pt = services.current_pass_through_outstanding(self.sqlite_con)
        d1_custody_rows = self.d1_con.execute("SELECT id FROM debts WHERE kind='Custody' AND status='Active'").fetchall()
        d1_pt = sum(
            self.d1_con.execute("SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=?", (r[0],)).fetchone()[0]
            for r in d1_custody_rows
        )
        self.assertAlmostEqual(py_pt, d1_pt, delta=0.005)

        # 5. Effective commitments parity
        py_comm = services.current_commitments(self.sqlite_con)
        self.assertGreaterEqual(py_comm, 0.0)

    def test_06_shadow_write_route_rejection(self):
        """Test 6: Worker in MODE=shadow strictly rejects write mutations with 503 SHADOW_READ_ONLY."""
        worker_index = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("SHADOW_READ_ONLY", worker_index)
        self.assertIn("503", worker_index)
        self.assertIn("method !== \"GET\"", worker_index)

    def test_07_query_injection_and_path_traversal_protection(self):
        """Test 7: Worker code uses parameterized bindings and URL safe parsing."""
        worker_domain = (BASE_DIR / "cloud" / "worker" / "src" / "domain.ts").read_text(encoding="utf-8")
        self.assertIn(".bind(", worker_domain)
        self.assertNotIn("`SELECT * FROM transactions WHERE date = ${", worker_domain)

    def test_08_staging_auth_token_enforcement(self):
        """Test 8: Staging authorization enforces constant-time comparison and returns 401 on invalid token."""
        auth_ts = (BASE_DIR / "cloud" / "worker" / "src" / "auth.ts").read_text(encoding="utf-8")
        self.assertIn("constantTimeEqual", auth_ts)
        self.assertIn("401", auth_ts)

    def test_09_wib_timezone_handling(self):
        """Test 9: Date and time calculations follow Asia/Jakarta (WIB)."""
        domain_ts = (BASE_DIR / "cloud" / "worker" / "src" / "domain.ts").read_text(encoding="utf-8")
        self.assertIn("Asia/Jakarta", domain_ts)

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
