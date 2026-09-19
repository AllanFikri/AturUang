"""
Universal Ingestion Phase 5D - Reconciliation Closure and Conservation Layer Tests.

Verifies deterministic, pure-Decimal reconciliation contracts, conservation
verification, ambiguity preservation, and zero data mutation.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path
import unittest

from aturuang.ingestion_adapter import (
    ConfidenceLevel,
    EventDirection,
    EventRole,
    SourceEventStatus,
)
from aturuang.ingestion_contracts import AccountType, OwnershipState
from aturuang.ingestion_matching import (
    EconomicEventGroup,
    EvidenceMatchDecision,
    EvidenceMatchPlan,
    MatchReasonCode,
    MatchRelation,
    MatchTier,
    ProtectedAccountBinding,
    SafeEvidenceRecord,
    compute_evidence_key,
)
from aturuang.ingestion_reconciliation import (
    DEFAULT_BROKER_ROUNDING_TOLERANCE,
    RECONCILIATION_CONTRACT_VERSION,
    ReconciliationPlan,
    ReconciliationRecord,
    ReconciliationScope,
    ReconciliationStatus,
    StatementControlInput,
    _require_decimal,
    build_reconciliation_plan,
    compute_reconciliation_id,
    reconcile_commerce_payment,
    reconcile_internal_transfer,
    reconcile_investment_settlement,
    reconcile_statement_control,
)
from aturuang.ingestion_semantics import (
    SEMANTICS_CONTRACT_VERSION,
    SemanticDecision,
    SemanticInterpretationPlan,
    TransactionSemanticType,
)

EXPECTED_PRODUCTION_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"


def _make_evidence_record(
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


def _find_production_db() -> Path:
    candidates = [
        Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db"),
        Path(__file__).resolve().parent.parent.parent / "AturUang" / "runtime" / "money_tracks.db",
        Path(__file__).resolve().parent.parent / "runtime" / "money_tracks.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Production database not found")


class UniversalIngestionPhase5ReconciliationTests(unittest.TestCase):
    """Deterministic reconciliation and conservation unit tests for Phase 5D."""

    def test_exact_statement_balance_reconciles_successfully(self) -> None:
        ctrl = StatementControlInput(
            control_id="CTRL_STMT_001",
            opening_balance=Decimal("1000000.00"),
            closing_balance=Decimal("1250000.00"),
            total_inflow=Decimal("500000.00"),
            total_outflow=Decimal("250000.00"),
            participating_evidence_keys=("KEY_A", "KEY_B"),
        )
        rec = reconcile_statement_control(ctrl)
        self.assertEqual(rec.scope, ReconciliationScope.STATEMENT_CONTROL)
        self.assertEqual(rec.status, ReconciliationStatus.RECONCILED)
        self.assertTrue(rec.is_balanced)
        self.assertEqual(rec.expected_delta, Decimal("0.00"))
        self.assertEqual(rec.actual_delta, Decimal("0.00"))
        self.assertEqual(rec.discrepancy_amount, Decimal("0.00"))
        self.assertEqual(rec.reason_code, "STATEMENT_EQUATION_BALANCED")
        self.assertEqual(rec.target_keys, ("KEY_A", "KEY_B"))

    def test_statement_balance_mismatch_fails_closed_to_review(self) -> None:
        ctrl = StatementControlInput(
            control_id="CTRL_STMT_002",
            opening_balance=Decimal("1000000.00"),
            closing_balance=Decimal("1300000.00"),  # Expected: 1,250,000.00; Delta: -50,000.00
            total_inflow=Decimal("500000.00"),
            total_outflow=Decimal("250000.00"),
            participating_evidence_keys=("KEY_A", "KEY_B"),
        )
        rec = reconcile_statement_control(ctrl)
        self.assertEqual(rec.scope, ReconciliationScope.STATEMENT_CONTROL)
        self.assertEqual(rec.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertFalse(rec.is_balanced)
        self.assertEqual(rec.expected_delta, Decimal("0.00"))
        self.assertEqual(rec.actual_delta, Decimal("-50000.00"))
        self.assertEqual(rec.discrepancy_amount, Decimal("50000.00"))
        self.assertEqual(rec.reason_code, "STATEMENT_CLOSING_BALANCE_MISMATCH")

    def test_internal_transfer_pair_conservation_net_zero(self) -> None:
        rec = reconcile_internal_transfer(
            outflow_amount=Decimal("500000.00"),
            inflow_amount=Decimal("500000.00"),
            outflow_key="KEY_TX_OUT_01",
            inflow_key="KEY_TX_IN_01",
        )
        self.assertEqual(rec.scope, ReconciliationScope.INTERNAL_TRANSFER_PAIR)
        self.assertEqual(rec.status, ReconciliationStatus.RECONCILED)
        self.assertTrue(rec.is_balanced)
        self.assertEqual(rec.expected_delta, Decimal("0.00"))
        self.assertEqual(rec.actual_delta, Decimal("0.00"))
        self.assertEqual(rec.discrepancy_amount, Decimal("0.00"))
        self.assertEqual(rec.reason_code, "INTERNAL_TRANSFER_CONSERVED")
        self.assertEqual(rec.target_keys, ("KEY_TX_IN_01", "KEY_TX_OUT_01"))

    def test_internal_transfer_pair_asymmetry_detected(self) -> None:
        rec = reconcile_internal_transfer(
            outflow_amount=Decimal("500000.00"),
            inflow_amount=Decimal("495000.00"),
            outflow_key="KEY_TX_OUT_01",
            inflow_key="KEY_TX_IN_01",
        )
        self.assertEqual(rec.scope, ReconciliationScope.INTERNAL_TRANSFER_PAIR)
        self.assertEqual(rec.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertFalse(rec.is_balanced)
        self.assertEqual(rec.expected_delta, Decimal("0.00"))
        self.assertEqual(rec.actual_delta, Decimal("5000.00"))
        self.assertEqual(rec.discrepancy_amount, Decimal("5000.00"))
        self.assertEqual(rec.reason_code, "INTERNAL_TRANSFER_ASYMMETRY")

    def test_commerce_payment_matches_order_total(self) -> None:
        rec = reconcile_commerce_payment(
            payment_amount=Decimal("150000.00"),
            order_amount=Decimal("170000.00"),
            documented_adjustment=Decimal("20000.00"),
            payment_key="KEY_PAY_01",
            order_key="KEY_ORD_01",
        )
        self.assertEqual(rec.scope, ReconciliationScope.COMMERCE_PAYMENT)
        self.assertEqual(rec.status, ReconciliationStatus.RECONCILED)
        self.assertTrue(rec.is_balanced)
        self.assertEqual(rec.expected_delta, Decimal("0.00"))
        self.assertEqual(rec.actual_delta, Decimal("0.00"))
        self.assertEqual(rec.discrepancy_amount, Decimal("0.00"))
        self.assertEqual(rec.reason_code, "COMMERCE_PAYMENT_CONSERVED")

    def test_commerce_payment_partial_or_mismatched_amount(self) -> None:
        rec = reconcile_commerce_payment(
            payment_amount=Decimal("140000.00"),
            order_amount=Decimal("170000.00"),
            documented_adjustment=Decimal("20000.00"),  # Expected: 150000.00; Actual: 140000.00
            payment_key="KEY_PAY_01",
            order_key="KEY_ORD_01",
        )
        self.assertEqual(rec.scope, ReconciliationScope.COMMERCE_PAYMENT)
        self.assertEqual(rec.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertFalse(rec.is_balanced)
        self.assertEqual(rec.expected_delta, Decimal("0.00"))
        self.assertEqual(rec.actual_delta, Decimal("-10000.00"))
        self.assertEqual(rec.discrepancy_amount, Decimal("10000.00"))
        self.assertEqual(rec.reason_code, "COMMERCE_PAYMENT_AMOUNT_MISMATCH")

    def test_investment_settlement_reconciles_exact_trade_cash(self) -> None:
        rec = reconcile_investment_settlement(
            cash_amount=Decimal("2500000.00"),
            trade_amount=Decimal("2500000.00"),
            cash_key="KEY_CASH_01",
            trade_key="KEY_TRADE_01",
        )
        self.assertEqual(rec.scope, ReconciliationScope.INVESTMENT_SETTLEMENT)
        self.assertEqual(rec.status, ReconciliationStatus.RECONCILED)
        self.assertTrue(rec.is_balanced)
        self.assertEqual(rec.discrepancy_amount, Decimal("0.00"))
        self.assertEqual(rec.reason_code, "INVESTMENT_SETTLEMENT_CONSERVED")

    def test_broker_rounding_difference_handled_as_explained_discrepancy(self) -> None:
        # Tolerated rounding within tolerance: 0.01
        rec = reconcile_investment_settlement(
            cash_amount=Decimal("2500000.00"),
            trade_amount=Decimal("2500000.01"),
            cash_key="KEY_CASH_01",
            trade_key="KEY_TRADE_01",
            rounding_tolerance=Decimal("0.01"),
        )
        self.assertEqual(rec.scope, ReconciliationScope.INVESTMENT_SETTLEMENT)
        self.assertEqual(rec.status, ReconciliationStatus.DISCREPANCY_EXPLAINED)
        self.assertTrue(rec.is_balanced)
        self.assertEqual(rec.discrepancy_amount, Decimal("0.01"))
        self.assertEqual(rec.reason_code, "BROKER_ROUNDING_TOLERANCE_EXPLAINED")

        # Exceeding tolerance: 0.02 > 0.01 -> REVIEW_REQUIRED
        rec_excess = reconcile_investment_settlement(
            cash_amount=Decimal("2500000.00"),
            trade_amount=Decimal("2500000.02"),
            cash_key="KEY_CASH_01",
            trade_key="KEY_TRADE_01",
            rounding_tolerance=Decimal("0.01"),
        )
        self.assertEqual(rec_excess.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertFalse(rec_excess.is_balanced)
        self.assertEqual(rec_excess.discrepancy_amount, Decimal("0.02"))
        self.assertEqual(rec_excess.reason_code, "INVESTMENT_SETTLEMENT_AMOUNT_MISMATCH")

    def test_upstream_ambiguous_evidence_forces_review_required(self) -> None:
        # Direct function call with upstream_unresolved=True
        rec = reconcile_internal_transfer(
            outflow_amount=Decimal("100000.00"),
            inflow_amount=Decimal("100000.00"),
            outflow_key="KEY_TX_OUT_01",
            inflow_key="KEY_TX_IN_01",
            upstream_unresolved=True,
        )
        self.assertEqual(rec.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertFalse(rec.is_balanced)
        self.assertEqual(rec.reason_code, "UNRESOLVED_UPSTREAM_SEMANTICS")

        # Via build_reconciliation_plan with match_tier=AMBIGUOUS
        rec1 = _make_evidence_record("DOC1", "REG1", EventRole.CASH_MOVEMENT, "fp1", amount=Decimal("100000.00"), direction=EventDirection.OUTFLOW)
        rec2 = _make_evidence_record("DOC2", "REG2", EventRole.CASH_MOVEMENT, "fp2", amount=Decimal("100000.00"), direction=EventDirection.INFLOW)
        group = EconomicEventGroup(
            group_key=hashlib.sha256(b"GRP_AMBIGUOUS_01").hexdigest(),
            match_tier=MatchTier.AMBIGUOUS,
            match_relation=MatchRelation.INTERNAL_TRANSFER_PAIR,
            member_evidence_keys=(rec1.evidence_key, rec2.evidence_key),
            reason_codes=(MatchReasonCode.MULTIPLE_COMPATIBLE_MATCHES,),
            is_auto_link_eligible=False,
        )
        match_plan = EvidenceMatchPlan(
            matcher_contract_version="truth-loop-matching-v1",
            decisions=(),
            groups=(group,),
        )
        plan = build_reconciliation_plan(
            match_plan=match_plan,
            evidence_records=(rec1, rec2),
        )
        self.assertEqual(len(plan.records), 1)
        r = plan.records[0]
        self.assertEqual(r.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertEqual(r.reason_code, "UNRESOLVED_UPSTREAM_SEMANTICS")
        self.assertFalse(r.is_balanced)

    def test_upstream_unknown_semantics_forces_review_required(self) -> None:
        rec1 = _make_evidence_record("DOC1", "REG1", EventRole.CASH_MOVEMENT, "fp1", amount=Decimal("100000.00"), direction=EventDirection.OUTFLOW)
        rec2 = _make_evidence_record("DOC2", "REG2", EventRole.CASH_MOVEMENT, "fp2", amount=Decimal("100000.00"), direction=EventDirection.INFLOW)
        group = EconomicEventGroup(
            group_key=hashlib.sha256(b"GRP_CONFIRMED_01").hexdigest(),
            match_tier=MatchTier.EXACT,
            match_relation=MatchRelation.INTERNAL_TRANSFER_PAIR,
            member_evidence_keys=(rec1.evidence_key, rec2.evidence_key),
            reason_codes=(MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT,),
            is_auto_link_eligible=True,
        )
        match_plan = EvidenceMatchPlan(
            matcher_contract_version="truth-loop-matching-v1",
            decisions=(),
            groups=(group,),
        )
        # Semantic decision for rec1 is UNKNOWN and unconfirmed
        sem_dec = SemanticDecision(
            evidence_key=rec1.evidence_key,
            semantic_type=TransactionSemanticType.UNKNOWN,
            source_evidence_keys=(rec1.evidence_key,),
            reason_code="UNKNOWN_PATTERN",
            rule_applied="RULE_FALLBACK",
            is_confirmed=False,
        )
        sem_plan = SemanticInterpretationPlan(
            semantic_contract_version=SEMANTICS_CONTRACT_VERSION,
            decisions=(sem_dec,),
            aggregations={},
        )
        plan = build_reconciliation_plan(
            match_plan=match_plan,
            semantic_plan=sem_plan,
            evidence_records=(rec1, rec2),
        )
        self.assertEqual(len(plan.records), 1)
        r = plan.records[0]
        self.assertEqual(r.status, ReconciliationStatus.REVIEW_REQUIRED)
        self.assertEqual(r.reason_code, "UNRESOLVED_UPSTREAM_SEMANTICS")
        self.assertFalse(r.is_balanced)

    def test_decimal_exactness_prevents_float_leakage(self) -> None:
        # Float inputs must raise TypeError
        with self.assertRaises(TypeError):
            _require_decimal(100.0, "amount")
        with self.assertRaises(TypeError):
            _require_decimal(100, "amount")  # int is not Decimal
        with self.assertRaises(TypeError):
            reconcile_internal_transfer(
                outflow_amount=1000.0,  # type: ignore
                inflow_amount=Decimal("1000.00"),
                outflow_key="KEY_A",
                inflow_key="KEY_B",
            )
        with self.assertRaises(TypeError):
            StatementControlInput(
                control_id="CTRL_FLOAT",
                opening_balance=100.0,  # type: ignore
                closing_balance=Decimal("100.00"),
                total_inflow=Decimal("0.00"),
                total_outflow=Decimal("0.00"),
            )
        # Non-finite decimal must raise ValueError
        with self.assertRaises(ValueError):
            _require_decimal(Decimal("NaN"), "amount")
        with self.assertRaises(ValueError):
            _require_decimal(Decimal("Infinity"), "amount")

        # Verify all deltas in records are Decimal
        rec = reconcile_statement_control(
            StatementControlInput(
                control_id="CTRL_STMT_DEC",
                opening_balance=Decimal("100.00"),
                closing_balance=Decimal("100.00"),
                total_inflow=Decimal("0.00"),
                total_outflow=Decimal("0.00"),
            )
        )
        self.assertIsInstance(rec.expected_delta, Decimal)
        self.assertIsInstance(rec.actual_delta, Decimal)
        self.assertIsInstance(rec.discrepancy_amount, Decimal)

    def test_deterministic_reconciliation_plan_ordering(self) -> None:
        rec1 = reconcile_internal_transfer(Decimal("10.00"), Decimal("10.00"), "K1", "K2")
        rec2 = reconcile_commerce_payment(Decimal("20.00"), Decimal("20.00"), "K3", "K4")
        rec3 = reconcile_investment_settlement(Decimal("30.00"), Decimal("30.00"), "K5", "K6")

        # Test both insertion orders produce strictly identical records order
        plan_a = ReconciliationPlan(
            reconciliation_contract_version=RECONCILIATION_CONTRACT_VERSION,
            records=(rec1, rec2, rec3),
            aggregations={},
        )
        plan_b = ReconciliationPlan(
            reconciliation_contract_version=RECONCILIATION_CONTRACT_VERSION,
            records=(rec3, rec1, rec2),
            aggregations={},
        )
        plan_c = ReconciliationPlan(
            reconciliation_contract_version=RECONCILIATION_CONTRACT_VERSION,
            records=(rec2, rec3, rec1),
            aggregations={},
        )
        self.assertEqual(
            [r.reconciliation_id for r in plan_a.records],
            [r.reconciliation_id for r in plan_b.records],
        )
        self.assertEqual(
            [r.reconciliation_id for r in plan_b.records],
            [r.reconciliation_id for r in plan_c.records],
        )
        # Ensure it is sorted strictly ascending
        ids = [r.reconciliation_id for r in plan_a.records]
        self.assertEqual(ids, sorted(ids))

    def test_no_source_amounts_mutated(self) -> None:
        amount_orig = Decimal("123456.78")
        rec1 = _make_evidence_record("DOC1", "REG1", amount=amount_orig)
        rec2 = _make_evidence_record("DOC2", "REG2", amount=amount_orig)

        reconcile_internal_transfer(
            outflow_amount=rec1.amount,
            inflow_amount=rec2.amount,
            outflow_key=rec1.evidence_key,
            inflow_key=rec2.evidence_key,
        )
        # Verify inputs are untouched
        self.assertEqual(rec1.amount, amount_orig)
        self.assertEqual(rec2.amount, amount_orig)

        # In statement control
        inflow_orig = Decimal("50000.00")
        ctrl = StatementControlInput(
            control_id="CTRL_01",
            opening_balance=Decimal("10000.00"),
            closing_balance=Decimal("60000.00"),
            total_inflow=inflow_orig,
            total_outflow=Decimal("0.00"),
        )
        reconcile_statement_control(ctrl)
        self.assertEqual(ctrl.total_inflow, inflow_orig)

    def test_multiple_statements_in_batch_reconcile_independently(self) -> None:
        ctrl1 = StatementControlInput(
            control_id="CTRL_ACC_01",
            opening_balance=Decimal("100.00"),
            closing_balance=Decimal("150.00"),
            total_inflow=Decimal("50.00"),
            total_outflow=Decimal("0.00"),
        )
        ctrl2 = StatementControlInput(
            control_id="CTRL_ACC_02",
            opening_balance=Decimal("200.00"),
            closing_balance=Decimal("200.00"),
            total_inflow=Decimal("50.00"),
            total_outflow=Decimal("0.00"),  # Mismatch: actual closing should be 250
        )
        plan = build_reconciliation_plan(statement_controls=(ctrl1, ctrl2))
        self.assertEqual(len(plan.records), 2)
        status_map = {r.target_keys: r.status for r in plan.records}
        self.assertEqual(plan.aggregations["reconciled_count"], 1)
        self.assertEqual(plan.aggregations["review_required_count"], 1)
        self.assertFalse(plan.aggregations["is_all_reconciled"])

    def test_no_private_fields_exposed_in_reconciliation_records(self) -> None:
        rec = reconcile_internal_transfer(
            outflow_amount=Decimal("100000.00"),
            inflow_amount=Decimal("100000.00"),
            outflow_key="KEY_SYNTHETIC_A",
            inflow_key="KEY_SYNTHETIC_B",
        )
        record_dict = rec.__dict__
        # Check forbidden private terms
        forbidden = ("account_number", "owner_name", "merchant", "notes", "description_raw", "path")
        for f in forbidden:
            self.assertNotIn(f, record_dict)
            self.assertNotIn(f, repr(rec))

        # Check fields match the contract exactly
        expected_fields = {
            "reconciliation_id",
            "scope",
            "status",
            "target_keys",
            "expected_delta",
            "actual_delta",
            "discrepancy_amount",
            "reason_code",
            "is_balanced",
        }
        self.assertEqual(set(record_dict.keys()), expected_fields)

    def test_production_db_remains_unmodified_and_unaccessed(self) -> None:
        db_path = _find_production_db()
        self.assertTrue(db_path.exists())

        # Pre-execution SHA-256
        pre_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertEqual(pre_hash, EXPECTED_PRODUCTION_DB_SHA256)

        # Run reconciliation operations
        ctrl = StatementControlInput(
            control_id="CTRL_STMT_SAFETY",
            opening_balance=Decimal("1000.00"),
            closing_balance=Decimal("1000.00"),
            total_inflow=Decimal("0.00"),
            total_outflow=Decimal("0.00"),
        )
        plan = build_reconciliation_plan(statement_controls=(ctrl,))
        self.assertEqual(len(plan.records), 1)

        # Post-execution SHA-256
        post_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertEqual(post_hash, EXPECTED_PRODUCTION_DB_SHA256)
        self.assertEqual(pre_hash, post_hash)


if __name__ == "__main__":
    unittest.main()
