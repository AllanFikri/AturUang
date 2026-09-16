"""
Universal Ingestion Phase 5C - Minimal Semantics Layer.

Provides deterministic, explainable, evidence-derived semantic interpretation
on top of validated evidence and match plan outputs without modifying P5A or P5B.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Callable, Mapping, Sequence

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

SEMANTICS_CONTRACT_VERSION: str = "truth-loop-semantics-v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class TransactionSemanticType(str, Enum):
    """Closed enumeration of evidence-derived semantic states."""

    EXPENSE = "expense"
    INCOME = "income"
    INTERNAL_TRANSFER = "internal_transfer"
    INVESTMENT_FLOW = "investment_flow"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SemanticDecision:
    """Deterministic, auditable semantic interpretation of a single evidence decision."""

    evidence_key: str
    semantic_type: TransactionSemanticType
    source_evidence_keys: tuple[str, ...]
    reason_code: str
    rule_applied: str
    is_confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_key, str) or not _HEX64_RE.fullmatch(self.evidence_key):
            raise ValueError("evidence_key must be a 64-char hex digest")
        if not isinstance(self.semantic_type, TransactionSemanticType):
            raise TypeError("semantic_type must be TransactionSemanticType")
        if not isinstance(self.source_evidence_keys, (tuple, list)):
            raise TypeError("source_evidence_keys must be a sequence")
        sorted_keys = tuple(sorted(self.source_evidence_keys))
        object.__setattr__(self, "source_evidence_keys", sorted_keys)
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("reason_code must be non-empty string")
        if not isinstance(self.rule_applied, str) or not self.rule_applied:
            raise ValueError("rule_applied must be non-empty string")
        if not isinstance(self.is_confirmed, bool):
            raise TypeError("is_confirmed must be boolean")


@dataclass(frozen=True)
class SemanticRuleDefinition:
    """Metadata and evaluator definition for a deterministic semantic rule."""

    rule_id: str
    description: str
    target_type: TransactionSemanticType
    default_reason: str
    is_confirmed: bool


@dataclass(frozen=True)
class SemanticInterpretationPlan:
    """Complete semantic interpretation over a batch or match plan."""

    semantic_contract_version: str
    decisions: tuple[SemanticDecision, ...]
    aggregations: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_contract_version, str) or not self.semantic_contract_version:
            raise ValueError("semantic_contract_version must not be empty")
        if not isinstance(self.decisions, (tuple, list)):
            raise TypeError("decisions must be a sequence")
        sorted_decisions = tuple(sorted(self.decisions, key=lambda d: d.evidence_key))
        object.__setattr__(self, "decisions", sorted_decisions)


# Explicit ordered list of deterministic semantic rules
SEMANTIC_RULE_DEFINITIONS: tuple[SemanticRuleDefinition, ...] = (
    SemanticRuleDefinition(
        rule_id="RULE_1_AMBIGUOUS_PRESERVED",
        description="Preserve ambiguous match tier as ambiguous unconfirmed result",
        target_type=TransactionSemanticType.AMBIGUOUS,
        default_reason="AMBIGUOUS_EVIDENCE_UNRESOLVED",
        is_confirmed=False,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_2_REVIEW_REQUIRED_PRESERVED",
        description="Preserve ineligible evidence or review requirement as unknown unconfirmed result",
        target_type=TransactionSemanticType.UNKNOWN,
        default_reason=MatchReasonCode.EVIDENCE_REQUIRES_REVIEW.value,
        is_confirmed=False,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_3_INTERNAL_TRANSFER_PAIR",
        description="Classify confirmed internal transfer pair as internal transfer",
        target_type=TransactionSemanticType.INTERNAL_TRANSFER,
        default_reason=MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT.value,
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_4_COMMERCE_PAYMENT",
        description="Classify corroborated commerce payment correlation as expense",
        target_type=TransactionSemanticType.EXPENSE,
        default_reason=MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION.value,
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_5_INVESTMENT_SETTLEMENT",
        description="Classify corroborated investment broker settlement correlation as investment flow",
        target_type=TransactionSemanticType.INVESTMENT_FLOW,
        default_reason=MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION.value,
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_6_DUPLICATE_CASH_OUTFLOW",
        description="Classify exact duplicate cash outflow evidence as confirmed expense",
        target_type=TransactionSemanticType.EXPENSE,
        default_reason=MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID.value,
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_7_DUPLICATE_CASH_INFLOW",
        description="Classify exact duplicate cash inflow evidence as confirmed income",
        target_type=TransactionSemanticType.INCOME,
        default_reason=MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID.value,
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_8_UNMATCHED_CASH_OUTFLOW",
        description="Classify standalone posted cash outflow as expense",
        target_type=TransactionSemanticType.EXPENSE,
        default_reason="UNMATCHED_CASH_OUTFLOW",
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_9_UNMATCHED_CASH_INFLOW",
        description="Classify standalone posted cash inflow as income",
        target_type=TransactionSemanticType.INCOME,
        default_reason="UNMATCHED_CASH_INFLOW",
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_10_INVESTMENT_TRADE",
        description="Classify investment trade event role as investment flow",
        target_type=TransactionSemanticType.INVESTMENT_FLOW,
        default_reason="STANDALONE_INVESTMENT_TRADE",
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_11_COMMERCE_ORDER",
        description="Classify commerce order event role as expense",
        target_type=TransactionSemanticType.EXPENSE,
        default_reason="STANDALONE_COMMERCE_ORDER",
        is_confirmed=True,
    ),
    SemanticRuleDefinition(
        rule_id="RULE_12_FALLBACK_UNKNOWN",
        description="Fallback classification for unclassified or non-transactional event roles",
        target_type=TransactionSemanticType.UNKNOWN,
        default_reason="NON_TRANSACTIONAL_OR_UNCLASSIFIED_ROLE",
        is_confirmed=False,
    ),
)

SEMANTIC_RULE_COUNT: int = len(SEMANTIC_RULE_DEFINITIONS)


def interpret_single_decision(
    decision: EvidenceMatchDecision,
    record: SafeEvidenceRecord | None,
    group: EconomicEventGroup | None,
) -> SemanticDecision:
    """Applies ordered deterministic rules to interpret a single evidence decision."""
    # Rule 1: Ambiguous preservation
    if decision.match_tier is MatchTier.AMBIGUOUS:
        reason = (
            decision.reason_codes[0].value
            if decision.reason_codes
            else "AMBIGUOUS_EVIDENCE_UNRESOLVED"
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.AMBIGUOUS,
            source_evidence_keys=(decision.evidence_key,),
            reason_code=reason,
            rule_applied="RULE_1_AMBIGUOUS_PRESERVED",
            is_confirmed=False,
        )

    # Rule 2: Ineligible or review required preservation
    if decision.match_tier is MatchTier.INELIGIBLE or (record is not None and record.requires_review):
        reason = (
            decision.reason_codes[0].value
            if decision.reason_codes
            else MatchReasonCode.EVIDENCE_REQUIRES_REVIEW.value
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.UNKNOWN,
            source_evidence_keys=(decision.evidence_key,),
            reason_code=reason,
            rule_applied="RULE_2_REVIEW_REQUIRED_PRESERVED",
            is_confirmed=False,
        )

    # Rule 3: Confirmed internal transfer pair
    if decision.match_relation is MatchRelation.INTERNAL_TRANSFER_PAIR:
        source_keys = group.member_evidence_keys if group else (decision.evidence_key,)
        reason = (
            group.reason_codes[0].value
            if (group and group.reason_codes)
            else MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT.value
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.INTERNAL_TRANSFER,
            source_evidence_keys=source_keys,
            reason_code=reason,
            rule_applied="RULE_3_INTERNAL_TRANSFER_PAIR",
            is_confirmed=True,
        )

    # Rule 4: Corroborated commerce payment
    if decision.match_relation is MatchRelation.COMMERCE_PAYMENT:
        source_keys = group.member_evidence_keys if group else (decision.evidence_key,)
        reason = (
            group.reason_codes[0].value
            if (group and group.reason_codes)
            else MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION.value
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.EXPENSE,
            source_evidence_keys=source_keys,
            reason_code=reason,
            rule_applied="RULE_4_COMMERCE_PAYMENT",
            is_confirmed=True,
        )

    # Rule 5: Corroborated investment broker settlement
    if decision.match_relation is MatchRelation.INVESTMENT_SETTLEMENT:
        source_keys = group.member_evidence_keys if group else (decision.evidence_key,)
        reason = (
            group.reason_codes[0].value
            if (group and group.reason_codes)
            else MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION.value
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.INVESTMENT_FLOW,
            source_evidence_keys=source_keys,
            reason_code=reason,
            rule_applied="RULE_5_INVESTMENT_SETTLEMENT",
            is_confirmed=True,
        )

    # Rule 6: Duplicate evidence on cash outflow
    if (
        decision.match_relation is MatchRelation.DUPLICATE_EVIDENCE
        and record is not None
        and record.event_role is EventRole.CASH_MOVEMENT
        and record.direction is EventDirection.OUTFLOW
    ):
        source_keys = group.member_evidence_keys if group else (decision.evidence_key,)
        reason = (
            group.reason_codes[0].value
            if (group and group.reason_codes)
            else MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID.value
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.EXPENSE,
            source_evidence_keys=source_keys,
            reason_code=reason,
            rule_applied="RULE_6_DUPLICATE_CASH_OUTFLOW",
            is_confirmed=True,
        )

    # Rule 7: Duplicate evidence on cash inflow
    if (
        decision.match_relation is MatchRelation.DUPLICATE_EVIDENCE
        and record is not None
        and record.event_role is EventRole.CASH_MOVEMENT
        and record.direction is EventDirection.INFLOW
    ):
        source_keys = group.member_evidence_keys if group else (decision.evidence_key,)
        reason = (
            group.reason_codes[0].value
            if (group and group.reason_codes)
            else MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID.value
        )
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.INCOME,
            source_evidence_keys=source_keys,
            reason_code=reason,
            rule_applied="RULE_7_DUPLICATE_CASH_INFLOW",
            is_confirmed=True,
        )

    # Rule 8: Standalone posted cash outflow
    if (
        record is not None
        and record.event_role is EventRole.CASH_MOVEMENT
        and record.direction is EventDirection.OUTFLOW
    ):
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.EXPENSE,
            source_evidence_keys=(decision.evidence_key,),
            reason_code="UNMATCHED_CASH_OUTFLOW",
            rule_applied="RULE_8_UNMATCHED_CASH_OUTFLOW",
            is_confirmed=True,
        )

    # Rule 9: Standalone posted cash inflow
    if (
        record is not None
        and record.event_role is EventRole.CASH_MOVEMENT
        and record.direction is EventDirection.INFLOW
    ):
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.INCOME,
            source_evidence_keys=(decision.evidence_key,),
            reason_code="UNMATCHED_CASH_INFLOW",
            rule_applied="RULE_9_UNMATCHED_CASH_INFLOW",
            is_confirmed=True,
        )

    # Rule 10: Standalone investment trade
    if record is not None and record.event_role is EventRole.INVESTMENT_TRADE:
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.INVESTMENT_FLOW,
            source_evidence_keys=(decision.evidence_key,),
            reason_code="STANDALONE_INVESTMENT_TRADE",
            rule_applied="RULE_10_INVESTMENT_TRADE",
            is_confirmed=True,
        )

    # Rule 11: Standalone commerce order
    if record is not None and record.event_role is EventRole.COMMERCE_ORDER:
        return SemanticDecision(
            evidence_key=decision.evidence_key,
            semantic_type=TransactionSemanticType.EXPENSE,
            source_evidence_keys=(decision.evidence_key,),
            reason_code="STANDALONE_COMMERCE_ORDER",
            rule_applied="RULE_11_COMMERCE_ORDER",
            is_confirmed=True,
        )

    # Rule 12: Fallback unknown (non-transactional role or unclassified)
    return SemanticDecision(
        evidence_key=decision.evidence_key,
        semantic_type=TransactionSemanticType.UNKNOWN,
        source_evidence_keys=(decision.evidence_key,),
        reason_code="NON_TRANSACTIONAL_OR_UNCLASSIFIED_ROLE",
        rule_applied="RULE_12_FALLBACK_UNKNOWN",
        is_confirmed=False,
    )


def interpret_evidence_semantics(
    match_plan: EvidenceMatchPlan,
    evidence_records: Mapping[str, SafeEvidenceRecord] | Sequence[SafeEvidenceRecord],
) -> SemanticInterpretationPlan:
    """Interprets validated match plan and evidence records into deterministic semantics."""
    if not isinstance(match_plan, EvidenceMatchPlan):
        raise TypeError("match_plan must be EvidenceMatchPlan")

    if isinstance(evidence_records, Mapping):
        records_by_key = evidence_records
    elif isinstance(evidence_records, Sequence):
        records_by_key = {r.evidence_key: r for r in evidence_records}
    else:
        raise TypeError("evidence_records must be Mapping or Sequence of SafeEvidenceRecord")

    groups_by_key = {g.group_key: g for g in match_plan.groups}
    decisions: list[SemanticDecision] = []

    by_semantic_type: dict[str, int] = {st.value: 0 for st in TransactionSemanticType}
    by_rule_applied: dict[str, int] = {}
    confirmed_count = 0
    unconfirmed_count = 0

    for d in sorted(match_plan.decisions, key=lambda item: item.evidence_key):
        rec = records_by_key.get(d.evidence_key)
        grp = groups_by_key.get(d.group_key) if d.group_key else None

        sem_dec = interpret_single_decision(d, rec, grp)
        decisions.append(sem_dec)

        by_semantic_type[sem_dec.semantic_type.value] += 1
        by_rule_applied[sem_dec.rule_applied] = by_rule_applied.get(sem_dec.rule_applied, 0) + 1
        if sem_dec.is_confirmed:
            confirmed_count += 1
        else:
            unconfirmed_count += 1

    aggregations = {
        "total_decisions": len(decisions),
        "confirmed_count": confirmed_count,
        "unconfirmed_count": unconfirmed_count,
        "by_semantic_type": dict(sorted(by_semantic_type.items())),
        "by_rule_applied": dict(sorted(by_rule_applied.items())),
    }

    return SemanticInterpretationPlan(
        semantic_contract_version=SEMANTICS_CONTRACT_VERSION,
        decisions=tuple(decisions),
        aggregations=aggregations,
    )
