r"""
Tests for STEP-16B: FIX-FLIP-PARSER-AND-DEDUP-V1.

Requirements:
   1. extractFlipReferenceId("#FT123456789" in body)
      -> {id:"#FT123456789", kind:"flip_transfer"}
   2. #W123456789 -> flip_transfer_digital
   3. #R12345678 -> flip_transfer_or_refund
   4. #INT1234567 -> flip_international
   5. #BT12345678 -> flip_bulk
   6. #QT-24112621595515247830 -> flip_qris
   7. FT123456789 (tanpa #) -> flip_fallback
   8. No ID -> {id:null, kind:null}
   9. Blacklist #TU -> financial_class Ignore
  10. Blacklist "Koin Flip" subject -> Ignore
  11. Blacklist hello@flip.id -> Ignore
  12. Blacklist "TRANSAKSI GAGAL DIPROSES" -> Ignore, FAILED_ATTEMPT
  13. Transfer normal -> event_kind EXTERNAL_TRANSFER, status AutoApproved
  14. QRIS -> event_kind MERCHANT_PAYMENT, category Other / Miscellaneous (rules classify later)
  15. Malaysia -> event_kind INTERNATIONAL_PURCHASE, category Lab Equipment
  16. Refund -> REFUND_REVERSAL, direction Credit, is_reversal True
  17. Bulk -> event_kind BULK_TRANSFER
  18. transaction_reference == external_order_id == extracted ID, event_id strips '#'
  19. Two emails same ID -> same event_id
  20. No ID -> status Pending
  21. description_normalized format: r"^Flip [A-Z_]+ #[A-Z0-9\-]+$"
  22. description_normalized contains no newline or control chars
  23. description_normalized stable across runs
  24. Determinism for unreferenced email, stable SHA-256 event_id without timestamp
  25. Production DB unchanged across tests
  26. Refund reversal properties
  27. QRIS without merchant name -> Pending, confidence 0.60
  28. Event ID has no '#' symbol across all reference formats
  29. Noref event_id is deterministic SHA-256 and identical across repeated executions
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest

EXPECTED_PROD_DB_HASH = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")
DOMAIN_TS_PATH = Path(__file__).resolve().parent.parent / "cloud" / "worker" / "src" / "domain.ts"


def get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


def get_extracted_flip_helper() -> str:
    code = DOMAIN_TS_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"(function extractFlipReferenceId[\s\S]*?return \{\s*id: null,\s*kind: null\s*\};\s*\})",
        code,
    )
    if not m:
        raise RuntimeError("extractFlipReferenceId not found in domain.ts")
    return m.group(1)


def run_node_extract_ref(subject: str, body: str) -> dict:
    helper_code = get_extracted_flip_helper()
    script = f"""
{helper_code}
const res = extractFlipReferenceId({json.dumps(subject)}, {json.dumps(body)});
process.stdout.write(JSON.stringify(res));
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def run_node_parse_gmail(
    subject: str,
    body: str,
    from_address: str = "no-reply@flip.id",
    occurred_at: str = "2026-09-24T12:00:00Z",
) -> dict:
    domain_file_uri = DOMAIN_TS_PATH.as_uri()
    script = f"""
import('{domain_file_uri}').then(m => {{
  const ev = m.parseGmailIntelligence(
    {json.dumps(subject)},
    {json.dumps(body)},
    {json.dumps(from_address)},
    {json.dumps(occurred_at)}
  );
  process.stdout.write(JSON.stringify(ev));
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


class TestFlipParserV2(unittest.TestCase):
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

    # 1. extractFlipReferenceId("#FT123456789" in body) -> {id:"#FT123456789", kind:"flip_transfer"}
    def test_01_extract_ref_ft(self) -> None:
        res = run_node_extract_ref("", "Transaksi berhasil #FT123456789 selesai.")
        self.assertEqual(res, {"id": "#FT123456789", "kind": "flip_transfer"})

    # 2. #W123456789 -> flip_transfer_digital
    def test_02_extract_ref_w(self) -> None:
        res = run_node_extract_ref("Transfer Digital #W123456789", "Top-up e-wallet")
        self.assertEqual(res, {"id": "#W123456789", "kind": "flip_transfer_digital"})

    # 3. #R12345678 -> flip_transfer_or_refund
    def test_03_extract_ref_r(self) -> None:
        res = run_node_extract_ref("", "Refund dana transaksi #R12345678 diproses")
        self.assertEqual(res, {"id": "#R12345678", "kind": "flip_transfer_or_refund"})

    # 4. #INT1234567 -> flip_international
    def test_04_extract_ref_int(self) -> None:
        res = run_node_extract_ref("Transfer ke Malaysia #INT1234567", "")
        self.assertEqual(res, {"id": "#INT1234567", "kind": "flip_international"})

    # 5. #BT12345678 -> flip_bulk
    def test_05_extract_ref_bt(self) -> None:
        res = run_node_extract_ref("", "Kirim uang ke banyak tujuan #BT12345678")
        self.assertEqual(res, {"id": "#BT12345678", "kind": "flip_bulk"})

    # 6. #QT-24112621595515247830 -> flip_qris
    def test_06_extract_ref_qt(self) -> None:
        res = run_node_extract_ref("Pembayaran QRIS", "Kode transaksi #QT-24112621595515247830 berhasil")
        self.assertEqual(res, {"id": "#QT-24112621595515247830", "kind": "flip_qris"})

    # 7. FT123456789 (tanpa #) -> flip_fallback
    def test_07_extract_ref_fallback(self) -> None:
        res = run_node_extract_ref("", "ID Transaksi: FT123456789 selesai.")
        self.assertEqual(res, {"id": "FT123456789", "kind": "flip_fallback"})

    # 8. No ID -> {id:null, kind:null}
    def test_08_extract_ref_none(self) -> None:
        res = run_node_extract_ref("Informasi Akun Flip", "Selamat datang di Flip.")
        self.assertEqual(res, {"id": None, "kind": None})

    # 9. Blacklist #TU -> financial_class Ignore
    def test_09_blacklist_tu_coin(self) -> None:
        ev = run_node_parse_gmail(
            "Kamu dapat koin reward!",
            "Koin reward #TU123456789 telah ditambahkan ke akunmu.",
        )
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["status"], "Ignored")
        self.assertIsNone(ev.get("candidate"))

    # 10. Blacklist "Koin Flip" subject -> Ignore
    def test_10_blacklist_koin_subject(self) -> None:
        ev = run_node_parse_gmail(
            "Koin Flip akan segera kedaluwarsa",
            "Gunakan koin Anda sebelum akhir bulan.",
        )
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["status"], "Ignored")
        self.assertIsNone(ev.get("candidate"))

    # 11. Blacklist hello@flip.id -> Ignore
    def test_11_blacklist_marketing_sender(self) -> None:
        ev = run_node_parse_gmail(
            "Update fitur terbaru Flip",
            "Nikmati kemudahan transfer bebas biaya admin.",
            from_address="hello@flip.id",
        )
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["status"], "Ignored")
        self.assertIsNone(ev.get("candidate"))

    # 12. Blacklist "TRANSAKSI GAGAL DIPROSES" -> Ignore, FAILED_ATTEMPT
    def test_12_blacklist_failed_attempt(self) -> None:
        ev = run_node_parse_gmail(
            "Transaksi Gagal",
            "Mohon maaf TRANSAKSI GAGAL DIPROSES karena gangguan bank tujuan.",
        )
        self.assertEqual(ev["financial_class"], "Ignore")
        self.assertEqual(ev["event_kind"], "FAILED_ATTEMPT")
        self.assertEqual(ev["status"], "Ignored")
        self.assertIsNone(ev.get("candidate"))

    # 13. Transfer normal -> event_kind EXTERNAL_TRANSFER, status AutoApproved
    def test_13_transfer_normal(self) -> None:
        ev = run_node_parse_gmail(
            "Successful transfer to ZAYYINNA FITRIANI. Here is the receipt.",
            "Transaksi berhasil #FT650581795\nJumlah Rp 50.000\nDestination Name: ZAYYINNA FITRIANI",
        )
        self.assertEqual(ev["event_kind"], "EXTERNAL_TRANSFER")
        self.assertEqual(ev["status"], "AutoApproved")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["amount"], 50000)
        self.assertIsNotNone(ev.get("candidate"))
        self.assertEqual(ev["candidate"]["status"], "AutoApproved")

    # 14. QRIS -> event_kind MERCHANT_PAYMENT, category Other / Miscellaneous (rules classify later)
    def test_14_qris_payment(self) -> None:
        ev = run_node_parse_gmail(
            "QRIS Payment Berhasil",
            "Pembayaran ke Kopi Kenangan #QT-24112621595515247830\nJumlah Rp 25.000\nPenerima: Kopi Kenangan",
        )
        self.assertEqual(ev["event_kind"], "MERCHANT_PAYMENT")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["status"], "AutoApproved")
        self.assertEqual(ev["amount"], 25000)
        self.assertEqual(ev["merchant_normalized"], "Kopi Kenangan")
        self.assertIsNotNone(ev.get("candidate"))
        self.assertEqual(ev["candidate"]["category"], "Other / Miscellaneous")
        self.assertEqual(ev["candidate"]["status"], "AutoApproved")

    # 15. Malaysia -> event_kind INTERNATIONAL_PURCHASE, category Lab Equipment
    def test_15_international_purchase(self) -> None:
        ev = run_node_parse_gmail(
            "Flip Globe: transfer to Malaysia #INT1234567",
            "Transaksi pengiriman dana internasional berhasil.\nJumlah Rp 1.500.000\nPenerima: Universiti Malaya",
        )
        self.assertEqual(ev["event_kind"], "INTERNATIONAL_PURCHASE")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertIsNotNone(ev.get("candidate"))
        self.assertEqual(ev["candidate"]["category"], "Lain-lain / Lab Equipment")

    # 16. Refund -> REFUND_REVERSAL, direction Credit, is_reversal True
    def test_16_refund(self) -> None:
        ev = run_node_parse_gmail(
            "Refund Transaksi #R12345678",
            "Dana transaksi telah dikembalikan ke rekening Anda sebesar Rp 100.000.",
        )
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["financial_direction"], "Credit")
        self.assertEqual(ev["event_kind"], "REFUND_REVERSAL")
        self.assertTrue(ev.get("is_reversal"))
        self.assertEqual(ev["status"], "AutoApproved")
        self.assertIsNotNone(ev.get("candidate"))
        self.assertTrue(ev["candidate"].get("is_reversal"))
        self.assertEqual(ev["candidate"]["tx_type"], "Reversal")
        self.assertEqual(ev["candidate"]["status"], "AutoApproved")

    # 17. Bulk -> event_kind BULK_TRANSFER
    def test_17_bulk_transfer(self) -> None:
        ev = run_node_parse_gmail(
            "Kirim uang banyak tujuan #BT12345678",
            "Semua transaksi berhasil diproses.\nJumlah Rp 500.000\nPenerima: Tim Project",
        )
        self.assertEqual(ev["event_kind"], "BULK_TRANSFER")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["status"], "AutoApproved")

    # 18. transaction_reference == external_order_id == extracted ID, event_id strips '#'
    def test_18_reference_matching(self) -> None:
        ev = run_node_parse_gmail(
            "Transfer Berhasil",
            "Transaksi #FT650581795 selesai.\nJumlah Rp 75.000\nAtas Nama: BUDI SANTOSO",
        )
        self.assertEqual(ev["transaction_reference"], "#FT650581795")
        self.assertEqual(ev["external_order_id"], "#FT650581795")
        self.assertEqual(ev["event_id"], "flip_FT650581795")

    # 19. Two emails same ID -> same event_id
    def test_19_duplicate_emails_same_event_id(self) -> None:
        ev1 = run_node_parse_gmail(
            "Successful transfer to ZAYYINNA FITRIANI. Here is the receipt.",
            "Transaksi #FT650581795 selesai.\nJumlah Rp 50.000\nPenerima: ZAYYINNA FITRIANI",
        )
        ev2 = run_node_parse_gmail(
            "Informasi Transaksi #FT650581795",
            "Detail transaksi Anda #FT650581795 sebesar Rp 50.000 telah berhasil.",
        )
        self.assertEqual(ev1["event_id"], ev2["event_id"])
        self.assertEqual(ev1["transaction_reference"], ev2["transaction_reference"])
        self.assertEqual(ev1["external_order_id"], ev2["external_order_id"])
        self.assertEqual(ev1["event_id"], "flip_FT650581795")

    # 20. No ID -> status Pending
    def test_20_no_id_pending(self) -> None:
        ev = run_node_parse_gmail(
            "Transfer Berhasil",
            "Transfer tanpa kode referensi berhasil.\nJumlah Rp 30.000\nPenerima: Rina",
        )
        self.assertEqual(ev["status"], "Pending")
        self.assertIsNone(ev["transaction_reference"])
        self.assertIsNone(ev["external_order_id"])
        self.assertIsNotNone(ev.get("candidate"))
        self.assertEqual(ev["candidate"]["status"], "Pending")
        self.assertEqual(ev["candidate"]["confidence_score"], 0.75)

    # 21. description_normalized format: r"^Flip [A-Z_]+ #[A-Z0-9\-]+$"
    def test_21_description_normalized_format(self) -> None:
        ev = run_node_parse_gmail(
            "Successful transfer to ZAYYINNA FITRIANI. Here is the receipt.",
            "Transaksi berhasil #FT650581795\nJumlah Rp 50.000\nDestination Name: ZAYYINNA FITRIANI",
        )
        desc = ev.get("description_normalized") or ""
        self.assertRegex(desc, r"^Flip [A-Z_]+ #[A-Z0-9\-]+$")

    # 22. description_normalized contains no newline or control chars
    def test_22_description_normalized_no_control_chars(self) -> None:
        ev = run_node_parse_gmail(
            "Transfer Berhasil\nBaris Baru",
            "Transaksi berhasil #FT999888777\r\nJumlah Rp 10.000\nDestination Name: JANE\tDOE\n",
        )
        desc = ev.get("description_normalized") or ""
        self.assertNotIn("\n", desc)
        self.assertNotIn("\r", desc)
        self.assertNotIn("\t", desc)
        for ch in desc:
            self.assertGreaterEqual(ord(ch), 32)

    # 23. description_normalized stable across two runs
    def test_23_description_normalized_stable_across_runs(self) -> None:
        subj = "Transfer #FT112233445"
        body = "Transfer berhasil #FT112233445 sebesar Rp 125.000 ke Budi."
        ev1 = run_node_parse_gmail(subj, body)
        ev2 = run_node_parse_gmail(subj, body)
        self.assertEqual(ev1.get("description_normalized"), ev2.get("description_normalized"))

    # 24. Determinism for unreferenced email, stable SHA-256 event_id without timestamp
    def test_24_deterministic_unreferenced_email(self) -> None:
        import time
        subj = "Transfer Tanpa Kode"
        body = "Transfer berhasil sebesar Rp 50.000 ke Rina."
        ev1 = run_node_parse_gmail(subj, body, occurred_at="2026-09-24T10:00:00Z")
        time.sleep(0.01)
        ev2 = run_node_parse_gmail(subj, body, occurred_at="2026-09-24T10:00:00Z")
        self.assertEqual(ev1["event_id"], ev2["event_id"])
        self.assertTrue(ev1["event_id"].startswith("flip_noref_"))
        self.assertEqual(len(ev1["event_id"]), len("flip_noref_") + 16)
        hex_part = ev1["event_id"][len("flip_noref_"):]
        self.assertTrue(all(c in "0123456789abcdef" for c in hex_part))

    # 25. Production DB unchanged across tests
    def test_25_production_db_unchanged(self) -> None:
        current_hash = get_prod_db_hash()
        self.assertEqual(
            current_hash,
            EXPECTED_PROD_DB_HASH,
            "Production database hash modified during test execution!",
        )

    # 26. Refund reversal properties
    def test_26_refund_reversal_properties(self) -> None:
        ev = run_node_parse_gmail(
            "Pengembalian Dana Transaksi #R87654321",
            "Refund untuk transaksi #R87654321 telah berhasil diproses sebesar Rp 150.000.",
        )
        self.assertEqual(ev["event_kind"], "REFUND_REVERSAL")
        self.assertEqual(ev["financial_class"], "Expense")
        self.assertEqual(ev["financial_direction"], "Credit")
        self.assertTrue(ev["is_reversal"])
        self.assertEqual(ev["status"], "AutoApproved")
        self.assertEqual(ev["event_id"], "flip_R87654321")
        self.assertEqual(ev["transaction_reference"], "#R87654321")
        self.assertEqual(ev["external_order_id"], "#R87654321")
        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["tx_type"], "Reversal")
        self.assertTrue(cand["is_reversal"])
        self.assertEqual(cand["status"], "AutoApproved")
        self.assertEqual(cand["amount"], 150000)

    # 27. QRIS without merchant name -> Pending, confidence 0.60
    def test_27_qris_no_merchant_pending(self) -> None:
        ev = run_node_parse_gmail(
            "Pembayaran QRIS #QT-24112621595515247830",
            "Pembayaran QRIS berhasil dengan kode #QT-24112621595515247830 sebesar Rp 45.000.",
        )
        self.assertEqual(ev["event_kind"], "MERCHANT_PAYMENT")
        self.assertEqual(ev["status"], "Pending")
        self.assertEqual(ev["confidence"], 0.60)
        self.assertIn("Flip QRIS payment without extractable merchant name", ev.get("review_reason") or "")
        cand = ev.get("candidate")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["status"], "Pending")
        self.assertEqual(cand["confidence_score"], 0.60)
        self.assertEqual(cand["category"], "Other / Miscellaneous")

    # 28. Event ID has no '#' symbol across all reference formats
    def test_28_event_id_no_hash_all_formats(self) -> None:
        cases = [
            ("#FT123456789", "flip_FT123456789"),
            ("#W123456789", "flip_W123456789"),
            ("#R12345678", "flip_R12345678"),
            ("#INT1234567", "flip_INT1234567"),
            ("#BT12345678", "flip_BT12345678"),
            ("#QT-24112621595515247830", "flip_QT-24112621595515247830"),
            ("FT123456789", "flip_FT123456789"),
        ]
        for ref, expected_event_id in cases:
            ev = run_node_parse_gmail(
                f"Transaksi {ref}",
                f"Transaksi {ref} sebesar Rp 10.000 berhasil kepada Penerima: Test.",
            )
            self.assertEqual(
                ev["event_id"],
                expected_event_id,
                f"Failed for reference {ref}: expected {expected_event_id}, got {ev['event_id']}",
            )
            self.assertNotIn("#", ev["event_id"])

    # 29. Noref event_id is deterministic SHA-256 and identical across repeated executions
    def test_29_noref_event_id_stable(self) -> None:
        subj = "Email Flip Tanpa Ref"
        body = "Halo Pengguna, transaksi Rp 20.000 berhasil."
        ts = "2026-09-24T12:00:00Z"
        ids = [run_node_parse_gmail(subj, body, occurred_at=ts)["event_id"] for _ in range(5)]
        self.assertEqual(len(set(ids)), 1)
        self.assertTrue(ids[0].startswith("flip_noref_"))
        self.assertEqual(len(ids[0]), len("flip_noref_") + 16)


if __name__ == "__main__":
    unittest.main()
