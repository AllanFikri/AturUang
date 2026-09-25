"""
Tests for STAGE: EDGE-SYNC-AUTOMATION-V1
Verifies the 20 specific requirements from Section 7:
1. valid auto-sync run end-to-end (mocked remote)
2. missing WORKER_URL disables sync without crash
3. missing STAGING_ADMIN_TOKEN disables sync without crash
4. wrong token returns safe code
5. malformed response rejected
6. oversized response rejected
7. unexpected schema rejected
8. repeated pull idempotency
9. repeated pull does not duplicate review candidates
10. concurrent pulls are serialized
11. status transitions are fail-closed
12. last_synced_at updates only on success
13. sync failure does not change last_synced_at
14. manual pull still works
15. manual pull still requires auth
16. production SQLite unchanged across all tests
17. no unbounded retry when credentials are invalid
18. UI freshness label matches actual state
19. no channel-specific shortcut to ledger
20. existing SEC-REMED-R2 protections not weakened
"""
from __future__ import annotations

from decimal import Decimal
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from aturuang.safe_apply import (
    ApplyCandidate,
    CandidateLifecycleState,
    LedgerMutation,
    SafeApplyEngine,
    compute_preview_hash,
)
from aturuang.review_queue_ui import ReviewQueueManager
from aturuang.server import (
    Handler,
    AutoEdgeSyncScheduler,
    get_auto_sync_scheduler,
    start_auto_sync_scheduler,
    stop_auto_sync_scheduler,
    sync_edge_inbox,
    init_edge_sync_schema,
    get_edge_sync_config,
    is_wrong_secret_type,
    get_local_auth_token,
    set_local_auth_token,
    _last_edge_sync_info,
)

EXPECTED_PRODUCTION_DB_SHA256 = (
    "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
)


def _find_production_db() -> Path:
    candidates = [
        Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db"),
        Path(__file__).resolve().parent.parent.parent / "AturUang" / "runtime" / "money_tracks.db",
        Path(__file__).resolve().parent.parent / "runtime" / "money_tracks.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Production database not found")


