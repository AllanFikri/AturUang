"""Unit tests for Universal Ingestion Phase 3: Shopee Orders Receipt PDF Adapter Skeleton.

Covers exactly 30 focused test methods:
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
25. Real application catalog selects Shopee Orders adapter.
26. Natural key does not expose raw order ID.
27. Explicit canonical_match_required is True.
28. CommerceOrderEvidence rejects cash_movement_emitted=True.
29. Impossible calendar date fails closed.
30. Valid leap date is accepted.
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
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    build_default_adapter_catalog,
)
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
        source_document_id: str = "test-shopee-doc-1",
        source_registry_id: str = SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
        template_id: str = SHOPEE_ORDERS_TEMPLATE_ID,
        template_match_status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
        template_fingerprint: str | None = SHOPEE_ORDERS_TEMPLATE_FINGERPRINT,
    ) -> AdapterInput:
        h = sha256(payload).hexdigest() if payload else (
            sha256(text_payload.encode()).hexdigest() if text_payload else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        return AdapterInput(
            source_document_id=source_document_id,
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
        catalog = build_default_adapter_catalog()
        selected = catalog.select(
            source_registry_id=SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
            template_id=SHOPEE_ORDERS_TEMPLATE_ID,
            parser_version=SHOPEE_ORDERS_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
        )
        self.assertIsNotNone(selected)
        self.assertIsInstance(selected, ShopeeOrdersReceiptAdapter)
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
        raw_order_id = "260508SYNTH001"
        key1 = build_natural_document_key(raw_order_id)
        key2 = build_natural_document_key(raw_order_id)
        self.assertEqual(key1, key2)
        expected_token = sha256(f"shopee_orders|receipt|{raw_order_id.upper()}".encode("utf-8")).hexdigest()
        self.assertEqual(key1, f"shopee_orders:receipt:{expected_token}")
        self.assertNotIn(raw_order_id, key1)

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
        raw_order_id = "260508SYNTH001"
        expected_token = sha256(f"shopee_orders|receipt|{raw_order_id.upper()}".encode("utf-8")).hexdigest()
        self.assertEqual(res1.natural_document_key_candidate, f"shopee_orders:receipt:{expected_token}")
        self.assertNotIn(raw_order_id, res1.natural_document_key_candidate)

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
        self.assertEqual(len(res.events), 1)
        ev = res.events[0]
        self.assertIsInstance(ev.payload, CommerceOrderEvidence)
        self.assertIs(ev.payload.canonical_match_required, True)
        self.assertIs(ev.payload.cash_movement_emitted, False)

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

    # 25. Real application catalog selects Shopee Orders adapter
    def test_25_real_application_catalog_selects_shopee_orders(self) -> None:
        catalog = build_default_adapter_catalog()
        selected = catalog.select(
            source_registry_id="shopee_orders",
            template_id="shopee_order_receipt_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        self.assertIsNotNone(selected)
        self.assertIsInstance(selected, ShopeeOrdersReceiptAdapter)
        # Confirm existing adapters remain selectable
        self.assertIsNotNone(
            catalog.select(
                source_registry_id="bca_statement",
                template_id="bca_monthly_statement_v1",
                parser_version="parser-v1",
                source_channel=SourceChannel.PDF,
            )
        )
        self.assertIsNotNone(
            catalog.select(
                source_registry_id="jago_statement",
                template_id="jago_monthly_statement_v1",
                parser_version="parser-v1",
                source_channel=SourceChannel.PDF,
            )
        )
        self.assertIsNotNone(
            catalog.select(
                source_registry_id="stockbit_soa",
                template_id="stockbit_soa_v1",
                parser_version="parser-v1",
                source_channel=SourceChannel.PDF,
            )
        )

    # 26. Natural key does not expose raw order ID
    def test_26_natural_key_does_not_expose_raw_order_id(self) -> None:
        raw_order_id = "260508SYNTH999SECRET"
        key = build_natural_document_key(raw_order_id)
        self.assertIsNotNone(key)
        self.assertNotIn(raw_order_id, key)
        self.assertNotIn(raw_order_id.lower(), key.lower())
        # Check requirements:
        # 1. same order ID produces the same natural key
        self.assertEqual(key, build_natural_document_key(raw_order_id))
        self.assertEqual(key, build_natural_document_key(f"  {raw_order_id}  "))
        # 2. different order IDs produce different keys
        key_other = build_natural_document_key("260508SYNTH999OTHER")
        self.assertNotEqual(key, key_other)
        # 3. raw order ID is not substring of key
        self.assertFalse(raw_order_id in key)

    # 27. Explicit canonical_match_required is True
    def test_27_explicit_canonical_match_required_is_true(self) -> None:
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([SYNTHETIC_RECEIPT_TEXT])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(len(res.events), 1)
        payload = res.events[0].payload
        self.assertIsInstance(payload, CommerceOrderEvidence)
        self.assertIs(payload.canonical_match_required, True)
        self.assertIs(type(payload.canonical_match_required), bool)

    # 28. CommerceOrderEvidence rejects cash_movement_emitted=True
    def test_28_commerce_order_evidence_rejects_cash_movement_emitted_true(self) -> None:
        with self.assertRaises(AdapterContractError):
            CommerceOrderEvidence(
                order_native_id_raw="ORD123",
                order_date="2026-05-08",
                order_total=Decimal("10000"),
                currency="IDR",
                cash_movement_emitted=True,
            )
        with self.assertRaises(AdapterContractError):
            CommerceOrderEvidence(
                order_native_id_raw="ORD123",
                order_date="2026-05-08",
                order_total=Decimal("10000"),
                currency="IDR",
                canonical_match_required="true",  # type: ignore
            )

    # 29. Impossible calendar date fails closed
    def test_29_impossible_calendar_date_fails_closed(self) -> None:
        impossible_dates = ["31/02/2026", "00/05/2026", "15/13/2026", "2026-02-30"]
        for bad_date in impossible_dates:
            text = f"""Nama Penjual:
Toko Sintetis
No. Pesanan
Tanggal Transaksi
Metode Pembayaran
Jasa Kirim
260508SYNTH001
{bad_date}
Saldo ShopeePay
Reguler
Subtotal
Rp50.000
Total Pembayaran
Rp50.000
Nama Pembeli:
Pembeli
Rincian Pesanan
Barang A
Nota Pesanan
"""
            adapter = ShopeeOrdersReceiptAdapter()
            pdf_bytes = make_multipage_pdf([text])
            inp = self._make_adapter_input(payload=pdf_bytes)
            res = adapter.parse(inp)
            self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
            self.assertTrue(
                any(d.code in ("INVALID_ORDER_DATE", "MISSING_ORDER_DATE") for d in res.diagnostics),
                f"Failed to catch invalid date: {bad_date}"
            )

    # 30. Valid leap date is accepted
    def test_30_valid_leap_date_is_accepted(self) -> None:
        leap_text = """Nama Penjual:
Toko Sintetis
No. Pesanan
Tanggal Transaksi
Metode Pembayaran
Jasa Kirim
260508SYNTHLEAP
29/02/2024
Saldo ShopeePay
Reguler
Subtotal
Rp50.000
Total Pembayaran
Rp50.000
Nama Pembeli:
Pembeli
Rincian Pesanan
Barang A
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        pdf_bytes = make_multipage_pdf([leap_text])
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(len(res.events), 1)
        self.assertEqual(res.events[0].payload.order_date, "2024-02-29")
        self.assertEqual(res.period_start, "2024-02-29")
        self.assertEqual(res.period_end, "2024-02-29")



    # 31. Single line item
    def test_31_single_line_item(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH031
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp50.000
Subtotal Pesanan: Rp50.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang Tunggal
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 1)
        item = ev.line_items[0]
        self.assertEqual(item.product_name_raw, "Barang Tunggal")
        self.assertIsNone(item.variation_raw)
        self.assertEqual(item.quantity, Decimal("1"))
        self.assertEqual(item.line_subtotal, Decimal("50000"))

    # 32. Multiple line items
    def test_32_multiple_line_items(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH032
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp80.000
Subtotal Pesanan: Rp80.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang Pertama
Rp30.000
1
Rp30.000
2
Barang Kedua
Rp25.000
2
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 2)
        self.assertEqual(ev.line_items[0].product_name_raw, "Barang Pertama")
        self.assertEqual(ev.line_items[0].line_subtotal, Decimal("30000"))
        self.assertEqual(ev.line_items[1].product_name_raw, "Barang Kedua")
        self.assertEqual(ev.line_items[1].quantity, Decimal("2"))
        self.assertEqual(ev.line_items[1].line_subtotal, Decimal("50000"))

    # 33. Wrapped product name
    def test_33_wrapped_product_name(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH033
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp40.000
Subtotal Pesanan: Rp40.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Nama Produk Sangat Panjang Baris 1
Nama Produk Lanjutan Baris 2
Rp40.000
1
Rp40.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 1)
        item = ev.line_items[0]
        self.assertEqual(
            item.product_name_raw,
            "Nama Produk Sangat Panjang Baris 1 Nama Produk Lanjutan Baris 2",
        )
        self.assertIsNone(item.variation_raw)

    # 34. Variation preservation
    def test_34_variation_preservation(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH034
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp60.000
Subtotal Pesanan: Rp60.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Baju Kaos Santai
Variasi: Warna Biru, Ukuran L
Rp60.000
1
Rp60.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 1)
        item = ev.line_items[0]
        self.assertEqual(item.product_name_raw, "Baju Kaos Santai")
        self.assertEqual(item.variation_raw, "Warna Biru, Ukuran L")

    # 35. Quantity parsing
    def test_35_quantity_parsing(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH035
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp75.000
Subtotal Pesanan: Rp75.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Buku Catatan
Rp25.000
3
Rp75.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        item = ev.line_items[0]
        self.assertEqual(item.quantity, Decimal("3"))
        self.assertEqual(item.line_subtotal, Decimal("75000"))

    # 36. Quantity 150 remains valid
    def test_36_quantity_150_remains_valid(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH036
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp150.000
Subtotal Pesanan: Rp150.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Komponen Baut
Rp1.000
150
Rp150.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        item = ev.line_items[0]
        self.assertEqual(item.quantity, Decimal("150"))
        self.assertEqual(item.line_subtotal, Decimal("150000"))

    # 37. Line subtotal parsing
    def test_37_line_subtotal_parsing(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH037
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp123.456
Subtotal Pesanan: Rp123.456
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang Presisi
Rp123.456
1
Rp123.456
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        item = res.events[0].payload.line_items[0]
        self.assertEqual(item.line_subtotal, Decimal("123456"))

    # 38. Multi-page item continuation
    def test_38_multipage_item_continuation(self) -> None:
        p1 = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH038
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp100.000
Subtotal Pesanan: Rp100.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang Halaman 1
Rp50.000
1
Rp50.000
"""
        p2 = """Rincian Pesanan
2
Barang Halaman 2
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([p1, p2]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 2)
        self.assertEqual(ev.line_items[0].product_name_raw, "Barang Halaman 1")
        self.assertEqual(ev.line_items[1].product_name_raw, "Barang Halaman 2")

    # 39. Repeated page header suppression
    def test_39_repeated_page_header_suppression(self) -> None:
        p1 = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH039
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp100.000
Subtotal Pesanan: Rp100.000
Nama Pembeli: Pembeli
Rincian Pesanan
No.
Produk
Variasi
Harga Produk
Kuantitas
Subtotal
1
Barang P1
Rp50.000
1
Rp50.000
"""
        p2 = """Rincian Pesanan
No.
Produk
Variasi
Harga Produk
Kuantitas
Subtotal
2
Barang P2
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([p1, p2]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 2)
        for it in ev.line_items:
            self.assertNotIn("No.", it.product_name_raw)
            self.assertNotIn("Produk", it.product_name_raw)
            self.assertNotIn("Subtotal", it.product_name_raw)

    # 40. Payment rows are not products
    def test_40_payment_rows_are_not_products(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH040
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp65.000
Subtotal Pesanan: Rp50.000
Subtotal Pengiriman: Rp10.000
Biaya Layanan: Rp5.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang Valid
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 1)
        self.assertEqual(ev.line_items[0].product_name_raw, "Barang Valid")
        prod_names = [it.product_name_raw.lower() for it in ev.line_items]
        self.assertNotIn("biaya layanan", prod_names)
        self.assertNotIn("subtotal pengiriman", prod_names)
        self.assertNotIn("total pembayaran", prod_names)

    # 41. Product subtotal component
    def test_41_product_subtotal_component(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH041
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp50.000
Subtotal Pesanan: Rp50.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        subtotal_comps = [c for c in ev.amount_components if "subtotal pesanan" in c.label_raw.lower()]
        self.assertEqual(len(subtotal_comps), 1)
        self.assertEqual(subtotal_comps[0].amount, Decimal("50000"))
        self.assertGreater(subtotal_comps[0].amount, Decimal("0"))

    # 42. Shipping component
    def test_42_shipping_component(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH042
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp62.000
Subtotal Pesanan: Rp50.000
Subtotal Pengiriman: Rp12.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        ship_comps = [c for c in ev.amount_components if "pengiriman" in c.label_raw.lower()]
        self.assertEqual(len(ship_comps), 1)
        self.assertEqual(ship_comps[0].amount, Decimal("12000"))
        self.assertGreater(ship_comps[0].amount, Decimal("0"))

    # 43. Service-fee component
    def test_43_service_fee_component(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH043
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp52.000
Subtotal Pesanan: Rp50.000
Biaya Layanan: Rp2.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        fee_comps = [c for c in ev.amount_components if "layanan" in c.label_raw.lower()]
        self.assertEqual(len(fee_comps), 1)
        self.assertEqual(fee_comps[0].amount, Decimal("2000"))
        self.assertGreater(fee_comps[0].amount, Decimal("0"))

    # 44. Seller-voucher negative sign
    def test_44_seller_voucher_negative_sign(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH044
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp45.000
Subtotal Pesanan: Rp50.000
Diskon Voucher Toko: -Rp5.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        seller_vouchers = [c for c in ev.amount_components if "toko" in c.label_raw.lower()]
        self.assertEqual(len(seller_vouchers), 1)
        self.assertEqual(seller_vouchers[0].amount, Decimal("-5000"))
        self.assertLess(seller_vouchers[0].amount, Decimal("0"))

    # 45. Shopee-voucher negative sign
    def test_45_shopee_voucher_negative_sign(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH045
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp40.000
Subtotal Pesanan: Rp50.000
Diskon Voucher Shopee: -Rp10.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        shopee_vouchers = [c for c in ev.amount_components if "shopee" in c.label_raw.lower()]
        self.assertEqual(len(shopee_vouchers), 1)
        self.assertEqual(shopee_vouchers[0].amount, Decimal("-10000"))
        self.assertLess(shopee_vouchers[0].amount, Decimal("0"))

    # 46. Shipping-discount negative sign
    def test_46_shipping_discount_negative_sign(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH046
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp50.000
Subtotal Pesanan: Rp50.000
Subtotal Pengiriman: Rp10.000
Diskon Pengiriman: -Rp10.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        ship_discs = [c for c in ev.amount_components if "diskon pengiriman" in c.label_raw.lower()]
        self.assertEqual(len(ship_discs), 1)
        self.assertEqual(ship_discs[0].amount, Decimal("-10000"))
        self.assertLess(ship_discs[0].amount, Decimal("0"))

    # 47. Shopee Coins negative sign
    def test_47_shopee_coins_negative_sign(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH047
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp49.500
Subtotal Pesanan: Rp50.000
500 Koin Shopee Ditukarkan: -Rp500
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        coin_comps = [c for c in ev.amount_components if "koin" in c.label_raw.lower()]
        self.assertEqual(len(coin_comps), 1)
        self.assertEqual(coin_comps[0].amount, Decimal("-500"))
        self.assertLess(coin_comps[0].amount, Decimal("0"))

    # 48. Total payment remains summary evidence
    def test_48_total_payment_remains_summary_evidence(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH048
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp50.000
Subtotal Pesanan: Rp50.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        self.assertEqual(ev.order_total, Decimal("50000"))
        self.assertIs(ev.cash_movement_emitted, False)
        self.assertIs(ev.canonical_match_required, True)

    # 49. Exact order equation match
    def test_49_exact_order_equation_match(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH049
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp78.000
Subtotal Pesanan: Rp80.000
Subtotal Pengiriman: Rp15.000
Biaya Layanan: Rp3.000
Diskon Voucher Toko: -Rp5.000
Diskon Voucher Shopee: -Rp15.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp80.000
1
Rp80.000
Nota Pesanan
"""
        # 80000 + 15000 + 3000 - 5000 - 15000 = 78000
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.order_reconciliation_status, "MATCHED")
        self.assertEqual(res.reconciliation_difference, Decimal("0.00"))
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)

    # 50. Order equation mismatch requires review
    def test_50_order_equation_mismatch_requires_review(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH050
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp99.000
Subtotal Pesanan: Rp80.000
Biaya Layanan: Rp3.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp80.000
1
Rp80.000
Nota Pesanan
"""
        # 80000 + 3000 = 83000 != 99000
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.order_reconciliation_status, "MISMATCH")
        self.assertNotEqual(res.reconciliation_difference, Decimal("0.00"))
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertTrue(any(d.code == "ORDER_TOTAL_MISMATCH" for d in res.diagnostics))

    # 51. Unknown priced component fails safely
    def test_51_unknown_priced_component_fails_safely(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH051
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp55.000
Subtotal Pesanan: Rp50.000
Biaya Misterius: Rp5.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertTrue(any(d.code == "UNKNOWN_ORDER_AMOUNT_COMPONENT" for d in res.diagnostics))

    # 52. Duplicate replay produces stable row and line keys
    def test_52_duplicate_replay_produces_stable_keys(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH052
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp50.000
Subtotal Pesanan: Rp50.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang A
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp1 = self._make_adapter_input(payload=make_multipage_pdf([text]), source_document_id="file_one.pdf")
        inp2 = self._make_adapter_input(payload=make_multipage_pdf([text]), source_document_id="file_two.pdf")
        res1 = adapter.parse(inp1)
        res2 = adapter.parse(inp2)

        self.assertEqual(res1.events[0].row_fingerprint, res2.events[0].row_fingerprint)
        self.assertEqual(
            res1.events[0].payload.line_items[0].line_key,
            res2.events[0].payload.line_items[0].line_key,
        )

    # 53. Identical physical item rows remain separate
    def test_53_identical_physical_items_remain_separate(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH053
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp100.000
Subtotal Pesanan: Rp100.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
Barang Kembar
Rp50.000
1
Rp50.000
2
Barang Kembar
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        self.assertEqual(len(ev.line_items), 2)
        # Both items have identical product name, quantity, subtotal
        self.assertEqual(ev.line_items[0].product_name_raw, ev.line_items[1].product_name_raw)
        # But distinct stable line keys
        self.assertNotEqual(ev.line_items[0].line_key, ev.line_items[1].line_key)

    # 54. CO Buku text does not assign ownership semantics
    def test_54_co_buku_text_does_not_assign_ownership(self) -> None:
        text = """Nama Penjual: Toko Sintetis
No. Pesanan: 260508SYNTH054
Tanggal Transaksi: 08/05/2026
Metode Pembayaran: Saldo ShopeePay
Total Pembayaran: Rp50.000
Subtotal Pesanan: Rp50.000
Nama Pembeli: Pembeli
Rincian Pesanan
1
CO Buku Paket Sejarah
Rp50.000
1
Rp50.000
Nota Pesanan
"""
        adapter = ShopeeOrdersReceiptAdapter()
        inp = self._make_adapter_input(payload=make_multipage_pdf([text]))
        res = adapter.parse(inp)
        ev = res.events[0].payload
        self.assertEqual(ev.line_items[0].product_name_raw, "CO Buku Paket Sejarah")
        self.assertIs(ev.cash_movement_emitted, False)
        self.assertIs(ev.canonical_match_required, True)
        self.assertFalse(hasattr(ev, "economic_owner"))
        self.assertFalse(hasattr(ev, "payer_responsibility"))


if __name__ == "__main__":
    unittest.main()
