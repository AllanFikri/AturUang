"""Unit tests for Universal Ingestion Phase 3: Shopee Orders Receipt PDF Adapter Skeleton.

Covers exactly 24 focused test methods:
1. Descriptor identity.
2. Valid template recognition.
3. Single marker cannot establish template.
4. Unknown template fails closed.
5. Template drift fails closed.
6. Universal adapter routing.
7. Valid order ID extraction.
8. Order ID comes only from order-summary region.
9. Product-body order-like value cannot set identity.
10. Valid order date extraction.
11. Body date cannot set authoritative order date.
12. Natural document key stability.
13. Filename does not affect natural identity.
14. Content SHA alone is not natural identity.
15. Same order ID with different bytes has the same natural identity.
16. Missing order ID requires review.
17. Missing order date requires review.
18. Missing total requires review.
19. Conflicting order IDs require review.
20. Preliminary CommerceOrderEvidence is emitted.
21. No CashMovementEvidence is emitted.
22. Commerce evidence uses neutral/non-cash behavior.
23. Canonical matching is required.
24. Diagnostics do not leak private fixture values.
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import unittest

from aturuang.ingestion_adapter import (
    AdapterContractError,
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    CashMovementEvidence,
    CommerceOrderEvidence,
    DiagnosticSeverity,
    EventRole,
    SafeDiagnostic,
)
from aturuang.ingestion_contracts import (
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_orchestration import AdapterCatalog
from aturuang.ingestion_preflight import (
    SOURCE_TEMPLATE_SIGNATURES_V1,
    detect_template,
    template_fingerprint,
)
from aturuang.ingestion_shopee_orders_adapter import (
    REQUIRED_TEMPLATE_MARKERS,
    SHOPEE_ORDERS_ADAPTER_ID,
    SHOPEE_ORDERS_PARSER_VERSION,
    SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
    SHOPEE_ORDERS_TEMPLATE_FINGERPRINT,
    SHOPEE_ORDERS_TEMPLATE_ID,
    ShopeeOrderDocumentIdentity,
    ShopeeOrderEvidenceRole,
    ShopeeOrdersReceiptAdapter,
    build_natural_document_key,
    extract_order_identity,
    extract_order_regions,
    parse_id_amount,
    parse_order_date,
)


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def make_pdf_page_stream(text: str) -> bytes:
    commands = [
        "BT",
        "/F1 9 Tf",
        "40 760 Td",
        "11 TL",
    ]
    for line in text.splitlines():
        commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("T*")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1")


def make_multipage_pdf(pages: list[str]) -> bytes:
    page_streams = [make_pdf_page_stream(p) for p in pages]
    total_pages = len(page_streams)

    catalog_obj_num = 1
    pages_obj_num = 2
    first_page_obj_num = 3
    font_obj_num = 3 + total_pages
    first_content_obj_num = font_obj_num + 1

    page_refs = " ".join(f"{first_page_obj_num + i} 0 R" for i in range(total_pages))
    objects: list[bytes] = [
        f"{catalog_obj_num} 0 obj\n<< /Type /Catalog /Pages {pages_obj_num} 0 R >>\nendobj\n".encode("latin-1"),
        f"{pages_obj_num} 0 obj\n<< /Type /Pages /Kids [{page_refs}] /Count {total_pages} >>\nendobj\n".encode("latin-1"),
    ]

    for i in range(total_pages):
        content_obj = first_content_obj_num + i
        obj_num = first_page_obj_num + i
        objects.append(
            f"{obj_num} 0 obj\n<< /Type /Page /Parent {pages_obj_num} 0 R /MediaBox [0 0 595 842] "
            f"/Contents {content_obj} 0 R /Resources << /Font << /F1 {font_obj_num} 0 R >> >> >>\nendobj\n".encode("latin-1")
        )

    objects.append(
        f"{font_obj_num} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n".encode("latin-1")
    )

    for i, stream in enumerate(page_streams):
        obj_num = first_content_obj_num + i
        objects.append(
            f"{obj_num} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
            + stream
            + b"\nendstream\nendobj\n"
        )

    out = BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)
    xref_offset = out.tell()
    out.write(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("latin-1"))
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.write(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
    return out.getvalue()


SYNTHETIC_RECEIPT_TEXT = """Nama Penjual:
Toko Sintetis Jaya
No. Pesanan
Tanggal Transaksi
Metode Pembayaran
Jasa Kirim
260508SYNTH001
08/05/2026
Saldo ShopeePay
Reguler
Subtotal
Rp150.000
Total Pembayaran
Rp150.000
Nama Pembeli:
Budi Santoso
Rincian Pesanan
1. Barang Sintetis
Nota Pesanan
"""


class TestUniversalIngestionPhase3ShopeeOrders(unittest.TestCase):
    def _make_adapter_input(
        self,
        payload: bytes | None = None,
        text_payload: str | None = None,
        source_registry_id: str = SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
        template_id: str = SHOPEE_ORDERS_TEMPLATE_ID,
        template_match_status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
        template_fingerprint: str | None = SHOPEE_ORDERS_TEMPLATE_FINGERPRINT,
    ) -> AdapterInput:
        h = sha256(payload).hexdigest() if payload else (
            sha256(text_payload.encode()).hexdigest() if text_payload else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        return AdapterInput(
            source_document_id="test-shopee-doc-1",
            content_sha256=h,
            source_registry_id=source_registry_id,
            template_id=template_id,
            parser_version=SHOPEE_ORDERS_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=template_match_status,
            period_status=PeriodStatus.CLOSED,
            template_fingerprint=template_fingerprint,
            binary_payload=payload,
            text_payload=text_payload,
        )

    # 1. Descriptor identity
    def test_01_descriptor_identity(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        desc = adapter.descriptor
        self.assertEqual(desc.adapter_id, SHOPEE_ORDERS_ADAPTER_ID)
        self.assertEqual(desc.source_registry_id, SHOPEE_ORDERS_SOURCE_REGISTRY_ID)
        self.assertEqual(desc.template_id, SHOPEE_ORDERS_TEMPLATE_ID)
        self.assertEqual(desc.parser_version, SHOPEE_ORDERS_PARSER_VERSION)
        self.assertEqual(desc.source_channel, SourceChannel.PDF)

    # 2. Valid template recognition
    def test_02_valid_template_recognition(self) -> None:
        detection = detect_template(SYNTHETIC_RECEIPT_TEXT, media_type="PDF")
        self.assertEqual(detection.status, TemplateMatchStatus.KNOWN)
        self.assertEqual(detection.source_registry_id, SHOPEE_ORDERS_SOURCE_REGISTRY_ID)
        self.assertEqual(detection.template_id, SHOPEE_ORDERS_TEMPLATE_ID)
        self.assertEqual(detection.template_fingerprint, SHOPEE_ORDERS_TEMPLATE_FINGERPRINT)

    # 3. Single marker cannot establish template
    def test_03_single_marker_cannot_establish_template(self) -> None:
        detection = detect_template("Shopee No. Pesanan arbitrary invoice", media_type="PDF")
        self.assertNotEqual(detection.status, TemplateMatchStatus.KNOWN)
        self.assertIsNone(detection.template_id)

    # 4. Unknown template fails closed
    def test_04_unknown_template_fails_closed(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        invalid_pdf = make_multipage_pdf(["Invoice from random store with no Shopee markers"])
        inp = self._make_adapter_input(payload=invalid_pdf)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(any(d.code == "SHOPEE_UNKNOWN_TEMPLATE" for d in res.diagnostics))

    # 5. Template drift fails closed
    def test_05_template_drift_fails_closed(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        # Partial markers (e.g. 3 of 6) triggers template drift
        partial_text = "Nama Penjual:\nToko Jaya\nNo. Pesanan\nTotal Pembayaran\nRp10.000"
        drift_pdf = make_multipage_pdf([partial_text])
        inp = self._make_adapter_input(payload=drift_pdf)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertTrue(any(d.code == "SHOPEE_TEMPLATE_DRIFT" for d in res.diagnostics))

    # 6. Universal adapter routing
    def test_06_universal_adapter_routing(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        catalog = AdapterCatalog((adapter,))
        selected = catalog.select(
            source_registry_id=SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
            template_id=SHOPEE_ORDERS_TEMPLATE_ID,
            parser_version=SHOPEE_ORDERS_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
        )
        self.assertIs(selected, adapter)
        self.assertEqual(selected.descriptor.adapter_id, SHOPEE_ORDERS_ADAPTER_ID)

    # 7. Valid order ID extraction
    def test_07_valid_order_id_extraction(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(len(res.events), 1)
        self.assertEqual(res.events[0].payload.order_native_id_raw, "260508SYNTH001")

    # 8. Order ID comes only from order-summary region
    def test_08_order_id_comes_only_from_order_summary_region(self) -> None:
        text = """Nama Penjual:
