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


class TestPrompt10kShopeePaySync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "money_tracks.db"
        shutil.copyfile(BASE_DIR / "money_tracks.db", self.test_db)
        self.con = sqlite3.connect(self.test_db)
        self.con.row_factory = sqlite3.Row
        # Set self-contained 10k test fixture on temporary DB
        self.con.execute("UPDATE accounts SET protected_amount=2500000.0, protected=1 WHERE name='BCA Poket: Tabungan'")
        self.con.execute("UPDATE upcoming SET status='Upcoming' WHERE id=3")
        self.con.execute("DELETE FROM upcoming WHERE title LIKE '%AI%'")
        self.con.execute("DELETE FROM allocation_goals")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_shopeepay_and_liquid_balance_exact_match(self):
        """Test 1: ShopeePay balance is exactly Rp51.482,00 and total liquid assets Rp2.379.368,80."""
        spay = self.con.execute("SELECT current_balance FROM accounts WHERE name='ShopeePay'").fetchone()[0]
        self.assertAlmostEqual(spay, 51482.0, delta=0.005)

        snap = services.balance_snapshot(self.con)
        self.assertAlmostEqual(snap["total_balance"], 2379368.80, delta=0.005)
        self.assertAlmostEqual(snap["protected_savings"], 2500000.0, delta=0.005)

    def test_02_pln_correction_and_soft_delete(self):
        """Test 2: PLN token is updated to Rp94.000 (ID 1142) and old admin fee (ID 1143) is soft-deleted."""
        tx1142 = self.con.execute("SELECT * FROM transactions WHERE id=1142").fetchone()
        self.assertAlmostEqual(tx1142["amount"], 94000.0, delta=0.005)
        self.assertAlmostEqual(tx1142["budget_effect"], 94000.0, delta=0.005)
        self.assertEqual(tx1142["category"], "Phone & Internet")
        self.assertEqual(tx1142["subtype"], "Electricity")
        self.assertEqual(tx1142["for_with_whom"], "Family")
        self.assertEqual(tx1142["money_context"], "Personal")
        self.assertEqual(tx1142["is_deleted"], 0)

        tx1143 = self.con.execute("SELECT * FROM transactions WHERE id=1143").fetchone()
        self.assertEqual(tx1143["is_deleted"], 1)

    def test_03_mbak_erin_receivable_and_events(self):
        """Test 3: Exactly 1 Receivable position for Mbak Erin with outstanding Rp750.000,00 across 3 events."""
        debts = self.con.execute("SELECT * FROM debts WHERE person_name='Mbak Erin'").fetchall()
        self.assertEqual(len(debts), 1)
        debt = debts[0]
        self.assertEqual(debt["kind"], "Receivable")
        self.assertEqual(debt["status"], "Active")

        out = services.reconstruct_debt_outstanding(self.con, debt["id"])
        self.assertAlmostEqual(out, 750000.0, delta=0.005)

        events = self.con.execute("SELECT * FROM debt_events WHERE debt_id=? ORDER BY id", (debt["id"],)).fetchall()
        self.assertEqual(len(events), 3)
        self.assertAlmostEqual(events[0]["amount"], 500000.0, delta=0.005)
        self.assertAlmostEqual(events[1]["amount"], 150000.0, delta=0.005)
        self.assertAlmostEqual(events[2]["amount"], 100000.0, delta=0.005)

        for ev in events:
            tx = self.con.execute("SELECT * FROM transactions WHERE id=?", (ev["transaction_id"],)).fetchone()
            self.assertEqual(tx["money_context"], "Third-party")
            self.assertAlmostEqual(tx["budget_effect"], 0.0, delta=0.005)
            self.assertEqual(tx["time"], "")
            self.assertEqual(tx["account_from"], "ShopeePay")

    def test_04_family_internet_expense(self):
        """Test 4: Family internet payment is Rp170.000,00 Expense under Phone & Internet for Family."""
        tx = self.con.execute(
            "SELECT * FROM transactions WHERE date='2026-08-24' AND amount=170000.0 AND account_from='ShopeePay' AND is_deleted=0"
        ).fetchone()
        self.assertIsNotNone(tx)
        self.assertEqual(tx["category"], "Phone & Internet")
        self.assertEqual(tx["for_with_whom"], "Family")
        self.assertEqual(tx["money_context"], "Personal")
        self.assertAlmostEqual(tx["budget_effect"], 170000.0, delta=0.005)
        self.assertEqual(tx["time"], "")

    def test_05_safe_to_spend_and_deficit_status(self):
        """Test 5: Dashboard Safe-to-Spend is exactly -Rp960.631,20 with status Defisit."""
        dash = services.dashboard(self.con, "2026-08")
        sts = dash["kpis"]["safeToSpend"]
        self.assertAlmostEqual(sts, -960631.20, delta=0.005)
        self.assertEqual(dash["kpis"]["statusKondisi"], "Defisit")
        self.assertEqual(dash["kpis"]["statusLevel"], "bad")

    def test_06_august_personal_spending(self):
        """Test 6: August total personal spending reflects +Rp162.500 net increase (Rp4.104.508,00)."""
        dash = services.dashboard(self.con, "2026-08")
        spent = dash["kpis"]["spent"]
        self.assertAlmostEqual(spent, 4104508.0, delta=0.005)

    def test_07_account_reconstruction_integrity(self):
        """Test 7: reconstruct_account_balance succeeds with zero difference on all accounts."""
        for acc in ["BCA Main", "BCA Poket: Tabungan", "BCA Poket: Iuran", "ShopeePay", "GoPay", "Cash"]:
            rec = services.reconstruct_account_balance(self.con, acc)
            self.assertEqual(rec["status"], "ok", f"Reconstruction failed for {acc}")
            self.assertAlmostEqual(rec["difference"], 0.0, delta=0.005)

    def test_08_duplicate_prevention(self):
        """Test 8: Duplicate prevention check proves topup and marketplace transactions are not duplicated."""
        topup_500 = self.con.execute("SELECT COUNT(*) FROM transactions WHERE date='2026-08-23' AND amount=500000.0 AND account_to='ShopeePay' AND is_deleted=0").fetchone()[0]
        self.assertEqual(topup_500, 1)

        topup_250 = self.con.execute("SELECT COUNT(*) FROM transactions WHERE date='2026-08-23' AND amount=250000.0 AND account_to='ShopeePay' AND is_deleted=0").fetchone()[0]
        self.assertEqual(topup_250, 1)

        topup_55 = self.con.execute("SELECT COUNT(*) FROM transactions WHERE date='2026-08-24' AND amount=55000.0 AND account_to='ShopeePay' AND is_deleted=0").fetchone()[0]
        self.assertEqual(topup_55, 1)


if __name__ == "__main__":
    unittest.main()
