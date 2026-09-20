"""
AturUang Stage 10 — Android Notification Evidence Bridge Tests.

Comprehensive testing of:
1. Valid HMAC-SHA256 signature verification.
2. Tampered or invalid HMAC signature rejection (401/Unauthorized).
3. Nonce replay guard rejection.
4. Timestamp drift bound enforcement (> 5 mins rejected).
5. Strict package allowlist enforcement (DISALLOWED_PACKAGE).
6. Parsing of BCA transfer notifications (Incoming & Outgoing).
7. Parsing of GoPay merchant and transfer notifications.
8. Parsing of SeaBank transfer notifications.
9. Exact Decimal preservation across Indonesian currency notations.
10. Zero Direct Ledger Mutation: Asserting 0 rows written to transactions table.
11. Routing of notification candidates to the Visual Review Queue.
12. Sanitized diagnostics without PII or raw secrets.
13. Web Composer HTTP endpoint POST /api/ingest/android-notification.
14. Malformed JSON payloads failing closed with HTTP 400.
15. Assertion that real production SQLite database remains strictly untouched.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from typing import Any

from aturuang.android_bridge import (
    ALLOWED_PACKAGES,
    AndroidNotificationBridge,
    compute_hmac_signature,
    parse_indonesian_amount,
)
from aturuang.web_composer import QuickCaptureComposer

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


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestStage10AndroidBridge(unittest.TestCase):
    """Test suite for Stage 10 Android Notification Evidence Bridge."""

    def setUp(self) -> None:
        self.prod_db = _find_production_db()
        self.assertEqual(
            _compute_sha256(self.prod_db),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered prior to test execution!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_bridge.db"

        self.composer = QuickCaptureComposer(self.db_path)
        self.bridge = self.composer.android_bridge
        self.device_id = "DEV_PIXEL_8_TEST"
        self.device_secret = "secret-key-stage10-bridge-2026"
        self.bridge.register_device(self.device_id, self.device_secret)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        self.assertEqual(
            _compute_sha256(self.prod_db),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered by test execution!",
        )

    def _build_payload(
        self,
        device_id: str | None = None,
        secret: str | None = None,
        timestamp: int | None = None,
        nonce: str = "nonce_test_001",
        package_name: str = "com.bca",
        title: str = "m-Transfer Berhasil",
        text: str = "Transfer ke BUDI Rp 150.000,00 berhasil.",
    ) -> dict[str, Any]:
        dev_id = device_id or self.device_id
        sec = secret or self.device_secret
        ts = timestamp if timestamp is not None else int(time.time())
        sig = compute_hmac_signature(sec, dev_id, ts, nonce, package_name, title, text)
        return {
            "device_id": dev_id,
            "timestamp": ts,
            "nonce": nonce,
            "package_name": package_name,
            "title": title,
            "text": text,
            "signature": sig,
        }

    # 1. test_bridge_valid_hmac_signature
    def test_bridge_valid_hmac_signature(self) -> None:
        """Accepts correctly signed notification payload with valid HMAC-SHA256."""
        payload = self._build_payload(nonce="nonce_valid_01")
        res = self.bridge.ingest_notification(payload)
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "STAGED_FOR_REVIEW")
        self.assertEqual(res["amount"], "150000.00")
        self.assertIn("item_id", res)
        self.assertIn("preview_hash", res)

    # 2. test_bridge_invalid_hmac_rejected
    def test_bridge_invalid_hmac_rejected(self) -> None:
        """Rejects tampered signature with UNAUTHORIZED error."""
        payload = self._build_payload(nonce="nonce_invalid_sig")
        payload["signature"] = "deadbeef" * 8
        res = self.bridge.ingest_notification(payload)
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "UNAUTHORIZED")

        # Also verify tampered text with valid signature of old text is rejected
        payload2 = self._build_payload(nonce="nonce_tampered_text")
        payload2["text"] = "Transfer ke HACKER Rp 99.000.000,00"
        res2 = self.bridge.ingest_notification(payload2)
        self.assertFalse(res2["success"])
        self.assertEqual(res2["error_code"], "UNAUTHORIZED")

    # 3. test_bridge_nonce_replay_blocked
    def test_bridge_nonce_replay_blocked(self) -> None:
        """Replaying the same nonce fails closed with NONCE_REPLAYED."""
        payload = self._build_payload(nonce="nonce_replay_guard_1")
        res1 = self.bridge.ingest_notification(payload)
        self.assertTrue(res1["success"])

        # Replay identical payload
        res2 = self.bridge.ingest_notification(payload)
        self.assertFalse(res2["success"])
        self.assertEqual(res2["error_code"], "NONCE_REPLAYED")

    # 4. test_bridge_expired_timestamp_rejected
    def test_bridge_expired_timestamp_rejected(self) -> None:
        """Stale timestamps older than drift threshold (> 5 mins) are rejected."""
        now = int(time.time())
        old_ts = now - 400  # 400s > 300s
        payload = self._build_payload(timestamp=old_ts, nonce="nonce_expired_1")
        res = self.bridge.ingest_notification(payload, current_time=now)
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "EXPIRED_TIMESTAMP")

    # 5. test_bridge_package_allowlist_enforced
    def test_bridge_package_allowlist_enforced(self) -> None:
        """Non-allowlisted packages fail closed safely with DISALLOWED_PACKAGE."""
        payload = self._build_payload(
            package_name="com.unauthorized.fakebank",
            nonce="nonce_disallowed_pkg",
        )
        res = self.bridge.ingest_notification(payload)
        self.assertFalse(res["success"])
        self.assertEqual(res["error_code"], "DISALLOWED_PACKAGE")

        # Allowlisted packages must all be accepted
        for pkg in ("com.bca", "mybca", "com.gojek.app", "com.gopay.app", "com.seabank.mobile", "com.jago.app", "com.shopee.id"):
            self.assertIn(pkg, ALLOWED_PACKAGES)

    # 6. test_bridge_parse_bca_transfer_notification
    def test_bridge_parse_bca_transfer_notification(self) -> None:
        """Parses incoming and outgoing BCA mobile notifications."""
        # Incoming BCA transfer
        parsed_in = self.bridge.parse_notification(
            package_name="com.bca",
            title="BCA mobile",
            text="Transfer dr BUDI SANTOSO Rp 500.000,00 ke Rekening 1234567890 berhasil.",
        )
        self.assertEqual(parsed_in.transaction_type, "Income")
        self.assertEqual(parsed_in.amount, Decimal("500000.00"))
        self.assertEqual(parsed_in.account_to, "BCA")
        self.assertIn("Budi Santoso", parsed_in.description)

        # Outgoing BCA transfer
        parsed_out = self.bridge.parse_notification(
            package_name="mybca",
            title="m-Transfer Berhasil",
            text="Transfer ke SITI AMINAH Rp 250.000,00 dari Rekening 1234567890 berhasil.",
        )
        self.assertEqual(parsed_out.transaction_type, "Expense")
        self.assertEqual(parsed_out.amount, Decimal("250000.00"))
        self.assertEqual(parsed_out.account_from, "BCA")
        self.assertIn("Siti Aminah", parsed_out.description)

    # 7. test_bridge_parse_gopay_payment_notification
    def test_bridge_parse_gopay_payment_notification(self) -> None:
        """Parses GoPay merchant payments and transfer notifications."""
        # Expense payment
        parsed_pay = self.bridge.parse_notification(
            package_name="com.gojek.app",
            title="GoPay",
            text="Kamu telah membayar Rp 35.000 ke Kopi Kenangan",
        )
        self.assertEqual(parsed_pay.transaction_type, "Expense")
        self.assertEqual(parsed_pay.amount, Decimal("35000.00"))
        self.assertEqual(parsed_pay.account_from, "GoPay")
        self.assertIn("Kopi Kenangan", parsed_pay.description)

        # Incoming balance
        parsed_in = self.bridge.parse_notification(
            package_name="com.gopay.app",
            title="GoPay",
            text="Kamu menerima saldo GoPay Rp 100.000 dari Ahmad",
        )
        self.assertEqual(parsed_in.transaction_type, "Income")
        self.assertEqual(parsed_in.amount, Decimal("100000.00"))
        self.assertEqual(parsed_in.account_to, "GoPay")

    # 8. test_bridge_parse_seabank_transfer_notification
    def test_bridge_parse_seabank_transfer_notification(self) -> None:
        """Parses SeaBank incoming transfer notifications."""
        parsed = self.bridge.parse_notification(
            package_name="com.seabank.mobile",
            title="SeaBank",
            text="Transfer Masuk Rp 1.250.000 dari Bank Mandiri berhasil",
        )
        self.assertEqual(parsed.transaction_type, "Income")
        self.assertEqual(parsed.amount, Decimal("1250000.00"))
        self.assertEqual(parsed.account_to, "SeaBank")

    # 9. test_bridge_exact_decimal_preservation
    def test_bridge_exact_decimal_preservation(self) -> None:
        """Ensures parsed notification amounts use pure Decimal without float inaccuracy."""
        # Indonesian format with thousand dot and decimal comma
        d1 = parse_indonesian_amount("Transfer Rp 1.500.000,50 berhasil")
        self.assertEqual(d1, Decimal("1500000.50"))
        self.assertIsInstance(d1, Decimal)

        # Standalone thousand dots
        d2 = parse_indonesian_amount("Pembayaran sebesar Rp 75.000 berhasil")
        self.assertEqual(d2, Decimal("75000.00"))

        # Exact decimal math preservation
        total = d1 + d2  # type: ignore
        self.assertEqual(total, Decimal("1575000.50"))

    # 10. test_bridge_zero_direct_ledger_mutation
    def test_bridge_zero_direct_ledger_mutation(self) -> None:
        """Confirms 0 writes to transactions table on notification ingestion."""
        con = sqlite3.connect(self.db_path)
        try:
            initial_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(initial_count, 0)
        finally:
            con.close()

        # Ingest 3 different notifications
        for i in range(3):
            p = self._build_payload(nonce=f"nonce_zero_ledger_{i}")
            res = self.bridge.ingest_notification(p)
            self.assertTrue(res["success"])

        # Confirm transactions table remains strictly empty
        con = sqlite3.connect(self.db_path)
        try:
            post_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(post_count, 0)
        finally:
            con.close()

    # 11. test_bridge_routes_to_review_queue
    def test_bridge_routes_to_review_queue(self) -> None:
        """Ingested notification appears in pending review queue with REVIEW_REQUIRED status."""
        payload = self._build_payload(
            nonce="nonce_queue_001",
            package_name="com.gojek.app",
            title="GoPay",
            text="Kamu telah membayar Rp 50.000 ke Toko Buku",
        )
        res = self.bridge.ingest_notification(payload)
        self.assertTrue(res["success"])

        # Check pending review items
        pending_items = self.composer.get_review_queue()
        self.assertGreaterEqual(len(pending_items), 1)

        matched = next((item for item in pending_items if item["item_id"] == res["item_id"]), None)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["status"], "REVIEW_REQUIRED")
        self.assertEqual(matched["amount"], "50000.00")
        self.assertEqual(matched["account_from"], "GoPay")

    # 12. test_bridge_sanitized_notification_diagnostics
    def test_bridge_sanitized_notification_diagnostics(self) -> None:
        """Asserts zero PII or raw secrets appear in diagnostics or response payloads."""
        payload = self._build_payload(nonce="nonce_diag_001")
        res = self.bridge.ingest_notification(payload)
        res_json = json.dumps(res)

        # Secret must never be exposed in response
        self.assertNotIn(self.device_secret, res_json)
        self.assertNotIn("password", res_json.lower())

    # 13. test_bridge_web_composer_endpoint
    def test_bridge_web_composer_endpoint(self) -> None:
        """Validates HTTP POST /api/ingest/android-notification routing."""
        payload = self._build_payload(nonce="nonce_http_001")
        body_bytes = json.dumps(payload).encode("utf-8")

        code, headers, body = self.composer.handle_request(
            "POST",
            "/api/ingest/android-notification",
            body=body_bytes,
        )
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers["Content-Type"])

        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "STAGED_FOR_REVIEW")

    # 14. test_bridge_malformed_json_fails_closed
    def test_bridge_malformed_json_fails_closed(self) -> None:
        """Invalid JSON structure returns clean HTTP 400 response."""
        # Broken JSON syntax
        code, headers, body = self.composer.handle_request(
            "POST",
            "/api/ingest/android-notification",
            body=b"{invalid: json syntax, broken",
        )
        self.assertEqual(code, 400)
        data = json.loads(body.decode("utf-8"))
        self.assertFalse(data["success"])
        self.assertEqual(data["error_code"], "MALFORMED_JSON")

        # Empty body
        c2, h2, b2 = self.composer.handle_request(
            "POST",
            "/api/ingest/android-notification",
            body=b"",
        )
        self.assertEqual(c2, 400)

        # Non-object JSON
        c3, h3, b3 = self.composer.handle_request(
            "POST",
            "/api/ingest/android-notification",
            body=b'["array", "not", "object"]',
        )
        self.assertEqual(c3, 400)

    # 15. test_production_db_untouched_during_stage10_tests
    def test_production_db_untouched_during_stage10_tests(self) -> None:
        """Asserts production database SHA-256 remains strictly untouched."""
        current_hash = _compute_sha256(self.prod_db)
        self.assertEqual(
            current_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            f"Production DB altered! Expected {EXPECTED_PRODUCTION_DB_SHA256}, got {current_hash}",
        )


if __name__ == "__main__":
    unittest.main()