def _init_synthetic_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL DEFAULT 'Owned',
                active INTEGER NOT NULL DEFAULT 1,
                current_balance REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS transaction_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER,
                action TEXT NOT NULL,
                old_data TEXT NOT NULL DEFAULT '',
                new_data TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
            VALUES ('BCA Main', 'Owned', 1, 5000000.0);
            INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
            VALUES ('Merchant External', 'External', 1, 0.0);
            INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
            VALUES ('Jago Main', 'Owned', 1, 2000000.0);
        """)
        con.commit()
    finally:
        con.close()


class TestEdgeSyncAutomationV1(unittest.TestCase):
    """Rigorous tests covering all 20 requirements of STAGE: EDGE-SYNC-AUTOMATION-V1."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.prod_db = _find_production_db()
        cls.initial_hash = hashlib.sha256(cls.prod_db.read_bytes()).hexdigest()
        if cls.initial_hash != EXPECTED_PRODUCTION_DB_SHA256:
            raise RuntimeError(f"Production DB hash mismatch at test start: {cls.initial_hash}")

    @classmethod
    def tearDownClass(cls) -> None:
        current_hash = hashlib.sha256(cls.prod_db.read_bytes()).hexdigest()
        if current_hash != EXPECTED_PRODUCTION_DB_SHA256:
            raise RuntimeError(f"Production DB hash changed during tests! {current_hash}")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.home_patcher = patch.object(Path, "home", return_value=self.temp_path / "home")
        self.home_patcher.start()
        _last_edge_sync_info.update({
            "status": "IDLE",
            "last_synced_at": None,
            "last_sync_at": None,
            "last_error": None,
            "freshness": "Belum pernah sinkron",
            "is_stale": False,
            "last_result": None,
        })
        self.db_path = self.temp_path / "synthetic_test.db"
        self._db_file_patcher = patch("aturuang.server.DB_FILE", self.db_path)
        self._db_file_patcher.start()
        self.staging_path = self.temp_path / "staged"
        self.backup_path = self.temp_path / "backups"
        self.backup_path.mkdir(parents=True, exist_ok=True)
        _init_synthetic_db(self.db_path)
        self.engine = SafeApplyEngine(self.db_path, self.backup_path)
        self.review_manager = ReviewQueueManager(self.db_path, self.backup_path)
        self.local_token = "synthetic_local_auth_token_for_tests_12345"
        set_local_auth_token(self.local_token)

    def tearDown(self) -> None:
        self.home_patcher.stop()
        self._db_file_patcher.stop()
        stop_auto_sync_scheduler()
        set_local_auth_token(None)
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _make_handler(self, method: str, path: str, headers: dict[str, str] | None = None, body_bytes: bytes = b"") -> Handler:
        handler = Handler.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body_bytes)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 54321)
        handler.send_error = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.send_json = MagicMock()
        return handler

    # 1. Valid auto-sync run end-to-end (mocked remote)
    def test_01_valid_auto_sync_run_end_to_end(self) -> None:
        mock_messages = [
            {
                "message_id": "auto_msg_001",
                "from": "ebanking@bca.co.id",
                "subject": "Transaksi Pembayaran QRIS",
                "amount": 75000.0,
                "account_from": "BCA Main",
                "account_to": "Merchant External",
                "description": "Makan Siang",
                "category": "Dining",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
            }
        ]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
                resp.headers = {"Content-Length": str(len(resp.read.return_value))}
            elif "/api/sync/ack" in url:
                resp.read.return_value = b'{"status": "acknowledged"}'
                resp.headers = {"Content-Length": "25"}
            else:
                resp.read.return_value = b"[]"
                resp.headers = {"Content-Length": "2"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            scheduler = AutoEdgeSyncScheduler(
                interval_seconds=300,
                target_db=self.db_path,
            )
            with patch.dict(os.environ, {
                "CLOUDFLARE_WORKER_URL": "https://worker.internal",
                "STAGING_ADMIN_TOKEN": "staging_secret_token_123",
            }):
                res = scheduler.run_cycle()
                self.assertEqual(res["status"], "success")
                self.assertEqual(res["staged_count"], 1)
                self.assertEqual(res["auto_applied_count"], 1)

                con = sqlite3.connect(str(self.db_path))
                try:
                    row = con.execute("SELECT status, content_hash FROM edge_synced_messages WHERE message_id = 'auto_msg_001'").fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row[0], "AUTO_APPLIED")
                    self.assertIsNotNone(row[1])
                finally:
                    con.close()

    # 2. Missing WORKER_URL disables sync without crash
    def test_02_missing_worker_url_disables_sync_without_crash(self) -> None:
        with patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "", "WORKER_URL": "", "EDGE_WORKER_URL": "", "STAGING_ADMIN_TOKEN": "some_token"}, clear=True):
            scheduler = AutoEdgeSyncScheduler(target_db=self.db_path)
            res = scheduler.run_cycle()
            self.assertEqual(res["status"], "skipped")
            self.assertEqual(res["reason"], "CONFIG_MISSING")

    # 3. Missing STAGING_ADMIN_TOKEN disables sync without crash
    def test_03_missing_staging_admin_token_disables_sync_without_crash(self) -> None:
        with patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "https://worker.internal", "STAGING_ADMIN_TOKEN": "", "EDGE_SYNC_SECRET": ""}, clear=True):
            scheduler = AutoEdgeSyncScheduler(target_db=self.db_path)
            res = scheduler.run_cycle()
            self.assertEqual(res["status"], "skipped")
            self.assertEqual(res["reason"], "CONFIG_MISSING")

    # 4. Wrong token returns safe code
    def test_04_wrong_token_returns_safe_code(self) -> None:
        # A. HTTP 401 from Worker
        def mock_urlopen_401(req, *args, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_401):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="invalid_token_xyz",
                db_path=self.db_path,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "INVALID_CREDENTIALS")

        # B. Wrong secret type (GMAIL_RELAY_SECRET or REPAIR_SECRET)
        res_wrong = sync_edge_inbox(
            worker_url="https://worker.internal",
            secret="GMAIL_RELAY_SECRET",
            db_path=self.db_path,
        )
        self.assertEqual(res_wrong["status"], "disabled")
        self.assertEqual(res_wrong["reason"], "WRONG_SECRET_TYPE")

    # 5. Malformed response rejected
    def test_05_malformed_response_rejected(self) -> None:
        def mock_urlopen_bad_json(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = b"<!DOCTYPE html><html><body>Error 502 Bad Gateway</body></html>"
            resp.headers = {"Content-Length": "60"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_bad_json):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="valid_token_123",
                db_path=self.db_path,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "RESPONSE_MALFORMED")

    # 6. Oversized response rejected
    def test_06_oversized_response_rejected(self) -> None:
        def mock_urlopen_oversized(req, *args, **kwargs):
            resp = MagicMock()
            # 6MB payload exceeding 5MB limit
            resp.read.return_value = b"x" * (6 * 1024 * 1024)
            resp.headers = {"Content-Length": str(6 * 1024 * 1024)}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_oversized):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="valid_token_123",
                db_path=self.db_path,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "RESPONSE_OVERSIZED")

    # 7. Unexpected schema rejected
    def test_07_unexpected_schema_rejected(self) -> None:
        # A. Not list or dict
        def mock_urlopen_scalar(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = b'"unexpected string payload"'
            resp.headers = {"Content-Length": "27"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_scalar):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="valid_token_123",
                db_path=self.db_path,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "SCHEMA_UNEXPECTED")

        # B. Elements are not objects
        def mock_urlopen_bad_elements(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(["not_a_dict_item", 12345]).encode("utf-8")
            resp.headers = {"Content-Length": "26"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_bad_elements):
            res2 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="valid_token_123",
                db_path=self.db_path,
            )
            self.assertEqual(res2["status"], "unreachable")
            self.assertEqual(res2["reason"], "SCHEMA_UNEXPECTED")

    # 8. Repeated pull idempotency
    def test_08_repeated_pull_idempotency(self) -> None:
        mock_messages = [
            {"message_id": "idem_msg_101", "from": "jago@jago.com", "subject": "Transfer Masuk", "amount": 50000.0}
        ]

        def mock_urlopen(req, *args, **kwargs):
            resp = MagicMock()
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.headers = {"Content-Length": "20"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res1 = sync_edge_inbox(worker_url="https://worker.internal", secret="sec_1", db_path=self.db_path)
            self.assertEqual(res1["staged_count"], 1)

            res2 = sync_edge_inbox(worker_url="https://worker.internal", secret="sec_1", db_path=self.db_path)
            self.assertEqual(res2["staged_count"], 0)

            con = sqlite3.connect(str(self.db_path))
            try:
                cnt = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE message_id='idem_msg_101'").fetchone()[0]
                self.assertEqual(cnt, 1)
            finally:
                con.close()

    # 9. Repeated pull does not duplicate review candidates
    def test_09_repeated_pull_does_not_duplicate_review_candidates(self) -> None:
        mock_ambiguous = [
            {
                "message_id": "ambig_001",
                "from": "bca@bca.co.id",
                "subject": "Transfer Cabang",
                "amount": 250000.0,
                "match_tier": "AMBIGUOUS",
                "reconciliation_status": "UNRESOLVED",
            }
        ]

        def mock_urlopen(req, *args, **kwargs):
            resp = MagicMock()
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_ambiguous).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.headers = {"Content-Length": "20"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res1 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="sec_1",
                db_path=self.db_path,
                apply_engine=self.engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res1["review_required_count"], 1)
            initial_review_items = len(self.review_manager.get_pending_items())

            # Second pull must skip and not add duplicate review item
            res2 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="sec_1",
                db_path=self.db_path,
                apply_engine=self.engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res2["review_required_count"], 0)
            self.assertEqual(len(self.review_manager.get_pending_items()), initial_review_items)

    # 10. Concurrent pulls are serialized
    def test_10_concurrent_pulls_are_serialized(self) -> None:
        pull_order = []
        concurrency_violation = []

        def mock_urlopen(req, *args, **kwargs):
            tid = threading.get_ident()
            pull_order.append(f"enter_{tid}")
            time.sleep(0.05)
            pull_order.append(f"exit_{tid}")
            resp = MagicMock()
            resp.read.return_value = json.dumps([{"message_id": f"msg_{tid}", "amount": 1000}]).encode("utf-8")
            resp.headers = {"Content-Length": "30"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        threads = []
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for i in range(3):
                t = threading.Thread(
                    target=sync_edge_inbox,
                    kwargs={"worker_url": "https://worker.internal", "secret": "sec_1", "db_path": self.db_path},
                )
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

        # Check serialization: an exit must follow an enter before another enter can occur
        currently_active = 0
        for event in pull_order:
            if event.startswith("enter"):
                currently_active += 1
                if currently_active > 1:
                    concurrency_violation.append("Concurrent execution detected!")
            elif event.startswith("exit"):
                currently_active -= 1

        self.assertEqual(len(concurrency_violation), 0, f"Concurrency violation: {concurrency_violation}")

    # 11. Status transitions are fail-closed (never APPLIED without explicit apply gate)
    def test_11_status_transitions_are_fail_closed(self) -> None:
        mock_raw = [
            {"message_id": "unparsed_001", "from": "unknown@sender.com", "subject": "No Amount Here"}
        ]

        def mock_urlopen(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_raw).encode("utf-8")
            resp.headers = {"Content-Length": "20"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="sec_1",
                db_path=self.db_path,
                apply_engine=self.engine,
            )
            self.assertEqual(res["auto_applied_count"], 0)

            con = sqlite3.connect(str(self.db_path))
            try:
                row = con.execute("SELECT status FROM edge_synced_messages WHERE message_id='unparsed_001'").fetchone()
                self.assertEqual(row[0], "STAGED", "Unparsed or unconfirmed evidence must never transition to AUTO_APPLIED")
            finally:
                con.close()

    # 12. last_synced_at updates only on success
    def test_12_last_synced_at_updates_only_on_success(self) -> None:
        handler = self._make_handler("GET", "/api/sync/status")
        handler.do_GET()
        info_before = handler.send_json.call_args[0][0]
        self.assertIsNone(info_before.get("last_synced_at"))

        def mock_urlopen_success(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = b"[]"
            resp.headers = {"Content-Length": "2"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_success):
            sync_edge_inbox(worker_url="https://worker.internal", secret="sec_1", db_path=self.db_path)

        handler2 = self._make_handler("GET", "/api/sync/status")
        handler2.do_GET()
        info_after = handler2.send_json.call_args[0][0]
        self.assertIsNotNone(info_after.get("last_synced_at"))
        self.assertEqual(info_after.get("status"), "OK")

    # 13. Sync failure does not change last_synced_at
    def test_13_sync_failure_does_not_change_last_synced_at(self) -> None:
        # 1. Establish initial success
        def mock_urlopen_ok(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = b"[]"
            resp.headers = {"Content-Length": "2"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_ok):
            sync_edge_inbox(worker_url="https://worker.internal", secret="sec_1", db_path=self.db_path)

        handler1 = self._make_handler("GET", "/api/sync/status")
        handler1.do_GET()
        last_synced = handler1.send_json.call_args[0][0]["last_synced_at"]
        self.assertIsNotNone(last_synced)

        # 2. Trigger network failure
        def mock_urlopen_fail(req, *args, **kwargs):
            raise urllib.error.URLError("Connection refused")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_fail):
            sync_edge_inbox(worker_url="https://worker.internal", secret="sec_1", db_path=self.db_path)

        handler2 = self._make_handler("GET", "/api/sync/status")
        handler2.do_GET()
        info_after_fail = handler2.send_json.call_args[0][0]
        self.assertEqual(info_after_fail["last_synced_at"], last_synced, "last_synced_at must not be modified on failure")
        self.assertEqual(info_after_fail["status"], "FAILED")
        self.assertEqual(info_after_fail["last_error"], "NETWORK_ERROR")

    # 14. Manual pull still works
    def test_14_manual_pull_still_works(self) -> None:
        def mock_urlopen(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([{"message_id": "man_001", "amount": 25000.0}]).encode("utf-8")
            resp.headers = {"Content-Length": "20"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch.dict(os.environ, {"STAGING_ADMIN_TOKEN": "valid_staging_token_123", "CLOUDFLARE_WORKER_URL": "https://worker.internal"}):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                h = self._make_handler(
                    "POST",
                    "/api/sync/pull-cloud",
                    {
                        "Authorization": "Bearer valid_staging_token_123",
                        "Host": "127.0.0.1:5050",
                        "Origin": "http://127.0.0.1:5050",
                    },
                )
                h.do_POST()
                h.send_json.assert_called()
                call_arg = h.send_json.call_args[0][0]
                self.assertEqual(call_arg.get("status"), "success")

    # 15. Manual pull still requires auth
    def test_15_manual_pull_still_requires_auth(self) -> None:
        with patch.dict(os.environ, {"STAGING_ADMIN_TOKEN": "staging_secret_999"}):
            # Missing auth header
            h = self._make_handler(
                "POST",
                "/api/sync/pull-cloud",
                {"Host": "127.0.0.1:5050", "Origin": "http://127.0.0.1:5050"},
            )
            h.do_POST()
            h.send_json.assert_called()
            self.assertEqual(h.send_json.call_args[1]["status"], 401)
            self.assertEqual(h.send_json.call_args[0][0]["error"], "MISSING_CREDENTIALS")

            # Invalid auth header
            h2 = self._make_handler(
                "POST",
                "/api/sync/pull-cloud",
                {
                    "Authorization": "Bearer completely_wrong_secret",
                    "Host": "127.0.0.1:5050",
                    "Origin": "http://127.0.0.1:5050",
                },
            )
            h2.do_POST()
            self.assertEqual(h2.send_json.call_args[1]["status"], 401)
            self.assertEqual(h2.send_json.call_args[0][0]["error"], "INVALID_CREDENTIALS")

    # 16. Production SQLite unchanged across all tests
    def test_16_production_sqlite_unchanged_across_all_tests(self) -> None:
        current_hash = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(
            current_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database file has been mutated during testing!",
        )

    # 17. No unbounded retry when credentials are invalid
    def test_17_no_unbounded_retry_when_credentials_are_invalid(self) -> None:
        scheduler = AutoEdgeSyncScheduler(
            interval_seconds=30,
            max_backoff_seconds=3600,
            max_auth_failures=3,
            target_db=self.db_path,
        )

        def mock_urlopen_401(req, *args, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "https://worker.internal", "STAGING_ADMIN_TOKEN": "wrong_tok"}):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen_401):
                # Cycle 1: initial backoff doubles
                res1 = scheduler.run_cycle()
                self.assertEqual(res1["reason"], "INVALID_CREDENTIALS")
                self.assertEqual(scheduler.current_backoff, 60)
                self.assertEqual(scheduler.consecutive_auth_failures, 1)

                # Cycle 2: doubles to 120
                res2 = scheduler.run_cycle()
                self.assertEqual(scheduler.current_backoff, 120)
                self.assertEqual(scheduler.consecutive_auth_failures, 2)

                # Cycle 3: doubles to 240
                res3 = scheduler.run_cycle()
                self.assertEqual(scheduler.current_backoff, 240)
                self.assertEqual(scheduler.consecutive_auth_failures, 3)

                # Cycle 4: exceeds max_auth_failures (3) -> fails closed with MAX_RETRIES_EXCEEDED
                res4 = scheduler.run_cycle()
                self.assertEqual(res4["status"], "disabled")
                self.assertEqual(res4["reason"], "MAX_RETRIES_EXCEEDED")

    # 18. UI freshness label matches actual state
    def test_18_ui_freshness_label_matches_actual_state(self) -> None:
        # A. Fresh (< 30 minutes)
        def mock_urlopen_ok(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = b"[]"
            resp.headers = {"Content-Length": "2"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_ok):
            sync_edge_inbox(worker_url="https://worker.internal", secret="sec_1", db_path=self.db_path)

        h = self._make_handler("GET", "/api/sync/status")
        h.do_GET()
        info = h.send_json.call_args[0][0]
        self.assertFalse(info["is_stale"])
        self.assertIn("WIB", info["freshness"])
        self.assertIn("(Segar)", info["freshness"])

        # B. Stale (> 30 minutes ago)
        old_time = (dt.datetime.now() - dt.timedelta(minutes=45)).isoformat()
        with patch.dict("aturuang.server._last_edge_sync_info", {"last_synced_at": old_time, "status": "OK"}):
            h_stale = self._make_handler("GET", "/api/sync/status")
            h_stale.do_GET()
            info_stale = h_stale.send_json.call_args[0][0]
            self.assertTrue(info_stale["is_stale"])
            self.assertIn("(Usang)", info_stale["freshness"])

    # 19. No channel-specific shortcut to ledger
    def test_19_no_channel_specific_shortcut_to_ledger(self) -> None:
        """All edge evidence must route through staging and SafeApplyEngine / review queue."""
        mock_evidence = [
            {
                "message_id": "direct_check_01",
                "from": "bca@bca.co.id",
                "amount": 999999.0,
                "match_tier": "WEAK",
                "reconciliation_status": "UNRESOLVED",
            }
        ]

        def mock_urlopen(req, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_evidence).encode("utf-8")
            resp.headers = {"Content-Length": "20"}
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="sec_1",
                db_path=self.db_path,
                apply_engine=self.engine,
                review_manager=self.review_manager,
            )

        con = sqlite3.connect(str(self.db_path))
        try:
            # Must NOT be written directly to transactions ledger
            tx = con.execute("SELECT id FROM transactions WHERE amount=999999.0").fetchone()
            self.assertIsNone(tx, "Weak / unresolved evidence must never be written directly to ledger")
            # Must be routed to review queue
            st = con.execute("SELECT status FROM edge_synced_messages WHERE message_id='direct_check_01'").fetchone()
            self.assertEqual(st[0], "REVIEW_REQUIRED")
        finally:
            con.close()

    # 20. Existing SEC-REMED-R2 protections not weakened
    def test_20_existing_sec_remed_r2_protections_not_weakened(self) -> None:
        # A. Unauthenticated download_db rejected with 401
        h_db = self._make_handler("GET", "/api/download_db", {"Host": "127.0.0.1:5050"})
        h_db.do_GET()
        h_db.send_error.assert_called_with(401, "Missing local authentication")

        # B. Unauthenticated export_csv rejected with 401
        h_csv = self._make_handler("GET", "/api/export_csv", {"Host": "127.0.0.1:5050"})
        h_csv.do_GET()
        h_csv.send_error.assert_called_with(401, "Missing local authentication")

        # C. Disallowed CORS origin rejected with None
        h_cors = self._make_handler("GET", "/api/sync/status", {"Origin": "https://evil-site.com", "Host": "127.0.0.1:5050"})
        self.assertIsNone(h_cors._get_allowed_origin())

        # D. Path traversal rejected with 403
        h_trav = self._make_handler("GET", "/assets/%2e%2e/secret.txt")
        h_trav.do_GET()
        h_trav.send_error.assert_called_with(403, "Forbidden")


if __name__ == "__main__":
    unittest.main()
