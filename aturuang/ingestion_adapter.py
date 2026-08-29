from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from .ingestion_contracts import (
    ConfidenceLevel,
    PeriodStatus,
    SourceChannel,
    SourceProvenanceContract,
    TemplateMatchStatus,
)


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_LOCATOR_TOKEN_RE = re.compile(r"^[0-9a-f]{12,64}$")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_LONG_DIGIT_RE = re.compile(r"\b\d{8,}\b")
_PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|/(?:home|users|mnt|private|var|tmp)/)"
)
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:password|secret|api[_ -]?key|access[_ -]?token)\s*[:=]"
)


class AdapterContractError(ValueError):
    pass


class EventRole(str, Enum):
    CASH_MOVEMENT = "CASH_MOVEMENT"
    BALANCE_SNAPSHOT = "BALANCE_SNAPSHOT"
    SOURCE_SUMMARY = "SOURCE_SUMMARY"
    ACCOUNT_PERIOD_SUMMARY = "ACCOUNT_PERIOD_SUMMARY"
    ACCOUNT_OBSERVATION = "ACCOUNT_OBSERVATION"
    INVESTMENT_TRADE = "INVESTMENT_TRADE"
    COMMERCE_ORDER = "COMMERCE_ORDER"


class EventDirection(str, Enum):
    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class SourceEventStatus(str, Enum):
    POSTED = "POSTED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class AdapterParseStatus(str, Enum):
    COMPLETED = "COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class DiagnosticSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class SnapshotKind(str, Enum):
    OPENING = "OPENING"
    CLOSING = "CLOSING"
    CURRENT = "CURRENT"
    OTHER = "OTHER"


def _require_text(value: str, field_name: str) -> str:
    value = str(value).strip()
    if not value:
        raise AdapterContractError(f"{field_name} must not be empty")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_sha256(value: str, field_name: str) -> str:
    value = str(value).strip().lower()
    if not _HEX64_RE.fullmatch(value):
        raise AdapterContractError(
            f"{field_name} must be a 64-character SHA-256 hex digest"
        )
    return value


def _require_decimal(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise AdapterContractError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise AdapterContractError(f"{field_name} must be finite")
    return value


def _optional_decimal(
    value: Decimal | None,
    field_name: str,
) -> Decimal | None:
    if value is None:
        return None
    return _require_decimal(value, field_name)


def _currency(value: str) -> str:
    value = _require_text(value, "currency").upper()
    if len(value) > 12:
        raise AdapterContractError("currency is unexpectedly long")
    return value


def _validate_safe_message(value: str, field_name: str) -> str:
    value = _require_text(value, field_name)
    if len(value) > 240:
        raise AdapterContractError(f"{field_name} is too long")
    if "\n" in value or "\r" in value or "\t" in value:
        raise AdapterContractError(f"{field_name} must be single-line")
    if _PRIVATE_PATH_RE.search(value):
        raise AdapterContractError(f"{field_name} contains private path data")
    if _EMAIL_RE.search(value):
        raise AdapterContractError(f"{field_name} contains an email address")
    if _LONG_DIGIT_RE.search(value):
        raise AdapterContractError(
            f"{field_name} contains a long numeric identifier"
        )
    if _CREDENTIAL_RE.search(value):
        raise AdapterContractError(
            f"{field_name} contains credential-like content"
        )
    return value


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    source_registry_id: str
    template_id: str
    parser_version: str
    source_channel: SourceChannel

    def __post_init__(self) -> None:
        for name in (
            "adapter_id",
            "source_registry_id",
            "template_id",
            "parser_version",
        ):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name),
            )

        if not isinstance(self.source_channel, SourceChannel):
            raise AdapterContractError("source_channel must be SourceChannel")


