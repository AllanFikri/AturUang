from decimal import Decimal
import inspect
import sqlite3
import unittest
from hashlib import sha256

from aturuang.ingestion_adapter import (
    AccountPeriodSummaryEvidence,
    AdapterDescriptor,
    AdapterParseStatus,
    AdapterResult,
    BalanceSnapshotEvidence,
    CashMovementEvidence,
    CommerceOrderEvidence,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    InvestmentTradeEvidence,
    NormalizedEventEnvelope,
    ObservedAccountEvidence,
    SafeDiagnostic,
    SnapshotKind,
    SourceEventStatus,
    SourceSummaryEvidence,
)
from aturuang.ingestion_contracts import (
    ConfidenceLevel,
    DocumentIdentityStatus,
    PeriodStatus,
    SourceChannel,
    SourceProvenanceContract,
    TemplateMatchStatus,
)
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
    DiscoveryResult,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    AdapterCatalogError,
    DryRunBatchStatus,
    DryRunDisposition,
    SqliteRegistryAuthority,
    dry_run_artifact,
    dry_run_batch,
    safe_dry_run_batch_json,
    semantic_document_canonical_json,
    semantic_document_sha256,
)
from aturuang.ingestion_preflight import (
    DetectionMethod,
    DocumentPreflight,
    PdfEncryptionState,
    PreflightQuality,
    TemplateDetection,
)
from aturuang.ingestion_registry import init_registry_schema
from aturuang.ingestion_registry_service import (
    SourceDocumentReadRecord,
    TemplateResolution,
)


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def artifact(
    data: bytes,
    *,
    extension: str = ".pdf",
    locator: str = "private/statement.pdf",
    extra_extensions: tuple[str, ...] = (),
) -> DiscoveredArtifact:
    occurrences = [
        ArtifactOccurrence(
            source_locator=locator,
            extension=extension,
        )
    ]
    for index, extra in enumerate(extra_extensions, start=1):
        occurrences.append(
            ArtifactOccurrence(
                source_locator=f"private/copy-{index}{extra}",
                extension=extra,
            )
        )
    common = (
        extension
        if len({item.extension for item in occurrences}) == 1
        else ""
    )
    return DiscoveredArtifact(
        content_sha256=digest(data),
        size_bytes=len(data),
        extension=common,
        occurrences=tuple(occurrences),
    )


def known_preflight(
    data: bytes,
    *,
    source_registry_id: str = "bca_statement",
    template_id: str = "bca_monthly_statement_v1",
    template_fingerprint: str = "fingerprint-v1",
    status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
) -> DocumentPreflight:
    detection = TemplateDetection(
        status=status,
        source_registry_id=(
            source_registry_id
            if status is TemplateMatchStatus.KNOWN
            else None
        ),
        template_id=(
            template_id
            if status is TemplateMatchStatus.KNOWN
            else None
        ),
        template_fingerprint=(
            template_fingerprint
            if status is TemplateMatchStatus.KNOWN
            else None
        ),
        method=DetectionMethod.TEXT_MARKERS,
        required_marker_matches=4,
        required_marker_total=4,
        reason_code=(
            "EXACT_REQUIRED_MARKERS"
            if status is TemplateMatchStatus.KNOWN
            else status.value
        ),
    )
    return DocumentPreflight(
        content_sha256=digest(data),
        size_bytes=len(data),
        extension=".pdf",
        media_type="PDF",
        mime_type="application/pdf",
        quality_status=PreflightQuality.READY,
        page_count=1,
        image_dimensions=None,
        text_layer_available=True,
        pdf_encryption_state=PdfEncryptionState.UNENCRYPTED,
        period_status=PeriodStatus.UNKNOWN,
        template_detection=detection,
    )


class FakeRegistry:
    def __init__(
        self,
        *,
        exact=(),
        source_registry_id="bca_statement",
        channel="PDF",
        template_status=TemplateMatchStatus.KNOWN,
        template_id="bca_monthly_statement_v1",
        parser_version="parser-v1",
        natural=(),
    ):
        self.exact = tuple(exact)
        self.natural = tuple(natural)
        self.natural_calls = 0
        self.capability = {
            "source_registry_id": source_registry_id,
            "channel": channel,
        }
        self.resolution = TemplateResolution(
            status=template_status,
            template_id=(
                template_id
                if template_status is TemplateMatchStatus.KNOWN
                else None
            ),
            parser_version=(
                parser_version
                if template_status is TemplateMatchStatus.KNOWN
                else None
            ),
        )

    def lookup_exact_documents(self, content_sha256):
        return self.exact

    def lookup_natural_documents(
        self,
        source_registry_id,
        natural_document_key,
    ):
        self.natural_calls += 1
        return self.natural

    def source_capability(self, source_registry_id):
        return self.capability

    def template_authority(
        self,
        *,
        source_registry_id,
        template_fingerprint,
    ):
        return self.resolution


