"""
Tests for FIX-LOCAL-EDGE-SYNC-PAYLOAD-V1.

Verifies:
1. payload uses candidate dict when present
2. payload merges root metadata
3. payload uses item when candidate absent
4. payload uses item when candidate is malformed
5. content_hash matches payload_str
6. idempotency holds
7. ack payload correct
8. cursor advances
9. downstream validate_edge_candidate now sees date/account/tx_type
10. downstream sees amount as number
11. no secret leaks
12. Production DB unchanged across tests
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

from aturuang.server import (
    sync_edge_inbox,
    init_edge_sync_schema,
    validate_edge_candidate,
)

EXPECTED_PRODUCTION_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")


def _get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


class TestLocalEdgeSyncPayloadV1(unittest.TestCase):
    """Test suite for FIX-LOCAL-EDGE-SYNC-PAYLOAD-V1 candidate payload storage."""

    def setUp(self) -> None:
        self.prod_hash_before = _get_prod_db_hash()
        self.assertEqual(
            self.prod_hash_before,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash invariant violated at test setUp!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_payload.db"
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

    # =========================================================================
    # Section 6: Regression Test
    # =========================================================================

    def test_section_6_regression_test(self) -> None:
        """Section 6 regression test covering candidate items, legacy items, deduplication, ack, and cursor."""
        # 1. 3 synthetic items with candidate dicts
        candidate_items = [
            {
                "id": 101,
                "cursor": "101",
                "message_id": "cand_msg_101",
                "occurred_at": "2026-09-20T10:00:00.000Z",
                "sender": "bca@bca.co.id",
                "subject": "Transfer BCA 1",
                "candidate": {
                    "raw_event_id": 101,
                    "date": "2026-09-20",
                    "time": "17:00:00",
                    "amount": 50000,
                    "account": "BCA Main",
                    "tx_type": "Expense",
                    "match_tier": "EXACT",
                    "reconciliation_status": "RECONCILED",
                    "status": "Ready",
                },
            },
            {
                "id": 102,
                "cursor": "102",
                "message_id": "cand_msg_102",
                "occurred_at": "2026-09-21T11:00:00.000Z",
                "sender": "noreply@jago.com",
                "subject": "Transfer Jago 2",
                "candidate": {
                    "raw_event_id": 102,
                    "date": "2026-09-21",
                    "time": "18:00:00",
                    "amount": 75000,
                    "account": "Jago Main",
                    "tx_type": "Income",
                    "match_tier": "EXACT",
                    "reconciliation_status": "RECONCILED",
                    "status": "Ready",
                },
            },
            {
                "id": 103,
                "cursor": "103",
                "message_id": "cand_msg_103",
                "occurred_at": "2026-09-22T12:00:00.000Z",
                "sender": "no-reply@flip.id",
                "subject": "Transfer Flip 3",
                "candidate": {
                    "raw_event_id": 103,
                    "date": "2026-09-22",
                    "time": "19:00:00",
                    "amount": 120000,
                    "account": "Flip",
                    "tx_type": "Transfer",
                    "match_tier": "EXACT",
                    "reconciliation_status": "RECONCILED",
                    "status": "Ready",
                },
            },
        ]

        ack_sent_bodies = []

        def mock_urlopen_cand(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps({"results": candidate_items}).encode("utf-8")
            elif "/api/sync/ack" in url:
                data = request.data.decode("utf-8") if hasattr(request, "data") and request.data else "{}"
                ack_sent_bodies.append(json.loads(data))
                resp.read.return_value = b'{"status": "ok"}'
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_cand):
            res1 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="test_secret",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )

        self.assertEqual(res1["staged_count"], 3)
        self.assertEqual(res1["acknowledged_count"], 3)

        # Verify 3 rows with correct payload.date, payload.account, payload.tx_type
        con = sqlite3.connect(str(self.db_path))
        cand_rows = con.execute(
            "SELECT message_id, json_extract(payload, '$.date'), json_extract(payload, '$.account'), "
            "json_extract(payload, '$.tx_type'), json_extract(payload, '$.amount'), "
            "json_extract(payload, '$.raw_event_id'), json_extract(payload, '$.cursor') "
            "FROM edge_synced_messages WHERE message_id LIKE 'cand_msg_%' ORDER BY id ASC"
        ).fetchall()
        con.close()

        self.assertEqual(len(cand_rows), 3)
        self.assertEqual(cand_rows[0], ("cand_msg_101", "2026-09-20", "BCA Main", "Expense", 50000, 101, "101"))
        self.assertEqual(cand_rows[1], ("cand_msg_102", "2026-09-21", "Jago Main", "Income", 75000, 102, "102"))
        self.assertEqual(cand_rows[2], ("cand_msg_103", "2026-09-22", "Flip", "Transfer", 120000, 103, "103"))

        # Verify ACK payload sent correct ids and cursor
        self.assertTrue(len(ack_sent_bodies) >= 1)
        last_ack = ack_sent_bodies[-1]
        self.assertEqual(last_ack["acknowledged_ids"], ["cand_msg_101", "cand_msg_102", "cand_msg_103"])
        self.assertEqual(last_ack["cursor"], "103")

        # 2. 3 synthetic legacy items without candidate
        legacy_items = [
            {"id": 201, "cursor": "201", "message_id": "legacy_msg_201", "raw_field": "val1"},
            {"id": 202, "cursor": "202", "message_id": "legacy_msg_202", "raw_field": "val2"},
            {"id": 203, "cursor": "203", "message_id": "legacy_msg_203", "raw_field": "val3"},
        ]

        def mock_urlopen_legacy(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps({"results": legacy_items}).encode("utf-8")
            elif "/api/sync/ack" in url:
                resp.read.return_value = b'{"status": "ok"}'
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_legacy):
            res2 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="test_secret",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )

        self.assertEqual(res2["staged_count"], 3)

        con = sqlite3.connect(str(self.db_path))
        legacy_rows = con.execute(
            "SELECT message_id, payload FROM edge_synced_messages WHERE message_id LIKE 'legacy_msg_%' ORDER BY id ASC"
        ).fetchall()
        con.close()

        self.assertEqual(len(legacy_rows), 3)
        for i, (m_id, p_str) in enumerate(legacy_rows):
            p_dict = json.loads(p_str)
            self.assertEqual(p_dict, legacy_items[i])

        # 3. Repeated sync does not duplicate
        with patch("urllib.request.urlopen", side_effect=mock_urlopen_cand):
            res3 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="test_secret",
                db_path=self.db_path,
                staging_dir=self.staging_dir,
            )
        self.assertEqual(res3["staged_count"], 0)

        con = sqlite3.connect(str(self.db_path))
        total_rows = con.execute("SELECT COUNT(*) FROM edge_synced_messages").fetchone()[0]
        # Check cursor advanced to 203
        cursor_row = con.execute(
            "SELECT MAX(CAST(COALESCE(json_extract(payload, '$.cursor'), json_extract(payload, '$.id')) AS INTEGER)) "
            "FROM edge_synced_messages"
        ).fetchone()
        con.close()

        self.assertEqual(total_rows, 6)
        self.assertEqual(cursor_row[0], 203)

    # =========================================================================
    # Section 7: Detailed Test Requirements
    # =========================================================================

    def test_01_payload_uses_candidate_dict_when_present(self) -> None:
        """1. Stored payload uses candidate sub-object as base when present."""
        item = {
            "id": 1,
            "cursor": "1",
            "message_id": "test_01",
            "candidate": {
                "raw_event_id": 1,
                "date": "2026-09-20",
                "time": "12:00:00",
                "amount": 50000,
                "account": "BCA Main",
                "tx_type": "Expense",
            },
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        payload_str = con.execute("SELECT payload FROM edge_synced_messages WHERE message_id = 'test_01'").fetchone()[0]
        con.close()

        stored = json.loads(payload_str)
        self.assertEqual(stored["raw_event_id"], 1)
        self.assertEqual(stored["date"], "2026-09-20")
        self.assertEqual(stored["account"], "BCA Main")
        self.assertEqual(stored["tx_type"], "Expense")

    def test_02_payload_merges_root_metadata_without_overwrite(self) -> None:
        """2. Merges root metadata (message_id, cursor, cursor_id, occurred_at, sender, subject) without overwriting candidate fields."""
        item = {
            "id": 2,
            "cursor": "2",
            "message_id": "test_02",
            "occurred_at": "2026-09-20T10:00:00.000Z",
            "sender": "bca@bca.co.id",
            "subject": "Root Subject",
            "candidate": {
                "raw_event_id": 2,
                "date": "2026-09-20",
                "time": "12:00:00",
                "amount": 25000,
                "account": "BCA Main",
                "tx_type": "Expense",
                "subject": "Candidate Protected Subject",
            },
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        payload_str = con.execute("SELECT payload FROM edge_synced_messages WHERE message_id = 'test_02'").fetchone()[0]
        con.close()

        stored = json.loads(payload_str)
        self.assertEqual(stored["message_id"], "test_02")
        self.assertEqual(stored["cursor"], "2")
        self.assertEqual(stored["cursor_id"], 2)
        self.assertEqual(stored["occurred_at"], "2026-09-20T10:00:00.000Z")
        self.assertEqual(stored["sender"], "bca@bca.co.id")
        # Candidate's existing field must not be overwritten
        self.assertEqual(stored["subject"], "Candidate Protected Subject")
        self.assertEqual(stored["raw_event_id"], 2)

    def test_03_payload_uses_item_when_candidate_absent(self) -> None:
        """3. When candidate is absent, payload uses item as-is (legacy compatibility)."""
        item = {
            "id": 3,
            "cursor": "3",
            "message_id": "test_03",
            "from": "ebanking@bca.co.id",
            "subject": "Legacy Email",
            "body": "Raw email body",
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        payload_str = con.execute("SELECT payload FROM edge_synced_messages WHERE message_id = 'test_03'").fetchone()[0]
        con.close()

        stored = json.loads(payload_str)
        self.assertEqual(stored, item)

    def test_04_payload_uses_item_when_candidate_malformed(self) -> None:
        """4. When candidate is malformed (not a dict, e.g. str, list, int), payload uses item."""
        items = [
            {"id": 41, "cursor": "41", "message_id": "test_04_str", "candidate": "not a dict"},
            {"id": 42, "cursor": "42", "message_id": "test_04_list", "candidate": [1, 2, 3]},
            {"id": 43, "cursor": "43", "message_id": "test_04_int", "candidate": 12345},
        ]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps(items).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        rows = con.execute("SELECT message_id, payload FROM edge_synced_messages WHERE message_id LIKE 'test_04_%' ORDER BY id ASC").fetchall()
        con.close()

        self.assertEqual(len(rows), 3)
        for i, (m_id, p_str) in enumerate(rows):
            self.assertEqual(json.loads(p_str), items[i])

    def test_05_content_hash_matches_payload_str(self) -> None:
        """5. content_hash column matches sha256 of the stored payload_str."""
        item = {
            "id": 5,
            "cursor": "5",
            "message_id": "test_05",
            "candidate": {"raw_event_id": 5, "date": "2026-09-20", "amount": 10000, "account": "BCA Main", "tx_type": "Expense"},
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        row = con.execute("SELECT payload, content_hash FROM edge_synced_messages WHERE message_id = 'test_05'").fetchone()
        con.close()

        expected_hash = hashlib.sha256(row[0].encode("utf-8")).hexdigest()
        self.assertEqual(row[1], expected_hash)

    def test_06_idempotency_holds(self) -> None:
        """6. Repeated pulls of the same item do not create duplicate rows."""
        item = {
            "id": 6,
            "cursor": "6",
            "message_id": "test_06",
            "candidate": {"raw_event_id": 6, "date": "2026-09-20", "amount": 20000, "account": "BCA Main", "tx_type": "Expense"},
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res1 = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)
            res2 = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res1["staged_count"], 1)
        self.assertEqual(res2["staged_count"], 0)

        con = sqlite3.connect(str(self.db_path))
        cnt = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE message_id = 'test_06'").fetchone()[0]
        con.close()
        self.assertEqual(cnt, 1)

    def test_07_ack_payload_correct(self) -> None:
        """7. ACK payload correctly reports acknowledged_ids and advanced cursor."""
        items = [
            {"id": 71, "cursor": "71", "message_id": "test_07_1", "candidate": {"raw_event_id": 71, "date": "2026-09-20", "amount": 10000, "account": "BCA Main", "tx_type": "Expense"}},
            {"id": 72, "cursor": "72", "message_id": "test_07_2", "candidate": {"raw_event_id": 72, "date": "2026-09-20", "amount": 20000, "account": "BCA Main", "tx_type": "Expense"}},
        ]
        sent_ack = []

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(items).encode("utf-8")
            elif "/api/sync/ack" in url:
                body = request.data.decode("utf-8") if hasattr(request, "data") and request.data else "{}"
                sent_ack.append(json.loads(body))
                resp.read.return_value = b'{"status": "ok"}'
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        self.assertEqual(res["acknowledged_count"], 2)
        self.assertEqual(len(sent_ack), 1)
        self.assertEqual(sent_ack[0]["acknowledged_ids"], ["test_07_1", "test_07_2"])
        self.assertEqual(sent_ack[0]["cursor"], "72")
        self.assertEqual(sent_ack[0]["source"], "gmail")

    def test_08_cursor_advances(self) -> None:
        """8. Stored cursor in SQLite advances and is used in subsequent GET request query parameter."""
        items1 = [{"id": 81, "cursor": "81", "message_id": "test_08_1", "candidate": {"raw_event_id": 81, "date": "2026-09-20", "amount": 10000, "account": "BCA Main", "tx_type": "Expense"}}]
        requested_urls = []

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            requested_urls.append(url)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(items1).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        # Second sync should include cursor=81
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        second_get_urls = [u for u in requested_urls if "/api/sync/gmail" in u]
        self.assertTrue(len(second_get_urls) >= 2)
        self.assertIn("cursor=81", second_get_urls[1])

    def test_09_downstream_validate_edge_candidate_sees_fields(self) -> None:
        """9. Downstream validate_edge_candidate now sees date, account, tx_type directly on stored payload."""
        item = {
            "id": 91,
            "cursor": "91",
            "message_id": "test_09",
            "candidate": {
                "raw_event_id": 91,
                "date": "2026-09-20",
                "time": "14:30:00",
                "amount": 75000,
                "account": "BCA Main",
                "tx_type": "Expense",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
                "status": "Ready",
            },
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        payload_str = con.execute("SELECT payload FROM edge_synced_messages WHERE message_id = 'test_09'").fetchone()[0]
        con.close()

        stored_payload = json.loads(payload_str)
        is_valid, reason, cand = validate_edge_candidate(stored_payload)
        self.assertTrue(is_valid, f"Validation failed with reason: {reason}")
        self.assertEqual(cand["date"], "2026-09-20")
        self.assertEqual(cand["account"], "BCA Main")
        self.assertEqual(cand["tx_type"], "Expense")

    def test_10_downstream_sees_amount_as_number(self) -> None:
        """10. Downstream sees amount as valid numeric, convertible to Decimal."""
        item = {
            "id": 1001,
            "cursor": "1001",
            "message_id": "test_10",
            "candidate": {
                "raw_event_id": 1001,
                "date": "2026-09-20",
                "time": "15:00:00",
                "amount": 150000,
                "account": "BCA Main",
                "tx_type": "Expense",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
                "status": "Ready",
            },
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret="test_secret", db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        payload_str = con.execute("SELECT payload FROM edge_synced_messages WHERE message_id = 'test_10'").fetchone()[0]
        con.close()

        stored_payload = json.loads(payload_str)
        amount_val = stored_payload.get("amount")
        self.assertIsInstance(amount_val, (int, float))
        dec = Decimal(str(amount_val))
        self.assertEqual(dec, Decimal("150000"))

    def test_11_no_secret_leaks(self) -> None:
        """11. Secret tokens do not leak into stored payload or database records."""
        secret_token = "synthetic_secret_xyz123"
        item = {
            "id": 1101,
            "cursor": "1101",
            "message_id": "test_11",
            "candidate": {
                "raw_event_id": 1101,
                "date": "2026-09-20",
                "time": "16:00:00",
                "amount": 35000,
                "account": "BCA Main",
                "tx_type": "Expense",
            },
        }

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            resp.read.return_value = json.dumps([item]).encode("utf-8")
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            sync_edge_inbox(worker_url="https://worker.internal", secret=secret_token, db_path=self.db_path)

        con = sqlite3.connect(str(self.db_path))
        all_text = con.execute("SELECT message_id, source, sender, subject, payload, content_hash FROM edge_synced_messages").fetchall()
        con.close()

        for row in all_text:
            for field in row:
                self.assertNotIn(secret_token, str(field))

    def test_12_production_db_unchanged(self) -> None:
        """12. Production DB hash matches expected baseline."""
        self.assertEqual(_get_prod_db_hash(), EXPECTED_PRODUCTION_DB_SHA256)


if __name__ == "__main__":
    unittest.main()