Toko Sintetis
No. Pesanan
Tanggal Transaksi
Metode Pembayaran
Jasa Kirim
260508ORDER999
10/05/2026
Saldo ShopeePay
Reguler
Subtotal
Rp50.000
Total Pembayaran
Rp50.000
Nama Pembeli:
Pembeli
Rincian Pesanan
Product description mentions fake order 999999FAKE999
Nota Pesanan
"""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        regions, _ = extract_order_regions(lines)
        self.assertIn("260508ORDER999", " ".join(regions["summary"]))
        self.assertNotIn("999999FAKE999", " ".join(regions["summary"]))

        identity, _ = extract_order_identity(lines)
        self.assertEqual(identity.order_number, "260508ORDER999")

    # 9. Product-body order-like value cannot set identity
    def test_09_product_body_order_like_value_cannot_set_identity(self) -> None:
        text = SYNTHETIC_RECEIPT_TEXT + "\nItem Serial: 260999BODYORDER123"
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.events[0].payload.order_native_id_raw, "260508SYNTH001")

    # 10. Valid order date extraction
    def test_10_valid_order_date_extraction(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.events[0].payload.order_date, "2026-05-08")
        self.assertEqual(res.period_start, "2026-05-08")
        self.assertEqual(res.period_end, "2026-05-08")

    # 11. Body date cannot set authoritative order date
    def test_11_body_date_cannot_set_authoritative_order_date(self) -> None:
        text = SYNTHETIC_RECEIPT_TEXT + "\nWarranty valid until 31/12/2030"
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.events[0].payload.order_date, "2026-05-08")

    # 12. Natural document key stability
    def test_12_natural_document_key_stability(self) -> None:
        key1 = build_natural_document_key("260508SYNTH001")
        key2 = build_natural_document_key("260508SYNTH001")
        self.assertEqual(key1, key2)
        self.assertEqual(key1, "shopee_orders:receipt:260508SYNTH001")

    # 13. Filename does not affect natural identity
    def test_13_filename_does_not_affect_natural_identity(self) -> None:
        lines = [l.strip() for l in SYNTHETIC_RECEIPT_TEXT.splitlines() if l.strip()]
        ident1, _ = extract_order_identity(lines, source_file_name="receipt_order_A.pdf")
        ident2, _ = extract_order_identity(lines, source_file_name="download_batch_B.pdf")
        self.assertEqual(ident1.natural_document_key_candidate, ident2.natural_document_key_candidate)

    # 14. Content SHA alone is not natural identity
    def test_14_content_sha_alone_is_not_natural_identity(self) -> None:
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        h = sha256(pdf_bytes).hexdigest()
        key = build_natural_document_key("260508SYNTH001")
        self.assertNotEqual(key, h)
        self.assertFalse(key.startswith(h))

    # 15. Same order ID with different bytes has the same natural identity
    def test_15_same_order_id_different_bytes_same_natural_identity(self) -> None:
        text1 = SYNTHETIC_RECEIPT_TEXT
        text2 = SYNTHETIC_RECEIPT_TEXT + "\nExtra comment line in footer\n"
        pdf1 = make_multipage_pdf([text1])
        pdf2 = make_multipage_pdf([text2])
        self.assertNotEqual(sha256(pdf1).hexdigest(), sha256(pdf2).hexdigest())

        adapter = ShopeeOrdersReceiptAdapter()
        inp1 = self._make_adapter_input(payload=pdf1)
        inp2 = self._make_adapter_input(payload=pdf2)
        res1 = adapter.parse(inp1)
        res2 = adapter.parse(inp2)
        self.assertEqual(res1.natural_document_key_candidate, res2.natural_document_key_candidate)
        self.assertEqual(res1.natural_document_key_candidate, "shopee_orders:receipt:260508SYNTH001")

    # 16. Missing order ID requires review
    def test_16_missing_order_id_requires_review(self) -> None:
        text = """Nama Penjual:
Toko Sintetis
No. Pesanan
Tanggal Transaksi
Metode Pembayaran
Jasa Kirim
08/05/2026
Saldo ShopeePay
Reguler
Subtotal
Rp150.000
Total Pembayaran
Rp150.000
Nama Pembeli:
Pembeli
Rincian Pesanan
Item A
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertIsNone(res.natural_document_key_candidate)
        self.assertTrue(any(d.code == "MISSING_ORDER_IDENTITY" for d in res.diagnostics))

    # 17. Missing order date requires review
    def test_17_missing_order_date_requires_review(self) -> None:
        text = """Nama Penjual:
Toko Sintetis
No. Pesanan: 260508SYNTH001
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp 150.000
Nama Pembeli: Pembeli
Rincian Pesanan
Tanggal Transaksi
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertTrue(any(d.code == "MISSING_ORDER_DATE" for d in res.diagnostics))

    # 18. Missing total requires review
    def test_18_missing_total_requires_review(self) -> None:
        text = """Nama Penjual:
Toko Sintetis
No. Pesanan: 260508SYNTH001
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Nama Pembeli: Pembeli
Rincian Pesanan
Total Pembayaran
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertTrue(any(d.code == "MISSING_TOTAL_PAYMENT" for d in res.diagnostics))

    # 19. Conflicting order IDs require review
    def test_19_conflicting_order_ids_require_review(self) -> None:
        text = """Nama Penjual:
Toko Sintetis
No. Pesanan: 260508SYNTH001
No. Pesanan: 260508CONFLICT999
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp 150.000
Nama Pembeli: Pembeli
Rincian Pesanan
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertIsNone(res.natural_document_key_candidate)
        self.assertTrue(any(d.code == "CONFLICTING_ORDER_IDS" for d in res.diagnostics))

    # 20. Preliminary CommerceOrderEvidence is emitted
    def test_20_preliminary_commerce_order_evidence_emitted(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(len(res.events), 1)
        ev = res.events[0]
        self.assertEqual(ev.event_role, EventRole.COMMERCE_ORDER)
        self.assertIsInstance(ev.payload, CommerceOrderEvidence)
        self.assertEqual(ev.payload.order_native_id_raw, "260508SYNTH001")
        self.assertEqual(ev.payload.order_date, "2026-05-08")
        self.assertEqual(ev.payload.order_total, Decimal("150000"))
        self.assertEqual(ev.payload.currency, "IDR")
        self.assertEqual(ev.payload.seller_raw, "Toko Sintetis Jaya")
        self.assertEqual(ev.payload.recipient_raw, "Budi Santoso")

    # 21. No CashMovementEvidence is emitted
    def test_21_no_cash_movement_evidence_emitted(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        for ev in res.events:
            self.assertNotEqual(ev.event_role, EventRole.CASH_MOVEMENT)
            self.assertNotIsInstance(ev.payload, CashMovementEvidence)

    # 22. Commerce evidence uses neutral/non-cash behavior
    def test_22_commerce_evidence_uses_neutral_non_cash_behavior(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        ev = res.events[0]
        self.assertEqual(ev.event_role, EventRole.COMMERCE_ORDER)
        # CommerceOrderEvidence does not carry direction or account balances
        self.assertFalse(hasattr(ev.payload, "direction"))
        self.assertFalse(hasattr(ev.payload, "balance_after"))

    # 23. Canonical matching is required
    def test_23_canonical_matching_is_required(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        # Order skeleton evidence requires cross-source matching to determine financial ledger effects
        self.assertTrue(any(d.code == "ORDER_DETAILS_NOT_PARSED" for d in res.diagnostics))
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)

    # 24. Diagnostics do not leak private fixture values
    def test_24_diagnostics_do_not_leak_private_values(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        text = """Nama Penjual:
SecretSellerName
No. Pesanan: 260508SECRETID123
No. Pesanan: 260508CONFLICT456
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp 999.999
Nama Pembeli: SecretBuyerName
Rincian Pesanan
Nota Pesanan
"""
        pdf_bytes = make_multipage_pdf([text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        for diag in res.diagnostics:
            self.assertNotIn("SecretSellerName", diag.code)
            self.assertNotIn("SecretSellerName", diag.message)
            self.assertNotIn("SecretBuyerName", diag.code)
            self.assertNotIn("SecretBuyerName", diag.message)
            self.assertNotIn("260508SECRETID123", diag.code)
            self.assertNotIn("260508SECRETID123", diag.message)
            self.assertNotIn("999.999", diag.message)


if __name__ == "__main__":
    unittest.main()
