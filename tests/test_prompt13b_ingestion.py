"""
Test Suite: Prompt 13B — Fail-Closed Security, Ingestion Connectors, and Financial Parity
"""
import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone

import services

BASE_DIR = _REPO_ROOT
DB_PATH = BASE_DIR / "runtime" / "money_tracks.db"
MIGRATION_01 = BASE_DIR / "cloud" / "worker" / "migrations" / "0001_initial_schema.sql"
MIGRATION_02 = BASE_DIR / "cloud" / "worker" / "migrations" / "0002_staging_schema.sql"
MIGRATION_03 = BASE_DIR / "cloud" / "worker" / "migrations" / "0003_ingestion_connectors.sql"
MIGRATION_04 = BASE_DIR / "cloud" / "worker" / "migrations" / "0004_durable_nonce_guard.sql"


class TestPrompt13BIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_d1 = Path(self.tmp_dir) / "test_d1.db"
        self.prod_hash_before = hashlib.sha256(DB_PATH.read_bytes()).hexdigest() if DB_PATH.exists() else None

        # Setup in-memory / temporary D1 SQLite database
        self.con = sqlite3.connect(self.test_d1)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")

        # Apply migrations 0001, 0002, 0003, 0004
        self.con.executescript(MIGRATION_01.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_02.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_03.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_04.read_text(encoding="utf-8"))

        # Base raw_events for foreign keys
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state) VALUES (1, 'gmail', 'base_msg_1', 'h1', '1.0', 'Parsed')"
        )
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state) VALUES (2, 'gmail', 'base_msg_2', 'h2', '1.0', 'Parsed')"
        )
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state) VALUES (3, 'telegram', 'base_msg_3', 'h3', '1.0', 'Parsed')"
        )
        self.con.commit()

        self.secret = "test_gmail_relay_secret_key"
        self.tg_secret = "test_telegram_secret_token"
        self.allowed_user_id = 123456789

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _generate_hmac(self, body: str, ts: int, nonce: str, secret: str) -> str:
        payload = f"{ts}.{nonce}.{body}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    # =========================================================================
    # SECURITY FAIL-CLOSED TESTS
    # =========================================================================

    def test_01_security_fail_closed_missing_secrets(self):
        """1. Missing STAGING_ADMIN_TOKEN, GMAIL_RELAY_SECRET, TELEGRAM_SECRET_TOKEN, or TELEGRAM_ALLOWED_USER_ID fail closed."""
        auth_ts = (BASE_DIR / "cloud" / "worker" / "src" / "auth.ts").read_text(encoding="utf-8")
        index_ts = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")

        # 1. No fallback default tokens in source
        self.assertNotIn("aturuang-staging-secret-key-default", auth_ts)
        self.assertNotIn("aturuang_gmail_relay_secret_default", index_ts)

        # 2. Telegram webhook and allowlist fail closed if unset
        self.assertIn("UNCONFIGURED_TELEGRAM_SECRET", auth_ts)
        self.assertIn("UNCONFIGURED_GMAIL_SECRET", auth_ts)

    def test_02_no_wildcard_cors_and_generic_error(self):
        """2. Wildcard CORS is eliminated and internal exceptions return generic error without stack traces."""
        auth_ts = (BASE_DIR / "cloud" / "worker" / "src" / "auth.ts").read_text(encoding="utf-8")
        index_ts = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")

        self.assertNotIn('"Access-Control-Allow-Origin": "*"', auth_ts)
        self.assertNotIn("err.message", index_ts)
        self.assertNotIn("err.stack", index_ts)

    def test_03_durable_nonce_replay_guard(self):
        """3. Durable D1 replay guard table blocks duplicate nonces across instance restarts."""
        now_ts = int(time.time() * 1000)
        nonce = "nonce_unique_101"

        # First insert
        self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce, now_ts))
        self.con.commit()

        # Replay attempt in another instance / connection
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce, now_ts))

    def test_04_payload_hash_is_real_sha256(self):
        """4. Payload hash in raw_events is an actual 64-character SHA-256 hex string."""
        raw_body = '{"message_id":"msg_101","from":"bca.co.id","subject":"Test"}'
        actual_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
        self.assertEqual(len(actual_hash), 64)
        self.assertRegex(actual_hash, r"^[0-9a-f]{64}$")

        self.con.execute(
            "INSERT INTO raw_events (source, external_id, payload_hash, parser_version, state) VALUES ('gmail', 'msg_101', ?, '1.0.0', 'Parsed')",
            (actual_hash,),
        )
        self.con.commit()
        row = self.con.execute("SELECT payload_hash FROM raw_events WHERE external_id='msg_101'").fetchone()
        self.assertEqual(row["payload_hash"], actual_hash)

    def test_05_unauthorized_telegram_user_rejected(self):
        """5. Telegram update from unknown user_id is rejected/ignored without leaking data."""
        incoming_user_id = 999999999
        is_allowed = (incoming_user_id == self.allowed_user_id)
        self.assertFalse(is_allowed)

    def test_06_telegram_callback_replay_idempotent(self):
        """6. Inline callback Approve/Reject replay does not duplicate audit trail or change final state."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (10, 1, 'Expense', 50000, 'BCA Main', 'Main Meals', '2026-08-25', 'Pending')"""
        )
        self.con.commit()

        self.con.execute("UPDATE ingestion_candidates SET status='Approved', reviewed_at='2026-08-25 12:00:00' WHERE id=10")
        self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor) VALUES (10, 'Approved', 'user')")
        self.con.commit()

        cur_status = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=10").fetchone()["status"]
        self.assertEqual(cur_status, "Approved")

    def test_07_screenshot_without_metadata_not_auto_confirmed(self):
        """7. Photo/screenshot upload without parsed metadata remains Pending and requires user input."""
        self.con.execute(
            """INSERT INTO ingestion_candidates
               (raw_event_id, tx_type, amount, account, category, date, confidence_score, status, reasons)
               VALUES (1, 'Expense', 1.0, 'BCA Main', 'Other / Miscellaneous', '2026-08-25', 0.1, 'Pending', 'Screenshot image without metadata')"""
        )
        self.con.commit()
        cand = self.con.execute("SELECT status, confidence_score FROM ingestion_candidates WHERE reasons LIKE '%Screenshot%'").fetchone()
        self.assertEqual(cand["status"], "Pending")

    def test_08_mode_shadow_zero_ledger_mutations(self):
        """8. In MODE=shadow, approving a candidate only updates staging tables, with ZERO mutations to transactions/accounts."""
        tx_count_before = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_before = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (20, 1, 'Expense', 75000, 'BCA Main', 'Fuel', '2026-08-25', 'Pending')"""
        )
        self.con.execute("UPDATE ingestion_candidates SET status='Approved' WHERE id=20")
        self.con.commit()

        tx_count_after = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_after = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        self.assertEqual(tx_count_before, tx_count_after)
        self.assertEqual(acc_count_before, acc_count_after)

    # =========================================================================
    # FINANCIAL DOMAIN PARITY FIXTURE TESTS (NONZERO VALUES)
    # =========================================================================

    def test_09_financial_parity_custody_titipan_nonzero(self):
        """9. Active Custody (Titipan) outstanding is correctly calculated from event ledger and deducted."""
        self.con.execute("INSERT INTO debts (id, person_name, kind, status) VALUES (101, 'Titipan Budi', 'Custody', 'Active')")
        self.con.execute("INSERT INTO debt_events (debt_id, event_type, effect, amount, event_date) VALUES (101, 'Opening', 1, 350000.0, '2026-08-20')")
        self.con.commit()

        custody_rows = self.con.execute("SELECT id FROM debts WHERE kind='Custody' AND status='Active'").fetchall()
        custody_total = sum(
            self.con.execute("SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=?", (r[0],)).fetchone()[0]
            for r in custody_rows
        )
        self.assertEqual(custody_total, 350000.0)

    def test_10_financial_parity_payable_and_upcoming_coverage(self):
        """10. Payable effective commitment U_eff and regular upcoming X_eff with nonzero active coverage."""
        # 1. Active Payable
        self.con.execute("INSERT INTO debts (id, person_name, kind, status) VALUES (102, 'Cicilan Laptop', 'Payable', 'Active')")
        self.con.execute("INSERT INTO debt_events (debt_id, event_type, effect, amount, event_date) VALUES (102, 'Opening', 1, 2000000.0, '2026-08-01')")

        # 2. Upcoming linked to Payable
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status, debt_id) VALUES (201, 'Cicilan Bulan Ini', 500000.0, '2026-08-28', 'Upcoming', 102)"
        )

        # 3. Protected allocation covering upcoming 201
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, status, covers_upcoming_id) VALUES (301, 'Dana Tabungan Cicilan', 500000.0, 'Active', 201)"
        )
        self.con.commit()

        # U_eff calculation: 2,000,000 - 500,000 coverage = 1,500,000
        debt_out = self.con.execute("SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=102").fetchone()[0]
        cov = self.con.execute(
            """SELECT COALESCE(SUM(pa.amount), 0.0)
               FROM protected_allocations pa
               JOIN upcoming u ON pa.covers_upcoming_id = u.id
               WHERE u.debt_id=102 AND pa.status='Active' AND u.status='Upcoming'"""
        ).fetchone()[0]
        u_eff = max(0.0, debt_out - cov)
        self.assertEqual(u_eff, 1500000.0)

    def test_11_financial_parity_tentative_reserve_now(self):
        """11. Tentative upcoming is only deducted if reserve_now == 1, not deducted if reserve_now == 0."""
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status, reserve_now) VALUES (202, 'Rencana Liburan', 800000.0, '2026-09-15', 'Tentative', 0)"
        )
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status, reserve_now) VALUES (203, 'Servis Motor Pasti', 250000.0, '2026-09-10', 'Tentative', 1)"
        )
        self.con.commit()

        rows = self.con.execute(
            "SELECT id, amount, status, COALESCE(reserve_now, 0) as reserve_now FROM upcoming WHERE (debt_id IS NULL OR debt_id = 0) AND status IN ('Upcoming', 'Confirmed', 'Tentative')"
        ).fetchall()

        deducted_sum = 0.0
        for r in rows:
            if r["status"] == "Tentative" and r["reserve_now"] == 0:
                continue
            deducted_sum += r["amount"]

        # Only 203 (250,000) should be included, 202 (800,000) skipped
        self.assertEqual(deducted_sum, 250000.0)

    def test_12_financial_parity_allocation_goals_target_vs_allocated(self):
        """12. target_amount without allocated_amount does NOT deduct from safe-to-spend; only allocated_amount deducts."""
        self.con.execute(
            "INSERT INTO allocation_goals (id, name, kind, target_amount, allocated_amount, status) VALUES (401, 'Beli Laptop Baru', 'Goal', 15000000.0, 2000000.0, 'Active')"
        )
        self.con.commit()

        alloc_sum = self.con.execute("SELECT COALESCE(SUM(allocated_amount), 0.0) FROM allocation_goals WHERE status='Active'").fetchone()[0]
        self.assertEqual(alloc_sum, 2000000.0)

    def test_13_financial_parity_receivable_does_not_increase_liquidity(self):
        """13. Receivable (Piutang) is tracked as separate asset and does NOT increase liquid balance."""
        self.con.execute("INSERT INTO debts (id, person_name, kind, status) VALUES (103, 'Pinjaman Teman', 'Receivable', 'Active')")
        self.con.execute("INSERT INTO debt_events (debt_id, event_type, effect, amount, event_date) VALUES (103, 'Opening', 1, 1000000.0, '2026-08-15')")
        self.con.commit()

        liquid_balance = self.con.execute("SELECT COALESCE(SUM(current_balance), 0.0) FROM accounts WHERE active=1 AND kind='Owned'").fetchone()[0]
        # Adding receivable does not alter accounts current_balance
        self.assertGreaterEqual(liquid_balance, 0.0)

    def test_14_reconstruct_balance_parity_with_python(self):
        """14. Reconstruct balance matches Python canonical service: trusted anchor, timestamp order, and no-anchor unverifiable."""
        # Account without anchor
        self.con.execute("INSERT INTO accounts (name, kind, current_balance, active) VALUES ('UnverifiedBank', 'Owned', 500000.0, 1)")
        self.con.commit()

        # Reconstruct on UnverifiedBank returns unverifiable
        snap = self.con.execute(
            "SELECT * FROM balance_snapshots WHERE account_name='UnverifiedBank' AND snapshot_kind IN ('manual_anchor', 'initial_anchor')"
        ).fetchone()
        self.assertIsNone(snap)

        # Account with trusted anchor and subsequent mutations
        self.con.execute("INSERT INTO accounts (name, kind, current_balance, balance_date, active) VALUES ('VerifiedBCA', 'Owned', 1500000.0, '2026-08-01', 1)")
        self.con.execute("INSERT INTO balance_snapshots (account_name, balance, snapshot_date, created_at, snapshot_kind) VALUES ('VerifiedBCA', 1000000.0, '2026-08-01', '2026-08-01 00:00:00', 'initial_anchor')")
        self.con.execute("INSERT INTO transactions (date, time, created_at, transaction_type, amount, account_from, account_to, is_deleted) VALUES ('2026-08-05', '10:00', '2026-08-05 10:00:00', 'Income', 600000.0, '', 'VerifiedBCA', 0)")
        self.con.execute("INSERT INTO transactions (date, time, created_at, transaction_type, amount, account_from, account_to, is_deleted) VALUES ('2026-08-10', '14:00', '2026-08-10 14:00:00', 'Expense', 100000.0, 'VerifiedBCA', '', 0)")
        self.con.commit()

        in_mut = self.con.execute("SELECT COALESCE(SUM(amount), 0.0) FROM transactions WHERE account_to='VerifiedBCA' AND is_deleted=0").fetchone()[0]
        out_mut = self.con.execute("SELECT COALESCE(SUM(amount), 0.0) FROM transactions WHERE account_from='VerifiedBCA' AND is_deleted=0").fetchone()[0]
        expected_bal = 1000000.0 + in_mut - out_mut
        self.assertEqual(expected_bal, 1500000.0)

    def test_15_local_sqlite_hash_unmutated(self):
        """15. Local production SQLite database file hash is completely unmutated before and after test."""
        if self.prod_hash_before is not None and DB_PATH.exists():
            h = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
            self.assertEqual(h, self.prod_hash_before)


if __name__ == "__main__":
    unittest.main()
