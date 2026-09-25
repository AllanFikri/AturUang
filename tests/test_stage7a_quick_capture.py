"""
Universal Ingestion Stage 7A — Owner Experience MVP Test Suite.

Comprehensive unit tests verifying:
1. Natural Indonesian financial grammar parser (quick_capture.py).
2. Read-only query service for balances and monthly summaries (query_service.py).
3. Web composer & Safe Apply preview binding (web_composer.py).
4. Automated backup creation, idempotent submission, and zero impact on production database.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aturuang.quick_capture import (
    QuickCaptureResult,
    parse_quick_capture,
)
from aturuang.query_service import (
    get_account_balances,
    get_monthly_summary,
    get_pending_reviews,
    get_read_only_connection,
)
from aturuang.web_composer import (
    QuickCaptureComposer,
)
from aturuang.safe_apply import (
    ApplyCandidate,
    ApplyResult,
    CandidateLifecycleState,
    SafeApplyEngine,
)

EXPECTED_PRODUCTION_DB_HASH = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
PRODUCTION_DB_PATH = Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db")


def compute_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class TestStage7AQuickCapture(unittest.TestCase):
    """Stage 7A test cases covering parser, query services, web composer, and safety invariants."""

    @classmethod
    def setUpClass(cls) -> None:
        if PRODUCTION_DB_PATH.exists():
            cls.initial_prod_hash = compute_sha256(PRODUCTION_DB_PATH)
        else:
            cls.initial_prod_hash = None

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.test_db = self.td / "test_money_tracks.db"
        self.backup_dir = self.td / "backups"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_simple_expense_natural_grammar(self) -> None:
        """1. Parses '-25rb makan' correctly into an EXPENSE mutation with Decimal amount."""
        res = parse_quick_capture("-25rb makan", default_account="Cash")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.is_valid)
        self.assertIn(res.transaction_type, ("EXPENSE", "Expense"))
        self.assertEqual(res.amount, Decimal("25000.00"))
        self.assertEqual(res.account_from, "Cash")
        self.assertEqual(res.account_to, "")
        self.assertEqual(res.description, "makan")
        self.assertEqual(res.category, "Main Meals")
        self.assertIsNotNone(res.candidate)
        self.assertEqual(len(res.candidate.mutations), 1)
        self.assertEqual(res.candidate.mutations[0].transaction_type, "Expense")
        self.assertEqual(res.candidate.mutations[0].amount, Decimal("25000.00"))

    def test_parse_simple_income_natural_grammar(self) -> None:
        """2. Parses '+5jt gaji' correctly into an INCOME mutation."""
        res = parse_quick_capture("+5jt gaji", default_account="Cash")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.is_valid)
        self.assertIn(res.transaction_type, ("INCOME", "Income"))
        self.assertEqual(res.amount, Decimal("5000000.00"))
        self.assertEqual(res.account_from, "")
        self.assertEqual(res.account_to, "Cash")
        self.assertEqual(res.description, "gaji")
        self.assertEqual(res.category, "Income")
        self.assertIsNotNone(res.candidate)
        self.assertEqual(res.candidate.mutations[0].transaction_type, "Income")

    def test_parse_internal_transfer_grammar(self) -> None:
        """3. Parses 'transfer 500k bca ke jago' correctly into an INTERNAL_TRANSFER."""
        res = parse_quick_capture("transfer 500k bca ke jago")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.is_valid)
        self.assertIn(res.transaction_type, ("INTERNAL_TRANSFER", "Transfer"))
        self.assertEqual(res.amount, Decimal("500000.00"))
        self.assertIn(res.account_from, ("BCA", "BCA Main"))
        self.assertIn(res.account_to, ("Jago", "Jago Main"))
        self.assertIsNotNone(res.candidate)
        self.assertEqual(res.candidate.mutations[0].transaction_type, "Transfer")
        self.assertEqual(res.candidate.mutations[0].account_from, res.account_from)
        self.assertEqual(res.candidate.mutations[0].account_to, res.account_to)

    def test_fail_closed_on_ambiguous_grammar(self) -> None:
        """4. Unparsable or ambiguous sentences yield REVIEW_REQUIRED with diagnostic reason."""
        ambiguous_cases = [
            ("makan bakso enak tanpa nominal", "MISSING_AMOUNT"),
            ("transfer 500k", "MISSING_TRANSFER_DESTINATION"),
            ("transfer 500k bca ke bca", "SAME_SOURCE_AND_DESTINATION"),
            ("-0rb beli angin", "NON_POSITIVE_AMOUNT"),
            ("", "EMPTY_INPUT"),
            ("   ", "EMPTY_INPUT"),
        ]
        for text, expected_reason_code in ambiguous_cases:
            res = parse_quick_capture(text)
            self.assertEqual(res.status, "REVIEW_REQUIRED", f"Failed for text: '{text}'")
            self.assertFalse(res.is_valid)
            self.assertIsNotNone(res.diagnostic_reason)
            self.assertIn(expected_reason_code, res.diagnostic_reason)

    def test_exact_decimal_preservation_in_quick_capture(self) -> None:
        """5. Ensures zero float rounding errors and strict Decimal quantization."""
        cases = [
            ("-12.5rb jajan", Decimal("12500.00")),
            ("+3.75jt freelance", Decimal("3750000.00")),
            ("-99.99k belanja", Decimal("99990.00")),
            ("-123.456,78 tagihan", Decimal("123456.78")),
            ("-25000.50 belanja", Decimal("25000.50")),
        ]
        for text, expected_amount in cases:
            res = parse_quick_capture(text)
            self.assertEqual(res.status, "SUCCESS")
            self.assertIsInstance(res.amount, Decimal)
            self.assertEqual(res.amount, expected_amount)
            # Ensure candidate mutation preserves exact Decimal
            mut_amt = res.candidate.mutations[0].amount
            self.assertIsInstance(mut_amt, Decimal)
            self.assertEqual(mut_amt, expected_amount)

    def test_query_service_account_balances(self) -> None:
        """6. Validates read-only deterministic account balance calculation."""
        # Setup synthetic database with accounts and transactions
        con = sqlite3.connect(self.test_db)
        con.executescript("""
            CREATE TABLE accounts (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                current_balance REAL,
                balance_date TEXT,
                protected INTEGER NOT NULL DEFAULT 0,
                protected_amount REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO accounts VALUES ('BCA Main', 'Owned', 5000000.00, '2026-09-20', 0, 0, 1);
            INSERT INTO accounts VALUES ('GoPay', 'Owned', 250000.00, '2026-09-20', 0, 0, 1);
            INSERT INTO accounts VALUES ('Old Suspense', 'Suspense', 0.00, '2026-09-20', 0, 0, 0);

            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                time TEXT DEFAULT '',
                transaction_type TEXT NOT NULL,
                amount REAL NOT NULL,
                account_from TEXT DEFAULT '',
                account_to TEXT DEFAULT '',
                status TEXT DEFAULT 'Confirmed',
                is_deleted INTEGER DEFAULT 0
            );
        """)
        con.close()

        bals = get_account_balances(self.test_db)
        self.assertEqual(bals["count"], 3)
        self.assertEqual(bals["total_balance"], Decimal("5250000.00"))
        bca_info = next(a for a in bals["accounts"] if a["name"] == "BCA Main")
        self.assertEqual(bca_info["balance"], Decimal("5000000.00"))
        self.assertTrue(bca_info["active"])

    def test_query_service_monthly_summary(self) -> None:
        """7. Validates monthly cash flow aggregation and expense categorization."""
        con = sqlite3.connect(self.test_db)
        con.executescript("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                time TEXT DEFAULT '',
                transaction_type TEXT NOT NULL,
                amount REAL NOT NULL,
                account_from TEXT DEFAULT '',
                account_to TEXT DEFAULT '',
                category TEXT DEFAULT '',
                description TEXT DEFAULT '',
                exclude_from_budget INTEGER DEFAULT 0,
                status TEXT DEFAULT 'Confirmed',
                is_deleted INTEGER DEFAULT 0
            );
            INSERT INTO transactions VALUES (1, '2026-09-05', '10:00:00', 'Income', 10000000.00, '', 'BCA Main', 'Income', 'gaji', 0, 'Confirmed', 0);
            INSERT INTO transactions VALUES (2, '2026-09-10', '12:30:00', 'Expense', 50000.00, 'BCA Main', '', 'Main Meals', 'lunch', 0, 'Confirmed', 0);
            INSERT INTO transactions VALUES (3, '2026-09-15', '15:00:00', 'Expense', 100000.00, 'GoPay', '', 'Fuel', 'bensin', 0, 'Confirmed', 0);
            INSERT INTO transactions VALUES (4, '2026-09-20', '18:00:00', 'Expense', 500000.00, 'BCA Main', '', 'Fashion', 'baju', 1, 'Confirmed', 0);
            -- Different month (should be excluded)
            INSERT INTO transactions VALUES (5, '2026-08-25', '12:00:00', 'Expense', 75000.00, 'BCA Main', '', 'Main Meals', 'aug meal', 0, 'Confirmed', 0);
        """)
        con.close()

        summary = get_monthly_summary(2026, 9, self.test_db)
        self.assertEqual(summary["year"], 2026)
        self.assertEqual(summary["month"], 9)
        self.assertEqual(summary["total_income"], Decimal("10000000.00"))
        self.assertEqual(summary["total_expense"], Decimal("650000.00"))
        self.assertEqual(summary["net_cash_flow"], Decimal("9350000.00"))
        self.assertEqual(summary["budget_expense"], Decimal("150000.00"))
        self.assertEqual(summary["excluded_expense"], Decimal("500000.00"))
        self.assertEqual(summary["expense_by_category"]["Main Meals"], Decimal("50000.00"))
        self.assertEqual(summary["expense_by_category"]["Fuel"], Decimal("100000.00"))
        self.assertEqual(summary["expense_by_category"]["Fashion"], Decimal("500000.00"))
        self.assertEqual(summary["transaction_count"], 4)

    def test_web_composer_preview_generation(self) -> None:
        """8. Web Composer generates correct deterministic preview hash and candidate breakdown."""
        composer = QuickCaptureComposer(self.test_db, self.backup_dir)
        preview = composer.preview("-25rb makan bakso gopay")
        self.assertEqual(preview["status"], "SUCCESS")
        self.assertIsNotNone(preview["preview_hash"])
        self.assertEqual(len(preview["preview_hash"]), 64)
        self.assertIsNotNone(preview["candidate_id"])
        self.assertIsNotNone(preview["idempotency_key"])
        self.assertEqual(preview["amount"], "25000.00")
        self.assertEqual(preview["account_from"], "GoPay")
        self.assertEqual(preview["description"], "makan bakso")
        self.assertEqual(len(preview["mutations"]), 1)

    def test_web_composer_confirm_and_apply_flow(self) -> None:
        """9. End-to-end composer to Stage 6 Safe Apply flow."""
        composer = QuickCaptureComposer(self.test_db, self.backup_dir)
        preview = composer.preview("-25rb makan bakso gopay")
        self.assertEqual(preview["status"], "SUCCESS")

        apply_result = composer.confirm_and_apply(preview)
        self.assertTrue(apply_result.success)
        self.assertEqual(apply_result.state, CandidateLifecycleState.APPLIED)
        self.assertEqual(len(apply_result.applied_row_ids), 1)
        self.assertFalse(apply_result.is_idempotent_replay)

        # Verify transaction row exists in database
        con = sqlite3.connect(self.test_db)
        row = con.execute("SELECT amount, account_from, description, status FROM transactions WHERE id=?", (apply_result.applied_row_ids[0],)).fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 25000.00)
        self.assertEqual(row[1], "GoPay")
        self.assertEqual(row[2], "makan bakso")
        self.assertEqual(row[3], "Confirmed")

    def test_idempotent_quick_capture_submission(self) -> None:
        """10. Re-submitting the exact same capture does not duplicate ledger rows."""
        composer = QuickCaptureComposer(self.test_db, self.backup_dir)
        preview = composer.preview("-50rb pulsa bca")
        self.assertEqual(preview["status"], "SUCCESS")

        # First apply
        res1 = composer.confirm_and_apply(preview)
        self.assertTrue(res1.success)
        self.assertFalse(res1.is_idempotent_replay)

        # Second apply (replay with same candidate/payload)
        res2 = composer.confirm_and_apply(preview)
        self.assertTrue(res2.success)
        self.assertTrue(res2.is_idempotent_replay)
        self.assertEqual(res1.applied_row_ids, res2.applied_row_ids)

        # Assert row count remains strictly 1
        con = sqlite3.connect(self.test_db)
        cnt = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        con.close()
        self.assertEqual(cnt, 1)

    def test_pre_apply_backup_triggered_on_composer_apply(self) -> None:
        """11. Confirms backup creation during web apply and zero backup creation on replay."""
        composer = QuickCaptureComposer(self.test_db, self.backup_dir)
        preview = composer.preview("-30rb bensin shell cash")

        # Prior to apply: no backups
        backups_pre = list(self.backup_dir.glob("*.db"))
        self.assertEqual(len(backups_pre), 0)

        # First apply triggers atomic backup
        res1 = composer.confirm_and_apply(preview)
        self.assertTrue(res1.success)
        backups_post1 = list(self.backup_dir.glob("*.db"))
        self.assertEqual(len(backups_post1), 1)

        # Verify integrity of backup
        b_conn = sqlite3.connect(backups_post1[0])
        check = b_conn.execute("PRAGMA quick_check").fetchone()[0]
        b_conn.close()
        self.assertEqual(check, "ok")

        # Idempotent replay must NOT create orphaned backup
        res2 = composer.confirm_and_apply(preview)
        self.assertTrue(res2.is_idempotent_replay)
        backups_post2 = list(self.backup_dir.glob("*.db"))
        self.assertEqual(len(backups_post2), 1)

    def test_read_only_query_enforcement(self) -> None:
        """12. Asserts query service enforces read-only mode and blocks write operations."""
        con = sqlite3.connect(self.test_db)
        con.execute("CREATE TABLE test_tbl (id INT)")
        con.close()

        ro_conn = get_read_only_connection(self.test_db)
        try:
            with self.assertRaises(sqlite3.OperationalError) as cm:
                ro_conn.execute("INSERT INTO test_tbl VALUES (1)")
            self.assertIn("readonly", str(cm.exception).lower())

            with self.assertRaises(sqlite3.OperationalError) as cm:
                ro_conn.execute("CREATE TABLE evil (id INT)")
            self.assertIn("readonly", str(cm.exception).lower())
        finally:
            ro_conn.close()

    def test_sanitized_quick_capture_diagnostics(self) -> None:
        """13. Asserts zero PII, secret keys, or local absolute paths in diagnostics."""
        dirty_input = "beli rahasia C:\\\\Users\\\\Admin\\\\Desktop\\\\secret.key -10rb token"
        res = parse_quick_capture(dirty_input)
        if res.diagnostic_reason:
            self.assertNotIn("Users", res.diagnostic_reason)
            self.assertNotIn("Admin", res.diagnostic_reason)
            self.assertNotIn("Desktop", res.diagnostic_reason)

    def test_unrecognized_currency_fail_closed(self) -> None:
        """14. Invalid or foreign currency units fail closed safely with UNRECOGNIZED_CURRENCY."""
        foreign_inputs = [
            "-25usd makan siang",
            "100 dollar beli baju",
            "bayar 50eur kopi",
            "100sgd beli sepatu",
            "-5000jpy ramen",
            "1000000 krw belanja seoul",
            "transfer $500 bca ke jago",
        ]
        for fi in foreign_inputs:
            res = parse_quick_capture(fi)
            self.assertEqual(res.status, "REVIEW_REQUIRED", f"Failed for input: '{fi}'")
            self.assertFalse(res.is_valid)
            self.assertIsNotNone(res.diagnostic_reason)
            self.assertIn("UNRECOGNIZED_CURRENCY", res.diagnostic_reason)

    def test_production_db_untouched_during_stage7a_tests(self) -> None:
        """15. Asserts production database SHA-256 is strictly unchanged throughout tests."""
        if not PRODUCTION_DB_PATH.exists():
            self.skipTest("Production database does not exist on this host.")
        current_hash = compute_sha256(PRODUCTION_DB_PATH)
        self.assertEqual(
            current_hash,
            EXPECTED_PRODUCTION_DB_HASH,
            "CRITICAL VIOLATION: Production database hash was mutated during Stage 7A testing!",
        )
        if self.initial_prod_hash:
            self.assertEqual(
                current_hash,
                self.initial_prod_hash,
                "CRITICAL VIOLATION: Production database hash changed from initial setup!",
            )

    def test_web_composer_http_dispatch(self) -> None:
        """16. Supplemental: Validates HTTP handler dispatching for HTML and JSON endpoints."""
        composer = QuickCaptureComposer(self.test_db, self.backup_dir)

        # GET /
        code, headers, body = composer.handle_request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Catat Cepat", body)

        # GET /api/quick-capture/preview
        code, headers, body = composer.handle_request("GET", "/api/quick-capture/preview", query={"q": ["-15rb nasi goreng"]})
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "SUCCESS")
        self.assertEqual(data["amount"], "15000.00")

        # POST /api/quick-capture/apply
        code, headers, body = composer.handle_request("POST", "/api/quick-capture/apply", body=json.dumps(data))
        self.assertEqual(code, 200)
        apply_res = json.loads(body.decode("utf-8"))
        self.assertTrue(apply_res["success"])

        # GET /api/quick-capture/balances
        code, headers, body = composer.handle_request("GET", "/api/quick-capture/balances")
        self.assertEqual(code, 200)
        bal_res = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(bal_res["count"], 1)

    def test_query_service_pending_reviews(self) -> None:
        """17. Supplemental: Validates get_pending_reviews aggregation."""
        con = sqlite3.connect(self.test_db)
        con.executescript("""
            CREATE TABLE import_candidates (
                id INTEGER PRIMARY KEY,
                raw_event_id INTEGER DEFAULT 1,
                fingerprint TEXT UNIQUE,
                date TEXT,
                time TEXT DEFAULT '',
                transaction_type TEXT,
                amount REAL,
                account_from TEXT DEFAULT '',
                account_to TEXT DEFAULT '',
                description TEXT DEFAULT '',
                category TEXT DEFAULT '',
                confidence TEXT DEFAULT 'high',
                review_status TEXT DEFAULT 'Pending',
                review_notes TEXT DEFAULT ''
            );
            INSERT INTO import_candidates (id, fingerprint, date, transaction_type, amount, review_status, review_notes)
            VALUES (1, 'fp_123', '2026-09-20', 'Expense', 45000.0, 'Pending', 'Uncertain category');
        """)
        con.close()

        pending = get_pending_reviews(self.test_db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "1")
        self.assertEqual(pending[0]["amount"], Decimal("45000.00"))
        self.assertEqual(pending[0]["status"], "Pending")


if __name__ == "__main__":
    unittest.main()
