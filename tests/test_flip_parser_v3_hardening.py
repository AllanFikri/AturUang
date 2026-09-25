r"""
Tests for STEP-17A: Flip parser hardening.
- Security-alert blacklist (NON_TRANSACTION).
- SELF transfer detection (OWN_TRANSFER / Internal Transfer / Neutral / SELF).

Coverage:
  T_H01: Security alert subject "Another device is trying to login to your account." -> NON_TRANSACTION, Ignore, Neutral, amount 0, candidate null.
  T_H02: Security alert body match -> NON_TRANSACTION (same).
  T_H03: Flip Coins expire subject -> NON_TRANSACTION (regression check).
  T_H04: #W transfer to "ALLAN FIKRI MAHARDIKA SANTOSA" -> OWN_TRANSFER, Internal Transfer, Neutral, SELF.
  T_H05: #FT transfer to "SOME OTHER PERSON" -> EXTERNAL_TRANSFER, Expense, Debit, OTHER_PERSON (regression check).
  T_H06: #INT transfer to "LEMBAGA MINYAK SAWIT MALAYSIA" -> EXTERNAL_TRANSFER, Expense (regression check).
  T_H07: transfer receipt without Destination Name line -> falls through to default (EXTERNAL_TRANSFER).
  T_H08: owner name with mixed case "Allan Fikri Mahardika Santosa" -> normalized, matches SELF.
  T_H09: owner name with extra whitespace "  ALLAN   FIKRI  MAHARDIKA  SANTOSA  " -> normalized, matches SELF.
  T_H10: event_kind guard: confirm OWN_TRANSFER is in CANONICAL_EVENT_KIND_SET.
  T_H11: prod DB hash check in setUpClass + tearDownClass.
  T_H12: QRIS payment takes precedence over SELF transfer detection.
  T_H13: Refund takes precedence over SELF transfer detection.
  T_H14: normalizeOwnerName helper unit tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import unittest

EXPECTED_PROD_DB_HASH = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")
DOMAIN_TS_PATH = Path(__file__).resolve().parent.parent / "cloud" / "worker" / "src" / "domain.ts"


def get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


def run_node_parse_gmail(
    subject: str,
    body: str,
    from_address: str = "no-reply@flip.id",
    occurred_at: str = "2026-09-24T12:00:00Z",
) -> dict:
    domain_file_uri = DOMAIN_TS_PATH.as_uri()
    script = f"""
import('{domain_file_uri}').then(m => {{
  try {{
    const ev = m.parseGmailIntelligence(
      {json.dumps(subject)},
      {json.dumps(body)},
      {json.dumps(from_address)},
      {json.dumps(occurred_at)}
    );
    process.stdout.write(JSON.stringify(ev));
  }} catch (err) {{
    process.stderr.write(err.message || String(err));
    process.exit(2);
  }}
}}).catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types"],
        input=script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Node script failed: {proc.stderr}")
    return json.loads(proc.stdout)


def run_node_canonical_event_kind_set() -> list[str]:
    domain_file_uri = DOMAIN_TS_PATH.as_uri()
    script = f"""
import('{domain_file_uri}').then(m => {{
  const arr = Array.from(m.CANONICAL_EVENT_KIND_SET);
  process.stdout.write(JSON.stringify(arr));
}}).catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def run_node_normalize_owner_name(name: str | None) -> str:
    domain_file_uri = DOMAIN_TS_PATH.as_uri()
    script = f"""
