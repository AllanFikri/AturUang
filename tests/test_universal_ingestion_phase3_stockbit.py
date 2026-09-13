"""Unit tests for Universal Ingestion Phase 3: Stockbit SOA PDF Adapter Skeleton.

Covers 20 focused test methods:
1. Recognized STOCKBIT_SOA document.
2. Unsupported document fails closed.
3. Period is taken from the authoritative header.
4. Date-like text in the body cannot establish document period.
5. Missing period returns unknown/review-required.
6. Stable account fingerprint.
7. Raw account number is absent from public identity output.
8. Natural document key is deterministic.
9. Filename changes do not change natural document key.
10. Byte-level duplicate preserves logical document identity.
11. Different statement type produces a different natural key.
12. Different account fingerprint produces a different natural key.
13. Holding snapshot cannot emit cash movement.
14. Portfolio valuation cannot emit cash movement.
15. Unrealized gain/loss cannot emit income or expense.
16. Trade date and settlement date remain separate.
17. Jago RDN evidence requires cross-source matching.
18. Row ordinal is provenance, not preferred identity.
19. Missing stable row identity becomes review-required.
20. Unrecognized corporate-action-like evidence fails closed.
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
    ConfidenceLevel,
    DiagnosticSeverity,
    EventRole,
    SafeDiagnostic,
)
from aturuang.ingestion_contracts import (
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_preflight import (
    SOURCE_TEMPLATE_SIGNATURES_V1,
    detect_template,
    template_fingerprint,
)
from aturuang.ingestion_stockbit_adapter import (
    STOCKBIT_ADAPTER_ID,
    STOCKBIT_PARSER_VERSION,
    STOCKBIT_SOURCE_REGISTRY_ID,
    STOCKBIT_TEMPLATE_FINGERPRINT,
    STOCKBIT_TEMPLATE_ID,
    StockbitCashEvidence,
    StockbitDocumentIdentity,
    StockbitEvidenceRole,
    StockbitHoldingSnapshot,
    StockbitPortfolioValuation,
    StockbitStatementAdapter,
    StockbitTradeEvidence,
    build_account_fingerprint,
    build_natural_document_key,
    build_row_evidence_key,
    extract_header_metadata,
    handle_unrecognized_row,
    normalize_account_string,
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
    out.write(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("latin-1"))

    out.write(
        f"trailer\n<< /Size {len(offsets)} /Root {catalog_obj_num} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
    )
    return out.getvalue()


SYNTHETIC_HEADER = """PT. STOCKBIT SEKURITAS DIGITAL
Statement of Account
Bank / Ccy / SID : JAGO 1099887766 / IDR SID987654321
Period : 01/10/2025 - 31/10/2025
Account / Sub Account : 1099887766 1 / SYNTH_CLIENT_001
"""

SYNTHETIC_TABLE_HEADER = """Tr. Date Due Date Reference Description Db Amount Cr Amount Balance
Ending Balance : 15,000,000.00
"""


class TestUniversalIngestionPhase3Stockbit(unittest.TestCase):
    """20 focused tests for Stockbit SOA source contract and adapter skeleton."""

    def _make_adapter_input(
        self,
        payload: bytes | None = None,
        text_payload: str | None = None,
        source_registry_id: str = STOCKBIT_SOURCE_REGISTRY_ID,
        template_id: str = STOCKBIT_TEMPLATE_ID,
        template_match_status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
    ) -> AdapterInput:
        h = sha256(payload).hexdigest() if payload else (sha256(text_payload.encode()).hexdigest() if text_payload else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        return AdapterInput(
            source_document_id="test-stockbit-doc-1",
            content_sha256=h,
            source_registry_id=source_registry_id,
            template_id=template_id,
            parser_version=STOCKBIT_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=template_match_status,
            period_status=PeriodStatus.CLOSED,
            template_fingerprint=STOCKBIT_TEMPLATE_FINGERPRINT,
            binary_payload=payload,
            text_payload=text_payload,
        )

    # 1. Recognized STOCKBIT_SOA document
    def test_01_recognized_stockbit_soa_document(self) -> None:
        raw_text = SYNTHETIC_HEADER + "\n" + SYNTHETIC_TABLE_HEADER
        pdf_bytes = make_multipage_pdf([raw_text])

        detection = detect_template(raw_text, media_type="PDF")
        self.assertEqual(detection.status, TemplateMatchStatus.KNOWN)
        self.assertEqual(detection.source_registry_id, STOCKBIT_SOURCE_REGISTRY_ID)
        self.assertEqual(detection.template_id, STOCKBIT_TEMPLATE_ID)

        adapter = StockbitStatementAdapter()
        inp = self._make_adapter_input(payload=pdf_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(res.period_status, PeriodStatus.CLOSED)
        self.assertEqual(res.period_start, "2025-10-01")
        self.assertEqual(res.period_end, "2025-10-31")
        self.assertIsNotNone(res.natural_document_key_candidate)

    # 2. Unsupported document fails closed
    def test_02_unsupported_document_fails_closed(self) -> None:
        adapter = StockbitStatementAdapter()
        # Non-matching template markers
        invalid_pdf = make_multipage_pdf(["Arbitrary report without required markers."])
        inp = self._make_adapter_input(payload=invalid_pdf)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(any(d.code == "STOCKBIT_TEMPLATE_CONTENT_UNCONFIRMED" for d in res.diagnostics))

        # Mismatched template_id raises contract error during input validation
        mismatched_inp = self._make_adapter_input(payload=invalid_pdf, template_id="other_template_v1")
        with self.assertRaises(AdapterContractError):
            adapter.parse(mismatched_inp)

    # 3. Period is taken from the authoritative header
    def test_03_period_is_taken_from_authoritative_header(self) -> None:
        header = """PT. STOCKBIT SEKURITAS DIGITAL