@dataclass(frozen=True)
class AdapterInput:
    source_document_id: str
    content_sha256: str
    source_registry_id: str
    template_id: str
    parser_version: str
    source_channel: SourceChannel
    template_match_status: TemplateMatchStatus
    period_status: PeriodStatus
    template_fingerprint: str | None = None
    binary_payload: bytes | None = field(default=None, repr=False)
    text_payload: str | None = field(default=None, repr=False)
    structured_payload: Mapping[str, object] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "source_document_id",
            "source_registry_id",
            "template_id",
            "parser_version",
        ):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name),
            )

        object.__setattr__(
            self,
            "content_sha256",
            _require_sha256(self.content_sha256, "content_sha256"),
        )

        if not isinstance(self.source_channel, SourceChannel):
            raise AdapterContractError("source_channel must be SourceChannel")
        if not isinstance(self.template_match_status, TemplateMatchStatus):
            raise AdapterContractError(
                "template_match_status must be TemplateMatchStatus"
            )
        if not isinstance(self.period_status, PeriodStatus):
            raise AdapterContractError("period_status must be PeriodStatus")

        object.__setattr__(
            self,
            "template_fingerprint",
            _optional_text(
                self.template_fingerprint,
                "template_fingerprint",
            ),
        )

        payload_count = sum(
            value is not None
            for value in (
                self.binary_payload,
                self.text_payload,
                self.structured_payload,
            )
        )
        if payload_count != 1:
            raise AdapterContractError(
                "AdapterInput requires exactly one private payload"
            )

        if self.binary_payload is not None:
            if not isinstance(self.binary_payload, bytes):
                raise AdapterContractError("binary_payload must be bytes")

        if self.text_payload is not None:
            if not isinstance(self.text_payload, str):
                raise AdapterContractError("text_payload must be str")

        if self.structured_payload is not None:
            if not isinstance(self.structured_payload, Mapping):
                raise AdapterContractError(
                    "structured_payload must be a mapping"
                )
            object.__setattr__(
                self,
                "structured_payload",
                MappingProxyType(dict(self.structured_payload)),
            )

    @property
    def payload_kind(self) -> str:
        if self.binary_payload is not None:
            return "BINARY"
        if self.text_payload is not None:
            return "TEXT"
        return "STRUCTURED"


def validate_adapter_input(
    descriptor: AdapterDescriptor,
    source: AdapterInput,
) -> None:
    if descriptor.source_registry_id != source.source_registry_id:
        raise AdapterContractError("adapter/source registry mismatch")
    if descriptor.template_id != source.template_id:
        raise AdapterContractError("adapter/template mismatch")
    if descriptor.parser_version != source.parser_version:
        raise AdapterContractError("adapter/parser version mismatch")
    if descriptor.source_channel is not source.source_channel:
        raise AdapterContractError("adapter/source channel mismatch")
    if source.template_match_status is not TemplateMatchStatus.KNOWN:
        raise AdapterContractError(
            "adapter input must have KNOWN template authority"
        )


@dataclass(frozen=True)
class SafeDiagnostic:
    code: str
    severity: DiagnosticSeverity
    message: str
    field_name: str | None = None
    review_required: bool = False
    locator_token: str | None = None

    def __post_init__(self) -> None:
        code = _require_text(self.code, "code").upper()
        if not _CODE_RE.fullmatch(code):
            raise AdapterContractError("diagnostic code is invalid")
        object.__setattr__(self, "code", code)

        if not isinstance(self.severity, DiagnosticSeverity):
            raise AdapterContractError(
                "severity must be DiagnosticSeverity"
            )

        object.__setattr__(
            self,
            "message",
            _validate_safe_message(self.message, "message"),
        )

        if self.field_name is not None:
            field_name = _require_text(self.field_name, "field_name")
            if not _FIELD_RE.fullmatch(field_name):
                raise AdapterContractError("field_name is invalid")
            object.__setattr__(self, "field_name", field_name)

        if self.locator_token is not None:
            token = str(self.locator_token).strip().lower()
            if not _LOCATOR_TOKEN_RE.fullmatch(token):
                raise AdapterContractError("locator_token is invalid")
            object.__setattr__(self, "locator_token", token)


@dataclass(frozen=True)
class PaymentComponentEvidence:
    method_raw: str = field(repr=False)
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method_raw",
            _require_text(self.method_raw, "method_raw"),
        )
        object.__setattr__(
            self,
            "amount",
            _require_decimal(self.amount, "amount"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))


