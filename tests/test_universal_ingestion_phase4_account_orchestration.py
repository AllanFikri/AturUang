"""
Tests for Universal Ingestion Phase 4 Account Discovery Orchestration.
Verifies end-to-end dry-run wiring of provider observation extraction,
fail-closed boundary enforcement, safe serialization, and isolation invariants.

Contains exactly 24 focused test methods.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
import hashlib
import json
import os
import sqlite3
import tempfile
from typing import Any, Callable, Mapping, Sequence
import unittest

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    DocumentIdentityStatus,
    LifecycleState,
    OwnershipState,
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_adapter import (
    AdapterDescriptor,
    AdapterParseStatus,
    AdapterResult,
    DiagnosticSeverity,
    EventRole,
    NormalizedEventEnvelope,
    ObservedAccountEvidence,
    SafeDiagnostic,
    SourceProvenanceContract,
)
from aturuang.ingestion_account_discovery import (
    AccountDiscoveryObservation,
    AccountDiscoveryPlan,
    AccountDiscoveryResolver,
    AccountResolution,
    ExistingAccountState,
)
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
)
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
    DiscoveryResult,
)
from aturuang.ingestion_preflight import (
    DetectionMethod,
    DocumentPreflight,
    PdfEncryptionState,
    PreflightQuality,
    TemplateDetection,
)
from aturuang.ingestion_registry_service import (
    SourceDocumentReadRecord,
    TemplateResolution,
)
from aturuang.ingestion_registry import (
    init_registry_schema,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    DIAGNOSTIC_ACCOUNT_DISCOVERY_RESOLUTION_FAILED,
    DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED,
    DIAGNOSTIC_ACCOUNT_OBSERVATION_EMPTY,
    DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTION_FAILED,
    DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTOR_MISSING,
    DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID,
    DryRunBatchResult,
    DryRunBatchStatus,
    DryRunDiagnostic,
    DryRunDisposition,
    DryRunDocumentResult,
    DryRunStage,
    SqliteRegistryAuthority,
    dry_run_artifact,
    dry_run_batch,
    safe_dry_run_batch_json,
    safe_dry_run_document_view,
    semantic_document_sha256,
)

import aturuang.ingestion_jago_adapter as jago_mod
import aturuang.ingestion_seabank_adapter as seabank_mod
import aturuang.ingestion_blu_mutation_adapter as blu_mod
import aturuang.ingestion_gopay_adapter as gopay_mod
import aturuang.ingestion_bca_adapter as bca_mod
import aturuang.ingestion_stockbit_adapter as stockbit_mod
import aturuang.ingestion_shopeepay_adapter as shopeepay_mod
import aturuang.ingestion_shopee_orders_adapter as shopee_orders_mod


TEST_SECRET = "synthetic-test-secret-at-least-32-chars-long-strictly-mocked!"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_artifact(
    data: bytes,
    *,
    extension: str = ".pdf",
    locator: str = "private/statement.pdf",
) -> DiscoveredArtifact:
    return DiscoveredArtifact(
        content_sha256=_sha256_hex(data),
        size_bytes=len(data),
        extension=extension,
        occurrences=(
            ArtifactOccurrence(
                source_locator=locator,
                extension=extension,
            ),
        ),
    )


def _make_preflight(
    data: bytes,
    *,
    source_registry_id: str,
    template_id: str,
    template_fingerprint: str = "fp-synthetic-1",
    status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
) -> DocumentPreflight:
    detection = TemplateDetection(
        status=status,
        source_registry_id=source_registry_id if status is TemplateMatchStatus.KNOWN else None,
        template_id=template_id if status is TemplateMatchStatus.KNOWN else None,
        template_fingerprint=template_fingerprint if status is TemplateMatchStatus.KNOWN else None,
        method=DetectionMethod.TEXT_MARKERS,
        required_marker_matches=4,
        required_marker_total=4,
        reason_code="EXACT_REQUIRED_MARKERS" if status is TemplateMatchStatus.KNOWN else status.value,
    )
    return DocumentPreflight(
        content_sha256=_sha256_hex(data),
        size_bytes=len(data),
        extension=".pdf",
        media_type="PDF",
        mime_type="application/pdf",
        quality_status=PreflightQuality.READY,
        page_count=1,
        image_dimensions=None,
        text_layer_available=True,
        pdf_encryption_state=PdfEncryptionState.UNENCRYPTED,
        period_status=PeriodStatus.CLOSED,
        template_detection=detection,
    )


def _make_envelope(
    source_document_id: str,
    descriptor: AdapterDescriptor,
    event_role: EventRole,
    payload: Any,
    source_event_id: str = "evt-1",
) -> NormalizedEventEnvelope:
    return NormalizedEventEnvelope(
        source_document_id=source_document_id,
        source_registry_id=descriptor.source_registry_id,
        template_id=descriptor.template_id,
        parser_version=descriptor.parser_version,
        source_channel=descriptor.source_channel,
        event_role=event_role,
        source_event_id=source_event_id,
        row_fingerprint="0" * 64,
        evidence_quality=ConfidenceLevel.HIGH,
        parse_confidence=ConfidenceLevel.HIGH,
        provenance=SourceProvenanceContract(
            source_document_id=source_document_id,
            raw_locator="locator-1",
        ),
        payload=payload,
    )


class MockRegistry:
    def __init__(
        self,
        *,
        exact: Sequence[SourceDocumentReadRecord] = (),
        natural: Sequence[SourceDocumentReadRecord] = (),
        source_registry_id: str = "bca_statement",
        channel: SourceChannel = SourceChannel.PDF,
        template_id: str = "bca_monthly_statement_v1",
        parser_version: str = "parser-v1",
        template_status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
    ) -> None:
        self.exact = tuple(exact)
        self.natural = tuple(natural)
        self.exact_calls = 0
        self.natural_calls = 0
        self.source_registry_id = source_registry_id
        self.channel = channel
        self.template_id = template_id
        self.parser_version = parser_version
        self.template_status = template_status

    def lookup_exact_documents(self, content_sha256: str) -> Sequence[SourceDocumentReadRecord]:
        self.exact_calls += 1
        return self.exact

    def lookup_natural_documents(
        self, source_registry_id: str, natural_document_key: str
    ) -> Sequence[SourceDocumentReadRecord]:
        self.natural_calls += 1
        return self.natural

    def source_capability(self, source_registry_id: str) -> dict[str, Any]:
        return {
            "source_registry_id": source_registry_id,
            "channel": self.channel.value if isinstance(self.channel, SourceChannel) else str(self.channel),
        }

    def template_authority(
        self, *, source_registry_id: str, template_fingerprint: str
    ) -> TemplateResolution:
        return TemplateResolution(
            status=self.template_status,
            template_id=self.template_id if self.template_status is TemplateMatchStatus.KNOWN else None,
            parser_version=self.parser_version if self.template_status is TemplateMatchStatus.KNOWN else None,
        )


class StubAdapter:
    def __init__(
        self,
        descriptor: AdapterDescriptor,
        *,
        events: tuple[NormalizedEventEnvelope, ...] = (),
        parse_status: AdapterParseStatus = AdapterParseStatus.COMPLETED,
        natural_key: str = "synthetic-natural-key-1",
        diagnostics: tuple[SafeDiagnostic, ...] = (),
        extractor_func: Callable[[AdapterResult], Sequence[AccountDiscoveryObservation]] | None = None,
        custom_result: AdapterResult | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.events = events
        self.parse_status = parse_status
        self.natural_key = natural_key
        self.diagnostics = diagnostics
        self.parse_calls = 0
        self.last_parsed_source = None
        self.custom_result = custom_result
        if extractor_func is not None:
            self.extract_account_observations = extractor_func

    def parse(self, source: Any) -> AdapterResult:
        self.parse_calls += 1
        self.last_parsed_source = source
        source_doc_id = getattr(source, "source_document_id", "synthetic-doc-1")
        period_status = getattr(source, "period_status", PeriodStatus.CLOSED)
        if self.custom_result is not None:
            return dataclasses.replace(
                self.custom_result,
                source_document_id=source_doc_id,
                period_status=period_status,
            )
        events = tuple(
            dataclasses.replace(
                evt,
                source_document_id=source_doc_id,
                provenance=SourceProvenanceContract(
                    source_document_id=source_doc_id,
                    raw_locator=evt.provenance.raw_locator,
                ),
            )
            for evt in self.events
        )
        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source_doc_id,
            parse_status=self.parse_status,
            period_status=period_status,
            period_start="2026-03-01",
            period_end="2026-03-31",
            events=events,
            diagnostics=self.diagnostics,
            natural_document_key_candidate=self.natural_key,
        )


class TestUniversalIngestionPhase4AccountOrchestration(unittest.TestCase):
    """
    Focused test suite for Universal Ingestion Phase 4 Account Discovery Orchestration.
    Strictly verifies exactly 24 test methods.
    """

    def setUp(self) -> None:
        self.protector = AccountIdentityProtector(secret=TEST_SECRET)
        self.resolver = AccountDiscoveryResolver(protector=self.protector)

    def _run_dry_run(
        self,
        adapter: Any,
        data: bytes = b"synthetic-document-content-v1",
        *,
        registry: Any | None = None,
        account_resolver: AccountDiscoveryResolver | None = None,
        existing_accounts: Sequence[ExistingAccountState] | None = None,
    ) -> DryRunDocumentResult:
        art = _make_artifact(data)
        preflight = _make_preflight(
            data,
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
        )
        if registry is None:
            registry = MockRegistry(
                source_registry_id=adapter.descriptor.source_registry_id,
                channel=adapter.descriptor.source_channel,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
            )
        catalog = AdapterCatalog((adapter,))
        resolver_to_use = self.resolver if account_resolver is None else account_resolver
        return dry_run_artifact(
            "root",
            art,
            registry=registry,
            adapter_catalog=catalog,
            payload_reader=lambda r, a, **k: data,
            preflight_func=lambda p, **k: preflight,
            account_resolver=resolver_to_use,
            existing_accounts=existing_accounts,
        )

    # -------------------------------------------------------------------------
    # Test 01: Jago root and pocket hierarchy reaches resolver
    # -------------------------------------------------------------------------
    def test_01_jago_root_and_pocket_hierarchy_reaches_resolver(self) -> None:
        real_adapter = jago_mod.JagoMonthlyStatementAdapter()
        events = (
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-JAGO-ROOT-001",
                    parent_observed_key="SYNTHETIC-JAGO-ROOT-001",
                    display_name_raw="Kantong Utama",
                ),
            ),
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-JAGO-PKT-001",
                    parent_observed_key="SYNTHETIC-JAGO-ROOT-001",
                    display_name_raw="Kantong Belanja",
                ),
            ),
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            events=events,
            extractor_func=real_adapter.extract_account_observations,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 2)
        root = [r for r in plan.resolutions if r.parent_protected_account_key is None][0]
        pocket = [r for r in plan.resolutions if r.parent_protected_account_key is not None][0]
        self.assertEqual(pocket.parent_protected_account_key, root.protected_account_key)
        self.assertEqual(root.account_type, AccountType.TRANSACTIONAL)
        self.assertEqual(pocket.account_type, AccountType.SAVINGS)
        self.assertTrue(root.persistence_eligible)
        self.assertTrue(pocket.persistence_eligible)

    # -------------------------------------------------------------------------
    # Test 02: SeaBank observation reaches resolver
    # -------------------------------------------------------------------------
    def test_02_seabank_observation_reaches_resolver(self) -> None:
        real_adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        events = (
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-SEABANK-ACC-001",
                    display_name_raw="SeaBank Tabungan",
                ),
            ),
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            events=events,
            extractor_func=real_adapter.extract_account_observations,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.institution_id, "seabank")
        self.assertEqual(res.account_type, AccountType.SAVINGS)
        self.assertEqual(res.lifecycle_state, LifecycleState.NEW)
        self.assertTrue(res.persistence_eligible)

    # -------------------------------------------------------------------------
    # Test 03: blu observation reaches resolver
    # -------------------------------------------------------------------------
    def test_03_blu_observation_reaches_resolver(self) -> None:
        real_adapter = blu_mod.BluAccountMutationAdapter()
        events = (
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-BLU-ACC-001",
                    display_name_raw="bluAccount",
                ),
            ),
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            events=events,
            extractor_func=real_adapter.extract_account_observations,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.institution_id, "blu")
        self.assertEqual(res.account_type, AccountType.SAVINGS)
        self.assertTrue(res.persistence_eligible)

    # -------------------------------------------------------------------------
    # Test 04: GoPay observation reaches resolver
    # -------------------------------------------------------------------------
    def test_04_gopay_observation_reaches_resolver(self) -> None:
        real_adapter = gopay_mod.GoPayEStatementAdapter()
        events = (
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-GOPAY-ACC-001",
                    display_name_raw="GoPay Saldo",
                ),
            ),
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            events=events,
            extractor_func=real_adapter.extract_account_observations,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.institution_id, "gopay")
        self.assertEqual(res.account_type, AccountType.WALLET)
        self.assertTrue(res.persistence_eligible)

    # -------------------------------------------------------------------------
    # Test 05: BCA root and poket observations reach resolver in hierarchy
    # -------------------------------------------------------------------------
    def test_05_bca_root_and_poket_observations_reach_resolver_in_hierarchy(self) -> None:
        real_adapter = bca_mod.BCAMonthlyStatementAdapter()
        events = (
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-BCA-ROOT-001",
                    parent_observed_key="SYNTHETIC-BCA-ROOT-001",
                    display_name_raw="Tahapan BCA",
                ),
            ),
            _make_envelope(
                "temp-placeholder",
                real_adapter.descriptor,
                EventRole.ACCOUNT_OBSERVATION,
                ObservedAccountEvidence(
                    observed_provider_account_key="SYNTHETIC-BCA-PKT-001",
                    parent_observed_key="SYNTHETIC-BCA-ROOT-001",
                    display_name_raw="Tahapan Poket",
                ),
            ),
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            events=events,
            extractor_func=real_adapter.extract_account_observations,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 2)
        root = [r for r in plan.resolutions if r.parent_protected_account_key is None][0]
        poket = [r for r in plan.resolutions if r.parent_protected_account_key is not None][0]
        self.assertEqual(poket.parent_protected_account_key, root.protected_account_key)
        self.assertEqual(root.account_type, AccountType.TRANSACTIONAL)
        self.assertEqual(poket.account_type, AccountType.SAVINGS)

    # -------------------------------------------------------------------------
    # Test 06: Stockbit investment observation not merged with bank account
    # -------------------------------------------------------------------------
    def test_06_stockbit_investment_observation_not_merged_with_bank_account(self) -> None:
        real_adapter = stockbit_mod.StockbitStatementAdapter()
        identity = stockbit_mod.StockbitDocumentIdentity(
            client_code_raw="SYNTHETIC-STOCKBIT-CLIENT-001",
            period_start="2026-03-01",
            period_end="2026-03-31",
        )
        custom_result = stockbit_mod.StockbitAdapterResult(
            descriptor=real_adapter.descriptor,
            source_document_id="doc-placeholder",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="stockbit-cand-1",
            events=(),
            document_identity=identity,
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            custom_result=custom_result,
            extractor_func=real_adapter.extract_account_observations,
        )
        existing_bank = ExistingAccountState(
            protected_account_key=self.protector.protect_account_key(
                "stockbit",
                real_adapter.descriptor.source_registry_id,
                AccountType.TRANSACTIONAL,
                "SYNTHETIC-STOCKBIT-CLIENT-001",
            ),
            institution_id="stockbit",
            source_registry_id=real_adapter.descriptor.source_registry_id,
            display_name_safe="Stockbit Bank Account",
            account_type=AccountType.TRANSACTIONAL,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.UNCHANGED,
            effective_date="2026-01-01",
        )
        result = self._run_dry_run(adapter, existing_accounts=[existing_bank])
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.account_type, AccountType.INVESTMENT)
        self.assertNotEqual(res.protected_account_key, existing_bank.protected_account_key)
        self.assertEqual(res.lifecycle_state, LifecycleState.NEW)

    # -------------------------------------------------------------------------
    # Test 07: ShopeePay unstable identity fails closed for review
    # -------------------------------------------------------------------------
    def test_07_shopeepay_unstable_identity_fails_closed_for_review(self) -> None:
        real_adapter = shopeepay_mod.ShopeePayTransactionHistoryImageAdapter()
        adapter = StubAdapter(
            real_adapter.descriptor,
            extractor_func=real_adapter.extract_account_observations,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.REVIEW_REQUIRED)
        self.assertIsNotNone(result.account_discovery_plan)
        plan = result.account_discovery_plan
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertFalse(res.persistence_eligible)
        self.assertIsNone(res.protected_account_key)
        self.assertTrue(
            any(d.code == DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED for d in result.diagnostics)
        )

    # -------------------------------------------------------------------------
    # Test 08: Shopee Orders is explicitly NOT_APPLICABLE and creates no account
    # -------------------------------------------------------------------------
    def test_08_shopee_orders_is_explicitly_not_applicable_and_creates_no_account(self) -> None:
        real_adapter = shopee_orders_mod.ShopeeOrdersReceiptAdapter()
        identity = shopee_orders_mod.ShopeeOrderDocumentIdentity(
            order_number="SYNTHETIC-SHOPEE-ORD-001",
            seller_name="SYNTHETIC-SELLER-001",
            order_date="2026-03-01",
        )
        custom_result = shopee_orders_mod.ShopeeOrderAdapterResult(
            descriptor=real_adapter.descriptor,
            source_document_id="doc-placeholder",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-01",
            natural_document_key_candidate="shopee-ord-cand-1",
            events=(),
            diagnostics=(),
            order_reconciliation_status="MATCHED",
            reconciliation_difference=Decimal("0.00"),
            order_identity=identity,
        )
        adapter = StubAdapter(
            real_adapter.descriptor,
            custom_result=custom_result,
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        self.assertEqual(result.account_discovery_plan.resolutions, ())
        self.assertEqual(result.account_discovery_plan.diagnostics, ())
        self.assertFalse(
            any(
                d.code
                in (
                    DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTOR_MISSING,
                    DIAGNOSTIC_ACCOUNT_OBSERVATION_EMPTY,
                    DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED,
                )
                for d in result.diagnostics
            )
        )

    # -------------------------------------------------------------------------
    # Test 09: Extractor receives AdapterResult and adapter.parse is called once
    # -------------------------------------------------------------------------
    def test_09_extractor_receives_adapter_result_and_adapter_parse_is_called_once(self) -> None:
        received_args = []

        def spy_extractor(res: AdapterResult) -> Sequence[AccountDiscoveryObservation]:
            received_args.append(res)
            return (
                AccountDiscoveryObservation(
                    raw_account_key="SYNTHETIC-ACC-SPY",
                    parent_raw_account_key=None,
                    institution_id="bca",
                    source_registry_id="bca_statement",
                    account_type=AccountType.TRANSACTIONAL,
                    display_name_safe="BCA Spy",
                    ownership_state=OwnershipState.OWNED,
                    ownership_confidence=ConfidenceLevel.HIGH,
                    effective_date="2026-03-01",
                ),
            )

        descriptor = AdapterDescriptor(
            adapter_id="spy-adapter",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        adapter = StubAdapter(descriptor, extractor_func=spy_extractor)
        result = self._run_dry_run(adapter)
        self.assertEqual(adapter.parse_calls, 1)
        self.assertEqual(len(received_args), 1)
        self.assertIs(received_args[0], result.adapter_result)

    # -------------------------------------------------------------------------
    # Test 10: Missing extractor on account-bearing adapter requires review
    # -------------------------------------------------------------------------
    def test_10_missing_extractor_on_account_bearing_adapter_requires_review(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-without-extractor",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        adapter = StubAdapter(descriptor)
        self.assertFalse(hasattr(adapter, "extract_account_observations"))
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.REVIEW_REQUIRED)
        diag = [d for d in result.diagnostics if d.code == DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTOR_MISSING]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0].severity, DiagnosticSeverity.WARNING)

    # -------------------------------------------------------------------------
    # Test 11: Empty observations on account-bearing adapter require review
    # -------------------------------------------------------------------------
    def test_11_empty_observations_on_account_bearing_adapter_require_review(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-empty-obs",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: ())
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.REVIEW_REQUIRED)
        diag = [d for d in result.diagnostics if d.code == DIAGNOSTIC_ACCOUNT_OBSERVATION_EMPTY]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0].severity, DiagnosticSeverity.WARNING)

    # -------------------------------------------------------------------------
    # Test 12: Invalid extractor return type fails document
    # -------------------------------------------------------------------------
    def test_12_invalid_extractor_return_type_fails_document(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-invalid-type",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: "not-a-sequence")  # type: ignore
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.FAILED)
        diag = [d for d in result.diagnostics if d.code == DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0].severity, DiagnosticSeverity.ERROR)

    # -------------------------------------------------------------------------
    # Test 13: Invalid observation item type fails document
    # -------------------------------------------------------------------------
    def test_13_invalid_observation_item_type_fails_document(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-invalid-item",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: [{"foreign": "object"}])  # type: ignore
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.FAILED)
        diag = [d for d in result.diagnostics if d.code == DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0].severity, DiagnosticSeverity.ERROR)

    # -------------------------------------------------------------------------
    # Test 14: Extractor exception fails without guessing
    # -------------------------------------------------------------------------
    def test_14_extractor_exception_fails_without_guessing(self) -> None:
        def crash_extractor(res: Any) -> Any:
            raise RuntimeError("Extractor internal failure!")

        descriptor = AdapterDescriptor(
            adapter_id="bca-crash",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        adapter = StubAdapter(descriptor, extractor_func=crash_extractor)
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.FAILED)
        diag = [d for d in result.diagnostics if d.code == DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTION_FAILED]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0].severity, DiagnosticSeverity.ERROR)

    # -------------------------------------------------------------------------
    # Test 15: Resolver exception fails without guessing
    # -------------------------------------------------------------------------
    def test_15_resolver_exception_fails_without_guessing(self) -> None:
        class CrashingResolver:
            def resolve(self, *args: Any, **kwargs: Any) -> Any:
                raise ValueError("Resolver crash simulation!")

        class NoneResolver:
            def resolve(self, *args: Any, **kwargs: Any) -> Any:
                return None

        class ForeignObjectResolver:
            def resolve(self, *args: Any, **kwargs: Any) -> Any:
                return "foreign-non-plan-object"

        class MalformedPlanResolver:
            def resolve(self, *args: Any, **kwargs: Any) -> Any:
                return AccountDiscoveryPlan(
                    resolutions=("not-an-account-resolution",),  # type: ignore
                    diagnostics=(),
                )

        descriptor = AdapterDescriptor(
            adapter_id="bca-res-crash",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-001",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Safe",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs,))

        cases = [
            ("exception", CrashingResolver()),
            ("none", NoneResolver()),
            ("foreign_object", ForeignObjectResolver()),
            ("malformed_plan", MalformedPlanResolver()),
        ]

        for case_name, bad_resolver in cases:
            with self.subTest(case=case_name):
                result = self._run_dry_run(adapter, account_resolver=bad_resolver)  # type: ignore
                self.assertEqual(result.disposition, DryRunDisposition.FAILED)
                fail_diags = [
                    d for d in result.diagnostics
                    if d.code == DIAGNOSTIC_ACCOUNT_DISCOVERY_RESOLUTION_FAILED
                ]
                self.assertEqual(len(fail_diags), 1)
                self.assertEqual(fail_diags[0].severity, DiagnosticSeverity.ERROR)
                self.assertEqual(
                    fail_diags[0].message,
                    "Account discovery resolution execution failed.",
                )
                self.assertIsNone(result.account_discovery_plan)

    # -------------------------------------------------------------------------
    # Test 16: Valid NEW persistence-eligible resolution preserves READY_FOR_STAGING
    # -------------------------------------------------------------------------
    def test_16_valid_new_persistence_eligible_resolution_preserves_ready_for_staging(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-valid-new",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-VALID-001",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Valid",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs,))
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.READY_FOR_STAGING)
        self.assertIsNotNone(result.account_discovery_plan)
        self.assertEqual(len(result.account_discovery_plan.resolutions), 1)
        res = result.account_discovery_plan.resolutions[0]
        self.assertTrue(res.persistence_eligible)
        self.assertEqual(res.lifecycle_state, LifecycleState.NEW)

    # -------------------------------------------------------------------------
    # Test 17: UNVERIFIED or non-persistence-eligible requires review
    # -------------------------------------------------------------------------
    def test_17_unverified_or_non_persistence_eligible_requires_review(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-unverified",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key="",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Unverified",
            ownership_state=OwnershipState.UNKNOWN,
            ownership_confidence=ConfidenceLevel.UNKNOWN,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs,))
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.REVIEW_REQUIRED)
        self.assertTrue(
            any(d.code == DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED for d in result.diagnostics)
        )

    # -------------------------------------------------------------------------
    # Test 18: Exact duplicate short-circuits before extraction and resolution
    # -------------------------------------------------------------------------
    def test_18_exact_duplicate_short_circuits_before_extraction_and_resolution(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-exact-dup",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        data = b"synthetic-duplicate-data"
        existing_rec = SourceDocumentReadRecord(
            source_document_id="doc-existing-dup-1",
            source_registry_id="bca_statement",
            content_sha256=_sha256_hex(data),
            natural_document_key="bca:dup:2026-03",
            semantic_sha256="sem-sha-dup-1",
        )
        registry = MockRegistry(
            exact=(existing_rec,),
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
        )
        extractor_called = False

        def spy_extractor(res: Any) -> Any:
            nonlocal extractor_called
            extractor_called = True
            return ()

        adapter = StubAdapter(descriptor, extractor_func=spy_extractor)
        result = self._run_dry_run(adapter, data=data, registry=registry)
        self.assertEqual(result.disposition, DryRunDisposition.SKIPPED_DUPLICATE)
        self.assertEqual(result.identity_status, DocumentIdentityStatus.EXACT_DUPLICATE)
        self.assertIsNone(result.account_discovery_plan)
        self.assertFalse(extractor_called)
        self.assertEqual(adapter.parse_calls, 0)

    # -------------------------------------------------------------------------
    # Test 19: Semantic duplicate remains SKIPPED_DUPLICATE
    # -------------------------------------------------------------------------
    def test_19_semantic_duplicate_remains_skipped_duplicate(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-sem-dup",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-SEM-001",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Sem",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, natural_key="bca:main:2026-03", extractor_func=lambda res: (obs,))
        res1 = self._run_dry_run(adapter)
        expected_sem_sha = res1.semantic_sha256
        natural_rec = SourceDocumentReadRecord(
            source_document_id="doc-existing-sem-1",
            source_registry_id="bca_statement",
            content_sha256="different-content-sha-1",
            natural_document_key="bca:main:2026-03",
            semantic_sha256=expected_sem_sha,
        )
        registry = MockRegistry(
            natural=(natural_rec,),
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
        )
        result = self._run_dry_run(adapter, registry=registry)
        self.assertEqual(result.disposition, DryRunDisposition.SKIPPED_DUPLICATE)
        self.assertEqual(result.identity_status, DocumentIdentityStatus.SEMANTIC_DUPLICATE)
        self.assertIsNotNone(result.account_discovery_plan)
        self.assertEqual(len(result.account_discovery_plan.resolutions), 1)

    # -------------------------------------------------------------------------
    # Test 20: Adapter REVIEW_REQUIRED remains REVIEW_REQUIRED
    # -------------------------------------------------------------------------
    def test_20_adapter_review_required_remains_review_required(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-review-req",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-PERFECT-001",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Perfect",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(
            descriptor,
            parse_status=AdapterParseStatus.REVIEW_REQUIRED,
            extractor_func=lambda res: (obs,),
        )
        result = self._run_dry_run(adapter)
        self.assertEqual(result.disposition, DryRunDisposition.REVIEW_REQUIRED)
        self.assertIsNotNone(result.account_discovery_plan)
        self.assertTrue(result.account_discovery_plan.resolutions[0].persistence_eligible)

    # -------------------------------------------------------------------------
    # Test 21: Semantic SHA-256 unchanged by existing account state and lifecycle
    # -------------------------------------------------------------------------
    def test_21_semantic_sha256_unchanged_by_existing_account_state_and_lifecycle(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-lifecycle-invariance",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-INVARIANT-001",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Invariant",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs,))
        result_without_existing = self._run_dry_run(adapter, existing_accounts=None)
        existing_account = ExistingAccountState(
            protected_account_key=self.protector.protect_account_key(
                "bca",
                "bca_statement",
                AccountType.TRANSACTIONAL,
                "SYNTHETIC-ACC-INVARIANT-001",
            ),
            institution_id="bca",
            source_registry_id="bca_statement",
            display_name_safe="BCA Invariant",
            account_type=AccountType.TRANSACTIONAL,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.UNCHANGED,
            effective_date="2025-01-01",
        )
        result_with_existing = self._run_dry_run(adapter, existing_accounts=[existing_account])
        self.assertEqual(
            result_without_existing.semantic_sha256,
            result_with_existing.semantic_sha256,
        )
        self.assertEqual(
            result_without_existing.account_discovery_plan.resolutions[0].lifecycle_state,
            LifecycleState.NEW,
        )
        self.assertEqual(
            result_with_existing.account_discovery_plan.resolutions[0].lifecycle_state,
            LifecycleState.UNCHANGED,
        )

    # -------------------------------------------------------------------------
    # Test 22: Safe JSON contains sanitized plan, no raw identifiers or private payload
    # -------------------------------------------------------------------------
    def test_22_safe_json_contains_sanitized_plan_no_raw_identifiers_or_private_event_payload(self) -> None:
        sentinel_raw_key = "SYNTHETIC-PRIVATE-ACC-777888999"
        sentinel_secret = TEST_SECRET
        descriptor = AdapterDescriptor(
            adapter_id="bca-privacy-test",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs = AccountDiscoveryObservation(
            raw_account_key=sentinel_raw_key,
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Safe Privacy",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs,))
        art = _make_artifact(b"synthetic-document-content-v1")
        batch = dry_run_batch(
            "root",
            DiscoveryResult(
                artifacts=(art,),
                diagnostics=(),
                archives_seen=0,
                physical_files_seen=1,
                supported_occurrences=1,
            ),
            registry=MockRegistry(source_registry_id="bca_statement", template_id="bca_monthly_statement_v1"),
            adapter_catalog=AdapterCatalog((adapter,)),
            payload_reader=lambda r, a, **k: b"synthetic-document-content-v1",
            preflight_func=lambda p, **k: _make_preflight(
                b"synthetic-document-content-v1",
                source_registry_id="bca_statement",
                template_id="bca_monthly_statement_v1",
            ),
            account_resolver=self.resolver,
        )
        json_output = safe_dry_run_batch_json(batch)
        self.assertNotIn(sentinel_raw_key, json_output)
        self.assertNotIn(sentinel_secret, json_output)
        data = json.loads(json_output)
        doc_entry = data["documents"][0]
        self.assertIn("account_discovery", doc_entry)
        acc_disc = doc_entry["account_discovery"]
        self.assertIsNotNone(acc_disc)
        self.assertEqual(len(acc_disc["resolutions"]), 1)
        res_entry = acc_disc["resolutions"][0]
        self.assertIn("protected_account_key", res_entry)
        self.assertTrue(res_entry["protected_account_key"].startswith("v1:"))
        self.assertEqual(res_entry["display_name_safe"], "BCA Safe Privacy")

    # -------------------------------------------------------------------------
    # Test 23: Deterministic plans and byte-identical safe JSON across repeated dry runs
    # -------------------------------------------------------------------------
    def test_23_deterministic_plans_and_byte_identical_safe_json_across_repeated_dry_runs(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="bca-determinism",
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
        )
        obs1 = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-DET-001",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA Det 1",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        obs2 = AccountDiscoveryObservation(
            raw_account_key="SYNTHETIC-ACC-DET-002",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.SAVINGS,
            display_name_safe="BCA Det 2",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs2, obs1))
        art = _make_artifact(b"synthetic-deterministic-content")

        def run_batch_once() -> str:
            batch = dry_run_batch(
                "root",
                DiscoveryResult(
                    artifacts=(art,),
                    diagnostics=(),
                    archives_seen=0,
                    physical_files_seen=1,
                    supported_occurrences=1,
                ),
                registry=MockRegistry(source_registry_id="bca_statement", template_id="bca_monthly_statement_v1"),
                adapter_catalog=AdapterCatalog((adapter,)),
                payload_reader=lambda r, a, **k: b"synthetic-deterministic-content",
                preflight_func=lambda p, **k: _make_preflight(
                    b"synthetic-deterministic-content",
                    source_registry_id="bca_statement",
                    template_id="bca_monthly_statement_v1",
                ),
                account_resolver=self.resolver,
            )
            return safe_dry_run_batch_json(batch)

        out1 = run_batch_once()
        out2 = run_batch_once()
        self.assertEqual(out1, out2)
        self.assertEqual(out1.encode("utf-8"), out2.encode("utf-8"))

    # -------------------------------------------------------------------------
    # Test 24: SQLite-backed dry run executes no application writes and creates no DB/sidecar
    # -------------------------------------------------------------------------
    def test_24_sqlite_backed_dry_run_executes_no_application_writes_and_creates_no_db_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_registry.db")
            init_con = sqlite3.connect(db_path)
            try:
                init_registry_schema(init_con)
                init_con.commit()
            finally:
                init_con.close()

            with open(db_path, "rb") as f:
                sha_before = hashlib.sha256(f.read()).hexdigest()
            size_before = os.path.getsize(db_path)

            con = sqlite3.connect(db_path)
            forbidden_actions: list[Any] = []
            traced_statements: list[str] = []

            FORBIDDEN_AUTHORIZER_ACTIONS = {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_CREATE_TEMP_INDEX,
                sqlite3.SQLITE_CREATE_TEMP_TABLE,
                sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
                sqlite3.SQLITE_CREATE_TEMP_VIEW,
                sqlite3.SQLITE_CREATE_TRIGGER,
                sqlite3.SQLITE_CREATE_VIEW,
                sqlite3.SQLITE_CREATE_VTABLE,
                sqlite3.SQLITE_DROP_INDEX,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_DROP_TEMP_INDEX,
                sqlite3.SQLITE_DROP_TEMP_TABLE,
                sqlite3.SQLITE_DROP_TEMP_TRIGGER,
                sqlite3.SQLITE_DROP_TEMP_VIEW,
                sqlite3.SQLITE_DROP_TRIGGER,
                sqlite3.SQLITE_DROP_VIEW,
                sqlite3.SQLITE_DROP_VTABLE,
            }

            def _authorizer(action_code: int, arg1: Any, arg2: Any, db_name: Any, trigger_name: Any) -> int:
                if action_code in FORBIDDEN_AUTHORIZER_ACTIONS or (action_code == sqlite3.SQLITE_PRAGMA and arg2 is not None):
                    forbidden_actions.append((action_code, arg1, arg2, db_name, trigger_name))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            con.set_authorizer(_authorizer)
            con.set_trace_callback(traced_statements.append)

            try:
                registry = SqliteRegistryAuthority(con)
                descriptor = AdapterDescriptor(
                    adapter_id="bca-sqlite-test",
                    source_registry_id="bca_statement",
                    template_id="bca_monthly_statement_v1",
                    parser_version="parser-v1",
                    source_channel=SourceChannel.PDF,
                )
                obs = AccountDiscoveryObservation(
                    raw_account_key="SYNTHETIC-ACC-SQLITE-001",
                    parent_raw_account_key=None,
                    institution_id="bca",
                    source_registry_id="bca_statement",
                    account_type=AccountType.TRANSACTIONAL,
                    display_name_safe="BCA Sqlite",
                    ownership_state=OwnershipState.OWNED,
                    ownership_confidence=ConfidenceLevel.HIGH,
                    effective_date="2026-03-01",
                )
                adapter = StubAdapter(descriptor, extractor_func=lambda res: (obs,))
                art = _make_artifact(b"synthetic-sqlite-document")
                preflight = _make_preflight(
                    b"synthetic-sqlite-document",
                    source_registry_id="bca_statement",
                    template_id="bca_monthly_statement_v1",
                )
                result = dry_run_artifact(
                    "root",
                    art,
                    registry=registry,
                    adapter_catalog=AdapterCatalog((adapter,)),
                    payload_reader=lambda r, a, **k: b"synthetic-sqlite-document",
                    preflight_func=lambda p, **k: preflight,
                    account_resolver=self.resolver,
                )
            finally:
                con.close()

            self.assertEqual(forbidden_actions, [])
            self.assertGreater(len(traced_statements), 0)
            FORBIDDEN_SQL_PREFIXES = (
                "INSERT",
                "UPDATE",
                "DELETE",
                "REPLACE",
                "CREATE",
                "ALTER",
                "DROP",
                "VACUUM",
                "ATTACH",
                "DETACH",
            )
            for stmt in traced_statements:
                stmt_upper = stmt.strip().upper()
                for prefix in FORBIDDEN_SQL_PREFIXES:
                    self.assertFalse(
                        stmt_upper.startswith(prefix),
                        f"Forbidden statement executed: {stmt}",
                    )
                if stmt_upper.startswith("PRAGMA"):
                    self.assertNotIn("=", stmt_upper, f"Mutating PRAGMA executed: {stmt}")

            with open(db_path, "rb") as f:
                sha_after = hashlib.sha256(f.read()).hexdigest()
            size_after = os.path.getsize(db_path)

            self.assertEqual(sha_before, sha_after)
            self.assertEqual(size_before, size_after)

            sidecar_files = [f for f in os.listdir(tmpdir) if f.startswith("test_registry.db-")]
            self.assertEqual(sidecar_files, [])
            self.assertFalse(os.path.exists("money_tracks.db"))


if __name__ == "__main__":
    unittest.main()
