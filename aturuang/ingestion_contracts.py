from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Iterable, Mapping, Sequence


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_text(value: str, field_name: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    value = str(value).strip().lower()
    if not _HEX64_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a 64-character SHA-256 hex digest"
        )
    return value


class InstitutionType(str, Enum):
    BANK = "BANK"
    WALLET = "WALLET"
    BROKER = "BROKER"
    COMMERCE = "COMMERCE"
    CASH = "CASH"
    OTHER = "OTHER"


class AccountType(str, Enum):
    TRANSACTIONAL = "TRANSACTIONAL"
    SAVINGS = "SAVINGS"
    SUBACCOUNT = "SUBACCOUNT"
    WALLET = "WALLET"
    RDN = "RDN"
    INVESTMENT = "INVESTMENT"
    CASH = "CASH"
    COMMERCE = "COMMERCE"
    OTHER = "OTHER"


class OwnershipState(str, Enum):
    OWNED = "OWNED"
    THIRD_PARTY = "THIRD_PARTY"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class LifecycleState(str, Enum):
    NEW = "NEW"
    RENAMED = "RENAMED"
    CLOSED = "CLOSED"
    REUSED = "REUSED"
    UNCHANGED = "UNCHANGED"
    UNVERIFIED = "UNVERIFIED"


class SourceChannel(str, Enum):
    PDF = "PDF"
    IMAGE = "IMAGE"
    EMAIL = "EMAIL"
    CSV = "CSV"
    API = "API"
    MANUAL = "MANUAL"


class CompletenessRole(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    SNAPSHOT = "SNAPSHOT"
    ENRICHMENT = "ENRICHMENT"
    NEAR_REALTIME = "NEAR_REALTIME"
    UNKNOWN = "UNKNOWN"


class PeriodStatus(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class TemplateMatchStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN_TEMPLATE = "UNKNOWN_TEMPLATE"
    TEMPLATE_DRIFT = "TEMPLATE_DRIFT"


class DocumentIdentityStatus(str, Enum):
    NEW = "NEW"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    SEMANTIC_DUPLICATE = "SEMANTIC_DUPLICATE"
    REVISION_OR_CONFLICT = "REVISION_OR_CONFLICT"
    AMBIGUOUS = "AMBIGUOUS"


class ImportBatchMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    STAGING = "STAGING"


class ImportBatchStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class EconomicOwner(str, Enum):
    SELF = "SELF"
    THIRD_PARTY = "THIRD_PARTY"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class PayerResponsibility(str, Enum):
    SELF = "SELF"
    THIRD_PARTY_DIRECT = "THIRD_PARTY_DIRECT"
    SELF_REIMBURSABLE = "SELF_REIMBURSABLE"
    UNKNOWN = "UNKNOWN"


class PaymentMatchStatus(str, Enum):
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PersonalSpendingEffect(str, Enum):
    PERSONAL_EXPENSE = "PERSONAL_EXPENSE"
    NO_PERSONAL_EXPENSE = "NO_PERSONAL_EXPENSE"
    PASS_THROUGH_RECEIVABLE = "PASS_THROUGH_RECEIVABLE"
    MIXED_ALLOCATION = "MIXED_ALLOCATION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class InstitutionContract:
    institution_id: str
    display_name: str
    institution_type: InstitutionType
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "institution_id",
            _require_text(self.institution_id, "institution_id"),
        )
        object.__setattr__(
            self,
            "display_name",
            _require_text(self.display_name, "display_name"),
        )


@dataclass(frozen=True)
class SourceRegistryContract:
    source_registry_id: str
    provider_key: str
    display_name: str
    channel: SourceChannel
    institution_id: str | None = None
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_registry_id",
            _require_text(self.source_registry_id, "source_registry_id"),
        )
        object.__setattr__(
            self,
            "provider_key",
            _require_text(self.provider_key, "provider_key"),
        )
        object.__setattr__(
            self,
            "display_name",
            _require_text(self.display_name, "display_name"),
        )
        if self.institution_id is not None:
            object.__setattr__(
                self,
                "institution_id",
                _require_text(self.institution_id, "institution_id"),
            )


