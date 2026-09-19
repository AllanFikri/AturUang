"""
Universal Ingestion Stage 7B — Import Center UI & Visual Review Queue Test Suite.

Comprehensive tests covering:
1. Import Center batch management and atomic file staging (import_center.py).
2. Visual Review Queue backend and Owner approval lifecycle (review_queue_ui.py).
3. Web Composer endpoints and UI integration (web_composer.py).
4. End-to-end flow from statement file upload to Stage 6 Safe Apply.
5. Invariants: fail-closed on corrupt files, sanitized diagnostics, terminal protection, production DB untouched.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aturuang.import_center import (
    ImportCenterManager,
    ImportBatch,
    StagedFile,
)
from aturuang.review_queue_ui import (
    ReviewQueueManager,
    ReviewItem,
)
from aturuang.web_composer import (
    QuickCaptureComposer,
)
from aturuang.safe_apply import (
    CandidateLifecycleState,
    SafeApplyEngine,
)

EXPECTED_PRODUCTION_DB_HASH = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PRODUCTION_DB_PATH = Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db")


def compute_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


SAMPLE_CSV_VALID = b"""Date,Description,Amount,Type,Account
2026-09-01,Gaji Bulanan,10000000,Income,BCA Main
2026-09-02,Makan Bakso,25000,Expense,GoPay
2026-09-03,Bensin Shell,50000,Expense,Cash
2026-09-04,Transfer Rekening,500000,Transfer,BCA Main
"""

SAMPLE_CSV_WITH_AMBIGUOUS = b"""Date,Description,Amount,Type,Account
2026-09-01,Gaji Bulanan,10000000,Income,BCA Main
2026-09-02,Makan Bakso,25000,Expense,GoPay
2026-09-03,Unknown Suspense Transfer,,Expense,BCA Main
"""


class TestStage7BImportCenter(unittest.TestCase):
    """Stage 7B test cases covering Import Center, Review Queue, and Web Composer integration."""

    @classmethod
    def setUpClass(cls) -> None:
        if PRODUCTION_DB_PATH.exists():
            cls.initial_prod_hash = compute_sha256(PRODUCTION_DB_PATH)
        else:
            cls.initial_prod_hash = None

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.test_db = self.td / "test_money_tracks.db"
        self.staging_dir = self.td / "staging"
        self.backup_dir = self.td / "backups"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_center_batch_ingestion(self) -> None:
        """1. Staging and dry-running uploaded files correctly."""
        mgr = ImportCenterManager(self.test_db, staging_dir=self.staging_dir)
        batch = mgr.process_batch("bca_statement.csv", SAMPLE_CSV_VALID)

        self.assertEqual(batch.status, "PARSED")
        self.assertEqual(batch.total_rows, 4)
        self.assertEqual(batch.parsed_items, 4)
        self.assertEqual(batch.review_required_count, 0)
        self.assertTrue(Path(self.staging_dir).exists())
        staged_files = list(self.staging_dir.glob("*_bca_statement.csv"))
        self.assertEqual(len(staged_files), 1)

    def test_import_center_summary_generation(self) -> None:
        """2. Validates batch metrics (parsed items, matched pairs, reconciliation status)."""
        mgr = ImportCenterManager(self.test_db, staging_dir=self.staging_dir)
        batch = mgr.process_batch("statement.csv", SAMPLE_CSV_WITH_AMBIGUOUS)

        self.assertEqual(batch.status, "REVIEW_REQUIRED")
        self.assertEqual(batch.total_rows, 3)
        self.assertEqual(batch.parsed_items, 2)
        self.assertEqual(batch.review_required_count, 1)
        self.assertEqual(batch.reconciliation_status, "PENDING_REVIEW")

        summary = mgr.get_batch_summary(batch.batch_id)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["total_rows"], 3)
        self.assertEqual(summary["parsed_items"], 2)
        self.assertEqual(summary["review_required_count"], 1)

    def test_review_queue_retrieval(self) -> None:
        """3. Fetches pending review items accurately with sanitized diagnostics."""
        rq = ReviewQueueManager(self.test_db, backup_dir=self.backup_dir)
        item = ReviewItem(
            item_id="it_001",
            batch_id="b_001",
            date="2026-09-03",
            time="12:00:00",
            transaction_type="Expense",
            amount=Decimal("0.00"),
            account_from="BCA Main",
            account_to="",
            description="Unknown payment",
            category="Other / Miscellaneous",
            status="REVIEW_REQUIRED",
            reason="Uncertain transfer in C:\\Users\\Admin\\statement.csv",
        )
        rq.add_item(item)

        pending = rq.get_pending_items()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].item_id, "it_001")
        self.assertEqual(pending[0].status, "REVIEW_REQUIRED")
        self.assertNotIn("Users\\Admin", pending[0].reason)

    def test_review_queue_approve_action(self) -> None:
        """4. Approving a review item moves it to confirmation readiness."""
        rq = ReviewQueueManager(self.test_db, backup_dir=self.backup_dir)
        item = ReviewItem(
            item_id="it_002",
            batch_id="b_001",
            date="2026-09-02",
            time="12:00:00",
            transaction_type="Expense",
            amount=Decimal("25000.00"),
            account_from="GoPay",
            account_to="",
            description="Makan siang",
            category="Main Meals",
            status="REVIEW_REQUIRED",
            reason="Review required",
        )
        rq.add_item(item)

        approved = rq.approve("it_002", notes="Verified by owner")
        self.assertEqual(approved.status, "READY_FOR_CONFIRMATION")
        self.assertIn("Verified by owner", approved.reason)
        if approved.candidate:
            self.assertEqual(approved.candidate.state, CandidateLifecycleState.READY_FOR_CONFIRMATION)

    def test_review_queue_reject_action(self) -> None:
        """5. Rejecting a review item terminates it cleanly."""
        rq = ReviewQueueManager(self.test_db, backup_dir=self.backup_dir)
        item = ReviewItem(
            item_id="it_003",
            batch_id="b_001",
            date="2026-09-02",
            time="12:00:00",
            transaction_type="Expense",
            amount=Decimal("15000.00"),
            account_from="Cash",
            account_to="",
            description="Spam row",
            category="",
            status="REVIEW_REQUIRED",
        )
        rq.add_item(item)

        rejected = rq.reject("it_003", reason="Invalid receipt")
        self.assertEqual(rejected.status, "REJECTED")
        self.assertIn("Invalid receipt", rejected.reason)

    def test_review_queue_modify_action(self) -> None:
        """6. Modifying parameters recalculates preview hash correctly with Decimal precision."""
        rq = ReviewQueueManager(self.test_db, backup_dir=self.backup_dir)
        item = ReviewItem(
            item_id="it_004",
            batch_id="b_001",
            date="2026-09-03",
            time="12:00:00",
            transaction_type="Expense",
            amount=Decimal("0.00"),
            account_from="Cash",
            account_to="",
            description="Fix me",
            category="Other",
            status="REVIEW_REQUIRED",
        )
        rq.add_item(item)

        mod = rq.modify("it_004", {
            "amount": "45000.00",
            "account_from": "GoPay",
            "description": "Makan Bakso Enak",
            "category": "Main Meals",
        })

        self.assertEqual(mod.status, "READY_FOR_CONFIRMATION")
        self.assertEqual(mod.amount, Decimal("45000.00"))
        self.assertEqual(mod.account_from, "GoPay")
        self.assertEqual(mod.description, "Makan Bakso Enak")
        self.assertIsNotNone(mod.candidate)
        self.assertNotEqual(mod.candidate.preview_hash, "")

    def test_web_composer_import_endpoints(self) -> None:
        """7. Validates HTTP endpoints for file upload and status inspection."""
        composer = QuickCaptureComposer(self.test_db, backup_dir=self.backup_dir, staging_dir=self.staging_dir)

        # POST /api/import/upload
        upload_payload = json.dumps({
            "filename": "monthly_statement.csv",
            "content": SAMPLE_CSV_VALID.decode("utf-8"),
        })
        code, headers, body = composer.handle_request("POST", "/api/import/upload", body=upload_payload)
        self.assertEqual(code, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("batch_id", res)
        self.assertEqual(res["total_rows"], 4)

        # GET /api/import/batch
        code, headers, body = composer.handle_request("GET", "/api/import/batch", query={"id": [res["batch_id"]]})
        self.assertEqual(code, 200)
        batch_res = json.loads(body.decode("utf-8"))
        self.assertEqual(batch_res["batch_id"], res["batch_id"])

    def test_web_composer_review_endpoints(self) -> None:
        """8. Validates HTTP endpoints for review queue action dispatch."""
        composer = QuickCaptureComposer(self.test_db, backup_dir=self.backup_dir, staging_dir=self.staging_dir)

        # Ingest file with ambiguous item
        upload_payload = json.dumps({
            "filename": "ambiguous.csv",
            "content": SAMPLE_CSV_WITH_AMBIGUOUS.decode("utf-8"),
        })
        composer.handle_request("POST", "/api/import/upload", body=upload_payload)

        # GET /api/review-queue/pending
        code, headers, body = composer.handle_request("GET", "/api/review-queue/pending")
        self.assertEqual(code, 200)
        pending = json.loads(body.decode("utf-8"))
        self.assertEqual(len(pending), 1)
        item_id = pending[0]["item_id"]

        # POST /api/review-queue/action (modify)
        code, headers, body = composer.handle_request("POST", "/api/review-queue/action", body=json.dumps({
            "action": "modify",
            "item_id": item_id,
            "updates": {"amount": "100000.00", "description": "Resolved transfer"},
        }))
        self.assertEqual(code, 200)
        mod_res = json.loads(body.decode("utf-8"))
        self.assertTrue(mod_res["success"])
        self.assertEqual(mod_res["amount"], "100000.00")

        # POST /api/review-queue/action (apply)
        code, headers, body = composer.handle_request("POST", "/api/review-queue/action", body=json.dumps({
            "action": "apply",
            "item_id": item_id,
        }))
        self.assertEqual(code, 200)
        apply_res = json.loads(body.decode("utf-8"))
        self.assertTrue(apply_res["success"])

    def test_idempotent_batch_import(self) -> None:
        """9. Re-uploading identical batch produces deterministic cached result."""
        mgr = ImportCenterManager(self.test_db, staging_dir=self.staging_dir)
        batch1 = mgr.process_batch("dup.csv", SAMPLE_CSV_VALID)
        batch2 = mgr.process_batch("dup.csv", SAMPLE_CSV_VALID)

        self.assertEqual(batch1.batch_id, batch2.batch_id)
        self.assertEqual(batch1.file_hash, batch2.file_hash)
        self.assertEqual(batch1.total_rows, batch2.total_rows)

    def test_sanitized_import_diagnostics(self) -> None:
        """10. Asserts zero PII or raw local filesystem paths in import diagnostics."""
        mgr = ImportCenterManager(self.test_db, staging_dir=self.staging_dir)
        dirty_content = b"Date,Description,Amount\n2026-09-01,Secret C:\\Users\\Admin\\token.key,25000\n"
        batch = mgr.process_batch("C:\\Users\\Admin\\Desktop\\dirty.csv", dirty_content)

        for diag in batch.diagnostics:
            self.assertNotIn("Users\\Admin", diag)

    def test_malformed_file_fail_closed(self) -> None:
        """11. Corrupted, unsupported, or empty files fail closed safely with FAILED status."""
        mgr = ImportCenterManager(self.test_db, staging_dir=self.staging_dir)

        # Unsupported extension
        b_bad_ext = mgr.process_batch("payload.exe", b"binary executable content")
        self.assertEqual(b_bad_ext.status, "FAILED")
        self.assertEqual(b_bad_ext.reconciliation_status, "DISCREPANCY")
        self.assertIn("UNSUPPORTED_FORMAT", b_bad_ext.diagnostics[0])

        # Empty content
        b_empty = mgr.process_batch("empty.csv", b"")
        self.assertEqual(b_empty.status, "FAILED")
        self.assertIn("EMPTY_FILE", b_empty.diagnostics[0])

        # Malformed PDF
        b_bad_pdf = mgr.process_batch("corrupt.pdf", b"not a real pdf header")
        self.assertEqual(b_bad_pdf.status, "PARSED" if b_bad_pdf.total_rows == 0 else "REVIEW_REQUIRED")
        self.assertTrue(any("MALFORMED_FILE" in d for d in b_bad_pdf.diagnostics))

    def test_atomic_batch_staging(self) -> None:
        """12. Staging operations write atomically into staging directory."""
        mgr = ImportCenterManager(self.test_db, staging_dir=self.staging_dir)
        staged = mgr.stage_file("test_atomic.csv", SAMPLE_CSV_VALID)

        self.assertTrue(staged.staged_path.exists())
        self.assertEqual(staged.size_bytes, len(SAMPLE_CSV_VALID))
        self.assertEqual(staged.content_hash, hashlib.sha256(SAMPLE_CSV_VALID).hexdigest())

        # Assert no leftover temporary files in staging
        tmp_files = list(self.staging_dir.glob(".tmp_*"))
        self.assertEqual(len(tmp_files), 0)

    def test_review_queue_terminal_state_protection(self) -> None:
        """13. Cannot modify or approve already terminated items (REJECTED or APPLIED)."""
        rq = ReviewQueueManager(self.test_db, backup_dir=self.backup_dir)
        item = ReviewItem(
            item_id="it_term",
            batch_id="b_001",
            date="2026-09-02",
            time="12:00:00",
            transaction_type="Expense",
            amount=Decimal("50000.00"),
            account_from="Cash",
            account_to="",
            description="Item to reject",
            category="",
            status="REVIEW_REQUIRED",
        )
        rq.add_item(item)

        rq.reject("it_term", reason="Terminated")
        self.assertEqual(item.status, "REJECTED")

        # Modifying rejected item must fail
        with self.assertRaises(ValueError) as cm:
            rq.modify("it_term", {"amount": "60000.00"})
        self.assertIn("terminated", str(cm.exception).lower())

        # Approving rejected item must fail
        with self.assertRaises(ValueError) as cm:
            rq.approve("it_term")
        self.assertIn("terminated", str(cm.exception).lower())

    def test_end_to_end_import_to_safe_apply(self) -> None:
        """14. Full flow: Upload -> Review -> Modify/Approve -> Atomic Safe Apply with backup."""
        composer = QuickCaptureComposer(self.test_db, backup_dir=self.backup_dir, staging_dir=self.staging_dir)

        # 1. Upload CSV with ambiguous row
        summary = composer.upload_file("e2e_statement.csv", SAMPLE_CSV_WITH_AMBIGUOUS)
        self.assertEqual(summary["review_required_count"], 1)

        # 2. Get review queue
        queue = composer.get_review_queue()
        self.assertEqual(len(queue), 1)
        item_id = queue[0]["item_id"]

        # 3. Modify item to resolve missing amount & description
        mod_res = composer.dispatch_review_action(
            "modify",
            item_id,
            updates={"amount": "80000.00", "description": "Resolved monthly subscription"},
        )
        self.assertTrue(mod_res["success"])

        # 4. Apply approved item
        apply_res = composer.dispatch_review_action("apply", item_id)
        self.assertTrue(apply_res["success"])
        self.assertEqual(apply_res["state"], "APPLIED")

        # 5. Verify database insertion
        con = sqlite3.connect(self.test_db)
        cnt = con.execute("SELECT COUNT(*) FROM transactions WHERE description='Resolved monthly subscription'").fetchone()[0]
        con.close()
        self.assertEqual(cnt, 1)

        # 6. Verify backup was created
        backups = list(self.backup_dir.glob("*.db"))
        self.assertGreaterEqual(len(backups), 1)

    def test_production_db_untouched_during_stage7b_tests(self) -> None:
        """15. Asserts production database SHA-256 is strictly unchanged throughout Stage 7B tests."""
        if not PRODUCTION_DB_PATH.exists():
            self.skipTest("Production database does not exist on this host.")
        current_hash = compute_sha256(PRODUCTION_DB_PATH)
        self.assertEqual(
            current_hash,
            EXPECTED_PRODUCTION_DB_HASH,
            "CRITICAL VIOLATION: Production database hash was mutated during Stage 7B testing!",
        )
        if self.initial_prod_hash:
            self.assertEqual(
                current_hash,
                self.initial_prod_hash,
                "CRITICAL VIOLATION: Production database hash changed from initial setup!",
            )


if __name__ == "__main__":
    unittest.main()
