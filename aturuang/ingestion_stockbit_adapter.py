"""Stockbit Statement of Account (SOA) PDF ingestion adapter for AturUang.

Phase 3.7 Full Table Parser and Evidence Extraction.
Parses multi-page Cash Ledger, Trade Evidence, Portfolio Holdings, and Valuation Summaries.
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
    CashMovementEvidence,
    ConfidenceLevel,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    InvestmentTradeEvidence,
    NormalizedEventEnvelope,
    SafeDiagnostic,
    SourceEventStatus,
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


@dataclass(frozen=True, init=False)
class StockbitCashEvidence:
    transaction_date: str
    due_date: str | None
    source_reference: str | None
    source_description: str
    source_debit: Decimal | None
    source_credit: Decimal | None
    signed_amount: Decimal
    source_ending_balance: Decimal | None
    source_row_ordinal: int
    row_evidence_key: str
    source_entry_type: str = "TRADE_ACCRUAL"
    source_action: str = ""
    owner_cash_direction: str | None = None
    owner_cash_signed_amount: Decimal | None = None
    cash_movement_candidate: bool = False
    requires_cross_source_match: bool = False
    review_reason: str | None = None
    evidence_role: str = StockbitEvidenceRole.CASH_EVIDENCE.value

    def __init__(
        self,
        transaction_date: str,
        due_date: str | None,
        source_reference: str | None,
        source_description: str,
        source_debit: Decimal | None,
        source_credit: Decimal | None,
        signed_amount: Decimal,
        source_ending_balance: Decimal | None = None,
        source_row_ordinal: int = 0,
        row_evidence_key: str = "",
        source_entry_type: str = "TRADE_ACCRUAL",
        source_action: str = "",
        owner_cash_direction: str | None = None,
        owner_cash_signed_amount: Decimal | None = None,
        cash_movement_candidate: bool = False,
        requires_cross_source_match: bool = False,
        review_reason: str | None = None,
        evidence_role: str = StockbitEvidenceRole.CASH_EVIDENCE.value,
        running_balance: Decimal | None = None,
    ) -> None:
        if source_ending_balance is None and running_balance is not None:
            source_ending_balance = running_balance
        object.__setattr__(self, "transaction_date", transaction_date)
        object.__setattr__(self, "due_date", due_date)
        object.__setattr__(self, "source_reference", source_reference)
        object.__setattr__(self, "source_description", source_description)
        object.__setattr__(self, "source_debit", source_debit)
        object.__setattr__(self, "source_credit", source_credit)
        object.__setattr__(self, "signed_amount", signed_amount)
        object.__setattr__(self, "source_ending_balance", source_ending_balance)
        object.__setattr__(self, "source_row_ordinal", source_row_ordinal)
        object.__setattr__(self, "row_evidence_key", row_evidence_key)
        object.__setattr__(self, "source_entry_type", source_entry_type)
        object.__setattr__(self, "source_action", source_action)
        object.__setattr__(self, "owner_cash_direction", owner_cash_direction)
        object.__setattr__(self, "owner_cash_signed_amount", owner_cash_signed_amount)
        object.__setattr__(self, "cash_movement_candidate", cash_movement_candidate)
        object.__setattr__(self, "requires_cross_source_match", requires_cross_source_match)
        object.__setattr__(self, "review_reason", review_reason)
        object.__setattr__(self, "evidence_role", evidence_role)

    @property
    def running_balance(self) -> Decimal | None:
        return self.source_ending_balance


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


@dataclass(frozen=True)
class StockbitAdapterResult(AdapterResult):
    cash_evidence: tuple[StockbitCashEvidence, ...] = ()
    trade_evidence: tuple[StockbitTradeEvidence, ...] = ()
    holding_snapshots: tuple[StockbitHoldingSnapshot, ...] = ()
    portfolio_valuation: StockbitPortfolioValuation | None = None
    opening_balance: Decimal | None = None
    ending_balance: Decimal | None = None
    total_debits: Decimal | None = None
    total_credits: Decimal | None = None
    document_reconciliation_status: str = "DETAIL_EXACT"
    detail_difference: Decimal = Decimal("0.00")
    source_total_control_matched: bool = True
    undue_trading_control_matched: bool = True


# ---------------------------------------------------------------------------
# Identity and Parsing Helpers
# ---------------------------------------------------------------------------

def normalize_account_string(value: str) -> str:
    """Normalizes an account or client code string by stripping whitespace and punctuation."""
    return re.sub(r"[^A-Za-z0-9]", "", str(value).strip().upper())


def build_account_fingerprint(raw_account_or_client: str | None) -> str | None:
    """Constructs a stable, private SHA-256 account fingerprint."""
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
    """Constructs the deterministic natural document key for a Stockbit SOA."""
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
    source_ending_balance: Decimal | None = None,
    running_balance: Decimal | None = None,
    source_row_ordinal: int | None = None,
) -> tuple[str, bool, str | None]:
    """Constructs a deterministic row-level evidence key."""
    if source_ending_balance is None and running_balance is not None:
        source_ending_balance = running_balance
    has_stable_core = bool(
        transaction_date
        and source_description
        and signed_amount is not None
        and (source_reference or source_ending_balance is not None)
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
                str(source_ending_balance) if source_ending_balance is not None else "",
            )
        )
        return sha256(payload.encode("utf-8")).hexdigest(), False, None

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


def _parse_decimal_amount(value_str: str | None) -> Decimal:
    """Parses currency/number strings supporting commas and parentheses for negative values."""
    if not value_str:
        return Decimal("0.00")
    s = value_str.strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    elif s.startswith("-"):
        neg = True
        s = s[1:].strip()
    s = s.replace(",", "")
    val = Decimal(s)
    return -val if neg else val


# ---------------------------------------------------------------------------
# Header Extraction and Parsing
# ---------------------------------------------------------------------------

_HEADER_BOUNDARY_RE = re.compile(
    r"(?mi)^(?:.*?\bTr\.\s*Date\b|.*?\bDue\s*Date\b|.*?\bPORTFOLIO\s+STATEMENT\b|.*?\bShare\s+Code\b)"
)

_HEADER_PERIOD_RE = re.compile(
    r"\b(?:Period|Date)\s*[:\s]+(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})\b",
    re.IGNORECASE,
)
_HEADER_BANK_SID_RE = re.compile(
    r"\bBank\s*/\s*(?:Ccy|Currency)\s*/?\s*SID\b[:\s]*[\r\n]*\s*([A-Za-z]+)\s+([A-Za-z0-9_.-]+)\s*/\s*([A-Za-z]+)\s+([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_HEADER_ACCOUNT_RE = re.compile(
    r"\bAccount\s*/\s*Sub\s*Account\s*[:\s]*([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_HEADER_CLIENT_CODE_RE = re.compile(
    r"\bClient(?:\s*Code)?\s*[:\s]+([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

_TRADE_DETAIL_RE = re.compile(
    r"^\s*(\d{6,8})\s+([BS]):\s*RG\s+([A-Za-z0-9]+)\s+([\d,]+(?:\.\d+)?)\s*@\s*([\d,]+(?:\.\d+)?)\s*=\s*([\d,]+(?:\.\d+)?)"
)

_PORTFOLIO_NUM_RE = re.compile(
    r"(?:([A-Z])\s+)?([\d,]+)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([-\(]?[\d,]+(?:\.\d+)?\)?)\s+([-\(]?[\d,]+(?:\.\d+)?\)?)\s+([-\(]?[\d,]+(?:\.\d+)?\)?)\s+([-\(]?[\d,]+(?:\.\d+)?\)?)$"
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
    """Extracts authoritative document identity from header text only."""
    review_required = False
    review_reason = None

    # Strictly bound the header region before the first statement section / table marker
    m_bound = _HEADER_BOUNDARY_RE.search(header_text)
    bounded_header = header_text[:m_bound.start()] if m_bound else header_text

    # 1. Authoritative Period from Header
    period_match = _HEADER_PERIOD_RE.search(bounded_header)
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

    bank_match = _HEADER_BANK_SID_RE.search(bounded_header)
    if bank_match:
        rdn_bank = bank_match.group(1).upper()
        client_raw = bank_match.group(2)
        currency = bank_match.group(3).upper()

    if not client_raw:
        client_match = _HEADER_CLIENT_CODE_RE.search(bounded_header)
        if client_match:
            client_raw = client_match.group(1)

    if not client_raw:
        acc_match = _HEADER_ACCOUNT_RE.search(bounded_header)
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


def extract_portfolio_valuation(
    bounded_header: str,
    valuation_date: str | None = None,
) -> StockbitPortfolioValuation | None:
    """Extracts the portfolio valuation summary totals from the header block."""
    patterns = {
        "cash_investor": r"\bCash\s+Investor\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
        "cash_balance": r"\bCash\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
        "undue_trading": r"\bUndue\s+Trading\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
        "short_sell": r"\bShort\s+Sell\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
        "portfolio_value": r"\bPortfolio\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
        "equity_or_nav": r"\bEquity\s+(?:NAB|NAV)\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
        "available_limit": r"\bAvail(?:able)?\s+Limit\s+([-\(]?[\d,]+(?:\.\d+)?\)?|\b0\b)",
    }
    values: dict[str, Decimal | None] = {}
    found_any = False
    for k, pat in patterns.items():
        m = re.search(pat, bounded_header, re.IGNORECASE)
        if m:
            values[k] = _parse_decimal_amount(m.group(1))
            found_any = True
        else:
            values[k] = None

    if not found_any:
        return None

    return StockbitPortfolioValuation(
        valuation_date=valuation_date,
        cash_investor=values["cash_investor"],
        cash_balance=values["cash_balance"],
        undue_trading=values["undue_trading"],
        short_sell=values["short_sell"],
        portfolio_value=values["portfolio_value"],
        equity_or_nav=values["equity_or_nav"],
        available_limit=values["available_limit"],
    )


# ---------------------------------------------------------------------------
# Fail-Closed Event Handling
# ---------------------------------------------------------------------------

def handle_unrecognized_row(
    raw_text: str,
    row_ordinal: int,
    natural_document_key: str,
) -> tuple[SafeDiagnostic, str]:
    """Fail-closed handler for unrecognized events (e.g. corporate actions, dividends)."""
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

    def parse(self, source: AdapterInput) -> StockbitAdapterResult:
        """Parses a Stockbit SOA document into full structured evidence."""
        validate_adapter_input(self.descriptor, source)

        diagnostics: list[SafeDiagnostic] = []
        review_reasons: list[str] = []

        # 1. Extract text per page
        pages_text: list[str] = []
        full_text = ""

        if source.binary_payload is not None:
            try:
                reader = self._pdf_reader_factory(BytesIO(source.binary_payload))
                if not reader.pages:
                    return StockbitAdapterResult(
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
                pages_text = [p.extract_text() or "" for p in reader.pages]
                full_text = "\n".join(pages_text)
            except Exception:
                return StockbitAdapterResult(
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
            pages_text = [source.text_payload]
            full_text = source.text_payload
        else:
            return StockbitAdapterResult(
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
        )
        missing_markers = [m for m in required_markers if m not in full_text]
        if "Ending Balance" not in full_text and "Balance" not in full_text:
            missing_markers.append("Balance")

        if missing_markers:
            diagnostics.append(
                SafeDiagnostic(
                    code="STOCKBIT_TEMPLATE_CONTENT_UNCONFIRMED",
                    severity=DiagnosticSeverity.ERROR,
                    message="Required template markers are missing from text layer.",
                )
            )
            return StockbitAdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=tuple(diagnostics),
            )

        # 3. Extract Document Identity (Header only, strictly bounded)
        first_page = pages_text[0] if pages_text else ""
        m_bound = _HEADER_BOUNDARY_RE.search(first_page)
        bounded_header = first_page[:m_bound.start()] if m_bound else first_page

        identity = extract_header_metadata(
            bounded_header,
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

        doc_key = identity.natural_document_key_candidate or f"stockbit_fallback:{source.content_sha256}"

        # 4. Extract Portfolio Valuation Summary from Header
        portfolio_valuation = extract_portfolio_valuation(
            bounded_header,
            valuation_date=identity.period_end,
        )

        # 5. Parse Cash Ledger across pages
        idx_port = full_text.find("PORTFOLIO STATEMENT")
        cash_text = full_text[:idx_port] if idx_port != -1 else full_text
        port_text = full_text[idx_port:] if idx_port != -1 else ""

        cash_lines = [l.strip() for l in cash_text.splitlines() if l.strip()]

        cash_evidence_list: list[StockbitCashEvidence] = []
        trade_evidence_list: list[StockbitTradeEvidence] = []
        opening_balance: Decimal | None = None
        source_ending_balance: Decimal | None = None
        parsed_total_debits: Decimal | None = None
        parsed_total_credits: Decimal | None = None

        cur_trade_date: str | None = None
        cur_due_date: str | None = None
        row_ordinal = 0

        for line in cash_lines:
            if "Beginning Balance" in line:
                m_tx = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.+)$", line)
                rest_beg = m_tx.group(3) if m_tx else line
                nums_beg = [x for x in rest_beg.split() if re.match(r"^[-\(]?[\d,]+(?:\.\d+)?\)?$", x)]
                if len(nums_beg) >= 3:
                    opening_balance = _parse_decimal_amount(nums_beg[2])
                elif nums_beg:
                    opening_balance = _parse_decimal_amount(nums_beg[-1])
                continue

            m_end_bal = re.search(r"Ending\s+Balance\s*[:\s]+\s*([-\(]?[\d,]+(?:\.\d+)?\)?)", line, re.I)
            if m_end_bal and not line.startswith("Tr. Date"):
                source_ending_balance = _parse_decimal_amount(m_end_bal.group(1))
                continue

            if line.startswith("Tr. Date") and "Due Date" in line:
                continue

            if line.startswith("T O T A L"):
                m_t = re.findall(r"[-\(]?[\d,]+(?:\.\d+)?\)?", line)
                if len(m_t) >= 3:
                    parsed_total_debits = _parse_decimal_amount(m_t[0])
                    parsed_total_credits = _parse_decimal_amount(m_t[1])
                    if source_ending_balance is None:
                        source_ending_balance = _parse_decimal_amount(m_t[2])
                continue

            if line.startswith("Total - Interest"):
                continue

            m_tr = _TRADE_DETAIL_RE.match(line)
            if m_tr:
                ref, side, ticker, qty_str, price_str, net_str = m_tr.groups()
                action = "BUY" if side == "B" else "SELL"
                qty = _parse_decimal_amount(qty_str)
                price = _parse_decimal_amount(price_str)
                net_amount = _parse_decimal_amount(net_str)
                gross_amount = (qty * price).quantize(Decimal("0.01"))

                trade_ev = StockbitTradeEvidence(
                    trade_date=cur_trade_date or (identity.period_start or ""),
                    settlement_date=cur_due_date or cur_trade_date or (identity.period_end or ""),
                    contract_reference=ref,
                    source_action=action,
                    ticker=ticker,
                    quantity=qty,
                    price=price,
                    gross_amount=gross_amount,
                    net_settlement_amount=net_amount,
                )
                trade_evidence_list.append(trade_ev)
                continue

            m_tx = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.+)$", line)
            if m_tx:
                tr_raw, due_raw, rest = m_tx.group(1), m_tx.group(2), m_tx.group(3).strip()
                tr_date = _format_date(tr_raw)
                due_date = _format_date(due_raw)
                cur_trade_date = tr_date
                cur_due_date = due_date
                row_ordinal += 1

                m_trx = re.match(r"^I\s+(Trx\s+on\s+\d{2}/\d{2}/\d{4})\s+([\d,\(\)\-\s]+)$", rest)
                m_pay = re.match(r"^P\s+(\d+)\s+(Payment\s+to:\s*.+?)\s+([\d,\(\)\-\s]+)$", rest, re.I)
                m_rec = re.match(r"^R\s+(\d+)\s+(Receipt\s+From:\s*.+?)\s+([\d,\(\)\-\s]+)$", rest, re.I)
                m_inv = re.match(r"^([PR])\s+(\d+)\s+(Inv:\s*.*?)\.\s+([\d,\(\)\-\s]+)$", rest)
                m_feed = re.match(r"^([MR])\s+(?:(\d+)\s+)?(Biaya\s+Datafeed\s+[A-Za-z]+\s+\d{4})\s+([\d,\(\)\-\s]+)$", rest)
                m_int = re.match(r"^Z\s+Z\s+(Estimated\s+Interest)\s+([\d,\(\)\-\s]+)$", rest)

                ref_val: str | None = None
                desc_val: str = rest
                db_val = Decimal("0.00")
                cr_val = Decimal("0.00")
                bal_val: Decimal | None = None

                source_entry_type = "UNRECOGNIZED_STOCKBIT_EVENT"
                source_action = ""
                owner_cash_direction = None
                owner_cash_signed_amount = None
                cash_movement_candidate = False
                requires_cross_source_match = False

                if m_trx:
                    source_entry_type = "TRADE_ACCRUAL"
                    source_action = "TRADE_ACCRUAL"
                    ref_val = "I"
                    desc_val = m_trx.group(1)
                    nums = [_parse_decimal_amount(x) for x in m_trx.group(2).split()]
                    if len(nums) >= 3:
                        db_val, cr_val, bal_val = nums[0], nums[1], nums[2]
                elif m_pay:
                    source_entry_type = "BROKER_PAYMENT_TO_OWNER"
                    source_action = "PAYMENT"
                    ref_val = f"P {m_pay.group(1)}"
                    desc_val = m_pay.group(2).strip()
                    nums = [_parse_decimal_amount(x) for x in m_pay.group(3).split()]
                    if len(nums) >= 3:
                        db_val, cr_val, bal_val = nums[0], nums[1], nums[2]
                    cash_movement_candidate = True
                    requires_cross_source_match = True
                    owner_cash_direction = "IN"
                    owner_cash_signed_amount = db_val if db_val > 0 else (cr_val if cr_val > 0 else Decimal("0.00"))
                elif m_rec:
                    source_entry_type = "BROKER_RECEIPT_FROM_OWNER"
                    source_action = "RECEIPT"
                    ref_val = f"R {m_rec.group(1)}"
                    desc_val = m_rec.group(2).strip()
                    nums = [_parse_decimal_amount(x) for x in m_rec.group(3).split()]
                    if len(nums) >= 3:
                        db_val, cr_val, bal_val = nums[0], nums[1], nums[2]
                    cash_movement_candidate = True
                    requires_cross_source_match = True
                    owner_cash_direction = "OUT"
                    owner_cash_signed_amount = -(cr_val if cr_val > 0 else (db_val if db_val > 0 else Decimal("0.00")))
                elif m_inv:
                    source_entry_type = "INVOICE_SETTLEMENT"
                    source_action = "INVOICE_SETTLEMENT"
                    inv_pref = m_inv.group(1).upper()
                    ref_val = f"{inv_pref} {m_inv.group(2)}"
                    desc_val = m_inv.group(3).strip() + "."
                    nums = [_parse_decimal_amount(x) for x in m_inv.group(4).split()]
                    if len(nums) >= 3:
                        db_val, cr_val, bal_val = nums[0], nums[1], nums[2]
                    cash_movement_candidate = True
                    requires_cross_source_match = True
                    if inv_pref == "P":
                        owner_cash_direction = "IN"
                        owner_cash_signed_amount = db_val if db_val > 0 else (cr_val if cr_val > 0 else Decimal("0.00"))
                    else:
                        owner_cash_direction = "OUT"
                        owner_cash_signed_amount = -(cr_val if cr_val > 0 else (db_val if db_val > 0 else Decimal("0.00")))
                elif m_feed:
                    feed_pref = m_feed.group(1).upper()
                    feed_ref_num = m_feed.group(2) or ""
                    ref_val = f"{feed_pref} {feed_ref_num}".strip()
                    desc_val = m_feed.group(3).strip()
                    nums = [_parse_decimal_amount(x) for x in m_feed.group(4).split()]
                    if len(nums) >= 3:
                        db_val, cr_val, bal_val = nums[0], nums[1], nums[2]
                    if feed_pref == "M":
                        source_entry_type = "DATAFEED_FEE"
                        source_action = "DATAFEED_FEE"
                    else:
                        source_entry_type = "DATAFEED_SETTLEMENT"
                        source_action = "DATAFEED_SETTLEMENT"
                    cash_movement_candidate = False
                    requires_cross_source_match = False
                elif m_int:
                    source_entry_type = "ESTIMATED_INTEREST"
                    source_action = "ESTIMATED_INTEREST"
                    ref_val = "Z"
                    desc_val = m_int.group(1)
                    nums = [_parse_decimal_amount(x) for x in m_int.group(2).split()]
                    if len(nums) >= 3:
                        db_val, cr_val, bal_val = nums[0], nums[1], nums[2]
                    cash_movement_candidate = False
                    requires_cross_source_match = False
                else:
                    diag, row_key = handle_unrecognized_row(line, row_ordinal, doc_key)
                    diagnostics.append(diag)
                    review_reasons.append("UNRECOGNIZED_STOCKBIT_EVENT")
                    continue

                signed_amount = cr_val - db_val
                row_key, is_fallback, r_reason = build_row_evidence_key(
                    natural_document_key=doc_key,
                    evidence_role="CASH_EVIDENCE",
                    transaction_date=tr_date,
                    due_date=due_date,
                    source_reference=ref_val,
                    source_description=desc_val,
                    signed_amount=signed_amount,
                    source_ending_balance=bal_val,
                    source_row_ordinal=row_ordinal,
                )

                cash_ev = StockbitCashEvidence(
                    transaction_date=tr_date,
                    due_date=due_date,
                    source_reference=ref_val,
                    source_description=desc_val,
                    source_debit=db_val if db_val > 0 else None,
                    source_credit=cr_val if cr_val > 0 else None,
                    signed_amount=signed_amount,
                    source_ending_balance=bal_val,
                    source_row_ordinal=row_ordinal,
                    row_evidence_key=row_key,
                    source_entry_type=source_entry_type,
                    source_action=source_action,
                    owner_cash_direction=owner_cash_direction,
                    owner_cash_signed_amount=owner_cash_signed_amount,
                    cash_movement_candidate=cash_movement_candidate,
                    requires_cross_source_match=requires_cross_source_match,
                    review_reason=r_reason,
                )
                cash_evidence_list.append(cash_ev)

        # 6. Parse Portfolio Statement Holdings
        holding_snapshots_list: list[StockbitHoldingSnapshot] = []
        if port_text:
            m_start = re.search(r"Stocks\s+Special\s+Notes.*?\n", port_text, re.I)
            if m_start:
                tbl = port_text[m_start.end():]
                m_end = re.search(r"T\s+O\s+T\s+A\s+L", tbl)
                if m_end:
                    tbl = tbl[:m_end.start()]
                p_lines = [l.strip() for l in tbl.splitlines() if l.strip()]
                filt = [
                    l for l in p_lines
                    if "Stocks Special Notes" not in l
                    and "PRICE UNREAL" not in l
                    and "PORTFOLIO STATEMENT" not in l
                ]
                cur_text: list[str] = []
                for l in filt:
                    m_n = _PORTFOLIO_NUM_RE.search(l)
                    if m_n:
                        prefix = l[:m_n.start()].strip()
                        if prefix:
                            cur_text.append(prefix)
                        qty = _parse_decimal_amount(m_n.group(2))
                        buy_p = _parse_decimal_amount(m_n.group(3))
                        close_p = _parse_decimal_amount(m_n.group(4))
                        buy_v = _parse_decimal_amount(m_n.group(5))
                        mkt_v = _parse_decimal_amount(m_n.group(6))
                        unreal_v = _parse_decimal_amount(m_n.group(7))
                        unreal_p = _parse_decimal_amount(m_n.group(8))

                        full_desc = " ".join(cur_text)
                        tokens = full_desc.split()
                        if not tokens:
                            ticker = "UNKNOWN"
                            name_str = None
                        elif len(tokens) > 1 and len(tokens[0]) <= 5 and len(tokens[1]) <= 5 and tokens[1].isupper() and tokens[0].isupper() and "ARTOB" in tokens[0]:
                            ticker = tokens[0] + tokens[1]
                            name_str = " ".join(tokens[2:])
                        else:
                            ticker = tokens[0]
                            name_str = " ".join(tokens[1:]) if len(tokens) > 1 else None

                        snap = StockbitHoldingSnapshot(
                            snapshot_date=identity.period_end or "",
                            ticker=ticker,
                            source_security_name=name_str,
                            quantity=qty,
                            average_price=buy_p,
                            closing_price=close_p,
                            market_value=mkt_v,
                            source_unrealized_gain_loss=unreal_v,
                            source_unrealized_percentage=unreal_p,
                        )
                        holding_snapshots_list.append(snap)
                        cur_text = []
                    else:
                        cur_text.append(l)

        # 7. Broker Cash Reconciliation & Controls
        detail_total_debits = sum((c.source_debit or Decimal("0.00")) for c in cash_evidence_list)
        detail_total_credits = sum((c.source_credit or Decimal("0.00")) for c in cash_evidence_list)

        doc_total_debits = parsed_total_debits if parsed_total_debits is not None else detail_total_debits
        doc_total_credits = parsed_total_credits if parsed_total_credits is not None else detail_total_credits
        doc_ending_balance = source_ending_balance if source_ending_balance is not None else (opening_balance or Decimal("0.00"))

        if not cash_evidence_list and parsed_total_debits is None:
            source_total_control_matched = True
            undue_trading_control_matched = True
            doc_reconciliation_status = "DETAIL_EXACT"
            detail_diff = Decimal("0.00")
        else:
            # P3.7.3-R1.2: source_total_debit - source_total_credit == source_ending_balance
            source_total_control_matched = (doc_total_debits - doc_total_credits == doc_ending_balance)

            # header_undue_trading == -source_ending_balance
            undue_trading_control_matched = True
            if portfolio_valuation and portfolio_valuation.undue_trading is not None and doc_ending_balance is not None:
                undue_trading_control_matched = (portfolio_valuation.undue_trading == -doc_ending_balance)

            # P3.7.3-R1.3: Detail difference = (detail_debits - detail_credits) - doc_ending_balance
            detail_diff = (detail_total_debits - detail_total_credits) - doc_ending_balance

            if not source_total_control_matched:
                doc_reconciliation_status = "CASH_RECONCILIATION_MISMATCH"
                review_reasons.append("CASH_RECONCILIATION_MISMATCH")
                diagnostics.append(
                    SafeDiagnostic(
                        code="CASH_RECONCILIATION_MISMATCH",
                        severity=DiagnosticSeverity.WARNING,
                        message="Broker source total control mismatch between debits, credits, and ending balance.",
                        review_required=True,
                    )
                )
            elif detail_diff == Decimal("0.00"):
                doc_reconciliation_status = "DETAIL_EXACT"
            elif abs(detail_diff) <= Decimal("1.00"):
                doc_reconciliation_status = "SOURCE_ROUNDING_DIFFERENCE"
                diagnostics.append(
                    SafeDiagnostic(
                        code="SOURCE_ROUNDING_DIFFERENCE",
                        severity=DiagnosticSeverity.INFO,
                        message="Non-blocking source rounding difference between ledger details and total balance.",
                        review_required=False,
                    )
                )
            else:
                doc_reconciliation_status = "CASH_RECONCILIATION_MISMATCH"
                review_reasons.append("CASH_RECONCILIATION_MISMATCH")
                diagnostics.append(
                    SafeDiagnostic(
                        code="CASH_RECONCILIATION_MISMATCH",
                        severity=DiagnosticSeverity.WARNING,
                        message="Cash reconciliation mismatch: detail difference exceeds allowable tolerance.",
                        review_required=True,
                    )
                )

            if not undue_trading_control_matched:
                review_reasons.append("UNDUE_TRADING_CONTROL_MISMATCH")
                diagnostics.append(
                    SafeDiagnostic(
                        code="UNDUE_TRADING_CONTROL_MISMATCH",
                        severity=DiagnosticSeverity.WARNING,
                        message="Header undue trading does not match negative ending balance.",
                        review_required=True,
                    )
                )

        # 8. Build Normalized Event Envelopes
        events: list[NormalizedEventEnvelope] = []
        for c in cash_evidence_list:
            if not c.cash_movement_candidate:
                continue

            if c.owner_cash_direction == "IN":
                direction = EventDirection.INFLOW
                amt = c.owner_cash_signed_amount if c.owner_cash_signed_amount is not None else Decimal("0.00")
            elif c.owner_cash_direction == "OUT":
                direction = EventDirection.OUTFLOW
                amt = abs(c.owner_cash_signed_amount) if c.owner_cash_signed_amount is not None else Decimal("0.00")
            else:
                direction = EventDirection.NEUTRAL
                amt = Decimal("0.00")

            payload = CashMovementEvidence(
                amount=amt,
                currency="IDR",
                direction=direction,
                status=SourceEventStatus.POSTED,
                occurred_at=c.transaction_date,
                posted_at=c.transaction_date,
                settlement_date=c.due_date,
                direction_raw=c.owner_cash_direction,
                description_raw=c.source_description,
                reference_raw=c.source_reference,
                balance_after=c.source_ending_balance,
            )
            provenance = SourceProvenanceContract(
                source_document_id=source.source_document_id,
                raw_locator=f"stockbit:cash:ord:{c.source_row_ordinal}",
                row_index=c.source_row_ordinal,
                raw_text=c.source_description,
                raw_reference=c.source_reference or "",
                raw_description=c.source_description,
            )
            envelope = NormalizedEventEnvelope(
                source_document_id=source.source_document_id,
                source_registry_id=self.descriptor.source_registry_id,
                template_id=self.descriptor.template_id,
                parser_version=self.descriptor.parser_version,
                source_channel=self.descriptor.source_channel,
                event_role=EventRole.CASH_MOVEMENT,
                source_event_id=None,
                row_fingerprint=c.row_evidence_key,
                evidence_quality=ConfidenceLevel.HIGH,
                parse_confidence=ConfidenceLevel.HIGH,
                provenance=provenance,
                payload=payload,
            )
            events.append(envelope)

        for t in trade_evidence_list:
            t_payload = InvestmentTradeEvidence(
                instrument_raw=t.ticker,
                trade_date=t.trade_date,
                settlement_date=t.settlement_date or t.trade_date,
                side_raw=t.source_action,
                quantity=t.quantity,
                unit_price=t.price,
                currency="IDR",
                gross_amount=t.gross_amount,
                net_amount=t.net_settlement_amount,
                reference_raw=t.contract_reference,
            )
            t_key = sha256(
                "\x1f".join(
                    (
                        "stockbit-trade-v1",
                        doc_key,
                        "TRADE_EVIDENCE",
                        t.trade_date,
                        t.settlement_date or "",
                        t.contract_reference or "",
                        t.source_action,
                        t.ticker,
                        str(t.quantity),
                        str(t.price),
                        str(t.net_settlement_amount),
                    )
                ).encode("utf-8")
            ).hexdigest()
            t_provenance = SourceProvenanceContract(
                source_document_id=source.source_document_id,
                raw_locator=f"stockbit:trade:ref:{t.contract_reference or 'unknown'}",
                raw_text=f"{t.source_action} {t.ticker} {t.quantity} @ {t.price}",
                raw_reference=t.contract_reference or "",
            )
            t_envelope = NormalizedEventEnvelope(
                source_document_id=source.source_document_id,
                source_registry_id=self.descriptor.source_registry_id,
                template_id=self.descriptor.template_id,
                parser_version=self.descriptor.parser_version,
                source_channel=self.descriptor.source_channel,
                event_role=EventRole.INVESTMENT_TRADE,
                source_event_id=None,
                row_fingerprint=t_key,
                evidence_quality=ConfidenceLevel.HIGH,
                parse_confidence=ConfidenceLevel.HIGH,
                provenance=t_provenance,
                payload=t_payload,
            )
            events.append(t_envelope)

        period_status = (
            PeriodStatus.CLOSED
            if (identity.period_start and identity.period_end)
            else PeriodStatus.UNKNOWN
        )

        has_review = bool(
            identity.review_required
            or any(d.review_required for d in diagnostics)
        )

        parse_status = (
            AdapterParseStatus.REVIEW_REQUIRED
            if has_review
            else AdapterParseStatus.COMPLETED
        )

        return StockbitAdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=parse_status,
            period_status=period_status,
            events=tuple(events),
            diagnostics=tuple(diagnostics),
            review_reasons=tuple(dict.fromkeys(review_reasons)),
            period_start=identity.period_start,
            period_end=identity.period_end,
            natural_document_key_candidate=identity.natural_document_key_candidate,
            cash_evidence=tuple(cash_evidence_list),
            trade_evidence=tuple(trade_evidence_list),
            holding_snapshots=tuple(holding_snapshots_list),
            portfolio_valuation=portfolio_valuation,
            opening_balance=opening_balance,
            ending_balance=doc_ending_balance,
            total_debits=doc_total_debits,
            total_credits=doc_total_credits,
            document_reconciliation_status=doc_reconciliation_status,
            detail_difference=detail_diff,
            source_total_control_matched=source_total_control_matched,
            undue_trading_control_matched=undue_trading_control_matched,
        )
