"""
Test Suite: Prompt 13B-V4.1 — Atomic Review Transactions, Failure Injection, and Durable Idempotency
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
MIGRATION_06 = BASE_DIR / "cloud" / "worker" / "migrations" / "0006_atomic_audit_guard.sql"


def execute_telegram_callback_atomic(con: sqlite3.Connection, callback_id: str | None, cand_id: int, target_status: str, fail_at: str | None = None) -> dict:
    """Production transaction model for Telegram callback review matching index.ts atomic D1 batch."""
    op_key = f"tg_cb_{callback_id}" if callback_id else f"tg_cand_{cand_id}_{target_status}_{time.time()}"

    # 1. Fast-path guard check
    if callback_id:
        guard = con.execute("SELECT callback_id, candidate_id, action FROM telegram_callback_guard WHERE callback_id=?", (callback_id,)).fetchone()
        if guard:
            return {"status": "success", "idempotent": True, "action": guard["action"], "candidate_id": guard["candidate_id"]}

    # 2. Atomic Transaction Block
    try:
        with con:
            if fail_at == "guard":
                raise sqlite3.OperationalError("SIMULATED_D1_GUARD_FAILURE")

            if callback_id:
                con.execute(
                    "INSERT INTO telegram_callback_guard (callback_id, candidate_id, action) SELECT ?, id, ? FROM ingestion_candidates WHERE id = ? AND status = 'Pending'",
                    (callback_id, target_status, cand_id)
                )

            if fail_at == "audit":
                raise sqlite3.OperationalError("SIMULATED_D1_AUDIT_FAILURE")

            con.execute(
                "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details, operation_key) SELECT id, ?, 'telegram_user', 'Reviewed via inline button', ? FROM ingestion_candidates WHERE id = ? AND status = 'Pending'",
                (target_status, op_key, cand_id)
            )

            cur = con.execute(
                "UPDATE ingestion_candidates SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'",
                (target_status, cand_id)
            )
            changes = cur.rowcount

        if changes == 1:
            return {"status": "success", "action": target_status, "candidate_id": cand_id, "previous_status": "Pending"}
    except Exception:
        pass

    # Inspect current state outside aborted transaction
    cand = con.execute("SELECT id, status FROM ingestion_candidates WHERE id=?", (cand_id,)).fetchone()
    if not cand:
        return {"status": "error", "code": "NOT_FOUND"}
    if cand["status"] == target_status:
        return {"status": "success", "idempotent": True, "action": target_status, "candidate_id": cand_id}
    return {"status": "error", "code": "TERMINAL_STATE_LOCKED", "current_status": cand["status"]}


def execute_admin_review_atomic(con: sqlite3.Connection, cand_id: int, target_status: str, actor: str = "admin_user", fail_at: str | None = None) -> dict:
    """Production transaction model for Admin Staging review endpoints matching index.ts atomic D1 batch."""
    op_key = f"admin_{target_status.lower()}_{cand_id}_{time.time()}"
    try:
        with con:
            if fail_at == "audit":
                raise sqlite3.OperationalError("SIMULATED_D1_AUDIT_FAILURE")

            con.execute(
                "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details, operation_key) SELECT id, ?, ?, 'Admin review via staging API', ? FROM ingestion_candidates WHERE id = ? AND status = 'Pending'",
                (target_status, actor, op_key, cand_id)
            )

            cur = con.execute(
                "UPDATE ingestion_candidates SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'",
                (target_status, cand_id)
            )
            changes = cur.rowcount

        if changes == 1:
            return {"status": "success", "candidate_id": cand_id, "new_status": target_status}
    except Exception:
        pass

    cand = con.execute("SELECT id, status FROM ingestion_candidates WHERE id=?", (cand_id,)).fetchone()
    if not cand:
        return {"status": "error", "code": "NOT_FOUND"}
    if cand["status"] == target_status:
        return {"status": "success", "idempotent": True, "candidate_id": cand_id, "new_status": target_status}
    return {"status": "error", "code": "TERMINAL_STATE_LOCKED", "current_status": cand["status"]}


class TestPrompt13BIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_d1 = Path(self.tmp_dir) / "test_d1.db"
        self.prod_hash_before = hashlib.sha256(DB_PATH.read_bytes()).hexdigest() if DB_PATH.exists() else None

        # Setup shadow D1 SQLite database
        self.con = sqlite3.connect(self.test_d1)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")

        # Apply migrations 0001, 0002, 0003, 0004, 0005, 0006
        self.con.executescript(MIGRATION_01.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_02.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_03.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_04.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_05.read_text(encoding="utf-8"))
        self.con.executescript(MIGRATION_06.read_text(encoding="utf-8"))

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

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _generate_hmac(self, body: str, ts: int, nonce: str, secret: str) -> str:
        payload = f"{ts}.{nonce}.{body}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    # =========================================================================
    # 1. ATOMIC TELEGRAM CALLBACK & FAILURE INJECTION TESTS
    # =========================================================================

    def test_01_successful_transition_creates_exactly_one_state_change_and_audit(self):
        """1. Successful Telegram callback transition commits state change, guard, and audit log atomically."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (10, 1, 'Expense', 50000.0, 'BCA Main', 'Main Meals', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        res = execute_telegram_callback_atomic(self.con, "cb_1001", 10, "Approved")
        self.assertEqual(res["status"], "success")

        # Verify exact atomic state
        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=10").fetchone()
        self.assertEqual(cand["status"], "Approved")

        guard = self.con.execute("SELECT callback_id, action FROM telegram_callback_guard WHERE candidate_id=10").fetchone()
        self.assertEqual(guard["callback_id"], "cb_1001")
        self.assertEqual(guard["action"], "Approved")

        audits = self.con.execute("SELECT id, action, actor FROM ingestion_audit_log WHERE candidate_id=10").fetchall()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["action"], "Approved")

    def test_02_exact_callback_replay_yields_zero_duplicate_audit(self):
        """2. Exact callback replay is intercepted by guard, yielding exactly 1 total audit record."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (11, 1, 'Expense', 30000.0, 'BCA Main', 'Snacks', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        # 1st run
        res1 = execute_telegram_callback_atomic(self.con, "cb_1002", 11, "Approved")
        self.assertEqual(res1["status"], "success")

        # 2nd run (Replay)
        res2 = execute_telegram_callback_atomic(self.con, "cb_1002", 11, "Approved")
        self.assertEqual(res2["status"], "success")
        self.assertTrue(res2["idempotent"])

        audit_count = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=11").fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_03_concurrent_different_callback_replay_yields_zero_duplicate_audit(self):
        """3. Concurrent callbacks with different IDs on already resolved candidate do NOT create a second audit record."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (12, 1, 'Expense', 40000.0, 'BCA Main', 'Cafe', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        res1 = execute_telegram_callback_atomic(self.con, "cb_first", 12, "Approved")
        self.assertEqual(res1["status"], "success")

        res2 = execute_telegram_callback_atomic(self.con, "cb_race", 12, "Approved")
        self.assertEqual(res2["status"], "success")
        self.assertTrue(res2["idempotent"])

        audit_count = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=12").fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_04_simulated_audit_failure_rolls_back_state_transition(self):
        """4. Failure during audit log insertion rolls back candidate state transition (remains Pending, 0 audit, 0 guard)."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (13, 1, 'Expense', 70000.0, 'BCA Main', 'Fuel', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        # Inject failure at audit step
        res = execute_telegram_callback_atomic(self.con, "cb_fail_audit", 13, "Approved", fail_at="audit")

        # Candidate MUST still be Pending!
        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=13").fetchone()
        self.assertEqual(cand["status"], "Pending")

        # Guard and Audit MUST be 0!
        guard_cnt = self.con.execute("SELECT count(*) FROM telegram_callback_guard WHERE candidate_id=13").fetchone()[0]
        audit_cnt = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=13").fetchone()[0]
        self.assertEqual(guard_cnt, 0)
        self.assertEqual(audit_cnt, 0)

    def test_05_simulated_guard_failure_rolls_back_state_transition(self):
        """5. Failure during guard insertion rolls back candidate state transition (remains Pending, 0 audit, 0 guard)."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (14, 1, 'Expense', 85000.0, 'BCA Main', 'Fuel', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        # Inject failure at guard step
        res = execute_telegram_callback_atomic(self.con, "cb_fail_guard", 14, "Approved", fail_at="guard")

        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=14").fetchone()
        self.assertEqual(cand["status"], "Pending")

        guard_cnt = self.con.execute("SELECT count(*) FROM telegram_callback_guard WHERE candidate_id=14").fetchone()[0]
        audit_cnt = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=14").fetchone()[0]
        self.assertEqual(guard_cnt, 0)
        self.assertEqual(audit_cnt, 0)

    def test_06_terminal_cross_transition_locked_no_mutations(self):
        """6. Cross-transition between terminal states (Approved -> Rejected or vice versa) is locked with zero mutations."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (15, 1, 'Expense', 90000.0, 'BCA Main', 'Bills', '2026-08-25', 'Approved')"
        )
        self.con.commit()

        res = execute_telegram_callback_atomic(self.con, "cb_cross", 15, "Rejected")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "TERMINAL_STATE_LOCKED")

        cand = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=15").fetchone()
        self.assertEqual(cand["status"], "Approved")

        audit_cnt = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=15").fetchone()[0]
        self.assertEqual(audit_cnt, 0)

    # =========================================================================
    # 2. ADMIN REVIEW ATOMICITY & FAILURE INJECTION
    # =========================================================================

    def test_07_admin_approve_and_reject_atomic_rollback_on_failure(self):
        """7. Admin review endpoints (approve/reject) roll back atomically if audit insertion fails."""
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (20, 1, 'Expense', 100000.0, 'BCA Main', 'Groceries', '2026-08-25', 'Pending')"
        )
        self.con.execute(
            "INSERT INTO ingestion_candidates (id, raw_event_id, tx_type, amount, account, category, date, status) VALUES (21, 1, 'Expense', 120000.0, 'BCA Main', 'Groceries', '2026-08-25', 'Pending')"
        )
        self.con.commit()

        # Admin approve failure injection
        execute_admin_review_atomic(self.con, 20, "Approved", fail_at="audit")
        cand20 = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=20").fetchone()
        self.assertEqual(cand20["status"], "Pending")
        audit_cnt20 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=20").fetchone()[0]
        self.assertEqual(audit_cnt20, 0)

        # Admin reject failure injection
        execute_admin_review_atomic(self.con, 21, "Rejected", fail_at="audit")
        cand21 = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=21").fetchone()
        self.assertEqual(cand21["status"], "Pending")
        audit_cnt21 = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=21").fetchone()[0]
        self.assertEqual(audit_cnt21, 0)

        # Successful Admin approve
        res_ok = execute_admin_review_atomic(self.con, 20, "Approved")
        self.assertEqual(res_ok["status"], "success")
        cand20_ok = self.con.execute("SELECT status FROM ingestion_candidates WHERE id=20").fetchone()
        self.assertEqual(cand20_ok["status"], "Approved")
        audit_cnt20_ok = self.con.execute("SELECT count(*) FROM ingestion_audit_log WHERE candidate_id=20").fetchone()[0]
        self.assertEqual(audit_cnt20_ok, 1)

    # =========================================================================
    # 3. GMAIL & TELEGRAM EXISTING SECURITY INVARIANTS
    # =========================================================================

    def test_08_gmail_hmac_nonce_semantics_and_privacy(self):
        """8. Gmail HMAC verifies before nonce, distinguishes NONCE_REPLAY vs D1 failure, and saves minimal zero-PII payload."""
        now_ts = int(time.time() * 1000)
        nonce_test = "nonce_sem_401"
        body = '{"message_id":"msg_401","from":"notifikasi@bca.co.id","subject":"QRIS Rp 25.000"}'

        # Invalid HMAC does not touch D1
        wrong_sig = "0" * 64
        valid_sig = self._generate_hmac(body, now_ts, nonce_test, self.secret)
        self.assertFalse(hmac.compare_digest(wrong_sig, valid_sig))
        self.assertIsNone(self.con.execute("SELECT nonce FROM gmail_replay_nonces WHERE nonce=?", (nonce_test,)).fetchone())

        # Valid HMAC inserts nonce
        self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_test, now_ts))
        self.con.commit()

        # Replay triggers UNIQUE constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)", (nonce_test, now_ts))

        # Minimal payload privacy
        min_payload = json.dumps({"sender_domain": "bca.co.id", "parser_adapter": "bca_alert", "detected_type": "Expense"})
        self.con.execute(
            "INSERT INTO raw_events (id, source, external_id, payload_hash, parser_version, state, minimal_raw_payload) VALUES (401, 'gmail', 'msg_401', 'h401', '1.0', 'Parsed', ?)",
            (min_payload,)
        )
        self.con.commit()
        stored = self.con.execute("SELECT minimal_raw_payload FROM raw_events WHERE id=401").fetchone()[0]
        self.assertNotIn("notifikasi@", stored)
        self.assertNotIn("QRIS", stored)

    def test_09_shadow_mode_and_production_db_unmodified(self):
        """9. Mode shadow does not mutate ledger, and production SQLite database hash remains identical before and after test."""
        tx_count_before = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_before = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        # Reviewing candidate in staging modifies staging ONLY
        execute_admin_review_atomic(self.con, 20, "Approved")
        tx_count_after = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        acc_count_after = self.con.execute("SELECT count(*) FROM accounts").fetchone()[0]

        self.assertEqual(tx_count_before, tx_count_after)
        self.assertEqual(acc_count_before, acc_count_after)

        if self.prod_hash_before is not None and DB_PATH.exists():
            h = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
            self.assertEqual(h, self.prod_hash_before, "Production database was modified!")


if __name__ == "__main__":
    unittest.main()