Statement of Account
Period : 01/03/2026 - 31/03/2026
Bank / Ccy / SID : JAGO 11223344 / IDR SID1234
"""
        meta = extract_header_metadata(header)
        self.assertEqual(meta.period_start, "2026-03-01")
        self.assertEqual(meta.period_end, "2026-03-31")
        self.assertFalse(meta.review_required)

    # 4. Date-like text in the body cannot establish document period
    def test_04_date_like_text_in_body_cannot_establish_period(self) -> None:
        # Header missing Period line, but body full of transaction dates
        body_with_dates = """PT. STOCKBIT SEKURITAS DIGITAL
Statement of Account
Bank / Ccy / SID : JAGO 11223344 / IDR SID1234
15/10/2025 17/10/2025 Trx on RG FAST B 100 @ 200.00
20/10/2025 22/10/2025 Trx on RG GOTO S 500 @ 80.00
"""
        meta = extract_header_metadata(body_with_dates)
        self.assertIsNone(meta.period_start)
        self.assertIsNone(meta.period_end)
        self.assertTrue(meta.review_required)
        self.assertEqual(meta.review_reason, "MISSING_STATEMENT_PERIOD")

    # 5. Missing period returns unknown/review-required
    def test_05_missing_period_returns_unknown_and_review_required(self) -> None:
        header_no_period = """PT. STOCKBIT SEKURITAS DIGITAL
Statement of Account
Bank / Ccy / SID : JAGO 11223344 / IDR SID1234
Client Code : TESTUSER01
"""
        meta = extract_header_metadata(header_no_period)
        self.assertTrue(meta.review_required)
        self.assertEqual(meta.review_reason, "MISSING_STATEMENT_PERIOD")
        self.assertIsNone(meta.natural_document_key_candidate)

    # 6. Stable account fingerprint
    def test_06_stable_account_fingerprint(self) -> None:
        raw_code_1 = " SYNTH_ACC_9988 "
        raw_code_2 = "synth-acc.9988"
        fp1 = build_account_fingerprint(raw_code_1)
        fp2 = build_account_fingerprint(raw_code_2)
        self.assertIsNotNone(fp1)
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

    # 7. Raw account number is absent from public identity output
    def test_07_raw_account_number_absent_from_public_identity(self) -> None:
        raw_account = "CONFIDENTIAL_RDN_77665544"
        header = f"""PT. STOCKBIT SEKURITAS DIGITAL
