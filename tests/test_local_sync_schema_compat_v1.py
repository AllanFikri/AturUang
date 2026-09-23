"""
Tests for STEP-11A: FIX-LOCAL-SYNC-SCHEMA-COMPAT-V1.

Verifies:
1. sync_edge_inbox accepts {results, candidates, filtered_count}
2. sync_edge_inbox accepts legacy list shape
3. sync_edge_inbox rejects unknown dict shape
4. sync_edge_inbox prefers results over candidates
5. filtered_count is never used as messages list
6. manual pull endpoint returns 200 with valid local auth
7. manual pull endpoint returns 401 with invalid local auth
8. CF secret never appears in browser response
9. idempotency holds across two runs of the same response
10. Production DB unchanged across tests
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

from aturuang.server import (
    Handler,
    sync_edge_inbox,
    init_edge_sync_schema,
    get_local_auth_token,
)

EXPECTED_PRODUCTION_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")


def _get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


class TestLocalSyncSchemaCompatV1(unittest.TestCase):
    """Test suite for FIX-LOCAL-SYNC-SCHEMA-COMPAT-V1 schema compatibility and manual pull auth."""

    def setUp(self) -> None:
        self.prod_hash_before = _get_prod_db_hash()
        self.assertEqual(
            self.prod_hash_before,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash invariant violated at test setUp!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_schema.db"
        self.staging_dir = self.temp_path / "staged"
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        con = sqlite3.connect(str(self.db_path))
        init_edge_sync_schema(con)
        con.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        prod_hash_after = _get_prod_db_hash()
        self.assertEqual(
            prod_hash_after,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash modified during test execution!",
        )

    def _make_handler(self, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> Handler:
        handler = Handler.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body or b"")
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 54321)
        handler.send_error = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.send_json = MagicMock()
        return handler

    # =========================================================================
    # Schema Compatibility Tests
    # =========================================================================

    def test_01_sync_accepts_results_candidates_filtered_count(self) -> None:
        """1. sync_edge_inbox accepts current Worker shape {results, candidates, filtered_count} including empty results."""
        # Non-empty results
        mock_resp = {
            "results": [
                {
                    "id": 101,
                    "cursor": "101",
                    "message_id": "msg_shape2_01",
                    "candidate": {
                        "raw_event_id": 101,
                        "date": "2026-09-20",
                        "amount": 50000,
                        "account": "BCA Main",
                        "tx_type": "Expense",
                    },
                }
            ],
            "candidates": [
                {
                    "raw_event_id": 101,
                    "date": "2026-09-20",
                    "amount": 50000,
                    "account": "BCA Main",
                    "tx_type": "Expense",
                }
            ],
            "filtered_count": 0,
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_resp).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["fetched_count"], 1)
        self.assertEqual(res["staged_count"], 1)

        # Empty results (all caught up)
        mock_empty_resp = {
            "results": [],
            "candidates": [],
            "filtered_count": 0,
        }

        def mock_urlopen_empty(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_empty_resp).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_empty):
            res_empty = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res_empty["status"], "success")
        self.assertEqual(res_empty["fetched_count"], 0)
        self.assertEqual(res_empty["staged_count"], 0)

    def test_02_sync_accepts_legacy_list(self) -> None:
        """2. sync_edge_inbox accepts legacy flat list of items."""
        legacy_list = [
            {
                "id": 201,
                "cursor": "201",
                "message_id": "msg_legacy_01",
                "candidate": {
                    "raw_event_id": 201,
                    "date": "2026-09-20",
                    "amount": 25000,
                    "account": "BCA Main",
                    "tx_type": "Expense",
                },
            }
        ]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(legacy_list).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["fetched_count"], 1)
        self.assertEqual(res["staged_count"], 1)

    def test_03_sync_rejects_unknown_dict_shape(self) -> None:
        """3. sync_edge_inbox rejects unknown dict shape with SCHEMA_UNEXPECTED."""
        unknown_resp = {"unknown_envelope": [{"id": 1}], "status_code": 200}

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(unknown_resp).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res["status"], "unreachable")
        self.assertEqual(res["reason"], "SCHEMA_UNEXPECTED")

    def test_04_sync_prefers_results_over_candidates(self) -> None:
        """4. sync_edge_inbox prefers 'results' when both 'results' and 'candidates' exist."""
        mock_resp = {
            "results": [
                {
                    "id": 401,
                    "cursor": "401",
                    "message_id": "res_preferred_401",
                    "candidate": {
                        "raw_event_id": 401,
                        "date": "2026-09-20",
                        "amount": 40000,
                        "account": "BCA Main",
                        "tx_type": "Expense",
                    },
                }
            ],
            "candidates": [
                {
                    "raw_event_id": 999,
                    "date": "2026-09-20",
                    "amount": 99999,
                    "account": "Other Account",
                    "tx_type": "Expense",
                }
            ],
            "filtered_count": 0,
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_resp).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["staged_ids"], ["res_preferred_401"])

        con = sqlite3.connect(str(self.db_path))
        row = con.execute("SELECT message_id, json_extract(payload, '$.amount') FROM edge_synced_messages WHERE message_id = 'res_preferred_401'").fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], 40000)

    def test_05_filtered_count_never_used_as_messages(self) -> None:
        """5. filtered_count is never treated as messages list."""
        mock_resp = {
            "filtered_count": [1, 2, 3],
            "metadata": "non_list",
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_resp).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res["status"], "unreachable")
        self.assertEqual(res["reason"], "SCHEMA_UNEXPECTED")

    # =========================================================================
    # Manual Pull Authentication Tests
    # =========================================================================

    def test_06_manual_pull_endpoint_returns_200_with_valid_local_auth(self) -> None:
        """6. POST /api/sync/pull-cloud returns 200 with valid local CSRF auth and server injects CF secret."""
        local_tok = get_local_auth_token()
        cf_sec = "staging_cf_token_secret_123"

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps({"results": [], "candidates": [], "filtered_count": 0}).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("aturuang.server.get_edge_sync_config", return_value=("https://worker.internal", cf_sec)):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                h = self._make_handler(
                    "POST",
                    "/api/sync/pull-cloud",
                    {
                        "Host": "127.0.0.1:5050",
                        "Origin": "http://127.0.0.1:5050",
                        "X-CSRF-Token": local_tok,
                    },
                )
                h.do_POST()
                h.send_json.assert_called()
                call_args = h.send_json.call_args
                status = call_args[1].get("status", 200)
                body = call_args[0][0]
                self.assertEqual(status, 200)
                self.assertEqual(body.get("status"), "success")

    def test_07_manual_pull_endpoint_returns_401_with_invalid_local_auth(self) -> None:
        """7. POST /api/sync/pull-cloud returns 401 with invalid local auth token."""
        h = self._make_handler(
            "POST",
            "/api/sync/pull-cloud",
            {
                "Host": "127.0.0.1:5050",
                "Origin": "http://127.0.0.1:5050",
                "X-CSRF-Token": "bad_invalid_local_token",
            },
        )
        h.do_POST()
        h.send_json.assert_called()
        self.assertEqual(h.send_json.call_args[1]["status"], 401)
        self.assertEqual(h.send_json.call_args[0][0]["error"], "INVALID_LOCAL_AUTH")

    def test_08_cf_secret_never_in_browser_response(self) -> None:
        """8. CF secret never appears in any response from manual pull."""
        local_tok = get_local_auth_token()
        cf_sec = "super_secret_cf_token_never_leak_xyz"

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps({"results": [], "candidates": [], "filtered_count": 0}).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("aturuang.server.get_edge_sync_config", return_value=("https://worker.internal", cf_sec)):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                h = self._make_handler(
                    "POST",
                    "/api/sync/pull-cloud",
                    {
                        "Host": "127.0.0.1:5050",
                        "Origin": "http://127.0.0.1:5050",
                        "X-CSRF-Token": local_tok,
                    },
                )
                h.do_POST()
                body = h.send_json.call_args[0][0]
                body_str = json.dumps(body)
                self.assertNotIn(cf_sec, body_str)

    def test_09_idempotency_holds_across_two_runs_of_same_response(self) -> None:
        """9. Idempotency holds across two runs of the same response."""
        mock_resp = {
            "results": [
                {
                    "id": 901,
                    "cursor": "901",
                    "message_id": "msg_idem_901",
                    "candidate": {
                        "raw_event_id": 901,
                        "date": "2026-09-20",
                        "amount": 30000,
                        "account": "BCA Main",
                        "tx_type": "Expense",
                    },
                }
            ],
            "filtered_count": 0,
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(mock_resp).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res1 = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)
            res2 = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res1["staged_count"], 1)
        self.assertEqual(res2["staged_count"], 0)

        con = sqlite3.connect(str(self.db_path))
        cnt = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE message_id = 'msg_idem_901'").fetchone()[0]
        con.close()
        self.assertEqual(cnt, 1)

    def test_10_production_db_unchanged(self) -> None:
        """10. Production DB hash matches expected baseline."""
        self.assertEqual(_get_prod_db_hash(), EXPECTED_PRODUCTION_DB_SHA256)


if __name__ == "__main__":
    unittest.main()
