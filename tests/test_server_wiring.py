"""
tests/test_server_wiring.py — Unified Server Wiring & Ingestion Compatibility Verification

Verifies that:
1. START_MONEY_TRACKS.bat / launcher.py and money_tracks_server.py serve all
   Universal Ingestion endpoints alongside legacy core endpoints through Handler.
2. PWA manifest, service worker, and icon are served with correct headers.
3. Quick Capture, Import Center, Review Queue, Receipt OCR, and Android Notification endpoints work.
4. Production SQLite database remains strictly untouched (canonical SHA-256 preserved).
"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from aturuang.config import DB_FILE, PROJECT_ROOT
from aturuang.db import db_connect, init_db
from aturuang.server import Handler, ThreadedTCPServer, get_composer, set_composer
from aturuang.web_composer import QuickCaptureComposer

CANONICAL_PROD_DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")
EXPECTED_PROD_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"


class TestUnifiedServerWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Verify canonical production database SHA256 before any test runs
        if CANONICAL_PROD_DB.exists():
            with open(CANONICAL_PROD_DB, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
            if sha != EXPECTED_PROD_DB_SHA256:
                raise RuntimeError(
                    f"Canonical production DB hash mismatch before tests: expected {EXPECTED_PROD_DB_SHA256}, got {sha}"
                )

        cls.local_db_sha = hashlib.sha256(DB_FILE.read_bytes()).hexdigest() if DB_FILE.exists() else None

        # 2. Setup isolated test environment with temporary DB and directories
        cls.temp_dir = tempfile.mkdtemp(prefix="aturuang_wiring_test_")
        cls.test_db = Path(cls.temp_dir) / "test_money_tracks.db"
        cls.test_backup = Path(cls.temp_dir) / "backups"
        cls.test_staging = Path(cls.temp_dir) / "staging"
        cls.test_backup.mkdir(parents=True, exist_ok=True)
        cls.test_staging.mkdir(parents=True, exist_ok=True)

        init_db(cls.test_db)

        # 3. Create test composer bound to isolated test DB
        cls.composer = QuickCaptureComposer(
            db_path=cls.test_db,
            backup_dir=cls.test_backup,
            staging_dir=cls.test_staging,
        )
        set_composer(cls.composer)

        # 4. Start ThreadedTCPServer on random ephemeral port
        cls.server = ThreadedTCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        set_composer(None)
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

        # Verify canonical production DB remains untouched after tests
        if CANONICAL_PROD_DB.exists():
            with open(CANONICAL_PROD_DB, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
            if sha != EXPECTED_PROD_DB_SHA256:
                raise RuntimeError(
                    f"CRITICAL: Canonical production DB modified during test suite: expected {EXPECTED_PROD_DB_SHA256}, got {sha}"
                )

        if DB_FILE.exists() and cls.local_db_sha:
            curr_sha = hashlib.sha256(DB_FILE.read_bytes()).hexdigest()
            if curr_sha != cls.local_db_sha:
                raise RuntimeError(
                    f"CRITICAL: Worktree DB modified during test suite: expected {cls.local_db_sha}, got {curr_sha}"
                )

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()

    def _post(self, path: str, payload: dict | bytes, headers: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        hdrs = headers or {}
        if isinstance(payload, dict):
            body = json.dumps(payload).encode("utf-8")
            if "Content-Type" not in hdrs:
                hdrs["Content-Type"] = "application/json"
        else:
            body = payload
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def test_pwa_routes(self):
        # 1. Manifest
        status, hdrs, body = self._get("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertIn("application/manifest+json", hdrs.get("Content-Type", ""))
        manifest = json.loads(body.decode("utf-8"))
        self.assertIn("name", manifest)
        self.assertEqual(manifest.get("short_name"), "AturUang")

        # 2. Service Worker
        status, hdrs, body = self._get("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", hdrs.get("Content-Type", ""))
        self.assertIn(b"CACHE_NAME", body)

        # 3. Icon
        status, hdrs, body = self._get("/icon.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", hdrs.get("Content-Type", ""))
        self.assertIn(b"<svg", body)

    def test_web_index_and_scripts(self):
        status, hdrs, body = self._get("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("Catat Cepat", html)
        self.assertIn("Pusat Impor", html)
        self.assertIn("Tinjauan", html)
        self.assertIn("Pindai Struk", html)
        self.assertIn('data-page="quick-capture"', html)
        self.assertIn('data-page="import"', html)
        self.assertIn('data-page="review"', html)
        self.assertIn('data-page="receipt"', html)
        self.assertIn("manifest.webmanifest", html)

        # Check app.js includes Universal Ingestion wiring
        status, hdrs, body = self._get("/js/app.js")
        self.assertEqual(status, 200)
        js = body.decode("utf-8")
        self.assertIn("openQuickCaptureModal", js)
        self.assertIn("uploadImportFile", js)
        self.assertIn("loadReviewQueue", js)
        self.assertIn("uploadReceiptFile", js)
        self.assertIn("serviceWorker.register", js)

    def test_quick_capture_preview_and_apply_flow(self):
        # 1. Preview
        status, hdrs, body = self._get("/api/quick-capture/preview?q=-35rb+makan+nasi+padang+bca")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn(data["status"], ("SUCCESS", "valid"))
        self.assertIn(data["transaction_type"], ("EXPENSE", "Expense"))
        self.assertEqual(data["amount"], "35000.00")
        self.assertEqual(data["account_from"], "BCA")
        self.assertTrue(data.get("preview_hash"))

        # 2. Apply
        status, hdrs, body = self._post("/api/quick-capture/apply", data)
        self.assertEqual(status, 200)
        apply_res = json.loads(body.decode("utf-8"))
        self.assertTrue(apply_res.get("success"))
        self.assertTrue(len(apply_res.get("applied_row_ids", [])) > 0)

    def test_import_center_flow(self):
        csv_content = (
            "Tanggal,Keterangan,Jumlah,Jenis,Akun\n"
            "2026-09-01,Transfer Masuk Dari Budi,100000,Income,BCA\n"
        ).encode("utf-8")

        status, hdrs, body = self._post(
            "/api/import/upload",
            {"filename": "mutasi_test.csv", "content": csv_content.decode("latin-1")},
        )
        self.assertEqual(status, 200)
        summary = json.loads(body.decode("utf-8"))
        self.assertIn("batch_id", summary)

    def test_review_queue_flow(self):
        status, hdrs, body = self._get("/api/review-queue/pending")
        self.assertEqual(status, 200)
        items = json.loads(body.decode("utf-8"))
        self.assertIsInstance(items, list)

    def test_receipt_ocr_flow(self):
        # Create a small valid 1x1 PNG
        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
            b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        status, hdrs, body = self._post(
            "/api/receipt/upload",
            {"filename": "nota_test.png", "content": tiny_png.decode("latin-1")},
        )
        self.assertEqual(status, 200)
        draft = json.loads(body.decode("utf-8"))
        self.assertIn("receipt_id", draft)
        self.assertIn("preview_hash", draft)

        # Confirm / reject draft
        status, hdrs, body = self._post(
            "/api/receipt/confirm",
            {"receipt_id": draft["receipt_id"], "action": "reject", "reason": "Test rejection"},
        )
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertTrue(res.get("success"))

    def test_android_notification_endpoint(self):
        # Android notification bridge POST should reject unauthorized payloads
        status, hdrs, body = self._post("/api/ingest/android-notification", {"dummy": 123})
        # Expect 400 or 401 because HMAC signature and timestamp headers are missing
        self.assertIn(status, (400, 401, 403))

    def test_legacy_core_endpoints(self):
        status, hdrs, body = self._get("/api/dashboard")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("kpis", data)

        status, hdrs, body = self._get("/api/accounts")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("accounts", data)


if __name__ == "__main__":
    unittest.main()