Statement of Account
Bank / Ccy / SID : JAGO {raw_account} / IDR SID999
Period : 01/10/2025 - 31/10/2025
"""
        meta = extract_header_metadata(header)
        self.assertNotIn(raw_account, meta.natural_document_key_candidate or "")
        self.assertNotEqual(meta.account_fingerprint, raw_account)
        self.assertEqual(len(meta.account_fingerprint or ""), 64)

    # 8. Natural document key is deterministic
    def test_08_natural_document_key_is_deterministic(self) -> None:
        fp = sha256(b"TESTFINGERPRINT").hexdigest()
        key1 = build_natural_document_key(
            source_type=STOCKBIT_SOURCE_REGISTRY_ID,
            statement_type="statement_of_account",
            account_fingerprint=fp,
            period_start="2025-10-01",
            period_end="2025-10-31",
        )
        key2 = build_natural_document_key(
            source_type=STOCKBIT_SOURCE_REGISTRY_ID,
            statement_type="statement_of_account",
            account_fingerprint=fp,
            period_start="2025-10-01",
            period_end="2025-10-31",
        )
        self.assertEqual(key1, key2)
        self.assertEqual(key1, f"stockbit_soa:statement_of_account:{fp}:2025-10-01:2025-10-31")

    # 9. Filename changes do not change natural document key
    def test_09_filename_changes_do_not_change_natural_key(self) -> None:
        header = SYNTHETIC_HEADER
        meta1 = extract_header_metadata(header, source_file_name="original_statement_oct2025.pdf")
        meta2 = extract_header_metadata(header, source_file_name="renamed_download_file_(1).pdf")
        self.assertEqual(meta1.natural_document_key_candidate, meta2.natural_document_key_candidate)

    # 10. Byte-level duplicate preserves logical document identity
    def test_10_byte_level_duplicate_preserves_logical_identity(self) -> None:
        header = SYNTHETIC_HEADER
        meta1 = extract_header_metadata(header, content_sha256="1111111111111111111111111111111111111111111111111111111111111111")
        meta2 = extract_header_metadata(header, content_sha256="2222222222222222222222222222222222222222222222222222222222222222")
        self.assertEqual(meta1.natural_document_key_candidate, meta2.natural_document_key_candidate)

    # 11. Different statement type produces a different natural key
    def test_11_different_statement_type_produces_different_key(self) -> None:
        fp = sha256(b"TESTFINGERPRINT").hexdigest()
        key_soa = build_natural_document_key(
            statement_type="statement_of_account",
            account_fingerprint=fp,
            period_start="2025-10-01",
            period_end="2025-10-31",
        )
        key_portfolio = build_natural_document_key(
            statement_type="portfolio_statement",
            account_fingerprint=fp,
            period_start="2025-10-01",
            period_end="2025-10-31",
        )
        self.assertNotEqual(key_soa, key_portfolio)

    # 12. Different account fingerprint produces a different natural key
    def test_12_different_account_produces_different_key(self) -> None:
        fp1 = build_account_fingerprint("CLIENT_A")
        fp2 = build_account_fingerprint("CLIENT_B")
        key1 = build_natural_document_key(
            account_fingerprint=fp1,
            period_start="2025-10-01",
            period_end="2025-10-31",
        )
        key2 = build_natural_document_key(
            account_fingerprint=fp2,
            period_start="2025-10-01",
            period_end="2025-10-31",
        )
        self.assertNotEqual(key1, key2)

    # 13. Holding snapshot cannot emit cash movement
    def test_13_holding_snapshot_cannot_emit_cash_movement(self) -> None:
        holding = StockbitHoldingSnapshot(
            snapshot_date="2025-10-31",
            ticker="BBCA",
            source_security_name="Bank Central Asia Tbk",
            quantity=Decimal("1000"),
            average_price=Decimal("9500.00"),
            closing_price=Decimal("10000.00"),
            market_value=Decimal("10000000.00"),
        )
        self.assertFalse(holding.emits_cash_movement)
        self.assertEqual(holding.evidence_role, StockbitEvidenceRole.HOLDING_SNAPSHOT.value)

    # 14. Portfolio valuation cannot emit cash movement
    def test_14_portfolio_valuation_cannot_emit_cash_movement(self) -> None:
        val = StockbitPortfolioValuation(
            valuation_date="2025-10-31",
            cash_investor=Decimal("5000000.00"),
            cash_balance=Decimal("5000000.00"),
            portfolio_value=Decimal("25000000.00"),
            equity_or_nav=Decimal("30000000.00"),
        )
        self.assertFalse(val.emits_cash_movement)
        self.assertEqual(val.evidence_role, StockbitEvidenceRole.PORTFOLIO_VALUATION.value)

    # 15. Unrealized gain/loss cannot emit income or expense
    def test_15_unrealized_gain_loss_cannot_emit_income_or_expense(self) -> None:
        holding = StockbitHoldingSnapshot(
            snapshot_date="2025-10-31",
            ticker="BBCA",
            source_security_name="Bank Central Asia",
            quantity=Decimal("1000"),
            average_price=Decimal("9000.00"),
            closing_price=Decimal("10000.00"),
            market_value=Decimal("10000000.00"),
            source_unrealized_gain_loss=Decimal("1000000.00"),
            source_unrealized_percentage=Decimal("11.11"),
        )
        self.assertFalse(holding.is_income_or_expense)
        self.assertFalse(holding.emits_cash_movement)

    # 16. Trade date and settlement date remain separate
    def test_16_trade_date_and_settlement_date_remain_separate(self) -> None:
        trade = StockbitTradeEvidence(
            trade_date="2025-10-15",
            settlement_date="2025-10-17",
            contract_reference="TRX-10101",
            source_action="BUY",
            ticker="TLKM",
            quantity=Decimal("500"),
            price=Decimal("3500.00"),
            gross_amount=Decimal("1750000.00"),
            net_settlement_amount=Decimal("1753000.00"),
        )
        self.assertEqual(trade.trade_date, "2025-10-15")
        self.assertEqual(trade.settlement_date, "2025-10-17")
        self.assertNotEqual(trade.trade_date, trade.settlement_date)

    # 17. Jago RDN evidence requires cross-source matching
    def test_17_jago_rdn_evidence_requires_cross_source_match(self) -> None:
        cash = StockbitCashEvidence(
            transaction_date="2025-10-17",
            due_date="2025-10-17",
            source_reference="SETTLE-101",
            source_description="Trx on RG TLKM B",
            source_debit=Decimal("1753000.00"),
            source_credit=None,
            signed_amount=Decimal("-1753000.00"),
            running_balance=Decimal("8247000.00"),
            source_row_ordinal=1,
            row_evidence_key="row-ev-101",
            requires_cross_source_match=True,
        )
        self.assertTrue(cash.requires_cross_source_match)

    # 18. Row ordinal is provenance, not preferred identity
    def test_18_row_ordinal_is_provenance_not_primary_identity(self) -> None:
        doc_key = "stockbit_soa:statement_of_account:testfp:2025-10-01:2025-10-31"
        key_ord1, review1, _ = build_row_evidence_key(
            natural_document_key=doc_key,
            evidence_role="CASH_EVIDENCE",
            transaction_date="2025-10-15",
            due_date="2025-10-17",
            source_reference="REF-8899",
            source_description="Trx on RG WEHA B",
            signed_amount=Decimal("-500000.00"),
            running_balance=Decimal("4500000.00"),
            source_row_ordinal=1,
        )
        key_ord2, review2, _ = build_row_evidence_key(
            natural_document_key=doc_key,
            evidence_role="CASH_EVIDENCE",
            transaction_date="2025-10-15",
            due_date="2025-10-17",
            source_reference="REF-8899",
            source_description="Trx on RG WEHA B",
            signed_amount=Decimal("-500000.00"),
            running_balance=Decimal("4500000.00"),
            source_row_ordinal=2,
        )
        self.assertEqual(key_ord1, key_ord2)
        self.assertFalse(review1)
        self.assertFalse(review2)

    # 19. Missing stable row identity becomes review-required
    def test_19_missing_stable_row_identity_becomes_review_required(self) -> None:
        doc_key = "stockbit_soa:statement_of_account:testfp:2025-10-01:2025-10-31"
        # Missing reference and missing running balance -> falls back to row ordinal
        key, review_req, reason = build_row_evidence_key(
            natural_document_key=doc_key,
            evidence_role="CASH_EVIDENCE",
            transaction_date="2025-10-15",
            source_description="Ambiguous fee row",
            signed_amount=None,
            running_balance=None,
            source_row_ordinal=5,
        )
        self.assertTrue(review_req)
        self.assertEqual(reason, "UNSTABLE_ROW_IDENTITY")
        self.assertIsNotNone(key)

    # 20. Unrecognized corporate-action-like evidence fails closed
    def test_20_unrecognized_corporate_action_fails_closed(self) -> None:
        doc_key = "stockbit_soa:statement_of_account:testfp:2025-10-01:2025-10-31"
        raw_event = "CORP ACTION STOCK SPLIT 1:5 RATIO BBCA"
        diag, row_key = handle_unrecognized_row(raw_event, row_ordinal=3, natural_document_key=doc_key)
        self.assertEqual(diag.code, "UNRECOGNIZED_STOCKBIT_EVENT")
        self.assertEqual(diag.severity, DiagnosticSeverity.WARNING)
        self.assertTrue(diag.review_required)
        self.assertIsNotNone(row_key)


if __name__ == "__main__":
    unittest.main()
