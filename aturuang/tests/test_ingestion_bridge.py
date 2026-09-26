"""
Test suite for ingestion_bridge and watched-folder integration.

Test cases:
  TB-1: _envelope_to_mutation with dummy NormalizedEventEnvelope (INFLOW) -> Income LedgerMutation
  TB-2: _envelope_to_mutation with dummy envelope (OUTFLOW) -> Expense LedgerMutation
  TB-3: _envelope_to_mutation with dummy envelope (NEUTRAL) -> Transfer LedgerMutation
  TB-4: _envelope_to_mutation missing fields -> no exception, defaults applied
  TB-5: process_single_file with dry_run=True on synthetic file -> returns dict with "preview" key, no DB mutation
  TB-6: process_batch_files with empty list -> returns zero counts
  TB-7: process_single_file with provider="unknown_xyz" -> returns errors list non-empty, no exception
  TB-8: process_single_file with dry_run=False on synthetic file -> auto-applies candidate to DB
  TB-9: process_batch_files with synthetic file -> aggregates results correctly
  TB-10: WatchedFolderScanner with ATURUANG_USE_BRIDGE="1" dispatches via bridge
  TB-11: WatchedFolderScanner with ATURUANG_USE_BRIDGE unset preserves legacy behavior
  TB-12: WatchedFolderScanner SUPPORTED_EXTENSIONS includes .csv, .pdf, .jpg
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from aturuang.ingestion_adapter import (
    EventDirection,
    EventRole,
    SourceEventStatus,
)
from aturuang.ingestion_bridge import (
    BRIDGE_SUPPORTED_PROVIDERS,
    _envelope_to_mutation,
    process_batch_files,
    process_single_file,
)
from aturuang.safe_apply import LedgerMutation, SafeApplyEngine
from aturuang.watched_folder import (
    SUPPORTED_EXTENSIONS,
    WatchedFolderScanner,
)


def _pdf_escape(val: str) -> str:
    return val.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_text_pdf(text: str) -> bytes:
    commands = ["BT", "/F1 9 Tf", "40 760 Td", "11 TL"]
    for line in text.splitlines():
        commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("T*")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{i} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        output.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(output)


SYNTHETIC_BCA_TEXT = """REKENING TAHAPAN XPRESI
NO. REKENING : 9911-2233-44
PERIODE : JULI 2026
TANGGAL KETERANGAN CBG MUTASI SALDO
SALDO AWAL : 1.000.000,00
01/07 TRANSFER UJI ALPHA 100.000,00 DB 900.000,00
REF: TEST-REF-A
02/07 DANA MASUK UJI BETA 250.000,00 CR 1.150.000,00
REF: TEST-REF-B
SALDO AKHIR : 1.150.000,00"""


class TestEnvelopeToMutation(unittest.TestCase):
    def test_tb1_inflow_to_income(self):
        payload = SimpleNamespace(
            amount=Decimal("500000.00"),
            currency="IDR",
            direction=EventDirection.INFLOW,
            status=SourceEventStatus.POSTED,
            occurred_at="2026-07-02T09:30:00",
            account_from="BCA-1234",
            account_to="Main-Wallet",
            description_raw="Gaji Bulanan PT Maju",
        )
        env = SimpleNamespace(
            event_role=EventRole.CASH_MOVEMENT,
            direction=EventDirection.INFLOW,
            payload=payload,
        )
        mutation = _envelope_to_mutation(env, "doc_test_1234567890abcdef")
        self.assertIsInstance(mutation, LedgerMutation)
        self.assertEqual(mutation.transaction_type, "Income")
        self.assertEqual(mutation.amount, Decimal("500000.00"))
        self.assertEqual(mutation.date, "2026-07-02")
        self.assertEqual(mutation.time, "09:30:00")
        self.assertEqual(mutation.account_from, "BCA-1234")
        self.assertEqual(mutation.account_to, "Main-Wallet")
        self.assertEqual(mutation.description, "Gaji Bulanan PT Maju")
        self.assertEqual(mutation.status, "Confirmed")
        self.assertEqual(mutation.source_refs, "ingestion:doc_test_1234567")
        self.assertEqual(mutation.notes, "[BRIDGE: doc_test_1234567]")

    def test_tb2_outflow_to_expense(self):
        payload = SimpleNamespace(
            amount=Decimal("75000.00"),
            currency="IDR",
            direction=EventDirection.OUTFLOW,
            status=SourceEventStatus.POSTED,
            occurred_at="2026-07-01 14:15:00",
            account_from="BCA-1234",
            account_to="",
            description_raw="Makan Siang",
        )
        env = SimpleNamespace(
            event_role=EventRole.CASH_MOVEMENT,
            direction=EventDirection.OUTFLOW,
            payload=payload,
        )
        mutation = _envelope_to_mutation(env, "doc_outflow_987654321")
        self.assertIsInstance(mutation, LedgerMutation)
        self.assertEqual(mutation.transaction_type, "Expense")
        self.assertEqual(mutation.amount, Decimal("75000.00"))
        self.assertEqual(mutation.date, "2026-07-01")
        self.assertEqual(mutation.time, "14:15:00")
        self.assertEqual(mutation.description, "Makan Siang")

    def test_tb3_neutral_to_transfer(self):
        payload = SimpleNamespace(
            amount=Decimal("200000.00"),
            currency="IDR",
            direction=EventDirection.NEUTRAL,
            status=SourceEventStatus.POSTED,
            occurred_at="2026-07-03",
            account_from="BCA-1234",
            account_to="Jago-5678",
            description_raw="Pindah Dana",
        )
        env = SimpleNamespace(
            event_role=EventRole.CASH_MOVEMENT,
            direction=EventDirection.NEUTRAL,
            payload=payload,
        )
        mutation = _envelope_to_mutation(env, "doc_neutral_11223344")
        self.assertIsInstance(mutation, LedgerMutation)
        self.assertEqual(mutation.transaction_type, "Transfer")
        self.assertEqual(mutation.amount, Decimal("200000.00"))
        self.assertEqual(mutation.date, "2026-07-03")
        self.assertEqual(mutation.account_from, "BCA-1234")
        self.assertEqual(mutation.account_to, "Jago-5678")

    def test_tb4_missing_fields_defaults_applied(self):
        env = SimpleNamespace()
        mutation = _envelope_to_mutation(env, "")
        self.assertIsInstance(mutation, LedgerMutation)
        self.assertEqual(mutation.amount, Decimal("0.00"))
        self.assertEqual(mutation.account_from, "")
        self.assertEqual(mutation.account_to, "")
        self.assertEqual(mutation.description, "")
        self.assertEqual(mutation.status, "Confirmed")
        self.assertTrue(len(mutation.date) == 10)
        self.assertEqual(mutation.time, "")
        self.assertIn(mutation.transaction_type, ("Income", "Expense", "Transfer"))


class TestProcessSingleFile(unittest.TestCase):
    def test_tb5_process_single_file_dry_run_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "synthetic_test.db")
            engine = SafeApplyEngine(db_path)

            pdf_path = os.path.join(td, "bca_statement.pdf")
            pdf_bytes = make_text_pdf(SYNTHETIC_BCA_TEXT)
            with open(pdf_path, "wb") as f:
                f.write(pdf_bytes)

            hash_before = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()

            res = process_single_file(db_path, pdf_path, "bca", dry_run=True)

            self.assertIn("preview", res)
            self.assertEqual(res["applied"], 0)
            self.assertEqual(res["queued"], 0)
            self.assertEqual(res["errors"], [])
            self.assertGreaterEqual(len(res["preview"]), 1)
            preview_item = res["preview"][0]
            self.assertIn("candidate_id", preview_item)
            self.assertIn("mutations", preview_item)
            self.assertEqual(preview_item["mutation_count"], len(preview_item["mutations"]))

            hash_after = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
            self.assertEqual(hash_before, hash_after)

    def test_tb6_process_batch_files_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "synthetic_test.db")
            res = process_batch_files(db_path, [], "bca", dry_run=True)
            self.assertEqual(res["applied"], 0)
            self.assertEqual(res["queued"], 0)
            self.assertEqual(res["skipped"], 0)
            self.assertEqual(res["documents_processed"], 0)
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["preview"], [])

    def test_tb7_process_single_file_unknown_provider(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "synthetic_test.db")
            file_path = os.path.join(td, "dummy.pdf")
            with open(file_path, "wb") as f:
                f.write(b"%PDF-1.4 dummy")

            res = process_single_file(db_path, file_path, "unknown_xyz", dry_run=True)
            self.assertTrue(len(res["errors"]) > 0)
            self.assertIn("unknown_xyz", res["errors"][0])
            self.assertEqual(res["applied"], 0)
            self.assertEqual(res["documents_processed"], 0)

    def test_tb8_process_single_file_live_apply(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "synthetic_test.db")
            SafeApplyEngine(db_path)

            pdf_path = os.path.join(td, "bca_statement.pdf")
            with open(pdf_path, "wb") as f:
                f.write(make_text_pdf(SYNTHETIC_BCA_TEXT))

            res = process_single_file(db_path, pdf_path, "bca", dry_run=False)
            self.assertEqual(res["errors"], [])
            self.assertEqual(res["applied"], 1)

            con = sqlite3.connect(db_path)
            try:
                rows = con.execute("SELECT amount, description, transaction_type FROM transactions").fetchall()
                self.assertGreaterEqual(len(rows), 2)
            finally:
                con.close()

    def test_tb9_process_batch_files_multiple(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "synthetic_test.db")
            SafeApplyEngine(db_path)

            sub1 = os.path.join(td, "f1")
            sub2 = os.path.join(td, "f2")
            os.makedirs(sub1)
            os.makedirs(sub2)

            p1 = os.path.join(sub1, "bca1.pdf")
            p2 = os.path.join(sub2, "bca2.pdf")
            with open(p1, "wb") as f:
                f.write(make_text_pdf(SYNTHETIC_BCA_TEXT))
            with open(p2, "wb") as f:
                f.write(make_text_pdf(SYNTHETIC_BCA_TEXT))

            res = process_batch_files(db_path, [p1, p2], "bca", dry_run=True)
            self.assertEqual(res["documents_processed"], 2)
            self.assertEqual(len(res["preview"]), 2)
            self.assertEqual(res["errors"], [])


class TestWatchedFolderBridgeHook(unittest.TestCase):
    def test_tb10_watched_folder_with_bridge_flag(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "watched_test.db")
            watch_dir = os.path.join(td, "watch")
            sub_dir = os.path.join(watch_dir, "Mutasi Rekening BCA")
            os.makedirs(sub_dir)

            pdf_path = os.path.join(sub_dir, "bca_juli_2026.pdf")
            with open(pdf_path, "wb") as f:
                f.write(make_text_pdf(SYNTHETIC_BCA_TEXT))

            orig_env = os.environ.get("ATURUANG_USE_BRIDGE")
            try:
                os.environ["ATURUANG_USE_BRIDGE"] = "1"
                scanner = WatchedFolderScanner(db_path=db_path, watched_dir=watch_dir)
                status = scanner.scan_now()

                self.assertTrue(status["success"])
                self.assertEqual(status["scanned_files"], 1)
                self.assertEqual(status["new_files"], 1)

                con = sqlite3.connect(db_path)
                try:
                    recs = con.execute("SELECT file_hash, provider, status FROM watched_folder_files").fetchall()
                    self.assertEqual(len(recs), 1)
                    self.assertEqual(recs[0][1], "bca")
                    self.assertEqual(recs[0][2], "APPLIED")

                    tx_rows = con.execute("SELECT amount FROM transactions").fetchall()
                    self.assertGreaterEqual(len(tx_rows), 2)
                finally:
                    con.close()
            finally:
                if orig_env is None:
                    os.environ.pop("ATURUANG_USE_BRIDGE", None)
                else:
                    os.environ["ATURUANG_USE_BRIDGE"] = orig_env

    def test_tb11_watched_folder_without_bridge_flag_preserves_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "watched_test.db")
            watch_dir = os.path.join(td, "watch")
            sub_dir = os.path.join(watch_dir, "gopay")
            os.makedirs(sub_dir)

            csv_path = os.path.join(sub_dir, "statement.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("Date,Amount,Description\n2026-07-01,50000,Kopi\n")

            orig_env = os.environ.get("ATURUANG_USE_BRIDGE")
            try:
                os.environ.pop("ATURUANG_USE_BRIDGE", None)
                scanner = WatchedFolderScanner(db_path=db_path, watched_dir=watch_dir)
                status = scanner.scan_now()
                self.assertTrue(status["success"])
                self.assertEqual(status["scanned_files"], 1)
            finally:
                if orig_env is not None:
                    os.environ["ATURUANG_USE_BRIDGE"] = orig_env

    def test_tb12_supported_extensions_and_providers(self):
        self.assertIn(".csv", SUPPORTED_EXTENSIONS)
        self.assertIn(".pdf", SUPPORTED_EXTENSIONS)
        self.assertIn(".jpg", SUPPORTED_EXTENSIONS)
        self.assertIn("bca", BRIDGE_SUPPORTED_PROVIDERS)
        self.assertIn("jago", BRIDGE_SUPPORTED_PROVIDERS)
        self.assertIn("blu", BRIDGE_SUPPORTED_PROVIDERS)
        self.assertIn("gopay", BRIDGE_SUPPORTED_PROVIDERS)
        self.assertIn("seabank", BRIDGE_SUPPORTED_PROVIDERS)
        self.assertIn("shopeepay", BRIDGE_SUPPORTED_PROVIDERS)


if __name__ == "__main__":
    unittest.main()