class SyntheticAdapter:
    def __init__(
        self,
        *,
        descriptor=None,
        parse_status=AdapterParseStatus.COMPLETED,
        natural_key="bca:main:2026-08",
        diagnostics=(),
    ):
        self.descriptor = descriptor or AdapterDescriptor(
            adapter_id="synthetic-bca",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        self.parse_status = parse_status
        self.natural_key = natural_key
        self.diagnostics = tuple(diagnostics)
        self.parse_calls = 0

    def parse(self, source):
        self.parse_calls += 1
        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=self.parse_status,
            period_status=source.period_status,
            events=(),
            diagnostics=self.diagnostics,
            natural_document_key_candidate=self.natural_key,
        )


def run_known(
    data: bytes,
    *,
    registry=None,
    adapter=None,
    claimed_source_registry_id=None,
    preflight_func=None,
):
    registry = registry or FakeRegistry()
    adapter = adapter or SyntheticAdapter()
    preflight_func = preflight_func or (
        lambda payload, **kwargs: known_preflight(payload)
    )
    return dry_run_artifact(
        "ignored-root",
        artifact(data),
        registry=registry,
        adapter_catalog=AdapterCatalog((adapter,)),
        claimed_source_registry_id=claimed_source_registry_id,
        payload_reader=lambda *args, **kwargs: data,
        preflight_func=preflight_func,
    ), adapter




def semantic_event(
    *,
    source_document_id="dryrun-doc",
    source_registry_id="bca_statement",
    template_id="template-v1",
    parser_version="parser-v1",
    role=EventRole.CASH_MOVEMENT,
    payload=None,
    row_seed="row",
    evidence_quality=ConfidenceLevel.HIGH,
    parse_confidence=ConfidenceLevel.HIGH,
    raw_locator="private/source.pdf",
    raw_text="raw evidence",
    diagnostics=(),
):
    if payload is None:
        payload = CashMovementEvidence(
            amount=Decimal("1"),
            currency="IDR",
            direction=EventDirection.OUTFLOW,
            status=SourceEventStatus.POSTED,
            occurred_at="2026-08-01",
            description_raw="Merchant Alpha",
        )
    return NormalizedEventEnvelope(
        source_document_id=source_document_id,
        source_registry_id=source_registry_id,
        template_id=template_id,
        parser_version=parser_version,
        source_channel=SourceChannel.PDF,
        event_role=role,
        source_event_id="row-source-id",
        row_fingerprint=digest(row_seed.encode("utf-8")),
        evidence_quality=evidence_quality,
        parse_confidence=parse_confidence,
        provenance=SourceProvenanceContract(
            source_document_id=source_document_id,
            raw_locator=raw_locator,
            raw_text=raw_text,
        ),
        payload=payload,
        diagnostics=tuple(diagnostics),
    )


def semantic_result(
    events,
    *,
    source_document_id="dryrun-doc",
    source_registry_id="bca_statement",
    template_id="template-v1",
    parser_version="parser-v1",
    natural_key="bca:main:2026-08",
    period_status=PeriodStatus.CLOSED,
    period_start="2026-08-01",
    period_end="2026-08-31",
):
    descriptor = AdapterDescriptor(
        adapter_id="semantic-test",
        source_registry_id=source_registry_id,
        template_id=template_id,
        parser_version=parser_version,
        source_channel=SourceChannel.PDF,
    )
    return AdapterResult(
        descriptor=descriptor,
        source_document_id=source_document_id,
        parse_status=AdapterParseStatus.COMPLETED,
        period_status=period_status,
        events=tuple(events),
        period_start=period_start,
        period_end=period_end,
        natural_document_key_candidate=natural_key,
    )


def semantic_sha_for_result(result):
    return semantic_document_sha256(
        result.descriptor.source_registry_id,
        result.natural_document_key_candidate,
        result,
    )

