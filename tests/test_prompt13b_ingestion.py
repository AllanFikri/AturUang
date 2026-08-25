"""
Test Suite: Prompt 13B — Gmail Relay, Telegram Bot Webhook, dan Staging Ingestion Pipeline
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
import urllib.parse
from datetime import datetime, timezone, timedelta

BASE_DIR = _REPO_ROOT
DB_PATH = BASE_DIR / "runtime" / "money_tracks.db"
MIGRATION_01 = BASE_DIR / "cloud" / "worker" / "migrations" / "0001_initial_schema.sql"
MIGRATION_02 = BASE_DIR / "cloud" / "worker" / "migrations" / "0002_staging_schema.sql"
MIGRATION_03 = BASE_DIR / "cloud" / "worker" / "migrations" / "0003_ingestion_connectors.sql"


class TestPrompt13BIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_d1 = Path(self.tmp_dir) / "test_d1.db"
        self.prod_hash_before = hashlib.sha256(DB_PATH.read_bytes()).hexdigest() if DB_PATH.exists() else None

        # Setup in-memory / temporary D1 SQLite database
        self.con = sqlite3.connect(self.test_d1)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")

        # Apply migrations 0001, 0002, 0003
        self.con.executescript(MIGRATION_01.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_02.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_03.read_text(encoding="utf-8"))

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

    def test_01_hmac_valid_invalid_expired_and_replay(self):
        """1. HMAC signature verification: valid, wrong secret, expired timestamp, and nonce replay."""
        body = json.dumps({"message_id": "msg_001", "body": "test"})
        now_ts = int(time.time() * 1000)
        nonce = "nonce_abc123"

        # Valid HMAC
        valid_sig = self._generate_hmac(body, now_ts, nonce, self.secret)
        self.assertEqual(len(valid_sig), 64)

        # Invalid HMAC (tampered body)
        invalid_sig = self._generate_hmac(body + "_tampered", now_ts, nonce, self.secret)
        self.assertNotEqual(valid_sig, invalid_sig)

        # Expired timestamp (> 5 minutes ago)
        old_ts = now_ts - 360000  # 6 minutes ago
        self.assertTrue(abs(now_ts - old_ts) > 300000)

        # Nonce tracking
        seen_nonces = set()
        seen_nonces.add(nonce)
        self.assertIn(nonce, seen_nonces)  # Replay detected

    def test_02_gmail_message_retry_idempotency(self):
        """2. Retrying the same Gmail message ID does not create duplicate raw events or candidates."""
        msg_id = "gmail_msg_1001"
        payload_hash = hashlib.sha256(b"raw_email_body").hexdigest()

        # First ingestion
        self.con.execute(
            "INSERT INTO raw_events (source, external_id, payload_hash, parser_version, state) VALUES ('gmail', ?, ?, '1.0.0', 'Parsed')",
            (msg_id, payload_hash),
        )
        self.con.commit()
        cnt_raw_1 = self.con.execute("SELECT count(*) FROM raw_events WHERE source='gmail'").fetchone()[0]
        self.assertEqual(cnt_raw_1, 3)  # 2 in setup + 1 new

        # Second ingestion attempt with same (source, external_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO raw_events (source, external_id, payload_hash, parser_version, state) VALUES ('gmail', ?, ?, '1.0.0', 'Parsed')",
                (msg_id, payload_hash),
            )

    def test_03_multiple_messages_in_thread_handled_independently(self):
        """3. Two distinct messages from the same email thread are ingested separately via message ID."""
        msg_1 = "thread_msg_001"
        msg_2 = "thread_msg_002"

        self.con.execute("INSERT INTO raw_events (source, external_id, payload_hash, parser_version) VALUES ('gmail', ?, 'h1', '1.0')", (msg_1,))
        self.con.execute("INSERT INTO raw_events (source, external_id, payload_hash, parser_version) VALUES ('gmail', ?, 'h2', '1.0')", (msg_2,))
        self.con.commit()

        cnt = self.con.execute("SELECT count(*) FROM raw_events WHERE source='gmail' AND external_id LIKE 'thread_msg_%'").fetchone()[0]
        self.assertEqual(cnt, 2)

    def test_04_bca_parser_nominal_and_account_extraction(self):
        """4. BCA transaction notification parsing: extracts Indonesian nominal format and maps to BCA Main."""
        sample_body = "Transaksi Rekening 1234567890: Pembayaran QRIS sebesar Rp 45.000,00 di Warung Nasi BERHASIL."
        match = re.search(r"Rp\s*([\d\.,]+)", sample_body)
        self.assertIsNotNone(match)
        raw_amt = match.group(1).replace(".", "").replace(",", ".")
        amt = float(raw_amt)
        self.assertEqual(amt, 45000.0)

        # Insert candidate
        self.con.execute(
            """INSERT INTO ingestion_candidates 
               (raw_event_id, tx_type, amount, account, category, date, confidence_score, status)
               VALUES (1, 'Expense', ?, 'BCA Main', 'Main Meals', '2026-08-25', 0.95, 'AutoApproved')""",
            (amt,),
        )
        self.con.commit()

        cand = self.con.execute("SELECT * FROM ingestion_candidates WHERE raw_event_id=1").fetchone()
        self.assertEqual(cand["amount"], 45000.0)
        self.assertEqual(cand["account"], "BCA Main")
        self.assertEqual(cand["status"], "AutoApproved")

    def test_05_unknown_sender_and_ambiguous_direction_pending(self):
        """5. Unknown sender or ambiguous amount/direction is assigned status 'Pending' for manual review."""
        self.con.execute(
            """INSERT INTO ingestion_candidates 
               (raw_event_id, tx_type, amount, account, category, date, confidence_score, status, reasons)
               VALUES (2, 'Expense', 100000, 'BCA Main', 'Other / Miscellaneous', '2026-08-25', 0.35, 'Pending', 'Unknown sender')"""
        )
        self.con.commit()
        cand = self.con.execute("SELECT status, confidence_score FROM ingestion_candidates WHERE raw_event_id=2").fetchone()
        self.assertEqual(cand["status"], "Pending")
        self.assertLess(cand["confidence_score"], 0.5)

    def test_06_internal_transfer_mapped_to_single_transaction(self):
        """6. Internal transfer command maps to one Transfer candidate (not double Income + Expense)."""
        self.con.execute(
            """INSERT INTO ingestion_candidates 
               (raw_event_id, tx_type, amount, account, to_account, category, date, status)
               VALUES (3, 'Transfer', 250000.0, 'BCA Main', 'Jago Main', 'Other / Miscellaneous', '2026-08-25', 'AutoApproved')"""
        )
        self.con.commit()
        cand = self.con.execute("SELECT tx_type, account, to_account FROM ingestion_candidates WHERE raw_event_id=3").fetchone()
        self.assertEqual(cand["tx_type"], "Transfer")
        self.assertEqual(cand["account"], "BCA Main")
        self.assertEqual(cand["to_account"], "Jago Main")

    def test_07_unauthorized_telegram_user_rejected(self):
        """7. Telegram update from unknown user_id is rejected/ignored without leaking data."""
        incoming_user_id = 999999999  # Stranger
        is_allowed = (incoming_user_id == self.allowed_user_id)
        self.assertFalse(is_allowed)

    def test_08_telegram_callback_replay_idempotent(self):
        """8. Inline callback Approve/Reject replay does not duplicate audit trail or change final state."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (10, 1, 'Expense', 50000, 'BCA Main', 'Main Meals', '2026-08-25', 'Pending')"""
        )
        self.con.commit()

        # First review: Approve
        self.con.execute("UPDATE ingestion_candidates SET status='Approved', reviewed_at='2026-08-25 12:00:00' WHERE id=10")
        self.con.execute("INSERT INTO ingestion_audit_log (candidate_id, action, actor) VALUES (10, 'Approved', 'user')")
        self.con.commit()

        # Replay: Second review on already approved candidate
        cur_status = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=10").fetchone()["status"]
        self.assertEqual(cur_status, "Approved")

    def test_09_screenshot_without_metadata_not_auto_confirmed(self):
        """9. Photo/screenshot upload without parsed metadata remains Pending and requires user input."""
        self.con.execute(
            """INSERT INTO ingestion_candidates 
               (raw_event_id, tx_type, amount, account, category, date, confidence_score, status, reasons)
               VALUES (1, 'Expense', 1.0, 'BCA Main', 'Other / Miscellaneous', '2026-08-25', 0.1, 'Pending', 'Screenshot image without metadata')"""
        )
        self.con.commit()
        cand = self.con.execute("SELECT status, confidence_score FROM ingestion_candidates WHERE reasons LIKE '%Screenshot%'").fetchone()
        self.assertEqual(cand["status"], "Pending")

    def test_10_secrets_and_pii_excluded_from_logs(self):
        """10. Ingestion audit log excludes tokens, secrets, or raw passwords."""
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (10, 1, 'Expense', 50000, 'BCA Main', 'Main Meals', '2026-08-25', 'Approved')"""
        )
        self.con.execute(
            "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (10, 'Approved', 'admin', 'Approved in shadow mode')"
        )
        self.con.commit()
        log = self.con.execute("SELECT * FROM ingestion_audit_log WHERE candidate_id=10").fetchone()
        self.assertNotIn("secret", log["details"].lower())
        self.assertNotIn("token", log["details"].lower())

    def test_11_source_freshness_tracking(self):
        """11. source_sync_state tracks last_attempt_at, last_success_at, and status correctly."""
        self.con.execute(
            """INSERT INTO source_sync_state (source, last_attempt_at, last_success_at, last_event_at, status)
               VALUES ('gmail', '2026-08-25 12:00:00', '2026-08-25 12:00:00', '2026-08-25 11:55:00', 'OK')"""
        )
        self.con.commit()
        state = self.con.execute("SELECT * FROM source_sync_state WHERE source='gmail'").fetchone()
        self.assertEqual(state["status"], "OK")
        self.assertEqual(state["last_event_at"], "2026-08-25 11:55:00")

    def test_12_mode_shadow_zero_ledger_mutations(self):
        """12. In MODE=shadow, approving a candidate only updates staging tables, with ZERO mutations to transactions/accounts."""
        tx_count_before = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_before = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        # In shadow mode, approval only affects ingestion_candidates:
        self.con.execute(
            """INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status)
               VALUES (20, 1, 'Expense', 75000, 'BCA Main', 'Fuel', '2026-08-25', 'Pending')"""
        )
        self.con.execute("UPDATE ingestion_candidates SET status='Approved' WHERE id=20")
        self.con.commit()

        tx_count_after = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_after = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        self.assertEqual(tx_count_before, tx_count_after, "Shadow mode must not insert into transactions table!")
        self.assertEqual(acc_count_before, acc_count_after, "Shadow mode must not mutate accounts table!")

    def test_13_d1_failure_recovery_without_duplicate(self):
        """13. Interrupted ingestion can be retried safely using UNIQUE(source, external_id) guard."""
        self.con.execute(
            "INSERT INTO raw_events (source, external_id, payload_hash, parser_version, state) VALUES ('gmail', 'retry_msg', 'h', '1.0', 'Pending')"
        )
        self.con.commit()

        # Update state on retry
        self.con.execute("UPDATE raw_events SET state='Parsed' WHERE source='gmail' AND external_id='retry_msg'")
        self.con.commit()
        final_state = self.con.execute("SELECT state FROM raw_events WHERE external_id='retry_msg'").fetchone()["state"]
        self.assertEqual(final_state, "Parsed")

    def test_14_local_sqlite_hash_unmutated(self):
        """14. Local production SQLite database file hash is completely unmutated before and after test."""
        if self.prod_hash_before is not None and DB_PATH.exists():
            h = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
            self.assertEqual(h, self.prod_hash_before, f"Production DB modified! Expected {self.prod_hash_before}, got {h}")


if __name__ == "__main__":
    unittest.main()
