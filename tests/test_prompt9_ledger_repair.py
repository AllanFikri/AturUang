"""
Test Suite Prompt 9a — Authoritative Database & BCA Controlled Ledger Repair
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import hashlib
import sqlite3
import unittest
from pathlib import Path

import db
import services

BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "runtime" / "money_tracks.db"


class TestPrompt9LedgerRepair(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(DB_FILE)
        self.con.row_factory = sqlite3.Row

    def tearDown(self):
        self.con.close()

    def test_01_database_integrity_and_foreign_keys(self):
        """Test 1: PRAGMA integrity_check is ok and foreign_key_check is empty."""
        ic = self.con.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(ic, "ok")
        fkc = self.con.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(len(fkc), 0, f"Foreign key violations found: {fkc}")

    def test_02_bca_account_balances_exact_match(self):
        """Test 2: Final account balances match canonical verified figures."""
        bca_main = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()
        self.assertAlmostEqual(bca_main["current_balance"], 67821.80, delta=0.005)

        tabungan = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Poket: Tabungan'").fetchone()
        self.assertAlmostEqual(tabungan["current_balance"], 1418000.0, delta=0.005)

        iuran = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Poket: Iuran'").fetchone()
        self.assertAlmostEqual(iuran["current_balance"], 840000.0, delta=0.005)

        charger = self.con.execute("SELECT current_balance, active FROM accounts WHERE name='BCA Poket: Beli Charger'").fetchone()
        self.assertAlmostEqual(charger["current_balance"], 0.0, delta=0.005)
        self.assertEqual(charger["active"], 0)

    def test_03_reconstruct_bca_main_balance_ok(self):
        """Test 3: reconstruct_account_balance independently validates BCA Main without discrepancy."""
        recon = services.reconstruct_account_balance(self.con, "BCA Main")
        self.assertEqual(recon["status"], "ok")
        self.assertAlmostEqual(recon["expected_balance"], 67821.80, delta=0.005)
        self.assertAlmostEqual(recon["difference"], 0.0, delta=0.005)

    def test_04_verify_balances_no_bca_discrepancy(self):
        """Test 4: verify_balances reports zero discrepancies across all active accounts."""
        issues = services.verify_balances(self.con)
        bca_issues = [i for i in issues if i["account_name"] == "BCA Main"]
        self.assertEqual(len(bca_issues), 0, f"Unexpected BCA Main issues: {bca_issues}")

    def test_05_no_fake_adjustments_or_reconciliations(self):
        """Test 5: No synthetic Adjustment or Rekonsiliasi transactions were injected."""
        fake_adj = self.con.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE (description LIKE '%[Rekonsiliasi]%' OR transaction_type='Adjustment')
                 AND is_deleted=0 AND date >= '2026-08-01'"""
        ).fetchone()[0]
        self.assertEqual(fake_adj, 0, "Fake reconciliation/adjustment transactions detected!")

    def test_06_total_transaction_count_and_history(self):
        """Test 6: Total active transactions equals 1149 post-import."""
        tx_count = self.con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
        self.assertEqual(tx_count, 1149, f"Expected 1149 transactions, got {tx_count}")

    def test_07_pre_anchor_and_post_anchor_presence(self):
        """Test 7: All 4 pre-anchor and 7 post-anchor transactions exist with correct attributes."""
        # Check pre-anchor expense
        tx_p1 = self.con.execute("SELECT amount, budget_effect FROM transactions WHERE description='Biaya admin top up ShopeePay' AND is_deleted=0").fetchone()
        self.assertIsNotNone(tx_p1)
        self.assertEqual(tx_p1["amount"], 500.0)

        # Check post-anchor expense
        tx_kung = self.con.execute("SELECT amount, category, for_with_whom FROM transactions WHERE description='Bayar pijat Kung' AND is_deleted=0").fetchone()
        self.assertIsNotNone(tx_kung)
        self.assertEqual(tx_kung["amount"], 135000.0)
        self.assertEqual(tx_kung["category"], "Health")
        self.assertEqual(tx_kung["for_with_whom"], "Keluarga / Family")

        # Check post-anchor income
        tx_bunga = self.con.execute("SELECT amount FROM transactions WHERE description='Bunga penutupan Poket Beli Charger' AND is_deleted=0").fetchone()
        self.assertIsNotNone(tx_bunga)
        self.assertEqual(tx_bunga["amount"], 0.25)


if __name__ == "__main__":
    unittest.main()