@dataclass(frozen=True)
class AmountComponentEvidence:
    label_raw: str = field(repr=False)
    amount: Decimal
    currency: str
    direction_raw: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "label_raw",
            _require_text(self.label_raw, "label_raw"),
        )
        object.__setattr__(
            self,
            "amount",
            _require_decimal(self.amount, "amount"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self,
            "direction_raw",
            _optional_text(self.direction_raw, "direction_raw"),
        )


@dataclass(frozen=True)
class CashMovementEvidence:
    amount: Decimal
    currency: str
    direction: EventDirection
    status: SourceEventStatus
    account_id: str | None = None
    subaccount_id: str | None = None
    source_account_key_raw: str | None = field(default=None, repr=False)
    source_account_display_raw: str | None = field(default=None, repr=False)
    destination_account_key_raw: str | None = field(
        default=None,
        repr=False,
    )
    destination_account_display_raw: str | None = field(
        default=None,
        repr=False,
    )
    occurred_at: str | None = None
    posted_at: str | None = None
    settlement_date: str | None = None
    direction_raw: str | None = field(default=None, repr=False)
    status_raw: str | None = field(default=None, repr=False)
    counterparty_raw: str | None = field(default=None, repr=False)
    counterparty_normalized: str | None = field(default=None, repr=False)
    description_raw: str | None = field(default=None, repr=False)
    provider_transaction_id_raw: str | None = field(
        default=None,
        repr=False,
    )
    reference_raw: str | None = field(default=None, repr=False)
    reference_normalized: str | None = field(default=None, repr=False)
    balance_after: Decimal | None = None
    payment_method_raw: str | None = field(default=None, repr=False)
    provider_category_raw: str | None = field(default=None, repr=False)
    event_hint: str | None = None
    payment_components: tuple[PaymentComponentEvidence, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "amount",
            _require_decimal(self.amount, "amount"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))

        if not isinstance(self.direction, EventDirection):
            raise AdapterContractError("direction must be EventDirection")
        if not isinstance(self.status, SourceEventStatus):
            raise AdapterContractError("status must be SourceEventStatus")

        for name in (
            "account_id",
            "subaccount_id",
            "source_account_key_raw",
            "source_account_display_raw",
            "destination_account_key_raw",
            "destination_account_display_raw",
            "occurred_at",
            "posted_at",
            "settlement_date",
            "direction_raw",
            "status_raw",
            "counterparty_raw",
            "counterparty_normalized",
            "description_raw",
            "provider_transaction_id_raw",
            "reference_raw",
            "reference_normalized",
            "payment_method_raw",
            "provider_category_raw",
            "event_hint",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )

        object.__setattr__(
            self,
            "balance_after",
            _optional_decimal(self.balance_after, "balance_after"),
        )
        object.__setattr__(
            self,
            "payment_components",
            tuple(self.payment_components),
        )

        for item in self.payment_components:
            if not isinstance(item, PaymentComponentEvidence):
                raise AdapterContractError(
                    "payment_components contain an invalid item"
                )


@dataclass(frozen=True)
class BalanceSnapshotEvidence:
    balance: Decimal
    currency: str
    snapshot_kind: SnapshotKind
    account_id: str | None = None
    subaccount_id: str | None = None
    observed_account_key_raw: str | None = field(default=None, repr=False)
    observed_account_display_raw: str | None = field(
        default=None,
        repr=False,
    )
    observed_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "balance",
            _require_decimal(self.balance, "balance"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))

        if not isinstance(self.snapshot_kind, SnapshotKind):
            raise AdapterContractError(
                "snapshot_kind must be SnapshotKind"
            )

        for name in (
            "account_id",
            "subaccount_id",
            "observed_account_key_raw",
            "observed_account_display_raw",
            "observed_at",
            "period_start",
            "period_end",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )


@dataclass(frozen=True)
class SourceSummaryEvidence:
    currency: str
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: Decimal | None = None
    incoming_total: Decimal | None = None
    outgoing_total: Decimal | None = None
    closing_balance: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", _currency(self.currency))

        for name in ("period_start", "period_end"):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )

        for name in (
            "opening_balance",
            "incoming_total",
            "outgoing_total",
            "closing_balance",
        ):
            object.__setattr__(
                self,
                name,
                _optional_decimal(getattr(self, name), name),
            )