@dataclass(frozen=True)
class AccountContract:
    account_id: str
    institution_id: str
    display_name: str
    account_type: AccountType
    ownership_state: OwnershipState
    ownership_confidence: ConfidenceLevel = ConfidenceLevel.UNKNOWN
    parent_account_id: str | None = None
    provider_account_key: str | None = None
    legacy_account_name: str | None = None
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_id",
            _require_text(self.account_id, "account_id"),
        )
        object.__setattr__(
            self,
            "institution_id",
            _require_text(self.institution_id, "institution_id"),
        )
        object.__setattr__(
            self,
            "display_name",
            _require_text(self.display_name, "display_name"),
        )
        if self.parent_account_id is not None:
            object.__setattr__(
                self,
                "parent_account_id",
                _require_text(self.parent_account_id, "parent_account_id"),
            )
        if self.provider_account_key is not None:
            object.__setattr__(
                self,
                "provider_account_key",
                _require_text(self.provider_account_key, "provider_account_key"),
            )
        if self.legacy_account_name is not None:
            object.__setattr__(
                self,
                "legacy_account_name",
                _require_text(self.legacy_account_name, "legacy_account_name"),
            )


@dataclass(frozen=True)
class AccountObservationContract:
    source_document_id: str
    institution_id: str
    observed_account_key: str
    display_name_raw: str
    ownership_state: OwnershipState
    lifecycle_state: LifecycleState
    lifecycle_evidence: str
    parent_observed_account_key: str | None = None
    parent_account_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_document_id",
            "institution_id",
            "observed_account_key",
            "display_name_raw",
            "lifecycle_evidence",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.parent_observed_account_key is not None:
            object.__setattr__(
                self,
                "parent_observed_account_key",
                _require_text(
                    self.parent_observed_account_key,
                    "parent_observed_account_key",
                ),
            )
        if self.parent_account_id is not None:
            object.__setattr__(
                self,
                "parent_account_id",
                _require_text(self.parent_account_id, "parent_account_id"),
            )


@dataclass(frozen=True)
class AccountLifecycleEvent:
    account_id: str
    event_type: LifecycleState
    effective_at: str
    source_document_id: str
    previous_alias: str | None = None
    new_alias: str | None = None
    related_account_id: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "account_id",
            "effective_at",
            "source_document_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )

        if self.event_type is LifecycleState.RENAMED:
            if not self.previous_alias or not self.new_alias:
                raise ValueError("RENAMED requires previous_alias and new_alias")
            if self.previous_alias.strip() == self.new_alias.strip():
                raise ValueError("RENAMED aliases must differ")

        if self.event_type is LifecycleState.REUSED:
            if not self.related_account_id:
                raise ValueError("REUSED requires related_account_id")
            if self.related_account_id == self.account_id:
                raise ValueError(
                    "REUSED related_account_id must be a different account identity"
                )


@dataclass(frozen=True)
class SourceCapabilityContract:
    source_registry_id: str
    document_kind: str
    completeness_role: CompletenessRole
    has_transactions: bool = False
    has_balances: bool = False
    has_running_balance: bool = False
    has_native_event_id: bool = False
    has_account_hierarchy: bool = False
    supports_reconciliation: bool = False
    supports_commerce_enrichment: bool = False
    near_realtime: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_registry_id",
            _require_text(self.source_registry_id, "source_registry_id"),
        )
        object.__setattr__(
            self,
            "document_kind",
            _require_text(self.document_kind, "document_kind"),
        )


@dataclass(frozen=True)
class SourceTemplateContract:
    template_id: str
    source_registry_id: str
    template_version: str
    parser_version: str
    document_kind: str
    input_type: SourceChannel
    template_fingerprint: str
    text_layer_required: bool = False
    active: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "template_id",
            "source_registry_id",
            "template_version",
            "parser_version",
            "document_kind",
            "template_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True)
class ArchiveLineageContract:
    parent_archive_sha256: str
    archive_depth: int
    member_path: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parent_archive_sha256",
            _require_sha256(
                self.parent_archive_sha256,
                "parent_archive_sha256",
            ),
        )
        object.__setattr__(
            self,
            "member_path",
            _require_text(self.member_path, "member_path"),
        )
        if self.archive_depth < 0:
            raise ValueError("archive_depth must be >= 0")


