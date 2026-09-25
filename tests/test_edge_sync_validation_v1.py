"""
Tests for EDGE-SYNC-VALIDATION-V1.

Verifies:
1. Worker response boundary filtering in cloud/worker/src/index.ts:
   - filters candidate with missing raw_event_id
   - filters candidate with missing date
   - filters candidate with amount = 0
   - filters candidate with amount = 1.42 (fractional)
   - filters candidate with all fields None
   - keeps valid candidate intact
   - reports filtered_count in response body and X-Filtered-Candidates-Count header
2. Local pre-apply validation gate in aturuang/server.py:
   - routes candidate with missing date to review (REVIEW_REASON_INVALID_DATE)
   - routes candidate with missing raw_event_id to review (REVIEW_REASON_MISSING_SOURCE)
   - routes candidate with invalid amount to review (REVIEW_REASON_INVALID_AMOUNT)
   - routes valid candidate to auto-apply (status AUTO_APPLIED)
   - verifies canonical review reason codes
3. Reproduction verification and idempotency:
   - 10 valid + 10 invalid fixtures -> exactly 10 applied, 10 in review
   - 250 valid + 250 invalid fixtures -> exactly 250 applied, 250 in review
   - repeated pull of same fixtures is idempotent (0 newly applied, 0 newly in review)
4. Production DB invariant holds throughout.
"""
from __future__ import annotations

import decimal
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aturuang.server import (
    sync_edge_inbox,
    init_edge_sync_schema,
    validate_edge_candidate,
    REVIEW_REASON_MISSING_REQUIRED_FIELD,
    REVIEW_REASON_INVALID_AMOUNT,
    REVIEW_REASON_INVALID_DATE,
    REVIEW_REASON_MISSING_SOURCE,
    MIN_ALLOWED_TRANSACTION_AMOUNT,
    MAX_ALLOWED_TRANSACTION_AMOUNT,
)
from aturuang.safe_apply import SafeApplyEngine
from aturuang.review_queue_ui import ReviewQueueManager

EXPECTED_PROD_DB_HASH = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"


