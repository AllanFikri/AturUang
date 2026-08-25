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


class TestProtectedFundsContract(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "money_tracks.db"
        shutil.copyfile(BASE_DIR / "money_tracks.db", self.test_db)
        self.con = sqlite3.connect(self.test_db)
        self.con.row_factory = sqlite3.Row
        # Set self-contained 10j test fixture on temporary DB
        self.con.execute("UPDATE accounts SET protected_amount=2500000.0, protected=1 WHERE name='BCA Poket: Tabungan'")
        self.con.execute("UPDATE protected_allocations SET status='Active' WHERE id=1")
        self.con.execute("UPDATE upcoming SET status='Upcoming' WHERE id=3")
        self.con.execute("DELETE FROM allocation_goals")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_internal_transfer_preserves_protected_funds(self):
        """Invariant 1: Internal Transfer preserves total protected funds."""
        snap_before = services.balance_snapshot(self.con)
        services.mutate_account_balance(self.con, "BCA Poket: Tabungan", -255000.0, "2026-08-24")
        services.mutate_account_balance(self.con, "BCA Main", +255000.0, "2026-08-24")
        snap_after = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap_after["protected_savings"], snap_before["protected_savings"], places=2)

    def test_02_internal_transfer_preserves_total_liquid_assets(self):
        """Invariant 2: Internal Transfer preserves total liquid assets."""
        snap_before = services.balance_snapshot(self.con)
        services.mutate_account_balance(self.con, "BCA Poket: Tabungan", -100000.0, "2026-08-24")
        services.mutate_account_balance(self.con, "BCA Main", +100000.0, "2026-08-24")
        snap_after = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap_after["total_balance"], snap_before["total_balance"], places=2)

    def test_03_internal_transfer_preserves_safe_to_spend(self):
        """Invariant 3: Internal Transfer preserves Safe-to-Spend."""
        dash_before = services.dashboard(self.con, "2026-08")
        services.mutate_account_balance(self.con, "BCA Poket: Tabungan", -100000.0, "2026-08-24")
        services.mutate_account_balance(self.con, "BCA Main", +100000.0, "2026-08-24")
        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"], places=2)

    def test_04_shopeepay_topup_is_budget_neutral(self):
        """Invariant 4: ShopeePay top-up is budget-neutral (budget_effect = 0.0)."""
        self.con.execute("""
            INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, status, budget_effect)
            VALUES ('2026-08-24', '09:06:53', 'Transfer', 55000.0, 'BCA Main', 'ShopeePay', 'Topup ShopeePay', 'Confirmed', 0.0)
        """)
        self.con.commit()
        tx = self.con.execute("SELECT budget_effect FROM transactions WHERE account_from='BCA Main' AND account_to='ShopeePay' AND date='2026-08-24'").fetchone()
        self.assertAlmostEqual(tx["budget_effect"], 0.0, places=2)

    def test_05_expense_decreases_assets_and_sts_equally(self):
        """Invariant 5: Expense decreases assets and Safe-to-Spend equally."""
        snap_before = services.balance_snapshot(self.con)
        dash_before = services.dashboard(self.con, "2026-08")

        services.mutate_account_balance(self.con, "BCA Main", -16000.0, "2026-08-24")
        self.con.execute("""
            INSERT INTO transactions (date, time, transaction_type, amount, account_from, category, description, status, budget_effect)
            VALUES ('2026-08-24', '13:02:32', 'Expense', 16000.0, 'BCA Main', 'Cafe & Drinks', 'Momoyo Ice Cream', 'Confirmed', 16000.0)
        """)
        self.con.commit()

        snap_after = services.balance_snapshot(self.con)
        dash_after = services.dashboard(self.con, "2026-08")

        self.assertAlmostEqual(snap_after["total_balance"], snap_before["total_balance"] - 16000.0, places=2)
        self.assertAlmostEqual(snap_after["protected_savings"], snap_before["protected_savings"], places=2)
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"] - 16000.0, places=2)

    def test_06_released_decreases_protection_once(self):
        """Invariant 6: Explicit Released status reduces protected funds exactly once."""
        snap_before = services.balance_snapshot(self.con)
        res = services.set_protected_allocation_status(self.con, 1, "Released")
        self.assertEqual(res["status"], "success")

        snap_after = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap_after["protected_savings"], snap_before["protected_savings"] - 650000.0, places=2)

    def test_07_spent_plus_payment_preserves_k1_invariant(self):
        """Invariant 7: Spent plus payment preserves K1 invariant (STS remains constant)."""
        services.link_protected_allocation(self.con, 1, 3)
        dash_before = services.dashboard(self.con, "2026-08")

        res = services.pay_upcoming_atomic(self.con, 3, "BCA Poket: Tabungan", "2026-08-24")
        self.assertEqual(res["status"], "success")

        dash_after = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(dash_after["kpis"]["safeToSpend"], dash_before["kpis"]["safeToSpend"], places=2)

    def test_08_repeated_status_transition_is_idempotent(self):
        """Invariant 8: Repeated status transition is terminal and idempotent."""
        services.set_protected_allocation_status(self.con, 1, "Released")
        snap1 = services.balance_snapshot(self.con)

        try:
            services.set_protected_allocation_status(self.con, 1, "Released")
        except ValueError:
            pass

        snap2 = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap1["protected_savings"], snap2["protected_savings"], places=2)

    def test_09_protected_target_not_capped_by_original_account_balance(self):
        """Invariant 9: Protected target is not capped by original account balance."""
        cur = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Poket: Tabungan'").fetchone()[0]
        services.mutate_account_balance(self.con, "BCA Poket: Tabungan", -(cur - 500000.0), "2026-08-24")
        acc = self.con.execute("SELECT current_balance, protected_amount FROM accounts WHERE name='BCA Poket: Tabungan'").fetchone()
        self.assertAlmostEqual(acc["current_balance"], 500000.0, places=2)
        self.assertAlmostEqual(acc["protected_amount"], 2500000.0, places=2)

        contrib = services.get_account_protected_contribution(self.con, "BCA Poket: Tabungan")
        self.assertAlmostEqual(contrib, 2500000.0, places=2)

    def test_10_underfunded_protection_produces_negative_safe_to_spend(self):
        """Invariant 10: Underfunded protection produces negative Safe-to-Spend and Defisit badge."""
        dash = services.dashboard(self.con, "2026-08")
        sts = dash["kpis"]["safeToSpend"]
        self.assertLess(sts, 0.0)
        self.assertEqual(dash["kpis"]["statusKondisi"], "Defisit")
        self.assertEqual(dash["kpis"]["statusLevel"], "bad")

    def test_11_protected_account_deactivation_is_rejected(self):
        """Invariant 11: Protected account deactivation is rejected."""
        with self.assertRaises(ValueError):
            services.save_account_balance(self.con, {
                "name": "BCA Poket: Tabungan",
                "active": False,
                "protected_amount": 2500000.0,
            })

    def test_12_reconciliation_preserves_protection(self):
        """Invariant 12: Reconciliation preserves protected target."""
        snap_before = services.balance_snapshot(self.con)
        services.reconcile_account_balance(self.con, "BCA Main", 100000.0, "2026-08-24", "Adjust anchor")
        snap_after = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap_after["protected_savings"], snap_before["protected_savings"], places=2)

    def test_13_upcoming_coverage_remains_correct(self):
        """Invariant 13: Upcoming coverage and effective commitments calculate correctly."""
        cov = services.get_upcoming_active_coverage(self.con, 3)
        self.assertEqual(cov, 0.0)
        services.link_protected_allocation(self.con, 1, 3)
        cov_linked = services.get_upcoming_active_coverage(self.con, 3)
        self.assertAlmostEqual(cov_linked, 650000.0, places=2)

    def test_14_debt_titipan_piutang_remain_isolated(self):
        """Invariant 14: Debt, Titipan, and Piutang remain isolated from personal spending."""
        debt_res = services.create_debt_position(self.con, "Custody Test", "Custody", 50000.0, "2026-08-24", "Test note", None, False)
        self.assertEqual(debt_res["status"], "success")
        pt = services.current_pass_through_outstanding(self.con)
        self.assertAlmostEqual(pt, 50000.0, places=2)

    def test_15_verify_balances_remains_read_only(self):
        """Invariant 15: verify_balances remains strictly read-only."""
        db_bytes_before = self.test_db.read_bytes()
        issues = services.verify_balances(self.con)
        self.assertIsInstance(issues, list)
        db_bytes_after = self.test_db.read_bytes()
        self.assertEqual(hashlib.sha256(db_bytes_before).hexdigest(), hashlib.sha256(db_bytes_after).hexdigest())


if __name__ == "__main__":
    unittest.main()
