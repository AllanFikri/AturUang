import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import unittest
import sqlite3
import shutil
import tempfile
import hashlib
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "aturuang"))
sys.path.insert(0, str(BASE_DIR / "scripts"))

import db
import services
from run_smoke_tests import (
    SYNTHETIC_ACCOUNT_NAME,
    SYNTHETIC_ACCOUNT_BALANCE,
    SYNTHETIC_GOAL_NAME,
    SYNTHETIC_GOAL_ALLOCATED,
    SYNTHETIC_COMMITMENT_NAME,
    SYNTHETIC_COMMITMENT_AMOUNT,
    SYNTHETIC_SAFE_TO_SPEND,
    seed_synthetic_smoke_fixture,
    run_smoke_in_dir,
)

def get_db_fingerprint(con: sqlite3.Connection) -> dict:
    """Extracts a comprehensive logical & data fingerprint of the database."""
    # 1. Schema objects from sqlite_master
    schema_rows = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name").fetchall()
    schema_objs = [dict(r) for r in schema_rows]
    
    # 2. Table columns info
    tables = [r["name"] for r in schema_rows if r["type"] == "table"]
    table_infos = {}
    fk_infos = {}
    for t in tables:
        table_infos[t] = [dict(r) for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()]
        fk_infos[t] = [dict(r) for r in con.execute(f"PRAGMA foreign_key_list('{t}')").fetchall()]
        
    # 3. Row counts per table
    counts = {}
    for t in tables:
        counts[t] = con.execute(f"SELECT count(*) FROM '{t}'").fetchone()[0]
        
    # 4. Critical financial aggregates
    tx_agg = con.execute("SELECT count(*), round(sum(amount), 2), round(sum(budget_effect), 2) FROM transactions WHERE is_deleted=0").fetchone()
    accounts = {r["name"]: r["current_balance"] for r in con.execute("SELECT name, current_balance FROM accounts").fetchall()}
    goals = [dict(r) for r in con.execute("SELECT * FROM allocation_goals").fetchall()]
    upcoming = [dict(r) for r in con.execute("SELECT * FROM upcoming").fetchall()]
    settings = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings").fetchall()}
    
    return {
        "schema_objs": schema_objs,
        "table_infos": table_infos,
        "fk_infos": fk_infos,
        "counts": counts,
        "tx_count": tx_agg[0],
        "tx_sum_amt": tx_agg[1],
        "tx_sum_be": tx_agg[2],
        "accounts": accounts,
        "goals": goals,
        "upcoming": upcoming,
        "settings": settings,
    }


