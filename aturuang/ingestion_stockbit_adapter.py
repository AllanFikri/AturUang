"""Stockbit Statement of Account (SOA) PDF ingestion adapter for AturUang.

Phase 3.7 Source Contract Freeze and Adapter Skeleton.
Defines deterministic identity behavior, evidence roles, and fail-closed handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from io import BytesIO
import re
from typing import Callable, Sequence

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .ingestion_adapter import (
    AdapterContractError,
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    AdapterResult,
    ConfidenceLevel,
    DiagnosticSeverity,
    EventRole,
    NormalizedEventEnvelope,
    SafeDiagnostic,
    UniversalSourceAdapter,
    validate_adapter_input,
)
from .ingestion_contracts import (
    PeriodStatus,
    SourceChannel,
    SourceProvenanceContract,
    TemplateMatchStatus,
)

STOCKBIT_SOURCE_REGISTRY_ID = "stockbit_soa"
STOCKBIT_TEMPLATE_ID = "stockbit_soa_v1"
STOCKBIT_ADAPTER_ID = "stockbit-soa-pdf-v1"
STOCKBIT_PARSER_VERSION = "parser-v1"
STOCKBIT_TEMPLATE_FINGERPRINT = (
    "5c39503419938772d4fcb0d0f75364da81736d88f149b9e0d54e7eae8ef76bee"
)


class StockbitEvidenceRole(str, Enum):
    DOCUMENT_SUMMARY = "DOCUMENT_SUMMARY"
    CASH_EVIDENCE = "CASH_EVIDENCE"
    TRADE_EVIDENCE = "TRADE_EVIDENCE"
    HOLDING_SNAPSHOT = "HOLDING_SNAPSHOT"
    PORTFOLIO_VALUATION = "PORTFOLIO_VALUATION"


# ---------------------------------------------------------------------------
# Evidence Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StockbitDocumentIdentity:
    source_type: str = STOCKBIT_SOURCE_REGISTRY_ID
    statement_type: str = "statement_of_account"
    period_start: str | None = None
    period_end: str | None = None
    generated_at: str | None = None
    account_fingerprint: str | None = None
    rdn_bank: str | None = None
    currency: str = "IDR"
    document_sha256: str = ""
    natural_document_key_candidate: str | None = None
    source_file_name: str | None = None  # Provenance only, never identity authority
    review_required: bool = False
    review_reason: str | None = None


@dataclass(frozen=True)
class StockbitCashEvidence:
    transaction_date: str
    due_date: str | None
    source_reference: str | None
    source_description: str
    source_debit: Decimal | None
    source_credit: Decimal | None
    signed_amount: Decimal
    running_balance: Decimal | None
    source_row_ordinal: int
    row_evidence_key: str
    requires_cross_source_match: bool = True  # Subject to Phase 5 Jago RDN matching
    review_reason: str | None = None
    evidence_role: str = StockbitEvidenceRole.CASH_EVIDENCE.value


@dataclass(frozen=True)
class StockbitTradeEvidence:
    trade_date: str
    settlement_date: str | None
    contract_reference: str | None
    source_action: str
    ticker: str
    quantity: Decimal
    price: Decimal
    gross_amount: Decimal
    net_settlement_amount: Decimal
    itemized_fees: Decimal | None = None
    itemized_taxes: Decimal | None = None
    review_reason: str | None = None
    evidence_role: str = StockbitEvidenceRole.TRADE_EVIDENCE.value


@dataclass(frozen=True)
class StockbitHoldingSnapshot:
    snapshot_date: str
    ticker: str
    source_security_name: str | None
    quantity: Decimal
    average_price: Decimal
    closing_price: Decimal
    market_value: Decimal
    source_unrealized_gain_loss: Decimal | None = None
    source_unrealized_percentage: Decimal | None = None
    evidence_role: str = StockbitEvidenceRole.HOLDING_SNAPSHOT.value

    @property
    def emits_cash_movement(self) -> bool:
        """Invariable rule: a holding snapshot never emits a cash movement."""
        return False

    @property
    def is_income_or_expense(self) -> bool:
        """Invariable rule: unrealized valuation is never income or expense."""
        return False


@dataclass(frozen=True)
class StockbitPortfolioValuation:
    valuation_date: str | None = None
    cash_investor: Decimal | None = None
    cash_balance: Decimal | None = None
    undue_trading: Decimal | None = None
    short_sell: Decimal | None = None
    portfolio_value: Decimal | None = None
    equity_or_nav: Decimal | None = None
    available_limit: Decimal | None = None
    evidence_role: str = StockbitEvidenceRole.PORTFOLIO_VALUATION.value

    @property
    def emits_cash_movement(self) -> bool:
        """Invariable rule: portfolio valuation totals never emit cash movements."""
        return False


# ---------------------------------------------------------------------------
# Identity and Privacy Helpers
# ---------------------------------------------------------------------------

def normalize_account_string(value: str) -> str:
    """Normalizes an account or client code string by stripping whitespace and punctuation."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value).strip().upper())
    return cleaned