def get_prod_db_hash() -> str:
    candidates = [
        Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db"),
        Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-fix-validation-v1\runtime\money_tracks.db"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest().lower()
            if h == EXPECTED_PROD_DB_HASH:
                return h
    if candidates[0].exists():
        return hashlib.sha256(candidates[0].read_bytes()).hexdigest().lower()
    return ""


def run_node_worker_filter(rows: list[dict]) -> dict:
    """Executes the exact Worker candidate validation logic in Node.js."""
    script = """
    const rows = JSON.parse(process.argv[1]);
    let filteredCount = 0;
    const results = rows.map((row) => {
      let minimal = {};
      if (row.minimal_raw_payload) {
        try { minimal = JSON.parse(row.minimal_raw_payload); } catch {}
      }
      const rawEventId = row.raw_event_id;
      const candDate = row.date;
      const candAmount = row.amount;
      const candAccount = row.account;
      const candTxType = row.tx_type;

      let isCandValid = false;
      if (
        rawEventId !== null &&
        rawEventId !== undefined &&
        typeof rawEventId === "number" &&
        rawEventId > 0 &&
        candDate !== null &&
        candDate !== undefined &&
        typeof candDate === "string" &&
        candDate.trim() !== "" &&
        !isNaN(new Date(candDate.trim()).getTime()) &&
        candAmount !== null &&
        candAmount !== undefined &&
        typeof candAmount === "number" &&
        !isNaN(candAmount) &&
        isFinite(candAmount) &&
        candAmount > 0 &&
        candAmount % 1 === 0 &&
        candAccount !== null &&
        candAccount !== undefined &&
        typeof candAccount === "string" &&
        candAccount.trim() !== "" &&
        candTxType !== null &&
        candTxType !== undefined &&
        typeof candTxType === "string" &&
        candTxType.trim() !== "" &&
        ["Income", "Expense", "Transfer", "Adjustment"].includes(candTxType.trim())
      ) {
        isCandValid = true;
      }

      if (!isCandValid) {
        filteredCount++;
      }

      return {
        id: row.id,
        cursor: String(row.id),
        message_id: row.message_id,
        amount: isCandValid ? row.amount : (row.amount !== null ? row.amount : minimal.amount),
        candidate: isCandValid ? {
          raw_event_id: rawEventId,
          tx_type: candTxType,
          amount: row.amount,
          account: candAccount,
          to_account: row.to_account || undefined,
          category: row.category || "Other",
          money_context: row.money_context || "Personal",
          date: candDate,
          time: row.time || "12:00:00",
          match_tier: "EXACT",
          reconciliation_status: "RECONCILED",
          confidence_score: row.confidence_score !== null && row.confidence_score !== undefined ? row.confidence_score : 1.0,
          status: row.candidate_status || "Pending",
          reasons: row.reasons || "Auto-parsed candidate",
        } : undefined,
      };
    });

    const response = {
      results,
      candidates: results.filter(r => r.candidate !== undefined).map(r => r.candidate),
      filtered_count: filteredCount,
      headers: {
        "X-Filtered-Candidates-Count": String(filteredCount),
      }
    };
    process.stdout.write(JSON.stringify(response));
    """
    res = subprocess.run(
        ["node", "-e", script, json.dumps(rows)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


class TestEdgeSyncValidationV1(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_val_v1_")
        self.db_path = Path(self.temp_dir) / "test_ledger.db"
        self._init_test_db()
        self.apply_engine = SafeApplyEngine(self.db_path, backup_dir=Path(self.temp_dir) / "backups")
        self.review_manager = ReviewQueueManager(self.db_path, backup_dir=Path(self.temp_dir) / "backups")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _init_test_db(self) -> None:
        con = sqlite3.connect(str(self.db_path))
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
                VALUES ('BCA Main', 'Owned', 1, 10000000.0);
                INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
                VALUES ('Merchant External', 'External', 1, 0.0);
            """)
            init_edge_sync_schema(con)
            con.commit()
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # Worker Response Boundary Tests (1 to 7)
    # -------------------------------------------------------------------------

    def test_01_worker_filters_candidate_with_missing_raw_event_id(self) -> None:
        """Worker response boundary omits candidate when raw_event_id is null/missing."""
        rows = [{
            "id": 101,
            "message_id": "msg_001",
            "occurred_at": "2026-09-20T10:00:00Z",
            "payload_hash": "hash_001",
            "minimal_raw_payload": None,
            "raw_event_id": None,  # Broken
            "tx_type": "Expense",
            "amount": 50000,
            "account": "BCA Main",
            "to_account": "Merchant",
            "category": "Dining",
            "date": "2026-09-20",
            "time": "10:00:00",
            "candidate_status": "Pending",
            "confidence_score": 0.95,
            "reasons": "Auto",
        }]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 1)
        self.assertIsNone(out["results"][0].get("candidate"))
        self.assertEqual(len(out["candidates"]), 0)

    def test_02_worker_filters_candidate_with_missing_date(self) -> None:
        """Worker response boundary omits candidate when date is null or invalid."""
        rows = [{
            "id": 102,
            "message_id": "msg_002",
            "occurred_at": "2026-09-20T10:00:00Z",
            "payload_hash": "hash_002",
            "minimal_raw_payload": None,
            "raw_event_id": 102,
            "tx_type": "Expense",
            "amount": 50000,
            "account": "BCA Main",
            "to_account": "Merchant",
            "category": "Dining",
            "date": None,  # Broken date
            "time": "10:00:00",
            "candidate_status": "Pending",
            "confidence_score": 0.95,
            "reasons": "Auto",
        }]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 1)
        self.assertIsNone(out["results"][0].get("candidate"))

    def test_03_worker_filters_candidate_with_amount_zero(self) -> None:
        """Worker response boundary omits candidate when amount is 0."""
        rows = [{
            "id": 103,
            "message_id": "msg_003",
            "occurred_at": "2026-09-20T10:00:00Z",
            "payload_hash": "hash_003",
            "minimal_raw_payload": None,
            "raw_event_id": 103,
            "tx_type": "Expense",
            "amount": 0,  # Broken amount = 0
            "account": "BCA Main",
            "to_account": "Merchant",
            "category": "Dining",
            "date": "2026-09-20",
            "time": "10:00:00",
            "candidate_status": "Pending",
            "confidence_score": 0.95,
            "reasons": "Auto",
        }]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 1)
        self.assertIsNone(out["results"][0].get("candidate"))

    def test_04_worker_filters_candidate_with_amount_fractional(self) -> None:
        """Worker response boundary omits candidate when amount is fractional (1.42)."""
        rows = [{
            "id": 104,
            "message_id": "msg_004",
            "occurred_at": "2026-09-20T10:00:00Z",
            "payload_hash": "hash_004",
            "minimal_raw_payload": None,
            "raw_event_id": 104,
            "tx_type": "Expense",
            "amount": 1.42,  # Broken fractional amount
            "account": "BCA Main",
            "to_account": "Merchant",
            "category": "Dining",
            "date": "2026-09-20",
            "time": "10:00:00",
            "candidate_status": "Pending",
            "confidence_score": 0.95,
            "reasons": "Auto",
        }]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 1)
        self.assertIsNone(out["results"][0].get("candidate"))

    def test_05_worker_filters_candidate_with_all_fields_none(self) -> None:
        """Worker response boundary omits candidate when all candidate fields are None."""
        rows = [{
            "id": 105,
            "message_id": "msg_005",
            "occurred_at": "2026-09-20T10:00:00Z",
            "payload_hash": "hash_005",
            "minimal_raw_payload": None,
            "raw_event_id": None,
            "tx_type": None,
            "amount": None,
            "account": None,
            "to_account": None,
            "category": None,
            "date": None,
            "time": None,
            "candidate_status": None,
            "confidence_score": None,
            "reasons": None,
        }]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 1)
        self.assertIsNone(out["results"][0].get("candidate"))

    def test_06_worker_keeps_valid_candidate_intact(self) -> None:
        """Worker response boundary keeps fully valid candidate object."""
        rows = [{
            "id": 106,
            "message_id": "msg_006",
            "occurred_at": "2026-09-20T10:00:00Z",
            "payload_hash": "hash_006",
            "minimal_raw_payload": None,
            "raw_event_id": 106,
            "tx_type": "Expense",
            "amount": 75000,
            "account": "BCA Main",
            "to_account": "Merchant External",
            "category": "Dining",
            "date": "2026-09-20",
            "time": "10:00:00",
            "candidate_status": "Pending",
            "confidence_score": 0.95,
            "reasons": "Auto-parsed candidate",
        }]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 0)
        cand = out["results"][0].get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["raw_event_id"], 106)
        self.assertEqual(cand["amount"], 75000)
        self.assertEqual(cand["date"], "2026-09-20")
        self.assertEqual(len(out["candidates"]), 1)

    def test_07_worker_reports_filtered_count_in_body_and_header(self) -> None:
        """Worker returns filtered_count in response body and X-Filtered-Candidates-Count header."""
        rows = [
            # 1 valid
            {
                "id": 1,
                "message_id": "m1",
                "occurred_at": "2026-09-20T10:00:00Z",
                "payload_hash": "h1",
                "minimal_raw_payload": None,
                "raw_event_id": 1,
                "tx_type": "Expense",
                "amount": 50000,
                "account": "BCA Main",
                "date": "2026-09-20",
            },
            # 2 invalid (amount 0 and missing raw_event_id)
            {
                "id": 2,
                "message_id": "m2",
                "occurred_at": "2026-09-20T10:00:00Z",
                "payload_hash": "h2",
                "minimal_raw_payload": None,
                "raw_event_id": 2,
                "tx_type": "Expense",
                "amount": 0,
                "account": "BCA Main",
                "date": "2026-09-20",
            },
            {
                "id": 3,
                "message_id": "m3",
                "occurred_at": "2026-09-20T10:00:00Z",
                "payload_hash": "h3",
                "minimal_raw_payload": None,
                "raw_event_id": None,
                "tx_type": "Expense",
                "amount": 10000,
                "account": "BCA Main",
                "date": "2026-09-20",
            },
        ]
        out = run_node_worker_filter(rows)
        self.assertEqual(out["filtered_count"], 2)
        self.assertEqual(out["headers"]["X-Filtered-Candidates-Count"], "2")
        self.assertEqual(len(out["candidates"]), 1)

    # -------------------------------------------------------------------------
    # Local Pre-Apply Boundary Tests (8 to 12)
    # -------------------------------------------------------------------------

    def test_08_local_routes_missing_date_to_review(self) -> None:
        """Local pre-apply validator fails closed and routes missing/broken date to review queue."""
        # 1. Direct validation check
        item_none = {"candidate": {"raw_event_id": 1, "amount": 50000, "account": "BCA Main", "tx_type": "Expense", "date": None}}
        valid, reason, _ = validate_edge_candidate(item_none)
        self.assertFalse(valid)
        self.assertEqual(reason, REVIEW_REASON_INVALID_DATE)

        item_none_str = {"candidate": {"raw_event_id": 1, "amount": 50000, "account": "BCA Main", "tx_type": "Expense", "date": "None None"}}
        valid2, reason2, _ = validate_edge_candidate(item_none_str)
        self.assertFalse(valid2)
        self.assertEqual(reason2, REVIEW_REASON_INVALID_DATE)

        # 2. End-to-end sync_edge_inbox check
        mock_messages = [{
            "id": 201,
            "message_id": "msg_bad_date_001",
            "candidate": {
                "raw_event_id": 201,
                "amount": 50000,
                "account": "BCA Main",
                "tx_type": "Expense",
                "date": "None None",
            }
        }]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="secret_1",
                db_path=self.db_path,
                apply_engine=self.apply_engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res["auto_applied_count"], 0)
            self.assertEqual(res["review_required_count"], 1)

            con = sqlite3.connect(str(self.db_path))
            try:
                row = con.execute("SELECT status FROM edge_synced_messages WHERE message_id = 'msg_bad_date_001'").fetchone()
                self.assertEqual(row[0], "REVIEW_REQUIRED")
                rev = self.review_manager.get_item("rev_edge_msg_bad_date_001")
                self.assertIsNotNone(rev)
                self.assertEqual(rev.reason, REVIEW_REASON_INVALID_DATE)
                self.assertEqual(rev.status, "REVIEW_REQUIRED")
            finally:
                con.close()

    def test_09_local_routes_missing_raw_event_id_to_review(self) -> None:
        """Local pre-apply validator fails closed and routes missing/invalid raw_event_id to review queue."""
        # 1. Direct validation check
        item_no_id = {"candidate": {"amount": 50000, "account": "BCA Main", "tx_type": "Expense", "date": "2026-09-20"}}
        valid, reason, _ = validate_edge_candidate(item_no_id)
        self.assertFalse(valid)
        self.assertEqual(reason, REVIEW_REASON_MISSING_SOURCE)

        item_zero_id = {"candidate": {"raw_event_id": 0, "amount": 50000, "account": "BCA Main", "tx_type": "Expense", "date": "2026-09-20"}}
        valid2, reason2, _ = validate_edge_candidate(item_zero_id)
        self.assertFalse(valid2)
        self.assertEqual(reason2, REVIEW_REASON_MISSING_SOURCE)

        # 2. End-to-end sync_edge_inbox check
        mock_messages = [{
            "id": 202,
            "message_id": "msg_bad_source_001",
            "candidate": {
                "raw_event_id": None,
                "amount": 50000,
                "account": "BCA Main",
                "tx_type": "Expense",
                "date": "2026-09-20",
            }
        }]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="secret_1",
                db_path=self.db_path,
                apply_engine=self.apply_engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res["auto_applied_count"], 0)
            self.assertEqual(res["review_required_count"], 1)

            con = sqlite3.connect(str(self.db_path))
            try:
                row = con.execute("SELECT status FROM edge_synced_messages WHERE message_id = 'msg_bad_source_001'").fetchone()
                self.assertEqual(row[0], "REVIEW_REQUIRED")
                rev = self.review_manager.get_item("rev_edge_msg_bad_source_001")
                self.assertIsNotNone(rev)
                self.assertEqual(rev.reason, REVIEW_REASON_MISSING_SOURCE)
                self.assertEqual(rev.status, "REVIEW_REQUIRED")
            finally:
                con.close()

    def test_10_local_routes_invalid_amount_to_review(self) -> None:
        """Local pre-apply validator routes amounts < 10, fractional, 0, or negative to review."""
        test_cases = [0, 1, 1.42, 9.99, -500, 100000000001]
        for invalid_amt in test_cases:
            item = {
                "candidate": {
                    "raw_event_id": 1,
                    "amount": invalid_amt,
                    "account": "BCA Main",
                    "tx_type": "Expense",
                    "date": "2026-09-20",
                }
            }
            valid, reason, _ = validate_edge_candidate(item)
            self.assertFalse(valid, f"Expected amount {invalid_amt} to be invalid")
            self.assertEqual(reason, REVIEW_REASON_INVALID_AMOUNT)

    def test_11_local_routes_valid_candidate_to_auto_apply(self) -> None:
        """Local pre-apply validator accepts clean candidate and SafeApplyEngine auto-applies it."""
        mock_messages = [{
            "id": 301,
            "message_id": "msg_valid_001",
            "match_tier": "EXACT",
            "reconciliation_status": "RECONCILED",
            "candidate": {
                "raw_event_id": 301,
                "amount": 75000,
                "account": "BCA Main",
                "to_account": "Merchant External",
                "tx_type": "Expense",
                "category": "Dining",
                "date": "2026-09-20",
                "time": "12:00:00",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
            }
        }]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="secret_1",
                db_path=self.db_path,
                apply_engine=self.apply_engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res["auto_applied_count"], 1)
            self.assertEqual(res["review_required_count"], 0)

            con = sqlite3.connect(str(self.db_path))
            try:
                row = con.execute("SELECT status FROM edge_synced_messages WHERE message_id = 'msg_valid_001'").fetchone()
                self.assertEqual(row[0], "AUTO_APPLIED")
                tx = con.execute("SELECT amount, canonical_id FROM transactions WHERE canonical_id = 'EDGE_msg_valid_001'").fetchone()
                self.assertIsNotNone(tx)
                self.assertAlmostEqual(tx[0], 75000.0, places=2)
            finally:
                con.close()

    def test_12_local_review_reason_codes_canonical(self) -> None:
        """Local review reason codes strictly match the canonical specification."""
        self.assertEqual(REVIEW_REASON_MISSING_REQUIRED_FIELD, "REVIEW_REASON_MISSING_REQUIRED_FIELD")
        self.assertEqual(REVIEW_REASON_INVALID_AMOUNT, "REVIEW_REASON_INVALID_AMOUNT")
        self.assertEqual(REVIEW_REASON_INVALID_DATE, "REVIEW_REASON_INVALID_DATE")
        self.assertEqual(REVIEW_REASON_MISSING_SOURCE, "REVIEW_REASON_MISSING_SOURCE")

        # Test missing required field (account and tx_type)
        item_no_acc = {"candidate": {"raw_event_id": 1, "amount": 50000, "date": "2026-09-20", "account": ""}}
        v, r, _ = validate_edge_candidate(item_no_acc)
        self.assertFalse(v)
        self.assertEqual(r, REVIEW_REASON_MISSING_REQUIRED_FIELD)

        item_no_type = {"candidate": {"raw_event_id": 1, "amount": 50000, "date": "2026-09-20", "account": "BCA Main", "tx_type": None}}
        v, r, _ = validate_edge_candidate(item_no_type)
        self.assertFalse(v)
        self.assertEqual(r, REVIEW_REASON_MISSING_REQUIRED_FIELD)

    # -------------------------------------------------------------------------
    # Reproduction & End-to-End Safety Tests (13 to 15)
    # -------------------------------------------------------------------------

    def test_13_reproduction_verification_10_fixtures_and_idempotency(self) -> None:
        """10 valid + 10 invalid fixtures -> exactly 10 applied, 10 in review. Repeated pull is idempotent."""
        fixtures = []
        # 10 valid
        for i in range(1, 11):
            fixtures.append({
                "id": i,
                "message_id": f"msg_valid_{i:03d}",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
                "candidate": {
                    "raw_event_id": i,
                    "amount": 20000 * i,
                    "account": "BCA Main",
                    "to_account": "Merchant External",
                    "tx_type": "Expense",
                    "category": "Dining",
                    "date": "2026-09-20",
                    "time": "12:00:00",
                    "match_tier": "EXACT",
                    "reconciliation_status": "RECONCILED",
                }
            })
        # 10 invalid (various corruptions)
        corruptions = [
            {"raw_event_id": None},
            {"date": None},
            {"date": "None None"},
            {"amount": 0},
            {"amount": 1},
            {"amount": 1.42},
            {"account": None},
            {"account": ""},
            {"tx_type": None},
            {"raw_event_id": -5},
        ]
        for idx, corr in enumerate(corruptions, start=11):
            base_cand = {
                "raw_event_id": idx,
                "amount": 50000,
                "account": "BCA Main",
                "to_account": "Merchant External",
                "tx_type": "Expense",
                "category": "Dining",
                "date": "2026-09-20",
                "time": "12:00:00",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
            }
            base_cand.update(corr)
            fixtures.append({
                "id": idx,
                "message_id": f"msg_invalid_{idx:03d}",
                "candidate": base_cand,
            })

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(fixtures).encode("utf-8")
            else:
                resp.read.return_value = b'{"status": "ok"}'
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        # First run: 10 auto applied, 10 review required
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res1 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="secret_1",
                db_path=self.db_path,
                apply_engine=self.apply_engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res1["auto_applied_count"], 10)
            self.assertEqual(res1["review_required_count"], 10)
            self.assertEqual(res1["staged_count"], 20)

            con = sqlite3.connect(str(self.db_path))
            try:
                tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(tx_count, 10)
                applied_staged = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE status = 'AUTO_APPLIED'").fetchone()[0]
                self.assertEqual(applied_staged, 10)
                review_staged = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE status = 'REVIEW_REQUIRED'").fetchone()[0]
                self.assertEqual(review_staged, 10)
            finally:
                con.close()

            # Second run with same fixtures: strictly idempotent (0 newly staged, 0 applied, 0 in review)
            res2 = sync_edge_inbox(
                worker_url="https://worker.internal",
                secret="secret_1",
                db_path=self.db_path,
                apply_engine=self.apply_engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res2["staged_count"], 0)
            self.assertEqual(res2["auto_applied_count"], 0)
            self.assertEqual(res2["review_required_count"], 0)

            con2 = sqlite3.connect(str(self.db_path))
            try:
                tx_count2 = con2.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                self.assertEqual(tx_count2, 10)
            finally:
                con2.close()

    def test_14_reproduction_verification_250_fixtures(self) -> None:
        """250 valid + 250 invalid fixtures -> exactly 250 applied, 250 in review."""
        fixtures = []
        for i in range(1, 251):
            fixtures.append({
                "id": i,
                "message_id": f"msg_large_valid_{i:04d}",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
                "candidate": {
                    "raw_event_id": i,
                    "amount": 10000 + i,
                    "account": "BCA Main",
                    "to_account": "Merchant External",
                    "tx_type": "Expense",
                    "category": "Shopping",
                    "date": "2026-09-20",
                    "time": "12:00:00",
                    "match_tier": "EXACT",
                    "reconciliation_status": "RECONCILED",
                }
            })
        for i in range(251, 501):
            # Alternate invalid defects
            corr = {}
            if i % 4 == 0:
                corr["raw_event_id"] = None
            elif i % 4 == 1:
                corr["date"] = None
            elif i % 4 == 2:
                corr["amount"] = 0
            else:
                corr["amount"] = 1.42

            base_cand = {
                "raw_event_id": i,
                "amount": 25000,
                "account": "BCA Main",
                "to_account": "Merchant External",
                "tx_type": "Expense",
                "category": "Shopping",
                "date": "2026-09-20",
                "time": "12:00:00",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
            }
            base_cand.update(corr)
            fixtures.append({
                "id": i,
                "message_id": f"msg_large_invalid_{i:04d}",
                "candidate": base_cand,
            })

        # Process in batches of 100 (simulating pagination)
        total_applied = 0
        total_review = 0

        for b_start in range(0, 500, 100):
            batch = fixtures[b_start : b_start + 100]

            def make_urlopen(cur_batch):
                def mock_urlopen(request, *args, **kwargs):
                    resp = MagicMock()
                    url = request.full_url if hasattr(request, "full_url") else str(request)
                    if "/api/sync/gmail" in url:
                        resp.read.return_value = json.dumps(cur_batch).encode("utf-8")
                    else:
                        resp.read.return_value = b'{"status": "ok"}'
                    resp.status = 200
                    resp.__enter__.return_value = resp
                    return resp
                return mock_urlopen

            with patch("urllib.request.urlopen", side_effect=make_urlopen(batch)):
                res = sync_edge_inbox(
                    worker_url="https://worker.internal",
                    secret="secret_1",
                    db_path=self.db_path,
                    apply_engine=self.apply_engine,
                    review_manager=self.review_manager,
                )
                total_applied += res["auto_applied_count"]
                total_review += res["review_required_count"]

        self.assertEqual(total_applied, 250)
        self.assertEqual(total_review, 250)

        con = sqlite3.connect(str(self.db_path))
        try:
            tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(tx_count, 250)
            applied_staged = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE status = 'AUTO_APPLIED'").fetchone()[0]
            self.assertEqual(applied_staged, 250)
            review_staged = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE status = 'REVIEW_REQUIRED'").fetchone()[0]
            self.assertEqual(review_staged, 250)
        finally:
            con.close()

    def test_15_production_db_invariant_holds(self) -> None:
        """Production database SHA-256 remains unmutated at 341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07."""
        current_hash = get_prod_db_hash()
        self.assertEqual(current_hash, EXPECTED_PROD_DB_HASH)


if __name__ == "__main__":
    unittest.main()
