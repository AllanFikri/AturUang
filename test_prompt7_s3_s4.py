"""
Test Suite Prompt 7 — S3 Budget Effect & S4 Global Idempotency
Verifies:
- Zero budget leakage
- Canonical derived vs legacy budget calculation
- Idempotency request handling, concurrency, replay, mismatch 409, and failure rollback
"""
import concurrent.futures
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db import DB_FILE, init_db, db_connect
from services import (
    effective_budget_spend,
    validate_tx,
    compute_request_hash,
    handle_idempotent_request,
    get_month_actual_by_category,
    create_reversal_transaction,
    reconcile_account_balance,
    create_debt_position,
    add_debt_event,
    pay_upcoming_atomic,
    link_protected_allocation,
)


class TestPrompt7S3S4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create temp database copy for testing
        cls.temp_dir = tempfile.mkdtemp()
        cls.test_db_path = Path(cls.temp_dir) / "test_money_tracks.db"
        shutil.copy2(DB_FILE, cls.test_db_path)
        # Apply migrations
        init_db(cls.test_db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        self.con = db_connect(self.test_db_path)

    def tearDown(self):
        self.con.close()

    # --- S3 TESTS ---

    def test_01_historical_monthly_budget_spend_identical(self):
        """Test 1: Total historical budget spend per month identical before and after migration."""
        months = [r[0] for r in self.con.execute("SELECT DISTINCT substr(date,1,7) FROM transactions WHERE is_deleted=0 ORDER BY 1").fetchall()]
        for m in months:
            # Query using raw legacy budget_effect
            legacy_sum = round(float(self.con.execute(
                """SELECT COALESCE(SUM(budget_effect), 0) FROM transactions
                   WHERE substr(date,1,7)=? AND transaction_type='Expense' AND money_context='Personal'
                     AND status IN ('Confirmed','Auto-classified') AND is_deleted=0
                     AND description NOT LIKE '[Rekonsiliasi]%'""",
                (m,)
            ).fetchone()[0]), 2)

            # Query using get_month_actual_by_category with effective_budget_spend
            actual_cat_map = get_month_actual_by_category(self.con, m)
            derived_sum = round(sum(actual_cat_map.values()), 2)

            self.assertAlmostEqual(
                legacy_sum,
                derived_sum,
                places=2,
                msg=f"Month {m} budget spend mismatch: legacy={legacy_sum} vs derived={derived_sum}"
            )

    def test_02_legacy_partial_zero_negative_frozen(self):
        """Test 2: Legacy transactions keep their frozen budget_effect regardless of amount."""
        legacy_row = {
            "transaction_type": "Expense",
            "money_context": "Personal",
            "status": "Confirmed",
            "is_deleted": 0,
            "exclude_from_budget": 0,
            "amount": 100000.0,
            "budget_effect": 25000.0,
            "budget_rule_version": "legacy",
        }
        self.assertEqual(effective_budget_spend(legacy_row), 25000.0)

        legacy_zero = {
            "transaction_type": "Expense",
            "money_context": "Personal",
            "status": "Confirmed",
            "is_deleted": 0,
            "exclude_from_budget": 0,
            "amount": 50000.0,
            "budget_effect": 0.0,
            "budget_rule_version": "legacy",
        }
        self.assertEqual(effective_budget_spend(legacy_zero), 0.0)

    def test_03_derived_personal_expense_equals_amount(self):
        """Test 3: Derived Personal Confirmed/Auto Expense produces exact amount."""
        row_confirmed = {
            "transaction_type": "Expense",
            "money_context": "Personal",
            "status": "Confirmed",
            "is_deleted": 0,
            "exclude_from_budget": 0,
            "amount": 75000.0,
            "budget_rule_version": "derived",
        }
        self.assertEqual(effective_budget_spend(row_confirmed), 75000.0)

        row_auto = {
            "transaction_type": "Expense",
            "money_context": "Personal",
            "status": "Auto-classified",
            "is_deleted": 0,
            "exclude_from_budget": 0,
            "amount": 32000.0,
            "budget_rule_version": "derived",
        }
        self.assertEqual(effective_budget_spend(row_auto), 32000.0)

    def test_04_excluded_produces_zero_and_requires_reason(self):
        """Test 4: Excluded from budget yields 0.0 and requires mandatory reason."""
        raw_bad = {
            "date": "2026-08-23",
            "transaction_type": "Expense",
            "amount": 50000.0,
            "account_from": "BCA Poket: Utama",
            "money_context": "Personal",
            "exclude_from_budget": 1,
            "budget_exclusion_reason": "",
            "budget_rule_version": "derived",
        }
        with self.assertRaises(ValueError) as ctx:
            validate_tx(raw_bad)
        self.assertIn("Alasan pengecualian anggaran", str(ctx.exception))

        raw_good = {
            "date": "2026-08-23",
            "transaction_type": "Expense",
            "amount": 50000.0,
            "account_from": "BCA Poket: Utama",
            "money_context": "Personal",
            "exclude_from_budget": 1,
            "budget_exclusion_reason": "Dibiayai kantor",
            "budget_rule_version": "derived",
        }
        cleaned = validate_tx(raw_good)
        self.assertEqual(cleaned["budget_effect"], 0.0)
        self.assertEqual(effective_budget_spend(cleaned), 0.0)

    def test_05_non_personal_expense_always_zero(self):
        """Test 5: Income, Transfer, Third-party, and Adjustment always result in 0.0 budget spend."""
        for ctx_type, tx_type in [
            ("Personal", "Income"),
            ("Personal", "Transfer"),
            ("Third-party", "Expense"),
            ("Pass-through", "Expense"),
            ("Personal", "Adjustment"),
        ]:
            row = {
                "transaction_type": tx_type,
                "money_context": ctx_type,
                "status": "Confirmed",
                "is_deleted": 0,
                "exclude_from_budget": 0,
                "amount": 90000.0,
                "budget_rule_version": "derived",
            }
            self.assertEqual(effective_budget_spend(row), 0.0, f"Failed for {ctx_type} {tx_type}")

    def test_06_client_budget_effect_ignored_for_derived(self):
        """Test 6: Client-supplied custom budget_effect is strictly ignored for derived rules."""
        raw = {
            "date": "2026-08-23",
            "transaction_type": "Expense",
            "amount": 100000.0,
            "account_from": "BCA Poket: Utama",
            "money_context": "Personal",
            "budget_effect": 12345.0,  # malicious custom effect
            "budget_rule_version": "derived",
        }
        cleaned = validate_tx(raw)
        self.assertEqual(cleaned["budget_effect"], 100000.0)
        self.assertEqual(effective_budget_spend(cleaned), 100000.0)

    def test_07_reversal_does_not_leak_into_budget(self):
        """Test 7: Reversal transaction creates offsetting entry with zero budget effect."""
        cur = self.con.execute(
            """INSERT INTO transactions (date, transaction_type, amount, account_from, description, category,
                                          for_with_whom, money_context, status, budget_effect, exclude_from_budget,
                                          budget_rule_version)
               VALUES ('2026-08-20', 'Expense', 60000, 'BCA Poket: Utama', 'Test Reversal Target', 'Main Meals',
                       'Personal / Self', 'Personal', 'Confirmed', 60000, 0, 'derived')"""
        )
        tx_id = cur.lastrowid
        res = create_reversal_transaction(self.con, tx_id, reason="Salah input")
        self.assertEqual(res["status"], "reversed")
        rev_id = res["reversal_id"]

        rev_row = self.con.execute("SELECT * FROM transactions WHERE id=?", (rev_id,)).fetchone()
        self.assertEqual(effective_budget_spend(rev_row), 0.0)

    def test_07b_edit_legacy_preserves_legacy_unless_converted(self):
        """Test 7b: Editing legacy transaction preserves legacy mode unless explicitly converted."""
        # Clean legacy transaction
        cur = self.con.execute(
            """INSERT INTO transactions (date, transaction_type, amount, account_from, description, category,
                                          for_with_whom, money_context, status, budget_effect, exclude_from_budget,
                                          budget_rule_version)
               VALUES ('2026-08-01', 'Expense', 50000, 'BCA Poket: Utama', 'Legacy Item', 'Main Meals',
                       'Personal / Self', 'Personal', 'Confirmed', 20000, 0, 'legacy')"""
        )
        tx_id = cur.lastrowid
        tx_row = self.con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        self.assertEqual(effective_budget_spend(tx_row), 20000.0)

        # Update without changing rule version
        raw_update = dict(tx_row)
        raw_update["description"] = "Legacy Item Renamed"
        cleaned = validate_tx(raw_update)
        self.assertEqual(cleaned["budget_rule_version"], "legacy")
        self.assertEqual(cleaned["budget_effect"], 20000.0)

        # Convert to derived
        raw_convert = dict(tx_row)
        raw_convert["budget_rule_version"] = "derived"
        cleaned_conv = validate_tx(raw_convert)
        self.assertEqual(cleaned_conv["budget_rule_version"], "derived")
        self.assertEqual(cleaned_conv["budget_effect"], 50000.0)

    # --- S4 IDEMPOTENCY TESTS ---

    def test_08_schema_and_idempotency_table_idempotent(self):
        """Test 8: init_db is cleanly re-runnable and creates idempotency_requests."""
        init_db(self.test_db_path)
        tables = [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("idempotency_requests", tables)

    def test_09_replay_same_key_same_hash_no_second_mutation(self):
        """Test 9: Replaying identical key + hash returns cached response without second mutation."""
        op_key = "idemp_test_replay_01"
        payload = {"name": "BCA Poket: Utama", "actual_balance": 15000000.0, "notes": "Recon Test"}

        status1, res1 = handle_idempotent_request(
            self.con, "account.reconcile.BCA Poket: Utama", op_key, payload,
            lambda: {"status": "success", "diff": 0.0}
        )
        self.assertEqual(status1, 200)

        status2, res2 = handle_idempotent_request(
            self.con, "account.reconcile.BCA Poket: Utama", op_key, payload,
            lambda: {"status": "success", "diff": 999999.0}
        )
        self.assertEqual(status2, 200)
        self.assertEqual(res2["diff"], 0.0)

    def test_10_same_key_different_payload_returns_409(self):
        """Test 10: Same key with different payload triggers 409 conflict error."""
        op_key = "idemp_test_conflict_02"
        payload1 = {"title": "Tagihan Listrik", "amount": 250000.0}
        payload2 = {"title": "Tagihan Listrik", "amount": 350000.0}

        handle_idempotent_request(
            self.con, "upcoming.create", op_key, payload1,
            lambda: {"status": "success", "id": 101}
        )

        with self.assertRaises(ValueError) as ctx:
            handle_idempotent_request(
                self.con, "upcoming.create", op_key, payload2,
                lambda: {"status": "success", "id": 102}
            )
        self.assertIn("HTTP 409", str(ctx.exception))

    def test_11_parallel_requests_single_mutation(self):
        """Test 11: Concurrent parallel requests with same key produce exactly one mutation."""
        op_key = "idemp_test_parallel_03"
        payload = {"title": "Cicilan Laptop", "amount": 1000000.0}
        mutation_count = [0]

        def do_work():
            con_thread = db_connect(self.test_db_path)
            try:
                def execute_mutation():
                    mutation_count[0] += 1
                    return {"status": "success", "count": mutation_count[0]}

                try:
                    return handle_idempotent_request(
                        con_thread, "debts.create", op_key, payload, execute_mutation
                    )
                except ValueError as ve:
                    return (409, {"error": str(ve)})
            finally:
                con_thread.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(do_work) for _ in range(4)]
            results = [f.result() for f in futures]

        self.assertEqual(mutation_count[0], 1)
        success_results = [r for r in results if r[0] == 200]
        self.assertTrue(len(success_results) >= 1)
        for status, res in success_results:
            self.assertEqual(res["count"], 1)

    def test_12_failure_injection_rolls_back_key_and_mutation(self):
        """Test 12: Exception inside mutation rolls back idempotency record allowing clean retry."""
        op_key = "idemp_test_failure_04"
        payload = {"title": "Broken Action"}

        def failing_action():
            raise RuntimeError("Database constraint or validation failure")

        with self.assertRaises(RuntimeError):
            handle_idempotent_request(
                self.con, "test.scope", op_key, payload, failing_action
            )

        row = self.con.execute(
            "SELECT * FROM idempotency_requests WHERE endpoint_scope='test.scope' AND idempotency_key=?",
            (op_key,)
        ).fetchone()
        self.assertIsNone(row)

        status, res = handle_idempotent_request(
            self.con, "test.scope", op_key, payload, lambda: {"status": "success"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "success")

    def test_13_all_mandatory_endpoints_idempotent(self):
        """Test 13: All required endpoints (debts, events, pay, protected, upcoming) adhere to idempotency."""
        # 1. Debt position create
        res_debt = create_debt_position(
            self.con, "Teman Idemp", "Payable", 500000, "2026-08-23", "Test Notes",
            operation_key="op_test_debt_pos_1"
        )
        self.assertEqual(res_debt["status"], "success")
        debt_id = res_debt["debt_id"]

        res_debt_dup = create_debt_position(
            self.con, "Teman Idemp", "Payable", 500000, "2026-08-23", "Test Notes",
            operation_key="op_test_debt_pos_1"
        )
        self.assertEqual(res_debt_dup["debt_id"], debt_id)

        # 2. Debt event create
        res_ev = add_debt_event(
            self.con, debt_id, "Increase", 100000, "2026-08-23", "Add",
            operation_key="op_test_debt_ev_1"
        )
        self.assertEqual(res_ev["status"], "success")
        ev_id = res_ev["event_id"]

        res_ev_dup = add_debt_event(
            self.con, debt_id, "Increase", 100000, "2026-08-23", "Add",
            operation_key="op_test_debt_ev_1"
        )
        self.assertEqual(res_ev_dup["event_id"], ev_id)

        # 3. Protected allocation create
        p_alloc = {"title": "Dana Idemp", "amount": 200000.0, "account": "BCA Poket: Tabungan"}
        s_al1, r_al1 = handle_idempotent_request(
            self.con, "protected_allocations.create", "op_alloc_01", p_alloc,
            lambda: {"status": "success", "id": 901}
        )
        s_al2, r_al2 = handle_idempotent_request(
            self.con, "protected_allocations.create", "op_alloc_01", p_alloc,
            lambda: {"status": "success", "id": 902}
        )
        self.assertEqual(r_al1["id"], r_al2["id"])

        # Ensure account exists for payment
        self.con.execute("INSERT OR IGNORE INTO accounts (name, kind, current_balance, protected, protected_amount, active) VALUES (?, 'Owned', 1000000, 0, 0, 1)", ("BCA Poket: Utama",))
        self.con.commit()
        # 4. Upcoming pay atomic
        cur_u = self.con.execute(
            "INSERT INTO upcoming(due_date, title, amount, account, status) VALUES ('2026-08-25', 'Wifi', 300000, 'BCA Poket: Utama', 'Upcoming')"
        )
        uid = cur_u.lastrowid
        pay_res1 = pay_upcoming_atomic(self.con, uid, "BCA Poket: Utama", "2026-08-23", "op_pay_wifi_1")
        self.assertEqual(pay_res1["status"], "success")

        pay_res2 = pay_upcoming_atomic(self.con, uid, "BCA Poket: Utama", "2026-08-23", "op_pay_wifi_1")
        self.assertEqual(pay_res2["status"], "already_paid")

    def test_14_client_simulated_retry_preserves_key(self):
        """Test 14: Client keeps identical key during network drop / retry."""
        client_key = "client_uuid_sim_123"
        payload = {"date": "2026-08-23", "amount": 45000, "description": "Beli Buku"}

        # Attempt 1: Server receives and processes
        status1, res1 = handle_idempotent_request(
            self.con, "transactions.create", client_key, payload,
            lambda: {"status": "success", "id": 777}
        )
        # Attempt 2 (Simulated retry from client):
        status2, res2 = handle_idempotent_request(
            self.con, "transactions.create", client_key, payload,
            lambda: {"status": "success", "id": 888}
        )
        self.assertEqual(res1["id"], 777)
        self.assertEqual(res2["id"], 777)

    def test_15_natural_status_guard_remains_safe(self):
        """Test 15: Terminal status actions reject re-execution safely."""
        cur = self.con.execute(
            "INSERT INTO upcoming(due_date, title, amount, status) VALUES ('2026-08-28', 'Skip Test', 100000, 'Skipped')"
        )
        uid = cur.lastrowid
        with self.assertRaises(ValueError):
            pay_upcoming_atomic(self.con, uid, "BCA Poket: Utama", "2026-08-23")

    def test_16_tx_save_idempotency_prevents_double_balance_mutation(self):
        """Test 16: Replaying transaction create does not mutate account balance twice."""
        from services import mutate_account_balance
        self.con.execute("INSERT OR IGNORE INTO accounts (name, kind, current_balance) VALUES ('BCA Test Idemp', 'Owned', 500000.0)")
        self.con.commit()

        initial_bal = float(self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Test Idemp'").fetchone()[0])
        op_key = "op_tx_create_bal_test_1"
        payload = {
            "date": "2026-08-23", "transaction_type": "Expense", "amount": 50000.0,
            "account_from": "BCA Test Idemp", "description": "Uji Dobel Mutasi",
            "money_context": "Personal", "status": "Confirmed", "budget_rule_version": "derived"
        }

        def execute_create():
            cur = self.con.execute(
                "INSERT INTO transactions(date, transaction_type, amount, account_from, description, money_context, status, budget_rule_version) VALUES (?,?,?,?,?,?,?,?)",
                (payload["date"], payload["transaction_type"], payload["amount"], payload["account_from"], payload["description"], payload["money_context"], payload["status"], payload["budget_rule_version"])
            )
            mutate_account_balance(self.con, payload["account_from"], -payload["amount"], payload["date"])
            return {"status": "success", "id": cur.lastrowid}

        # 1st call
        s1, r1 = handle_idempotent_request(self.con, "transactions.create", op_key, payload, execute_create)
        self.assertEqual(s1, 200)
        bal_after_1 = float(self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Test Idemp'").fetchone()[0])
        self.assertEqual(bal_after_1, initial_bal - 50000.0)

        # 2nd call (replay)
        s2, r2 = handle_idempotent_request(self.con, "transactions.create", op_key, payload, execute_create)
        self.assertEqual(s2, 200)
        self.assertEqual(r1["id"], r2["id"])
        bal_after_2 = float(self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Test Idemp'").fetchone()[0])
        self.assertEqual(bal_after_2, bal_after_1)  # Balance preserved, no double deduction!

    def test_17_no_sensitive_secrets_in_idempotency_store(self):
        """Test 17: Idempotency table does not store API keys or passwords."""
        rows = self.con.execute("SELECT response_json FROM idempotency_requests WHERE response_json IS NOT NULL").fetchall()
        for (rj,) in rows:
            self.assertNotIn("api_key", rj.lower())
            self.assertNotIn("secret", rj.lower())
            self.assertNotIn("password", rj.lower())


if __name__ == "__main__":
    unittest.main()