def build_account_fingerprint(raw_account_or_client: str | None) -> str | None:
    """Constructs a stable, private SHA-256 account fingerprint.

    The raw account number or client code is never exposed in logs, git history, or public output.
    """
    if not raw_account_or_client:
        return None
    normalized = normalize_account_string(raw_account_or_client)
    if not normalized:
        return None
    return sha256(normalized.encode("utf-8")).hexdigest()


def build_natural_document_key(
    *,
    source_type: str = STOCKBIT_SOURCE_REGISTRY_ID,
    statement_type: str = "statement_of_account",
    account_fingerprint: str | None,
    period_start: str | None,
    period_end: str | None,
) -> str | None:
    """Constructs the deterministic natural document key for a Stockbit SOA.

    Must contain: source_type, normalized statement_type, account_fingerprint,
    period_start, period_end.
    Does NOT depend on filename, local path, byte hash alone, or ingestion timestamp.
    """
    if not account_fingerprint or not period_start or not period_end:
        return None
    norm_source = str(source_type).strip().lower()
    norm_statement = str(statement_type).strip().lower()
    return f"{norm_source}:{norm_statement}:{account_fingerprint}:{period_start}:{period_end}"


def build_row_evidence_key(
    *,
    natural_document_key: str,
    evidence_role: str,
    transaction_date: str | None,
    due_date: str | None = None,
    source_reference: str | None = None,
    source_description: str | None = None,
    signed_amount: Decimal | None = None,
    running_balance: Decimal | None = None,
    source_row_ordinal: int | None = None,
) -> tuple[str, bool, str | None]:
    """Constructs a deterministic row-level evidence key.

    Row ordinal is used only as provenance and fallback when stable source fields
    are insufficient, in which case review_required is set to True.
    """
    has_stable_core = bool(
        transaction_date
        and source_description
        and signed_amount is not None
        and (source_reference or running_balance is not None)
    )

    if has_stable_core:
        payload = "\x1f".join(
            (
                "stockbit-row-v1",
                natural_document_key,
                evidence_role,
                transaction_date or "",
                due_date or "",
                re.sub(r"\s+", " ", (source_reference or "").strip().upper()),
                re.sub(r"\s+", " ", (source_description or "").strip().upper()),
                str(signed_amount),
                str(running_balance) if running_balance is not None else "",
            )
        )
        return sha256(payload.encode("utf-8")).hexdigest(), False, None

    # Fallback using row ordinal: requires review
    payload = "\x1f".join(
        (
            "stockbit-row-v1-fallback",
            natural_document_key,
            evidence_role,
            transaction_date or "",
            re.sub(r"\s+", " ", (source_description or "").strip().upper()),
            str(source_row_ordinal or 0),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest(), True, "UNSTABLE_ROW_IDENTITY"


# ---------------------------------------------------------------------------
# Header Extraction and Parsing
# ---------------------------------------------------------------------------

_HEADER_PERIOD_RE = re.compile(
    r"\bPeriod\s*[:\s]+(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})\b",
    re.IGNORECASE,
)
_HEADER_BANK_SID_RE = re.compile(
    r"\bBank\s*/\s*(?:Ccy|Currency)\s*/\s*SID\b[:\s]*[\r\n]*\s*([A-Za-z]+)\s+([A-Za-z0-9_.-]+)\s*/\s*([A-Za-z]+)\s+([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_HEADER_ACCOUNT_RE = re.compile(
    r"\bAccount\s*/\s*Sub\s*Account\s*[:\s]*([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_HEADER_CLIENT_CODE_RE = re.compile(
    r"\bClient\s*Code\s*[:\s]+([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


def _format_date(dd_mm_yyyy: str) -> str:
    parts = dd_mm_yyyy.split("/")
    return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"


def extract_header_metadata(
    header_text: str,
    *,
    content_sha256: str = "",
    source_file_name: str | None = None,
    statement_type: str = "statement_of_account",
) -> StockbitDocumentIdentity:
    """Extracts authoritative document identity from header text only.

    Invariable rule: dates in transaction body text must NOT establish document period.
    """
    review_required = False
    review_reason = None

    # 1. Authoritative Period from Header
    period_match = _HEADER_PERIOD_RE.search(header_text)
    if period_match:
        period_start = _format_date(period_match.group(1))
        period_end = _format_date(period_match.group(2))
    else:
        period_start = None
        period_end = None
        review_required = True
        review_reason = "MISSING_STATEMENT_PERIOD"

    # 2. Account Identity / Client Code
    client_raw: str | None = None
    rdn_bank: str | None = None
    currency: str = "IDR"

    bank_match = _HEADER_BANK_SID_RE.search(header_text)
    if bank_match:
        rdn_bank = bank_match.group(1).upper()
        client_raw = bank_match.group(2)
        currency = bank_match.group(3).upper()

    if not client_raw:
        client_match = _HEADER_CLIENT_CODE_RE.search(header_text)
        if client_match:
            client_raw = client_match.group(1)

    if not client_raw:
        acc_match = _HEADER_ACCOUNT_RE.search(header_text)
        if acc_match:
            client_raw = acc_match.group(1)

    account_fingerprint = build_account_fingerprint(client_raw)
    if not account_fingerprint:
        review_required = True
        if not review_reason:
            review_reason = "MISSING_ACCOUNT_IDENTITY"

    natural_key = build_natural_document_key(
        source_type=STOCKBIT_SOURCE_REGISTRY_ID,
        statement_type=statement_type,
        account_fingerprint=account_fingerprint,
        period_start=period_start,
        period_end=period_end,
    )

    if not natural_key:
        review_required = True
        if not review_reason:
            review_reason = "INCOMPLETE_DOCUMENT_IDENTITY"

    return StockbitDocumentIdentity(
        source_type=STOCKBIT_SOURCE_REGISTRY_ID,
        statement_type=statement_type,
        period_start=period_start,
        period_end=period_end,
        account_fingerprint=account_fingerprint,
        rdn_bank=rdn_bank,
        currency=currency,
        document_sha256=content_sha256,
        natural_document_key_candidate=natural_key,
        source_file_name=source_file_name,
        review_required=review_required,
        review_reason=review_reason,
    )


# ---------------------------------------------------------------------------
# Fail-Closed Event Handling
# ---------------------------------------------------------------------------

def handle_unrecognized_row(
    raw_text: str,
    row_ordinal: int,
    natural_document_key: str,
) -> tuple[SafeDiagnostic, str]:
    """Fail-closed handler for unrecognized events (e.g. corporate actions, dividends).

    Emits no invented financial meaning, marks review required, preserves sanitized evidence.
    """
    row_key, _, _ = build_row_evidence_key(
        natural_document_key=natural_document_key,
        evidence_role="UNRECOGNIZED_EVENT",
        transaction_date=None,
        source_description=raw_text[:100],
        source_row_ordinal=row_ordinal,
    )
    diag = SafeDiagnostic(
        code="UNRECOGNIZED_STOCKBIT_EVENT",
        severity=DiagnosticSeverity.WARNING,
        message="Unrecognized event encountered in statement; review required.",
        review_required=True,
    )
    return diag, row_key


# ---------------------------------------------------------------------------
# Stockbit SOA Universal Ingestion Adapter
# ---------------------------------------------------------------------------

class StockbitStatementAdapter:
    """Universal Ingestion adapter for Stockbit Statement of Account (SOA) PDFs."""

    def __init__(
        self,
        *,
        pdf_reader_factory: Callable[[BytesIO], PdfReader] | None = None,
    ) -> None:
        self._pdf_reader_factory = pdf_reader_factory or PdfReader

    @property
    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=STOCKBIT_ADAPTER_ID,
            source_registry_id=STOCKBIT_SOURCE_REGISTRY_ID,
            template_id=STOCKBIT_TEMPLATE_ID,
            parser_version=STOCKBIT_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
        )

    def parse(self, source: AdapterInput) -> AdapterResult:
        """Parses a Stockbit SOA document.

        In the P3.7.2 skeleton stage, extracts authoritative header metadata and returns
        deterministic document identity with empty evidence collections until P3.7.3.
        """
        validate_adapter_input(self.descriptor, source)

        diagnostics: list[SafeDiagnostic] = []
        review_reasons: list[str] = []

        # 1. Extract text from PDF payload
        header_text = ""
        full_text = ""

        if source.binary_payload is not None:
            try:
                reader = self._pdf_reader_factory(BytesIO(source.binary_payload))
                if not reader.pages:
                    return AdapterResult(
                        descriptor=self.descriptor,
                        source_document_id=source.source_document_id,
                        parse_status=AdapterParseStatus.FAILED,
                        period_status=PeriodStatus.UNKNOWN,
                        diagnostics=(
                            SafeDiagnostic(
                                code="STOCKBIT_EMPTY_PAYLOAD",
                                severity=DiagnosticSeverity.ERROR,
                                message="PDF document contains no pages.",
                            ),
                        ),
                    )
                header_text = reader.pages[0].extract_text() or ""
                full_text = "\n".join(
                    p.extract_text() or "" for p in reader.pages
                )
            except Exception:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    diagnostics=(
                        SafeDiagnostic(
                            code="STOCKBIT_PDF_READ_FAILED",
                            severity=DiagnosticSeverity.ERROR,
                            message="Failed to read PDF payload.",
                        ),
                    ),
                )
        elif source.text_payload is not None:
            header_text = source.text_payload[:3000]
            full_text = source.text_payload
        else:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="STOCKBIT_UNSUPPORTED_PAYLOAD",
                        severity=DiagnosticSeverity.ERROR,
                        message="Unsupported payload kind for Stockbit PDF adapter.",
                    ),
                ),
            )

        # 2. Validate template markers
        required_markers = (
            "Statement of Account",
            "Tr. Date",
            "Due Date",
            "Reference",
            "Db Amount",
            "Cr Amount",
            "Ending Balance",
        )
        missing_markers = [m for m in required_markers if m not in full_text]
        if missing_markers:
            diagnostics.append(
                SafeDiagnostic(
                    code="STOCKBIT_TEMPLATE_CONTENT_UNCONFIRMED",
                    severity=DiagnosticSeverity.ERROR,
                    message="Required template markers are missing from text layer.",
                )
            )
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=tuple(diagnostics),
            )

        # 3. Extract Document Identity (Header only)
        identity = extract_header_metadata(
            header_text,
            content_sha256=source.content_sha256,
        )

        if identity.review_required and identity.review_reason:
            review_reasons.append(identity.review_reason)
            diagnostics.append(
                SafeDiagnostic(
                    code=identity.review_reason,
                    severity=DiagnosticSeverity.WARNING,
                    message="Document header has review-required condition.",
                    review_required=True,
                )
            )

        period_status = (
            PeriodStatus.CLOSED
            if (identity.period_start and identity.period_end)
            else PeriodStatus.UNKNOWN
        )

        parse_status = (
            AdapterParseStatus.REVIEW_REQUIRED
            if identity.review_required
            else AdapterParseStatus.COMPLETED
        )

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=parse_status,
            period_status=period_status,
            events=(),
            diagnostics=tuple(diagnostics),
            review_reasons=tuple(review_reasons),
            period_start=identity.period_start,
            period_end=identity.period_end,
            natural_document_key_candidate=identity.natural_document_key_candidate,
        )
