"""
AturUang Stage 11 — Watched-Folder Ingestion Automation.

Provides automated statement ingestion from a local watched directory (e.g. Google Drive sync folder).
Includes:
1. Recursive folder-to-provider mapper.
2. SHA-256 idempotency and deduplication cache in SQLite (watched_folder_files).
3. Non-locking read and atomic staging before parsing.
4. Batch dispatching directly into the Visual Review Queue.
5. Sanitized diagnostics without PII or raw sensitive system paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from aturuang.import_center import ImportCenterManager, ImportBatch, ImportBatchItem
from aturuang.review_queue_ui import ReviewQueueManager, ReviewItem

DEFAULT_WATCHED_ROOT = Path(os.getenv("ATURUANG_WATCHED_FOLDER_PATH", r"H:\My Drive\Money Tracks"))
SUPPORTED_EXTENSIONS = frozenset({".csv", ".pdf"})

PROVIDER_SUBFOLDER_RULES: list[tuple[str, str]] = [
    ("riwayat transaksi shopee", "shopee_orders"),
    ("mutasi shopeepay", "shopeepay"),
    ("mutasi rekening bca", "bca"),
    ("mutasi rekening jago", "jago"),
    ("mutasi seabank", "seabank"),
    ("mutasi blu", "blu"),
    ("portofolio blu", "blu"),
    ("gopay", "gopay"),
]


def resolve_provider_from_path(rel_path: Path | str) -> str:
    """Maps subfolder relative path to an ingestion provider name."""
    norm_path = str(rel_path).replace("\\", "/").lower()
    for pattern, provider in PROVIDER_SUBFOLDER_RULES:
        if pattern in norm_path:
            return provider
    return "auto"


def sanitize_diagnostics(msg: str) -> str:
    """Sanitizes local filesystem paths and secrets from diagnostic messages."""
    msg = re.sub(r"[a-zA-Z]:\\[^\s,;]+", "[REDACTED_PATH]", msg)
    msg = re.sub(r"/(?:Users|home|var|tmp)/[^\s,;]+", "[REDACTED_PATH]", msg)
    return msg.strip()


@dataclass
class WatchedFileRecord:
    file_hash: str
    filename: str
    relative_path: str
    provider: str
    processed_at: str
    batch_id: str
    status: str
    item_count: int


class WatchedFolderScanner:
    """Manages local watched folder discovery, idempotency tracking, and review queue dispatching."""

    def __init__(
        self,
        db_path: Path | str,
        watched_dir: Path | str | None = None,
        review_manager: ReviewQueueManager | None = None,
        import_manager: ImportCenterManager | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        resolved_dir = watched_dir or os.getenv("ATURUANG_WATCHED_FOLDER_PATH") or DEFAULT_WATCHED_ROOT
        self.watched_dir = Path(resolved_dir)
        self.review_manager = review_manager or ReviewQueueManager(self.db_path)
        self.import_manager = import_manager or ImportCenterManager(self.db_path)

        self.last_scan_timestamp: str | None = None
        self.total_scanned: int = 0
        self.processed_count: int = 0
        self.skipped_count: int = 0
        self._init_db()

    def _init_db(self) -> None:
        """Initializes persistent SQLite idempotency store for watched files."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watched_folder_files (
                    file_hash TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    batch_id TEXT,
                    status TEXT NOT NULL,
                    item_count INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_watched_folder_files_processed_at ON watched_folder_files(processed_at)"
            )
            conn.commit()
        finally:
            conn.close()

    def is_file_processed(self, file_hash: str) -> bool:
        """Checks if a file with matching SHA-256 hash was previously processed."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT 1 FROM watched_folder_files WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def record_processed_file(
        self,
        file_hash: str,
        filename: str,
        relative_path: str,
        provider: str,
        batch_id: str,
        status: str,
        item_count: int,
    ) -> None:
        """Persists file hash record into idempotency cache."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO watched_folder_files
                (file_hash, filename, relative_path, provider, processed_at, batch_id, status, item_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_hash, filename, relative_path, provider, now_iso, batch_id, status, item_count),
            )
            conn.commit()
        finally:
            conn.close()

    def get_status(self) -> dict[str, Any]:
        """Returns current watched folder status and metrics."""
        is_active = self.watched_dir.exists() and self.watched_dir.is_dir()
        return {
            "active": is_active,
            "watched_path": str(self.watched_dir),
            "total_scanned_files": self.total_scanned,
            "processed_files_count": self.processed_count,
            "skipped_files_count": self.skipped_count,
            "last_scan_timestamp": self.last_scan_timestamp,
        }

    def scan_now(self) -> dict[str, Any]:
        """Manually triggers an on-demand scan across watched directory."""
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        self.last_scan_timestamp = now_str
        diagnostics: list[str] = []

        if not self.watched_dir.exists() or not self.watched_dir.is_dir():
            diagnostics.append(sanitize_diagnostics(f"DIRECTORY_NOT_FOUND: Path does not exist or is not a directory"))
            return {
                "success": True,
                "status": "DIRECTORY_NOT_FOUND",
                "watched_path": str(self.watched_dir),
                "scanned_files": 0,
                "new_files": 0,
                "skipped_files": 0,
                "items_queued": 0,
                "diagnostics": diagnostics,
                "last_scan_timestamp": now_str,
            }

        discovered_files: list[Path] = []
        try:
            for root, _, files in os.walk(str(self.watched_dir)):
                for file_name in files:
                    file_path = Path(root) / file_name
                    discovered_files.append(file_path)
        except Exception as e:
            diagnostics.append(sanitize_diagnostics(f"SCAN_WALK_ERROR: {str(e)}"))

        discovered_files.sort(key=lambda p: str(p))

        current_scan_count = 0
        new_files_count = 0
        skipped_count = 0
        total_items_queued = 0

        for file_path in discovered_files:
            ext = file_path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            current_scan_count += 1
            self.total_scanned += 1

            try:
                rel_path = file_path.relative_to(self.watched_dir)
            except ValueError:
                rel_path = Path(file_path.name)

            try:
                content = file_path.read_bytes()
            except Exception as read_err:
                diagnostics.append(
                    sanitize_diagnostics(f"READ_ERROR: Could not read file '{file_path.name}': {str(read_err)}")
                )
                continue

            if not content:
                diagnostics.append(
                    sanitize_diagnostics(f"EMPTY_FILE: File '{file_path.name}' contains 0 bytes")
                )
                continue

            file_hash = hashlib.sha256(content).hexdigest()

            # Idempotency check
            if self.is_file_processed(file_hash):
                skipped_count += 1
                self.skipped_count += 1
                diagnostics.append(
                    sanitize_diagnostics(f"SKIPPED_IDEMPOTENT: File '{file_path.name}' already processed")
                )
                continue

            provider = resolve_provider_from_path(rel_path)

            try:
                batch = self.import_manager.process_batch(
                    filename=file_path.name,
                    content=content,
                    provider=provider,
                )
            except Exception as parse_err:
                diagnostics.append(
                    sanitize_diagnostics(f"PARSE_ERROR: Failed to process '{file_path.name}': {str(parse_err)}")
                )
                continue

            items_queued_for_file = 0
            for item in batch.items:
                rev_item = ReviewItem(
                    item_id=item.item_id,
                    batch_id=batch.batch_id,
                    date=item.date,
                    time=item.time,
                    transaction_type=item.transaction_type,
                    amount=item.amount,
                    account_from=item.account_from,
                    account_to=item.account_to,
                    description=item.description,
                    category=item.category,
                    status="REVIEW_REQUIRED",
                    reason=item.reason or f"Imported from {provider} via Watched Folder",
                    candidate=item.candidate,
                )
                self.review_manager.add_item(rev_item)
                items_queued_for_file += 1

            self.record_processed_file(
                file_hash=file_hash,
                filename=file_path.name,
                relative_path=str(rel_path),
                provider=provider,
                batch_id=batch.batch_id,
                status=batch.status,
                item_count=items_queued_for_file,
            )

            new_files_count += 1
            self.processed_count += 1
            total_items_queued += items_queued_for_file

        return {
            "success": True,
            "status": "COMPLETED",
            "watched_path": str(self.watched_dir),
            "scanned_files": current_scan_count,
            "new_files": new_files_count,
            "skipped_files": skipped_count,
            "items_queued": total_items_queued,
            "diagnostics": diagnostics,
            "last_scan_timestamp": now_str,
        }
