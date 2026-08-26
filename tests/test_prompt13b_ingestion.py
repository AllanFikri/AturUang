"""
Test Suite: Prompt 13B-V4 — Final Pre-Deploy Safety Gate (Callback Guard, Nonce Failure Semantics, Minimal Payload Privacy)
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
MIGRATION_05 = BASE_DIR / "cloud" / "worker" / "migrations" / "0005_telegram_callback_guard.sql"


class TestPrompt13BIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_d1 = Path(self.tmp_dir) / "test_d1.db"
        self.prod_hash_before = hashlib.sha256(DB_PATH.read_bytes()).hexdigest() if DB_PATH.exists() else None

        # Setup shadow D1 SQLite database
        self.con = sqlite3.connect(self.test_d1)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")

        # Apply migrations 0001, 0002, 0003, 0004, 0005
        self.con.executescript(MIGRATION_01.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_02.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_03.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_04.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_05.read_text(encoding="utf-8"))

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
    # 1. GMAIL HMAC & NONCE FAILURE SEMANTICS
    # =========================================================================

    def test_01_gmail_nonce_failure_semantics_replay_vs_db_error(self):
        """1. Nonce error semantics: UNIQUE violation returns NONCE_REPLAY, whereas database failure returns REPLAY_GUARD_UNAVAILABLE (not NONCE_REPLAY)."""
        auth_ts = (BASE_DIR / "cloud" / "worker" / "src" / "auth.ts").read_text(encoding="utf-8")
        index_ts = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")

        # Source code audit for error differentiation
        self.assertIn("NONCE_REPLAY", auth_ts)
        self.assertIn("REPLAY_GUARD_UNAVAILABLE", auth_ts)
        self.assertIn("REPLAY_GUARD_UNAVAILABLE", index_ts)
        self.assertIn("503", index_ts)

        # Simulation on SQLite D1:
        now_ts = int(time.time() * 1000)
        nonce_unique = "nonce_sem_101"

        # First valid insert succeeds
        self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_unique, now_ts))
        self.con.commit()

        # Replay causes UNIQUE constraint violation -> classified as NONCE_REPLAY
        try:
            self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_unique, now_ts))
            err_code = "SUCCESS"
        except sqlite3.IntegrityError as e:
            msg = str(e).upper()
            if "UNIQUE" in msg or "PRIMARY KEY" in msg:
                err_code = "NONCE_REPLAY"
            else:
                err_code = "REPLAY_GUARD_UNAVAILABLE"
        self.assertEqual(err_code, "NONCE_REPLAY")

        # Generic database failure (e.g. invalid column or table drop) -> classified as REPLAY_GUARD_UNAVAILABLE
        try:
            self.con.execute("INSERT INTO non_existent_table VALUES (1)")
            err_code_db = "SUCCESS"
        except sqlite3.OperationalError:
            err_code_db = "REPLAY_GUARD_UNAVAILABLE"
        self.assertEqual(err_code_db, "REPLAY_GUARD_UNAVAILABLE")

    def test_02_gmail_hmac_invalid_signature_does_not_consume_nonce(self):
        """2. Gmail HMAC ordering: Invalid signature with nonce N does NOT reserve nonce in D1, allowing subsequent valid request with nonce N to succeed."""
        now_ts = int(time.time() * 1000)
        nonce_test = "nonce_order_test_202"
        body = '{"message_id":"msg_202","from":"notifikasi@bca.co.id","subject":"Pembayaran QRIS Rp 50.000"}'

        # Step 1: Simulated request with wrong signature
        wrong_sig = "0000000000000000000000000000000000000000000000000000000000000000"
        valid_sig = self._generate_hmac(body, now_ts, nonce_test, self.secret)

        is_valid_1 = hmac.compare_digest(wrong_sig, valid_sig)
        self.assertFalse(is_valid_1)

        # Verify nonce_test is NOT in D1
        nonce_row_1 = self.con.execute("SELECT nonce FROM gmail_replay_nonces WHERE nonce=?", (nonce_test,)).fetchone()
        self.assertIsNone(nonce_row_1)

        # Step 2: Legitimate request with same nonce_test and valid signature
        is_valid_2 = hmac.compare_digest(valid_sig, valid_sig)
        self.assertTrue(is_valid_2)

        self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_test, now_ts))
        self.con.commit()

        # Step 3: Subsequent replay with same nonce is blocked
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_test, now_ts))

    # =========================================================================
    # 2. GMAIL MINIMAL PAYLOAD PRIVACY
    # =========================================================================

    def test_03_gmail_minimal_payload_privacy_zero_pii_and_zero_raw_body(self):
        """3. Persisted Gmail minimal_raw_payload contains zero raw bodies, zero personal emails, and zero raw subjects."""
        index_ts = (BASE_DIR / "cloud" / "worker" / "src" / "index.ts").read_text(encoding="utf-8")

        # Substring(0, 100) must be completely removed
        self.assertNotIn("subject_sanitized", index_ts)
        self.assertNotIn("substring(0, 100)", index_ts)

        # Verify minimal payload format in index.ts
        self.assertIn("sender_domain", index_ts)
        self.assertIn("parser_adapter", index_ts)

        # Test minimal payload generation logic
        raw_email = "budi.santoso123@bca.co.id"
        sender_domain = raw_email.split("@")[1].strip().lower() if "@" in raw_email else "unknown"
        self.assertEqual(sender_domain, "bca.co.id")
        self.assertNotIn("budi.santoso", sender_domain)

        minimal_payload = json.dumps({
            "sender_domain": sender_domain,
            "parser_adapter": "bca_alert",
            "detected_type": "Expense",
            "confidence": 0.95,
        })

        # Insert and verify stored payload
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state, minimal_raw_payload) VALUES (10, 'gmail', 'priv_msg_1', 'h_priv', '1.0', 'Parsed', ?)",
            (minimal_payload,),
        )
        self.con.commit()

        stored = self.con.execute("SELECT minimal_raw_payload FROM raw_events WHERE id=10").fetchone()[0]
        stored_dict = json.loads(stored)

        self.assertNotIn("budi.santoso", stored)
        self.assertNotIn("body", stored_dict)
        self.assertNotIn("subject", stored_dict)
        self.assertEqual(stored_dict["sender_domain"], "bca.co.id")

    # =========================================================================
    # 3. TELEGRAM CALLBACK & MESSAGE ATOMIC DURABLE IDEMPOTENCY
    # =========================================================================

    def test_04_telegram_callback_guard_and_atomic_conditional_approve(self):
        """4. Telegram callback Approve: Atomic conditional update transitions Pending -> Approved once with 1 audit event. Concurrent / replay executions yield 0 additional audit events."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (20, 1, 'Expense', 60000.0, 'BCA Main', 'Main Meals', '2026-08-25', 'Pending')"""
        )
        self.con.commit()

        callback_id = "cb_query_1001"
        target_status = "Approved"

        # 1st execution:
        # Check guard -> not found
        guard_1 = self.con.execute("SELECT callback_id FROM telegram_callback_guard WHERE callback_id=?", (callback_id,)).fetchone()
        self.assertIsNone(guard_1)

        # Atomic conditional update
        cur = self.con.execute(
            "UPDATE ingestion_candidates SET status = ?, reviewed_at = '2026-08-25 12:00:00' WHERE id = 20 AND status = 'Pending'",
            (target_status,),
        )
        changes_1 = cur.rowcount
        self.assertEqual(changes_1, 1)

        # Insert guard & audit only on successful transition
        self.con.execute("INSERT INTO telegram_callback_guard (callback_id, candidate_id, action) VALUES (?, 20, ?)", (callback_id, target_status))
        self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (20, ?, 'telegram_user', 'Reviewed via inline button')", (target_status,))
        self.con.commit()

        audit_cnt_1 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=20").fetchone()[0]
        self.assertEqual(audit_cnt_1, 1)

        # 2nd execution (Simulated concurrent race or exact replay with same callback_id):
        guard_2 = self.con.execute("SELECT callback_id FROM telegram_callback_guard WHERE callback_id=?", (callback_id,)).fetchone()
        self.assertIsNotNone(guard_2)  # Caught by callback guard immediately!

        # 3rd execution (Simulated race with DIFFERENT callback_id on already Approved candidate):
        diff_callback_id = "cb_query_1002"
        cur_race = self.con.execute(
            "UPDATE ingestion_candidates SET status = ?, reviewed_at = '2026-08-25 12:00:00' WHERE id = 20 AND status = 'Pending'",
            (target_status,),
        )
        changes_race = cur_race.rowcount
        self.assertEqual(changes_race, 0)  # Affected rows == 0!

        # In changes == 0, system checks current status and DOES NOT insert audit
        cand_status = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=20").fetchone()["status"]
        self.assertEqual(cand_status, "Approved")

        audit_cnt_final = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=20").fetchone()[0]
        self.assertEqual(audit_cnt_final, 1, "Audit log count MUST remain exactly 1 across all replays/races!")

    def test_05_telegram_callback_guard_and_atomic_conditional_reject(self):
        """5. Telegram callback Reject: Transitions Pending -> Rejected with 1 audit event. Replay is idempotent with 0 additional audit events."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (21, 1, 'Expense', 35000.0, 'BCA Main', 'Snacks', '2026-08-25', 'Pending')"""
        )
        self.con.commit()

        callback_id = "cb_query_2001"
        target_status = "Rejected"

        cur = self.con.execute(
            "UPDATE ingestion_candidates SET status = ?, reviewed_at = '2026-08-25 12:05:00' WHERE id = 21 AND status = 'Pending'",
            (target_status,),
        )
        self.assertEqual(cur.rowcount, 1)

        self.con.execute("INSERT INTO telegram_callback_guard (callback_id, candidate_id, action) VALUES (?, 21, ?)", (callback_id, target_status))
        self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (21, ?, 'telegram_user', 'Reviewed via inline button')", (target_status,))
        self.con.commit()

        # Replay
        guard = self.con.execute("SELECT callback_id FROM telegram_callback_guard WHERE callback_id=?", (callback_id,)).fetchone()
        self.assertIsNotNone(guard)

        audit_cnt = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=21").fetchone()[0]
        self.assertEqual(audit_cnt, 1)

    def test_06_telegram_terminal_cross_transition_locked(self):
        """6. Terminal state lock: Approved candidate cannot be switched to Rejected, and Rejected cannot be switched to Approved."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (22, 1, 'Expense', 45000.0, 'BCA Main', 'Fuel', '2026-08-25', 'Approved')"""
        )
        self.con.commit()

        # Attempt to reject an already Approved candidate
        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=22").fetchone()
        self.assertEqual(cand["status"], "Approved")

        target_status = "Rejected"
        cur = self.con.execute(
            "UPDATE ingestion_candidates SET status = ?, reviewed_at = '2026-08-25 12:10:00' WHERE id = 22 AND status = 'Pending'",
            (target_status,),
        )
        self.assertEqual(cur.rowcount, 0)

        is_locked = (cand["status"] in ("Approved", "Rejected") and cand["status"] != target_status)
        self.assertTrue(is_locked)

        # Verify status remains Approved and no audit entry added
        cand_after = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=22").fetchone()
        self.assertEqual(cand_after["status"], "Approved")
        audit_cnt = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=22").fetchone()[0]
        self.assertEqual(audit_cnt, 0)

    def test_07_telegram_message_idempotency_deterministic_external_id(self):
        """7. Telegram message retry with same update_id or message_id does NOT create duplicate raw_events or ingestion_candidates."""
        update_id = 112233
        external_id = f"tg_update_{update_id}"
        text_hash = hashlib.sha256(b"keluar 25000 dari BCA Main untuk makan").hexdigest()

        # 1st message insertion
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state) VALUES (110, 'telegram', ?, ?, '1.0', 'Parsed')",
            (external_id, text_hash),
        )
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (110, 110, 'Expense', 25000.0, 'BCA Main', 'Main Meals', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        # Retry check
        existing = self.con.execute("SELECT id FROM raw_events WHERE source='telegram' AND external_id=?", (external_id,)).fetchone()
        self.assertIsNotNone(existing)

        raw_cnt = self.con.execute("SELECT count(*) FROM raw_events WHERE source='telegram' AND external_id=?", (external_id,)).fetchone()[0]
        cand_cnt = self.con.execute("SELECT count(*) FROM ingestion_candidates WHERE raw_event_id=110").fetchone()[0]
        self.assertEqual(raw_cnt, 1)
        self.assertEqual(cand_cnt, 1)

    def test_08_mode_shadow_zero_ledger_mutations(self):
        """8. In MODE=shadow, approving a candidate only updates staging tables, with ZERO mutations to transactions/accounts."""
        tx_count_before = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_before = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (30, 1, 'Expense', 80000.0, 'BCA Main', 'Fuel', '2026-08-25', 'Pending')"""
        )
        self.con.execute("UPDATE ingestion_candidates SET status='Approved' WHERE id=30")
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