class TestPrompt11fStartupIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "disposable_idempotency.db"
        db.init_db(self.test_db)
        seed_synthetic_smoke_fixture(self.test_db)
        
    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_init_db_first_call_completes_schema(self):
        """Test 1: First init_db completes and ensures all required tables and columns."""
        db.init_db(self.test_db)
        con = sqlite3.connect(self.test_db)
        con.row_factory = sqlite3.Row
        
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("allocation_goals", tables)
        self.assertIn("upcoming", tables)
        self.assertIn("transactions", tables)
        self.assertIn("accounts", tables)
        
        u_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
        self.assertIn("reserve_now", u_cols)
        self.assertIn("linked_goal_id", u_cols)
        
        con.close()

    def test_02_init_db_second_and_third_calls_are_strict_no_ops(self):
        """Test 2 & 3: Subsequent init_db calls are exact logical and byte no-ops."""
        db.init_db(self.test_db)
        con1 = sqlite3.connect(self.test_db)
        con1.row_factory = sqlite3.Row
        fp1 = get_db_fingerprint(con1)
        con1.close()
        h1 = hashlib.sha256(self.test_db.read_bytes()).hexdigest()

        # Call 2
        db.init_db(self.test_db)
        con2 = sqlite3.connect(self.test_db)
        con2.row_factory = sqlite3.Row
        fp2 = get_db_fingerprint(con2)
        con2.close()
        h2 = hashlib.sha256(self.test_db.read_bytes()).hexdigest()

        self.assertEqual(fp1, fp2, "Logical fingerprint changed on 2nd init_db call!")
        self.assertEqual(h1, h2, "File byte hash changed on 2nd init_db call!")

        # Call 3
        db.init_db(self.test_db)
        con3 = sqlite3.connect(self.test_db)
        con3.row_factory = sqlite3.Row
        fp3 = get_db_fingerprint(con3)
        con3.close()
        h3 = hashlib.sha256(self.test_db.read_bytes()).hexdigest()

        self.assertEqual(fp2, fp3, "Logical fingerprint changed on 3rd init_db call!")
        self.assertEqual(h2, h3, "File byte hash changed on 3rd init_db call!")

    def test_04_no_data_mutation_or_spurious_rows(self):
        """Test 4: init_db does not insert spurious transactions, accounts, or adjustments."""
        con_before = sqlite3.connect(self.test_db)
        con_before.row_factory = sqlite3.Row
        tx_count_before = con_before.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_before = con_before.execute("SELECT count(*) FROM accounts").fetchone()[0]
        con_before.close()

        db.init_db(self.test_db)
        db.init_db(self.test_db)

        con_after = sqlite3.connect(self.test_db)
        con_after.row_factory = sqlite3.Row
        tx_count_after = con_after.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_after = con_after.execute("SELECT count(*) FROM accounts").fetchone()[0]
        con_after.close()

        self.assertEqual(tx_count_before, tx_count_after)
        self.assertEqual(acc_count_before, acc_count_after)

    def test_05_no_duplicate_indices_or_triggers(self):
        """Test 5: No duplicate indices or triggers exist in sqlite_master."""
        db.init_db(self.test_db)
        con = sqlite3.connect(self.test_db)
        indices = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
        con.close()

        self.assertEqual(len(indices), len(set(indices)), "Duplicate indices detected in database!")

    def test_06_legacy_migration_guard(self):
        """Test 6: Migration guard safely converts a legacy test database with old upcoming constraints."""
        legacy_db = Path(self.tmp_dir) / "legacy_test.db"
        con = sqlite3.connect(legacy_db)
        con.execute("""CREATE TABLE upcoming (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            due_date TEXT NOT NULL,
            title TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            account TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Upcoming','Paid'))
        )""")
        con.execute("INSERT INTO upcoming (due_date, title, amount, category, account, status) VALUES ('2026-08-30', 'Old Test', 100.0, 'Education', 'Synthetic Account Alpha', 'Upcoming')")
        con.commit()
        con.close()

        # Run migration helper
        con_mig = sqlite3.connect(legacy_db)
        con_mig.row_factory = sqlite3.Row
        services.migrate_allocation_schema(con_mig)
        
        # Verify columns and status constraint
        cols = [r[1] for r in con_mig.execute("PRAGMA table_info(upcoming)").fetchall()]
        self.assertIn("reserve_now", cols)
        self.assertIn("linked_goal_id", cols)
        
        # Insert Tentative status row (must succeed)
        con_mig.execute("INSERT INTO upcoming (due_date, title, amount, category, account, status, reserve_now) VALUES ('2026-08-31', 'Tentative Test', 50.0, 'General', 'Synthetic Account Alpha', 'Tentative', 0)")
        con_mig.commit()
        con_mig.close()

    def test_07_disposable_db_isolation(self):
        """Test 7: Tests run strictly on disposable database in temp directory."""
        self.assertTrue(self.test_db.exists(), "Disposable test database must exist in temp directory!")
        self.assertTrue(str(self.test_db).startswith(tempfile.gettempdir()), "Test database must reside in temp directory!")

    def test_08_prompt11_formula_invariant(self):
        """Test 8: Formula calculation matches expected invariants on sanitized synthetic fixtures."""
        con = sqlite3.connect(f"file:{self.test_db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        dash = services.dashboard(con, "2026-08")
        k = dash["kpis"]
        con.close()

        self.assertAlmostEqual(k["totalBalance"], SYNTHETIC_ACCOUNT_BALANCE, delta=0.005)
        self.assertAlmostEqual(k["emergencyAllocated"], SYNTHETIC_GOAL_ALLOCATED, delta=0.005)
        self.assertAlmostEqual(k["effectiveConfirmedCommitments"], SYNTHETIC_COMMITMENT_AMOUNT, delta=0.005)
        self.assertAlmostEqual(k["tentativeReserved"], 0.0, delta=0.005)
        self.assertAlmostEqual(k["safeToSpend"], SYNTHETIC_SAFE_TO_SPEND, delta=0.005)

    def test_09_smoke_cleanup_normal_completion(self):
        """Test 9: Smoke test normal completion leaves no database or sidecar files behind."""
        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            run_smoke_in_dir(tpath)
            remaining = [f.name for f in tpath.glob("*.db*")]
            self.assertEqual(remaining, [], f"Expected zero database/sidecar files, found: {remaining}")

    def test_10_smoke_cleanup_intentional_exception(self):
        """Test 10: Smoke test exception handling leaves no database or sidecar files behind."""
        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            with self.assertRaises(RuntimeError):
                run_smoke_in_dir(tpath, raise_intentional=True)
            remaining = [f.name for f in tpath.glob("*.db*")]
            self.assertEqual(remaining, [], f"Expected zero database/sidecar files after exception, found: {remaining}")

    def test_11_smoke_cleanup_connection_failure(self):
        """Test 11: Smoke test connection failure leaves no database or sidecar files behind."""
        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            with self.assertRaises(Exception):
                run_smoke_in_dir(tpath, cause_conn_failure=True)
            remaining = [f.name for f in tpath.glob("*.db*")]
            self.assertEqual(remaining, [], f"Expected zero database/sidecar files after conn failure, found: {remaining}")


if __name__ == "__main__":
    unittest.main()
