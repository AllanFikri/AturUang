"""
Tests for STAGE: FIX-EDGE-SYNC-CURSOR-V1
Verifies cursor pagination advances so all pending events are pulled.

Sections tested:
Section 6: Synthetic full catch-up test with 2262 Parsed events.
Section 7: 11 required test cases:
  1. cursor advances on first batch
  2. cursor advances on partial final batch
  3. cursor does not skip events
  4. cursor does not pull duplicates
  5. ack failure does not advance
  6. malformed response does not advance
  7. oversized response does not advance
  8. missing config does not advance
  9. wrong token does not advance
  10. concurrent pulls serialize
  11. production database unchanged across tests
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

from aturuang.server import (
    sync_edge_inbox,
    init_edge_sync_schema,
    AutoEdgeSyncScheduler,
    get_edge_sync_config,
)

EXPECTED_PRODUCTION_DB_SHA256 = (
    "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
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

            INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
            VALUES ('BCA Main', 'Owned', 1, 5000000.0);
            INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
            VALUES ('Merchant External', 'External', 1, 0.0);
        """)
        con.commit()
    finally:
        con.close()


def _make_synthetic_event(event_id: int, include_candidate: bool = False) -> dict:
    ev = {
        "id": event_id,
        "cursor": str(event_id),
        "message_id": f"msg_{event_id:06d}",
        "source": "gmail",
        "occurred_at": "2026-09-20T10:00:00Z",
        "payload_hash": hashlib.sha256(f"raw_{event_id}".encode("utf-8")).hexdigest(),
        "from": "ebanking@bca.co.id",
        "sender": "ebanking@bca.co.id",
        "subject": f"Transaksi msg_{event_id:06d}",
        "amount": 50000.0 + (event_id % 1000),
    }
    if include_candidate:
        ev["candidate"] = {
            "tx_type": "Expense",
            "amount": 50000.0 + (event_id % 1000),
            "account": "BCA Main",
            "to_account": "Merchant External",
            "category": "Food",
            "money_context": "Personal",
            "date": "2026-09-20",
            "time": "10:00:00",
            "match_tier": "EXACT",
            "reconciliation_status": "RECONCILED",
        }
    return ev


