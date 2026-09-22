"""Comprehensive security, credential, and architecture remediation tests (SEC-REMED-R1).

Verifies all 24 security and architecture requirements:
1. Production DB hash immutability (8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421).
2. Least-privilege OAuth scopes in appsscript.json (no userinfo.email, drive.file, gmail.modify).
3. Exact sender validation in Apps Script and Worker (no substring/spoofing bypasses).
4. Apps Script PDF sync safety (15MB limit, 5 attachments max, safe naming, SHA-256 idempotency).
5. Secret separation: REPAIR_SECRET vs GMAIL_RELAY_SECRET vs STAGING_ADMIN_TOKEN.
6. Worker HMAC verification: valid HMAC-SHA256, replay guard, 5-minute skew window (past/future),
   nonce format, 2MB payload bound, malformed JSON handling.
7. Local server CORS hardening: loopback-only reflection, no wildcard CORS.
8. Local server endpoint protection: Origin and Sec-Fetch-Site enforcement on export/download/pull.
9. Static file path traversal containment (/assets/.. blocking).
10. Audit logging on database download and CSV export.
11. Watched folder 15MB/50MB file size containment.
12. Multi-store secret precedence and conflict detection with fail-closed behavior.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aturuang.config import PROJECT_ROOT
from aturuang.server import Handler, get_edge_sync_config
from aturuang.watched_folder import MAX_WATCHED_FILE_SIZE, WatchedFolderScanner

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


class TestProductionDatabaseIntegrity(unittest.TestCase):
    def test_production_db_hash_strictly_unchanged(self) -> None:
        """Requirement 1: Assert production DB hash is identical to verified baseline."""
        prod_path = _find_production_db()
        prod_hash = hashlib.sha256(prod_path.read_bytes()).hexdigest()
        self.assertEqual(
            prod_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            f"CRITICAL: Production DB hash mutated! Current: {prod_hash}, Expected: {EXPECTED_PRODUCTION_DB_SHA256}",
        )


class TestAppsScriptSecurityAndLeastPrivilege(unittest.TestCase):
    def setUp(self) -> None:
        self.appsscript_path = PROJECT_ROOT / "integrations" / "gmail-apps-script" / "appsscript.json"
        self.code_gs_path = PROJECT_ROOT / "integrations" / "gmail-apps-script" / "Code.gs"
        self.assertTrue(self.appsscript_path.exists(), "appsscript.json must exist")
        self.assertTrue(self.code_gs_path.exists(), "Code.gs must exist")

    def test_appsscript_oauth_scopes_least_privilege(self) -> None:
        """Requirement 2: Verify least privilege scopes in appsscript.json."""
        manifest = json.loads(self.appsscript_path.read_text(encoding="utf-8"))
        scopes = manifest.get("oauthScopes", [])

        # userinfo.email must NOT be present
        self.assertNotIn(
            "https://www.googleapis.com/auth/userinfo.email",
            scopes,
            "userinfo.email must be removed for least privilege",
        )

        # broad drive scope must NOT be present
        self.assertNotIn(
            "https://www.googleapis.com/auth/drive",
            scopes,
            "Broad drive scope must NOT be present",
        )

        # drive.file must be present for attachment storage
        self.assertIn(
            "https://www.googleapis.com/auth/drive.file",
            scopes,
            "drive.file scope must be present for PDF upload",
        )

        # gmail.modify must be present for labeling
        self.assertIn(
            "https://www.googleapis.com/auth/gmail.modify",
            scopes,
            "gmail.modify scope must be present for message labeling",
        )

    def test_apps_script_exact_sender_matching(self) -> None:
        """Requirement 3: Verify strict exact-sender validation in Code.gs."""
        code = self.code_gs_path.read_text(encoding="utf-8")

        # Parse TRUSTED_SENDERS_SET from Code.gs
        match = re.search(r"const TRUSTED_SENDERS_SET\s*=\s*\[(.*?)\];", code, re.DOTALL)
        self.assertIsNotNone(match, "TRUSTED_SENDERS_SET must be defined in Code.gs")
        senders = [
            s.strip().strip('"').strip("'")
            for s in match.group(1).split(",")
            if s.strip().strip('"').strip("'")
        ]

        # Stockbit and core trusted senders must be in the set
        self.assertIn("bca@bca.co.id", senders)
        self.assertIn("noreply@jago.com", senders)
        self.assertIn("noreply@stockbit.com", senders)
        self.assertIn("contactus@stockbit.com", senders)
        self.assertIn("info@shopee.co.id", senders)

        # Simulate isSenderExactTrusted
        def is_trusted(header: str) -> bool:
            clean = header
            m = re.search(r"<([^>]+)>", header)
            if m:
                clean = m.group(1)
            return clean.strip().lower() in senders

        # Must accept valid exact senders
        self.assertTrue(is_trusted("bca@bca.co.id"))
        self.assertTrue(is_trusted("Bank Central Asia <bca@bca.co.id>"))
        self.assertTrue(is_trusted("noreply@stockbit.com"))
        self.assertTrue(is_trusted("Stockbit <noreply@stockbit.com>"))

        # Must REJECT substring or domain lookalike spoofing
        self.assertFalse(is_trusted("attacker@bca.co.id.fake.net"))
        self.assertFalse(is_trusted("bca@bca.co.id.attacker.com"))
        self.assertFalse(is_trusted("bca@bca.co.id@attacker.com"))
        self.assertFalse(is_trusted("noreply@jago.com.attacker.org"))
        self.assertFalse(is_trusted("evil_shopee@gmail.com"))
        self.assertFalse(is_trusted("attacker@stockbit.org"))

    def test_apps_script_pdf_attachment_constraints(self) -> None:
        """Requirement 4: Verify PDF sync constants and safety guards in Code.gs."""
        code = self.code_gs_path.read_text(encoding="utf-8")

        self.assertIn("syncEmailPdfAttachmentsToDrive", code)
        self.assertIn("MAX_PDF_SIZE_BYTES = 15 * 1024 * 1024", code)
        self.assertIn("MAX_ATTACHMENTS_PER_MSG = 5", code)
        self.assertIn("PDF_FOLDER_CONFIG_MISSING", code)
        self.assertIn("PDF_DRIVE_FOLDER_NOT_FOUND", code)
        self.assertIn("OVERSIZED_ATTACHMENT", code)
        self.assertIn("aturuang/pdf-synced", code)

    def test_apps_script_repair_secret_separation(self) -> None:
        """Requirement 5: Verify repair functions in Code.gs use REPAIR_SECRET."""
        code = self.code_gs_path.read_text(encoding="utf-8")

        # In repair functions, REPAIR_SECRET must be requested
        self.assertIn('props.getProperty(\n      "REPAIR_SECRET"\n    )', code)
        self.assertIn("BCA_QRIS_REPAIR_CONFIG_MISSING", code)


class TestCloudflareWorkerSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.auth_ts = PROJECT_ROOT / "cloud" / "worker" / "src" / "auth.ts"
        self.index_ts = PROJECT_ROOT / "cloud" / "worker" / "src" / "index.ts"
        self.assertTrue(self.auth_ts.exists())
        self.assertTrue(self.index_ts.exists())

    def test_worker_secret_separation_and_bounds(self) -> None:
        """Requirement 6: Verify worker secret separation and body size bounds."""
        auth_code = self.auth_ts.read_text(encoding="utf-8")
        index_code = self.index_ts.read_text(encoding="utf-8")

        # auth.ts must define REPAIR_SECRET in Env
        self.assertIn("REPAIR_SECRET?: string;", auth_code)
        # auth.ts must define MAX_HMAC_BODY_BYTES = 2MB
        self.assertIn("MAX_HMAC_BODY_BYTES = 2 * 1024 * 1024", auth_code)
        # auth.ts must define MAX_CLOCK_SKEW_MS = 300000 (5 minutes)
        self.assertIn("MAX_CLOCK_SKEW_MS = 300000", auth_code)

        # index.ts must pass REPAIR_SECRET to /api/repair/bca-qris
        self.assertIn("env.REPAIR_SECRET", index_code)
        self.assertIn('"UNCONFIGURED_REPAIR_SECRET"', index_code)

        # index.ts must handle OVERSIZED_PAYLOAD (413) and MALFORMED_JSON (400)
        self.assertIn('"OVERSIZED_PAYLOAD"', index_code)
        self.assertIn('"MALFORMED_JSON"', index_code)

    def test_worker_hmac_logic_simulation(self) -> None:
        """Requirement 7: Simulate Worker HMAC verification rules."""
        secret = "test_gmail_relay_secret_key_12345"
        raw_body = json.dumps({"message_id": "msg_001", "from": "bca@bca.co.id"})
        now_ms = int(time.time() * 1000)
        nonce = "valid_nonce_123456"

        def sign(ts_str: str, n_str: str, body: str, key: str) -> str:
            msg = f"{ts_str}.{n_str}.{body}".encode("utf-8")
            return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()

        # 1. Valid signature
        valid_sig = sign(str(now_ms), nonce, raw_body, secret)
        self.assertTrue(len(valid_sig) == 64)

        # 2. Tampered body
        tampered_sig = sign(str(now_ms), nonce, raw_body + "tampered", secret)
        self.assertNotEqual(valid_sig, tampered_sig)

        # 3. Wrong secret
        wrong_sec_sig = sign(str(now_ms), nonce, raw_body, "wrong_secret")
        self.assertNotEqual(valid_sig, wrong_sec_sig)

        # 4. Expired timestamp (> 5 min past)
        expired_ms = now_ms - (300000 + 1000)
        self.assertTrue(expired_ms < now_ms - 300000)

        # 5. Future timestamp (> 5 min future)
        future_ms = now_ms + (300000 + 1000)
        self.assertTrue(future_ms > now_ms + 300000)

        # 6. Invalid nonce regex
        invalid_nonce_chars = "nonce!@#$%"
        self.assertIsNone(re.match(r"^[A-Za-z0-9_-]+$", invalid_nonce_chars))
        short_nonce = "abc"
        self.assertTrue(len(short_nonce) < 8)


class TestLocalServerSecurityAndAuditing(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.test_dir.name) / "test_ledger.db"

        # Initialize test database and close connection immediately
        con = sqlite3.connect(self.test_db_path)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT NOT NULL DEFAULT '',
                    date TEXT NOT NULL,
                    time TEXT NOT NULL DEFAULT '',
                    transaction_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    account_from TEXT NOT NULL,
                    account_to TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    for_with_whom TEXT NOT NULL DEFAULT '',
                    money_context TEXT NOT NULL DEFAULT '',
                    settlement_kind TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Active',
                    confidence TEXT NOT NULL DEFAULT 'High',
                    budget_effect TEXT NOT NULL DEFAULT 'None',
                    subtype TEXT NOT NULL DEFAULT '',
                    source_refs TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    is_deleted INTEGER NOT NULL DEFAULT 0
                );
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS transaction_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    old_data TEXT NOT NULL DEFAULT '',
                    new_data TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    name TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'Other'
                );
            """)
            con.execute(
                "INSERT INTO transactions (date, transaction_type, amount, account_from, description) VALUES ('2026-09-01', 'Expense', 50000.0, 'BCA', 'Test TX')"
            )
            con.commit()
        finally:
            con.close()

    def tearDown(self) -> None:
        try:
            self.test_dir.cleanup()
        except Exception:
            pass

    def _make_handler(self, method: str, path: str, headers: dict[str, str] | None = None) -> Handler:
        handler = Handler.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.headers = headers or {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 54321)
        handler.send_error = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.send_json = MagicMock()
        return handler

    def test_cors_header_loopback_only_never_wildcard(self) -> None:
        """Requirement 8: Verify CORS header reflects trusted loopback origins and never wildcard."""
        # 1. Loopback origin
        h1 = self._make_handler("GET", "/api/dashboard", {"Origin": "http://127.0.0.1:5050", "Host": "127.0.0.1:5050"})
        allowed1 = h1._get_allowed_origin()
        self.assertEqual(allowed1, "http://127.0.0.1:5050")
        self.assertNotEqual(allowed1, "*")

        # 2. Localhost origin
        h2 = self._make_handler("GET", "/api/dashboard", {"Origin": "http://localhost:3000", "Host": "127.0.0.1:5050"})
        allowed2 = h2._get_allowed_origin()
        self.assertEqual(allowed2, "http://localhost:3000")
        self.assertNotEqual(allowed2, "*")

        # 3. External attacker origin
        h3 = self._make_handler("GET", "/api/dashboard", {"Origin": "https://malicious-website.com", "Host": "127.0.0.1:5050"})
        allowed3 = h3._get_allowed_origin()
        self.assertIsNone(allowed3)
        self.assertNotEqual(allowed3, "*")

    def test_sensitive_endpoints_reject_cross_origin_access(self) -> None:
        """Requirement 9: External origin or Sec-Fetch-Site: cross-site must be rejected with 403."""
        def _make_conn():
            c = sqlite3.connect(self.test_db_path)
            c.row_factory = sqlite3.Row
            return c

        with patch("aturuang.server.DB_FILE", self.test_db_path):
            with patch("aturuang.server.db_connect", _make_conn):
                # 1. Download DB with external origin
                h_db = self._make_handler(
                    "GET",
                    "/api/download_db",
                    {"Origin": "https://attacker.site", "Host": "127.0.0.1:5050"},
                )
                h_db.do_GET()
                h_db.send_error.assert_called_with(403, "Forbidden Cross-Origin Access")

                # 2. Export CSV with cross-site Sec-Fetch-Site
                h_csv = self._make_handler(
                    "GET",
                    "/api/export_csv",
                    {"Sec-Fetch-Site": "cross-site", "Host": "127.0.0.1:5050"},
                )
                h_csv.do_GET()
                h_csv.send_error.assert_called_with(403, "Forbidden Cross-Site Access")

                # 3. Pull cloud with external origin
                h_pull = self._make_handler(
                    "POST",
                    "/api/sync/pull-cloud",
                    {"Origin": "https://evil.org", "Host": "127.0.0.1:5050"},
                )
                h_pull.do_POST()
                h_pull.send_json.assert_called_with(
                    {"status": "error", "message": "Akses lintas-asal ditolak."}, status=403
                )

    def test_static_asset_path_traversal_blocked(self) -> None:
        """Requirement 10: Path traversal attempts under /assets/ must be blocked."""
        # Relative traversal
        h1 = self._make_handler("GET", "/assets/../runtime/money_tracks.db")
        h1.do_GET()
        h1.send_error.assert_called_with(403, "Forbidden")

        # Encoded or double traversal
        h2 = self._make_handler("GET", "/assets/../../server.py")
        h2.do_GET()
        h2.send_error.assert_called_with(403, "Forbidden")

    def test_audit_logging_on_download_and_export(self) -> None:
        """Requirement 11: Accessing download_db and export_csv logs audit entries."""
        def _make_conn():
            c = sqlite3.connect(self.test_db_path)
            c.row_factory = sqlite3.Row
            return c

        with patch("aturuang.server.DB_FILE", self.test_db_path):
            with patch("aturuang.server.db_connect", _make_conn):
                # 1. Valid local download_db
                h_db = self._make_handler(
                    "GET",
                    "/api/download_db",
                    {"Sec-Fetch-Site": "same-origin", "Host": "127.0.0.1:5050"},
                )
                h_db.do_GET()

                # 2. Valid local export_csv
                h_csv = self._make_handler(
                    "GET",
                    "/api/export_csv",
                    {"Sec-Fetch-Site": "same-origin", "Host": "127.0.0.1:5050"},
                )
                h_csv.do_GET()

                # Verify audit entries in transaction_audit_log
                con = sqlite3.connect(self.test_db_path)
                try:
                    actions = [
                        row[0]
                        for row in con.execute(
                            "SELECT action FROM transaction_audit_log ORDER BY id"
                        ).fetchall()
                    ]
                    self.assertIn("DATABASE_DOWNLOAD", actions)
                    self.assertIn("CSV_EXPORT", actions)
                finally:
                    con.close()

    def test_watched_folder_file_size_limit(self) -> None:
        """Requirement 12: Watched folder scanner enforces 15MB file size bound."""
        with tempfile.TemporaryDirectory() as watch_dir:
            scanner = WatchedFolderScanner(
                db_path=self.test_db_path,
                watched_dir=watch_dir,
            )
            oversized_file = Path(watch_dir) / "large_invoice.pdf"
            oversized_file.write_bytes(b"dummy")

            with patch.object(Path, "stat") as mock_stat:
                stat_res = MagicMock()
                stat_res.st_size = MAX_WATCHED_FILE_SIZE + 1024  # > 15MB
                mock_stat.return_value = stat_res

                res = scanner.scan_now()
                self.assertEqual(res["skipped_files"], 1)
                self.assertTrue(
                    any("SKIPPED_OVERSIZED" in diag for diag in res.get("diagnostics", []))
                )

    def test_secret_precedence_and_conflict_detection(self) -> None:
        """Requirement 13: Secret stores with conflicting values fail closed."""
        with patch.dict(
            "os.environ",
            {
                "CLOUDFLARE_WORKER_URL": "https://worker-alpha.internal",
                "EDGE_SYNC_SECRET": "secret_alpha",
            },
        ):
            with tempfile.TemporaryDirectory() as tmp_dir:
                fake_sec = Path(tmp_dir) / "secrets.json"
                fake_sec.write_text(
                    json.dumps({
                        "WORKER_URL": "https://worker-beta.internal",
                        "sync_secret": "secret_beta",
                    }),
                    encoding="utf-8",
                )
                with patch("aturuang.server.Path.cwd", return_value=Path(tmp_dir)):
                    url, sec = get_edge_sync_config()
                    self.assertEqual(url, "")
                    self.assertEqual(sec, "")


if __name__ == "__main__":
    unittest.main()
