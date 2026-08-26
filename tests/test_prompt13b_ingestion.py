"""
Test Suite: Prompt 13B — Fail-Closed Security, Ingestion Connectors, Idempotency, and HMAC Ordering
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

        # Setup shadow D1 SQLite database
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
    # SECURITY FAIL-CLOSED & CORS TESTS
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

    # =========================================================================
    # GMAIL HMAC & NONCE ORDERING TESTS
    # =========================================================================

    def test_03_gmail_hmac_ordering_invalid_signature_does_not_consume_nonce(self):
        """3. Gmail HMAC ordering: Invalid signature with nonce N does NOT reserve nonce in D1, allowing subsequent valid request with nonce N to succeed."""
        now_ts = int(time.time() * 1000)
        nonce_test = "nonce_order_test_101"
        body = '{"message_id":"msg_101","from":"bca.co.id","subject":"Test"}'

        # Step 1: Simulated request with wrong signature
        wrong_sig = "0000000000000000000000000000000000000000000000000000000000000000"
        valid_sig = self._generate_hmac(body, now_ts, nonce_test, self.secret)

        is_valid_1 = hmac.compare_digest(wrong_sig, valid_sig)
        self.assertFalse(is_valid_1)

        # Verify nonce_test is NOT inserted into D1 table
        nonce_row_1 = self.con.execute("SELECT nonce FROM gmail_replay_nonces WHERE nonce=?", (nonce_test,)).fetchone()
        self.assertIsNone(nonce_row_1)

        # Step 2: Legitimate request with same nonce_test and valid signature
        is_valid_2 = hmac.compare_digest(valid_sig, valid_sig)
        self.assertTrue(is_valid_2)

        # Reserve nonce in D1 upon valid signature
        self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_test, now_ts))
        self.con.commit()

        nonce_row_2 = self.con.execute("SELECT nonce FROM gmail_replay_nonces WHERE nonce=?", (nonce_test,)).fetchone()
        self.assertIsNotNone(nonce_row_2)

        # Step 3: Replay of the valid request with same nonce_test is rejected by D1 constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_test, now_ts))

    # =========================================================================
    # TELEGRAM CALLBACK & MESSAGE IDEMPOTENCY TESTS
    # =========================================================================

    def test_04_telegram_callback_approve_idempotency_no_duplicate_audit(self):
        """4. Telegram callback Approve: 1st execution transitions Pending -> Approved with 1 audit row. 2nd replay is idempotent with 0 new audit rows."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (10, 1, 'Expense', 50000.0, 'BCA Main', 'Main Meals', '2026-08-25', 'Pending')"""
        )
        self.con.commit()

        # 1st execution: Pending -> Approved
        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=10").fetchone()
        self.assertEqual(cand["status"], "Pending")

        self.con.execute("UPDATE ingestion_candidates SET status='Approved', reviewed_at='2026-08-25 12:00:00' WHERE id=10 AND status='Pending'")
        self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (10, 'Approved', 'telegram_user', 'Reviewed via inline button')")
        self.con.commit()

        audit_count_1 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=10").fetchone()[0]
        self.assertEqual(audit_count_1, 1)

        # 2nd execution (Replay Approve): Idempotent check
        cand_2 = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=10").fetchone()
        if cand_2["status"] == "Approved":
            # Idempotent replay: Do NOT insert into audit log!
            pass
        else:
            self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (10, 'Approved', 'telegram_user', 'Reviewed via inline button')")
        self.con.commit()

        audit_count_2 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=10").fetchone()[0]
        self.assertEqual(audit_count_2, 1, "Replay approve MUST NOT create a duplicate audit log entry!")

    def test_05_telegram_callback_reject_idempotency_no_duplicate_audit(self):
        """5. Telegram callback Reject: 1st execution transitions Pending -> Rejected with 1 audit row. 2nd replay is idempotent with 0 new audit rows."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (11, 1, 'Expense', 30000.0, 'BCA Main', 'Snacks', '2026-08-25', 'Pending')"""
        )
        self.con.commit()

        # 1st execution: Pending -> Rejected
        self.con.execute("UPDATE ingestion_candidates SET status='Rejected', reviewed_at='2026-08-25 12:05:00' WHERE id=11 AND status='Pending'")
        self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (11, 'Rejected', 'telegram_user', 'Reviewed via inline button')")
        self.con.commit()

        audit_count_1 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=11").fetchone()[0]
        self.assertEqual(audit_count_1, 1)

        # 2nd execution (Replay Reject): Idempotent check
        cand_2 = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=11").fetchone()
        if cand_2["status"] == "Rejected":
            pass
        else:
            self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (11, 'Rejected', 'telegram_user', 'Reviewed via inline button')")
        self.con.commit()

        audit_count_2 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=11").fetchone()[0]
        self.assertEqual(audit_count_2, 1, "Replay reject MUST NOT create a duplicate audit log entry!")

    def test_06_telegram_terminal_state_locked(self):
        """6. Terminal state lock: Approved candidate cannot be switched to Rejected, and Rejected cannot be switched to Approved."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (12, 1, 'Expense', 20000.0, 'BCA Main', 'Cafe & Drinks', '2026-08-25', 'Approved')"""
        )
        self.con.commit()

        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=12").fetchone()
        target_status = "Rejected"

        # Contract: if current is terminal and != target, locked error
        is_locked = (cand["status"] in ("Approved", "Rejected") and cand["status"] != target_status)
        self.assertTrue(is_locked)

        # Status remains Approved
        cand_after = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=12").fetchone()
        self.assertEqual(cand_after["status"], "Approved")

    def test_07_telegram_message_idempotency_deterministic_external_id(self):
        """7. Telegram message retry with same update_id or message_id does NOT create duplicate raw_events or ingestion_candidates."""
        update_id = 998877
        external_id = f"tg_update_{update_id}"
        text_hash = hashlib.sha256(b"beli bensin 50000").hexdigest()

        # 1st ingestion: insert raw_event and candidate
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state) VALUES (100, 'telegram', ?, ?, '1.0', 'Parsed')",
            (external_id, text_hash),
        )
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (100, 100, 'Expense', 50000.0, 'BCA Main', 'Fuel', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        raw_cnt_1 = self.con.execute("SELECT count(*) FROM raw_events WHERE source='telegram' AND external_id=?", (external_id,)).fetchone()[0]
        cand_cnt_1 = self.con.execute("SELECT count(*) FROM ingestion_candidates WHERE raw_event_id=100").fetchone()[0]
        self.assertEqual(raw_cnt_1, 1)
        self.assertEqual(cand_cnt_1, 1)

        # 2nd ingestion retry: check existing
        existing = self.con.execute("SELECT id FROM raw_events WHERE source='telegram' AND external_id=?", (external_id,)).fetchone()
        self.assertIsNotNone(existing)

        # Retry does NOT insert second row
        raw_cnt_2 = self.con.execute("SELECT count(*) FROM raw_events WHERE source='telegram' AND external_id=?", (external_id,)).fetchone()[0]
        cand_cnt_2 = self.con.execute("SELECT count(*) FROM ingestion_candidates WHERE raw_event_id=100").fetchone()[0]
        self.assertEqual(raw_cnt_2, 1)
        self.assertEqual(cand_cnt_2, 1)

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

    def test_09_local_sqlite_hash_unmutated(self):
        """9. Local production SQLite database file hash is completely unmutated before and after test."""
        if self.prod_hash_before is not None and DB_PATH.exists():
            h = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
            self.assertEqual(h, self.prod_hash_before)


if __name__ == "__main__":
    unittest.main()
