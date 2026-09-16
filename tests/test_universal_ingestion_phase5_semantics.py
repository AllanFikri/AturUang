"""
Universal Ingestion Phase 5C - Minimal Semantics Layer Unit Tests.

Verifies deterministic, explainable, evidence-derived semantic interpretation
on top of validated evidence and match plan outputs.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import unittest

from aturuang.ingestion_adapter import (
    ConfidenceLevel,
    EventDirection,
    EventRole,
    SourceEventStatus,
)
from aturuang.ingestion_contracts import AccountType, OwnershipState
from aturuang.ingestion_matching import (
    AccountRelationship,
    EconomicEventGroup,
    EvidenceMatchDecision,
    EvidenceMatchPlan,
    MatchingContext,
    MatchReasonCode,
    MatchRelation,
    MatchTier,
    ProtectedAccountBinding,
    RelationshipKind,
    SafeEvidenceRecord,
    compute_evidence_key,
)
from aturuang.ingestion_semantics import (
    SEMANTIC_RULE_COUNT,
    SEMANTIC_RULE_DEFINITIONS,
    SEMANTICS_CONTRACT_VERSION,
    SemanticDecision,
    SemanticInterpretationPlan,
    TransactionSemanticType,
    interpret_evidence_semantics,
    interpret_single_decision,
)


def _make_record(
    doc_id: str,
    reg_id: str,
    role: EventRole = EventRole.CASH_MOVEMENT,
    fingerprint: str = "fp",
    *,
    amount: Decimal = Decimal("100000.00"),
    currency: str = "IDR",
    direction: EventDirection = EventDirection.OUTFLOW,
    status: SourceEventStatus = SourceEventStatus.POSTED,
    event_date: str = "2026-03-01",
    account_binding: ProtectedAccountBinding | None = None,
    is_eligible: bool = True,
    requires_review: bool = False,
    description_raw: str | None = None,
) -> SafeEvidenceRecord:
    if len(fingerprint) != 64 or not all(c in "0123456789abcdef" for c in fingerprint):
        fp_clean = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    else:
        fp_clean = fingerprint
    key = compute_evidence_key(doc_id, reg_id, role, fp_clean)
    return SafeEvidenceRecord(
        evidence_key=key,
        source_document_id=doc_id,
        source_registry_id=reg_id,
        event_role=role,
        row_fingerprint=fp_clean,
        amount=amount,
        currency=currency,
        direction=direction,
        status=status,
        event_date=event_date,
        account_binding=account_binding,
        is_eligible=is_eligible,
        requires_review=requires_review,
        description_raw=description_raw,
    )


class UniversalIngestionPhase5SemanticsTests(unittest.TestCase):
    """Deterministic semantic interpretation tests for Phase 5C."""

    def setUp(self) -> None:
        self.valid_key_1 = "v1:" + "1" * 64
        self.valid_key_2 = "v1:" + "2" * 64
        self.binding_1 = ProtectedAccountBinding(
            protected_account_key=self.valid_key_1,
            institution_id="bca",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        self.binding_2 = ProtectedAccountBinding(
            protected_account_key=self.valid_key_2,
            institution_id="jago",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )

    def test_01_rule_count_matches_explicit_registry(self) -> None:
        self.assertEqual(SEMANTIC_RULE_COUNT, len(SEMANTIC_RULE_DEFINITIONS))
        self.assertEqual(SEMANTIC_RULE_COUNT, 12)
        # Verify each rule has required fields
        for rule in SEMANTIC_RULE_DEFINITIONS:
            self.assertTrue(rule.rule_id.startswith("RULE_"))
            self.assertTrue(len(rule.description) > 0)
            self.assertIsInstance(rule.target_type, TransactionSemanticType)
            self.assertIsInstance(rule.is_confirmed, bool)

    def test_02_same_evidence_produces_identical_deterministic_semantics(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        d1 = EvidenceMatchDecision(
            evidence_key=r1.evidence_key,
            match_tier=MatchTier.UNMATCHED,
            match_relation=None,
            group_key=None,
            reason_codes=(),
            is_auto_link_eligible=False,
        )
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d1,), ())

        sem_plan_1 = interpret_evidence_semantics(plan, [r1])
        sem_plan_2 = interpret_evidence_semantics(plan, [r1])

        self.assertEqual(len(sem_plan_1.decisions), 1)
        self.assertEqual(sem_plan_1.decisions, sem_plan_2.decisions)
        self.assertEqual(sem_plan_1.aggregations, sem_plan_2.aggregations)

    def test_03_internal_transfer_remains_internal_transfer(self) -> None:
        r_out = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp_out", direction=EventDirection.OUTFLOW, account_binding=self.binding_1)
        r_in = _make_record("d2", "jago_statement", EventRole.CASH_MOVEMENT, "fp_in", direction=EventDirection.INFLOW, account_binding=self.binding_2)
        gk = hashlib.sha256(b"transfer_group").hexdigest()
        grp = EconomicEventGroup(
            group_key=gk,
            match_tier=MatchTier.STRONG,
            match_relation=MatchRelation.INTERNAL_TRANSFER_PAIR,
            member_evidence_keys=(r_out.evidence_key, r_in.evidence_key),
            reason_codes=(MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT,),
            is_auto_link_eligible=True,
        )
        d_out = EvidenceMatchDecision(r_out.evidence_key, MatchTier.STRONG, MatchRelation.INTERNAL_TRANSFER_PAIR, gk, (MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT,), True)
        d_in = EvidenceMatchDecision(r_in.evidence_key, MatchTier.STRONG, MatchRelation.INTERNAL_TRANSFER_PAIR, gk, (MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT,), True)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_out, d_in), (grp,))

        sem_plan = interpret_evidence_semantics(plan, [r_out, r_in])

        for dec in sem_plan.decisions:
            self.assertEqual(dec.semantic_type, TransactionSemanticType.INTERNAL_TRANSFER)
            self.assertTrue(dec.is_confirmed)
            self.assertEqual(dec.rule_applied, "RULE_3_INTERNAL_TRANSFER_PAIR")
            self.assertEqual(dec.reason_code, MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT.value)
            # Source evidence keys contain both pair members
            self.assertEqual(set(dec.source_evidence_keys), {r_out.evidence_key, r_in.evidence_key})

    def test_04_expense_classification_requires_valid_evidence_condition(self) -> None:
        # Valid posted cash outflow without transfer relation -> expense
        r_out = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        d_out = EvidenceMatchDecision(r_out.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_out,), ())

        sem_plan = interpret_evidence_semantics(plan, [r_out])
        dec = sem_plan.decisions[0]
        self.assertEqual(dec.semantic_type, TransactionSemanticType.EXPENSE)
        self.assertTrue(dec.is_confirmed)
        self.assertEqual(dec.rule_applied, "RULE_8_UNMATCHED_CASH_OUTFLOW")

    def test_05_income_classification_requires_valid_inflow(self) -> None:
        r_in = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.INFLOW)
        d_in = EvidenceMatchDecision(r_in.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_in,), ())

        sem_plan = interpret_evidence_semantics(plan, [r_in])
        dec = sem_plan.decisions[0]
        self.assertEqual(dec.semantic_type, TransactionSemanticType.INCOME)
        self.assertTrue(dec.is_confirmed)
        self.assertEqual(dec.rule_applied, "RULE_9_UNMATCHED_CASH_INFLOW")

    def test_06_ambiguous_evidence_remains_ambiguous_and_unconfirmed(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        d1 = EvidenceMatchDecision(
            evidence_key=r1.evidence_key,
            match_tier=MatchTier.AMBIGUOUS,
            match_relation=None,
            group_key=None,
            reason_codes=(MatchReasonCode.MULTIPLE_COMPATIBLE_MATCHES,),
            is_auto_link_eligible=False,
        )
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d1,), ())

        sem_plan = interpret_evidence_semantics(plan, [r1])
        dec = sem_plan.decisions[0]

        # Must NOT convert to confirmed expense
        self.assertEqual(dec.semantic_type, TransactionSemanticType.AMBIGUOUS)
        self.assertFalse(dec.is_confirmed)
        self.assertEqual(dec.rule_applied, "RULE_1_AMBIGUOUS_PRESERVED")
        self.assertEqual(dec.reason_code, MatchReasonCode.MULTIPLE_COMPATIBLE_MATCHES.value)

    def test_07_ineligible_and_review_required_remains_unknown_unconfirmed(self) -> None:
        r_rev = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", requires_review=True)
        d_rev = EvidenceMatchDecision(
            evidence_key=r_rev.evidence_key,
            match_tier=MatchTier.INELIGIBLE,
            match_relation=None,
            group_key=None,
            reason_codes=(MatchReasonCode.EVIDENCE_REQUIRES_REVIEW,),
            is_auto_link_eligible=False,
        )
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_rev,), ())

        sem_plan = interpret_evidence_semantics(plan, [r_rev])
        dec = sem_plan.decisions[0]

        self.assertEqual(dec.semantic_type, TransactionSemanticType.UNKNOWN)
        self.assertFalse(dec.is_confirmed)
        self.assertEqual(dec.rule_applied, "RULE_2_REVIEW_REQUIRED_PRESERVED")

    def test_08_unknown_cases_remain_unknown(self) -> None:
        # Non-transactional event roles (e.g. ACCOUNT_OBSERVATION, SOURCE_SUMMARY)
        r_obs = _make_record("d1", "bca_statement", EventRole.ACCOUNT_OBSERVATION, "fp_obs")
        d_obs = EvidenceMatchDecision(r_obs.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_obs,), ())

        sem_plan = interpret_evidence_semantics(plan, [r_obs])
        dec = sem_plan.decisions[0]

        self.assertEqual(dec.semantic_type, TransactionSemanticType.UNKNOWN)
        self.assertFalse(dec.is_confirmed)
        self.assertEqual(dec.rule_applied, "RULE_12_FALLBACK_UNKNOWN")

    def test_09_commerce_payment_classified_as_expense(self) -> None:
        r_cash = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        r_order = _make_record("d2", "shopee_orders", EventRole.COMMERCE_ORDER, "fp2")
        gk = hashlib.sha256(b"commerce_group").hexdigest()
        grp = EconomicEventGroup(
            group_key=gk,
            match_tier=MatchTier.STRONG,
            match_relation=MatchRelation.COMMERCE_PAYMENT,
            member_evidence_keys=(r_cash.evidence_key, r_order.evidence_key),
            reason_codes=(MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION,),
            is_auto_link_eligible=True,
        )
        d_cash = EvidenceMatchDecision(r_cash.evidence_key, MatchTier.STRONG, MatchRelation.COMMERCE_PAYMENT, gk, (MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION,), True)
        d_order = EvidenceMatchDecision(r_order.evidence_key, MatchTier.STRONG, MatchRelation.COMMERCE_PAYMENT, gk, (MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION,), True)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_cash, d_order), (grp,))

        sem_plan = interpret_evidence_semantics(plan, [r_cash, r_order])

        for dec in sem_plan.decisions:
            self.assertEqual(dec.semantic_type, TransactionSemanticType.EXPENSE)
            self.assertTrue(dec.is_confirmed)
            self.assertEqual(dec.rule_applied, "RULE_4_COMMERCE_PAYMENT")

    def test_10_investment_settlement_classified_as_investment_flow(self) -> None:
        r_cash = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        r_trade = _make_record("d2", "stockbit_soa", EventRole.INVESTMENT_TRADE, "fp2")
        gk = hashlib.sha256(b"inv_group").hexdigest()
        grp = EconomicEventGroup(
            group_key=gk,
            match_tier=MatchTier.STRONG,
            match_relation=MatchRelation.INVESTMENT_SETTLEMENT,
            member_evidence_keys=(r_cash.evidence_key, r_trade.evidence_key),
            reason_codes=(MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION,),
            is_auto_link_eligible=True,
        )
        d_cash = EvidenceMatchDecision(r_cash.evidence_key, MatchTier.STRONG, MatchRelation.INVESTMENT_SETTLEMENT, gk, (MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION,), True)
        d_trade = EvidenceMatchDecision(r_trade.evidence_key, MatchTier.STRONG, MatchRelation.INVESTMENT_SETTLEMENT, gk, (MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION,), True)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_cash, d_trade), (grp,))

        sem_plan = interpret_evidence_semantics(plan, [r_cash, r_trade])

        for dec in sem_plan.decisions:
            self.assertEqual(dec.semantic_type, TransactionSemanticType.INVESTMENT_FLOW)
            self.assertTrue(dec.is_confirmed)
            self.assertEqual(dec.rule_applied, "RULE_5_INVESTMENT_SETTLEMENT")

    def test_11_duplicate_evidence_preserves_underlying_direction_semantics(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        r2 = _make_record("d2", "bca_statement", EventRole.CASH_MOVEMENT, "fp2", direction=EventDirection.OUTFLOW)
        gk = hashlib.sha256(b"dup_group").hexdigest()
        grp = EconomicEventGroup(
            group_key=gk,
            match_tier=MatchTier.EXACT,
            match_relation=MatchRelation.DUPLICATE_EVIDENCE,
            member_evidence_keys=(r1.evidence_key, r2.evidence_key),
            reason_codes=(MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID,),
            is_auto_link_eligible=True,
        )
        d1 = EvidenceMatchDecision(r1.evidence_key, MatchTier.EXACT, MatchRelation.DUPLICATE_EVIDENCE, gk, (MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID,), True)
        d2 = EvidenceMatchDecision(r2.evidence_key, MatchTier.EXACT, MatchRelation.DUPLICATE_EVIDENCE, gk, (MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID,), True)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d1, d2), (grp,))

        sem_plan = interpret_evidence_semantics(plan, [r1, r2])
        for dec in sem_plan.decisions:
            self.assertEqual(dec.semantic_type, TransactionSemanticType.EXPENSE)
            self.assertTrue(dec.is_confirmed)
            self.assertEqual(dec.rule_applied, "RULE_6_DUPLICATE_CASH_OUTFLOW")

    def test_12_standalone_trade_and_order_roles(self) -> None:
        r_trade = _make_record("d1", "stockbit_soa", EventRole.INVESTMENT_TRADE, "fp_trade")
        r_order = _make_record("d2", "shopee_orders", EventRole.COMMERCE_ORDER, "fp_order")
        d_trade = EvidenceMatchDecision(r_trade.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        d_order = EvidenceMatchDecision(r_order.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d_trade, d_order), ())

        sem_plan = interpret_evidence_semantics(plan, [r_trade, r_order])
        dec_by_key = {d.evidence_key: d for d in sem_plan.decisions}

        self.assertEqual(dec_by_key[r_trade.evidence_key].semantic_type, TransactionSemanticType.INVESTMENT_FLOW)
        self.assertEqual(dec_by_key[r_order.evidence_key].semantic_type, TransactionSemanticType.EXPENSE)

    def test_13_semantic_output_contains_evidence_references(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1")
        d1 = EvidenceMatchDecision(r1.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d1,), ())

        sem_plan = interpret_evidence_semantics(plan, [r1])
        dec = sem_plan.decisions[0]

        self.assertEqual(dec.evidence_key, r1.evidence_key)
        self.assertIn(r1.evidence_key, dec.source_evidence_keys)

    def test_14_no_raw_private_fields_exposed_in_semantics(self) -> None:
        r1 = _make_record(
            "d1",
            "bca_statement",
            EventRole.CASH_MOVEMENT,
            "fp1",
            amount=Decimal("999999.99"),
            description_raw="CONFIDENTIAL_SALARY_ROW_SHOULD_NOT_LEAK",
        )
        d1 = EvidenceMatchDecision(r1.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d1,), ())

        sem_plan = interpret_evidence_semantics(plan, [r1])
        dec = sem_plan.decisions[0]

        # Convert decision representation to string
        dec_str = repr(dec)
        self.assertNotIn("999999.99", dec_str)
        self.assertNotIn("CONFIDENTIAL_SALARY", dec_str)

    def test_15_aggregations_are_complete_and_accurate(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", direction=EventDirection.OUTFLOW)
        r2 = _make_record("d2", "bca_statement", EventRole.CASH_MOVEMENT, "fp2", direction=EventDirection.INFLOW)
        d1 = EvidenceMatchDecision(r1.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        d2 = EvidenceMatchDecision(r2.evidence_key, MatchTier.UNMATCHED, None, None, (), False)
        plan = EvidenceMatchPlan("truth-loop-matching-v1", (d1, d2), ())

        sem_plan = interpret_evidence_semantics(plan, [r1, r2])
        aggs = sem_plan.aggregations

        self.assertEqual(aggs["total_decisions"], 2)
        self.assertEqual(aggs["confirmed_count"], 2)
        self.assertEqual(aggs["unconfirmed_count"], 0)
        self.assertEqual(aggs["by_semantic_type"]["expense"], 1)
        self.assertEqual(aggs["by_semantic_type"]["income"], 1)


if __name__ == "__main__":
    unittest.main()
