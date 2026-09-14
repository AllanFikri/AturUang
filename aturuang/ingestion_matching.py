"""
Truth Loop Core - Evidence Matching Engine (P5A-R0).

Provides deterministic, read-only evidence matching that correlates
normalized evidence without merging, modifying, or deleting original records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .ingestion_adapter import (
    AdapterContractError,
    ConfidenceLevel,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    NormalizedEventEnvelope,
    SafeDiagnostic,
    SourceEventStatus,
)
from .ingestion_contracts import (
    AccountType,
    LifecycleState,
    OwnershipState,
)
from .ingestion_account_discovery import (
    AccountDiscoveryPlan,
    AccountResolution,
)


MATCHER_CONTRACT_VERSION: str = "truth-loop-matching-v1"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MatchTier(str, Enum):
    EXACT = "EXACT"
    STRONG = "STRONG"
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"
    INELIGIBLE = "INELIGIBLE"


class MatchRelation(str, Enum):
    DUPLICATE_EVIDENCE = "DUPLICATE_EVIDENCE"
    INTERNAL_TRANSFER_PAIR = "INTERNAL_TRANSFER_PAIR"
    COMMERCE_PAYMENT = "COMMERCE_PAYMENT"
    INVESTMENT_SETTLEMENT = "INVESTMENT_SETTLEMENT"


class MatchReasonCode(str, Enum):
    SAME_SOURCE_EVENT_ID = "SAME_SOURCE_EVENT_ID"
    SAME_PROVIDER_TRANSACTION_ID = "SAME_PROVIDER_TRANSACTION_ID"
    SAME_DOCUMENT_REFERENCE = "SAME_DOCUMENT_REFERENCE"
    OPPOSITE_OWNED_CASH_MOVEMENT = "OPPOSITE_OWNED_CASH_MOVEMENT"
    COMMERCE_PAYMENT_CORROBORATION = "COMMERCE_PAYMENT_CORROBORATION"
    INVESTMENT_SETTLEMENT_CORROBORATION = "INVESTMENT_SETTLEMENT_CORROBORATION"
    MULTIPLE_COMPATIBLE_MATCHES = "MULTIPLE_COMPATIBLE_MATCHES"
    CONFLICTING_EXACT_IDENTITY = "CONFLICTING_EXACT_IDENTITY"
    MISSING_ACCOUNT_BINDING = "MISSING_ACCOUNT_BINDING"
    INVALID_ACCOUNT_BINDING = "INVALID_ACCOUNT_BINDING"
    MISSING_REQUIRED_DATE = "MISSING_REQUIRED_DATE"
    INVALID_DATE = "INVALID_DATE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
    STATUS_NOT_ELIGIBLE = "STATUS_NOT_ELIGIBLE"
    EVIDENCE_REQUIRES_REVIEW = "EVIDENCE_REQUIRES_REVIEW"
    UNSUPPORTED_ROLE_PAIR = "UNSUPPORTED_ROLE_PAIR"
    MATCHER_CONTRACT_FAILURE = "MATCHER_CONTRACT_FAILURE"


def _require_finite_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{field_name} must not be float; exact Decimal required")
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite Decimal")
    return value


def _optional_finite_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _require_finite_decimal(value, field_name)


def _parse_iso_date(val: str | None) -> date | None:
    if not val or not isinstance(val, str):
        return None
    d_str = val[:10]
    if not _DATE_RE.fullmatch(d_str):
        return None
    try:
        return date.fromisoformat(d_str)
    except ValueError:
        return None


def calendar_days_between(d1: date, d2: date) -> int:
    return abs((d1 - d2).days)


def compute_evidence_key(
    source_document_id: str,
    source_registry_id: str,
    event_role: EventRole | str,
    row_fingerprint: str,
) -> str:
    role_str = event_role.value if isinstance(event_role, EventRole) else str(event_role)
    payload = f"{source_document_id}:{source_registry_id}:{role_str}:{row_fingerprint}"
    return sha256(payload.encode("utf-8")).hexdigest()


def compute_group_key(
    match_relation: MatchRelation | str,
    member_evidence_keys: Iterable[str],
    version: str = MATCHER_CONTRACT_VERSION,
) -> str:
    rel_str = match_relation.value if isinstance(match_relation, MatchRelation) else str(match_relation)
    sorted_keys = sorted(member_evidence_keys)
    payload = f"{version}:{rel_str}:{':'.join(sorted_keys)}"
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProtectedAccountBinding:
    protected_account_key: str
    institution_id: str
    account_type: AccountType | str
    ownership_state: OwnershipState | str
    ownership_confidence: ConfidenceLevel | str
    lifecycle_state: LifecycleState | str = LifecycleState.UNCHANGED
    persistence_eligible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.protected_account_key, str) or not self.protected_account_key.strip():
            raise ValueError("protected_account_key must not be empty")
        if not isinstance(self.institution_id, str) or not self.institution_id.strip():
            raise ValueError("institution_id must not be empty")

    @property
    def is_explicitly_owned(self) -> bool:
        try:
            state = OwnershipState(self.ownership_state)
            conf = ConfidenceLevel(self.ownership_confidence)
        except ValueError:
            return False
        return (
            state is OwnershipState.OWNED
            and conf in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)
            and self.persistence_eligible
        )


@dataclass(frozen=True)
class AccountRelationship:
    source_key: str
    target_key: str
    relationship_kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, str) or not self.source_key.strip():
            raise ValueError("source_key must not be empty")
        if not isinstance(self.target_key, str) or not self.target_key.strip():
            raise ValueError("target_key must not be empty")
        if not isinstance(self.relationship_kind, str) or not self.relationship_kind.strip():
            raise ValueError("relationship_kind must not be empty")


@dataclass(frozen=True)
class MatchingContext:
    relationships: tuple[AccountRelationship, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.relationships, (tuple, list)):
            raise TypeError("relationships must be a sequence")
        cleaned: list[AccountRelationship] = []
        for r in self.relationships:
            if not isinstance(r, AccountRelationship):
                raise TypeError("relationship entry must be AccountRelationship")
            cleaned.append(r)
        object.__setattr__(self, "relationships", tuple(cleaned))

    def has_relationship(
        self,
        key_a: str | None,
        key_b: str | None,
        relationship_kind: str | None = None,
    ) -> bool:
        if not key_a or not key_b:
            return False
        for r in self.relationships:
            if relationship_kind and r.relationship_kind != relationship_kind:
                continue
            if (r.source_key == key_a and r.target_key == key_b) or (
                r.source_key == key_b and r.target_key == key_a
            ):
                return True
        return False


@dataclass(frozen=True)
class SafeEvidenceRecord:
    evidence_key: str
    source_document_id: str
    source_registry_id: str
    event_role: EventRole
    row_fingerprint: str
    amount: Decimal | None = None
    currency: str | None = None
    direction: EventDirection | None = None
    status: SourceEventStatus | None = None
    event_date: str | None = None
    settlement_date: str | None = None
    account_binding: ProtectedAccountBinding | None = None
    is_eligible: bool = True
    requires_review: bool = False
    source_event_id: str | None = field(default=None, repr=False)
    provider_transaction_id_raw: str | None = field(default=None, repr=False)
    reference_raw: str | None = field(default=None, repr=False)
    description_raw: str | None = field(default=None, repr=False)
    trade_side: str | None = field(default=None, repr=False)
    gross_amount: Decimal | None = field(default=None, repr=False)
    net_amount: Decimal | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_key, str) or not _HEX64_RE.fullmatch(self.evidence_key):
            raise ValueError("evidence_key must be 64-char hex digest")
        if not isinstance(self.source_document_id, str) or not self.source_document_id.strip():
            raise ValueError("source_document_id must not be empty")
        if not isinstance(self.source_registry_id, str) or not self.source_registry_id.strip():
            raise ValueError("source_registry_id must not be empty")
        if not isinstance(self.event_role, EventRole):
            raise TypeError("event_role must be EventRole")
        if not isinstance(self.row_fingerprint, str) or not _HEX64_RE.fullmatch(self.row_fingerprint):
            raise ValueError("row_fingerprint must be 64-char hex digest")

        _optional_finite_decimal(self.amount, "amount")
        _optional_finite_decimal(self.gross_amount, "gross_amount")
        _optional_finite_decimal(self.net_amount, "net_amount")

        if self.direction is not None and not isinstance(self.direction, EventDirection):
            raise TypeError("direction must be EventDirection")
        if self.status is not None and not isinstance(self.status, SourceEventStatus):
            raise TypeError("status must be SourceEventStatus")


@dataclass(frozen=True)
class EvidenceMatchDecision:
    evidence_key: str
    match_tier: MatchTier
    match_relation: MatchRelation | None
    group_key: str | None
    reason_codes: tuple[MatchReasonCode, ...]
    is_auto_link_eligible: bool

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_key, str) or not _HEX64_RE.fullmatch(self.evidence_key):
            raise ValueError("evidence_key must be 64-char hex digest")
        if not isinstance(self.match_tier, MatchTier):
            raise TypeError("match_tier must be MatchTier")
        if self.match_relation is not None and not isinstance(self.match_relation, MatchRelation):
            raise TypeError("match_relation must be MatchRelation")
        if not isinstance(self.reason_codes, (tuple, list)):
            raise TypeError("reason_codes must be a sequence")
        for r in self.reason_codes:
            if not isinstance(r, MatchReasonCode):
                raise TypeError("reason_code must be MatchReasonCode")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


@dataclass(frozen=True)
class EconomicEventGroup:
    group_key: str
    match_tier: MatchTier
    match_relation: MatchRelation
    member_evidence_keys: tuple[str, ...]
    reason_codes: tuple[MatchReasonCode, ...]
    is_auto_link_eligible: bool
    diagnostics: tuple[SafeDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.group_key, str) or not _HEX64_RE.fullmatch(self.group_key):
            raise ValueError("group_key must be 64-char hex digest")
        if not isinstance(self.match_tier, MatchTier):
            raise TypeError("match_tier must be MatchTier")
        if not isinstance(self.match_relation, MatchRelation):
            raise TypeError("match_relation must be MatchRelation")
        if not isinstance(self.member_evidence_keys, (tuple, list)):
            raise TypeError("member_evidence_keys must be a sequence")
        sorted_members = tuple(sorted(self.member_evidence_keys))
        if len(sorted_members) != len(set(sorted_members)):
            raise ValueError("member_evidence_keys contains duplicates")
        object.__setattr__(self, "member_evidence_keys", sorted_members)

        if not isinstance(self.reason_codes, (tuple, list)):
            raise TypeError("reason_codes must be a sequence")
        for r in self.reason_codes:
            if not isinstance(r, MatchReasonCode):
                raise TypeError("reason_code must be MatchReasonCode")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

        if not isinstance(self.diagnostics, (tuple, list)):
            raise TypeError("diagnostics must be a sequence")
        for d in self.diagnostics:
            if not isinstance(d, SafeDiagnostic):
                raise TypeError("diagnostic must be SafeDiagnostic")
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class EvidenceMatchPlan:
    matcher_contract_version: str
    decisions: tuple[EvidenceMatchDecision, ...]
    groups: tuple[EconomicEventGroup, ...]
    diagnostics: tuple[SafeDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.matcher_contract_version, str) or not self.matcher_contract_version:
            raise ValueError("matcher_contract_version must not be empty")
        if not isinstance(self.decisions, (tuple, list)):
            raise TypeError("decisions must be a sequence")
        sorted_decisions = tuple(sorted(self.decisions, key=lambda d: d.evidence_key))
        object.__setattr__(self, "decisions", sorted_decisions)

        if not isinstance(self.groups, (tuple, list)):
            raise TypeError("groups must be a sequence")
        sorted_groups = tuple(sorted(self.groups, key=lambda g: g.group_key))
        object.__setattr__(self, "groups", sorted_groups)

        if not isinstance(self.diagnostics, (tuple, list)):
            raise TypeError("diagnostics must be a sequence")
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def exact_groups(self) -> tuple[EconomicEventGroup, ...]:
        return tuple(g for g in self.groups if g.match_tier == MatchTier.EXACT)

    @property
    def strong_groups(self) -> tuple[EconomicEventGroup, ...]:
        return tuple(g for g in self.groups if g.match_tier == MatchTier.STRONG)

    @property
    def ambiguous_evidence(self) -> tuple[EvidenceMatchDecision, ...]:
        return tuple(d for d in self.decisions if d.match_tier == MatchTier.AMBIGUOUS)

    @property
    def unmatched_evidence(self) -> tuple[EvidenceMatchDecision, ...]:
        return tuple(d for d in self.decisions if d.match_tier == MatchTier.UNMATCHED)

    @property
    def ineligible_evidence(self) -> tuple[EvidenceMatchDecision, ...]:
        return tuple(d for d in self.decisions if d.match_tier == MatchTier.INELIGIBLE)

    @property
    def duplicate_evidence_groups(self) -> tuple[EconomicEventGroup, ...]:
        return tuple(g for g in self.groups if g.match_relation == MatchRelation.DUPLICATE_EVIDENCE)

    @property
    def internal_transfer_groups(self) -> tuple[EconomicEventGroup, ...]:
        return tuple(g for g in self.groups if g.match_relation == MatchRelation.INTERNAL_TRANSFER_PAIR)

    @property
    def commerce_payment_groups(self) -> tuple[EconomicEventGroup, ...]:
        return tuple(g for g in self.groups if g.match_relation == MatchRelation.COMMERCE_PAYMENT)

    @property
    def investment_settlement_groups(self) -> tuple[EconomicEventGroup, ...]:
        return tuple(g for g in self.groups if g.match_relation == MatchRelation.INVESTMENT_SETTLEMENT)


def make_evidence_record_from_envelope(
    envelope: NormalizedEventEnvelope,
    account_binding: ProtectedAccountBinding | None = None,
    document_disposition_eligible: bool = True,
    document_requires_review: bool = False,
) -> SafeEvidenceRecord:
    key = compute_evidence_key(
        envelope.source_document_id,
        envelope.source_registry_id,
        envelope.event_role,
        envelope.row_fingerprint,
    )

    amount: Decimal | None = None
    currency: str | None = None
    direction: EventDirection | None = None
    status: SourceEventStatus | None = None
    event_date: str | None = None
    settlement_date: str | None = None
    provider_transaction_id_raw: str | None = None
    reference_raw: str | None = None
    description_raw: str | None = None
    trade_side: str | None = None
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None

    payload = envelope.payload
    is_supported_role = True

    if envelope.event_role is EventRole.CASH_MOVEMENT:
        amount = getattr(payload, "amount", None)
        currency = getattr(payload, "currency", None)
        direction = getattr(payload, "direction", None)
        status = getattr(payload, "status", None)
        event_date = getattr(payload, "occurred_at", None) or getattr(payload, "posted_at", None)
        settlement_date = getattr(payload, "settlement_date", None)
        provider_transaction_id_raw = getattr(payload, "provider_transaction_id_raw", None)
        reference_raw = getattr(payload, "reference_raw", None)
        description_raw = getattr(payload, "description_raw", None)
    elif envelope.event_role is EventRole.COMMERCE_ORDER:
        amount = getattr(payload, "order_total", None)
        currency = getattr(payload, "currency", None)
        direction = EventDirection.OUTFLOW
        status = SourceEventStatus.POSTED
        event_date = getattr(payload, "order_date", None)
        provider_transaction_id_raw = getattr(payload, "order_native_id_raw", None)
        reference_raw = getattr(payload, "order_native_id_raw", None)
        description_raw = getattr(payload, "seller_raw", None)
    elif envelope.event_role is EventRole.INVESTMENT_TRADE:
        gross_amount = getattr(payload, "gross_amount", None)
        net_amount = getattr(payload, "net_amount", None)
        amount = net_amount if net_amount is not None else gross_amount
        currency = getattr(payload, "currency", None)
        side = getattr(payload, "side_raw", None)
        if side:
            trade_side = str(side).upper()
            if trade_side == "BUY":
                direction = EventDirection.OUTFLOW
            elif trade_side == "SELL":
                direction = EventDirection.INFLOW
            else:
                direction = EventDirection.UNKNOWN
        status = SourceEventStatus.POSTED
        event_date = getattr(payload, "trade_date", None)
        settlement_date = getattr(payload, "settlement_date", None)
        provider_transaction_id_raw = getattr(payload, "provider_transaction_id_raw", None)
        reference_raw = getattr(payload, "reference_raw", None)
    else:
        is_supported_role = False

    has_error_diags = any(
        getattr(d, "severity", None) == DiagnosticSeverity.ERROR
        for d in envelope.diagnostics
    )

    is_eligible = (
        is_supported_role
        and document_disposition_eligible
        and not document_requires_review
        and not has_error_diags
        and status is SourceEventStatus.POSTED
        and direction not in (None, EventDirection.UNKNOWN)
        and amount is not None
        and amount > 0
        and currency is not None
        and _parse_iso_date(event_date) is not None
    )

    return SafeEvidenceRecord(
        evidence_key=key,
        source_document_id=envelope.source_document_id,
        source_registry_id=envelope.source_registry_id,
        event_role=envelope.event_role,
        row_fingerprint=envelope.row_fingerprint,
        amount=amount,
        currency=currency,
        direction=direction,
        status=status,
        event_date=event_date,
        settlement_date=settlement_date,
        account_binding=account_binding,
        is_eligible=is_eligible,
        requires_review=document_requires_review,
        source_event_id=envelope.source_event_id,
        provider_transaction_id_raw=provider_transaction_id_raw,
        reference_raw=reference_raw,
        description_raw=description_raw,
        trade_side=trade_side,
        gross_amount=gross_amount,
        net_amount=net_amount,
    )


class DeterministicEvidenceMatcher:
    def __init__(self, version: str = MATCHER_CONTRACT_VERSION) -> None:
        self.version = version

    def match(
        self,
        evidence_records: Iterable[SafeEvidenceRecord],
        context: MatchingContext | None = None,
    ) -> EvidenceMatchPlan:
        if evidence_records is None:
            raise TypeError("evidence_records must not be None")
        records: list[SafeEvidenceRecord] = []
        for r in evidence_records:
            if not isinstance(r, SafeEvidenceRecord):
                raise TypeError("Item in evidence_records must be SafeEvidenceRecord")
            records.append(r)

        if context is None:
            context = MatchingContext()
        elif not isinstance(context, MatchingContext):
            raise TypeError("context must be MatchingContext")

        records.sort(key=lambda r: r.evidence_key)

        decisions: dict[str, EvidenceMatchDecision] = {}
        groups: list[EconomicEventGroup] = []
        handled_keys: set[str] = set()

        # Step 1: Pre-categorize ineligible records
        for r in records:
            if not r.is_eligible or r.requires_review:
                reason = self._ineligible_reason(r)
                decisions[r.evidence_key] = EvidenceMatchDecision(
                    evidence_key=r.evidence_key,
                    match_tier=MatchTier.INELIGIBLE,
                    match_relation=None,
                    group_key=None,
                    reason_codes=(reason,),
                    is_auto_link_eligible=False,
                )
                handled_keys.add(r.evidence_key)

        # Step 2: EXACT Matching on eligible records
        eligible_records = [r for r in records if r.evidence_key not in handled_keys]

        # 2a. By source_event_id
        source_event_groups: dict[tuple[str, str], list[SafeEvidenceRecord]] = {}
        for r in eligible_records:
            if r.source_event_id:
                token = (r.source_registry_id, r.source_event_id)
                source_event_groups.setdefault(token, []).append(r)

        for token, cluster in source_event_groups.items():
            if len(cluster) > 1:
                self._evaluate_exact_cluster(
                    cluster,
                    MatchReasonCode.SAME_SOURCE_EVENT_ID,
                    handled_keys,
                    decisions,
                    groups,
                )

        # 2b. By provider_transaction_id_raw
        provider_tx_groups: dict[tuple[str, str], list[SafeEvidenceRecord]] = {}
        for r in eligible_records:
            if r.evidence_key not in handled_keys and r.provider_transaction_id_raw:
                token = (r.source_registry_id, r.provider_transaction_id_raw)
                provider_tx_groups.setdefault(token, []).append(r)

        for token, cluster in provider_tx_groups.items():
            if len(cluster) > 1:
                self._evaluate_exact_cluster(
                    cluster,
                    MatchReasonCode.SAME_PROVIDER_TRANSACTION_ID,
                    handled_keys,
                    decisions,
                    groups,
                )

        # 2c. By document reference (inside same document)
        doc_ref_groups: dict[tuple[str, str], list[SafeEvidenceRecord]] = {}
        for r in eligible_records:
            if r.evidence_key not in handled_keys and r.reference_raw:
                token = (r.source_document_id, r.reference_raw)
                doc_ref_groups.setdefault(token, []).append(r)

        for token, cluster in doc_ref_groups.items():
            if len(cluster) > 1:
                self._evaluate_exact_cluster(
                    cluster,
                    MatchReasonCode.SAME_DOCUMENT_REFERENCE,
                    handled_keys,
                    decisions,
                    groups,
                )

        # Step 3: STRONG Matching
        remaining_eligible = [r for r in records if r.evidence_key not in handled_keys]

        # Evaluate candidate compatibility pairs for STRONG relations
        transfer_pairs = self._find_transfer_pairs(remaining_eligible)
        commerce_pairs = self._find_commerce_pairs(remaining_eligible, context)
        investment_pairs = self._find_investment_pairs(remaining_eligible, context)

        # Combine candidate pairings per relation and check unique mutual pairing
        all_relations = [
            (MatchRelation.INTERNAL_TRANSFER_PAIR, MatchReasonCode.OPPOSITE_OWNED_CASH_MOVEMENT, transfer_pairs),
            (MatchRelation.COMMERCE_PAYMENT, MatchReasonCode.COMMERCE_PAYMENT_CORROBORATION, commerce_pairs),
            (MatchRelation.INVESTMENT_SETTLEMENT, MatchReasonCode.INVESTMENT_SETTLEMENT_CORROBORATION, investment_pairs),
        ]

        # Global adjacency across all strong relations to prevent ambiguous cross-relation matching
        global_adj: dict[str, set[str]] = {}
        rel_adj: dict[MatchRelation, dict[str, set[str]]] = {}

        for relation, reason_code, pairs in all_relations:
            radj: dict[str, set[str]] = {}
            for k1, k2 in pairs:
                radj.setdefault(k1, set()).add(k2)
                radj.setdefault(k2, set()).add(k1)
                global_adj.setdefault(k1, set()).add(k2)
                global_adj.setdefault(k2, set()).add(k1)
            rel_adj[relation] = radj

        # Find keys that have degree > 1 in global adjacency
        ambiguous_keys: set[str] = set()
        for k, neighbors in global_adj.items():
            if len(neighbors) > 1:
                ambiguous_keys.add(k)
                ambiguous_keys.update(neighbors)

        # Mark ambiguous candidates
        for k in sorted(ambiguous_keys):
            if k not in handled_keys:
                decisions[k] = EvidenceMatchDecision(
                    evidence_key=k,
                    match_tier=MatchTier.AMBIGUOUS,
                    match_relation=None,
                    group_key=None,
                    reason_codes=(MatchReasonCode.MULTIPLE_COMPATIBLE_MATCHES,),
                    is_auto_link_eligible=False,
                )
                handled_keys.add(k)

        # Form unique mutual pairs
        for relation, reason_code, pairs in all_relations:
            radj = rel_adj[relation]
            for k1, k2 in pairs:
                if k1 in handled_keys or k2 in handled_keys:
                    continue
                if radj.get(k1) == {k2} and radj.get(k2) == {k1}:
                    member_keys = (min(k1, k2), max(k1, k2))
                    g_key = compute_group_key(relation, member_keys, self.version)
                    group = EconomicEventGroup(
                        group_key=g_key,
                        match_tier=MatchTier.STRONG,
                        match_relation=relation,
                        member_evidence_keys=member_keys,
                        reason_codes=(reason_code,),
                        is_auto_link_eligible=True,
                    )
                    groups.append(group)
                    decisions[k1] = EvidenceMatchDecision(
                        evidence_key=k1,
                        match_tier=MatchTier.STRONG,
                        match_relation=relation,
                        group_key=g_key,
                        reason_codes=(reason_code,),
                        is_auto_link_eligible=True,
                    )
                    decisions[k2] = EvidenceMatchDecision(
                        evidence_key=k2,
                        match_tier=MatchTier.STRONG,
                        match_relation=relation,
                        group_key=g_key,
                        reason_codes=(reason_code,),
                        is_auto_link_eligible=True,
                    )
                    handled_keys.add(k1)
                    handled_keys.add(k2)

        # Step 4: Any remaining records are UNMATCHED
        for r in records:
            if r.evidence_key not in handled_keys:
                reason = self._unmatched_reason(r)
                decisions[r.evidence_key] = EvidenceMatchDecision(
                    evidence_key=r.evidence_key,
                    match_tier=MatchTier.UNMATCHED,
                    match_relation=None,
                    group_key=None,
                    reason_codes=(reason,) if reason else (),
                    is_auto_link_eligible=False,
                )
                handled_keys.add(r.evidence_key)

        return EvidenceMatchPlan(
            matcher_contract_version=self.version,
            decisions=tuple(decisions.values()),
            groups=tuple(groups),
            diagnostics=(),
        )

    def _ineligible_reason(self, r: SafeEvidenceRecord) -> MatchReasonCode:
        if r.requires_review:
            return MatchReasonCode.EVIDENCE_REQUIRES_REVIEW
        if r.event_role in (
            EventRole.BALANCE_SNAPSHOT,
            EventRole.SOURCE_SUMMARY,
            EventRole.ACCOUNT_PERIOD_SUMMARY,
            EventRole.ACCOUNT_OBSERVATION,
        ):
            return MatchReasonCode.UNSUPPORTED_ROLE_PAIR
        if r.status not in (SourceEventStatus.POSTED,):
            return MatchReasonCode.STATUS_NOT_ELIGIBLE
        if r.direction in (None, EventDirection.UNKNOWN):
            return MatchReasonCode.DIRECTION_MISMATCH
        if r.amount is None or r.amount <= 0:
            return MatchReasonCode.AMOUNT_MISMATCH
        if not r.currency:
            return MatchReasonCode.CURRENCY_MISMATCH
        if not _parse_iso_date(r.event_date):
            return MatchReasonCode.MISSING_REQUIRED_DATE
        return MatchReasonCode.STATUS_NOT_ELIGIBLE

    def _unmatched_reason(self, r: SafeEvidenceRecord) -> MatchReasonCode | None:
        if r.event_role is EventRole.CASH_MOVEMENT:
            if not r.account_binding:
                return MatchReasonCode.MISSING_ACCOUNT_BINDING
            if not r.account_binding.is_explicitly_owned:
                return MatchReasonCode.INVALID_ACCOUNT_BINDING
        return None

    def _evaluate_exact_cluster(
        self,
        cluster: list[SafeEvidenceRecord],
        reason_code: MatchReasonCode,
        handled_keys: set[str],
        decisions: dict[str, EvidenceMatchDecision],
        groups: list[EconomicEventGroup],
    ) -> None:
        unhandled = [r for r in cluster if r.evidence_key not in handled_keys]
        if len(unhandled) < 2:
            return

        # Check for contradictory fields
        amounts = {r.amount for r in unhandled}
        currencies = {r.currency for r in unhandled}
        statuses = {r.status for r in unhandled}
        directions = {r.direction for r in unhandled}

        has_conflict = (
            len(amounts) > 1
            or len(currencies) > 1
            or len(statuses) > 1
            or (reason_code != MatchReasonCode.SAME_DOCUMENT_REFERENCE and len(directions) > 1)
        )

        if has_conflict:
            for r in unhandled:
                decisions[r.evidence_key] = EvidenceMatchDecision(
                    evidence_key=r.evidence_key,
                    match_tier=MatchTier.AMBIGUOUS,
                    match_relation=None,
                    group_key=None,
                    reason_codes=(MatchReasonCode.CONFLICTING_EXACT_IDENTITY,),
                    is_auto_link_eligible=False,
                )
                handled_keys.add(r.evidence_key)
        else:
            member_keys = tuple(sorted(r.evidence_key for r in unhandled))
            g_key = compute_group_key(
                MatchRelation.DUPLICATE_EVIDENCE,
                member_keys,
                self.version,
            )
            group = EconomicEventGroup(
                group_key=g_key,
                match_tier=MatchTier.EXACT,
                match_relation=MatchRelation.DUPLICATE_EVIDENCE,
                member_evidence_keys=member_keys,
                reason_codes=(reason_code,),
                is_auto_link_eligible=True,
            )
            groups.append(group)
            for r in unhandled:
                decisions[r.evidence_key] = EvidenceMatchDecision(
                    evidence_key=r.evidence_key,
                    match_tier=MatchTier.EXACT,
                    match_relation=MatchRelation.DUPLICATE_EVIDENCE,
                    group_key=g_key,
                    reason_codes=(reason_code,),
                    is_auto_link_eligible=True,
                )
                handled_keys.add(r.evidence_key)

    def _find_transfer_pairs(
        self,
        records: list[SafeEvidenceRecord],
    ) -> list[tuple[str, str]]:
        cash_records = [
            r for r in records
            if r.event_role is EventRole.CASH_MOVEMENT
            and r.account_binding is not None
            and r.account_binding.is_explicitly_owned
            and r.amount is not None
            and r.currency is not None
            and r.status is SourceEventStatus.POSTED
        ]

        pairs: list[tuple[str, str]] = []
        n = len(cash_records)
        for i in range(n):
            a = cash_records[i]
            d_a = _parse_iso_date(a.event_date)
            if d_a is None:
                continue
            for j in range(i + 1, n):
                b = cash_records[j]
                d_b = _parse_iso_date(b.event_date)
                if d_b is None:
                    continue

                # Distinct protected accounts
                if (
                    a.account_binding.protected_account_key
                    == b.account_binding.protected_account_key
                ):
                    continue

                # Opposite directions
                is_opposite = (
                    (a.direction == EventDirection.INFLOW and b.direction == EventDirection.OUTFLOW)
                    or (a.direction == EventDirection.OUTFLOW and b.direction == EventDirection.INFLOW)
                )
                if not is_opposite:
                    continue

                # Identical Decimal amount and currency
                if a.amount != b.amount or a.currency != b.currency:
                    continue

                # Compatible dates within 2 calendar days
                if calendar_days_between(d_a, d_b) > 2:
                    continue

                pairs.append((a.evidence_key, b.evidence_key))

        return pairs

    def _find_commerce_pairs(
        self,
        records: list[SafeEvidenceRecord],
        context: MatchingContext,
    ) -> list[tuple[str, str]]:
        orders = [
            r for r in records
            if r.event_role is EventRole.COMMERCE_ORDER
            and r.amount is not None
            and r.currency is not None
            and r.status is SourceEventStatus.POSTED
        ]
        cash = [
            r for r in records
            if r.event_role is EventRole.CASH_MOVEMENT
            and r.direction is EventDirection.OUTFLOW
            and r.amount is not None
            and r.currency is not None
            and r.status is SourceEventStatus.POSTED
        ]

        pairs: list[tuple[str, str]] = []
        for o in orders:
            d_o = _parse_iso_date(o.event_date)
            if d_o is None:
                continue
            for c in cash:
                d_c = _parse_iso_date(c.event_date)
                if d_c is None:
                    continue

                # Identical amount and currency
                if o.amount != c.amount or o.currency != c.currency:
                    continue

                # Compatible dates within 3 calendar days
                if calendar_days_between(d_o, d_c) > 3:
                    continue

                # Relationship required in context
                if not self._has_commerce_relationship(o, c, context):
                    continue

                pairs.append((o.evidence_key, c.evidence_key))

        return pairs

    def _has_commerce_relationship(
        self,
        order: SafeEvidenceRecord,
        cash: SafeEvidenceRecord,
        context: MatchingContext,
    ) -> bool:
        order_keys = [order.source_registry_id, order.source_document_id, order.evidence_key]
        if order.account_binding:
            order_keys.append(order.account_binding.protected_account_key)

        cash_keys = [cash.source_registry_id, cash.source_document_id, cash.evidence_key]
        if cash.account_binding:
            cash_keys.extend([
                cash.account_binding.protected_account_key,
                cash.account_binding.institution_id,
            ])

        for ok in order_keys:
            for ck in cash_keys:
                if context.has_relationship(ok, ck, "COMMERCE_PAYMENT"):
                    return True
                if context.has_relationship(ok, ck):
                    return True
        return False

    def _find_investment_pairs(
        self,
        records: list[SafeEvidenceRecord],
        context: MatchingContext,
    ) -> list[tuple[str, str]]:
        trades = [
            r for r in records
            if r.event_role is EventRole.INVESTMENT_TRADE
            and r.currency is not None
            and r.status is SourceEventStatus.POSTED
        ]
        cash = [
            r for r in records
            if r.event_role is EventRole.CASH_MOVEMENT
            and r.amount is not None
            and r.currency is not None
            and r.status is SourceEventStatus.POSTED
        ]

        pairs: list[tuple[str, str]] = []
        for t in trades:
            # Net amount is strictly required for auto-settlement (gross amount must not replace it)
            if t.net_amount is None:
                continue
            if not t.settlement_date:
                continue
            d_t = _parse_iso_date(t.settlement_date)
            if d_t is None:
                continue

            for c in cash:
                d_c = _parse_iso_date(c.event_date)
                if d_c is None:
                    continue

                # Settlement date must be compatible (same calendar day)
                if calendar_days_between(d_t, d_c) != 0:
                    continue

                # Direction compatibility
                if t.trade_side == "BUY" and c.direction != EventDirection.OUTFLOW:
                    continue
                if t.trade_side == "SELL" and c.direction != EventDirection.INFLOW:
                    continue

                # Exact net amount and currency
                if t.net_amount != c.amount or t.currency != c.currency:
                    continue

                # Relationship required in context
                if not self._has_broker_relationship(t, c, context):
                    continue

                pairs.append((t.evidence_key, c.evidence_key))

        return pairs

    def _has_broker_relationship(
        self,
        trade: SafeEvidenceRecord,
        cash: SafeEvidenceRecord,
        context: MatchingContext,
    ) -> bool:
        trade_keys = [trade.source_registry_id, trade.source_document_id, trade.evidence_key]
        if trade.account_binding:
            trade_keys.append(trade.account_binding.protected_account_key)

        cash_keys = [cash.source_registry_id, cash.source_document_id, cash.evidence_key]
        if cash.account_binding:
            cash_keys.extend([
                cash.account_binding.protected_account_key,
                cash.account_binding.institution_id,
            ])

        for tk in trade_keys:
            for ck in cash_keys:
                if (
                    context.has_relationship(tk, ck, "BROKER_SETTLEMENT")
                    or context.has_relationship(tk, ck, "INVESTMENT_SETTLEMENT")
                    or context.has_relationship(tk, ck)
                ):
                    return True
        return False
