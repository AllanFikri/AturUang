import unittest
from decimal import Decimal

from aturuang.ingestion_adapter import (
    AdapterContractError,
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    AdapterResult,
    AmountComponentEvidence,
    BalanceSnapshotEvidence,
    CashMovementEvidence,
    CommerceLineItemEvidence,
    CommerceOrderEvidence,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    InvestmentTradeEvidence,
    NormalizedEventEnvelope,
    ObservedAccountEvidence,
    PaymentComponentEvidence,
    SafeDiagnostic,
    SnapshotKind,
    SourceEventStatus,
    SourceSummaryEvidence,
    UniversalSourceAdapter,
    validate_adapter_input,
)
from aturuang.ingestion_contracts import (
    ConfidenceLevel,
    PeriodStatus,
    ReferenceEvidenceContract,
    SourceChannel,
    SourceProvenanceContract,
    TemplateMatchStatus,
)


def sha(char: str) -> str:
    return char * 64


def descriptor(
    *,
    source_registry_id: str = "bca_statement",
    template_id: str = "bca_monthly_statement_v1",
    parser_version: str = "phase2c-b-contract",
    source_channel: SourceChannel = SourceChannel.PDF,
) -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id="adapter-bca-v1",
        source_registry_id=source_registry_id,
        template_id=template_id,
        parser_version=parser_version,
        source_channel=source_channel,
    )


def adapter_input(
    *,
    source_registry_id: str = "bca_statement",
    template_id: str = "bca_monthly_statement_v1",
    parser_version: str = "phase2c-b-contract",
    source_channel: SourceChannel = SourceChannel.PDF,
    binary_payload: bytes | None = b"%PDF synthetic",
    text_payload: str | None = None,
    structured_payload=None,
) -> AdapterInput:
    return AdapterInput(
        source_document_id="doc-001",
        content_sha256=sha("a"),
        source_registry_id=source_registry_id,
        template_id=template_id,
        parser_version=parser_version,
        source_channel=source_channel,
        template_match_status=TemplateMatchStatus.KNOWN,
        period_status=PeriodStatus.CLOSED,
        template_fingerprint="fingerprint-v1",
        binary_payload=binary_payload,
        text_payload=text_payload,
        structured_payload=structured_payload,
    )


def provenance(
    source_document_id: str = "doc-001",
) -> SourceProvenanceContract:
    return SourceProvenanceContract(
        source_document_id=source_document_id,
        raw_locator=r"C:\Private\Statements\source.pdf",
        page_number=1,
        row_index=2,
        raw_text="PRIVATE RAW TEXT",
        raw_reference="PRIVATE-REF-123",
        raw_description="PRIVATE DESCRIPTION",
    )


def cash_payload() -> CashMovementEvidence:
    return CashMovementEvidence(
        amount=Decimal("125000.50"),
        currency="IDR",
        direction=EventDirection.OUTFLOW,
        status=SourceEventStatus.POSTED,
        occurred_at="2026-08-28T10:00:00",
        posted_at="2026-08-28T10:01:00",
        settlement_date="2026-08-29",
        direction_raw="DB",
        counterparty_raw="PRIVATE COUNTERPARTY",
        description_raw="PRIVATE DESCRIPTION",
        provider_transaction_id_raw="RAW-TX-ID",
        reference_raw="RAW-REFERENCE",
        reference_normalized="NORMALIZEDREFERENCE",
        balance_after=Decimal("1000000.00"),
        payment_method_raw="BCA",
        event_hint="TRANSFER",
    )


def envelope(
    *,
    source_registry_id: str = "bca_statement",
    template_id: str = "bca_monthly_statement_v1",
    parser_version: str = "phase2c-b-contract",
    source_channel: SourceChannel = SourceChannel.PDF,
    role: EventRole = EventRole.CASH_MOVEMENT,
    payload=None,
) -> NormalizedEventEnvelope:
    if payload is None:
        payload = cash_payload()

    return NormalizedEventEnvelope(
        source_document_id="doc-001",
        source_registry_id=source_registry_id,
        template_id=template_id,
        parser_version=parser_version,
        source_channel=source_channel,
        event_role=role,
        source_event_id="provider-event-001",
        row_fingerprint=sha("b"),
        evidence_quality=ConfidenceLevel.HIGH,
        parse_confidence=ConfidenceLevel.HIGH,
        provenance=provenance(),
        payload=payload,
    )


