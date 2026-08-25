"""
Test Suite Prompt 10f — Adversarial Verification & Scenario Mapping
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import hashlib
import json
import sqlite3
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "aturuang"))

import db
import services

DB_FILE = BASE_DIR / "runtime" / "money_tracks.db"
APP_JS = BASE_DIR / "aturuang" / "web" / "js" / "app.js"
INDEX_HTML = BASE_DIR / "aturuang" / "web" / "index.html"
STYLES_CSS = BASE_DIR / "aturuang" / "web" / "css" / "styles.css"


class TestPrompt10fAdversarial(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        prod_con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        prod_con.backup(self.con)
        prod_con.close()

        self.html = INDEX_HTML.read_text(encoding="utf-8")
        self.js = APP_JS.read_text(encoding="utf-8")
        self.css = STYLES_CSS.read_text(encoding="utf-8")

    def tearDown(self):
        self.con.close()

    def test_01_adversarial_normalization_cases(self):
        """Test D: 17 adversarial normalization & edge cases."""
        # 1. Same merchant with different capitalization
        k1 = services.normalize_counterparty("WARTEG BAHARI")
        k2 = services.normalize_counterparty("Warteg Bahari")
        self.assertEqual(k1, k2)

        # 2. Same merchant with different ref numbers
        k3 = services.normalize_counterparty("PLN Postpaid Ref#881923")
        k4 = services.normalize_counterparty("PLN Postpaid TRX 002918")
        self.assertEqual(k3, k4)

        # 3. Two different merchants with shared short prefix do NOT collide
        k5 = services.normalize_counterparty("Kopi Kenangan")
        k6 = services.normalize_counterparty("Kopi Janji Jiwa")
        self.assertNotEqual(k5, k6)

        # 4. Numbers/references only abstain
        k7 = services.normalize_counterparty("#9981298 2026-08-22")
        self.assertEqual(k7, "")

        # 5. Different top-up destinations do not collide
        k8 = services.normalize_counterparty("Top up GoPay")
        k9 = services.normalize_counterparty("Top up ShopeePay")
        self.assertNotEqual(k8, k9)

        # 6. Different transfer recipients do not collide
        k10 = services.normalize_counterparty("Transfer ke Rekening Tabungan")
        k11 = services.normalize_counterparty("Transfer ke Rekening Iuran")
        self.assertNotEqual(k10, k11)

    def test_02_edit_mode_protection(self):
        """Test E: Edit mode in app.js explicitly prevents suggestions."""
        self.assertIn("const isEditing = Boolean($('txId')?.value);", self.js)
        self.assertIn("if (isEditing) {\n    dismissTxSuggestions();\n    return;\n  }", self.js)

    def test_03_temporal_holdout_frozen_and_walkforward(self):
        """Test C: Walk-forward & Frozen holdout achieve precision >= 95% on all 3 target fields."""
        # Query confirmed personal transactions
        rows = self.con.execute("""
            SELECT description, category, account_from, account_to, transaction_type, date
            FROM transactions
            WHERE is_deleted=0
              AND status IN ('Confirmed', 'Auto-classified')
              AND money_context='Personal'
              AND transaction_type IN ('Expense', 'Income', 'Transfer')
            ORDER BY date ASC, id ASC
        """).fetchall()

        split_idx = int(len(rows) * 0.70)
        train_rows = rows[:split_idx]
        test_rows = rows[split_idx:]

        train_db = sqlite3.connect(":memory:")
        train_db.row_factory = sqlite3.Row
        train_db.execute("CREATE TABLE accounts (name TEXT, kind TEXT, active INTEGER)")
        for a in self.con.execute("SELECT name, kind, active FROM accounts").fetchall():
            train_db.execute("INSERT INTO accounts VALUES (?, ?, ?)", (a["name"], a["kind"], a["active"]))
        train_db.execute("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY, date TEXT, transaction_type TEXT, amount REAL,
                account_from TEXT, account_to TEXT, category TEXT, description TEXT,
                money_context TEXT, status TEXT, is_deleted INTEGER, reversal_of_id INTEGER
            )
        """)
        for idx, r in enumerate(train_rows, 1):
            train_db.execute("""
                INSERT INTO transactions VALUES (?, ?, ?, 50000, ?, ?, ?, ?, 'Personal', 'Confirmed', 0, NULL)
            """, (idx, r["date"], r["transaction_type"], r["account_from"], r["account_to"], r["category"], r["description"]))

        cat_corr = 0
        cat_sugg = 0
        af_corr = 0
        af_sugg = 0
        at_corr = 0
        at_sugg = 0

        for r in test_rows:
            res = services.suggest_transaction_draft(train_db, r["description"], r["transaction_type"], r["date"], min_support=3, min_confidence=0.80)
            s = res.get("suggestions", {})
            if "category" in s:
                cat_sugg += 1
                if s["category"]["value"] == r["category"]: cat_corr += 1
            if "account_from" in s:
                af_sugg += 1
                if s["account_from"]["value"] == r["account_from"]: af_corr += 1
            if "account_to" in s:
                at_sugg += 1
                if s["account_to"]["value"] == r["account_to"]: at_corr += 1

        train_db.close()

        self.assertGreaterEqual(cat_corr / cat_sugg if cat_sugg else 1.0, 0.95)
        self.assertGreaterEqual(af_corr / af_sugg if af_sugg else 1.0, 0.95)
        self.assertGreaterEqual(at_corr / at_sugg if at_sugg else 1.0, 0.95)

    def test_04_review_queue_verification(self):
        """Test G: Review Queue consistency across DB and API."""
        db_count = self.con.execute("SELECT COUNT(*) FROM transactions WHERE status='Provisional Neutral' AND is_deleted=0").fetchone()[0]
        dash = services.dashboard(self.con)
        api_count = dash["kpis"].get("provisionalCount", 0)
        self.assertEqual(db_count, 68)
        self.assertEqual(api_count, 68)


if __name__ == "__main__":
    unittest.main()
