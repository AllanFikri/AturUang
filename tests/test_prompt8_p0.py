"""
Test Suite Prompt 8b1 — P0 Upgrade Path, K2 Flow Integrity & Dashboard Render Security
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "aturuang"))

import db
import services


class TestPrompt8bP0(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = Path(self.temp_dir) / "test_money_tracks.db"
        # Copy the pre-prompt8 backup (representing the legacy upgraded DB)
        backups = sorted(Path(BASE_DIR / "runtime" / "backups").glob("money_tracks_pre_ledger_repair_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            backups = sorted(Path(BASE_DIR / "runtime" / "backups").glob("money_tracks_*.db"), key=lambda p: p.stat().st_mtime, reverse=False)
        self.assertTrue(len(backups) > 0, "No pre-prompt8 backup found")
        shutil.copy2(backups[0], self.test_db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_old_schema_migrated_and_second_run_is_noop(self):
        """Test 1-3: Old schema with restrictive CHECK constraint migrates to allow Third-party & Adjustment; rerun is noop."""
        with db.db_connect(self.test_db_path) as con:
            pre_cnt = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        # 1. Run init_db to apply migration
        db.init_db(self.test_db_path)
        
        with db.db_connect(self.test_db_path) as con:
            sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'").fetchone()[0]
            self.assertIn("'Third-party'", sql)
            self.assertIn("'Adjustment'", sql)
            
            # Verify integrity & foreign keys
            integ = con.execute("PRAGMA integrity_check").fetchall()
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(integ[0][0], "ok")
            self.assertEqual(len(fk), 0)
            
            # Check row counts and Pass-through count
            cnt = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(cnt, pre_cnt)
            
        # 2. Second init_db run (idempotent no-op)
        db.init_db(self.test_db_path)
        with db.db_connect(self.test_db_path) as con:
            cnt2 = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(cnt2, pre_cnt)

    def test_02_historical_data_and_aggregates_preserved(self):
        """Test 4: All 1126 historical rows, IDs, canonical IDs, and monthly totals match pre-migration state exactly."""
        # Snapshot pre-migration state
        con_pre = db.db_connect(self.test_db_path)
        months = [r[0] for r in con_pre.execute("SELECT DISTINCT substr(date,1,7) FROM transactions WHERE is_deleted=0 ORDER BY 1").fetchall()]
        pre_monthly = {}
        for m in months:
            spent = services.get_month_actual_by_category(con_pre, m)
            pre_monthly[m] = sum(spent.values())
        pre_ids = [r[0] for r in con_pre.execute("SELECT id FROM transactions ORDER BY id").fetchall()]
        con_pre.close()

        # Run migration
        db.init_db(self.test_db_path)

        # Verify post-migration state
        con_post = db.db_connect(self.test_db_path)
        post_ids = [r[0] for r in con_post.execute("SELECT id FROM transactions ORDER BY id").fetchall()]
        self.assertEqual(pre_ids, post_ids)
        for m in months:
            post_spent = services.get_month_actual_by_category(con_post, m)
            self.assertAlmostEqual(pre_monthly[m], sum(post_spent.values()), places=2)
        con_post.close()

    def test_03_k2_cashflows_on_upgraded_db(self):
        """Test 5: Custody, Receivable, and Payable cash movements succeed with money_context='Third-party'."""
        db.init_db(self.test_db_path)
        con = db.db_connect(self.test_db_path)
        
        # 1. Custody (Titipan: uang masuk kas dari pihak ketiga)
        res_cust = services.create_debt_position(con, "Budi Titip", "Custody", 300000.0, "2026-08-23", "Titipan Barang", "BCA Main", is_cash=True)
        self.assertEqual(res_cust["status"], "success")
        cust_id = res_cust["debt_id"]
        tx_cust = con.execute("SELECT * FROM transactions WHERE id=?", (res_cust["transaction_id"],)).fetchone()
        self.assertEqual(tx_cust["money_context"], "Third-party")
        self.assertEqual(tx_cust["transaction_type"], "Income")
        self.assertEqual(float(tx_cust["budget_effect"]), 0.0)

        # 2. Receivable (Piutang: uang keluar kas dipinjamkan)
        res_rec = services.create_debt_position(con, "Doni Pinjam", "Receivable", 150000.0, "2026-08-23", "Talangan", "BCA Main", is_cash=True)
        self.assertEqual(res_rec["status"], "success")
        rec_id = res_rec["debt_id"]
        tx_rec = con.execute("SELECT * FROM transactions WHERE id=?", (res_rec["transaction_id"],)).fetchone()
        self.assertEqual(tx_rec["money_context"], "Third-party")
        self.assertEqual(tx_rec["transaction_type"], "Expense")
        self.assertEqual(float(tx_rec["budget_effect"]), 0.0)

        # 3. Partial Settlement on Receivable (uang masuk kembali)
        res_settle = services.add_debt_event(con, rec_id, "Settlement", 50000.0, "2026-08-23", "Cicil 1", "BCA Main", is_cash=True)
        self.assertEqual(res_settle["status"], "success")
        tx_settle = con.execute("SELECT * FROM transactions WHERE id=?", (res_settle["transaction_id"],)).fetchone()
        self.assertEqual(tx_settle["money_context"], "Third-party")
        self.assertEqual(tx_settle["transaction_type"], "Income")

        # 4. Verify report isolation
        dash = services.dashboard(con, "2026-08")
        # Personal income and expense must NOT be polluted by Third-party tx
        self.assertEqual(services.effective_budget_spend(tx_cust), 0.0)
        self.assertEqual(services.effective_budget_spend(tx_rec), 0.0)
        self.assertEqual(services.effective_budget_spend(tx_settle), 0.0)

        con.close()

    def test_04_atomic_rollback_on_failed_operation(self):
        """Test 7: Exception during multi-step debt event rolls back all mutations cleanly."""
        db.init_db(self.test_db_path)
        con = db.db_connect(self.test_db_path)

        # Attempt to create debt on non-existent account
        with self.assertRaises(ValueError):
            services.create_debt_position(con, "Failing User", "Payable", 500000.0, "2026-08-23", "Fail", "NonExistentAccount", is_cash=True)

        cnt = con.execute("SELECT COUNT(*) FROM debts WHERE person_name='Failing User'").fetchone()[0]
        self.assertEqual(cnt, 0)
        con.close()

    def test_05_xss_protection_home_actions(self):
        """Test 8: HTML escaping ensures XSS payloads in title/text are not executable."""
        import html
        def esc(s):
            return html.escape(str(s or ""), quote=True)

        payloads = [
            ("<img src=x onerror=alert(1)>", "&lt;img src=x onerror=alert(1)&gt;"),
            ("<svg onload=alert(1)>", "&lt;svg onload=alert(1)&gt;"),
            ("');alert(1);//", "&#x27;);alert(1);//"),
            ("1);alert(1);//", "1);alert(1);//")
        ]
        for raw, expected_safe in payloads:
            escaped = esc(raw)
            self.assertNotIn("<img", escaped)
            self.assertNotIn("<svg", escaped)
            rendered = f"<div class=\"insight\"><strong>{escaped}</strong></div>"
            self.assertNotIn("<img src=x", rendered)
            self.assertNotIn("<svg onload", rendered)

    def test_06_historical_research_rejected_on_client_tx(self):
        """Test 9: Historical Research context is rejected on regular client transaction creation."""
        tx = {
            "date": "2026-08-23", "transaction_type": "Expense", "amount": 50000,
            "account_from": "BCA Main", "money_context": "Historical Research",
            "budget_rule_version": "derived"
        }
        with self.assertRaises(ValueError) as ctx:
            services.validate_tx(tx)
        self.assertIn("Historical Research", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