class TestUniversalIngestionPhase2Adapter(unittest.TestCase):
    def test_01_adapter_input_supports_all_source_channels_without_path(self):
        cases = (
            (
                SourceChannel.PDF,
                {"binary_payload": b"pdf", "text_payload": None},
            ),
            (
                SourceChannel.IMAGE,
                {"binary_payload": b"image", "text_payload": None},
            ),
            (
                SourceChannel.EMAIL,
                {"binary_payload": None, "text_payload": "email"},
            ),
            (
                SourceChannel.CSV,
                {"binary_payload": None, "text_payload": "a,b"},
            ),
            (
                SourceChannel.API,
                {
                    "binary_payload": None,
                    "text_payload": None,
                    "structured_payload": {"id": "x"},
                },
            ),
            (
                SourceChannel.MANUAL,
                {
                    "binary_payload": None,
                    "text_payload": None,
                    "structured_payload": {"amount": "1"},
                },
            ),
        )

        for channel, payload_args in cases:
            source = adapter_input(
                source_channel=channel,
                **payload_args,
            )
            desc = descriptor(source_channel=channel)
            validate_adapter_input(desc, source)
            self.assertFalse(hasattr(source, "path"))
            self.assertFalse(hasattr(source, "file_path"))

    def test_02_private_input_payload_is_not_in_repr(self):
        source = adapter_input(binary_payload=b"TOP-SECRET-BYTES")
        rendered = repr(source)
        self.assertNotIn("TOP-SECRET-BYTES", rendered)

    def test_03_source_channel_mismatch_fails_closed(self):
        desc = descriptor(source_channel=SourceChannel.PDF)
        source = adapter_input(source_channel=SourceChannel.IMAGE)

        with self.assertRaises(AdapterContractError):
            validate_adapter_input(desc, source)

    def test_04_source_registry_mismatch_fails_closed(self):
        desc = descriptor(source_registry_id="bca_statement")
        source = adapter_input(source_registry_id="jago_statement")

        with self.assertRaises(AdapterContractError):
            validate_adapter_input(desc, source)

    def test_05_template_mismatch_fails_closed(self):
        desc = descriptor(template_id="bca_monthly_statement_v1")
        source = adapter_input(template_id="jago_monthly_statement_v1")

        with self.assertRaises(AdapterContractError):
            validate_adapter_input(desc, source)

    def test_06_parser_version_mismatch_fails_closed(self):
        desc = descriptor(parser_version="parser-a")
        source = adapter_input(parser_version="parser-b")

        with self.assertRaises(AdapterContractError):
            validate_adapter_input(desc, source)

    def test_07_protocol_has_no_db_ledger_or_apply_surface(self):
        names = set(UniversalSourceAdapter.__dict__)
        forbidden = {
            "apply",
            "commit",
            "db",
            "ledger",
            "save",
            "write",
            "mutate_balance",
        }
        self.assertFalse(names & forbidden)
        self.assertIn("parse", names)

    def test_08_valid_cash_movement_envelope(self):
        event = envelope()
        self.assertEqual(event.event_role, EventRole.CASH_MOVEMENT)
        self.assertIsInstance(event.payload, CashMovementEvidence)
        self.assertEqual(event.payload.amount, Decimal("125000.50"))

    def test_09_occurred_posted_and_settlement_dates_remain_distinct(self):
        event = envelope()
        payload = event.payload
        self.assertNotEqual(payload.occurred_at, payload.posted_at)
        self.assertNotEqual(payload.posted_at, payload.settlement_date)
        self.assertEqual(payload.settlement_date, "2026-08-29")

    def test_10_decimal_amount_rejects_float_nan_and_infinity(self):
        with self.assertRaises(AdapterContractError):
            PaymentComponentEvidence(
                "cash",
                1.25,
                "IDR",
            )

        for value in (Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(AdapterContractError):
                PaymentComponentEvidence("cash", value, "IDR")

    def test_11_direction_is_not_expense_or_income_semantics(self):
        values = {item.value for item in EventDirection}
        self.assertEqual(
            values,
            {"INFLOW", "OUTFLOW", "NEUTRAL", "UNKNOWN"},
        )
        self.assertNotIn("EXPENSE", values)
        self.assertNotIn("INCOME", values)

    def test_12_source_statuses_preserve_failure_refund_and_reversal(self):
        expected = {
            "POSTED",
            "PENDING",
            "FAILED",
            "REVERSED",
            "REFUNDED",
            "CANCELLED",
            "UNKNOWN",
        }
        self.assertEqual(
            {item.value for item in SourceEventStatus},
            expected,
        )

    def test_13_balance_snapshot_is_not_cash_movement(self):
        payload = BalanceSnapshotEvidence(
            balance=Decimal("2000000.00"),
            currency="IDR",
            snapshot_kind=SnapshotKind.CLOSING,
            observed_at="2026-07-31",
        )
        event = envelope(
            role=EventRole.BALANCE_SNAPSHOT,
            payload=payload,
        )

        self.assertIsInstance(event.payload, BalanceSnapshotEvidence)
        self.assertNotIsInstance(event.payload, CashMovementEvidence)

    def test_14_source_summary_supports_statement_equation_inputs(self):
        payload = SourceSummaryEvidence(
            currency="IDR",
            period_start="2026-07-01",
            period_end="2026-07-31",
            opening_balance=Decimal("100.00"),
            incoming_total=Decimal("50.00"),
            outgoing_total=Decimal("25.00"),
            closing_balance=Decimal("125.00"),
        )
        event = envelope(
            role=EventRole.SOURCE_SUMMARY,
            payload=payload,
        )

        self.assertEqual(event.payload.closing_balance, Decimal("125.00"))

    def test_15_observed_account_does_not_resolve_lifecycle_or_ownership(self):
        payload = ObservedAccountEvidence(
            observed_provider_account_key="pocket-slot-07",
            display_name_raw="Private Pocket Name",
            institution_id="jago",
            parent_observed_key="main",
            provider_state_raw="ACTIVE",
            observed_at="2026-07-31",
        )
        event = envelope(
            role=EventRole.ACCOUNT_OBSERVATION,
            payload=payload,
        )

        self.assertFalse(hasattr(event.payload, "ownership_state"))
        self.assertFalse(hasattr(event.payload, "lifecycle_state"))
        self.assertFalse(hasattr(event.payload, "account_id"))

    def test_16_investment_trade_preserves_trade_and_settlement_dates(self):
        payload = InvestmentTradeEvidence(
            instrument_raw="BBCA",
            trade_date="2026-07-10",
            settlement_date="2026-07-14",
            side_raw="BUY",
            quantity=Decimal("10"),
            unit_price=Decimal("9000"),
            currency="IDR",
            gross_amount=Decimal("90000"),
            net_amount=Decimal("90100"),
        )
        event = envelope(
            role=EventRole.INVESTMENT_TRADE,
            payload=payload,
        )

        self.assertNotEqual(
            event.payload.trade_date,
            event.payload.settlement_date,
        )

    def test_17_commerce_evidence_has_no_owner_or_payer_inference(self):
        line = CommerceLineItemEvidence(
            line_key="line-1",
            product_name_raw="Private Product",
            quantity=Decimal("1"),
            line_subtotal=Decimal("100000"),
            currency="IDR",
        )
        payload = CommerceOrderEvidence(
            order_native_id_raw="ORDER-RAW-001",
            order_date="2026-08-20",
            order_total=Decimal("105000"),
            currency="IDR",
            payment_method_raw="QRIS",
            line_items=(line,),
        )
        event = envelope(
            role=EventRole.COMMERCE_ORDER,
            payload=payload,
        )

        self.assertFalse(hasattr(event.payload, "economic_owner"))
        self.assertFalse(hasattr(event.payload, "payer_responsibility"))
        self.assertFalse(hasattr(event.payload, "personal_spending_effect"))

    def test_18_multiple_payment_components_are_preserved(self):
        payload = CashMovementEvidence(
            amount=Decimal("11000"),
            currency="IDR",
            direction=EventDirection.OUTFLOW,
            status=SourceEventStatus.POSTED,
            payment_components=(
                PaymentComponentEvidence(
                    "GoPay Balance",
                    Decimal("10000"),
                    "IDR",
                ),
                PaymentComponentEvidence(
                    "GoPay Coins",
                    Decimal("1000"),
                    "IDR",
                ),
            ),
        )

        self.assertEqual(len(payload.payment_components), 2)
        self.assertEqual(
            sum(item.amount for item in payload.payment_components),
            Decimal("11000"),
        )

    def test_19_row_fingerprint_must_be_sha256(self):
        with self.assertRaises(AdapterContractError):
            NormalizedEventEnvelope(
                source_document_id="doc-001",
                source_registry_id="bca_statement",
                template_id="bca_monthly_statement_v1",
                parser_version="phase2c-b-contract",
                source_channel=SourceChannel.PDF,
                event_role=EventRole.CASH_MOVEMENT,
                source_event_id=None,
                row_fingerprint="not-a-hash",
                evidence_quality=ConfidenceLevel.HIGH,
                parse_confidence=ConfidenceLevel.HIGH,
                provenance=provenance(),
                payload=cash_payload(),
            )

    def test_20_private_provenance_repr_is_redacted(self):
        item = provenance()
        rendered = repr(item)

        self.assertNotIn(r"C:\Private\Statements\source.pdf", rendered)
        self.assertNotIn("PRIVATE RAW TEXT", rendered)
        self.assertNotIn("PRIVATE-REF-123", rendered)
        self.assertNotIn("PRIVATE DESCRIPTION", rendered)

    def test_21_safe_diagnostics_reject_private_content(self):
        bad_messages = (
            r"failed at C:\Private\statement.pdf",
            "contact owner@example.com",
            "account 1234567890123456 failed",
        )

        for message in bad_messages:
            with self.assertRaises(AdapterContractError):
                SafeDiagnostic(
                    code="PRIVATE_DATA",
                    severity=DiagnosticSeverity.ERROR,
                    message=message,
                )

        safe = SafeDiagnostic(
            code="ROW_MALFORMED",
            severity=DiagnosticSeverity.WARNING,
            message="A source row could not be normalized.",
            review_required=True,
            locator_token="abcdef123456",
        )
        self.assertTrue(safe.review_required)

    def test_22_zero_activity_completed_result_is_valid(self):
        result = AdapterResult(
            descriptor=descriptor(),
            source_document_id="doc-001",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            events=(),
            period_start="2026-07-01",
            period_end="2026-07-31",
        )

        self.assertEqual(result.events, ())
        self.assertFalse(result.review_required)

    def test_23_failed_result_cannot_return_trusted_events(self):
        with self.assertRaises(AdapterContractError):
            AdapterResult(
                descriptor=descriptor(),
                source_document_id="doc-001",
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.CLOSED,
                events=(envelope(),),
            )

    def test_24_result_rejects_event_descriptor_mismatch(self):
        bad_event = envelope(source_registry_id="jago_statement")

        with self.assertRaises(AdapterContractError):
            AdapterResult(
                descriptor=descriptor(source_registry_id="bca_statement"),
                source_document_id="doc-001",
                parse_status=AdapterParseStatus.COMPLETED,
                period_status=PeriodStatus.CLOSED,
                events=(bad_event,),
            )

    def test_25_adapter_input_requires_exactly_one_private_payload(self):
        with self.assertRaises(AdapterContractError):
            adapter_input(
                binary_payload=None,
                text_payload=None,
                structured_payload=None,
            )

        with self.assertRaises(AdapterContractError):
            adapter_input(
                binary_payload=b"x",
                text_payload="y",
            )

    def test_26_reference_evidence_repr_does_not_leak_raw_ids(self):
        evidence = ReferenceEvidenceContract(
            raw_reference="SECRET-RAW-REFERENCE",
            normalized_reference="SECRETNORMALIZED",
            provider_transaction_id_raw="SECRET-TX-ID",
        )
        rendered = repr(evidence)

        self.assertNotIn("SECRET-RAW-REFERENCE", rendered)
        self.assertNotIn("SECRETNORMALIZED", rendered)
        self.assertNotIn("SECRET-TX-ID", rendered)


if __name__ == "__main__":
    unittest.main()