@dataclass(frozen=True)
class SourceDocumentIdentity:
    source_document_id: str
    content_sha256: str
    natural_document_key: str
    template_id: str
    template_fingerprint: str
    parser_version: str
    source_registry_id: str
    import_batch_id: str
    template_match_status: TemplateMatchStatus
    period_status: PeriodStatus
    semantic_sha256: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    archive_lineage: tuple[ArchiveLineageContract, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        for field_name in (
            "source_document_id",
            "natural_document_key",
            "template_id",
            "template_fingerprint",
            "parser_version",
            "source_registry_id",
            "import_batch_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "content_sha256",
            _require_sha256(self.content_sha256, "content_sha256"),
        )
        if self.semantic_sha256 is not None:
            object.__setattr__(
                self,
                "semantic_sha256",
                _require_sha256(self.semantic_sha256, "semantic_sha256"),
            )


@dataclass(frozen=True)
class SourceProvenanceContract:
    source_document_id: str
    raw_locator: str
    page_number: int | None = None
    image_index: int | None = None
    row_index: int | None = None
    block_id: str | None = None
    raw_text: str = ""
    raw_reference: str = ""
    raw_description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_document_id",
            _require_text(self.source_document_id, "source_document_id"),
        )
        object.__setattr__(
            self,
            "raw_locator",
            _require_text(self.raw_locator, "raw_locator"),
        )
        for field_name in ("page_number", "image_index", "row_index"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must be >= 0 when present")


@dataclass(frozen=True)
class ReferenceEvidenceContract:
    raw_reference: str
    normalized_reference: str | None = None
    provider_transaction_id_raw: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_reference", str(self.raw_reference))
        if self.normalized_reference is not None:
            object.__setattr__(
                self,
                "normalized_reference",
                str(self.normalized_reference),
            )
        if self.provider_transaction_id_raw is not None:
            object.__setattr__(
                self,
                "provider_transaction_id_raw",
                str(self.provider_transaction_id_raw),
            )


@dataclass(frozen=True)
class ImportBatchContract:
    import_batch_id: str
    source_registry_id: str
    mode: ImportBatchMode
    idempotency_key: str
    status: ImportBatchStatus = ImportBatchStatus.IN_PROGRESS
    document_count: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "import_batch_id",
            "source_registry_id",
            "idempotency_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.document_count < 0:
            raise ValueError("document_count must be >= 0")


@dataclass(frozen=True)
class CommerceLineAllocation:
    line_key: str
    economic_owner: EconomicOwner
    payer_responsibility: PayerResponsibility
    payer_actor: str | None = None
    economic_owner_actor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "line_key",
            _require_text(self.line_key, "line_key"),
        )
        if self.economic_owner is EconomicOwner.MIXED:
            raise ValueError(
                "A line allocation cannot itself have MIXED economic ownership"
            )


@dataclass(frozen=True)
class CommerceOrderOwnershipContract:
    order_key: str
    commerce_actor: str
    economic_owner: EconomicOwner
    payer_responsibility: PayerResponsibility
    payment_match_status: PaymentMatchStatus
    personal_spending_effect: PersonalSpendingEffect
    recipient: str | None = None
    payer_actor: str | None = None
    economic_owner_actor: str | None = None
    line_allocations: tuple[CommerceLineAllocation, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "order_key",
            _require_text(self.order_key, "order_key"),
        )
        object.__setattr__(
            self,
            "commerce_actor",
            _require_text(self.commerce_actor, "commerce_actor"),
        )

        if self.economic_owner is EconomicOwner.MIXED:
            if len(self.line_allocations) < 2:
                raise ValueError(
                    "MIXED economic ownership requires at least two line allocations"
                )
            owners = {line.economic_owner for line in self.line_allocations}
            if len(owners) < 2:
                raise ValueError(
                    "MIXED economic ownership requires distinct line owners"
                )
            if (
                self.personal_spending_effect
                is not PersonalSpendingEffect.MIXED_ALLOCATION
            ):
                raise ValueError(
                    "MIXED economic ownership requires MIXED_ALLOCATION spending effect"
                )
        elif self.line_allocations:
            raise ValueError(
                "line_allocations are only allowed when economic_owner is MIXED"
            )

        if (
            self.payer_responsibility
            is PayerResponsibility.THIRD_PARTY_DIRECT
            and self.personal_spending_effect
            is not PersonalSpendingEffect.NO_PERSONAL_EXPENSE
        ):
            raise ValueError(
                "THIRD_PARTY_DIRECT must have NO_PERSONAL_EXPENSE effect"
            )

        if (
            self.payer_responsibility
            is PayerResponsibility.SELF_REIMBURSABLE
            and self.personal_spending_effect
            is not PersonalSpendingEffect.PASS_THROUGH_RECEIVABLE
        ):
            raise ValueError(
                "SELF_REIMBURSABLE must have PASS_THROUGH_RECEIVABLE effect"
            )

        unresolved = (
            self.economic_owner is EconomicOwner.UNKNOWN
            or self.payer_responsibility is PayerResponsibility.UNKNOWN
            or self.payment_match_status
            in (PaymentMatchStatus.AMBIGUOUS, PaymentMatchStatus.UNMATCHED)
        )
        if (
            unresolved
            and self.personal_spending_effect
            is not PersonalSpendingEffect.REVIEW_REQUIRED
        ):
            raise ValueError(
                "Unresolved commerce ownership/payment must require review"
            )