@dataclass(frozen=True)
class AccountPeriodSummaryEvidence:
    observed_provider_account_key: str = field(repr=False)
    currency: str
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: Decimal | None = None
    incoming_total: Decimal | None = None
    outgoing_total: Decimal | None = None
    closing_balance: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_provider_account_key",
            _require_text(
                self.observed_provider_account_key,
                "observed_provider_account_key",
            ),
        )
        object.__setattr__(self, "currency", _currency(self.currency))

        for name in ("period_start", "period_end"):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )

        for name in (
            "opening_balance",
            "incoming_total",
            "outgoing_total",
            "closing_balance",
        ):
            object.__setattr__(
                self,
                name,
                _optional_decimal(getattr(self, name), name),
            )


@dataclass(frozen=True)
class ObservedAccountEvidence:
    observed_provider_account_key: str = field(repr=False)
    display_name_raw: str = field(repr=False)
    institution_id: str | None = None
    parent_observed_key: str | None = field(default=None, repr=False)
    provider_state_raw: str | None = field(default=None, repr=False)
    observed_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_provider_account_key",
            _require_text(
                self.observed_provider_account_key,
                "observed_provider_account_key",
            ),
        )
        object.__setattr__(
            self,
            "display_name_raw",
            _require_text(self.display_name_raw, "display_name_raw"),
        )

        for name in (
            "institution_id",
            "parent_observed_key",
            "provider_state_raw",
            "observed_at",
            "period_start",
            "period_end",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )


@dataclass(frozen=True)
class InvestmentTradeEvidence:
    instrument_raw: str = field(repr=False)
    trade_date: str
    settlement_date: str
    side_raw: str = field(repr=False)
    quantity: Decimal
    unit_price: Decimal
    currency: str
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None
    provider_transaction_id_raw: str | None = field(
        default=None,
        repr=False,
    )
    reference_raw: str | None = field(default=None, repr=False)
    account_hint_raw: str | None = field(default=None, repr=False)
    amount_components: tuple[AmountComponentEvidence, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        for name in ("instrument_raw", "trade_date", "settlement_date", "side_raw"):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name),
            )

        object.__setattr__(
            self,
            "quantity",
            _require_decimal(self.quantity, "quantity"),
        )
        object.__setattr__(
            self,
            "unit_price",
            _require_decimal(self.unit_price, "unit_price"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self,
            "gross_amount",
            _optional_decimal(self.gross_amount, "gross_amount"),
        )
        object.__setattr__(
            self,
            "net_amount",
            _optional_decimal(self.net_amount, "net_amount"),
        )

        for name in (
            "provider_transaction_id_raw",
            "reference_raw",
            "account_hint_raw",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )

        object.__setattr__(
            self,
            "amount_components",
            tuple(self.amount_components),
        )
        for item in self.amount_components:
            if not isinstance(item, AmountComponentEvidence):
                raise AdapterContractError(
                    "amount_components contain an invalid item"
                )


@dataclass(frozen=True)
class CommerceLineItemEvidence:
    line_key: str
    product_name_raw: str = field(repr=False)
    quantity: Decimal
    line_subtotal: Decimal
    currency: str
    variation_raw: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "line_key",
            _require_text(self.line_key, "line_key"),
        )
        object.__setattr__(
            self,
            "product_name_raw",
            _require_text(self.product_name_raw, "product_name_raw"),
        )
        object.__setattr__(
            self,
            "quantity",
            _require_decimal(self.quantity, "quantity"),
        )
        object.__setattr__(
            self,
            "line_subtotal",
            _require_decimal(self.line_subtotal, "line_subtotal"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self,
            "variation_raw",
            _optional_text(self.variation_raw, "variation_raw"),
        )


@dataclass(frozen=True)
class CommerceOrderEvidence:
    order_native_id_raw: str = field(repr=False)
    order_date: str
    order_total: Decimal
    currency: str
    seller_raw: str | None = field(default=None, repr=False)
    payment_method_raw: str | None = field(default=None, repr=False)
    shipping_service_raw: str | None = field(default=None, repr=False)
    recipient_raw: str | None = field(default=None, repr=False)
    line_items: tuple[CommerceLineItemEvidence, ...] = field(
        default_factory=tuple
    )
    amount_components: tuple[AmountComponentEvidence, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "order_native_id_raw",
            _require_text(
                self.order_native_id_raw,
                "order_native_id_raw",
            ),
        )
        object.__setattr__(
            self,
            "order_date",
            _require_text(self.order_date, "order_date"),
        )
        object.__setattr__(
            self,
            "order_total",
            _require_decimal(self.order_total, "order_total"),
        )
        object.__setattr__(self, "currency", _currency(self.currency))

        for name in (
            "seller_raw",
            "payment_method_raw",
            "shipping_service_raw",
            "recipient_raw",
        ):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name),
            )

        object.__setattr__(self, "line_items", tuple(self.line_items))
        object.__setattr__(
            self,
            "amount_components",
            tuple(self.amount_components),
        )

        for item in self.line_items:
            if not isinstance(item, CommerceLineItemEvidence):
                raise AdapterContractError(
                    "line_items contain an invalid item"
                )
        for item in self.amount_components:
            if not isinstance(item, AmountComponentEvidence):
                raise AdapterContractError(
                    "amount_components contain an invalid item"
                )


