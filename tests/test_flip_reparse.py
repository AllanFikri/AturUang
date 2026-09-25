r"""
Unit tests for STEP-17B: Flip Reparse Utility (scripts/flip_reparse/reparse_flip.py).

Requirements:
  T-R01: tool --dry-run with synthetic JSON + synthetic activation DB copy + prod DB hash check -> outputs summary, does NOT mutate DB.
  T-R02: tool --apply on synthetic DB -> replaces entries, creates backup file, produces new payload with reparsed_by marker.
  T-R03: NON_TRANSACTION classification -> DROP action, row deleted.
  T-R04: idempotency guard -> second --apply refuses.
  T-R05: prod DB hash mismatch (simulate by editing a copy) -> ABORT, no mutation.
  T-R06: scope exceeds 200 -> refuse.
  T-R07: --apply without backup capability -> refuse.
  T-R08: prod DB hash check in setUpClass + tearDownClass (EXPECTED 8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

EXPECTED_PROD_DB_HASH = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "flip_reparse" / "reparse_flip.py"


def get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


def init_synthetic_activation_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE edge_synced_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT 'gmail',
                sender TEXT,
                subject TEXT,
                payload TEXT NOT NULL,
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'STAGED',
                content_hash TEXT
            );
        """)
        con.commit()
    finally:
        con.close()


class TestFlipReparseTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            assert db_hash == EXPECTED_PROD_DB_HASH, f"Production DB corrupted before test: {db_hash}"

    @classmethod
    def tearDownClass(cls) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            assert db_hash == EXPECTED_PROD_DB_HASH, f"Production DB corrupted after test: {db_hash}"

    # T-R01: tool --dry-run with synthetic JSON + synthetic activation DB copy -> outputs summary, does NOT mutate DB
    def test_r01_dry_run_does_not_mutate_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            # Insert 2 synthetic rows
            con = sqlite3.connect(str(act_db))
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (1, "syn_msg_01", json.dumps({"amount": 10000, "event_kind": "EXTERNAL_TRANSFER"})),
            )
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (2, "syn_msg_02", json.dumps({"amount": 20000, "event_kind": "EXTERNAL_TRANSFER"})),
            )
            con.commit()
            con.close()

            act_hash_before = hashlib.sha256(act_db.read_bytes()).hexdigest()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 1,
                    "message_id": "syn_msg_01",
                    "from": "no-reply@flip.id",
                    "subject": "Transfer Digital #W123456789",
                    "body": "Destination Name\nALLAN FIKRI MAHARDIKA SANTOSA\nTotal: Rp 150.000",
                    "internal_date": "2026-06-04T04:21:34.000Z",
                },
                {
                    "edge_id": 2,
                    "message_id": "syn_msg_02",
                    "from": "no-reply@flip.id",
                    "subject": "Another device is trying to login to your account.",
                    "body": "Perangkat baru terdeteksi.",
                    "internal_date": "2026-06-04T04:25:00.000Z",
                },
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            # Run with --dry-run
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Process failed: {proc.stderr}")
            self.assertIn("MODE: DRY_RUN", proc.stdout)
            self.assertIn("REPLACE", proc.stdout)
            self.assertIn("DROP", proc.stdout)

            # Assert database has NOT been mutated
            act_hash_after = hashlib.sha256(act_db.read_bytes()).hexdigest()
            self.assertEqual(act_hash_before, act_hash_after)

            # Assert no backup files created
            backup_files = list(td.glob("money_tracks.db.bak-*"))
            self.assertEqual(len(backup_files), 0)

    # T-R02: tool --apply on synthetic DB -> replaces entries, creates backup file, produces new payload with reparsed_by marker
    def test_r02_apply_replaces_and_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            con = sqlite3.connect(str(act_db))
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (10, "syn_msg_10", json.dumps({"amount": 10000, "event_kind": "EXTERNAL_TRANSFER"})),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 10,
                    "message_id": "syn_msg_10",
                    "from": "no-reply@flip.id",
                    "subject": "Transfer Digital #W998877665",
                    "body": "Destination Name\nALLAN FIKRI MAHARDIKA SANTOSA\nTotal: Rp 250.000",
                    "internal_date": "2026-06-04T05:00:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Apply failed: {proc.stderr}")
            self.assertIn("APPLY_SUCCESSFUL", proc.stdout)

            # Verify backup file created
            backup_files = list(td.glob("money_tracks.db.bak-*"))
            self.assertEqual(len(backup_files), 1)
            self.assertGreater(backup_files[0].stat().st_size, 0)

            # Verify mutated row in DB
            con = sqlite3.connect(str(act_db))
            row = con.execute("SELECT message_id, payload, status FROM edge_synced_messages WHERE message_id = 'syn_msg_10'").fetchone()
            con.close()

            self.assertIsNotNone(row)
            payload = json.loads(row[1])
            self.assertEqual(payload.get("reparsed_by"), "step-17b-flip-reparse-v1")
            self.assertEqual(payload.get("tx_type"), "Transfer")
            self.assertEqual(payload.get("amount"), 250000)
            self.assertEqual(row[2], "REVIEW_REQUIRED")

    # T-R03: NON_TRANSACTION classification -> DROP action, row deleted
    def test_r03_drop_action_deletes_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            con = sqlite3.connect(str(act_db))
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (20, "syn_msg_alert", json.dumps({"amount": 0})),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 20,
                    "message_id": "syn_msg_alert",
                    "from": "no-reply@flip.id",
                    "subject": "Another device is trying to login to your account.",
                    "body": "Pemberitahuan keamanan.",
                    "internal_date": "2026-06-04T05:10:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Apply failed: {proc.stderr}")

            # Verify row deleted from DB
            con = sqlite3.connect(str(act_db))
            count = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE id = 20").fetchone()[0]
            con.close()
            self.assertEqual(count, 0)

    # T-R04: idempotency guard -> second --apply refuses
    def test_r04_idempotency_guard_refuses_second_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            # Insert an already-reparsed row
            con = sqlite3.connect(str(act_db))
            already_reparsed_payload = json.dumps({
                "amount": 50000,
                "event_kind": "OWN_TRANSFER",
                "reparsed_by": "step-17b-flip-reparse-v1",
            })
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (30, "syn_msg_reparsed", already_reparsed_payload),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 30,
                    "message_id": "syn_msg_reparsed",
                    "from": "no-reply@flip.id",
                    "subject": "Transfer Digital #W112233445",
                    "body": "Destination Name\nALLAN FIKRI MAHARDIKA SANTOSA\nTotal: Rp 50.000",
                    "internal_date": "2026-06-04T05:20:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("IDEMPOTENCY_GUARD", proc.stderr)

    # T-R05: prod DB hash mismatch -> ABORT, no mutation
    def test_r05_prod_db_hash_mismatch_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            fake_prod_db = td / "fake_prod.db"
            fake_prod_db.write_bytes(b"corrupted prod db content")

            input_json = td / "input.json"
            input_json.write_text(json.dumps({"messages": []}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={fake_prod_db}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("PROD_DB_HASH_MISMATCH", proc.stderr)

    # T-R06: scope exceeds 200 -> refuse
    def test_r06_scope_exceeds_200_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            messages = [
                {
                    "edge_id": i,
                    "message_id": f"syn_msg_{i}",
                    "from": "no-reply@flip.id",
                    "subject": f"Transfer #{i}",
                    "body": "Body",
                    "internal_date": "2026-06-04T05:00:00Z",
                }
                for i in range(205)
            ]
            input_json = td / "input.json"
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("SCOPE_LIMIT_EXCEEDED", proc.stderr)

    # T-R07: --apply without backup capability -> refuse
    def test_r07_apply_without_backup_capability_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 50,
                    "message_id": "syn_msg_50",
                    "from": "no-reply@flip.id",
                    "subject": "Transfer #FT500000000",
                    "body": "Destination Name\nZAYYINNA FITRIANI\nTotal: Rp 10.000",
                    "internal_date": "2026-06-04T05:00:00Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            test_script = """
import sys
from unittest.mock import patch
import shutil

with patch("shutil.copy2", side_effect=OSError("Permission denied for backup")):
    from scripts.flip_reparse.reparse_flip import main
    sys.exit(main(sys.argv[1:]))
"""
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    test_script,
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                cwd=str(Path(__file__).resolve().parent.parent),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("BACKUP_ERROR", proc.stderr)

    # T-R08: prod DB hash check in setUpClass + tearDownClass
    def test_r08_prod_db_hash_integrity(self) -> None:
        db_hash = get_prod_db_hash()
        self.assertEqual(db_hash, EXPECTED_PROD_DB_HASH)

    # T-R09: payload_shape_matches_existing
    def test_r09_payload_shape_matches_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            con = sqlite3.connect(str(act_db))
            old_payload = {
                "amount": 50000,
                "tx_type": "Expense",
                "raw_event_id": 101,
                "cursor": "101",
                "cursor_id": 101,
            }
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload, status, synced_at) VALUES (?, ?, ?, ?, ?)",
                (90, "syn_msg_90", json.dumps(old_payload), "STAGED", "2026-05-10 10:00:00"),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 90,
                    "message_id": "syn_msg_90",
                    "from": "no-reply@flip.id",
                    "subject": "Transfer #FT900000000",
                    "body": "Destination Name\nZAYYINNA FITRIANI\nTotal: Rp 75.000",
                    "internal_date": "2026-06-04T05:00:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Apply failed: {proc.stderr}")

            con = sqlite3.connect(str(act_db))
            row = con.execute(
                "SELECT message_id, payload, status FROM edge_synced_messages WHERE message_id = 'syn_msg_90'"
            ).fetchone()
            con.close()

            self.assertIsNotNone(row)
            new_payload = json.loads(row[1])
            self.assertIn("tx_type", new_payload)
            self.assertIn("amount", new_payload)
            self.assertNotIn("candidate", new_payload)
            self.assertEqual(new_payload.get("reparsed_by"), "step-17b-flip-reparse-v1")
            self.assertIn("message_id", new_payload)
            self.assertEqual(new_payload["message_id"], "syn_msg_90")
            self.assertEqual(new_payload.get("raw_event_id"), 101)
            self.assertEqual(new_payload.get("cursor"), "101")
            self.assertEqual(new_payload.get("cursor_id"), 101)
            self.assertEqual(row[2], "REVIEW_REQUIRED")

    # T-R10: synced_at_preserved
    def test_r10_synced_at_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            original_synced_at = "2026-05-15 08:30:00"
            con = sqlite3.connect(str(act_db))
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload, status, synced_at) VALUES (?, ?, ?, ?, ?)",
                (91, "syn_msg_91", json.dumps({"amount": 10000}), "STAGED", original_synced_at),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 91,
                    "message_id": "syn_msg_91",
                    "from": "no-reply@flip.id",
                    "subject": "Transfer #FT910000000",
                    "body": "Destination Name\nZAYYINNA FITRIANI\nTotal: Rp 80.000",
                    "internal_date": "2026-06-04T05:00:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Apply failed: {proc.stderr}")

            con = sqlite3.connect(str(act_db))
            row = con.execute(
                "SELECT synced_at, status FROM edge_synced_messages WHERE message_id = 'syn_msg_91'"
            ).fetchone()
            con.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[0], original_synced_at)
            self.assertEqual(row[1], "REVIEW_REQUIRED")

    # T-R11: after apply on synthetic DB, a NON_TRANSACTION row is DELETED
    def test_r11_non_transaction_row_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            con = sqlite3.connect(str(act_db))
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (110, "syn_msg_non_tx", json.dumps({"amount": 0})),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 110,
                    "message_id": "syn_msg_non_tx",
                    "from": "no-reply@flip.id",
                    "subject": "Flip Coins will expire soon!",
                    "body": "Pemberitahuan koin kedaluwarsa.",
                    "internal_date": "2026-06-04T05:10:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Apply failed: {proc.stderr}")

            # Verify row deleted from DB
            con = sqlite3.connect(str(act_db))
            count = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE id = 110").fetchone()[0]
            con.close()
            self.assertEqual(count, 0)

    # T-R12: after apply on synthetic DB, a FAILED_ATTEMPT row is DELETED
    def test_r12_failed_attempt_row_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            act_db = td / "money_tracks.db"
            init_synthetic_activation_db(act_db)

            con = sqlite3.connect(str(act_db))
            con.execute(
                "INSERT INTO edge_synced_messages (id, message_id, payload) VALUES (?, ?, ?)",
                (120, "syn_msg_failed", json.dumps({"amount": 0})),
            )
            con.commit()
            con.close()

            input_json = td / "input.json"
            messages = [
                {
                    "edge_id": 120,
                    "message_id": "syn_msg_failed",
                    "from": "no-reply@flip.id",
                    "subject": "Transaction Failed to Process FT123999888",
                    "body": "Your transaction cannot be processed.",
                    "internal_date": "2026-06-04T05:10:00.000Z",
                }
            ]
            input_json.write_text(json.dumps({"messages": messages}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    f"--input={input_json}",
                    f"--activation-db={act_db}",
                    f"--prod-db={PROD_DB_PATH}",
                    "--apply",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Apply failed: {proc.stderr}")

            # Verify row deleted from DB
            con = sqlite3.connect(str(act_db))
            count = con.execute("SELECT COUNT(*) FROM edge_synced_messages WHERE id = 120").fetchone()[0]
            con.close()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
