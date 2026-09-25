import datetime as dt
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from aturuang import db, services

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROD_DB = PROJECT_ROOT / "runtime" / "money_tracks.db"


class TestUISemanticFixV1(unittest.TestCase):
    """Focused tests for UI semantic fixes: report_data derived spend and recurring pattern labeling."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_money_tracks.db"
        db.init_db(self.test_db_path)
        self.con = sqlite3.connect(self.test_db_path)
        self.con.row_factory = sqlite3.Row

        # Invariant check: production DB must not be modified
        self.prod_hash_before = (
            hashlib.sha256(PROD_DB.read_bytes()).hexdigest() if PROD_DB.exists() else None
        )

    def tearDown(self) -> None:
        self.con.close()
        self.temp_dir.cleanup()
        if self.prod_hash_before and PROD_DB.exists():
            prod_hash_after = hashlib.sha256(PROD_DB.read_bytes()).hexdigest()
            self.assertEqual(
                self.prod_hash_before,
                prod_hash_after,
                "Production DB was modified during test run!",
            )

    def test_1_report_data_derived_rows_use_amount(self) -> None:
        """Test 1: report_data for derived rule rows uses amount instead of budget_effect."""
        self.con.execute(
            """INSERT INTO transactions (
                date, transaction_type, amount, account_from, description, category,
                money_context, status, budget_rule_version, budget_effect, exclude_from_budget
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-09-10",
                "Expense",
                50000.0,
                "BCA Main",
                "Belanja Supermarket",
                "Groceries & Daily Needs",
                "Personal",
                "Confirmed",
                "derived",
                0.0,
                0,
            ),
        )
        self.con.commit()

        res = services.report_data(self.con, month="2026-09")
        monthly = res["monthly"]
        self.assertTrue(len(monthly) > 0, "Expected at least one monthly report row")
        sep_report = next((m for m in monthly if m["month"] == "2026-09"), None)
        self.assertIsNotNone(sep_report, "2026-09 row should be present")
        self.assertEqual(sep_report["expense"], 50000.0)

    def test_2_report_data_legacy_still_uses_budget_effect(self) -> None:
        """Test 2: report_data for legacy rule rows preserves budget_effect behavior."""
        self.con.execute(
            """INSERT INTO transactions (
                date, transaction_type, amount, account_from, description, category,
                money_context, status, budget_rule_version, budget_effect, exclude_from_budget
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-09-12",
                "Expense",
                50000.0,
                "BCA Main",
                "Belanja Legacy Discrepancy",
                "Groceries & Daily Needs",
                "Personal",
                "Confirmed",
                "legacy",
                30000.0,
                0,
            ),
        )
        self.con.commit()

        res = services.report_data(self.con, month="2026-09")
        monthly = res["monthly"]
        sep_report = next((m for m in monthly if m["month"] == "2026-09"), None)
        self.assertIsNotNone(sep_report)
        self.assertEqual(sep_report["expense"], 30000.0)

    def test_3_recurring_income_classification(self) -> None:
        """Test 3: Recurring income transactions get classified as 'Pola Pemasukan'."""
        # 4 rows, 30-day intervals with amount variance so amt_cv > 0.10 (medium confidence branch)
        incomes = [
            ("2026-05-01", 10000000.0),
            ("2026-05-31", 7000000.0),
            ("2026-06-30", 12000000.0),
            ("2026-07-30", 9500000.0),
        ]
        for d, amt in incomes:
            self.con.execute(
                """INSERT INTO transactions (
                    date, transaction_type, amount, account_to, description, category,
                    money_context, status, budget_rule_version, budget_effect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d,
                    "Income",
                    amt,
                    "BCA Main",
                    "Gaji Bulanan",
                    "Income",
                    "Personal",
                    "Confirmed",
                    "derived",
                    0.0,
                ),
            )
        self.con.commit()

        patterns = services.detect_recurring_patterns(
            self.con, as_of_date="2026-08-15", min_support=3
        )
        gaji_p = next(
            (p for p in patterns if "gaji bulanan" in p["normalized_merchant"].lower()),
            None,
        )
        self.assertIsNotNone(gaji_p, "Expected recurring pattern for Gaji Bulanan")
        self.assertEqual(gaji_p["transaction_type"], "Income")
        self.assertEqual(gaji_p["classification"], "Pola Pemasukan")
        self.assertEqual(gaji_p["confidence"], "medium")
        self.assertIn("Pola pemasukan berulang", gaji_p["reason"])

    def test_4_recurring_expense_classification_unchanged(self) -> None:
        """Test 4: Recurring expense transactions remain classified as 'Pola Belanja' (regression guard)."""
        # 4 rows with irregular intervals and varying amounts (medium confidence branch)
        expenses = [
            ("2026-06-01", 35000.0),
            ("2026-06-05", 50000.0),
            ("2026-06-14", 30000.0),
            ("2026-06-25", 45000.0),
        ]
        for d, amt in expenses:
            self.con.execute(
                """INSERT INTO transactions (
                    date, transaction_type, amount, account_from, description, category,
                    money_context, status, budget_rule_version, budget_effect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d,
                    "Expense",
                    amt,
                    "BCA Main",
                    "Kopi Harian",
                    "Cafe & Drinks",
                    "Personal",
                    "Confirmed",
                    "derived",
                    0.0,
                ),
            )
        self.con.commit()

        patterns = services.detect_recurring_patterns(
            self.con, as_of_date="2026-07-01", min_support=3
        )
        kopi_p = next(
            (p for p in patterns if "kopi harian" in p["normalized_merchant"].lower()),
            None,
        )
        self.assertIsNotNone(kopi_p, "Expected recurring pattern for Kopi Harian")
        self.assertEqual(kopi_p["transaction_type"], "Expense")
        self.assertEqual(kopi_p["classification"], "Pola Belanja")
        self.assertEqual(kopi_p["confidence"], "medium")
        self.assertIn("Pola belanja berulang", kopi_p["reason"])


if __name__ == "__main__":
    unittest.main()
