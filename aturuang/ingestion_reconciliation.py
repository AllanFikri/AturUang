"""
Universal Ingestion Phase 5D - Reconciliation Closure and Conservation Layer.

Provides deterministic, pure-Decimal reconciliation contracts and conservation
verification on top of validated evidence, match plans, and semantic interpretations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import hashlib
import re
from typing import Any, Mapping, Sequence

from .ingestion_adapter import EventDirection, EventRole
from .ingestion_matching import (
    EconomicEventGroup,
    EvidenceMatchDecision,
    EvidenceMatchPlan,
    MatchReasonCode,
    MatchRelation,
    MatchTier,
    SafeEvidenceRecord,
)
from .ingestion_semantics import (
    SemanticDecision,
    SemanticInterpretationPlan,
    TransactionSemanticType,
)

RECONCILIATION_CONTRACT_VERSION: str = "truth-loop-reconciliation-v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_BROKER_ROUNDING_TOLERANCE: Decimal = Decimal("0.01")


def _require_decimal(value: Any, field_name: str) -> Decimal:
    """Enforces pure Decimal exact arithmetic and forbids floating-point values."""
    if isinstance(value, float):
        raise TypeError(f"{field_name} must not be float; exact Decimal required")
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite Decimal")
    return value


class ReconciliationStatus(str, Enum):
    """Status of reconciliation check."""

    RECONCILED = "RECONCILED"
    DISCREPANCY_EXPLAINED = "DISCREPANCY_EXPLAINED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReconciliationScope(str, Enum):
    """Scope of reconciliation check."""

    STATEMENT_CONTROL = "STATEMENT_CONTROL"
    INTERNAL_TRANSFER_PAIR = "INTERNAL_TRANSFER_PAIR"
    COMMERCE_PAYMENT = "COMMERCE_PAYMENT"
    INVESTMENT_SETTLEMENT = "INVESTMENT_SETTLEMENT"


@dataclass(frozen=True)
class StatementControlInput:
    """Input parameters for statement-level control balance verification."""

    control_id: str
    opening_balance: Decimal
    closing_balance: Decimal
    total_inflow: Decimal
    total_outflow: Decimal
    currency: str = "IDR"
    participating_evidence_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.control_id, str) or not self.control_id:
            raise ValueError("control_id must be non-empty string")
        object.__setattr__(self, "opening_balance", _require_decimal(self.opening_balance, "opening_balance"))
        object.__setattr__(self, "closing_balance", _require_decimal(self.closing_balance, "closing_balance"))
        object.__setattr__(self, "total_inflow", _require_decimal(self.total_inflow, "total_inflow"))
        object.__setattr__(self, "total_outflow", _require_decimal(self.total_outflow, "total_outflow"))
        if not isinstance(self.currency, str) or not self.currency:
            raise ValueError("currency must be non-empty string")
        if not isinstance(self.participating_evidence_keys, (tuple, list)):
            raise TypeError("participating_evidence_keys must be a sequence")
        sorted_keys = tuple(sorted(self.participating_evidence_keys))
        object.__setattr__(self, "participating_evidence_keys", sorted_keys)


@dataclass(frozen=True)
class ReconciliationRecord:
    """Deterministic, auditable outcome of a reconciliation check."""

    reconciliation_id: str
    scope: ReconciliationScope
    status: ReconciliationStatus
    target_keys: tuple[str, ...]
    expected_delta: Decimal
    actual_delta: Decimal
    discrepancy_amount: Decimal
    reason_code: str
    is_balanced: bool

    def __post_init__(self) -> None:
        if not isinstance(self.reconciliation_id, str) or not _HEX64_RE.fullmatch(self.reconciliation_id):
            raise ValueError("reconciliation_id must be a 64-char hex digest")
        if not isinstance(self.scope, ReconciliationScope):
            raise TypeError("scope must be ReconciliationScope")
        if not isinstance(self.status, ReconciliationStatus):
            raise TypeError("status must be ReconciliationStatus")
        if not isinstance(self.target_keys, (tuple, list)):
            raise TypeError("target_keys must be a sequence")
        sorted_keys = tuple(sorted(self.target_keys))
        object.__setattr__(self, "target_keys", sorted_keys)
        object.__setattr__(self, "expected_delta", _require_decimal(self.expected_delta, "expected_delta"))
        object.__setattr__(self, "actual_delta", _require_decimal(self.actual_delta, "actual_delta"))
        object.__setattr__(self, "discrepancy_amount", _require_decimal(self.discrepancy_amount, "discrepancy_amount"))
        if self.discrepancy_amount < Decimal("0.00"):
            raise ValueError("discrepancy_amount must be non-negative")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("reason_code must be non-empty string")
        if not isinstance(self.is_balanced, bool):
            raise TypeError("is_balanced must be boolean")


@dataclass(frozen=True)
class ReconciliationPlan:
    """Complete collection of deterministic reconciliation records and aggregations."""

    reconciliation_contract_version: str
    records: tuple[ReconciliationRecord, ...]
    aggregations: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.reconciliation_contract_version, str) or not self.reconciliation_contract_version:
            raise ValueError("reconciliation_contract_version must not be empty")
        if not isinstance(self.records, (tuple, list)):
            raise TypeError("records must be a sequence")
        sorted_records = tuple(sorted(self.records, key=lambda r: r.reconciliation_id))
        object.__setattr__(self, "records", sorted_records)


def compute_reconciliation_id(
    scope: ReconciliationScope,
    target_keys: Sequence[str],
    discriminator: str = "",
) -> str:
    """Computes a deterministic 64-character hex SHA-256 reconciliation ID."""
    sorted_keys = sorted(target_keys)
    seed = f"{scope.value}:{discriminator}:{':'.join(sorted_keys)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def reconcile_statement_control(
    input_data: StatementControlInput,
) -> ReconciliationRecord:
    """Verifies statement control balance: opening_balance + total_inflow - total_outflow == closing_balance."""
    if not isinstance(input_data, StatementControlInput):
        raise TypeError("input_data must be StatementControlInput")

    calculated_closing = input_data.opening_balance + input_data.total_inflow - input_data.total_outflow
    expected_delta = Decimal("0.00")
    actual_delta = calculated_closing - input_data.closing_balance
    discrepancy = abs(actual_delta)

    rec_id = compute_reconciliation_id(
        ReconciliationScope.STATEMENT_CONTROL,
        input_data.participating_evidence_keys,
        input_data.control_id,
    )

    if discrepancy == Decimal("0.00"):
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.STATEMENT_CONTROL,
            status=ReconciliationStatus.RECONCILED,
            target_keys=input_data.participating_evidence_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="STATEMENT_EQUATION_BALANCED",
            is_balanced=True,
        )

    return ReconciliationRecord(
        reconciliation_id=rec_id,
        scope=ReconciliationScope.STATEMENT_CONTROL,
        status=ReconciliationStatus.REVIEW_REQUIRED,
        target_keys=input_data.participating_evidence_keys,
        expected_delta=expected_delta,
        actual_delta=actual_delta,
        discrepancy_amount=discrepancy,
        reason_code="STATEMENT_CLOSING_BALANCE_MISMATCH",
        is_balanced=False,
    )


def reconcile_internal_transfer(
    outflow_amount: Decimal,
    inflow_amount: Decimal,
    outflow_key: str,
    inflow_key: str,
    *,
    is_confirmed: bool = True,
    upstream_unresolved: bool = False,
) -> ReconciliationRecord:
    """Reconciles an internal transfer pair: outflow_amount == inflow_amount (net delta == 0)."""
    dec_out = _require_decimal(outflow_amount, "outflow_amount")
    dec_in = _require_decimal(inflow_amount, "inflow_amount")
    target_keys = (outflow_key, inflow_key)
    rec_id = compute_reconciliation_id(ReconciliationScope.INTERNAL_TRANSFER_PAIR, target_keys)

    expected_delta = Decimal("0.00")
    actual_delta = dec_out - dec_in
    discrepancy = abs(actual_delta)

    if upstream_unresolved or not is_confirmed:
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.INTERNAL_TRANSFER_PAIR,
            status=ReconciliationStatus.REVIEW_REQUIRED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="UNRESOLVED_UPSTREAM_SEMANTICS",
            is_balanced=False,
        )

    if discrepancy == Decimal("0.00"):
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.INTERNAL_TRANSFER_PAIR,
            status=ReconciliationStatus.RECONCILED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="INTERNAL_TRANSFER_CONSERVED",
            is_balanced=True,
        )

    return ReconciliationRecord(
        reconciliation_id=rec_id,
        scope=ReconciliationScope.INTERNAL_TRANSFER_PAIR,
        status=ReconciliationStatus.REVIEW_REQUIRED,
        target_keys=target_keys,
        expected_delta=expected_delta,
        actual_delta=actual_delta,
        discrepancy_amount=discrepancy,
        reason_code="INTERNAL_TRANSFER_ASYMMETRY",
        is_balanced=False,
    )


def reconcile_commerce_payment(
    payment_amount: Decimal,
    order_amount: Decimal,
    payment_key: str,
    order_key: str,
    *,
    documented_adjustment: Decimal = Decimal("0.00"),
    is_confirmed: bool = True,
    upstream_unresolved: bool = False,
) -> ReconciliationRecord:
    """Reconciles commerce cash payment against commercial order total."""
    dec_payment = _require_decimal(payment_amount, "payment_amount")
    dec_order = _require_decimal(order_amount, "order_amount")
    dec_adj = _require_decimal(documented_adjustment, "documented_adjustment")

    target_keys = (payment_key, order_key)
    rec_id = compute_reconciliation_id(ReconciliationScope.COMMERCE_PAYMENT, target_keys)

    expected_net_order = dec_order - dec_adj
    expected_delta = Decimal("0.00")
    actual_delta = dec_payment - expected_net_order
    discrepancy = abs(actual_delta)

    if upstream_unresolved or not is_confirmed:
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.COMMERCE_PAYMENT,
            status=ReconciliationStatus.REVIEW_REQUIRED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="UNRESOLVED_UPSTREAM_SEMANTICS",
            is_balanced=False,
        )

    if discrepancy == Decimal("0.00"):
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.COMMERCE_PAYMENT,
            status=ReconciliationStatus.RECONCILED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="COMMERCE_PAYMENT_CONSERVED",
            is_balanced=True,
        )

    return ReconciliationRecord(
        reconciliation_id=rec_id,
        scope=ReconciliationScope.COMMERCE_PAYMENT,
        status=ReconciliationStatus.REVIEW_REQUIRED,
        target_keys=target_keys,
        expected_delta=expected_delta,
        actual_delta=actual_delta,
        discrepancy_amount=discrepancy,
        reason_code="COMMERCE_PAYMENT_AMOUNT_MISMATCH",
        is_balanced=False,
    )


def reconcile_investment_settlement(
    cash_amount: Decimal,
    trade_amount: Decimal,
    cash_key: str,
    trade_key: str,
    *,
    rounding_tolerance: Decimal = DEFAULT_BROKER_ROUNDING_TOLERANCE,
    is_confirmed: bool = True,
    upstream_unresolved: bool = False,
) -> ReconciliationRecord:
    """Reconciles bank cash movement with broker trade settlement candidate."""
    dec_cash = _require_decimal(cash_amount, "cash_amount")
    dec_trade = _require_decimal(trade_amount, "trade_amount")
    dec_tol = _require_decimal(rounding_tolerance, "rounding_tolerance")

    target_keys = (cash_key, trade_key)
    rec_id = compute_reconciliation_id(ReconciliationScope.INVESTMENT_SETTLEMENT, target_keys)

    expected_delta = Decimal("0.00")
    actual_delta = dec_cash - dec_trade
    discrepancy = abs(actual_delta)

    if upstream_unresolved or not is_confirmed:
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.INVESTMENT_SETTLEMENT,
            status=ReconciliationStatus.REVIEW_REQUIRED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="UNRESOLVED_UPSTREAM_SEMANTICS",
            is_balanced=False,
        )

    if discrepancy == Decimal("0.00"):
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.INVESTMENT_SETTLEMENT,
            status=ReconciliationStatus.RECONCILED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="INVESTMENT_SETTLEMENT_CONSERVED",
            is_balanced=True,
        )

    if discrepancy <= dec_tol:
        return ReconciliationRecord(
            reconciliation_id=rec_id,
            scope=ReconciliationScope.INVESTMENT_SETTLEMENT,
            status=ReconciliationStatus.DISCREPANCY_EXPLAINED,
            target_keys=target_keys,
            expected_delta=expected_delta,
            actual_delta=actual_delta,
            discrepancy_amount=discrepancy,
            reason_code="BROKER_ROUNDING_TOLERANCE_EXPLAINED",
            is_balanced=True,
        )

    return ReconciliationRecord(
        reconciliation_id=rec_id,
        scope=ReconciliationScope.INVESTMENT_SETTLEMENT,
        status=ReconciliationStatus.REVIEW_REQUIRED,
        target_keys=target_keys,
        expected_delta=expected_delta,
        actual_delta=actual_delta,
        discrepancy_amount=discrepancy,
        reason_code="INVESTMENT_SETTLEMENT_AMOUNT_MISMATCH",
        is_balanced=False,
    )


def build_reconciliation_plan(
    *,
    match_plan: EvidenceMatchPlan | None = None,
    semantic_plan: SemanticInterpretationPlan | None = None,
    evidence_records: Mapping[str, SafeEvidenceRecord] | Sequence[SafeEvidenceRecord] | None = None,
    statement_controls: Sequence[StatementControlInput] | None = None,
    custom_records: Sequence[ReconciliationRecord] | None = None,
    broker_rounding_tolerance: Decimal = DEFAULT_BROKER_ROUNDING_TOLERANCE,
) -> ReconciliationPlan:
    """Builds a complete, deterministic reconciliation plan across all scopes."""
    records_list: list[ReconciliationRecord] = []

    # 1. Process statement controls
    if statement_controls:
        for ctrl in statement_controls:
            records_list.append(reconcile_statement_control(ctrl))

    # 2. Build index of records by evidence key
    records_by_key: dict[str, SafeEvidenceRecord] = {}
    if evidence_records:
        if isinstance(evidence_records, Mapping):
            records_by_key = dict(evidence_records)
        elif isinstance(evidence_records, Sequence):
            records_by_key = {r.evidence_key: r for r in evidence_records}

    # 3. Build index of semantics by evidence key
    semantics_by_key: dict[str, SemanticDecision] = {}
    if semantic_plan:
        semantics_by_key = {d.evidence_key: d for d in semantic_plan.decisions}

    # 4. Process match plan groups if present
    if match_plan:
        for group in match_plan.groups:
            # Check upstream unresolved
            unresolved = (
                group.match_tier in (MatchTier.AMBIGUOUS, MatchTier.INELIGIBLE)
                or not group.is_auto_link_eligible
            )
            for m_key in group.member_evidence_keys:
                rec = records_by_key.get(m_key)
                if rec and rec.requires_review:
                    unresolved = True
                sem = semantics_by_key.get(m_key)
                if sem and (not sem.is_confirmed or sem.semantic_type in (TransactionSemanticType.AMBIGUOUS, TransactionSemanticType.UNKNOWN)):
                    unresolved = True

            if group.match_relation is MatchRelation.INTERNAL_TRANSFER_PAIR:
                if len(group.member_evidence_keys) == 2:
                    k1, k2 = group.member_evidence_keys
                    r1 = records_by_key.get(k1)
                    r2 = records_by_key.get(k2)
                    if r1 and r2 and r1.amount is not None and r2.amount is not None:
                        outflow_rec = r1 if r1.direction is EventDirection.OUTFLOW else r2
                        inflow_rec = r2 if r1.direction is EventDirection.OUTFLOW else r1
                        records_list.append(
                            reconcile_internal_transfer(
                                outflow_amount=outflow_rec.amount,
                                inflow_amount=inflow_rec.amount,
                                outflow_key=outflow_rec.evidence_key,
                                inflow_key=inflow_rec.evidence_key,
                                is_confirmed=not unresolved,
                                upstream_unresolved=unresolved,
                            )
                        )

            elif group.match_relation is MatchRelation.COMMERCE_PAYMENT:
                if len(group.member_evidence_keys) == 2:
                    k1, k2 = group.member_evidence_keys
                    r1 = records_by_key.get(k1)
                    r2 = records_by_key.get(k2)
                    if r1 and r2 and r1.amount is not None and r2.amount is not None:
                        pay_rec = r1 if r1.event_role is EventRole.CASH_MOVEMENT else r2
                        order_rec = r2 if r1.event_role is EventRole.CASH_MOVEMENT else r1
                        records_list.append(
                            reconcile_commerce_payment(
                                payment_amount=pay_rec.amount,
                                order_amount=order_rec.amount,
                                payment_key=pay_rec.evidence_key,
                                order_key=order_rec.evidence_key,
                                is_confirmed=not unresolved,
                                upstream_unresolved=unresolved,
                            )
                        )

            elif group.match_relation is MatchRelation.INVESTMENT_SETTLEMENT:
                if len(group.member_evidence_keys) == 2:
                    k1, k2 = group.member_evidence_keys
                    r1 = records_by_key.get(k1)
                    r2 = records_by_key.get(k2)
                    if r1 and r2 and r1.amount is not None and r2.amount is not None:
                        cash_rec = r1 if r1.event_role is EventRole.CASH_MOVEMENT else r2
                        trade_rec = r2 if r1.event_role is EventRole.CASH_MOVEMENT else r1
                        records_list.append(
                            reconcile_investment_settlement(
                                cash_amount=cash_rec.amount,
                                trade_amount=trade_rec.amount,
                                cash_key=cash_rec.evidence_key,
                                trade_key=trade_rec.evidence_key,
                                rounding_tolerance=broker_rounding_tolerance,
                                is_confirmed=not unresolved,
                                upstream_unresolved=unresolved,
                            )
                        )

    # 5. Add any custom records
    if custom_records:
        records_list.extend(custom_records)

    # Deterministic sorting
    sorted_records = tuple(sorted(records_list, key=lambda r: r.reconciliation_id))

    # Aggregations
    by_scope: dict[str, int] = {s.value: 0 for s in ReconciliationScope}
    by_status: dict[str, int] = {st.value: 0 for st in ReconciliationStatus}
    total_discrepancy = Decimal("0.00")
    reconciled_cnt = 0
    disc_exp_cnt = 0
    rev_req_cnt = 0

    for rec in sorted_records:
        by_scope[rec.scope.value] += 1
        by_status[rec.status.value] += 1
        total_discrepancy += rec.discrepancy_amount
        if rec.status is ReconciliationStatus.RECONCILED:
            reconciled_cnt += 1
        elif rec.status is ReconciliationStatus.DISCREPANCY_EXPLAINED:
            disc_exp_cnt += 1
        elif rec.status is ReconciliationStatus.REVIEW_REQUIRED:
            rev_req_cnt += 1

    aggregations = {
        "total_records": len(sorted_records),
        "reconciled_count": reconciled_cnt,
        "discrepancy_explained_count": disc_exp_cnt,
        "review_required_count": rev_req_cnt,
        "total_discrepancy_amount": total_discrepancy,
        "is_all_reconciled": (rev_req_cnt == 0 and len(sorted_records) > 0),
        "by_scope": dict(sorted(by_scope.items())),
        "by_status": dict(sorted(by_status.items())),
    }

    return ReconciliationPlan(
        reconciliation_contract_version=RECONCILIATION_CONTRACT_VERSION,
        records=sorted_records,
        aggregations=aggregations,
    )
