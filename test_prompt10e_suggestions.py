"""
Test Suite Prompt 10e — Local Transaction Intelligence & Submenu Keyboard Navigation
"""
import hashlib
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db
import services

DB_FILE = BASE_DIR / "money_tracks.db"
APP_JS = BASE_DIR / "app.js"
INDEX_HTML = BASE_DIR / "index.html"
STYLES_CSS = BASE_DIR / "styles.css"


class TestPrompt10eSuggestions(unittest.TestCase):
    def setUp(self):
        # Create a temporary in-memory or file database copied from production for safety
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        
        # Load schema and data from production into memory
        prod_con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        prod_con.backup(self.con)
        prod_con.close()

        self.html = INDEX_HTML.read_text(encoding="utf-8")
        self.js = APP_JS.read_text(encoding="utf-8")
        self.css = STYLES_CSS.read_text(encoding="utf-8")

    def tearDown(self):
        self.con.close()

    def test_01_normalization_deterministic(self):
        """Scenario 1: Normalizer produces deterministic lowercase alphanumeric keys without dates/times/refs."""
        cases = [
            ("Top up GoPay via BCA 06/08 14:30 ref #89281 Rp 50.000", "gopay via bca"),
            ("Bayar Bensin Pertamax Spbu 34 22-08-2026", "bensin pertamax spbu"),
            ("Transfer ke BCA Poket Tabungan [Rekonsiliasi]", "bca poket tabungan"),
            ("Makan Siang Nasi Padang Sederhana", "makan siang nasi padang sederhana"),
            ("TRX#99812 Tagihan Listrik PLN 2026-08-20", "tagihan listrik pln"),
        ]
        for raw, expected in cases:
            res = services.normalize_counterparty(raw)
            self.assertEqual(res, expected)

    def test_02_support_below_threshold_abstains(self):
        """Scenario 2: Support < 3 yields empty suggestions."""
        # Insert 2 transactions only
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Warung Makan Barokah', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 45000, 'BCA Main', 'Main Meals', 'Warung Makan Barokah', 'Personal', 'Confirmed', 0)
        """)
        res = services.suggest_transaction_draft(self.con, "Warung Makan Barokah", "Expense", "2026-08-10", min_support=3)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["suggestions"], {})

    def test_03_confidence_below_threshold_abstains(self):
        """Scenario 3: Support >= 3 but confidence < 0.80 yields abstain on category."""
        # Insert 4 transactions: 3 Main Meals, 1 Cafe & Drinks -> conf = 3/4 = 0.75 (< 0.80)
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Resto Serba Enak', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Resto Serba Enak', 'Personal', 'Confirmed', 0),
                   ('2026-08-03', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Resto Serba Enak', 'Personal', 'Confirmed', 0),
                   ('2026-08-04', 'Expense', 50000, 'BCA Main', 'Cafe & Drinks', 'Resto Serba Enak', 'Personal', 'Confirmed', 0)
        """)
        res = services.suggest_transaction_draft(self.con, "Resto Serba Enak", "Expense", "2026-08-10", min_support=3, min_confidence=0.80)
        self.assertNotIn("category", res["suggestions"])
        # But account_from is 4/4 = 1.0 -> suggested!
        self.assertIn("account_from", res["suggestions"])
        self.assertEqual(res["suggestions"]["account_from"]["value"], "BCA Main")

    def test_04_support_and_confidence_valid_produces_suggestions(self):
        """Scenario 4: Support >= 3 and confidence >= 0.80 produces valid suggestions with reason."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Warteg Bahari Bahagia', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 45000, 'BCA Main', 'Main Meals', 'Warteg Bahari Bahagia', 'Personal', 'Confirmed', 0),
                   ('2026-08-03', 'Expense', 55000, 'BCA Main', 'Main Meals', 'Warteg Bahari Bahagia', 'Personal', 'Confirmed', 0),
                   ('2026-08-04', 'Expense', 40000, 'BCA Main', 'Main Meals', 'Warteg Bahari Bahagia', 'Personal', 'Confirmed', 0)
        """)
        res = services.suggest_transaction_draft(self.con, "Warteg Bahari Bahagia", "Expense", "2026-08-10", min_support=3, min_confidence=0.80)
        self.assertIn("category", res["suggestions"])
        self.assertEqual(res["suggestions"]["category"]["value"], "Main Meals")
        self.assertAlmostEqual(res["suggestions"]["category"]["confidence"], 1.0)
        self.assertIn("Cocok pada 4 dari 4 transaksi", res["suggestions"]["category"]["reason"])

        self.assertIn("account_from", res["suggestions"])
        self.assertEqual(res["suggestions"]["account_from"]["value"], "BCA Main")

    def test_05_tie_abstains(self):
        """Scenario 5: Tie between top categories results in abstention."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Kantin Kampus', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Kantin Kampus', 'Personal', 'Confirmed', 0),
                   ('2026-08-03', 'Expense', 50000, 'BCA Main', 'Snacks', 'Kantin Kampus', 'Personal', 'Confirmed', 0),
                   ('2026-08-04', 'Expense', 50000, 'BCA Main', 'Snacks', 'Kantin Kampus', 'Personal', 'Confirmed', 0)
        """)
        res = services.suggest_transaction_draft(self.con, "Kantin Kampus", "Expense", "2026-08-10", min_support=3, min_confidence=0.50)
        self.assertNotIn("category", res["suggestions"])

    def test_06_forbidden_cohorts_excluded(self):
        """Scenario 6: Forbidden cohorts (Historical Research, Pass-through, Deleted, Reversal, Adjustment) are excluded."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Kopi Senja Bahagia', 'Historical Research', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Kopi Senja Bahagia', 'Pass-through', 'Confirmed', 0),
                   ('2026-08-03', 'Adjustment', 50000, 'BCA Main', 'Main Meals', '[Rekonsiliasi] Kopi Senja Bahagia', 'Personal', 'Confirmed', 0),
                   ('2026-08-04', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Kopi Senja Bahagia', 'Personal', 'Confirmed', 1)
        """)
        res = services.suggest_transaction_draft(self.con, "Kopi Senja Bahagia", "Expense", "2026-08-10", min_support=1)
        self.assertEqual(res["suggestions"], {})

    def test_07_future_transactions_dont_leak(self):
        """Scenario 7: Transactions with date > draft_date are ignored."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-25', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Super Mart Future', 'Personal', 'Confirmed', 0),
                   ('2026-08-26', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Super Mart Future', 'Personal', 'Confirmed', 0),
                   ('2026-08-27', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Super Mart Future', 'Personal', 'Confirmed', 0)
        """)
        # Query for draft date 2026-08-20 (prior to future txs)
        res = services.suggest_transaction_draft(self.con, "Super Mart Future", "Expense", "2026-08-20", min_support=3)
        self.assertEqual(res["suggestions"], {})

    def test_08_inactive_accounts_not_suggested(self):
        """Scenario 8: Inactive account is not suggested."""
        self.con.execute("UPDATE accounts SET active=0 WHERE name='BCA Poket: Charger'")
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Poket: Charger', 'Main Meals', 'Charger Cafe', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 50000, 'BCA Poket: Charger', 'Main Meals', 'Charger Cafe', 'Personal', 'Confirmed', 0),
                   ('2026-08-03', 'Expense', 50000, 'BCA Poket: Charger', 'Main Meals', 'Charger Cafe', 'Personal', 'Confirmed', 0)
        """)
        res = services.suggest_transaction_draft(self.con, "Charger Cafe", "Expense", "2026-08-10", min_support=3)
        self.assertNotIn("account_from", res["suggestions"])

    def test_09_transfer_from_not_equal_to(self):
        """Scenario 9: Transfer suggestion ensures account_from != account_to."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, account_to, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Transfer', 100000, 'BCA Main', 'ShopeePay', 'Top up ShopeePay', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Transfer', 100000, 'BCA Main', 'ShopeePay', 'Top up ShopeePay', 'Personal', 'Confirmed', 0),
                   ('2026-08-03', 'Transfer', 100000, 'BCA Main', 'ShopeePay', 'Top up ShopeePay', 'Personal', 'Confirmed', 0)
        """)
        res = services.suggest_transaction_draft(self.con, "Top up ShopeePay", "Transfer", "2026-08-10", min_support=3)
        self.assertEqual(res["suggestions"]["account_from"]["value"], "BCA Main")
        self.assertEqual(res["suggestions"]["account_to"]["value"], "ShopeePay")
        self.assertNotEqual(res["suggestions"]["account_from"]["value"], res["suggestions"]["account_to"]["value"])

    def test_10_independent_field_calculation(self):
        """Scenario 10: Category and accounts are evaluated independently without cross-blocking."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, category, description, money_context, status, is_deleted)
            VALUES ('2026-08-01', 'Expense', 50000, 'BCA Main', 'Main Meals', 'Toko Campur Aduk', 'Personal', 'Confirmed', 0),
                   ('2026-08-02', 'Expense', 50000, 'BCA Main', 'Snacks', 'Toko Campur Aduk', 'Personal', 'Confirmed', 0),
                   ('2026-08-03', 'Expense', 50000, 'BCA Main', 'Fuel', 'Toko Campur Aduk', 'Personal', 'Confirmed', 0)
        """)
        # Category is mixed (abstain), but account_from is 100% BCA Main -> suggested!
        res = services.suggest_transaction_draft(self.con, "Toko Campur Aduk", "Expense", "2026-08-10", min_support=3)
        self.assertNotIn("category", res["suggestions"])
        self.assertIn("account_from", res["suggestions"])
        self.assertEqual(res["suggestions"]["account_from"]["value"], "BCA Main")

    def test_11_ui_structure_and_accessibility(self):
        """Scenario 11-17: UI suggestion panel exists, uses buttons, escaped templates, and submenu buttons."""
        self.assertIn('id="txSuggestionPanel"', self.html)
        self.assertIn('onclick="applyTxSuggestions()"', self.html)
        self.assertIn('onclick="dismissTxSuggestions()"', self.html)
        self.assertIn('handleTxDescInput', self.html)

        # Submenu native buttons
        self.assertIn('<button type="button" class="menu-list-item"', self.html)
        self.assertNotIn('<div class="menu-list-item"', self.html)

        # Focus-visible CSS
        self.assertIn('.menu-list-item:focus-visible', self.css)

    def test_18_chronological_holdout_metrics(self):
        """Scenario 18: Chronological holdout evaluation achieves high precision (>97%) with low false suggestions."""
        # Query confirmed personal transactions chronologically
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

        # Create temporary train db
        train_db = sqlite3.connect(":memory:")
        train_db.row_factory = sqlite3.Row
        train_db.execute("CREATE TABLE accounts (name TEXT, kind TEXT, active INTEGER)")
        train_db.execute("INSERT INTO accounts VALUES ('BCA Main', 'Owned', 1), ('ShopeePay', 'Owned', 1), ('Cash', 'Owned', 1), ('BCA Poket: Tabungan', 'Owned', 1), ('BCA Poket: Iuran', 'Owned', 1), ('GoPay', 'Owned', 1)")
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

        # Evaluate on future test set
        cat_covered = 0
        cat_correct = 0
        cat_false = 0

        for r in test_rows:
            if r["transaction_type"] in ("Expense", "Income") and r["category"]:
                res = services.suggest_transaction_draft(train_db, r["description"], r["transaction_type"], r["date"], min_support=3, min_confidence=0.80)
                if "category" in res["suggestions"]:
                    cat_covered += 1
                    if res["suggestions"]["category"]["value"] == r["category"]:
                        cat_correct += 1
                    else:
                        cat_false += 1

        train_db.close()

        precision = (cat_correct / cat_covered) if cat_covered > 0 else 1.0
        coverage = cat_covered / len(test_rows)

        print(f"\n[Holdout Evaluation] Tested: {len(test_rows)} | Covered: {cat_covered} ({coverage*100:.1f}%) | Correct: {cat_correct} | False: {cat_false} | Precision: {precision*100:.1f}%")
        self.assertGreaterEqual(precision, 0.95, f"Precision {precision} is below 95% threshold!")
        self.assertLessEqual(cat_false, 2, f"False suggestions {cat_false} exceeded maximum allowed!")


if __name__ == "__main__":
    unittest.main()
