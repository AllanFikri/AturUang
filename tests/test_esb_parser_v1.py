r"""
Tests for STEP-16A: Fail-closed ESB parser (no-reply@mailer-esb.com).

Requirements:
  T01: Variant A body, merchant extractable, order_id present -> AutoApproved, MERCHANT_PAYMENT, merchant_normalized correct.
  T02: Variant A body, merchant extractable, order_id absent -> Pending, MERCHANT_PAYMENT, confidence 0.70.
  T03: Variant B body (no merchant block), subject [E-Receipt] <merchant> -> merchant from subject priority 2.
  T04: Variant B body, subject Receipt from <merchant> -> merchant from subject priority 3.
  T05: Body merchant line matches reject token (Order Details, Customer Name, etc.) -> falls through to subject.
  T06: Body and subject both lack merchant -> status=Pending, destination_owner_type=UNKNOWN, review_reason set, confidence=0.50.
  T07: Amount extraction yields exact decimal, no float artifacts.
  T08: event_kind is canonical MERCHANT_PAYMENT.
  T09: event_id format with order_id -> esb_<ORDER_ID>.
  T10: event_id format without order_id -> esb_noref_<16 hex chars>.
  T11: transaction_reference == order_id and external_order_id == order_id when present; None when absent.
  T12: Prod DB hash check in setUpClass and tearDownClass (341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07).
  T13: Body < 50 chars -> Pending, review_reason="ESB body too short to parse.", confidence=0.30.
  T14: Amount unextractable -> Pending, review_reason="ESB amount not extractable.", confidence=0.30.
  T15: Tolerance to rewritten subject (e.g. "Transaksi 0a1b2c3d") with Variant A body -> extracts merchant from body.
  T16: parseGmailIntelligence routing with cleanFrom variants.
  T17: CANONICAL_EVENT_KIND_SET integrity check (14 elements).
  T18: All 6 reject tokens fall through to subject.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest

EXPECTED_PROD_DB_HASH = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")
DOMAIN_TS_PATH = Path(__file__).resolve().parent.parent / "cloud" / "worker" / "src" / "domain.ts"


def get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


def run_node_parse_esb(
    subject: str,
    body: str,
    from_address: str = "no-reply@mailer-esb.com",
    occurred_at: str = "2026-09-24T12:00:00Z",
) -> dict:
    domain_file_uri = DOMAIN_TS_PATH.as_uri()
    script = f"""