EvidencePayload = (
    CashMovementEvidence
    | BalanceSnapshotEvidence
    | SourceSummaryEvidence
    | AccountPeriodSummaryEvidence
    | ObservedAccountEvidence
    | InvestmentTradeEvidence
    | CommerceOrderEvidence
)


_ROLE_PAYLOAD_TYPES: dict[EventRole, type[object]] = {
    EventRole.CASH_MOVEMENT: CashMovementEvidence,
    EventRole.BALANCE_SNAPSHOT: BalanceSnapshotEvidence,
    EventRole.SOURCE_SUMMARY: SourceSummaryEvidence,
    EventRole.ACCOUNT_PERIOD_SUMMARY: AccountPeriodSummaryEvidence,
    EventRole.ACCOUNT_OBSERVATION: ObservedAccountEvidence,
    EventRole.INVESTMENT_TRADE: InvestmentTradeEvidence,
    EventRole.COMMERCE_ORDER: CommerceOrderEvidence,
}


@dataclass(frozen=True)
class NormalizedEventEnvelope:
    source_document_id: str
    source_registry_id: str
    template_id: str
    parser_version: str
    source_channel: SourceChannel
    event_role: EventRole
    source_event_id: str | None
    row_fingerprint: str
    evidence_quality: ConfidenceLevel
    parse_confidence: ConfidenceLevel
    provenance: SourceProvenanceContract = field(repr=False)
    payload: EvidencePayload = field(repr=False)
    diagnostics: tuple[SafeDiagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in (
            "source_document_id",
            "source_registry_id",
            "template_id",
            "parser_version",
        ):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name),
            )

        object.__setattr__(
            self,
            "source_event_id",
            _optional_text(self.source_event_id, "source_event_id"),
        )
        object.__setattr__(
            self,
            "row_fingerprint",
            _require_sha256(self.row_fingerprint, "row_fingerprint"),
        )

        if not isinstance(self.source_channel, SourceChannel):
            raise AdapterContractError("source_channel must be SourceChannel")
        if not isinstance(self.event_role, EventRole):
            raise AdapterContractError("event_role must be EventRole")
        if not isinstance(self.evidence_quality, ConfidenceLevel):
            raise AdapterContractError(
                "evidence_quality must be ConfidenceLevel"
            )
        if not isinstance(self.parse_confidence, ConfidenceLevel):
            raise AdapterContractError(
                "parse_confidence must be ConfidenceLevel"
            )
        if not isinstance(self.provenance, SourceProvenanceContract):
            raise AdapterContractError(
                "provenance must be SourceProvenanceContract"
            )
        if self.provenance.source_document_id != self.source_document_id:
            raise AdapterContractError(
                "provenance/source document mismatch"
            )

        expected_type = _ROLE_PAYLOAD_TYPES[self.event_role]
        if type(self.payload) is not expected_type:
            raise AdapterContractError(
                "event role and evidence payload do not match"
            )

        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        for item in self.diagnostics:
            if not isinstance(item, SafeDiagnostic):
                raise AdapterContractError(
                    "diagnostics contain an invalid item"
                )


