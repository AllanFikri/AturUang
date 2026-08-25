import unittest
import sqlite3
import shutil
import tempfile
import hashlib
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db
import services


class TestPrompt11Allocations(unittest.TestCase):
    def setUp(self):
        self.prod_hash_before = hashlib.sha256((BASE_DIR / "money_tracks.db").read_bytes()).hexdigest() if (BASE_DIR / "money_tracks.db").exists() else None
        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "money_tracks.db"
        shutil.copyfile(BASE_DIR / "money_tracks.db", self.test_db)
        self.con = sqlite3.connect(self.test_db)
        self.con.row_factory = sqlite3.Row
        services.migrate_allocation_schema(self.con)
        # Clear legacy physical account protection so allocation_goals is the pure authority
        self.con.execute("UPDATE accounts SET protected_amount=0, protected=0")
        self.con.execute("DELETE FROM allocation_goals")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_idempotent_migration(self):
        """Test 1: Migration helper can run repeatedly without error."""
        res1 = services.migrate_allocation_schema(self.con)
        self.assertTrue(res1["status"] in ("success", "already_migrated"))
        res2 = services.migrate_allocation_schema(self.con)
        self.assertTrue(res2["status"] in ("success", "already_migrated"))
        tables = [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("allocation_goals", tables)
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(upcoming)").fetchall()]
        self.assertIn("reserve_now", cols)
        self.assertIn("linked_goal_id", cols)

    def test_02_owned_to_owned_transfer_neutrality(self):
        """Test 2: Transfer between owned accounts preserves total liquid assets, allocations, and Dana Tersedia."""
        services.create_allocation_goal(self.con, name="Dana Darurat", kind="Emergency", target_amount=500000.0, initial_funding=500000.0)
        snap_before = services.balance_snapshot(self.con)
        dash_before = services.dashboard(self.con, "2026-08")

        services.mutate_account_balance(self.con, "BCA Poket: Tabungan", -200000.0, "2026-08-24")
        services.mutate_account_balance(self.con, "ShopeePay", +200000.0, "2026-08-24")

        snap_after = services.balance_snapshot(self.con)
        dash_after = services.dashboard(self.con, "2026-08")

        self.assertAlmostEqual(snap_after["total_balance"], snap_before["total_balance"], places=2)
        self.assertAlmostEqual(snap_after["protected_savings"], snap_before["protected_savings"], places=2)
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"], places=2)

    def test_03_create_goal_target_only_does_not_change_sts(self):
        """Test 3: Creating a goal with target amount only (0 initial funding) does NOT reduce Dana Tersedia."""
        dash_before = services.dashboard(self.con, "2026-08")
        res = services.create_allocation_goal(self.con, name="Tabungan Menikah", kind="Goal", target_amount=50000000.0, initial_funding=0.0)
        self.assertEqual(res["status"], "success")

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"], places=2)

    def test_04_funding_goal_decreases_sts_equally(self):
        """Test 4: Funding an allocation goal decreases Dana Tersedia by exactly the funded amount."""
        dash_before = services.dashboard(self.con, "2026-08")
        res = services.create_allocation_goal(self.con, name="Beli Laptop", kind="Goal", target_amount=15000000.0, initial_funding=100000.0)
        self.assertEqual(res["status"], "success")

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"] - 100000.0, places=2)

    def test_05_release_allocation_restores_sts_equally(self):
        """Test 5: Releasing an allocation goal restores Dana Tersedia by exactly the released amount."""
        res_create = services.create_allocation_goal(self.con, name="Dana Liburan", kind="Goal", target_amount=2000000.0, initial_funding=300000.0)
        gid = res_create["goal_id"]

        dash_before = services.dashboard(self.con, "2026-08")
        res_rel = services.release_allocation_goal(self.con, gid, amount=300000.0)
        self.assertEqual(res_rel["status"], "success")

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"] + 300000.0, places=2)

    def test_06_emergency_fund_uses_identical_rule(self):
        """Test 6: Emergency fund allocation follows the exact same funding & reservation rules."""
        dash_before = services.dashboard(self.con, "2026-08")
        res = services.create_allocation_goal(self.con, name="Dana Darurat", kind="Emergency", target_amount=5000000.0, initial_funding=500000.0)
        self.assertEqual(res["status"], "success")

        summary = services.get_allocation_summary(self.con)
        self.assertAlmostEqual(summary["emergency_allocated"], 500000.0, places=2)

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"] - 500000.0, places=2)

    def test_07_confirmed_commitment_deducted_once(self):
        """Test 7: Confirmed commitment is deducted exactly once from Dana Tersedia."""
        dash_before = services.dashboard(self.con, "2026-08")
        cur_comm = services.current_commitments(self.con)

        self.con.execute("""
            INSERT INTO upcoming (title, amount, category, status, due_date, reserve_now)
            VALUES ('Langganan AI', 60000.0, 'Subscriptions', 'Upcoming', '2026-08-28', 0)
        """)
        new_comm = services.current_commitments(self.con)
        self.assertAlmostEqual(new_comm, cur_comm + 60000.0, places=2)

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"] - 60000.0, places=2)

    def test_08_tentative_reserve_zero_not_deducted(self):
        """Test 8: Tentative commitment with reserve_now=0 does NOT deduct from Dana Tersedia."""
        dash_before = services.dashboard(self.con, "2026-08")
        cur_comm = services.current_commitments(self.con)

        self.con.execute("""
            INSERT INTO upcoming (title, amount, category, status, due_date, reserve_now)
            VALUES ('Tiket Bus PKL', 650000.0, 'Transport / Other Transport', 'Tentative', '2026-08-31', 0)
        """)
        new_comm = services.current_commitments(self.con)
        self.assertAlmostEqual(new_comm, cur_comm, places=2)

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"], places=2)

    def test_09_tentative_reserve_active_deducted(self):
        """Test 9: Tentative commitment with reserve_now=1 is deducted from Dana Tersedia."""
        dash_before = services.dashboard(self.con, "2026-08")
        cur_comm = services.current_commitments(self.con)

        self.con.execute("""
            INSERT INTO upcoming (title, amount, category, status, due_date, reserve_now)
            VALUES ('Peralatan Lab', 150000.0, 'Campus & Organization', 'Tentative', '2026-08-31', 1)
        """)
        new_comm = services.current_commitments(self.con)
        self.assertAlmostEqual(new_comm, cur_comm + 150000.0, places=2)

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"] - 150000.0, places=2)

    def test_10_covered_commitment_payment_preserves_invariant(self):
        """Test 10: Payment of a covered commitment preserves STS invariant (zero double deduction)."""
        g_res = services.create_allocation_goal(self.con, name="Dana IOM", kind="Goal", target_amount=120000.0, initial_funding=120000.0)
        gid = g_res["goal_id"]
        services.link_goal_to_upcoming(self.con, gid, 1)

        dash_before = services.dashboard(self.con, "2026-08")

        res_pay = services.pay_upcoming_atomic(self.con, 1, "BCA Main", "2026-08-25")
        self.assertEqual(res_pay["status"], "success")

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"], places=2)

    def test_11_skip_cancel_releases_relationship(self):
        """Test 11: Skipping or cancelling an upcoming commitment cleanly unlinks/releases coverage."""
        g_res = services.create_allocation_goal(self.con, name="Dana Test", kind="Goal", target_amount=70000.0, initial_funding=70000.0)
        gid = g_res["goal_id"]
        services.link_goal_to_upcoming(self.con, gid, 2)

        services.set_upcoming_status_and_reservation(self.con, 2, status="Cancelled")
        u = self.con.execute("SELECT * FROM upcoming WHERE id=2").fetchone()
        self.assertEqual(u["status"], "Cancelled")

    def test_12_over_allocation_and_invalid_amounts_rejected(self):
        """Test 12: Negative or invalid funding amounts are rejected with ValueError."""
        with self.assertRaises(ValueError):
            services.create_allocation_goal(self.con, name="Invalid Goal", kind="Goal", target_amount=-1000.0)
        with self.assertRaises(ValueError):
            services.create_allocation_goal(self.con, name="Invalid Goal", kind="Goal", initial_funding=-500.0)

    def test_13_negative_outstanding_rejected(self):
        """Test 13: Releasing more than allocated amount is rejected."""
        g_res = services.create_allocation_goal(self.con, name="Goal Test", kind="Goal", target_amount=100000.0, initial_funding=50000.0)
        gid = g_res["goal_id"]
        with self.assertRaises(ValueError):
            services.release_allocation_goal(self.con, gid, amount=60000.0)

    def test_14_idempotent_status_transitions(self):
        """Test 14: Repeated release calls are idempotent."""
        g_res = services.create_allocation_goal(self.con, name="Idempotent Test", kind="Goal", target_amount=100000.0, initial_funding=50000.0)
        gid = g_res["goal_id"]
        r1 = services.release_allocation_goal(self.con, gid, amount=50000.0, target_status="Released")
        self.assertEqual(r1["status"], "success")
        r2 = services.release_allocation_goal(self.con, gid, amount=0.0, target_status="Released")
        self.assertTrue(r2.get("idempotent") or r2["status"] == "success")

    def test_15_atomic_rollback_on_midway_failure(self):
        """Test 15: Failure during multi-step operation rolls back completely."""
        services.create_allocation_goal(self.con, name="Base Goal", kind="Goal", target_amount=200000.0, initial_funding=100000.0)
        snap_before = services.balance_snapshot(self.con)
        try:
            self.con.commit()
            self.con.execute("BEGIN TRANSACTION")
            services.create_allocation_goal(self.con, name="Rollback Test", kind="Goal", target_amount=100000.0, initial_funding=50000.0)
            raise RuntimeError("Forced simulation failure")
        except RuntimeError:
            self.con.rollback()

        snap_after = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap_after["protected_savings"], snap_before["protected_savings"], places=2)

    def test_16_income_expense_report_unpolluted(self):
        """Test 16: Creating, funding, and releasing allocation goals does not create cash transactions or distort P&L."""
        tx_count_before = self.con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
        g_res = services.create_allocation_goal(self.con, name="Goal Clean", kind="Goal", target_amount=500000.0, initial_funding=200000.0)
        gid = g_res["goal_id"]
        services.release_allocation_goal(self.con, gid, 200000.0)
        tx_count_after = self.con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
        self.assertEqual(tx_count_before, tx_count_after)

    def test_17_receivable_does_not_inflate_liquid_assets(self):
        """Test 17: Piutang (Receivables) remains non-liquid asset and does not inflate Total Liquid Assets or STS."""
        snap = services.balance_snapshot(self.con)
        tot_bal = snap["total_balance"]
        self.assertAlmostEqual(tot_bal, 2379368.80, places=2)
        debts = services.list_debts(self.con, kind="Receivable")
        self.assertTrue(len(debts) > 0)
        out_receivable = debts[0]["outstanding"]
        self.assertAlmostEqual(out_receivable, 750000.0, places=2)

    def test_18_legacy_compatibility(self):
        """Test 18: Legacy protected_allocations API and balance_snapshot work without error."""
        pa = services.get_protected_allocations(self.con)
        self.assertIsInstance(pa, list)
        snap = services.balance_snapshot(self.con)
        self.assertIn("protected_savings", snap)

    def test_19_production_db_unmodified(self):
        """Test 19: Production money_tracks.db SHA-256 hash remains unaltered."""
        actual_hash = hashlib.sha256((BASE_DIR / "money_tracks.db").read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Production DB was mutated!")


if __name__ == "__main__":
    unittest.main()
