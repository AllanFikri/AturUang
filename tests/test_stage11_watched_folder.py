"""
tests/test_stage11_watched_folder.py — Stage 11 Watched-Folder Ingestion Automation Tests.

Comprehensive testing of:
1. Discovery of valid CSV/PDF statement files.
2. Recursive subfolder provider mapping (BCA, Jago, GoPay, SeaBank, etc.).
3. SHA-256 idempotency cache skipping processed files.
4. Ephemeral staging and review queue item routing.
5. Ignoring non-statement extensions (.exe, .tmp, .txt).
6. Fail-closed error handling on corrupt/unreadable files without crashing.
7. HTTP GET /api/watched-folder/status endpoint.
8. HTTP POST /api/watched-folder/scan-now endpoint.
9. Graceful handling of missing/non-existent directories.
10. Zero direct ledger mutation: 0 writes to transactions table.
11. Original watched files remain unmodified and intact.
12. Empty directory handling yielding 0 items cleanly.
13. Sanitized diagnostics without PII or raw system user paths.
14. Watched-folder UI card rendered inside #import tab in index.html.
15. Canonical production database SHA-256 remains strictly untouched.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from typing import Any

from aturuang.config import PROJECT_ROOT, WEB_ROOT
from aturuang.db import init_db
from aturuang.import_center import ImportCenterManager
from aturuang.review_queue_ui import ReviewQueueManager
from aturuang.server import (
    Handler,
    ThreadedTCPServer,
    get_watched_folder_scanner,
    set_watched_folder_scanner,
    set_composer,
)
from aturuang.web_composer import QuickCaptureComposer
from aturuang.watched_folder import (
    WatchedFolderScanner,
    resolve_provider_from_path,
    sanitize_diagnostics,
)

EXPECTED_PRODUCTION_DB_SHA256 = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"


def _find_production_db() -> Path:
    candidates = [
        Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db"),
        PROJECT_ROOT.parent / "AturUang" / "runtime" / "money_tracks.db",
        PROJECT_ROOT / "runtime" / "money_tracks.db",
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


class TestStage11WatchedFolder(unittest.TestCase):
    """Test suite for Stage 11 Watched-Folder Automation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.prod_db = _find_production_db()
        if _compute_sha256(cls.prod_db) != EXPECTED_PRODUCTION_DB_SHA256:
            raise RuntimeError("Production DB modified before test class run!")

        # Setup test server on ephemeral port for API route tests
        cls.server_temp_dir = tempfile.mkdtemp(prefix="aturuang_stage11_server_")
        cls.server_db = Path(cls.server_temp_dir) / "server_test.db"
        init_db(cls.server_db)

        cls.server_watched_dir = Path(cls.server_temp_dir) / "watched"
        cls.server_watched_dir.mkdir(parents=True, exist_ok=True)

        cls.composer = QuickCaptureComposer(db_path=cls.server_db)
        set_composer(cls.composer)

        cls.server_scanner = WatchedFolderScanner(
            db_path=cls.server_db,
            watched_dir=cls.server_watched_dir,
            review_manager=cls.composer.review_manager,
            import_manager=cls.composer.import_manager,
        )
        set_watched_folder_scanner(cls.server_scanner)

        cls.server = ThreadedTCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        set_watched_folder_scanner(None)
        set_composer(None)
        shutil.rmtree(cls.server_temp_dir, ignore_errors=True)

        if _compute_sha256(cls.prod_db) != EXPECTED_PRODUCTION_DB_SHA256:
            raise RuntimeError("Production DB modified after test class run!")

    def setUp(self) -> None:
        self.assertEqual(
            _compute_sha256(self.prod_db),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered prior to test execution!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_stage11.db"
        init_db(self.db_path)

        self.watched_dir = self.temp_path / "GoogleDriveWatched"
        self.watched_dir.mkdir(parents=True, exist_ok=True)

        self.review_manager = ReviewQueueManager(self.db_path)
        self.import_manager = ImportCenterManager(self.db_path, staging_dir=self.temp_path / "staging")

        self.scanner = WatchedFolderScanner(
            db_path=self.db_path,
            watched_dir=self.watched_dir,
            review_manager=self.review_manager,
            import_manager=self.import_manager,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        self.assertEqual(
            _compute_sha256(self.prod_db),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered by test execution!",
        )

    # 1. test_watched_folder_discovers_statement_files
    def test_watched_folder_discovers_statement_files(self) -> None:
        """Discovers valid CSV and PDF statement files recursively."""
        csv_file = self.watched_dir / "mutasi_januari.csv"
        csv_file.write_text("Date,Description,Amount\n2026-01-10,Makan Siang,35000\n", encoding="utf-8")

        pdf_file = self.watched_dir / "statement.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 dummy pdf content for discovery testing")

        res = self.scanner.scan_now()
        self.assertTrue(res["success"])
        self.assertEqual(res["scanned_files"], 2)
        self.assertEqual(res["new_files"], 2)
        self.assertEqual(res["skipped_files"], 0)

    # 2. test_watched_folder_subfolder_provider_mapping
    def test_watched_folder_subfolder_provider_mapping(self) -> None:
        """Correctly maps subfolder path names to provider IDs."""
        mapping_cases = [
            ("Mutasi Rekening BCA/mutasi.csv", "bca"),
            ("mutasi rekening bca/2026-01.csv", "bca"),
            ("Mutasi Rekening Jago/statement.pdf", "jago"),
            ("Gopay/transaksi.csv", "gopay"),
            ("gopay/history.csv", "gopay"),
            ("Mutasi Seabank/seabank_jan.csv", "seabank"),
            ("Mutasi ShopeePay/shopeepay.csv", "shopeepay"),
            ("Mutasi Blu/blu_2026.csv", "blu"),
            ("Portofolio Blu/blu_port.csv", "blu"),
            ("Riwayat Transaksi Shopee/orders.csv", "shopee_orders"),
            ("Lainnya/unknown.csv", "auto"),
            ("root_statement.csv", "auto"),
        ]
        for path_str, expected_provider in mapping_cases:
            p = resolve_provider_from_path(Path(path_str))
            self.assertEqual(p, expected_provider, f"Failed mapping for: {path_str}")

    # 3. test_watched_folder_idempotency_skips_processed_files
    def test_watched_folder_idempotency_skips_processed_files(self) -> None:
        """Avoids reprocessing identical SHA-256 files on subsequent scans."""
        f1 = self.watched_dir / "statement_1.csv"
        f1.write_text("Date,Description,Amount\n2026-01-05,Kopi Kenangan,25000\n", encoding="utf-8")

        # First scan: processed
        res1 = self.scanner.scan_now()
        self.assertEqual(res1["new_files"], 1)
        self.assertEqual(res1["skipped_files"], 0)

        # Second scan without changes: skipped
        res2 = self.scanner.scan_now()
        self.assertEqual(res2["new_files"], 0)
        self.assertEqual(res2["skipped_files"], 1)

        # Confirm SQLite table has 1 unique record
        conn = sqlite3.connect(str(self.db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM watched_folder_files").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    # 4. test_watched_folder_stages_and_dispatches_to_review_queue
    def test_watched_folder_stages_and_dispatches_to_review_queue(self) -> None:
        """Dispatches newly parsed transactions directly to Visual Review Queue."""
        csv_file = self.watched_dir / "mutasi_jago.csv"
        csv_file.write_text(
            "Date,Description,Amount\n2026-01-12,Topup Saldo,150000\n2026-01-13,Pembayaran Token Listrik,50000\n",
            encoding="utf-8",
        )

        res = self.scanner.scan_now()
        self.assertGreaterEqual(res["items_queued"], 1)

        pending_items = self.review_manager.get_pending_items()
        self.assertGreaterEqual(len(pending_items), 1)

    # 5. test_watched_folder_ignores_non_statement_extensions
    def test_watched_folder_ignores_non_statement_extensions(self) -> None:
        """Ignores non-financial or unsupported file types (.exe, .tmp, .txt)."""
        (self.watched_dir / "notes.txt").write_text("Personal notes", encoding="utf-8")
        (self.watched_dir / "temp_file.tmp").write_bytes(b"temp cache")
        (self.watched_dir / "installer.exe").write_bytes(b"binary data")
        (self.watched_dir / "archive.zip").write_bytes(b"zip data")

        res = self.scanner.scan_now()
        self.assertEqual(res["scanned_files"], 0)
        self.assertEqual(res["new_files"], 0)

    # 6. test_watched_folder_corrupt_file_fail_closed
    def test_watched_folder_corrupt_file_fail_closed(self) -> None:
        """Fails closed safely on corrupt or unreadable files without crashing the loop."""
        corrupt_file = self.watched_dir / "corrupt_data.csv"
        # Write corrupted/unparseable content
        corrupt_file.write_bytes(b"\x00\xff\xfe\x00corrupted bytes that fail parse")

        # Valid file alongside
        valid_file = self.watched_dir / "valid.csv"
        valid_file.write_text("Date,Description,Amount\n2026-01-01,Valid,10000\n", encoding="utf-8")

        res = self.scanner.scan_now()
        self.assertTrue(res["success"])
        self.assertEqual(res["scanned_files"], 2)
        # Scanner continues gracefully
        self.assertGreaterEqual(res["new_files"], 1)

    # 7. test_watched_folder_status_api_endpoint
    def test_watched_folder_status_api_endpoint(self) -> None:
        """Validates HTTP GET /api/watched-folder/status response schema and status."""
        url = f"http://127.0.0.1:{self.port}/api/watched-folder/status"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))

        self.assertIn("active", data)
        self.assertIn("watched_path", data)
        self.assertIn("total_scanned_files", data)
        self.assertIn("processed_files_count", data)
        self.assertIn("skipped_files_count", data)

    # 8. test_watched_folder_scan_now_api_endpoint
    def test_watched_folder_scan_now_api_endpoint(self) -> None:
        """Validates HTTP POST /api/watched-folder/scan-now manual scan trigger."""
        url = f"http://127.0.0.1:{self.port}/api/watched-folder/scan-now"
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))

        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "COMPLETED")
        self.assertIn("scanned_files", data)
        self.assertIn("new_files", data)

    # 9. test_watched_folder_handles_missing_directory_safely
    def test_watched_folder_handles_missing_directory_safely(self) -> None:
        """Missing or non-existent watched directory fails gracefully with status DIRECTORY_NOT_FOUND."""
        non_existent = self.temp_path / "does_not_exist_folder"
        scanner = WatchedFolderScanner(
            db_path=self.db_path,
            watched_dir=non_existent,
            review_manager=self.review_manager,
            import_manager=self.import_manager,
        )
        res = scanner.scan_now()
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "DIRECTORY_NOT_FOUND")
        self.assertEqual(res["scanned_files"], 0)
        self.assertFalse(scanner.get_status()["active"])

    # 10. test_watched_folder_zero_direct_ledger_mutation
    def test_watched_folder_zero_direct_ledger_mutation(self) -> None:
        """Confirms scanning writes strictly 0 rows to transactions table."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            initial_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(initial_count, 0)
        finally:
            conn.close()

        csv_file = self.watched_dir / "transaksi.csv"
        csv_file.write_text(
            "Date,Description,Amount\n2026-01-20,Beli Buku,45000\n2026-01-21,Makan Malam,55000\n",
            encoding="utf-8",
        )
        self.scanner.scan_now()

        conn = sqlite3.connect(str(self.db_path))
        try:
            post_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(post_count, 0)
        finally:
            conn.close()

    # 11. test_watched_folder_original_files_remain_unmodified
    def test_watched_folder_original_files_remain_unmodified(self) -> None:
        """Ensures source files in watched directory are read-only and never deleted or modified."""
        f = self.watched_dir / "source_statement.csv"
        original_content = "Date,Description,Amount\n2026-01-01,Test,1000\n"
        f.write_text(original_content, encoding="utf-8")
        orig_hash = hashlib.sha256(f.read_bytes()).hexdigest()

        self.scanner.scan_now()

        self.assertTrue(f.exists(), "Original file was deleted!")
        curr_hash = hashlib.sha256(f.read_bytes()).hexdigest()
        self.assertEqual(orig_hash, curr_hash, "Original file was modified!")

    # 12. test_watched_folder_empty_directory_yields_zero_items
    def test_watched_folder_empty_directory_yields_zero_items(self) -> None:
        """Clean behavior when watched directory is completely empty."""
        empty_dir = self.temp_path / "empty_dir"
        empty_dir.mkdir()
        scanner = WatchedFolderScanner(
            db_path=self.db_path,
            watched_dir=empty_dir,
            review_manager=self.review_manager,
            import_manager=self.import_manager,
        )
        res = scanner.scan_now()
        self.assertTrue(res["success"])
        self.assertEqual(res["scanned_files"], 0)
        self.assertEqual(res["new_files"], 0)
        self.assertEqual(res["items_queued"], 0)

    # 13. test_watched_folder_sanitized_diagnostics
    def test_watched_folder_sanitized_diagnostics(self) -> None:
        """Asserts zero PII or raw system user paths appear in diagnostic messages."""
        raw_win = r"Error reading file C:\Users\allan\Documents\Secret\statement.csv"
        sanitized_win = sanitize_diagnostics(raw_win)
        self.assertNotIn("Users", sanitized_win)
        self.assertNotIn("allan", sanitized_win)
        self.assertIn("[REDACTED_PATH]", sanitized_win)

        raw_nix = "/Users/allan/secret/data.pdf failed"
        sanitized_nix = sanitize_diagnostics(raw_nix)
        self.assertNotIn("Users", sanitized_nix)
        self.assertNotIn("allan", sanitized_nix)
        self.assertIn("[REDACTED_PATH]", sanitized_nix)

    # 14. test_watched_folder_ui_card_rendered_in_import_tab
    def test_watched_folder_ui_card_rendered_in_import_tab(self) -> None:
        """Asserts index.html contains the Watched-Folder card and trigger button under #import."""
        index_html_path = WEB_ROOT / "index.html"
        content = index_html_path.read_text(encoding="utf-8")

        self.assertIn('id="watchedFolderCard"', content)
        self.assertIn('id="watchedFolderStatusBadge"', content)
        self.assertIn('id="scanFolderBtn"', content)
        self.assertIn('scanWatchedFolderNow()', content)

        # Confirm it is located inside section id="import"
        import_pos = content.find('id="import"')
        card_pos = content.find('id="watchedFolderCard"')
        review_pos = content.find('id="review"')
        self.assertGreater(import_pos, -1)
        self.assertGreater(card_pos, import_pos)
        self.assertLess(card_pos, review_pos)

    # 15. test_production_db_untouched_during_stage11_tests
    def test_production_db_untouched_during_stage11_tests(self) -> None:
        """Asserts canonical production database SHA-256 remains strictly untouched."""
        curr_hash = _compute_sha256(self.prod_db)
        self.assertEqual(
            curr_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            f"Production DB altered! Expected {EXPECTED_PRODUCTION_DB_SHA256}, got {curr_hash}",
        )


if __name__ == "__main__":
    unittest.main()