@dataclass(frozen=True)
class AdapterResult:
    descriptor: AdapterDescriptor
    source_document_id: str
    parse_status: AdapterParseStatus
    period_status: PeriodStatus
    events: tuple[NormalizedEventEnvelope, ...] = field(
        default_factory=tuple
    )
    diagnostics: tuple[SafeDiagnostic, ...] = field(default_factory=tuple)
    review_reasons: tuple[str, ...] = field(default_factory=tuple)
    period_start: str | None = None
    period_end: str | None = None
    natural_document_key_candidate: str | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, AdapterDescriptor):
            raise AdapterContractError(
                "descriptor must be AdapterDescriptor"
            )
        object.__setattr__(
            self,
            "source_document_id",
            _require_text(
                self.source_document_id,
                "source_document_id",
            ),
        )

        if not isinstance(self.parse_status, AdapterParseStatus):
            raise AdapterContractError(
                "parse_status must be AdapterParseStatus"
            )
        if not isinstance(self.period_status, PeriodStatus):
            raise AdapterContractError(
                "period_status must be PeriodStatus"
            )

        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(
            self,
            "review_reasons",
            tuple(
                _validate_safe_message(value, "review_reason")
                for value in self.review_reasons
            ),
        )
        object.__setattr__(
            self,
            "period_start",
            _optional_text(self.period_start, "period_start"),
        )
        object.__setattr__(
            self,
            "period_end",
            _optional_text(self.period_end, "period_end"),
        )
        object.__setattr__(
            self,
            "natural_document_key_candidate",
            _optional_text(
                self.natural_document_key_candidate,
                "natural_document_key_candidate",
            ),
        )

        if self.parse_status is AdapterParseStatus.FAILED and self.events:
            raise AdapterContractError(
                "FAILED adapter result cannot return trusted events"
            )
        if (
            self.parse_status is AdapterParseStatus.COMPLETED
            and self.review_reasons
        ):
            raise AdapterContractError(
                "COMPLETED adapter result cannot carry review reasons"
            )

        for diagnostic in self.diagnostics:
            if not isinstance(diagnostic, SafeDiagnostic):
                raise AdapterContractError(
                    "diagnostics contain an invalid item"
                )

        for event in self.events:
            if not isinstance(event, NormalizedEventEnvelope):
                raise AdapterContractError(
                    "events contain an invalid item"
                )
            if event.source_document_id != self.source_document_id:
                raise AdapterContractError(
                    "result/event source document mismatch"
                )
            if (
                event.source_registry_id
                != self.descriptor.source_registry_id
            ):
                raise AdapterContractError(
                    "result/event source registry mismatch"
                )
            if event.template_id != self.descriptor.template_id:
                raise AdapterContractError(
                    "result/event template mismatch"
                )
            if event.parser_version != self.descriptor.parser_version:
                raise AdapterContractError(
                    "result/event parser version mismatch"
                )
            if event.source_channel is not self.descriptor.source_channel:
                raise AdapterContractError(
                    "result/event source channel mismatch"
                )

    @property
    def review_required(self) -> bool:
        return (
            self.parse_status is AdapterParseStatus.REVIEW_REQUIRED
            or any(item.review_required for item in self.diagnostics)
        )


@runtime_checkable
class UniversalSourceAdapter(Protocol):
    descriptor: AdapterDescriptor

    def parse(self, source: AdapterInput) -> AdapterResult:
        ...


__all__ = [
    "AccountPeriodSummaryEvidence",
    "AdapterContractError",
    "AdapterDescriptor",
    "AdapterInput",
    "AdapterParseStatus",
    "AdapterResult",
    "AmountComponentEvidence",
    "BalanceSnapshotEvidence",
    "CashMovementEvidence",
    "CommerceLineItemEvidence",
    "CommerceOrderEvidence",
    "DiagnosticSeverity",
    "EventDirection",
    "EventRole",
    "InvestmentTradeEvidence",
    "NormalizedEventEnvelope",
    "ObservedAccountEvidence",
    "PaymentComponentEvidence",
    "SafeDiagnostic",
    "SnapshotKind",
    "SourceEventStatus",
    "SourceSummaryEvidence",
    "UniversalSourceAdapter",
    "validate_adapter_input",
]