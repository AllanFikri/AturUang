import inspect
import sqlite3
import unittest
from hashlib import sha256

from aturuang.ingestion_adapter import (
    AdapterDescriptor,
    AdapterParseStatus,
    AdapterResult,
    DiagnosticSeverity,
    SafeDiagnostic,
)
from aturuang.ingestion_contracts import (
    DocumentIdentityStatus,
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    AdapterCatalogError,
    DryRunDisposition,
    SqliteRegistryAuthority,
    dry_run_artifact,
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
    ):
        self.exact = tuple(exact)
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


if __name__ == "__main__":
    unittest.main()