class MockWorkerState:
    """Mock Cloudflare Worker endpoint with D1-backed cursor semantics."""

    def __init__(self, events: list[dict], token: str = "test_token_2026"):
        self.events = sorted(events, key=lambda x: int(x["id"]))
        self.cursor = 0
        self.token = token
        self.fail_ack = False
        self.fail_pull_401 = False
        self.fail_pull_malformed = False
        self.fail_pull_oversized = False
        self.ack_history: list[dict] = []
        self.pull_history: list[tuple[int, int, int]] = []

    def handle_request(self, request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        headers = dict(request.headers) if hasattr(request, "headers") else {}

        # Authorization check
        auth_hdr = headers.get("Authorization") or headers.get("authorization", "")
        expected_auth = f"Bearer {self.token}"
        if auth_hdr != expected_auth:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        if "/api/sync/gmail" in url or "/api/ingestion/pending" in url:
            if self.fail_pull_401:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

            if self.fail_pull_malformed:
                resp = MagicMock()
                resp.status = 200
                resp.read.return_value = b'{"malformed_json: '
                resp.headers = {"Content-Length": str(len(resp.read.return_value))}
                resp.__enter__.return_value = resp
                return resp

            if self.fail_pull_oversized:
                resp = MagicMock()
                resp.status = 200
                large_blob = b"[" + (b'{"id": 1, "msg": "overflow"},' * 200000) + b'{"id": 1}]'
                resp.read.return_value = large_blob
                resp.headers = {"Content-Length": str(len(large_blob))}
                resp.__enter__.return_value = resp
                return resp

            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "cursor" in qs:
                req_cursor = int(qs["cursor"][0])
            else:
                req_cursor = self.cursor

            limit = int(qs.get("limit", [100])[0])
            limit = min(limit, 100)

            matched = [e for e in self.events if int(e["id"]) > req_cursor][:limit]
            self.pull_history.append((req_cursor, limit, len(matched)))

            data = json.dumps(matched).encode("utf-8")
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = data
            resp.headers = {"Content-Length": str(len(data))}
            resp.__enter__.return_value = resp
            return resp

        elif "/api/sync/ack" in url:
            if self.fail_ack:
                resp = MagicMock()
                resp.status = 500
                resp.read.return_value = b'{"status": "error", "message": "Internal error"}'
                resp.headers = {"Content-Length": str(len(resp.read.return_value))}
                resp.__enter__.return_value = resp
                return resp

            body_bytes = request.data if hasattr(request, "data") else b"{}"
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            new_cursor = body.get("cursor")
            if new_cursor is not None:
                self.cursor = max(self.cursor, int(new_cursor))
            self.ack_history.append(body)

            data = json.dumps({"status": "acknowledged", "cursor": str(self.cursor)}).encode("utf-8")
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = data
            resp.headers = {"Content-Length": str(len(data))}
            resp.__enter__.return_value = resp
            return resp

        resp = MagicMock()
        resp.status = 404
        resp.read.return_value = b'{"error": "Not found"}'
        resp.headers = {"Content-Length": "20"}
        resp.__enter__.return_value = resp
        return resp


class TestEdgeSyncCursorV1(unittest.TestCase):
    """Test suite for FIX-EDGE-SYNC-CURSOR-V1 cursor advance and catch-up."""

    def setUp(self) -> None:
        self.prod_db = _find_production_db()
        self.prod_hash_before = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(
            self.prod_hash_before,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash invariant violated at test setUp!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_cursor.db"
        self.staging_dir = self.temp_path / "staged"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        _init_synthetic_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        prod_hash_after = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(
            prod_hash_after,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash modified during test execution!",
        )

    # ==================================================================
    # Section 6: Regression Test — Synthetic Full Catch-Up (2262 events)
    # ==================================================================
    def test_synthetic_full_catch_up_2262_events(self) -> None:
        """Section 6: Pull loop catches up all 2262 Parsed events without duplicate staging."""
        total_events = 2262
        events = [_make_synthetic_event(i) for i in range(1, total_events + 1)]
        mock_worker = MockWorkerState(events, token="secret_token")

        pulled_counts: list[int] = []
        all_staged_ids: set[str] = set()
        seen_event_ids: list[int] = []

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            while True:
                res = sync_edge_inbox(
                    worker_url="https://worker.internal",
                    secret="secret_token",
                    db_path=self.db_path,
                    staging_dir=self.staging_dir,
                )
                self.assertEqual(res["status"], "success")
                fetched = res["fetched_count"]
                staged = res["staged_count"]
                pulled_counts.append(fetched)

                if fetched == 0:
                    break

                self.assertEqual(fetched, staged)

            # Verification of Section 6 claims
            total_pulled = sum(pulled_counts)
            self.assertEqual(total_pulled, 2262, "Total pulled must equal 2262")

            # Check database contents
            con = sqlite3.connect(str(self.db_path))
            try:
                rows = con.execute("SELECT message_id, payload FROM edge_synced_messages ORDER BY id ASC").fetchall()
                self.assertEqual(len(rows), 2262, "Must have exactly 2262 staged rows in SQLite")
                msg_ids = [r[0] for r in rows]
                self.assertEqual(len(set(msg_ids)), 2262, "All staged message_ids must be unique")

                # Verify cursor ends at the last event
                cur_row = con.execute(
                    "SELECT MAX(CAST(COALESCE(json_extract(payload, '$.cursor'), json_extract(payload, '$.id')) AS INTEGER)) "
                    "FROM edge_synced_messages"
                ).fetchone()
                cursor_final = cur_row[0]
                self.assertEqual(cursor_final, 2262, "Final cursor in SQLite must be 2262")
            finally:
                con.close()

            self.assertEqual(mock_worker.cursor, 2262, "Final cursor in Worker mock must be 2262")

            # Verify repeat pull after completion returns 0 new
            res_post = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="secret_token",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res_post["status"], "success")
            self.assertEqual(res_post["fetched_count"], 0)
            self.assertEqual(res_post["staged_count"], 0)

    # ==================================================================
    # Section 7: 11 Required Test Cases
    # ==================================================================

    def test_01_cursor_advances_on_first_batch(self) -> None:
        """1. Cursor advances on first batch of 100 items."""
        events = [_make_synthetic_event(i) for i in range(1, 251)]
        mock_worker = MockWorkerState(events, token="tok1")

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok1",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["fetched_count"], 100)
            self.assertEqual(res["staged_count"], 100)
            self.assertEqual(res["acknowledged_count"], 100)
            self.assertEqual(res["cursor"], "100")
            self.assertEqual(mock_worker.cursor, 100)

    def test_02_cursor_advances_on_partial_final_batch(self) -> None:
        """2. Cursor advances on partial final batch (e.g. 35 items)."""
        events = [_make_synthetic_event(i) for i in range(1, 136)]
        mock_worker = MockWorkerState(events, token="tok2")

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            # Batch 1: 100 items
            res1 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok2",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res1["fetched_count"], 100)
            self.assertEqual(res1["cursor"], "100")

            # Batch 2: partial final batch of 35 items
            res2 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok2",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res2["fetched_count"], 35)
            self.assertEqual(res2["staged_count"], 35)
            self.assertEqual(res2["acknowledged_count"], 35)
            self.assertEqual(res2["cursor"], "135")
            self.assertEqual(mock_worker.cursor, 135)

    def test_03_cursor_does_not_skip_events(self) -> None:
        """3. Cursor does not skip events across consecutive batches."""
        events = [_make_synthetic_event(i) for i in range(1, 251)]
        mock_worker = MockWorkerState(events, token="tok3")

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            for _ in range(3):
                sync_edge_inbox(
                    worker_url="https://worker.internal",
                    secret="tok3",
                    db_path=self.db_path,
                    staging_dir=self.staging_dir,
                )

        con = sqlite3.connect(str(self.db_path))
        try:
            rows = con.execute("SELECT json_extract(payload, '$.id') FROM edge_synced_messages ORDER BY CAST(json_extract(payload, '$.id') AS INTEGER) ASC").fetchall()
            ids = [r[0] for r in rows]
            self.assertEqual(ids, list(range(1, 251)), "Every event ID 1..250 must be present in sequence without skips")
        finally:
            con.close()

    def test_04_cursor_does_not_pull_duplicates(self) -> None:
        """4. Cursor does not pull duplicates across subsequent cycles."""
        events = [_make_synthetic_event(i) for i in range(1, 151)]
        mock_worker = MockWorkerState(events, token="tok4")

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            res1 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok4",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res1["fetched_count"], 100)

            res2 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok4",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res2["fetched_count"], 50)

            # Worker pull history shows 2nd pull requested cursor=100
            self.assertEqual(mock_worker.pull_history[1][0], 100)

            # Repeat pull when up to date: 0 items returned
            res3 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok4",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res3["fetched_count"], 0)
            self.assertEqual(res3["staged_count"], 0)

    def test_05_ack_failure_does_not_advance(self) -> None:
        """5. ACK failure does not advance Worker cursor and marks ack_failed."""
        events = [_make_synthetic_event(i) for i in range(1, 51)]
        mock_worker = MockWorkerState(events, token="tok5")
        mock_worker.fail_ack = True

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok5",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["fetched_count"], 50)
            self.assertEqual(res["staged_count"], 50)
            self.assertEqual(res["acknowledged_count"], 0)
            self.assertTrue(res.get("ack_failed"))
            self.assertEqual(mock_worker.cursor, 0, "Worker cursor must not advance on ACK failure")

    def test_06_malformed_response_does_not_advance(self) -> None:
        """6. Malformed JSON response is rejected and does not advance cursor."""
        events = [_make_synthetic_event(i) for i in range(1, 51)]
        mock_worker = MockWorkerState(events, token="tok6")
        mock_worker.fail_pull_malformed = True

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok6",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "RESPONSE_MALFORMED")
            self.assertEqual(res["staged_count"], 0)
            self.assertEqual(mock_worker.cursor, 0)

    def test_07_oversized_response_does_not_advance(self) -> None:
        """7. Oversized response (>5MB) is rejected and does not advance cursor."""
        events = [_make_synthetic_event(i) for i in range(1, 51)]
        mock_worker = MockWorkerState(events, token="tok7")
        mock_worker.fail_pull_oversized = True

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="tok7",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "RESPONSE_OVERSIZED")
            self.assertEqual(res["staged_count"], 0)
            self.assertEqual(mock_worker.cursor, 0)

    def test_08_missing_config_does_not_advance(self) -> None:
        """8. Missing config aborts safely without network calls or cursor advances."""
        with patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "", "STAGING_ADMIN_TOKEN": ""}, clear=True):
            res = sync_edge_inbox(
                worker_url=None,
                secret=None,
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res["status"], "skipped")
            self.assertEqual(res["reason"], "CONFIG_MISSING")
            self.assertEqual(res["staged_count"], 0)

    def test_09_wrong_token_does_not_advance(self) -> None:
        """9. Wrong token returns 401 and fails closed without advancing cursor."""
        events = [_make_synthetic_event(i) for i in range(1, 51)]
        mock_worker = MockWorkerState(events, token="correct_token")

        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="wrong_token",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["reason"], "INVALID_CREDENTIALS")
            self.assertEqual(res["staged_count"], 0)
            self.assertEqual(mock_worker.cursor, 0)

    def test_10_concurrent_pulls_serialize(self) -> None:
        """10. Concurrent pulls serialize safely without corrupting staging or cursor."""
        events = [_make_synthetic_event(i) for i in range(1, 101)]
        mock_worker = MockWorkerState(events, token="tok10")

        results: list[dict] = []
        errors: list[Exception] = []

        def worker_fn():
            try:
                res = sync_edge_inbox(
                    worker_url="https://worker.internal",
                    secret="tok10",
                    db_path=self.db_path,
                    staging_dir=self.staging_dir,
                )
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_fn) for _ in range(2)]
        with patch("urllib.request.urlopen", side_effect=mock_worker.handle_request):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        self.assertEqual(len(errors), 0, f"Concurrent pulls threw unexpected errors: {errors}")
        self.assertEqual(len(results), 2)
        # Exactly 100 items should be staged in SQLite without duplicate rows
        con = sqlite3.connect(str(self.db_path))
        try:
            count = con.execute("SELECT COUNT(*) FROM edge_synced_messages").fetchone()[0]
            self.assertEqual(count, 100)
        finally:
            con.close()

    def test_11_production_database_unchanged_across_tests(self) -> None:
        """11. Production database SHA-256 remains completely untouched across all tests."""
        prod_hash_now = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(
            prod_hash_now,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database hash changed during execution!",
        )


if __name__ == "__main__":
    unittest.main()