class TestUniversalIngestionPhase2Orchestration(unittest.TestCase):
    def test_13_exact_sha_registry_duplicate_short_circuits_before_adapter(self):
        data = b"exact-duplicate"
        sha_value = digest(data)

        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        init_registry_schema(con)
        try:
            con.execute(
                """
                INSERT INTO registry_institutions (
                    institution_id, display_name, institution_type
                ) VALUES ('bca', 'BCA', 'BANK')
                """
            )
            con.execute(
                """
                INSERT INTO registry_sources (
                    source_registry_id, provider_key, display_name,
                    channel, institution_id
                ) VALUES (
                    'bca_statement', 'bca', 'BCA Statement',
                    'PDF', 'bca'
                )
                """
            )
            con.execute(
                """
                INSERT INTO registry_import_batches (
                    import_batch_id, source_registry_id, mode,
                    idempotency_key
                ) VALUES (
                    'batch-1', 'bca_statement', 'DRY_RUN', 'key-1'
                )
                """
            )
            con.execute(
                """
                INSERT INTO registry_source_documents (
                    source_document_id, source_registry_id,
                    import_batch_id, content_sha256,
                    natural_document_key, semantic_sha256,
                    template_id, template_fingerprint,
                    parser_version, template_match_status,
                    period_status, identity_status
                ) VALUES (
                    'doc-existing', 'bca_statement',
                    'batch-1', ?, 'bca:main:2026-08', NULL,
                    'bca_monthly_statement_v1', 'fingerprint-v1',
                    'parser-v1', 'KNOWN', 'CLOSED', 'NEW'
                )
                """,
                (sha_value,),
            )

            adapter = SyntheticAdapter()
            reader_calls = []

            def forbidden_reader(*args, **kwargs):
                reader_calls.append(True)
                raise AssertionError("payload replay must not run")

            result = dry_run_artifact(
                "ignored-root",
                artifact(data),
                registry=SqliteRegistryAuthority(con),
                adapter_catalog=AdapterCatalog((adapter,)),
                payload_reader=forbidden_reader,
            )

            self.assertEqual(
                result.disposition,
                DryRunDisposition.SKIPPED_DUPLICATE,
            )
            self.assertEqual(
                result.identity_status,
                DocumentIdentityStatus.EXACT_DUPLICATE,
            )
            self.assertEqual(
                result.source_document_id,
                "doc-existing",
            )
            self.assertEqual(reader_calls, [])
            self.assertEqual(adapter.parse_calls, 0)
        finally:
            con.close()

    def test_14_exact_sha_ambiguity_fails_closed(self):
        data = b"ambiguous-exact"
        record_a = SourceDocumentReadRecord(
            source_document_id="doc-a",
            source_registry_id="one",
            content_sha256=digest(data),
            natural_document_key="one",
            semantic_sha256=None,
        )
        record_b = SourceDocumentReadRecord(
            source_document_id="doc-b",
            source_registry_id="two",
            content_sha256=digest(data),
            natural_document_key="two",
            semantic_sha256=None,
        )
        registry = FakeRegistry(exact=(record_a, record_b))
        adapter = SyntheticAdapter()

        result = dry_run_artifact(
            "ignored-root",
            artifact(data),
            registry=registry,
            adapter_catalog=AdapterCatalog((adapter,)),
            payload_reader=lambda *args, **kwargs: data,
        )

        self.assertEqual(result.disposition, DryRunDisposition.FAILED)
        self.assertEqual(result.diagnostics[0].code, "EXACT_SHA_AMBIGUOUS")
        self.assertEqual(adapter.parse_calls, 0)

    def test_15_preflight_content_detection_runs_without_source_hint_authority(self):
        data = b"content-only"
        seen = {}

        def spy(payload, **kwargs):
            seen.update(kwargs)
            return known_preflight(payload)

        result, adapter = run_known(
            data,
            preflight_func=spy,
        )

        self.assertNotIn("source_hint", seen)
        self.assertEqual(
            result.disposition,
            DryRunDisposition.READY_FOR_STAGING,
        )
        self.assertEqual(adapter.parse_calls, 1)

    def test_16_matching_source_claim_continues(self):
        result, adapter = run_known(
            b"matching-claim",
            claimed_source_registry_id="bca_statement",
        )

        self.assertEqual(
            result.disposition,
            DryRunDisposition.READY_FOR_STAGING,
        )
        self.assertEqual(adapter.parse_calls, 1)

    def test_17_conflicting_source_claim_becomes_review_required(self):
        result, adapter = run_known(
            b"conflicting-claim",
            claimed_source_registry_id="jago_statement",
        )

        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(
            result.diagnostics[-1].code,
            "SOURCE_CLAIM_CONFLICT",
        )
        self.assertEqual(adapter.parse_calls, 0)

    def test_18_unknown_template_never_reaches_adapter(self):
        data = b"unknown-template"
        adapter = SyntheticAdapter()

        result, adapter = run_known(
            data,
            adapter=adapter,
            preflight_func=lambda payload, **kwargs: known_preflight(
                payload,
                status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
            ),
        )

        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(result.diagnostics[-1].code, "UNKNOWN_TEMPLATE")
        self.assertEqual(adapter.parse_calls, 0)

    def test_19_template_drift_never_reaches_adapter(self):
        data = b"template-drift"
        adapter = SyntheticAdapter()

        result, adapter = run_known(
            data,
            adapter=adapter,
            preflight_func=lambda payload, **kwargs: known_preflight(
                payload,
                status=TemplateMatchStatus.TEMPLATE_DRIFT,
            ),
        )

        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(result.diagnostics[-1].code, "TEMPLATE_DRIFT")
        self.assertEqual(adapter.parse_calls, 0)

    def test_20_registry_template_mismatch_never_reaches_adapter(self):
        registry = FakeRegistry(
            template_id="different-template",
        )
        result, adapter = run_known(
            b"registry-mismatch",
            registry=registry,
        )

        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(
            result.diagnostics[-1].code,
            "REGISTRY_TEMPLATE_MISMATCH",
        )
        self.assertEqual(adapter.parse_calls, 0)

    def test_21_exact_adapter_catalog_key_selects_one_adapter(self):
        adapter = SyntheticAdapter()
        catalog = AdapterCatalog((adapter,))

        selected = catalog.select(
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )

        self.assertIs(selected, adapter)
        self.assertIsNone(
            catalog.select(
                source_registry_id="bca_statement",
                template_id="bca_monthly_statement_v1",
                parser_version="parser-v2",
                source_channel=SourceChannel.PDF,
            )
        )

    def test_22_missing_adapter_becomes_review_required(self):
        data = b"missing-adapter"
        result = dry_run_artifact(
            "ignored-root",
            artifact(data),
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog(()),
            payload_reader=lambda *args, **kwargs: data,
            preflight_func=lambda payload, **kwargs: known_preflight(
                payload
            ),
        )

        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(
            result.diagnostics[-1].code,
            "ADAPTER_NOT_AVAILABLE",
        )

    def test_23_duplicate_adapter_catalog_key_fails_closed(self):
        first = SyntheticAdapter()
        second = SyntheticAdapter()

        with self.assertRaises(AdapterCatalogError):
            AdapterCatalog((first, second))

    def test_24_descriptor_input_mismatch_fails_closed(self):
        data = b"descriptor-mismatch"
        adapter = SyntheticAdapter()
        catalog = AdapterCatalog((adapter,))
        adapter.descriptor = AdapterDescriptor(
            adapter_id="mutated",
            source_registry_id="jago_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )

        result = dry_run_artifact(
            "ignored-root",
            artifact(data),
            registry=FakeRegistry(),
            adapter_catalog=catalog,
            payload_reader=lambda *args, **kwargs: data,
            preflight_func=lambda payload, **kwargs: known_preflight(
                payload
            ),
        )

        self.assertEqual(result.disposition, DryRunDisposition.FAILED)
        self.assertEqual(
            result.diagnostics[-1].code,
            "ADAPTER_CATALOG_INVARIANT_FAILED",
        )
        self.assertEqual(adapter.parse_calls, 0)

    def test_25_dry_run_does_not_call_create_or_get_import_batch(self):
        import aturuang.ingestion_orchestration as module

        source = inspect.getsource(module)
        self.assertNotIn("create_or_get_import_batch", source)

    def test_26_dry_run_does_not_call_register_source_document(self):
        import aturuang.ingestion_orchestration as module

        source = inspect.getsource(module)
        self.assertNotIn("register_source_document", source)

    def test_27_dry_run_does_not_call_record_source_document_occurrence(self):
        import aturuang.ingestion_orchestration as module

        source = inspect.getsource(module)
        self.assertNotIn("record_source_document_occurrence", source)

    def test_28_zero_activity_completed_adapter_result_remains_valid(self):
        result, adapter = run_known(
            b"zero-activity",
            adapter=SyntheticAdapter(
                parse_status=AdapterParseStatus.COMPLETED,
            ),
        )

        self.assertEqual(adapter.parse_calls, 1)
        self.assertEqual(
            result.disposition,
            DryRunDisposition.READY_FOR_STAGING,
        )
        self.assertEqual(result.adapter_result.events, ())

    def test_29_adapter_failed_becomes_document_failure_without_ledger_mutation(self):
        result, adapter = run_known(
            b"adapter-failed",
            adapter=SyntheticAdapter(
                parse_status=AdapterParseStatus.FAILED,
            ),
        )

        self.assertEqual(adapter.parse_calls, 1)
        self.assertEqual(result.disposition, DryRunDisposition.FAILED)
        self.assertEqual(result.adapter_result.events, ())

    def test_30_safe_diagnostics_aggregate_without_raw_locator_leakage(self):
        data = b"safe-diagnostics"
        raw_locator = r"C:\Private\Bank\statement.pdf"
        adapter = SyntheticAdapter(
            diagnostics=(
                SafeDiagnostic(
                    code="ROW_REVIEW",
                    severity=DiagnosticSeverity.WARNING,
                    message="A source row requires review.",
                    review_required=True,
                    locator_token="abcdef123456",
                ),
            )
        )

        result = dry_run_artifact(
            "ignored-root",
            artifact(
                data,
                locator=raw_locator,
            ),
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog((adapter,)),
            payload_reader=lambda *args, **kwargs: data,
            preflight_func=lambda payload, **kwargs: known_preflight(
                payload
            ),
        )

        rendered = repr(result)
        self.assertNotIn(raw_locator, rendered)
        self.assertNotIn("Private", rendered)
        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(result.diagnostics[0].code, "ROW_REVIEW")


    def test_31_different_bytes_equivalent_evidence_same_semantic_sha(self):
        first = semantic_result(
            (semantic_event(source_document_id="dryrun-a", row_seed="a"),),
            source_document_id="dryrun-a",
        )
        second = semantic_result(
            (semantic_event(source_document_id="dryrun-b", row_seed="b"),),
            source_document_id="dryrun-b",
        )
        self.assertEqual(
            semantic_sha_for_result(first),
            semantic_sha_for_result(second),
        )

    def test_32_input_event_order_does_not_change_semantic_sha(self):
        a = semantic_event(
            payload=CashMovementEvidence(
                amount=Decimal("1"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                occurred_at="2026-08-01",
            ),
            row_seed="a",
        )
        b = semantic_event(
            payload=CashMovementEvidence(
                amount=Decimal("2"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                occurred_at="2026-08-02",
            ),
            row_seed="b",
        )
        self.assertEqual(
            semantic_sha_for_result(semantic_result((a, b))),
            semantic_sha_for_result(semantic_result((b, a))),
        )

    def test_33_duplicate_event_multiplicity_changes_semantic_sha(self):
        event = semantic_event()
        self.assertNotEqual(
            semantic_sha_for_result(semantic_result((event,))),
            semantic_sha_for_result(semantic_result((event, event))),
        )

    def test_34_decimal_equivalent_forms_canonicalize_equally(self):
        hashes = {
            semantic_sha_for_result(
                semantic_result(
                    (
                        semantic_event(
                            payload=CashMovementEvidence(
                                amount=value,
                                currency="IDR",
                                direction=EventDirection.OUTFLOW,
                                status=SourceEventStatus.POSTED,
                            )
                        ),
                    )
                )
            )
            for value in (
                Decimal("1"),
                Decimal("1.0"),
                Decimal("1.00"),
            )
        }
        self.assertEqual(len(hashes), 1)

    def test_35_negative_zero_canonicalizes_to_zero(self):
        def build(value):
            return semantic_result(
                (
                    semantic_event(
                        payload=CashMovementEvidence(
                            amount=value,
                            currency="IDR",
                            direction=EventDirection.OUTFLOW,
                            status=SourceEventStatus.POSTED,
                        )
                    ),
                )
            )
        self.assertEqual(
            semantic_sha_for_result(build(Decimal("0"))),
            semantic_sha_for_result(build(Decimal("-0.00"))),
        )

    def test_36_unicode_and_whitespace_normalization_is_deterministic(self):
        first = semantic_event(
            role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="acct-1",
                display_name_raw="  ＡＢＣ   Wallet  ",
            ),
        )
        second = semantic_event(
            role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="acct-1",
                display_name_raw="ABC Wallet",
            ),
        )
        self.assertEqual(
            semantic_sha_for_result(semantic_result((first,))),
            semantic_sha_for_result(semantic_result((second,))),
        )

    def test_37_provenance_differences_do_not_change_semantic_sha(self):
        first = semantic_event(
            raw_locator=r"C:\Private\one.pdf",
            raw_text="raw one",
        )
        second = semantic_event(
            raw_locator=r"D:\Private\two.pdf",
            raw_text="raw two",
        )
        self.assertEqual(
            semantic_sha_for_result(semantic_result((first,))),
            semantic_sha_for_result(semantic_result((second,))),
        )

    def test_38_diagnostic_and_confidence_differences_do_not_change_semantic_sha(self):
        first = semantic_event()
        second = semantic_event(
            evidence_quality=ConfidenceLevel.LOW,
            parse_confidence=ConfidenceLevel.UNKNOWN,
            diagnostics=(
                SafeDiagnostic(
                    code="ROW_REVIEW",
                    severity=DiagnosticSeverity.WARNING,
                    message="Safe review marker.",
                    review_required=True,
                    locator_token="abcdef123456",
                ),
            ),
        )
        self.assertEqual(
            semantic_sha_for_result(semantic_result((first,))),
            semantic_sha_for_result(semantic_result((second,))),
        )

    def test_39_template_and_parser_versions_do_not_change_semantic_sha(self):
        first = semantic_result(
            (
                semantic_event(
                    template_id="template-a",
                    parser_version="parser-a",
                ),
            ),
            template_id="template-a",
            parser_version="parser-a",
        )
        second = semantic_result(
            (
                semantic_event(
                    template_id="template-b",
                    parser_version="parser-b",
                ),
            ),
            template_id="template-b",
            parser_version="parser-b",
        )
        self.assertEqual(
            semantic_sha_for_result(first),
            semantic_sha_for_result(second),
        )

    def test_40_source_registry_id_changes_semantic_sha(self):
        result = semantic_result((semantic_event(),))
        self.assertNotEqual(
            semantic_document_sha256(
                "bca_statement",
                "bca:main:2026-08",
                result,
            ),
            semantic_document_sha256(
                "jago_statement",
                "bca:main:2026-08",
                result,
            ),
        )

    def test_41_natural_document_key_changes_semantic_sha(self):
        result = semantic_result((semantic_event(),))
        self.assertNotEqual(
            semantic_document_sha256(
                "bca_statement",
                "account-a:2026-08",
                result,
            ),
            semantic_document_sha256(
                "bca_statement",
                "account-b:2026-08",
                result,
            ),
        )

    def test_42_all_six_evidence_roles_have_deterministic_canonical_forms(self):
        payloads = (
            (
                EventRole.CASH_MOVEMENT,
                CashMovementEvidence(
                    amount=Decimal("100"),
                    currency="IDR",
                    direction=EventDirection.OUTFLOW,
                    status=SourceEventStatus.POSTED,
                ),
            ),
            (
                EventRole.BALANCE_SNAPSHOT,
                BalanceSnapshotEvidence(
                    balance=Decimal("1000"),
                    currency="IDR",
                    snapshot_kind=SnapshotKind.CLOSING,
                ),
            ),
            (
                EventRole.SOURCE_SUMMARY,
                SourceSummaryEvidence(currency="IDR"),
            ),
            (
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="acct-1",
                    display_name_raw="Main Account",
                ),
            ),
            (
                EventRole.INVESTMENT_TRADE,
                InvestmentTradeEvidence(
                    instrument_raw="BBCA",
                    trade_date="2026-08-01",
                    settlement_date="2026-08-05",
                    side_raw="BUY",
                    quantity=Decimal("1"),
                    unit_price=Decimal("9000"),
                    currency="IDR",
                ),
            ),
            (
                EventRole.COMMERCE_ORDER,
                CommerceOrderEvidence(
                    order_native_id_raw="order-1",
                    order_date="2026-08-01",
                    order_total=Decimal("50000"),
                    currency="IDR",
                ),
            ),
        )
        result = semantic_result(
            tuple(
                semantic_event(
                    role=role,
                    payload=payload,
                    row_seed=role.value,
                )
                for role, payload in payloads
            )
        )
        canonical = semantic_document_canonical_json(
            "bca_statement",
            "six-role-key",
            result,
        )
        self.assertEqual(
            canonical,
            semantic_document_canonical_json(
                "bca_statement",
                "six-role-key",
                result,
            ),
        )
        for role, _ in payloads:
            self.assertIn(role.value, canonical)

    def test_43_semantic_duplicate_maps_to_skipped_duplicate(self):
        key = "bca:main:2026-08"
        probe = AdapterResult(
            descriptor=SyntheticAdapter().descriptor,
            source_document_id="probe",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.UNKNOWN,
            events=(),
            natural_document_key_candidate=key,
        )
        existing = SourceDocumentReadRecord(
            source_document_id="doc-existing",
            source_registry_id="bca_statement",
            content_sha256="1" * 64,
            natural_document_key=key,
            semantic_sha256=semantic_document_sha256(
                "bca_statement",
                key,
                probe,
            ),
        )
        result, _ = run_known(
            b"semantic-duplicate-new-bytes",
            registry=FakeRegistry(natural=(existing,)),
        )
        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.SEMANTIC_DUPLICATE,
        )
        self.assertEqual(
            result.disposition,
            DryRunDisposition.SKIPPED_DUPLICATE,
        )

    def test_44_revision_conflict_maps_to_review_required(self):
        existing = SourceDocumentReadRecord(
            source_document_id="doc-existing",
            source_registry_id="bca_statement",
            content_sha256="2" * 64,
            natural_document_key="bca:main:2026-08",
            semantic_sha256="f" * 64,
        )
        result, _ = run_known(
            b"revision-conflict",
            registry=FakeRegistry(natural=(existing,)),
        )
        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.REVISION_OR_CONFLICT,
        )
        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )

    def test_45_missing_natural_key_maps_to_ambiguous_review(self):
        result, _ = run_known(
            b"missing-natural-key",
            adapter=SyntheticAdapter(natural_key=None),
        )
        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.AMBIGUOUS,
        )
        self.assertEqual(
            result.disposition,
            DryRunDisposition.REVIEW_REQUIRED,
        )

    def test_46_exact_duplicate_precedence_wins_before_semantic_classification(self):
        data = b"exact-precedence"
        existing = SourceDocumentReadRecord(
            source_document_id="exact-existing",
            source_registry_id="other-source",
            content_sha256=digest(data),
            natural_document_key="other-key",
            semantic_sha256="a" * 64,
        )
        registry = FakeRegistry(exact=(existing,))
        adapter = SyntheticAdapter()
        result = dry_run_artifact(
            "ignored-root",
            artifact(data),
            registry=registry,
            adapter_catalog=AdapterCatalog((adapter,)),
            payload_reader=lambda *args, **kwargs: self.fail(
                "payload reader should not run"
            ),
        )
        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.EXACT_DUPLICATE,
        )
        self.assertEqual(adapter.parse_calls, 0)
        self.assertEqual(registry.natural_calls, 0)

    def test_47_dry_run_document_ordering_is_deterministic(self):
        first = artifact(b"order-a", extra_extensions=(".jpg",))
        second = artifact(b"order-b", extra_extensions=(".jpg",))
        discovery = DiscoveryResult(
            artifacts=(second, first),
            diagnostics=(),
            archives_seen=0,
            physical_files_seen=4,
            supported_occurrences=4,
        )
        batch = dry_run_batch(
            "ignored-root",
            discovery,
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog(()),
        )
        self.assertEqual(
            [item.content_sha256 for item in batch.documents],
            sorted((first.content_sha256, second.content_sha256)),
        )

    def test_48_repeated_dry_run_has_identical_safe_output(self):
        first = artifact(b"repeat-a", extra_extensions=(".jpg",))
        second = artifact(b"repeat-b", extra_extensions=(".jpg",))
        discovery = DiscoveryResult(
            artifacts=(second, first),
            diagnostics=(),
            archives_seen=0,
            physical_files_seen=4,
            supported_occurrences=4,
        )
        first_batch = dry_run_batch(
            "ignored-root",
            discovery,
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog(()),
        )
        second_batch = dry_run_batch(
            "ignored-root",
            discovery,
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog(()),
        )
        first_safe = safe_dry_run_batch_json(first_batch)
        second_safe = safe_dry_run_batch_json(second_batch)
        self.assertEqual(first_safe, second_safe)
        self.assertNotIn("private/", first_safe)
        self.assertEqual(
            first_batch.overall_status,
            DryRunBatchStatus.REVIEW_REQUIRED,
        )



    def test_semantic_duplicate_precedence_over_adapter_review_flag(self):
        data = b"semantic-duplicate-review-adapter"
        key = "bca:main:2026-08"

        review_diagnostic = SafeDiagnostic(
            code="ROW_REVIEW",
            severity=DiagnosticSeverity.WARNING,
            message="Synthetic review marker.",
            review_required=True,
            locator_token="abcdef123456",
        )

        adapter = SyntheticAdapter(
            parse_status=AdapterParseStatus.REVIEW_REQUIRED,
            natural_key=key,
            diagnostics=(review_diagnostic,),
        )

        probe_result = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="probe",
            parse_status=AdapterParseStatus.REVIEW_REQUIRED,
            period_status=PeriodStatus.UNKNOWN,
            events=(),
            diagnostics=(review_diagnostic,),
            natural_document_key_candidate=key,
        )

        existing = SourceDocumentReadRecord(
            source_document_id="doc-existing",
            source_registry_id="bca_statement",
            content_sha256="4" * 64,
            natural_document_key=key,
            semantic_sha256=semantic_document_sha256(
                "bca_statement",
                key,
                probe_result,
            ),
        )

        result, _ = run_known(
            data,
            registry=FakeRegistry(natural=(existing,)),
            adapter=adapter,
        )

        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.SEMANTIC_DUPLICATE,
        )
        self.assertEqual(
            result.disposition,
            DryRunDisposition.SKIPPED_DUPLICATE,
        )


    def test_internal_adapter_exception_is_batch_fatal(self):
        data = b"adapter-internal-exception"

        class ExplodingAdapter:
            descriptor = AdapterDescriptor(
                adapter_id="synthetic-exploding",
                source_registry_id="bca_statement",
                template_id="bca_monthly_statement_v1",
                parser_version="parser-v1",
                source_channel=SourceChannel.PDF,
            )

            def parse(self, source):
                raise RuntimeError("synthetic internal adapter failure")

        discovery = DiscoveryResult(
            artifacts=(artifact(data),),
            diagnostics=(),
            archives_seen=0,
            physical_files_seen=1,
            supported_occurrences=1,
        )

        batch = dry_run_batch(
            "ignored-root",
            discovery,
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog((ExplodingAdapter(),)),
            payload_reader=lambda *args, **kwargs: data,
            preflight_func=lambda payload, **kwargs: known_preflight(payload),
        )

        self.assertEqual(
            batch.documents[0].disposition,
            DryRunDisposition.FAILED,
        )
        self.assertEqual(
            batch.documents[0].diagnostics[-1].code,
            "ADAPTER_EXECUTION_FAILED",
        )
        self.assertEqual(
            batch.overall_status,
            DryRunBatchStatus.FAILED,
        )

    def test_49_source_summary_semantic_canonical_form_locked(self):
        desc = AdapterDescriptor(
            adapter_id="synthetic-summary-adapter",
            source_registry_id="synthetic_statement",
            template_id="synthetic_template_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        prov = SourceProvenanceContract(
            source_document_id="doc-synthetic-001",
            raw_locator="synthetic:summary:1",
            page_number=1,
            row_index=1,
            raw_text="SYNTHETIC SUMMARY TEXT",
        )
        summary_payload = SourceSummaryEvidence(
            currency="IDR",
            period_start="2026-07-01",
            period_end="2026-07-31",
            opening_balance=Decimal("1000000.00"),
            incoming_total=Decimal("250000.00"),
            outgoing_total=Decimal("100000.00"),
            closing_balance=Decimal("1150000.00"),
        )
        envelope = NormalizedEventEnvelope(
            source_document_id="doc-synthetic-001",
            source_registry_id="synthetic_statement",
            template_id="synthetic_template_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
            event_role=EventRole.SOURCE_SUMMARY,
            source_event_id=None,
            row_fingerprint="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            evidence_quality=ConfidenceLevel.HIGH,
            parse_confidence=ConfidenceLevel.HIGH,
            provenance=prov,
            payload=summary_payload,
        )
        result = AdapterResult(
            descriptor=desc,
            source_document_id="doc-synthetic-001",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            events=(envelope,),
            period_start="2026-07-01",
            period_end="2026-07-31",
            natural_document_key_candidate="synthetic_statement:account-001:2026-07",
        )
        canonical_json = semantic_document_canonical_json(
            "synthetic_statement",
            result.natural_document_key_candidate,
            result,
        )
        canonical_sha = sha256(canonical_json.encode("utf-8")).hexdigest()
        semantic_sha = semantic_document_sha256(
            "synthetic_statement",
            result.natural_document_key_candidate,
            result,
        )
        self.assertEqual(
            canonical_sha,
            "47757b3a51ecaab5b947abaf821a40d0517d5b28ea87b24b57778a5bb1468e24",
        )
        self.assertEqual(
            semantic_sha,
            "47757b3a51ecaab5b947abaf821a40d0517d5b28ea87b24b57778a5bb1468e24",
        )

    def test_50_account_period_summary_scoped_semantic_identity(self):
        desc = AdapterDescriptor(
            adapter_id="synthetic-scoped-adapter",
            source_registry_id="jago_statement",
            template_id="jago_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )

        def make_result(pocket_key: str, locator: str) -> AdapterResult:
            prov = SourceProvenanceContract(
                source_document_id="doc-jago-01",
                raw_locator=locator,
                raw_text="POCKET SUMMARY EVIDENCE",
            )
            payload = AccountPeriodSummaryEvidence(
                observed_provider_account_key=pocket_key,
                currency="IDR",
                period_start="2026-07-01",
                period_end="2026-07-31",
                opening_balance=Decimal("100000.00"),
                incoming_total=Decimal("50000.00"),
                outgoing_total=Decimal("20000.00"),
                closing_balance=Decimal("130000.00"),
            )
            envelope = NormalizedEventEnvelope(
                source_document_id="doc-jago-01",
                source_registry_id="jago_statement",
                template_id="jago_monthly_statement_v1",
                parser_version="parser-v1",
                source_channel=SourceChannel.PDF,
                event_role=EventRole.ACCOUNT_PERIOD_SUMMARY,
                source_event_id=None,
                row_fingerprint="f" * 64,
                evidence_quality=ConfidenceLevel.HIGH,
                parse_confidence=ConfidenceLevel.HIGH,
                provenance=prov,
                payload=payload,
            )
            return AdapterResult(
                descriptor=desc,
                source_document_id="doc-jago-01",
                parse_status=AdapterParseStatus.COMPLETED,
                period_status=PeriodStatus.CLOSED,
                events=(envelope,),
                period_start="2026-07-01",
                period_end="2026-07-31",
                natural_document_key_candidate="jago_statement:cust-001:2026-07",
            )

        res_a1 = make_result("POCKET-A", "loc:page1:1")
        res_a2 = make_result("POCKET-A", "loc:different_page:99")
        res_b = make_result("POCKET-B", "loc:page1:1")

        sha_a1 = semantic_document_sha256(
            "jago_statement",
            res_a1.natural_document_key_candidate,
            res_a1,
        )
        sha_a2 = semantic_document_sha256(
            "jago_statement",
            res_a2.natural_document_key_candidate,
            res_a2,
        )
        sha_b = semantic_document_sha256(
            "jago_statement",
            res_b.natural_document_key_candidate,
            res_b,
        )

        # Same pocket key with different locator -> same semantic sha
        self.assertEqual(sha_a1, sha_a2)

        # Different pocket key -> different semantic sha
        self.assertNotEqual(sha_a1, sha_b)

        # Distinct from unscoped SOURCE_SUMMARY
        unscoped_payload = SourceSummaryEvidence(
            currency="IDR",
            period_start="2026-07-01",
            period_end="2026-07-31",
            opening_balance=Decimal("100000.00"),
            incoming_total=Decimal("50000.00"),
            outgoing_total=Decimal("20000.00"),
            closing_balance=Decimal("130000.00"),
        )
        unscoped_envelope = NormalizedEventEnvelope(
            source_document_id="doc-jago-01",
            source_registry_id="jago_statement",
            template_id="jago_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
            event_role=EventRole.SOURCE_SUMMARY,
            source_event_id=None,
            row_fingerprint="f" * 64,
            evidence_quality=ConfidenceLevel.HIGH,
            parse_confidence=ConfidenceLevel.HIGH,
            provenance=SourceProvenanceContract(
                source_document_id="doc-jago-01",
                raw_locator="loc:page1:1",
                raw_text="POCKET SUMMARY EVIDENCE",
            ),
            payload=unscoped_payload,
        )
        unscoped_res = AdapterResult(
            descriptor=desc,
            source_document_id="doc-jago-01",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            events=(unscoped_envelope,),
            period_start="2026-07-01",
            period_end="2026-07-31",
            natural_document_key_candidate="jago_statement:cust-001:2026-07",
        )
        sha_unscoped = semantic_document_sha256(
            "jago_statement",
            unscoped_res.natural_document_key_candidate,
            unscoped_res,
        )
        self.assertNotEqual(sha_a1, sha_unscoped)


if __name__ == "__main__":
    unittest.main()
