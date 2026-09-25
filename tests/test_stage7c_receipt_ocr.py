"""
Universal Ingestion Stage 7C — Receipt OCR Extraction, Review Queue & Safe Apply Tests.

Comprehensive testing of:
1. Local receipt OCR extraction, merchant/date/amount normalization, and field confidence scoring.
2. Exact Decimal monetary precision and absence of IEEE 754 float drift.
3. Fail-closed routing on low-confidence or missing amounts to REVIEW_REQUIRED.
4. Editable draft correction, recalculation of preview hashes, and candidate state transitions.
5. Receipt image duplicate detection via SHA-256 image hashes.
6. Web Composer HTTP endpoints: POST /api/receipt/upload, POST /api/receipt/confirm.
7. End-to-end receipt upload -> edit -> apply lifecycle into canonical SQLite ledger.
8. Sanitized diagnostics: zero leakage of local filesystem paths or PII.
9. Corrupt image fail-closed handling.
10. Strict idempotency preservation: re-submitting confirmed receipts does not duplicate rows.
11. Automated pre-apply SQLite backups triggered on receipt apply.
12. Unsupported image format rejection (raises ValueError).
13. Responsive multi-tab HTML/UI rendering with Receipt OCR tab.
14. Strict terminal state protection for rejected/applied receipts.
15. Zero-touch invariant: production SQLite database remains untouched and byte-identical.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aturuang.receipt_ocr import (
    ReceiptOCRManager,
    ReceiptDraft,
    PNG_HEADER,
    JPEG_HEADER,
    compute_sha256_bytes,
)
from aturuang.web_composer import (
    QuickCaptureComposer,
)
from aturuang.safe_apply import (
    CandidateLifecycleState,
    SafeApplyEngine,
)

EXPECTED_PRODUCTION_DB_HASH = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
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


# Synthetic receipt fixtures
SAMPLE_PNG_RECEIPT = (
    PNG_HEADER
    + b"\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x06\x00\x00\x00"
    + b"Indomaret Sudirman\n"
    + b"Tanggal: 2026-09-15 14:30\n"
    + b"1x Roti Manis Rp 15.000,00\n"
    + b"1x Air Mineral Rp 5.000,00\n"
    + b"Total Belanja: Rp 20.000,00\n"
)

SAMPLE_JPEG_RECEIPT = (
    JPEG_HEADER
    + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    + b"Starbucks Coffee\n"
    + b"Date: 2026-09-18 10:15:00\n"
    + b"Caramel Macchiato 65000.00\n"
    + b"Total: Rp 65.000,00\n"
)

SAMPLE_LOW_CONF_RECEIPT = (
    PNG_HEADER
    + b"\x00\x00\x00\rIHDR"
    + b"Blurry text with no readable merchant or price\n"
)


class TestStage7CReceiptOCR(unittest.TestCase):
    """Focused test suite for Stage 7C receipt OCR and review queue integration."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_stage7c.db"
        self.backup_dir = self.temp_path / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.temp_path / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        # Initialize test accounts
        con = sqlite3.connect(str(self.db_path))
        with con:
            con.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    account_type TEXT NOT NULL,
                    current_balance REAL NOT NULL DEFAULT 0.0,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
            """)
            con.execute("INSERT INTO accounts (name, account_type, current_balance) VALUES ('Cash', 'Asset', 100000.00);")
            con.execute("INSERT INTO accounts (name, account_type, current_balance) VALUES ('BCA Main', 'Asset', 5000000.00);")
        con.close()

        self.ocr_mgr = ReceiptOCRManager(self.db_path, backup_dir=self.backup_dir)
        self.composer = QuickCaptureComposer(
            self.db_path,
            backup_dir=self.backup_dir,
            staging_dir=self.staging_dir,
        )

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    def test_receipt_ocr_successful_extraction(self) -> None:
        draft = self.ocr_mgr.process_receipt("struk_indomaret.png", SAMPLE_PNG_RECEIPT)
        self.assertEqual(draft.status, "READY_FOR_CONFIRMATION")
        self.assertEqual(draft.merchant, "Indomaret")
        self.assertEqual(draft.date, "2026-09-15")
        self.assertEqual(draft.amount, Decimal("20000.00"))
        self.assertIsNotNone(draft.candidate)
        self.assertTrue(len(draft.preview_hash) > 0)

    def test_receipt_ocr_decimal_precision(self) -> None:
        draft = self.ocr_mgr.process_receipt("starbucks.jpg", SAMPLE_JPEG_RECEIPT)
        self.assertIsInstance(draft.amount, Decimal)
        self.assertEqual(draft.amount, Decimal("65000.00"))
        self.assertIsInstance(draft.candidate.mutations[0].amount, Decimal)

    def test_receipt_ocr_low_confidence_routes_to_review(self) -> None:
        draft = self.ocr_mgr.process_receipt("blurry.png", SAMPLE_LOW_CONF_RECEIPT)
        self.assertEqual(draft.status, "REVIEW_REQUIRED")
        self.assertEqual(draft.field_confidences["overall"], "LOW")
        self.assertTrue(any(w in draft.reason for w in ("MISSING_TOTAL_AMOUNT", "LOW_CONFIDENCE_OCR")))

    def test_receipt_ocr_editable_correction(self) -> None:
        draft = self.ocr_mgr.process_receipt("struk.png", SAMPLE_PNG_RECEIPT)
        initial_hash = draft.preview_hash

        # Owner modifies merchant and amount
        updated = self.ocr_mgr.modify_receipt(
            draft.receipt_id,
            {"merchant": "Indomaret Point", "amount": Decimal("25000.00")}
        )
        self.assertEqual(updated.merchant, "Indomaret Point")
        self.assertEqual(updated.amount, Decimal("25000.00"))
        self.assertNotEqual(updated.preview_hash, initial_hash)
        self.assertEqual(updated.status, "READY_FOR_CONFIRMATION")

    def test_receipt_ocr_duplicate_detection(self) -> None:
        # First submission
        d1 = self.ocr_mgr.process_receipt("first.png", SAMPLE_PNG_RECEIPT)
        self.assertEqual(d1.status, "READY_FOR_CONFIRMATION")

        # Second submission of identical image
        d2 = self.ocr_mgr.process_receipt("second_copy.png", SAMPLE_PNG_RECEIPT)
        self.assertEqual(d2.status, "REVIEW_REQUIRED")
        self.assertIn("DUPLICATE_RECEIPT", d2.reason)

    def test_web_composer_receipt_upload_endpoint(self) -> None:
        code, headers, body = self.composer.handle_request(
            "POST",
            "/api/receipt/upload",
            query={"filename": ["struk_test.png"]},
            body=SAMPLE_PNG_RECEIPT,
        )
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("receipt_id", data)
        self.assertEqual(data["merchant"], "Indomaret")
        self.assertEqual(data["amount"], "20000.00")
        self.assertEqual(data["status"], "READY_FOR_CONFIRMATION")

    def test_web_composer_receipt_confirm_flow(self) -> None:
        # Step 1: Upload receipt
        code1, _, body1 = self.composer.handle_request(
            "POST",
            "/api/receipt/upload",
            query={"filename": ["struk.png"]},
            body=SAMPLE_PNG_RECEIPT,
        )
        data1 = json.loads(body1.decode("utf-8"))
        rcpt_id = data1["receipt_id"]

        # Step 2: Confirm with correction (amount modified to 22000.00)
        confirm_body = json.dumps({
            "receipt_id": rcpt_id,
            "corrections": {"amount": "22000.00", "account_from": "Cash"},
            "action": "apply",
        }).encode("utf-8")
        code2, _, body2 = self.composer.handle_request(
            "POST",
            "/api/receipt/confirm",
            body=confirm_body,
        )
        self.assertEqual(code2, 200)
        data2 = json.loads(body2.decode("utf-8"))
        self.assertTrue(data2["success"])
        self.assertEqual(data2["status"], "APPLIED")
        self.assertEqual(data2["applied_count"], 1)

        # Step 3: Check database row inserted in transactions table
        con = sqlite3.connect(str(self.db_path))
        try:
            cur = con.cursor()
            row = cur.execute("SELECT amount, description FROM transactions WHERE id = ?", (data2["applied_row_ids"][0],)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(Decimal(str(row[0])).quantize(Decimal("0.01")), Decimal("22000.00"))
        finally:
            con.close()

    def test_sanitized_receipt_diagnostics(self) -> None:
        draft = self.ocr_mgr.process_receipt("test.png", SAMPLE_PNG_RECEIPT)
        self.ocr_mgr.reject(draft.receipt_id, reason="Parsing issue at C:\\Users\\admin\\Desktop\\secret\\struk.png line 10")
        rejected = self.ocr_mgr.get_receipt(draft.receipt_id)
        self.assertNotIn("C:\\Users\\admin", rejected.reason)
        self.assertIn("[REDACTED_PATH]", rejected.reason)

    def test_corrupt_image_fail_closed(self) -> None:
        corrupt_bytes = b"CORRUPTED_NON_IMAGE_DATA_12345"
        draft = self.ocr_mgr.process_receipt("corrupt.png", corrupt_bytes)
        self.assertEqual(draft.status, "FAILED")
        self.assertIn("CORRUPT_OR_INVALID_IMAGE", draft.reason)
        self.assertEqual(draft.field_confidences["overall"], "LOW")

    def test_idempotent_receipt_submission(self) -> None:
        draft = self.ocr_mgr.process_receipt("idemp.png", SAMPLE_PNG_RECEIPT)
        res1 = self.ocr_mgr.confirm_and_apply(draft.receipt_id)
        self.assertTrue(res1.success)
        self.assertEqual(len(res1.applied_row_ids), 1)

        # Re-apply same receipt candidate
        res2 = self.ocr_mgr.confirm_and_apply(draft.receipt_id)
        self.assertTrue(res2.success)
        self.assertTrue(res2.is_idempotent_replay)

        # Assert no duplicate transactions created
        con = sqlite3.connect(str(self.db_path))
        try:
            cur = con.cursor()
            cnt = cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(cnt, 1)
        finally:
            con.close()

    def test_pre_apply_backup_triggered_on_receipt_apply(self) -> None:
        draft = self.ocr_mgr.process_receipt("backup_test.png", SAMPLE_PNG_RECEIPT)
        res = self.ocr_mgr.confirm_and_apply(draft.receipt_id)
        self.assertTrue(res.success)
        backups = list(self.backup_dir.glob("*.db"))
        self.assertGreaterEqual(len(backups), 1)

    def test_unsupported_image_format_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.ocr_mgr.process_receipt("malicious.exe", b"binary content")
        with self.assertRaises(ValueError):
            self.ocr_mgr.process_receipt("invoice.pdf", b"%PDF-1.4...")

    def test_html_contains_receipt_tab(self) -> None:
        html = self.composer.render_html()
        self.assertIn("tab-receipt", html)
        self.assertIn("Pindai Struk", html)
        self.assertIn("receiptFileInput", html)
        self.assertIn("uploadReceipt", html)

    def test_terminal_state_receipt_protection(self) -> None:
        draft = self.ocr_mgr.process_receipt("term_test.png", SAMPLE_PNG_RECEIPT)
        self.ocr_mgr.reject(draft.receipt_id, reason="Receipt was declined")

        # Rejected receipt cannot be modified or applied
        with self.assertRaises(ValueError):
            self.ocr_mgr.modify_receipt(draft.receipt_id, {"amount": Decimal("10000.00")})
        with self.assertRaises(ValueError):
            self.ocr_mgr.confirm_and_apply(draft.receipt_id)

    def test_production_db_untouched_during_stage7c_tests(self) -> None:
        if PRODUCTION_DB_PATH.exists():
            actual_hash = compute_sha256(PRODUCTION_DB_PATH)
            self.assertEqual(actual_hash, EXPECTED_PRODUCTION_DB_HASH)


if __name__ == "__main__":
    unittest.main()