import('{domain_file_uri}').then(m => {{
  try {{
    const ev = m.parseEsbReceipt(
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
    if proc.returncode == 2:
        raise ValueError(proc.stderr.strip())
    if proc.returncode != 0:
        raise RuntimeError(f"Node script failed: {proc.stderr}")
    return json.loads(proc.stdout)


def run_node_parse_gmail(
    subject: str,
    body: str,
    from_address: str = "no-reply@mailer-esb.com",
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


class TestEsbParserV1(unittest.TestCase):
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

    def test_01_variant_a_body_merchant_with_order_id(self) -> None:
        body = (
            "*Sushi Tei Grand Indonesia*\n"
            "Phone Number : 02123580000\n\n"
            "#Order\n"
            "*ST987654321* 24-09-2026\n\n"
            "Detail Pesanan:\n"
            "Total Spent\n"
            "* Rp 275.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res = run_node_parse_esb("[E-Receipt] Transaksi Anda", body)
        self.assertEqual(res["status"], "AutoApproved")
        self.assertEqual(res["confidence"], 0.92)
        self.assertEqual(res["event_kind"], "MERCHANT_PAYMENT")
        self.assertEqual(res["financial_class"], "Expense")
        self.assertEqual(res["financial_direction"], "Debit")
        self.assertEqual(res["destination_owner_type"], "MERCHANT")
        self.assertEqual(res["merchant_normalized"], "Sushi Tei Grand Indonesia")
        self.assertEqual(res["counterparty_normalized"], "Sushi Tei Grand Indonesia")
        self.assertEqual(res["amount"], 275000)
        self.assertEqual(res["transaction_reference"], "ST987654321")
        self.assertEqual(res["external_order_id"], "ST987654321")
        self.assertEqual(res["event_id"], "esb_ST987654321")
        self.assertIsNone(res["review_reason"])
        self.assertIsNotNone(res["candidate"])
        self.assertEqual(res["candidate"]["status"], "AutoApproved")
        self.assertEqual(res["candidate"]["tx_type"], "Expense")
        self.assertIsNone(res["candidate"]["account"])
        self.assertEqual(res["candidate"]["category"], "Other / Miscellaneous")
        self.assertEqual(res["candidate"]["money_context"], "Personal")
        self.assertEqual(res["candidate"]["person_name"], "Sushi Tei Grand Indonesia")
        self.assertEqual(res["candidate"]["amount"], 275000)

    def test_02_variant_a_body_merchant_without_order_id(self) -> None:
        body = (
            "*Bakmi GM Plaza Senayan*\n"
            "Phone Number : 0215725000\n\n"
            "Total Spent\n"
            "* Rp 110.000 *\n"
            "Terima kasih atas kunjungan Anda di restoran kami!"
        )
        res = run_node_parse_esb("[E-Receipt] Transaksi Anda", body)
        self.assertEqual(res["status"], "Pending")
        self.assertEqual(res["confidence"], 0.70)
        self.assertEqual(res["event_kind"], "MERCHANT_PAYMENT")
        self.assertEqual(res["merchant_normalized"], "Bakmi GM Plaza Senayan")
        self.assertEqual(res["amount"], 110000)
        self.assertIsNone(res["transaction_reference"])
        self.assertIsNone(res["external_order_id"])
        self.assertTrue(res["event_id"].startswith("esb_noref_"))
        self.assertEqual(len(res["event_id"]), len("esb_noref_") + 16)
        self.assertIsNotNone(res["candidate"])
        self.assertEqual(res["candidate"]["status"], "Pending")

    def test_03_variant_b_merchant_from_subject_e_receipt(self) -> None:
        body = (
            "Terima kasih telah berkunjung ke restoran kami.\n"
            "#Order\n"
            "*KK11223344* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 48.000 *\n"
            "Semoga hari Anda menyenangkan."
        )
        res = run_node_parse_esb("[E-Receipt] Kopi Kenangan Senopati", body)
        self.assertEqual(res["merchant_normalized"], "Kopi Kenangan Senopati")
        self.assertEqual(res["status"], "AutoApproved")
        self.assertEqual(res["event_id"], "esb_KK11223344")

    def test_04_variant_b_merchant_from_subject_receipt_from(self) -> None:
        body = (
            "Terima kasih telah berkunjung ke outlet kami.\n"
            "#Order\n"
            "*FC99887766* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 35.000 *\n"
            "Semoga hari Anda menyenangkan."
        )
        res = run_node_parse_esb("Receipt from Fore Coffee Pacific Place", body)
        self.assertEqual(res["merchant_normalized"], "Fore Coffee Pacific Place")
        self.assertEqual(res["status"], "AutoApproved")
        self.assertEqual(res["event_id"], "esb_FC99887766")

    def test_05_body_matches_reject_token_falls_through_to_subject(self) -> None:
        body = (
            "*Order Details*\n"
            "Phone Number : 0812345678\n\n"
            "#Order\n"
            "*OD12345678* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 50.000 *\n"
            "Terima kasih atas pesanan Anda di resto kami!"
        )
        res = run_node_parse_esb("[E-Receipt] Ramen Seirock-Ya", body)
        self.assertEqual(res["merchant_normalized"], "Ramen Seirock-Ya")
        self.assertEqual(res["status"], "AutoApproved")

    def test_06_body_and_subject_both_lack_merchant(self) -> None:
        body = (
            "Terima kasih atas pesanan Anda.\n"
            "#Order\n"
            "*NO12345678* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 75.000 *\n"
            "Pesanan Anda telah selesai diproses."
        )
        res = run_node_parse_esb("E-Receipt Notification", body)
        self.assertEqual(res["status"], "Pending")
        self.assertEqual(res["confidence"], 0.50)
        self.assertEqual(res["destination_owner_type"], "UNKNOWN")
        self.assertEqual(res["merchant_normalized"], "ESB")
        self.assertEqual(res["review_reason"], "ESB receipt without extractable merchant name.")

    def test_07_amount_exact_decimal_no_float_artifacts(self) -> None:
        body = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "#Order\n"
            "*KB12345678* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 123.456,78 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body)
        self.assertEqual(res["amount"], 123456.78)
        self.assertEqual(res["candidate"]["amount"], 123456.78)

    def test_08_event_kind_is_canonical(self) -> None:
        body = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "#Order\n"
            "*KB12345678* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 45.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body)
        self.assertEqual(res["event_kind"], "MERCHANT_PAYMENT")
        canonical_kinds = run_node_canonical_event_kind_set()
        self.assertIn("MERCHANT_PAYMENT", canonical_kinds)

    def test_09_event_id_format_with_order_id(self) -> None:
        body = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "#Order\n"
            "*ORD998877* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 45.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body)
        self.assertEqual(res["event_id"], "esb_ORD998877")

    def test_10_event_id_format_without_order_id(self) -> None:
        body = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "Total Spent\n"
            "* Rp 45.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res1 = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body, occurred_at="2026-09-24T12:00:00Z")
        res2 = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body, occurred_at="2026-09-24T12:00:00Z")
        self.assertTrue(re.match(r"^esb_noref_[0-9a-f]{16}$", res1["event_id"]))
        self.assertEqual(res1["event_id"], res2["event_id"])

    def test_11_transaction_reference_and_external_order_id(self) -> None:
        body_with_ref = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "#Order\n"
            "*REF123456* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 45.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res1 = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body_with_ref)
        self.assertEqual(res1["transaction_reference"], "REF123456")
        self.assertEqual(res1["external_order_id"], "REF123456")

        body_without_ref = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "Total Spent\n"
            "* Rp 45.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res2 = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body_without_ref)
        self.assertIsNone(res2["transaction_reference"])
        self.assertIsNone(res2["external_order_id"])

    def test_12_body_too_short(self) -> None:
        res = run_node_parse_esb("[E-Receipt] Short", "Short body < 50 chars")
        self.assertEqual(res["status"], "Pending")
        self.assertEqual(res["confidence"], 0.30)
        self.assertEqual(res["review_reason"], "ESB body too short to parse.")
        self.assertEqual(res["destination_owner_type"], "UNKNOWN")
        self.assertEqual(res["amount"], 0)
        self.assertIsNone(res["candidate"])

    def test_13_amount_unextractable(self) -> None:
        body = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "#Order\n"
            "*KB12345678* 24-09-2026\n\n"
            "Terima kasih atas kunjungan Anda hari ini ke outlet kami!"
        )
        res = run_node_parse_esb("[E-Receipt] Kopi Kenangan", body)
        self.assertEqual(res["status"], "Pending")
        self.assertEqual(res["confidence"], 0.30)
        self.assertEqual(res["review_reason"], "ESB amount not extractable.")
        self.assertEqual(res["destination_owner_type"], "MERCHANT")
        self.assertEqual(res["merchant_normalized"], "Kopi Kenangan Senopati")
        self.assertEqual(res["amount"], 0)
        self.assertIsNone(res["candidate"])

    def test_14_tolerance_to_rewritten_subject(self) -> None:
        body = (
            "*Gyu-Kaku Mall Kelapa Gading*\n"
            "Phone Number : 02145853000\n\n"
            "#Order\n"
            "*GK55667788* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 420.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        res = run_node_parse_esb("Transaksi 0a1b2c3d", body)
        self.assertEqual(res["merchant_normalized"], "Gyu-Kaku Mall Kelapa Gading")
        self.assertEqual(res["status"], "AutoApproved")
        self.assertEqual(res["event_id"], "esb_GK55667788")

    def test_15_parse_gmail_intelligence_routing(self) -> None:
        body = (
            "*Kopi Kenangan Senopati*\n"
            "Phone Number : 081234567890\n\n"
            "#Order\n"
            "*KB12345678* 24-09-2026\n\n"
            "Total Spent\n"
            "* Rp 45.000 *\n"
            "Terima kasih atas pesanan Anda di ESB Resto!"
        )
        senders = [
            "no-reply@mailer-esb.com",
            "ESB Resto <no-reply@mailer-esb.com>",
            "no-reply@mailer-esb.com",
            "system@mailer-esb.com",
        ]
        for sender in senders:
            with self.subTest(sender=sender):
                res = run_node_parse_gmail("[E-Receipt] Kopi Kenangan", body, from_address=sender)
                self.assertEqual(res["event_id"], "esb_KB12345678")
                self.assertEqual(res["merchant_normalized"], "Kopi Kenangan Senopati")
                self.assertEqual(res["event_kind"], "MERCHANT_PAYMENT")

    def test_16_canonical_event_kind_set_integrity(self) -> None:
        kinds = run_node_canonical_event_kind_set()
        expected = [
            "MERCHANT_PAYMENT",
            "EXTERNAL_TRANSFER",
            "INCOMING_TRANSFER",
            "OWN_TRANSFER",
            "TOPUP",
            "CASH_WITHDRAWAL",
            "ALLOCATION_MOVEMENT",
            "INVESTMENT_MOVEMENT",
            "REFUND",
            "SUBSCRIPTION_CHARGE",
            "DIGITAL_PURCHASE",
            "INVOICE_EVIDENCE",
            "FAILED_ATTEMPT",
            "NON_TRANSACTION",
        ]
        self.assertEqual(len(kinds), 14)
        for k in expected:
            self.assertIn(k, kinds)

    def test_17_reject_tokens_all_six(self) -> None:
        reject_tokens = [
            "Total Spent",
            "Order Details",
            "Order Information",
            "Customer Name",
            "Membership",
            "Table Number",
        ]
        for tok in reject_tokens:
            with self.subTest(tok=tok):
                body = (
                    f"*{tok}*\n"
                    "Phone Number : 0812345678\n\n"
                    "#Order\n"
                    "*REJ123456* 24-09-2026\n\n"
                    "Total Spent\n"
                    "* Rp 50.000 *\n"
                    "Terima kasih atas pesanan Anda di restoran kami!"
                )
                res = run_node_parse_esb("[E-Receipt] Bakerzin Plaza Indonesia", body)
                self.assertEqual(res["merchant_normalized"], "Bakerzin Plaza Indonesia")
                self.assertEqual(res["status"], "AutoApproved")


if __name__ == "__main__":
    unittest.main()
