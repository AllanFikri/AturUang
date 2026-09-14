"""
Universal Ingestion Phase 5 - Evidence Matching Tests (P5A-R0).

Tests the pure, deterministic, read-only evidence matcher across:
- Contract and identity invariants
- Internal transfer pairing
- Commerce payment matching
- Investment settlement matching
- Orchestration integration and fail-closed safety

Contains exactly 36 focused test methods.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
import hashlib
import json
import os
import sqlite3
import tempfile
from typing import Any, Callable, Sequence
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
    CashMovementEvidence,
    CommerceOrderEvidence,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    InvestmentTradeEvidence,
    NormalizedEventEnvelope,
    SafeDiagnostic,
    SourceEventStatus,
    SourceProvenanceContract,
)
from aturuang.ingestion_account_discovery import (
    AccountDiscoveryDiagnostic,
    AccountDiscoveryObservation,
    AccountDiscoveryPlan,
    AccountDiscoveryResolver,
    AccountResolution,
    ExistingAccountState,
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
from aturuang.ingestion_registry import (
    init_registry_schema,
)
from aturuang.ingestion_registry_service import (
    TemplateResolution,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    DIAGNOSTIC_MATCHER_CONTRACT_FAILURE,
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
)
from aturuang.ingestion_matching import (
    AccountRelationship,
    DeterministicEvidenceMatcher,
    EconomicEventGroup,
    EvidenceMatchDecision,
    EvidenceMatchPlan,
    MatchReasonCode,
    MatchRelation,
    MatchTier,
    MatchingContext,
    ProtectedAccountBinding,
    SafeEvidenceRecord,
    compute_evidence_key,
    compute_group_key,
    make_evidence_record_from_envelope,
)


def _make_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_binding(
    key: str,
    inst: str = "bca",
    acc_type: AccountType = AccountType.TRANSACTIONAL,
    ownership: OwnershipState = OwnershipState.OWNED,
    conf: ConfidenceLevel = ConfidenceLevel.HIGH,
    eligible: bool = True,
) -> ProtectedAccountBinding:
    return ProtectedAccountBinding(
        protected_account_key=key,
        institution_id=inst,
        account_type=acc_type,
        ownership_state=ownership,
        ownership_confidence=conf,
        lifecycle_state=LifecycleState.UNCHANGED,
        persistence_eligible=eligible,
    )


def _make_cash_record(
    seed: str,
    amount: Decimal = Decimal("100000"),
    currency: str = "IDR",
    direction: EventDirection = EventDirection.INFLOW,
    status: SourceEventStatus = SourceEventStatus.POSTED,
    date: str = "2026-03-01",
    binding: ProtectedAccountBinding | None = None,
    source_reg: str = "bca_statement",
    source_doc: str | None = None,
    source_event_id: str | None = None,
    provider_tx_id: str | None = None,
    reference_raw: str | None = None,
    description_raw: str | None = None,
    is_eligible: bool = True,
    requires_review: bool = False,
) -> SafeEvidenceRecord:
    doc_id = source_doc or f"doc-{seed}"
    rf = _make_sha256(f"row-{seed}")
    ekey = compute_evidence_key(doc_id, source_reg, EventRole.CASH_MOVEMENT, rf)
    return SafeEvidenceRecord(
        evidence_key=ekey,
        source_document_id=doc_id,
        source_registry_id=source_reg,
        event_role=EventRole.CASH_MOVEMENT,
        row_fingerprint=rf,
        amount=amount,
        currency=currency,
        direction=direction,
        status=status,
        event_date=date,
        account_binding=binding,
        is_eligible=is_eligible,
        requires_review=requires_review,
        source_event_id=source_event_id,
        provider_transaction_id_raw=provider_tx_id,
        reference_raw=reference_raw,
        description_raw=description_raw,
    )


def _make_order_record(
    seed: str,
    amount: Decimal = Decimal("50000"),
    currency: str = "IDR",
    date: str = "2026-03-01",
    source_reg: str = "shopee_orders",
    source_doc: str | None = None,
    binding: ProtectedAccountBinding | None = None,
    description_raw: str | None = None,
) -> SafeEvidenceRecord:
    doc_id = source_doc or f"doc-{seed}"
    rf = _make_sha256(f"order-row-{seed}")
    ekey = compute_evidence_key(doc_id, source_reg, EventRole.COMMERCE_ORDER, rf)
    return SafeEvidenceRecord(
        evidence_key=ekey,
        source_document_id=doc_id,
        source_registry_id=source_reg,
        event_role=EventRole.COMMERCE_ORDER,
        row_fingerprint=rf,
        amount=amount,
        currency=currency,
        direction=EventDirection.OUTFLOW,
        status=SourceEventStatus.POSTED,
        event_date=date,
        account_binding=binding,
        description_raw=description_raw,
    )


def _make_trade_record(
    seed: str,
    gross: Decimal = Decimal("200000"),
    net: Decimal | None = Decimal("200000"),
    currency: str = "IDR",
    trade_date: str = "2026-03-01",
    settlement_date: str = "2026-03-03",
    side: str = "BUY",
    source_reg: str = "stockbit",
    source_doc: str | None = None,
    binding: ProtectedAccountBinding | None = None,
) -> SafeEvidenceRecord:
    doc_id = source_doc or f"doc-{seed}"
    rf = _make_sha256(f"trade-row-{seed}")
    ekey = compute_evidence_key(doc_id, source_reg, EventRole.INVESTMENT_TRADE, rf)
    direction = EventDirection.OUTFLOW if side == "BUY" else EventDirection.INFLOW
    return SafeEvidenceRecord(
        evidence_key=ekey,
        source_document_id=doc_id,
        source_registry_id=source_reg,
        event_role=EventRole.INVESTMENT_TRADE,
        row_fingerprint=rf,
        amount=net if net is not None else gross,
        currency=currency,
        direction=direction,
        status=SourceEventStatus.POSTED,
        event_date=trade_date,
        settlement_date=settlement_date,
        account_binding=binding,
        trade_side=side,
        gross_amount=gross,
        net_amount=net,
    )


def _make_artifact(
    data: bytes,
    *,
    extension: str = ".pdf",
    locator: str = "private/statement.pdf",
) -> DiscoveredArtifact:
    sha = hashlib.sha256(data).hexdigest()
    return DiscoveredArtifact(
        content_sha256=sha,
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
    source_registry_id: str = "bca_statement",
    template_id: str = "bca_statement_v1",
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
        content_sha256=hashlib.sha256(data).hexdigest(),
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


class StubAdapter:
    def __init__(
        self,
        descriptor: AdapterDescriptor,
        events: Sequence[NormalizedEventEnvelope] = (),
        natural_key: str = "synthetic-natural-key-1",
    ) -> None:
        self.descriptor = descriptor
        self.events = tuple(events)
        self.natural_key = natural_key

    def parse(self, adapter_input: Any) -> AdapterResult:
        source_doc_id = getattr(adapter_input, "source_document_id", "doc-stub")
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
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=getattr(adapter_input, "period_status", PeriodStatus.CLOSED),
            events=events,
            natural_document_key_candidate=self.natural_key,
        )


class MockRegistry:
    def __init__(
        self,
        *,
        exact: Sequence[Any] = (),
        natural: Sequence[Any] = (),
        source_registry_id: str = "bca_statement",
        template_id: str = "bca_statement_v1",
        parser_version: str = "v1",
        template_status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
    ) -> None:
        self.exact = tuple(exact)
        self.natural = tuple(natural)
        self.source_registry_id = source_registry_id
        self.template_id = template_id
        self.parser_version = parser_version
        self.template_status = template_status

    def lookup_exact_documents(self, sha: str) -> tuple[Any, ...]:
        return self.exact

    def lookup_natural_documents(self, reg_id: str, key: str) -> tuple[Any, ...]:
        return self.natural

    def source_capability(self, reg_id: str) -> Any:
        return {
            "source_registry_id": reg_id,
            "channel": "PDF",
        }

    def template_authority(
        self, *, source_registry_id: str, template_fingerprint: str
    ) -> TemplateResolution:
        return TemplateResolution(
            status=self.template_status,
            template_id=self.template_id,
            parser_version=self.parser_version,
        )


class TestUniversalIngestionPhase5Matching(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = DeterministicEvidenceMatcher()

    # -------------------------------------------------------------------------
    # Contract and identity
    # -------------------------------------------------------------------------

    def test_01_valid_evidence_key_is_deterministic(self) -> None:
        rf = _make_sha256("test-row-1")
        k1 = compute_evidence_key("doc-1", "bca_statement", EventRole.CASH_MOVEMENT, rf)
        k2 = compute_evidence_key("doc-1", "bca_statement", EventRole.CASH_MOVEMENT, rf)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)

    def test_02_group_key_is_independent_of_input_order(self) -> None:
        k_a = _make_sha256("member-a")
        k_b = _make_sha256("member-b")
        g1 = compute_group_key(MatchRelation.INTERNAL_TRANSFER_PAIR, [k_a, k_b])
        g2 = compute_group_key(MatchRelation.INTERNAL_TRANSFER_PAIR, [k_b, k_a])
        self.assertEqual(g1, g2)
        self.assertEqual(len(g1), 64)

    def test_03_float_amount_is_rejected(self) -> None:
        rf = _make_sha256("test-row-float")
        ekey = compute_evidence_key("doc-1", "bca_statement", EventRole.CASH_MOVEMENT, rf)
        with self.assertRaises((TypeError, ValueError)):
            SafeEvidenceRecord(
                evidence_key=ekey,
                source_document_id="doc-1",
                source_registry_id="bca_statement",
                event_role=EventRole.CASH_MOVEMENT,
                row_fingerprint=rf,
                amount=100.50,  # type: ignore
            )

    def test_04_non_finite_decimal_is_rejected(self) -> None:
        rf = _make_sha256("test-row-nan")
        ekey = compute_evidence_key("doc-1", "bca_statement", EventRole.CASH_MOVEMENT, rf)
        with self.assertRaises(ValueError):
            SafeEvidenceRecord(
                evidence_key=ekey,
                source_document_id="doc-1",
                source_registry_id="bca_statement",
                event_role=EventRole.CASH_MOVEMENT,
                row_fingerprint=rf,
                amount=Decimal("NaN"),
            )

    def test_05_invalid_enum_type_is_rejected(self) -> None:
        rf = _make_sha256("test-row-enum")
        ekey = compute_evidence_key("doc-1", "bca_statement", EventRole.CASH_MOVEMENT, rf)
        with self.assertRaises(TypeError):
            SafeEvidenceRecord(
                evidence_key=ekey,
                source_document_id="doc-1",
                source_registry_id="bca_statement",
                event_role="NOT_AN_ENUM",  # type: ignore
                row_fingerprint=rf,
            )

    def test_06_safe_repr_and_json_contain_no_raw_private_fields(self) -> None:
        rec = _make_cash_record(
            "privacy-test",
            source_event_id="EVT-PRIV-001",
            provider_tx_id="RAW-TXID-99999",
            reference_raw="RAW-REF-88888",
            description_raw="RAW PRIVATE DESCRIPTION SENSITIVE",
        )
        repr_str = repr(rec)
        self.assertNotIn("RAW-TXID-99999", repr_str)
        self.assertNotIn("RAW-REF-88888", repr_str)
        self.assertNotIn("RAW PRIVATE DESCRIPTION SENSITIVE", repr_str)

        plan = self.matcher.match([rec])
        batch_result = DryRunBatchResult(
            unique_artifact_count=1,
            occurrence_count=1,
            exact_duplicate_count=0,
            semantic_duplicate_count=0,
            ready_for_staging_count=1,
            review_required_count=0,
            failed_count=0,
            documents=(),
            diagnostics=(),
            overall_status=DryRunBatchStatus.COMPLETED,
            match_plan=plan,
        )
        json_str = safe_dry_run_batch_json(batch_result)
        self.assertNotIn("RAW-TXID-99999", json_str)
        self.assertNotIn("RAW-REF-88888", json_str)
        self.assertNotIn("RAW PRIVATE DESCRIPTION SENSITIVE", json_str)

    def test_07_duplicate_source_event_id_produces_exact_duplicate_decision(self) -> None:
        r1 = _make_cash_record("dup-evt-1", source_event_id="EVT-SHARED-001")
        r2 = _make_cash_record("dup-evt-2", source_event_id="EVT-SHARED-001")
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.exact_groups), 1)
        grp = plan.exact_groups[0]
        self.assertEqual(grp.match_relation, MatchRelation.DUPLICATE_EVIDENCE)
        self.assertIn(MatchReasonCode.SAME_SOURCE_EVENT_ID, grp.reason_codes)
        self.assertTrue(grp.is_auto_link_eligible)

    def test_08_duplicate_provider_transaction_id_produces_exact_duplicate_decision(self) -> None:
        r1 = _make_cash_record("dup-tx-1", provider_tx_id="TXID-SHARED-001")
        r2 = _make_cash_record("dup-tx-2", provider_tx_id="TXID-SHARED-001")
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.exact_groups), 1)
        grp = plan.exact_groups[0]
        self.assertEqual(grp.match_relation, MatchRelation.DUPLICATE_EVIDENCE)
        self.assertIn(MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID, grp.reason_codes)

    def test_09_same_raw_description_alone_does_not_match(self) -> None:
        r1 = _make_cash_record("desc-1", description_raw="TRANSFER SHARED DESCRIPTION")
        r2 = _make_cash_record("desc-2", description_raw="TRANSFER SHARED DESCRIPTION")
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.exact_groups), 0)
        self.assertEqual(len(plan.strong_groups), 0)

    def test_10_same_amount_and_date_alone_do_not_produce_exact_identity(self) -> None:
        r1 = _make_cash_record("same-amt-1", amount=Decimal("100000"), date="2026-03-01")
        r2 = _make_cash_record("same-amt-2", amount=Decimal("100000"), date="2026-03-01")
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.exact_groups), 0)

    def test_11_contradictory_exact_identity_becomes_ambiguous(self) -> None:
        r1 = _make_cash_record("conflict-1", source_event_id="EVT-CONFLICT-001", amount=Decimal("100000"))
        r2 = _make_cash_record("conflict-2", source_event_id="EVT-CONFLICT-001", amount=Decimal("200000"))
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.exact_groups), 0)
        self.assertEqual(len(plan.ambiguous_evidence), 2)
        for d in plan.ambiguous_evidence:
            self.assertIn(MatchReasonCode.CONFLICTING_EXACT_IDENTITY, d.reason_codes)

    def test_12_replaying_identical_input_produces_byte_identical_safe_json(self) -> None:
        r1 = _make_cash_record("replay-1", source_event_id="EVT-REP-001")
        r2 = _make_cash_record("replay-2", source_event_id="EVT-REP-001")
        plan1 = self.matcher.match([r1, r2])
        plan2 = self.matcher.match([r1, r2])
        b1 = DryRunBatchResult(
            unique_artifact_count=1,
            occurrence_count=2,
            exact_duplicate_count=0,
            semantic_duplicate_count=0,
            ready_for_staging_count=1,
            review_required_count=0,
            failed_count=0,
            documents=(),
            diagnostics=(),
            overall_status=DryRunBatchStatus.COMPLETED,
            match_plan=plan1,
        )
        b2 = DryRunBatchResult(
            unique_artifact_count=1,
            occurrence_count=2,
            exact_duplicate_count=0,
            semantic_duplicate_count=0,
            ready_for_staging_count=1,
            review_required_count=0,
            failed_count=0,
            documents=(),
            diagnostics=(),
            overall_status=DryRunBatchStatus.COMPLETED,
            match_plan=plan2,
        )
        json1 = safe_dry_run_batch_json(b1)
        json2 = safe_dry_run_batch_json(b2)
        self.assertEqual(json1, json2)
        self.assertEqual(json1.encode("utf-8"), json2.encode("utf-8"))

    # -------------------------------------------------------------------------
    # Internal transfer
    # -------------------------------------------------------------------------

    def test_13_explicit_owned_opposite_pair_produces_strong(self) -> None:
        b1 = _make_binding("ACC-OWNED-001")
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("transfer-out", amount=Decimal("150000"), direction=EventDirection.OUTFLOW, date="2026-03-01", binding=b1)
        r2 = _make_cash_record("transfer-in", amount=Decimal("150000"), direction=EventDirection.INFLOW, date="2026-03-02", binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 1)
        grp = plan.strong_groups[0]
        self.assertEqual(grp.match_relation, MatchRelation.INTERNAL_TRANSFER_PAIR)
        self.assertIn(MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT, grp.reason_codes)
        self.assertTrue(grp.is_auto_link_eligible)

    def test_14_same_direction_does_not_match(self) -> None:
        b1 = _make_binding("ACC-OWNED-001")
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("same-dir-1", amount=Decimal("150000"), direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("same-dir-2", amount=Decimal("150000"), direction=EventDirection.OUTFLOW, binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_15_different_amount_does_not_match(self) -> None:
        b1 = _make_binding("ACC-OWNED-001")
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("diff-amt-1", amount=Decimal("150000"), direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("diff-amt-2", amount=Decimal("150001"), direction=EventDirection.INFLOW, binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_16_different_currency_does_not_match(self) -> None:
        b1 = _make_binding("ACC-OWNED-001")
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("curr-idr", currency="IDR", direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("curr-usd", currency="USD", direction=EventDirection.INFLOW, binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_17_date_outside_two_day_window_does_not_match(self) -> None:
        b1 = _make_binding("ACC-OWNED-001")
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("date-d1", date="2026-03-01", direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("date-d4", date="2026-03-04", direction=EventDirection.INFLOW, binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_18_missing_account_binding_prevents_auto_match(self) -> None:
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("no-binding-1", direction=EventDirection.OUTFLOW, binding=None)
        r2 = _make_cash_record("has-binding-2", direction=EventDirection.INFLOW, binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)
        d1 = [d for d in plan.decisions if d.evidence_key == r1.evidence_key][0]
        self.assertIn(MatchReasonCode.MISSING_ACCOUNT_BINDING, d1.reason_codes)

    def test_19_unknown_ownership_prevents_auto_match(self) -> None:
        b1 = _make_binding("ACC-UNKNOWN-001", ownership=OwnershipState.UNKNOWN)
        b2 = _make_binding("ACC-OWNED-002", ownership=OwnershipState.OWNED)
        r1 = _make_cash_record("unk-owner-1", direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("owned-owner-2", direction=EventDirection.INFLOW, binding=b2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_20_same_protected_account_on_both_sides_prevents_transfer_pairing(self) -> None:
        b1 = _make_binding("ACC-SAME-001")
        r1 = _make_cash_record("same-acc-1", direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("same-acc-2", direction=EventDirection.INFLOW, binding=b1)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_21_multiple_equal_counterpart_candidates_become_ambiguous(self) -> None:
        b_src = _make_binding("ACC-SRC-001")
        b_dst1 = _make_binding("ACC-DST-001")
        b_dst2 = _make_binding("ACC-DST-002")
        r_out = _make_cash_record("amb-out", amount=Decimal("100000"), direction=EventDirection.OUTFLOW, binding=b_src)
        r_in1 = _make_cash_record("amb-in1", amount=Decimal("100000"), direction=EventDirection.INFLOW, binding=b_dst1)
        r_in2 = _make_cash_record("amb-in2", amount=Decimal("100000"), direction=EventDirection.INFLOW, binding=b_dst2)
        plan = self.matcher.match([r_out, r_in1, r_in2])
        self.assertEqual(len(plan.strong_groups), 0)
        self.assertEqual(len(plan.ambiguous_evidence), 3)
        for d in plan.ambiguous_evidence:
            self.assertIn(MatchReasonCode.MULTIPLE_COMPATIBLE_MATCHES, d.reason_codes)

    def test_22_input_reordering_produces_the_same_transfer_plan(self) -> None:
        b1 = _make_binding("ACC-OWNED-001")
        b2 = _make_binding("ACC-OWNED-002")
        r1 = _make_cash_record("reorder-1", amount=Decimal("100000"), direction=EventDirection.OUTFLOW, binding=b1)
        r2 = _make_cash_record("reorder-2", amount=Decimal("100000"), direction=EventDirection.INFLOW, binding=b2)
        r3 = _make_cash_record("reorder-3", amount=Decimal("50000"), direction=EventDirection.OUTFLOW, binding=b1)
        plan_a = self.matcher.match([r1, r2, r3])
        plan_b = self.matcher.match([r3, r2, r1])
        self.assertEqual(plan_a.groups, plan_b.groups)
        self.assertEqual(plan_a.decisions, plan_b.decisions)

    # -------------------------------------------------------------------------
    # Commerce
    # -------------------------------------------------------------------------

    def test_23_explicit_unique_commerce_payment_produces_strong(self) -> None:
        b_cash = _make_binding("ACC-BCA-001")
        order = _make_order_record("comm-ord-1", amount=Decimal("75000"), date="2026-03-01", source_reg="shopee_orders")
        cash = _make_cash_record("comm-csh-1", amount=Decimal("75000"), direction=EventDirection.OUTFLOW, date="2026-03-02", binding=b_cash)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="shopee_orders", target_key="ACC-BCA-001", relationship_kind="COMMERCE_PAYMENT"),
            )
        )
        plan = self.matcher.match([order, cash], context=ctx)
        self.assertEqual(len(plan.strong_groups), 1)
        grp = plan.strong_groups[0]
        self.assertEqual(grp.match_relation, MatchRelation.COMMERCE_PAYMENT)
        self.assertIn(MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION, grp.reason_codes)

    def test_24_commerce_match_requires_outgoing_cash(self) -> None:
        b_cash = _make_binding("ACC-BCA-001")
        order = _make_order_record("comm-ord-in", amount=Decimal("75000"))
        cash = _make_cash_record("comm-csh-in", amount=Decimal("75000"), direction=EventDirection.INFLOW, binding=b_cash)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="shopee_orders", target_key="ACC-BCA-001", relationship_kind="COMMERCE_PAYMENT"),
            )
        )
        plan = self.matcher.match([order, cash], context=ctx)
        self.assertEqual(len(plan.strong_groups), 0)

    def test_25_missing_provider_account_relationship_prevents_auto_match(self) -> None:
        b_cash = _make_binding("ACC-BCA-001")
        order = _make_order_record("comm-norel-1", amount=Decimal("75000"))
        cash = _make_cash_record("comm-norel-2", amount=Decimal("75000"), direction=EventDirection.OUTFLOW, binding=b_cash)
        plan = self.matcher.match([order, cash], context=MatchingContext())
        self.assertEqual(len(plan.strong_groups), 0)

    def test_26_merchant_or_description_similarity_alone_is_insufficient(self) -> None:
        b_cash = _make_binding("ACC-BCA-001")
        order = _make_order_record("comm-sim-ord", amount=Decimal("75000"), description_raw="SHOPEE PAY TOKO ABC")
        cash = _make_cash_record("comm-sim-csh", amount=Decimal("75000"), direction=EventDirection.OUTFLOW, description_raw="SHOPEE PAY TOKO ABC", binding=b_cash)
        plan = self.matcher.match([order, cash], context=MatchingContext())
        self.assertEqual(len(plan.strong_groups), 0)

    def test_27_multiple_compatible_cash_payments_become_ambiguous(self) -> None:
        b_cash = _make_binding("ACC-BCA-001")
        order = _make_order_record("comm-mult-ord", amount=Decimal("75000"), source_reg="shopee_orders")
        cash1 = _make_cash_record("comm-mult-csh1", amount=Decimal("75000"), direction=EventDirection.OUTFLOW, binding=b_cash)
        cash2 = _make_cash_record("comm-mult-csh2", amount=Decimal("75000"), direction=EventDirection.OUTFLOW, binding=b_cash)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="shopee_orders", target_key="ACC-BCA-001", relationship_kind="COMMERCE_PAYMENT"),
            )
        )
        plan = self.matcher.match([order, cash1, cash2], context=ctx)
        self.assertEqual(len(plan.strong_groups), 0)
        self.assertEqual(len(plan.ambiguous_evidence), 3)

    # -------------------------------------------------------------------------
    # Investment
    # -------------------------------------------------------------------------

    def test_28_explicit_buy_settlement_produces_strong_with_compatible_outflow(self) -> None:
        b_rdn = _make_binding("ACC-RDN-001", acc_type=AccountType.RDN)
        trade = _make_trade_record("inv-buy-1", gross=Decimal("200000"), net=Decimal("200300"), side="BUY", settlement_date="2026-03-03", source_reg="stockbit")
        cash = _make_cash_record("inv-buy-csh", amount=Decimal("200300"), direction=EventDirection.OUTFLOW, date="2026-03-03", binding=b_rdn)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="stockbit", target_key="ACC-RDN-001", relationship_kind="INVESTMENT_SETTLEMENT"),
            )
        )
        plan = self.matcher.match([trade, cash], context=ctx)
        self.assertEqual(len(plan.strong_groups), 1)
        grp = plan.strong_groups[0]
        self.assertEqual(grp.match_relation, MatchRelation.INVESTMENT_SETTLEMENT)
        self.assertIn(MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION, grp.reason_codes)

    def test_29_explicit_sell_settlement_produces_strong_with_compatible_inflow(self) -> None:
        b_rdn = _make_binding("ACC-RDN-001", acc_type=AccountType.RDN)
        trade = _make_trade_record("inv-sell-1", gross=Decimal("300000"), net=Decimal("299500"), side="SELL", settlement_date="2026-03-03", source_reg="stockbit")
        cash = _make_cash_record("inv-sell-csh", amount=Decimal("299500"), direction=EventDirection.INFLOW, date="2026-03-03", binding=b_rdn)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="stockbit", target_key="ACC-RDN-001", relationship_kind="INVESTMENT_SETTLEMENT"),
            )
        )
        plan = self.matcher.match([trade, cash], context=ctx)
        self.assertEqual(len(plan.strong_groups), 1)
        grp = plan.strong_groups[0]
        self.assertEqual(grp.match_relation, MatchRelation.INVESTMENT_SETTLEMENT)

    def test_30_gross_amount_must_not_replace_missing_net_settlement_amount(self) -> None:
        b_rdn = _make_binding("ACC-RDN-001", acc_type=AccountType.RDN)
        trade = _make_trade_record("inv-gross-only", gross=Decimal("200000"), net=None, side="BUY", settlement_date="2026-03-03", source_reg="stockbit")
        cash = _make_cash_record("inv-gross-csh", amount=Decimal("200000"), direction=EventDirection.OUTFLOW, date="2026-03-03", binding=b_rdn)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="stockbit", target_key="ACC-RDN-001", relationship_kind="INVESTMENT_SETTLEMENT"),
            )
        )
        plan = self.matcher.match([trade, cash], context=ctx)
        self.assertEqual(len(plan.strong_groups), 0)

    def test_31_settlement_date_mismatch_prevents_auto_match(self) -> None:
        b_rdn = _make_binding("ACC-RDN-001", acc_type=AccountType.RDN)
        trade = _make_trade_record("inv-date-trade", net=Decimal("200000"), side="BUY", settlement_date="2026-03-03", source_reg="stockbit")
        cash = _make_cash_record("inv-date-csh", amount=Decimal("200000"), direction=EventDirection.OUTFLOW, date="2026-03-05", binding=b_rdn)
        ctx = MatchingContext(
            relationships=(
                AccountRelationship(source_key="stockbit", target_key="ACC-RDN-001", relationship_kind="INVESTMENT_SETTLEMENT"),
            )
        )
        plan = self.matcher.match([trade, cash], context=ctx)
        self.assertEqual(len(plan.strong_groups), 0)

    def test_32_missing_broker_rdn_relationship_prevents_auto_match(self) -> None:
        b_rdn = _make_binding("ACC-RDN-001", acc_type=AccountType.RDN)
        trade = _make_trade_record("inv-norel-trade", net=Decimal("200000"), side="BUY", settlement_date="2026-03-03")
        cash = _make_cash_record("inv-norel-csh", amount=Decimal("200000"), direction=EventDirection.OUTFLOW, date="2026-03-03", binding=b_rdn)
        plan = self.matcher.match([trade, cash], context=MatchingContext())
        self.assertEqual(len(plan.strong_groups), 0)

    def test_33_unrealized_holding_valuation_evidence_never_emits_cash_matching_semantics(self) -> None:
        rf = _make_sha256("holding-rf")
        ekey = compute_evidence_key("doc-h", "stockbit", EventRole.BALANCE_SNAPSHOT, rf)
        holding_rec = SafeEvidenceRecord(
            evidence_key=ekey,
            source_document_id="doc-h",
            source_registry_id="stockbit",
            event_role=EventRole.BALANCE_SNAPSHOT,
            row_fingerprint=rf,
            amount=Decimal("5000000"),
            currency="IDR",
            direction=EventDirection.NEUTRAL,
            status=SourceEventStatus.POSTED,
            event_date="2026-03-01",
            is_eligible=False,
        )
        cash = _make_cash_record("cash-compare", amount=Decimal("5000000"))
        plan = self.matcher.match([holding_rec, cash])
        self.assertEqual(len(plan.strong_groups), 0)
        self.assertEqual(len(plan.exact_groups), 0)
        h_dec = [d for d in plan.decisions if d.evidence_key == ekey][0]
        self.assertEqual(h_dec.match_tier, MatchTier.INELIGIBLE)
        self.assertIn(MatchReasonCode.UNSUPPORTED_ROLE_PAIR, h_dec.reason_codes)

    # -------------------------------------------------------------------------
    # Orchestration and safety
    # -------------------------------------------------------------------------

    def test_34_batch_matching_runs_after_valid_account_discovery_and_appears_in_safe_json(self) -> None:
        b_src = _make_binding("ACC-BATCH-001")
        b_dst = _make_binding("ACC-BATCH-002")

        art1_content = b"content-artifact-1"
        art2_content = b"content-artifact-2"
        sha1 = hashlib.sha256(art1_content).hexdigest()
        sha2 = hashlib.sha256(art2_content).hexdigest()
        doc1_id = "dryrun-" + sha1
        doc2_id = "dryrun-" + sha2

        rf1 = _make_sha256("rf-batch-1")
        env1 = NormalizedEventEnvelope(
            source_document_id=doc1_id,
            source_registry_id="bca_statement",
            template_id="bca_statement_v1",
            parser_version="v1",
            source_channel=SourceChannel.PDF,
            event_role=EventRole.CASH_MOVEMENT,
            source_event_id=None,
            row_fingerprint=rf1,
            evidence_quality=ConfidenceLevel.HIGH,
            parse_confidence=ConfidenceLevel.HIGH,
            provenance=SourceProvenanceContract(
                source_document_id=doc1_id,
                raw_locator="doc1.pdf",
            ),
            payload=CashMovementEvidence(
                amount=Decimal("250000"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                occurred_at="2026-03-01",
            ),
        )

        rf2 = _make_sha256("rf-batch-2")
        env2 = NormalizedEventEnvelope(
            source_document_id=doc2_id,
            source_registry_id="bca_statement",
            template_id="bca_statement_v1",
            parser_version="v1",
            source_channel=SourceChannel.PDF,
            event_role=EventRole.CASH_MOVEMENT,
            source_event_id=None,
            row_fingerprint=rf2,
            evidence_quality=ConfidenceLevel.HIGH,
            parse_confidence=ConfidenceLevel.HIGH,
            provenance=SourceProvenanceContract(
                source_document_id=doc2_id,
                raw_locator="doc2.pdf",
            ),
            payload=CashMovementEvidence(
                amount=Decimal("250000"),
                currency="IDR",
                direction=EventDirection.INFLOW,
                status=SourceEventStatus.POSTED,
                occurred_at="2026-03-02",
            ),
        )

        plan1 = AccountDiscoveryPlan(
            resolutions=(
                AccountResolution(
                    protected_account_key="ACC-BATCH-001",
                    institution_id="bca",
                    source_registry_id="bca_statement",
                    display_name_safe="BCA 1",
                    account_type=AccountType.TRANSACTIONAL,
                    ownership_state=OwnershipState.OWNED,
                    ownership_confidence=ConfidenceLevel.HIGH,
                    lifecycle_state=LifecycleState.UNCHANGED,
                    effective_date="2026-03-01",
                    persistence_eligible=True,
                ),
            )
        )
        plan2 = AccountDiscoveryPlan(
            resolutions=(
                AccountResolution(
                    protected_account_key="ACC-BATCH-002",
                    institution_id="bca",
                    source_registry_id="bca_statement",
                    display_name_safe="BCA 2",
                    account_type=AccountType.TRANSACTIONAL,
                    ownership_state=OwnershipState.OWNED,
                    ownership_confidence=ConfidenceLevel.HIGH,
                    lifecycle_state=LifecycleState.UNCHANGED,
                    effective_date="2026-03-01",
                    persistence_eligible=True,
                ),
            )
        )

        obs1 = AccountDiscoveryObservation(
            raw_account_key="ACC-1",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA 1",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )
        obs2 = AccountDiscoveryObservation(
            raw_account_key="ACC-2",
            parent_raw_account_key=None,
            institution_id="bca",
            source_registry_id="bca_statement",
            account_type=AccountType.TRANSACTIONAL,
            display_name_safe="BCA 2",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            effective_date="2026-03-01",
        )

        descriptor = AdapterDescriptor(
            adapter_id="bca-test",
            source_registry_id="bca_statement",
            template_id="bca_statement_v1",
            parser_version="v1",
            source_channel=SourceChannel.PDF,
        )

        class MultiAdapter:
            def __init__(self, desc: AdapterDescriptor) -> None:
                self.descriptor = desc

            def parse(self, adapter_input: Any) -> AdapterResult:
                evs = (env1,) if adapter_input.source_document_id == doc1_id else (env2,)
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=adapter_input.source_document_id,
                    parse_status=AdapterParseStatus.COMPLETED,
                    period_status=PeriodStatus.CLOSED,
                    events=evs,
                    natural_document_key_candidate=f"nat-{adapter_input.source_document_id}",
                )

            def extract_account_observations(self, result: AdapterResult) -> Sequence[AccountDiscoveryObservation]:
                if result.source_document_id == doc1_id:
                    return (obs1,)
                return (obs2,)

        multi_adapter = MultiAdapter(descriptor)

        class MultiResolver:
            def resolve(self, obs: Any, **kwargs: Any) -> Any:
                if any(getattr(o, "raw_account_key", "") == "ACC-1" for o in obs):
                    return plan1
                return plan2

        art1 = _make_artifact(art1_content, locator="doc1.pdf")
        art2 = _make_artifact(art2_content, locator="doc2.pdf")

        discovery = DiscoveryResult(
            artifacts=(art1, art2),
            diagnostics=(),
            archives_seen=0,
            physical_files_seen=2,
            supported_occurrences=2,
        )

        payload_map = {sha1: art1_content, sha2: art2_content}
        batch = dry_run_batch(
            "root",
            discovery,
            registry=MockRegistry(),
            adapter_catalog=AdapterCatalog((multi_adapter,)),
            payload_reader=lambda r, a, **k: payload_map[a.content_sha256],
            preflight_func=lambda p, **k: _make_preflight(p),
            account_resolver=MultiResolver(),
        )

        self.assertIsNotNone(batch.match_plan)
        self.assertEqual(len(batch.match_plan.strong_groups), 1)
        json_out = safe_dry_run_batch_json(batch)
        data = json.loads(json_out)
        self.assertIn("matching", data)
        self.assertEqual(data["matching"]["counts_by_tier"]["STRONG"], 1)
        self.assertEqual(data["matching"]["counts_by_relation"]["INTERNAL_TRANSFER_PAIR"], 1)

    def test_35_raising_none_foreign_and_malformed_matcher_outputs_all_fail_closed(self) -> None:
        class RaisingMatcher:
            def match(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("Crashing matcher simulation!")

        class NoneMatcher:
            def match(self, *args: Any, **kwargs: Any) -> Any:
                return None

        class ForeignMatcher:
            def match(self, *args: Any, **kwargs: Any) -> Any:
                return {"foreign": "not an EvidenceMatchPlan"}

        class MalformedKeysMatcher:
            def match(self, records: Sequence[SafeEvidenceRecord], **kwargs: Any) -> Any:
                bogus_key = _make_sha256("bogus-unknown-evidence-key")
                dec = EvidenceMatchDecision(
                    evidence_key=bogus_key,
                    match_tier=MatchTier.UNMATCHED,
                    match_relation=None,
                    group_key=None,
                    reason_codes=(),
                    is_auto_link_eligible=False,
                )
                return EvidenceMatchPlan(
                    matcher_contract_version="v1",
                    decisions=(dec,),
                    groups=(),
                )

        class ConflictingAutoGroupsMatcher:
            def match(self, records: Sequence[SafeEvidenceRecord], **kwargs: Any) -> Any:
                if not records:
                    return EvidenceMatchPlan(matcher_contract_version="v1", decisions=(), groups=())
                k = records[0].evidence_key
                g1 = EconomicEventGroup(
                    group_key=_make_sha256("g1"),
                    match_tier=MatchTier.STRONG,
                    match_relation=MatchRelation.INTERNAL_TRANSFER_PAIR,
                    member_evidence_keys=(k,),
                    reason_codes=(MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT,),
                    is_auto_link_eligible=True,
                )
                g2 = EconomicEventGroup(
                    group_key=_make_sha256("g2"),
                    match_tier=MatchTier.STRONG,
                    match_relation=MatchRelation.INTERNAL_TRANSFER_PAIR,
                    member_evidence_keys=(k,),
                    reason_codes=(MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT,),
                    is_auto_link_eligible=True,
                )
                return EvidenceMatchPlan(
                    matcher_contract_version="v1",
                    decisions=(),
                    groups=(g1, g2),
                )

        cases = [
            ("raising", RaisingMatcher()),
            ("none", NoneMatcher()),
            ("foreign", ForeignMatcher()),
            ("malformed_keys", MalformedKeysMatcher()),
            ("conflicting_groups", ConflictingAutoGroupsMatcher()),
        ]

        art_content = b"content-single-doc"
        art = _make_artifact(art_content)
        discovery = DiscoveryResult(
            artifacts=(art,),
            diagnostics=(),
            archives_seen=0,
            physical_files_seen=1,
            supported_occurrences=1,
        )
        descriptor = AdapterDescriptor(
            adapter_id="bca-stub",
            source_registry_id="bca_statement",
            template_id="bca_statement_v1",
            parser_version="v1",
            source_channel=SourceChannel.PDF,
        )
        rf = _make_sha256("rf-stub")
        env = NormalizedEventEnvelope(
            source_document_id="doc-stub",
            source_registry_id="bca_statement",
            template_id="bca_statement_v1",
            parser_version="v1",
            source_channel=SourceChannel.PDF,
            event_role=EventRole.CASH_MOVEMENT,
            source_event_id=None,
            row_fingerprint=rf,
            evidence_quality=ConfidenceLevel.HIGH,
            parse_confidence=ConfidenceLevel.HIGH,
            provenance=SourceProvenanceContract(
                source_document_id="doc-stub",
                raw_locator="private/statement.pdf",
            ),
            payload=CashMovementEvidence(
                amount=Decimal("100000"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                occurred_at="2026-03-01",
            ),
        )
        adapter = StubAdapter(descriptor, events=[env])
        preflight = _make_preflight(art_content)

        for case_name, bad_matcher in cases:
            with self.subTest(case=case_name):
                result = dry_run_batch(
                    "root",
                    discovery,
                    registry=MockRegistry(),
                    adapter_catalog=AdapterCatalog((adapter,)),
                    payload_reader=lambda r, a, **k: art_content,
                    preflight_func=lambda p, **k: preflight,
                    evidence_matcher=bad_matcher,
                )
                self.assertIsNone(result.match_plan)
                fail_diags = [
                    d for d in result.diagnostics
                    if d.code == DIAGNOSTIC_MATCHER_CONTRACT_FAILURE
                ]
                self.assertEqual(len(fail_diags), 1)
                self.assertEqual(fail_diags[0].severity, DiagnosticSeverity.ERROR)
                self.assertEqual(fail_diags[0].stage, DryRunStage.MATCHING)
                self.assertEqual(fail_diags[0].message, "Evidence matching execution failed.")
                self.assertEqual(result.overall_status, DryRunBatchStatus.FAILED)

    def test_36_sqlite_authorizer_proves_zero_write_attempts_and_no_sidecars(self) -> None:
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
                art_content = b"content-sqlite-test"
                art = _make_artifact(art_content)
                discovery = DiscoveryResult(
                    artifacts=(art,),
                    diagnostics=(),
                    archives_seen=0,
                    physical_files_seen=1,
                    supported_occurrences=1,
                )
                descriptor = AdapterDescriptor(
                    adapter_id="bca-sqlite-test",
                    source_registry_id="bca_statement",
                    template_id="bca_statement_v1",
                    parser_version="v1",
                    source_channel=SourceChannel.PDF,
                )
                adapter = StubAdapter(descriptor)
                preflight = _make_preflight(art_content)
                batch = dry_run_batch(
                    "root",
                    discovery,
                    registry=registry,
                    adapter_catalog=AdapterCatalog((adapter,)),
                    payload_reader=lambda r, a, **k: art_content,
                    preflight_func=lambda p, **k: preflight,
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
