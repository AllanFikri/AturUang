r"""
Tests for STEP-17C: Flip parser request hardening and Kode Unik guard.
- Blacklist "Transaction Failed to Process" (FAILED_ATTEMPT).
- Handle "Informasi transfer" request emails (Pending / pure transfer amount from first "Jumlah Transfer").
- Kode Unik amount extraction guard.

Coverage:
  T-RH01: "Transaction Failed to Process FT..." subject + body -> FAILED_ATTEMPT, Ignore, Neutral, amount 0, candidate null.
  T-RH02: Body-only match of "TRANSACTION FAILED TO PROCESS" -> FAILED_ATTEMPT.
  T-RH03: "Informasi transfer ke <name>" request email -> amount from FIRST "Jumlah Transfer", not Kode Unik.
  T-RH04: Request email to owner (destination name == OWNER_NAMES) -> OWN_TRANSFER, Internal Transfer, SELF.
  T-RH05: General amount guard: body with only "Kode Unik Rp200" and no "Jumlah Transfer" -> no amount from Kode Unik (!= 200).
  T-RH06: Receipt email (TRANSFER RECEIPT + Amount Rp250.000) still parses with amount 250000 (regression).
  T-RH07: SELF receipt regression (from STEP-17A): destination = OWNER_NAMES -> OWN_TRANSFER.
  T-RH08: event_kind guard: FAILED_ATTEMPT is in CANONICAL_EVENT_KIND_SET.
  T-RH09: prod DB hash check in setUpClass + tearDownClass.
  T-RH10: Flip Coins expire subject still produces NON_TRANSACTION (regression).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import unittest

EXPECTED_PROD_DB_HASH = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
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
    occurred_at: str = "2026-09-25T10:00:00Z",
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


class TestFlipParserV4RequestHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            assert db_hash == EXPECTED_PROD_DB_HASH, f"Production DB corrupted before tests: {db_hash}"

    @classmethod
    def tearDownClass(cls) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            assert db_hash == EXPECTED_PROD_DB_HASH, f"Production DB corrupted after tests: {db_hash}"

    # T-RH01: "Transaction Failed to Process FT..." subject + body -> FAILED_ATTEMPT, Ignore, Neutral, amount 0, candidate null
    def test_trh01_failed_attempt_subject_and_body(self) -> None:
        subject = "Transaction Failed to Process FT998877112"
        body = "Synthetic notification: Your transaction cannot be processed at this time."
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("event_kind"), "FAILED_ATTEMPT")
        self.assertEqual(ev.get("financial_class"), "Ignore")
        self.assertEqual(ev.get("financial_direction"), "Neutral")
        self.assertEqual(ev.get("amount"), 0)
        self.assertIsNone(ev.get("candidate"))
        self.assertEqual(ev.get("status"), "Ignored")

    # T-RH02: Body-only match of "TRANSACTION FAILED TO PROCESS" -> FAILED_ATTEMPT
    def test_trh02_failed_attempt_body_only(self) -> None:
        subject = "Pemberitahuan Flip #FT998877113"
        body = "TRANSACTION FAILED TO PROCESS\nMohon maaf transfer tidak dapat diteruskan."
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("event_kind"), "FAILED_ATTEMPT")
        self.assertEqual(ev.get("financial_class"), "Ignore")
        self.assertEqual(ev.get("financial_direction"), "Neutral")
        self.assertEqual(ev.get("amount"), 0)
        self.assertIsNone(ev.get("candidate"))
        self.assertEqual(ev.get("status"), "Ignored")

    # T-RH03: "Informasi transfer ke <name>" request email -> amount from FIRST "Jumlah Transfer", not Kode Unik
    def test_trh03_request_email_amount_extraction(self) -> None:
        subject = "Informasi transfer ke SYNTHETIC OTHER PERSON"
        body = (
            "INFORMASI TRANSAKSI\n"
            "ID Transaksi\n"
            "#FT999000111\n"
            "Tujuan\n"
            "SYNTHETIC OTHER PERSON\n"
            "Jumlah Transfer\n"
            "Rp42.500\n"
            "Kode Unik*\n"
            "Rp200\n"
            "*Kode unik sebesar Rp200 juga akan masuk...\n"
            "Jumlah Transfer\n"
            "Rp42.700\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 42500)
        self.assertEqual(ev.get("event_kind"), "EXTERNAL_TRANSFER")
        self.assertEqual(ev.get("financial_class"), "Expense")
        self.assertEqual(ev.get("destination_owner_type"), "OTHER_PERSON")
        self.assertEqual(ev.get("financial_direction"), "Debit")
        self.assertEqual(ev.get("status"), "Pending")
        self.assertEqual(ev.get("confidence"), 0.75)
        self.assertEqual(ev.get("transaction_reference"), "#FT999000111")

        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.get("amount"), 42500)
        self.assertEqual(cand.get("status"), "Pending")
        self.assertEqual(cand.get("confidence_score"), 0.75)
        self.assertEqual(cand.get("person_name"), "SYNTHETIC OTHER PERSON")

    # T-RH04: Request email to owner (destination name == OWNER_NAMES) -> OWN_TRANSFER, Internal Transfer, SELF
    def test_trh04_request_email_to_owner_own_transfer(self) -> None:
        subject = "Informasi transfer ke ALLAN FIKRI MAHARDIKA SANTOSA"
        body = (
            "INFORMASI TRANSAKSI\n"
            "ID Transaksi\n"
            "#FT999000222\n"
            "Tujuan\n"
            "ALLAN FIKRI MAHARDIKA SANTOSA\n"
            "Jumlah Transfer\n"
            "Rp150.000\n"
            "Kode Unik*\n"
            "Rp123\n"
            "*Kode unik sebesar Rp123...\n"
            "Jumlah Transfer\n"
            "Rp150.123\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 150000)
        self.assertEqual(ev.get("event_kind"), "OWN_TRANSFER")
        self.assertEqual(ev.get("financial_class"), "Internal Transfer")
        self.assertEqual(ev.get("destination_owner_type"), "SELF")
        self.assertEqual(ev.get("financial_direction"), "Neutral")
        self.assertEqual(ev.get("status"), "Pending")
        self.assertEqual(ev.get("confidence"), 0.75)

        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.get("tx_type"), "Transfer")
        self.assertEqual(cand.get("amount"), 150000)
        self.assertEqual(cand.get("status"), "Pending")

    # T-RH05: General amount guard: a body with only "Kode Unik Rp200" and no "Jumlah Transfer" -> no amount from Kode Unik (!= 200)
    def test_trh05_general_amount_guard_kode_unik(self) -> None:
        subject = "Pemberitahuan Flip #FT999000333"
        body = "Kode Unik Rp200\nMohon selesaikan transfer."
        ev = run_node_parse_gmail(subject, body)

        # Must NOT extract 200 as amount
        self.assertNotEqual(ev.get("amount"), 200)
        self.assertEqual(ev.get("amount"), 0)
        self.assertIsNone(ev.get("candidate"))

    # T-RH06: Receipt email (TRANSFER RECEIPT + Amount Rp250.000) still parses with amount 250000 (regression)
    def test_trh06_receipt_email_regression(self) -> None:
        subject = "Transfer Digital #FT112233445 Berhasil"
        body = (
            "TRANSFER RECEIPT\n"
            "Destination Name\n"
            "SYNTHETIC RECIPIENT PERSON\n"
            "Amount Rp250.000\n"
            "Total Amount: Rp 250.000\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 250000)
        self.assertEqual(ev.get("event_kind"), "EXTERNAL_TRANSFER")
        self.assertEqual(ev.get("financial_class"), "Expense")
        self.assertEqual(ev.get("status"), "AutoApproved")
        self.assertEqual(ev.get("confidence"), 0.95)

    # T-RH07: SELF receipt regression (from STEP-17A): destination = OWNER_NAMES -> OWN_TRANSFER
    def test_trh07_self_receipt_regression(self) -> None:
        subject = "Transfer Digital #W998877665"
        body = (
            "Destination Name\n"
            "ALLAN FIKRI MAHARDIKA SANTOSA\n"
            "Total: Rp 300.000\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 300000)
        self.assertEqual(ev.get("event_kind"), "OWN_TRANSFER")
        self.assertEqual(ev.get("financial_class"), "Internal Transfer")
        self.assertEqual(ev.get("destination_owner_type"), "SELF")
        self.assertEqual(ev.get("financial_direction"), "Neutral")
        self.assertEqual(ev.get("status"), "AutoApproved")

    # T-RH08: event_kind guard: FAILED_ATTEMPT is in CANONICAL_EVENT_KIND_SET
    def test_trh08_canonical_event_kind_set_has_failed_attempt(self) -> None:
        kinds = run_node_canonical_event_kind_set()
        self.assertIn("FAILED_ATTEMPT", kinds)
        self.assertIn("NON_TRANSACTION", kinds)
        self.assertIn("OWN_TRANSFER", kinds)
        self.assertIn("EXTERNAL_TRANSFER", kinds)
        self.assertEqual(len(kinds), 14)

    # T-RH09: prod DB hash check in setUpClass + tearDownClass
    def test_trh09_prod_db_hash_integrity(self) -> None:
        db_hash = get_prod_db_hash()
        self.assertEqual(db_hash, EXPECTED_PROD_DB_HASH)

    # T-RH10: Flip Coins expire subject still produces NON_TRANSACTION (regression)
    def test_trh10_flip_coins_expire_regression(self) -> None:
        subject = "Flip Coins will expire soon!"
        body = "Synthetic notification: Your coin balance will expire on 30 June."
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("event_kind"), "NON_TRANSACTION")
        self.assertEqual(ev.get("financial_class"), "Ignore")
        self.assertEqual(ev.get("amount"), 0)
        self.assertIsNone(ev.get("candidate"))

    # T-RH11: "Informasi Transaksi #FT..." subject, body containing "Nama Penerima" + "Nominal\nRp1.500.000" + "Kode Unik*\nRp385" + second "Nominal\nRp1.500.385" -> amount == 1500000, event_kind == "EXTERNAL_TRANSFER", status == "Pending"
    def test_trh11_informasi_transaksi_nominal_nama_penerima(self) -> None:
        subject = "Informasi Transaksi #FT123456789"
        body = (
            "INFORMASI TRANSAKSI\n"
            "ID Transaksi\n"
            "#FT123456789\n"
            "Nama Penerima\n"
            "SYNTHETIC EXTERNAL PERSON\n"
            "Nominal\n"
            "Rp1.500.000\n"
            "Kode Unik*\n"
            "Rp385\n"
            "*Kode unik sebesar Rp385...\n"
            "Nominal\n"
            "Rp1.500.385\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 1500000)
        self.assertEqual(ev.get("event_kind"), "EXTERNAL_TRANSFER")
        self.assertEqual(ev.get("financial_class"), "Expense")
        self.assertEqual(ev.get("destination_owner_type"), "OTHER_PERSON")
        self.assertEqual(ev.get("status"), "Pending")
        self.assertEqual(ev.get("confidence"), 0.75)
        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.get("amount"), 1500000)
        self.assertEqual(cand.get("person_name"), "SYNTHETIC EXTERNAL PERSON")

    # T-RH12: "Informasi Top Up #FT..." subject, body containing "#TUFT608906177" + "Nominal\nRp15.000" + "Kode Unik*\nRp238" -> amount == 15000, event_kind == "TOPUP", financial_class == "Top-up", financial_direction == "Debit", status == "Pending"
    def test_trh12_informasi_topup_tuft_prefix(self) -> None:
        subject = "Informasi Top Up #FT608906177"
        body = (
            "INFORMASI TRANSAKSI\n"
            "ID Transaksi\n"
            "#TUFT608906177\n"
            "Nominal\n"
            "Rp15.000\n"
            "Kode Unik*\n"
            "Rp238\n"
            "*Kode unik sebesar Rp238...\n"
            "Nominal\n"
            "Rp15.238\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 15000)
        self.assertEqual(ev.get("event_kind"), "TOPUP")
        self.assertEqual(ev.get("financial_class"), "Top-up")
        self.assertEqual(ev.get("financial_direction"), "Debit")
        self.assertEqual(ev.get("destination_owner_type"), "SELF")
        self.assertEqual(ev.get("status"), "Pending")
        self.assertEqual(ev.get("confidence"), 0.75)
        self.assertEqual(ev.get("transaction_reference"), "#FT608906177")
        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.get("tx_type"), "Top Up")
        self.assertEqual(cand.get("category"), "Other / Miscellaneous")
        self.assertEqual(cand.get("amount"), 15000)

    # T-RH13: "Informasi Transaksi #FT..." subject, body with "Nama Penerima\nALLAN FIKRI MAHARDIKA SANTOSA" + "Nominal\nRp30.000" -> amount == 30000, event_kind == "OWN_TRANSFER", destination_owner_type == "SELF"
    def test_trh13_informasi_transaksi_owner_self(self) -> None:
        subject = "Informasi Transaksi #FT998811223"
        body = (
            "INFORMASI TRANSAKSI\n"
            "ID Transaksi\n"
            "#FT998811223\n"
            "Nama Penerima\n"
            "ALLAN FIKRI MAHARDIKA SANTOSA\n"
            "Nominal\n"
            "Rp30.000\n"
            "Kode Unik*\n"
            "Rp111\n"
            "Nominal\n"
            "Rp30.111\n"
        )
        ev = run_node_parse_gmail(subject, body)

        self.assertEqual(ev.get("amount"), 30000)
        self.assertEqual(ev.get("event_kind"), "OWN_TRANSFER")
        self.assertEqual(ev.get("financial_class"), "Internal Transfer")
        self.assertEqual(ev.get("destination_owner_type"), "SELF")
        self.assertEqual(ev.get("financial_direction"), "Neutral")
        self.assertEqual(ev.get("status"), "Pending")
        self.assertEqual(ev.get("confidence"), 0.75)
        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.get("amount"), 30000)


if __name__ == "__main__":
    unittest.main()