import('{domain_file_uri}').then(m => {{
  const norm = m.normalizeOwnerName({json.dumps(name)});
  process.stdout.write(JSON.stringify(norm));
}}).catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestFlipParserV3Hardening(unittest.TestCase):
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

    # T_H01: Security alert subject "Another device is trying to login to your account."
    def test_h01_security_alert_subject(self) -> None:
        ev = run_node_parse_gmail(
            subject="Another device is trying to login to your account.",
            body="Halo Pengguna, ada perangkat lain yang mencoba masuk ke akun Flip Anda.",
        )
        self.assertEqual(ev["event_kind"], "NON_TRANSACTION")
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["financial_direction"], "Neutral")
        self.assertEqual(ev["amount"], 0)
        self.assertIsNone(ev["candidate"])
        self.assertEqual(ev["status"], "Ignored")

    # T_H02: Security alert body match
    def test_h02_security_alert_body_match(self) -> None:
        ev = run_node_parse_gmail(
            subject="Pemberitahuan Keamanan Flip",
            body="Penting: A new device is trying to login to your Flip account from Chrome Windows.",
        )
        self.assertEqual(ev["event_kind"], "NON_TRANSACTION")
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["financial_direction"], "Neutral")
        self.assertEqual(ev["amount"], 0)
        self.assertIsNone(ev["candidate"])
        self.assertEqual(ev["status"], "Ignored")

    # T_H03: Flip Coins expire subject regression check
    def test_h03_flip_coins_expire_regression(self) -> None:
        ev = run_node_parse_gmail(
            subject="Your Flip Coins will expire soon!",
            body="Jangan lewatkan kesempatan menggunakan koin Flip Anda sebelum hangus.",
        )
        self.assertEqual(ev["event_kind"], "NON_TRANSACTION")
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["financial_direction"], "Neutral")
        self.assertEqual(ev["amount"], 0)
        self.assertIsNone(ev["candidate"])
        self.assertEqual(ev["status"], "Ignored")

    # T_H04: #W transfer to "ALLAN FIKRI MAHARDIKA SANTOSA" -> OWN_TRANSFER, Internal Transfer, Neutral, SELF
    def test_h04_wallet_transfer_to_owner(self) -> None:
        body = (
            "Transaksi berhasil diproses\n"
            "ID Transaksi: #W123456789\n"
            "Destination Name\n"
            "ALLAN FIKRI MAHARDIKA SANTOSA\n"
            "Total Pembayaran: Rp 150.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Transfer Digital Berhasil #W123456789",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "OWN_TRANSFER")
        self.assertEqual(ev["financial_class"], "Internal Transfer")
        self.assertEqual(ev["financial_direction"], "Neutral")
        self.assertEqual(ev["destination_owner_type"], "SELF")
        self.assertEqual(ev["amount"], 150000)
        self.assertEqual(ev["event_id"], "flip_W123456789")
        self.assertIsNotNone(ev["candidate"])
        self.assertEqual(ev["candidate"]["tx_type"], "Transfer")
        self.assertEqual(ev["candidate"]["category"], "Transfer & Investasi / Transfer ke Teman")
        self.assertEqual(ev["candidate"]["person_name"], "ALLAN FIKRI MAHARDIKA SANTOSA")
        self.assertEqual(ev["candidate"]["status"], "AutoApproved")

    # T_H05: #FT transfer to "SOME OTHER PERSON" -> EXTERNAL_TRANSFER, Expense, Debit, OTHER_PERSON
    def test_h05_transfer_to_other_person(self) -> None:
        body = (
            "Transaksi berhasil diproses\n"
            "ID Transaksi: #FT123456789\n"
            "Destination Name\n"
            "SOME OTHER PERSON\n"
            "Total: Rp 50.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Transfer Berhasil #FT123456789",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "EXTERNAL_TRANSFER")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["financial_direction"], "Debit")
        self.assertEqual(ev["destination_owner_type"], "OTHER_PERSON")
        self.assertEqual(ev["amount"], 50000)
        self.assertIsNotNone(ev["candidate"])
        self.assertEqual(ev["candidate"]["tx_type"], "Transfer")

    # T_H06: #INT transfer to "LEMBAGA MINYAK SAWIT MALAYSIA" -> EXTERNAL_TRANSFER, Expense
    def test_h06_international_transfer_regression(self) -> None:
        body = (
            "Transfer internasional berhasil diproses\n"
            "ID Transaksi: #INT1234567\n"
            "Beneficiary Name\n"
            "LEMBAGA MINYAK SAWIT MALAYSIA\n"
            "Jumlah: Rp 500.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Transfer to Malaysia #INT1234567",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "EXTERNAL_TRANSFER")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["financial_direction"], "Debit")
        self.assertEqual(ev["destination_owner_type"], "OTHER_PERSON")
        self.assertEqual(ev["amount"], 500000)
        self.assertEqual(ev["candidate"]["category"], "Lain-lain / Lab Equipment")

    # T_H07: transfer receipt without Destination Name line -> falls through to default (EXTERNAL_TRANSFER)
    def test_h07_transfer_without_destination_name(self) -> None:
        body = (
            "Transaksi berhasil diproses\n"
            "ID Transaksi: #FT998877665\n"
            "Jumlah transfer: Rp 75.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Transfer Berhasil #FT998877665",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "EXTERNAL_TRANSFER")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["financial_direction"], "Debit")
        self.assertEqual(ev["destination_owner_type"], "UNKNOWN")

    # T_H08: owner name with mixed case "Allan Fikri Mahardika Santosa" -> normalized, matches SELF
    def test_h08_owner_name_mixed_case(self) -> None:
        body = (
            "Transaksi berhasil diproses\n"
            "ID Transaksi: #W987654321\n"
            "Destination Name\n"
            "Allan Fikri Mahardika Santosa\n"
            "Nominal: Rp 200.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Transfer Digital Berhasil #W987654321",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "OWN_TRANSFER")
        self.assertEqual(ev["financial_class"], "Internal Transfer")
        self.assertEqual(ev["financial_direction"], "Neutral")
        self.assertEqual(ev["destination_owner_type"], "SELF")

    # T_H09: owner name with extra whitespace "  ALLAN   FIKRI  MAHARDIKA  SANTOSA  " -> normalized, matches SELF
    def test_h09_owner_name_extra_whitespace(self) -> None:
        body = (
            "Transaksi berhasil diproses\n"
            "ID Transaksi: #W556677889\n"
            "Destination Name\n"
            "  ALLAN   FIKRI  MAHARDIKA  SANTOSA  \n"
            "Nominal: Rp 300.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Transfer Digital Berhasil #W556677889",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "OWN_TRANSFER")
        self.assertEqual(ev["financial_class"], "Internal Transfer")
        self.assertEqual(ev["financial_direction"], "Neutral")
        self.assertEqual(ev["destination_owner_type"], "SELF")

    # T_H10: event_kind guard: confirm OWN_TRANSFER is in CANONICAL_EVENT_KIND_SET
    def test_h10_own_transfer_in_canonical_event_kind_set(self) -> None:
        kinds = run_node_canonical_event_kind_set()
        self.assertIn("OWN_TRANSFER", kinds)
        self.assertIn("NON_TRANSACTION", kinds)
        self.assertEqual(len(kinds), 14)

    # T_H11: prod DB hash check
    def test_h11_prod_db_hash_check(self) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            self.assertEqual(db_hash, EXPECTED_PROD_DB_HASH)

    # T_H12: QRIS payment takes precedence over SELF transfer detection
    def test_h12_qris_takes_precedence_over_self(self) -> None:
        body = (
            "Pembayaran QRIS berhasil\n"
            "ID Transaksi: #QT-24112621595515247830\n"
            "Destination Name\n"
            "ALLAN FIKRI MAHARDIKA SANTOSA\n"
            "Total Bayar: Rp 25.000\n"
        )
        ev = run_node_parse_gmail(
            subject="QRIS Payment #QT-24112621595515247830",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "MERCHANT_PAYMENT")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["financial_direction"], "Debit")

    # T_H13: Refund takes precedence over SELF transfer detection
    def test_h13_refund_takes_precedence_over_self(self) -> None:
        body = (
            "Refund dana berhasil diproses\n"
            "ID Transaksi: #R12345678\n"
            "Destination Name\n"
            "ALLAN FIKRI MAHARDIKA SANTOSA\n"
            "Total Pengembalian Dana: Rp 45.000\n"
        )
        ev = run_node_parse_gmail(
            subject="Pengembalian Dana #R12345678",
            body=body,
        )
        self.assertEqual(ev["event_kind"], "REFUND")
        self.assertEqual(ev["financial_class"], "Refund")
        self.assertEqual(ev["financial_direction"], "Credit")

    # T_H14: normalizeOwnerName helper unit tests
    def test_h14_normalize_owner_name_helper(self) -> None:
        self.assertEqual(run_node_normalize_owner_name(""), "")
        self.assertEqual(run_node_normalize_owner_name(None), "")
        self.assertEqual(
            run_node_normalize_owner_name("allan fikri mahardika santosa"),
            "ALLAN FIKRI MAHARDIKA SANTOSA",
        )
        self.assertEqual(
            run_node_normalize_owner_name("  ALLAN   FIKRI  MAHARDIKA  SANTOSA  "),
            "ALLAN FIKRI MAHARDIKA SANTOSA",
        )
        self.assertEqual(
            run_node_normalize_owner_name("Allan, Fikri: Mahardika - Santosa!"),
            "ALLAN FIKRI MAHARDIKA SANTOSA",
        )


if __name__ == "__main__":
    unittest.main()
