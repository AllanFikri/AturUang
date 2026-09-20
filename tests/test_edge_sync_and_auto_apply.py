"""
Focused Test Suite: Cloudflare Edge Inbox Sync & Zero-Click Auto-Apply Engine.

Tests:
1. test_edge_sync_pulls_and_stages_evidence_idempotently
2. test_auto_apply_bypasses_review_for_exact_match
3. test_auto_apply_routes_ambiguous_to_review_queue
4. test_auto_apply_triggers_pre_apply_backup
5. test_production_db_hash_untouched_during_tests
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aturuang.safe_apply import (
    ApplyCandidate,
    CandidateLifecycleState,
    LedgerMutation,
    SafeApplyEngine,
    compute_preview_hash,
)
from aturuang.review_queue_ui import ReviewQueueManager
from aturuang.server import sync_edge_inbox, init_edge_sync_schema

EXPECTED_PRODUCTION_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"


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
            INSERT OR IGNORE INTO accounts (name, kind, active, current_balance)
            VALUES ('Jago Main', 'Owned', 1, 2000000.0);
        """)
        con.commit()
    finally:
        con.close()


class EdgeSyncAndAutoApplyTests(unittest.TestCase):
    """Grand Design V2.1 Section 4.3 & 6 test suite."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "synthetic_test.db"
        self.backup_dir = self.temp_path / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        _init_synthetic_db(self.db_path)
        self.engine = SafeApplyEngine(self.db_path, self.backup_dir)
        self.review_manager = ReviewQueueManager(self.db_path, self.backup_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_candidate(
        self,
        candidate_id: str = "CAND_EDGE_001",
        idempotency_key: str = "IDEM_EDGE_001",
        amount: Decimal = Decimal("250000.00"),
        account_from: str = "BCA Main",
        account_to: str = "Merchant External",
        transaction_type: str = "Expense",
        description: str = "Bank QRIS Payment",
        category: str = "Dining",
    ) -> ApplyCandidate:
        mut = LedgerMutation(
            date="2026-03-20",
            time="11:30:00",
            transaction_type=transaction_type,
            amount=amount,
            account_from=account_from,
            account_to=account_to,
            description=description,
            category=category,
            canonical_id=f"CANON_{candidate_id}",
        )
        preview_hash = compute_preview_hash(
            mutations=[mut],
            participating_evidence_keys=["GMAIL_MSG_101"],
            candidate_id=candidate_id,
        )
        return ApplyCandidate(
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            state=CandidateLifecycleState.PARSED,
            participating_evidence_keys=("GMAIL_MSG_101",),
            mutations=(mut,),
            preview_hash=preview_hash,
        )

    def test_edge_sync_pulls_and_stages_evidence_idempotently(self) -> None:
        """Mock Cloudflare D1 response and assert local staging without duplicate keys."""
        mock_messages = [
            {
                "message_id": "gmail_msg_abc123",
                "from": "ebanking@bca.co.id",
                "subject": "Transaksi Rekening Tabungan",
                "body": "Pembayaran QRIS Rp 75.000 berhasil ke Toko Kopi",
                "internal_date": "1726001234",
            }
        ]

        def mock_urlopen(request, *args, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            headers = request.headers if hasattr(request, "headers") else {}
            headers_lower = {k.lower(): v for k, v in headers.items()}
            self.assertIn("x-timestamp", headers_lower)
            self.assertIn("x-nonce", headers_lower)
            self.assertIn("x-signature", headers_lower)

            resp = MagicMock()
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
                resp.status = 200
            elif "/api/sync/ack" in url:
                ack_body = request.data.decode("utf-8") if hasattr(request, "data") else "{}"
                ack_data = json.loads(ack_body)
                self.assertIn("gmail_msg_abc123", ack_data.get("acknowledged_ids", []))
                resp.read.return_value = json.dumps({"status": "acknowledged"}).encode("utf-8")
                resp.status = 200
            else:
                resp.read.return_value = b"{}"
                resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # First pull: stages the message and acknowledges it
            res1 = sync_edge_inbox(
                worker_url="https://cf-worker.internal",
                secret="test_edge_secret_2026",
                db_path=self.db_path,
                staging_dir=self.temp_path / "staged",
            )
            self.assertEqual(res1["status"], "success")
            self.assertEqual(res1["fetched_count"], 1)
            self.assertEqual(res1["staged_count"], 1)
            self.assertEqual(res1["acknowledged_count"], 1)

            # Verify row in edge_synced_messages
            con = sqlite3.connect(str(self.db_path))
            try:
                rows = con.execute("SELECT message_id, source, sender, status FROM edge_synced_messages").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "gmail_msg_abc123")
                self.assertEqual(rows[0][1], "gmail")
                self.assertEqual(rows[0][2], "ebanking@bca.co.id")
                self.assertEqual(rows[0][3], "STAGED")
            finally:
                con.close()

            # Second pull with identical message: must skip duplicate without error
            res2 = sync_edge_inbox(
                worker_url="https://cf-worker.internal",
                secret="test_edge_secret_2026",
                db_path=self.db_path,
                staging_dir=self.temp_path / "staged",
            )
            self.assertEqual(res2["status"], "success")
            self.assertEqual(res2["fetched_count"], 1)
            self.assertEqual(res2["staged_count"], 0)
            self.assertEqual(res2["acknowledged_count"], 0)

            # Table still has exactly 1 row (no duplicates)
            con2 = sqlite3.connect(str(self.db_path))
            try:
                count = con2.execute("SELECT COUNT(*) FROM edge_synced_messages").fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                con2.close()

    def test_auto_apply_bypasses_review_for_exact_match(self) -> None:
        """Assert high-confidence bank mutation writes to ledger without manual confirmation."""
        cand = self._make_candidate(
            candidate_id="CAND_EXACT_01",
            idempotency_key="IDEM_EXACT_01",
            amount=Decimal("185000.00"),
            description="BCA Notification Transfer Out",
        )

        applied, result, reason = self.engine.auto_apply_if_eligible(
            candidate=cand,
            match_tier="EXACT",
            reconciliation_status="RECONCILED",
            review_manager=self.review_manager,
        )

        self.assertTrue(applied)
        self.assertIsNotNone(result)
        self.assertEqual(reason, "AUTO_APPLIED_HIGH_CONFIDENCE")
        self.assertEqual(cand.state, CandidateLifecycleState.APPLIED)

        # Assert written to transactions table
        con = sqlite3.connect(str(self.db_path))
        try:
            tx = con.execute("SELECT date, amount, account_from, description FROM transactions WHERE canonical_id=?", ("CANON_CAND_EXACT_01",)).fetchone()
            self.assertIsNotNone(tx)
            self.assertEqual(tx[0], "2026-03-20")
            self.assertAlmostEqual(tx[1], 185000.0, places=2)
            self.assertEqual(tx[2], "BCA Main")

            # Assert audit log entry
            audit = con.execute("SELECT action FROM transaction_audit_log WHERE action='AUTO_APPLIED_HIGH_CONFIDENCE'").fetchone()
            self.assertIsNotNone(audit)
        finally:
            con.close()

        # Review queue should have NO pending items for this candidate
        pending = self.review_manager.get_pending_items()
        self.assertEqual(len(pending), 0)

    def test_auto_apply_routes_ambiguous_to_review_queue(self) -> None:
        """Assert ambiguous transfer or discrepant nominal remains in Review Queue."""
        # Case A: Ambiguous Match Tier
        cand_ambig = self._make_candidate(
            candidate_id="CAND_AMBIG_01",
            idempotency_key="IDEM_AMBIG_01",
            amount=Decimal("500000.00"),
            description="Unidentified Transfer",
        )
        applied_a, result_a, reason_a = self.engine.auto_apply_if_eligible(
            candidate=cand_ambig,
            match_tier="AMBIGUOUS",
            reconciliation_status="RECONCILED",
            review_manager=self.review_manager,
        )
        self.assertFalse(applied_a)
        self.assertIsNone(result_a)
        self.assertEqual(reason_a, "REVIEW_REQUIRED")
        self.assertEqual(cand_ambig.state, CandidateLifecycleState.REVIEW_REQUIRED)

        # Case B: Discrepant reconciliation status
        cand_discrep = self._make_candidate(
            candidate_id="CAND_DISCREP_02",
            idempotency_key="IDEM_DISCREP_02",
            amount=Decimal("350000.00"),
            description="Discrepant Amount Notification",
        )
        applied_b, result_b, reason_b = self.engine.auto_apply_if_eligible(
            candidate=cand_discrep,
            match_tier="EXACT",
            reconciliation_status="DISCREPANT",
            review_manager=self.review_manager,
        )
        self.assertFalse(applied_b)
        self.assertIsNone(result_b)
        self.assertEqual(reason_b, "REVIEW_REQUIRED")
        self.assertEqual(cand_discrep.state, CandidateLifecycleState.REVIEW_REQUIRED)

        # Assert no transaction rows were written
        con = sqlite3.connect(str(self.db_path))
        try:
            tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(tx_count, 0)
        finally:
            con.close()

        # Assert items are properly registered in Review Queue Manager
        pending_items = self.review_manager.get_pending_items()
        self.assertEqual(len(pending_items), 2)
        item_ids = {it.batch_id for it in pending_items}
        self.assertIn("CAND_AMBIG_01", item_ids)
        self.assertIn("CAND_DISCREP_02", item_ids)

    def test_auto_apply_triggers_pre_apply_backup(self) -> None:
        """Assert native SQLite backup is created before auto-apply mutation."""
        # Pre-seed transactions table with baseline data
        con = sqlite3.connect(str(self.db_path))
        try:
            con.execute(
                "INSERT INTO transactions (date, time, transaction_type, amount, account_from, description) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-03-01", "08:00:00", "Expense", 50000.0, "BCA Main", "Pre-existing Coffee"),
            )
            con.commit()
        finally:
            con.close()

        cand = self._make_candidate(
            candidate_id="CAND_BACKUP_01",
            idempotency_key="IDEM_BACKUP_01",
            amount=Decimal("320000.00"),
        )

        applied, result, _ = self.engine.auto_apply_if_eligible(
            candidate=cand,
            match_tier="STRONG",
            reconciliation_status="RECONCILED",
        )
        self.assertTrue(applied)

        # Assert native backup file was written in backup_dir
        backup_files = list(self.backup_dir.glob("*.db"))
        self.assertGreaterEqual(len(backup_files), 1)
        bpath = backup_files[0]
        self.assertTrue(bpath.exists())

        # Verify backup integrity: backup must only contain the pre-apply record
        bcon = sqlite3.connect(str(bpath))
        try:
            b_txs = bcon.execute("SELECT description FROM transactions").fetchall()
            self.assertEqual(len(b_txs), 1)
            self.assertEqual(b_txs[0][0], "Pre-existing Coffee")
        finally:
            bcon.close()

        # Verify live DB contains both records and audit entry recorded hashes
        con2 = sqlite3.connect(str(self.db_path))
        try:
            curr_txs = con2.execute("SELECT description FROM transactions").fetchall()
            self.assertEqual(len(curr_txs), 2)
            audit_rows = con2.execute("SELECT pre_state_hash, post_state_hash FROM safe_apply_audit").fetchall()
            self.assertGreaterEqual(len(audit_rows), 1)
        finally:
            con2.close()

    def test_edge_sync_unconfigured_skips_cleanly(self) -> None:
        """Assert unconfigured worker URL or secret skips sync gracefully with status 200 payload."""
        res = sync_edge_inbox(worker_url="", secret="", db_path=self.db_path)
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["staged_count"], 0)
        self.assertIn("belum dikonfigurasi", res["message"])

    def test_edge_sync_unreachable_network_error_handled_gracefully(self) -> None:
        """Assert network connection failure returns 'unreachable' status without throwing 500 error."""
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            res = sync_edge_inbox(
                worker_url="https://unreachable.worker.dev",
                secret="some_secret_123",
                db_path=self.db_path,
            )
            self.assertEqual(res["status"], "unreachable")
            self.assertEqual(res["staged_count"], 0)
            self.assertIn("Tidak dapat terhubung", res["message"])

    def test_edge_sync_auto_applies_exact_match_evidence(self) -> None:
        """Assert pull-cloud auto-applies exact matches and updates edge_synced_messages status to AUTO_APPLIED."""
        mock_messages = [
            {
                "message_id": "gmail_auto_999",
                "from": "ebanking@bca.co.id",
                "subject": "Transaksi Rekening Tabungan BCA",
                "amount": 125000.0,
                "account_from": "BCA Main",
                "account_to": "Merchant External",
                "description": "Pembayaran Toko Buku",
                "category": "Education",
                "match_tier": "EXACT",
                "reconciliation_status": "RECONCILED",
            }
        ]

        def mock_urlopen(request, *args, **kwargs):
            resp = MagicMock()
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/sync/gmail" in url:
                resp.read.return_value = json.dumps(mock_messages).encode("utf-8")
            else:
                resp.read.return_value = b"{}"
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = sync_edge_inbox(
                worker_url="https://cf-worker.internal",
                secret="test_secret_123",
                db_path=self.db_path,
                apply_engine=self.engine,
                review_manager=self.review_manager,
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["staged_count"], 1)
            self.assertEqual(res["auto_applied_count"], 1)

            # Verify transaction written
            con = sqlite3.connect(str(self.db_path))
            try:
                tx = con.execute("SELECT amount, description FROM transactions WHERE canonical_id='EDGE_gmail_auto_999'").fetchone()
                self.assertIsNotNone(tx)
                self.assertAlmostEqual(tx[0], 125000.0, places=2)

                # Verify message marked AUTO_APPLIED
                st = con.execute("SELECT status FROM edge_synced_messages WHERE message_id='gmail_auto_999'").fetchone()
                self.assertEqual(st[0], "AUTO_APPLIED")
            finally:
                con.close()

    def test_production_db_hash_untouched_during_tests(self) -> None:
        """Assert production DB hash remains strictly untouched."""
        prod_path = _find_production_db()
        prod_hash = hashlib.sha256(prod_path.read_bytes()).hexdigest()
        self.assertEqual(
            prod_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            f"CRITICAL: Production DB hash mutated! Current: {prod_hash}, Expected: {EXPECTED_PRODUCTION_DB_SHA256}",
        )


if __name__ == "__main__":
    unittest.main()