def validate_account_graph(accounts: Sequence[AccountContract]) -> None:
    by_id: dict[str, AccountContract] = {}

    for account in accounts:
        if account.account_id in by_id:
            raise ValueError(f"duplicate account_id: {account.account_id}")
        by_id[account.account_id] = account

    for account in accounts:
        parent_id = account.parent_account_id
        if parent_id is None:
            continue

        if parent_id == account.account_id:
            raise ValueError(
                f"account {account.account_id} cannot parent itself"
            )

        parent = by_id.get(parent_id)
        if parent is None:
            raise ValueError(f"parent account not found: {parent_id}")

        if parent.institution_id != account.institution_id:
            raise ValueError(
                "parent and child accounts must belong to the same institution"
            )

    for account in accounts:
        seen: set[str] = set()
        current = account

        while current.parent_account_id is not None:
            if current.account_id in seen:
                raise ValueError(
                    f"account hierarchy cycle detected at {current.account_id}"
                )
            seen.add(current.account_id)
            current = by_id[current.parent_account_id]


def validate_reuse_transition(
    previous_account: AccountContract,
    new_account: AccountContract,
    event: AccountLifecycleEvent,
) -> None:
    if event.event_type is not LifecycleState.REUSED:
        raise ValueError(
            "reuse transition requires a REUSED lifecycle event"
        )
    if event.account_id != new_account.account_id:
        raise ValueError(
            "REUSED event must belong to the new account identity"
        )
    if event.related_account_id != previous_account.account_id:
        raise ValueError(
            "REUSED event must reference the previous account identity"
        )
    if previous_account.account_id == new_account.account_id:
        raise ValueError(
            "reused provider slot must create a new stable account identity"
        )
    if previous_account.institution_id != new_account.institution_id:
        raise ValueError(
            "reused provider slot must remain inside the same institution"
        )
    if (
        not previous_account.provider_account_key
        or not new_account.provider_account_key
    ):
        raise ValueError(
            "reused provider slot requires provider_account_key on both accounts"
        )
    if (
        previous_account.provider_account_key
        != new_account.provider_account_key
    ):
        raise ValueError(
            "REUSED accounts must share the same provider_account_key"
        )


def match_template_fail_closed(
    templates: Iterable[SourceTemplateContract],
    *,
    source_registry_id: str,
    template_fingerprint: str,
) -> SourceTemplateContract | None:
    source_registry_id = _require_text(
        source_registry_id,
        "source_registry_id",
    )
    template_fingerprint = _require_text(
        template_fingerprint,
        "template_fingerprint",
    )

    matches = [
        template
        for template in templates
        if template.active
        and template.source_registry_id == source_registry_id
        and template.template_fingerprint == template_fingerprint
    ]

    if len(matches) == 1:
        return matches[0]
    return None


def classify_document_identity(
    existing_documents: Iterable[SourceDocumentIdentity],
    candidate: SourceDocumentIdentity,
) -> DocumentIdentityStatus:
    existing_list = list(existing_documents)

    for existing in existing_list:
        if existing.content_sha256 == candidate.content_sha256:
            return DocumentIdentityStatus.EXACT_DUPLICATE

    same_natural_key = [
        existing
        for existing in existing_list
        if existing.source_registry_id == candidate.source_registry_id
        and existing.natural_document_key == candidate.natural_document_key
    ]

    if not same_natural_key:
        return DocumentIdentityStatus.NEW

    candidate_semantic = candidate.semantic_sha256
    if not candidate_semantic:
        return DocumentIdentityStatus.AMBIGUOUS

    for existing in same_natural_key:
        if (
            existing.semantic_sha256
            and existing.semantic_sha256 == candidate_semantic
        ):
            return DocumentIdentityStatus.SEMANTIC_DUPLICATE

    if all(existing.semantic_sha256 for existing in same_natural_key):
        return DocumentIdentityStatus.REVISION_OR_CONFLICT

    return DocumentIdentityStatus.AMBIGUOUS


def capability_index(
    capabilities: Iterable[SourceCapabilityContract],
) -> Mapping[str, SourceCapabilityContract]:
    result: dict[str, SourceCapabilityContract] = {}

    for capability in capabilities:
        key = capability.source_registry_id
        if key in result:
            raise ValueError(f"duplicate source capability: {key}")
        result[key] = capability

    return result
