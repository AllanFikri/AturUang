from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
from typing import Sequence

from pypdf import PdfReader

from .ingestion_adapter import (
    AdapterContractError,
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    AdapterResult,
    CashMovementEvidence,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    NormalizedEventEnvelope,
    ObservedAccountEvidence,
    PaymentComponentEvidence,
    SafeDiagnostic,
    SourceEventStatus,
    SourceSummaryEvidence,
    UniversalSourceAdapter,
    validate_adapter_input,
)
from .ingestion_contracts import (
    ConfidenceLevel,
    PeriodStatus,
    SourceChannel,
    SourceProvenanceContract,
)

GOPAY_SOURCE_REGISTRY_ID = "gopay_statement"
GOPAY_TEMPLATE_ID = "gopay_estatement_v1"
GOPAY_PARSER_VERSION = "parser-v1"
GOPAY_ADAPTER_ID = "gopay-estatement-v1"
GOPAY_TEMPLATE_FINGERPRINT = (
    "6dcc7e3f14d52f0dc3452513fd8e7fcd8fe0fda2f725f3ae4eba0ae5ea477d48"
)

_MONTHS: dict[str, int] = {
    "JANUARI": 1,
    "FEBRUARI": 2,
    "MARET": 3,
    "APRIL": 4,
    "MEI": 5,
    "JUNI": 6,
    "JULI": 7,
    "AGUSTUS": 8,
    "SEPTEMBER": 9,
    "OKTOBER": 10,
    "NOVEMBER": 11,
    "DESEMBER": 12,
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "OCTOBER": 10,
    "DECEMBER": 12,
}

_PHONE_RE = re.compile(r"^\+62\d{8,13}$")
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")


def _parse_decimal(text: str) -> Decimal:
    cleaned = re.sub(r"[^\d,.-]", "", text).strip()
    is_neg = False
    if cleaned.startswith("-"):
        is_neg = True
        cleaned = cleaned[1:].strip()
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        cleaned = cleaned.replace(".", "")
    try:
        val = Decimal(cleaned)
        if not val.is_finite():
            raise AdapterContractError("non-finite decimal")
        return -val if is_neg else val
    except (InvalidOperation, ValueError) as exc:
        raise AdapterContractError("invalid decimal format") from exc


def _parse_date_str(s: str) -> str:
    parts = s.strip().split()
    day = int(parts[0])
    mon_name = parts[1].upper()
    if mon_name not in _MONTHS:
        raise AdapterContractError(f"unrecognized month: {mon_name}")
    mon = _MONTHS[mon_name]
    yr = int(parts[2])
    date_obj = date(yr, mon, day)
    return date_obj.isoformat()


@dataclass(frozen=True)
class _GoPaySummary:
    period_str: str
    period_start: str
    period_end: str
    incoming_total: Decimal
    outgoing_total: Decimal
    coins_got: int = 0
    coins_used: int = 0
    account_number: str | None = field(default=None, repr=False)
    account_holder: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _ParsedGoPayRow:
    occurred_at: str
    direction: EventDirection
    direction_raw: str
    amount: Decimal
    gross_amount: Decimal
    description_raw: str = field(repr=False)
    provider_transaction_id_raw: str | None = field(default=None, repr=False)
    payment_method_raw: str | None = field(default=None, repr=False)
    is_coins_loyalty: bool = False
    is_split_payment: bool = False
    payment_components: tuple[PaymentComponentEvidence, ...] = ()


class GoPayEStatementAdapter(UniversalSourceAdapter):
    descriptor: AdapterDescriptor = AdapterDescriptor(
        adapter_id=GOPAY_ADAPTER_ID,
        source_registry_id=GOPAY_SOURCE_REGISTRY_ID,
        template_id=GOPAY_TEMPLATE_ID,
        parser_version=GOPAY_PARSER_VERSION,
        source_channel=SourceChannel.PDF,
    )

    def _extract_account_candidates(
        self, lines: Sequence[str]
    ) -> str | None:
        if len(lines) > 2:
            candidate = lines[2].strip()
            if _PHONE_RE.match(candidate):
                return candidate
        return None

    def _extract_document_summary(
        self, lines: Sequence[str]
    ) -> tuple[_GoPaySummary | None, list[SafeDiagnostic]]:
        if not lines:
            return None, [
                SafeDiagnostic(
                    code="GOPAY_STRUCTURE_TRUNCATED",
                    message="Document is empty",
                    severity=DiagnosticSeverity.ERROR,
                )
            ]

        m_period = re.search(
            r"Periode transaksi\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*-\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            lines[0],
        )
        if not m_period:
            return None, [
                SafeDiagnostic(
                    code="GOPAY_PERIOD_AMBIGUOUS",
                    message="Missing or unparseable transaction period",
                    severity=DiagnosticSeverity.ERROR,
                )
            ]

        try:
            p_start = _parse_date_str(m_period.group(1))
            p_end = _parse_date_str(m_period.group(2))
        except Exception:
            return None, [
                SafeDiagnostic(
                    code="GOPAY_PERIOD_AMBIGUOUS",
                    message="Failed to parse statement period dates",
                    severity=DiagnosticSeverity.ERROR,
                )
            ]

        period_str = p_start[:7]

        # Extract identity strictly from lines[2] slot
        acc_num = self._extract_account_candidates(lines)
        acc_holder = (
            lines[1].strip()
            if len(lines) > 1 and lines[1].strip() != acc_num
            else None
        )

        coins_got = 0
        coins_used = 0
        inflow = None
        outflow = None

        for i, l in enumerate(lines):
            if "Total Coins didapatkan" in l and i + 1 < len(lines):
                try:
                    coins_got = int(lines[i + 1].strip())
                except ValueError:
                    pass
            elif "Total Coins dipakai" in l and i + 1 < len(lines):
                try:
                    coins_used = int(lines[i + 1].strip())
                except ValueError:
                    pass
            elif "Total pemasukan" in l and i + 1 < len(lines):
                try:
                    val_str = lines[i + 1].replace("Rp", "").replace(".", "").strip()
                    inflow = Decimal(val_str)
                except Exception:
                    return None, [
                        SafeDiagnostic(
                            code="GOPAY_SUMMARY_MISMATCH",
                            message="Invalid incoming total in summary",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    ]
            elif "Total pengeluaran" in l and i + 1 < len(lines):
                try:
                    val_str = lines[i + 1].replace("Rp", "").replace(".", "").strip()
                    outflow = Decimal(val_str)
                except Exception:
                    return None, [
                        SafeDiagnostic(
                            code="GOPAY_SUMMARY_MISMATCH",
                            message="Invalid outgoing total in summary",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    ]

        if inflow is None or outflow is None:
            return None, [
                SafeDiagnostic(
                    code="GOPAY_SUMMARY_MISMATCH",
                    message="Missing summary totals",
                    severity=DiagnosticSeverity.ERROR,
                )
            ]

        summary = _GoPaySummary(
            period_str=period_str,
            period_start=p_start,
            period_end=p_end,
            incoming_total=inflow,
            outgoing_total=outflow,
            coins_got=coins_got,
            coins_used=coins_used,
            account_number=acc_num,
            account_holder=acc_holder,
        )
        return summary, []

    def _parse_transaction_rows(
        self, lines: Sequence[str], summary: _GoPaySummary
    ) -> tuple[list[_ParsedGoPayRow], list[SafeDiagnostic], bool]:
        rows: list[_ParsedGoPayRow] = []
        diagnostics: list[SafeDiagnostic] = []

        table_lines: list[str] = []
        found_table = False
        for l in lines:
            if "Tanggal Transaksi" in l and "ID transaksi" in l:
                found_table = True
                continue
            if found_table:
                table_lines.append(l)

        if not found_table:
            return [], [
                SafeDiagnostic(
                    code="GOPAY_STRUCTURE_TRUNCATED",
                    message="Transaction table header not found",
                    severity=DiagnosticSeverity.ERROR,
                )
            ], False

        tx_blocks: list[list[str]] = []
        curr_block: list[str] = []
        for l in table_lines:
            if _DATE_RE.match(l):
                if curr_block:
                    tx_blocks.append(curr_block)
                curr_block = [l]
            else:
                if curr_block:
                    curr_block.append(l)
        if curr_block:
            tx_blocks.append(curr_block)

        raw_parsed_rows: list[_ParsedGoPayRow] = []
        seen_tx_ids: dict[str, tuple[str, ...]] = {}

        for blk in tx_blocks:
            if len(blk) < 3:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_STRUCTURE_TRUNCATED",
                        message="Incomplete transaction block",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            d_str = blk[0]
            m_date = _DATE_RE.match(d_str)
            if not m_date:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_DATETIME_INVALID",
                        message="Invalid transaction date format",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            try:
                day, mon, yr = (
                    int(m_date.group(1)),
                    int(m_date.group(2)),
                    int(m_date.group(3)),
                )
                date_obj = date(yr, mon, day)
                occurred_date = date_obj.isoformat()
            except ValueError:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_DATETIME_INVALID",
                        message="Invalid calendar date in transaction row",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            if len(blk) < 2 or not _TIME_RE.match(blk[1]):
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_DATETIME_INVALID",
                        message="Missing or invalid transaction time",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            m_time = _TIME_RE.match(blk[1])
            try:
                hh, mm = int(m_time.group(1)), int(m_time.group(2))
                time_obj = time(hh, mm)
                time_str = f"{time_obj.hour:02d}:{time_obj.minute:02d}"
            except ValueError:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_DATETIME_INVALID",
                        message="Invalid time values in transaction row",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            occurred_at = f"{occurred_date}T{time_str}:00"

            desc_line = blk[2] if len(blk) > 2 else ""
            id_line = blk[3] if len(blk) > 3 else ""

            pm_lines = blk[4:] if len(blk) > 4 else [blk[-1]]
            full_pm_text = " ".join(pm_lines)

            # Check if GoPay Coins loyalty event without cash
            if "GoPay Coins" in full_pm_text and "Rp" not in full_pm_text:
                raw_parsed_rows.append(
                    _ParsedGoPayRow(
                        occurred_at=occurred_at,
                        direction=EventDirection.INFLOW,
                        direction_raw="COINS",
                        amount=Decimal("0"),
                        gross_amount=Decimal("0"),
                        description_raw=desc_line,
                        provider_transaction_id_raw=id_line or None,
                        payment_method_raw="GoPay Coins",
                        is_coins_loyalty=True,
                    )
                )
                continue

            # Cash amount matching
            m_amt = re.search(r"(-?)\s*Rp\s*([0-9]+(?:\.[0-9]{3})*)", full_pm_text)
            if not m_amt:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_AMOUNT_INVALID",
                        message="Missing or unparseable IDR amount in transaction row",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            sign = m_amt.group(1)
            raw_amt_str = m_amt.group(2).replace(".", "")
            try:
                gross_amt_dec = Decimal(raw_amt_str)
            except Exception:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_AMOUNT_INVALID",
                        message="Invalid IDR amount format in transaction row",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics, False

            if sign == "-":
                direction = EventDirection.OUTFLOW
                direction_raw = "KELUAR"
            else:
                direction = EventDirection.INFLOW
                direction_raw = "MASUK"

            is_split = (
                "GoPay Saldo" in full_pm_text and "GoPay Coins" in full_pm_text
            )

            pm_raw = " ".join(l for l in pm_lines if "Rp" not in l).strip()
            if not pm_raw:
                if is_split:
                    pm_raw = "GoPay Saldo / GoPay Coins"
                elif "BCA VA" in full_pm_text:
                    pm_raw = "BCA VA"
                elif "GoPay Saldo" in full_pm_text:
                    pm_raw = "GoPay Saldo"

            raw_parsed_rows.append(
                _ParsedGoPayRow(
                    occurred_at=occurred_at,
                    direction=direction,
                    direction_raw=direction_raw,
                    amount=gross_amt_dec,
                    gross_amount=gross_amt_dec,
                    description_raw=desc_line,
                    provider_transaction_id_raw=id_line or None,
                    payment_method_raw=pm_raw or None,
                    is_coins_loyalty=False,
                    is_split_payment=is_split,
                )
            )

        # Handle Split Payment Resolution
        split_rows = [r for r in raw_parsed_rows if r.is_split_payment]
        is_split_ambiguous = False

        if len(split_rows) == 1:
            sr = split_rows[0]
            coins_dec = Decimal(summary.coins_used)
            if summary.coins_used > 0 and sr.gross_amount >= coins_dec:
                net_cash = sr.gross_amount - coins_dec
                components = (
                    PaymentComponentEvidence(
                        method_raw="GoPay Saldo",
                        amount=net_cash,
                        currency="IDR",
                    ),
                    PaymentComponentEvidence(
                        method_raw="GoPay Coins",
                        amount=coins_dec,
                        currency="IDR",
                    ),
                )
                resolved_rows: list[_ParsedGoPayRow] = []
                for r in raw_parsed_rows:
                    if r is sr:
                        resolved_rows.append(
                            _ParsedGoPayRow(
                                occurred_at=r.occurred_at,
                                direction=r.direction,
                                direction_raw=r.direction_raw,
                                amount=net_cash,
                                gross_amount=r.gross_amount,
                                description_raw=r.description_raw,
                                provider_transaction_id_raw=r.provider_transaction_id_raw,
                                payment_method_raw=r.payment_method_raw,
                                is_coins_loyalty=False,
                                is_split_payment=True,
                                payment_components=components,
                            )
                        )
                    else:
                        resolved_rows.append(r)
                raw_parsed_rows = resolved_rows
            else:
                is_split_ambiguous = True
                raw_parsed_rows = [
                    r for r in raw_parsed_rows if not r.is_split_payment
                ]
        elif len(split_rows) > 1:
            is_split_ambiguous = True
            raw_parsed_rows = [
                r for r in raw_parsed_rows if not r.is_split_payment
            ]

        # Handle Duplicate Provider Transaction IDs & Deduplication
        deduped_rows: list[_ParsedGoPayRow] = []
        for r in raw_parsed_rows:
            tx_id = r.provider_transaction_id_raw
            if tx_id:
                sig = (
                    r.occurred_at,
                    r.direction.value,
                    str(r.amount),
                    sha256(r.description_raw.encode("utf-8")).hexdigest(),
                    sha256((r.payment_method_raw or "").encode("utf-8")).hexdigest(),
                    str(r.is_coins_loyalty),
                )
                if tx_id in seen_tx_ids:
                    if seen_tx_ids[tx_id] == sig:
                        # Identical row evidence -> deduplicate
                        continue
                    else:
                        # Conflicting row evidence -> fail closed
                        diagnostics.append(
                            SafeDiagnostic(
                                code="GOPAY_DUPLICATE_TRANSACTION_ID_CONFLICT",
                                message="Conflicting transaction evidence found for duplicate provider transaction ID",
                                severity=DiagnosticSeverity.ERROR,
                            )
                        )
                        return [], diagnostics, False
                else:
                    seen_tx_ids[tx_id] = sig

            deduped_rows.append(r)

        return deduped_rows, [], is_split_ambiguous

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        if source.binary_payload is None:
            raise AdapterContractError(
                "GoPayEStatementAdapter requires binary_payload"
            )

        try:
            reader = PdfReader(BytesIO(source.binary_payload), strict=False)
            if reader.is_encrypted:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    diagnostics=(
                        SafeDiagnostic(
                            code="GOPAY_PDF_ENCRYPTED_UNSUPPORTED",
                            message="Encrypted PDF statement is not supported",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            if len(reader.pages) == 0:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    diagnostics=(
                        SafeDiagnostic(
                            code="GOPAY_STRUCTURE_TRUNCATED",
                            message="PDF has 0 pages",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            p1_text = reader.pages[0].extract_text() or ""
            lines = [l.strip() for l in p1_text.splitlines() if l.strip()]
        except Exception:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="GOPAY_STRUCTURE_TRUNCATED",
                        message="Failed to parse PDF binary payload",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # 1. Summary Extraction
        summary, summary_diagnostics = self._extract_document_summary(lines)
        if summary_diagnostics or summary is None:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=tuple(summary_diagnostics),
            )

        # 2. Transaction Rows Extraction
        rows, row_diagnostics, is_split_ambiguous = self._parse_transaction_rows(
            lines, summary
        )
        if row_diagnostics:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=tuple(row_diagnostics),
            )

        # 3. Sum Validation & Reconciliation Policy
        cash_rows = [r for r in rows if not r.is_coins_loyalty]
        calc_inflow = sum(
            (r.amount for r in cash_rows if r.direction == EventDirection.INFLOW),
            Decimal("0"),
        )
        calc_outflow = sum(
            (r.amount for r in cash_rows if r.direction == EventDirection.OUTFLOW),
            Decimal("0"),
        )

        inflow_reconciled = (
            abs(calc_inflow - summary.incoming_total) < Decimal("0.005")
        )
        outflow_reconciled = (
            abs(calc_outflow - summary.outgoing_total) < Decimal("0.005")
        )

        parse_status = AdapterParseStatus.COMPLETED
        diagnostics: list[SafeDiagnostic] = []

        if is_split_ambiguous:
            parse_status = AdapterParseStatus.REVIEW_REQUIRED
            diagnostics.append(
                SafeDiagnostic(
                    code="GOPAY_SPLIT_PAYMENT_ALLOCATION_AMBIGUOUS",
                    message="Multiple split payment transactions found with aggregate-only coins usage",
                    severity=DiagnosticSeverity.WARNING,
                )
            )
        elif not (inflow_reconciled and outflow_reconciled):
            if (
                calc_inflow > summary.incoming_total
                or calc_outflow > summary.outgoing_total
            ):
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=source.period_status,
                    diagnostics=(
                        SafeDiagnostic(
                            code="GOPAY_SUMMARY_MISMATCH",
                            message="Transaction cash sum exceeds statement summary totals",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            else:
                # Source summary reports totals greater than visible transaction rows (incomplete rowset)
                parse_status = AdapterParseStatus.REVIEW_REQUIRED
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_SOURCE_SUMMARY_ROWSET_MISMATCH",
                        message="Statement summary totals do not match visible transaction row sums",
                        severity=DiagnosticSeverity.WARNING,
                    )
                )

        # Natural Document Key
        acc_part = (
            sha256(summary.account_number.encode("utf-8")).hexdigest()
            if summary.account_number
            else "no_numeric_wallet"
        )
        natural_key = (
            f"{GOPAY_SOURCE_REGISTRY_ID}:{acc_part}:{summary.period_str}"
        )

        envelopes: list[NormalizedEventEnvelope] = []

        # Evidence 1: SOURCE_SUMMARY (IDR only)
        summary_evidence = SourceSummaryEvidence(
            currency="IDR",
            period_start=summary.period_start,
            period_end=summary.period_end,
            opening_balance=None,
            closing_balance=None,
            incoming_total=summary.incoming_total,
            outgoing_total=summary.outgoing_total,
        )

        sum_fp = sha256(
            f"SUMMARY:{natural_key}:{summary.incoming_total}:{summary.outgoing_total}".encode(
                "utf-8"
            )
        ).hexdigest()

        envelopes.append(
            NormalizedEventEnvelope(
                source_document_id=source.source_document_id,
                source_registry_id=self.descriptor.source_registry_id,
                template_id=self.descriptor.template_id,
                parser_version=self.descriptor.parser_version,
                source_channel=self.descriptor.source_channel,
                event_role=EventRole.SOURCE_SUMMARY,
                source_event_id=None,
                row_fingerprint=sum_fp,
                evidence_quality=ConfidenceLevel.HIGH,
                parse_confidence=ConfidenceLevel.HIGH,
                provenance=SourceProvenanceContract(
                    source_document_id=source.source_document_id,
                    raw_locator="page:1:summary_section",
                    page_number=1,
                    row_index=1,
                    raw_text="Total pemasukan / pengeluaran",
                ),
                payload=summary_evidence,
            )
        )

        # Evidence 2: ACCOUNT_OBSERVATION (if explicit wallet key present)
        if summary.account_number:
            account_evidence = ObservedAccountEvidence(
                observed_provider_account_key=summary.account_number,
                institution_id="gopay",
                display_name_raw=summary.account_holder,
            )

            acc_fp = sha256(
                f"ACCOUNT:{natural_key}:{summary.account_number}:{summary.account_holder}".encode(
                    "utf-8"
                )
            ).hexdigest()

            envelopes.append(
                NormalizedEventEnvelope(
                    source_document_id=source.source_document_id,
                    source_registry_id=self.descriptor.source_registry_id,
                    template_id=self.descriptor.template_id,
                    parser_version=self.descriptor.parser_version,
                    source_channel=self.descriptor.source_channel,
                    event_role=EventRole.ACCOUNT_OBSERVATION,
                    source_event_id=None,
                    row_fingerprint=acc_fp,
                    evidence_quality=ConfidenceLevel.HIGH,
                    parse_confidence=ConfidenceLevel.HIGH,
                    provenance=SourceProvenanceContract(
                        source_document_id=source.source_document_id,
                        raw_locator="page:1:header_identity",
                        page_number=1,
                        row_index=1,
                        raw_text="GoPay Account",
                    ),
                    payload=account_evidence,
                )
            )

        # Evidence 3..N: CASH_MOVEMENT (ONLY for IDR cash transactions)
        for idx, row in enumerate(cash_rows):
            cm_evidence = CashMovementEvidence(
                amount=row.amount,
                currency="IDR",
                direction=row.direction,
                direction_raw=row.direction_raw,
                status=SourceEventStatus.UNKNOWN,
                occurred_at=row.occurred_at,
                description_raw=row.description_raw,
                provider_transaction_id_raw=row.provider_transaction_id_raw,
                payment_method_raw=row.payment_method_raw,
                balance_after=None,
                payment_components=row.payment_components,
            )

            row_fp = sha256(
                f"CASH_MOVEMENT:{natural_key}:{idx}:{row.occurred_at}:{row.amount}:{row.direction.value}:{row.provider_transaction_id_raw}".encode(
                    "utf-8"
                )
            ).hexdigest()

            envelopes.append(
                NormalizedEventEnvelope(
                    source_document_id=source.source_document_id,
                    source_registry_id=self.descriptor.source_registry_id,
                    template_id=self.descriptor.template_id,
                    parser_version=self.descriptor.parser_version,
                    source_channel=self.descriptor.source_channel,
                    event_role=EventRole.CASH_MOVEMENT,
                    source_event_id=None,
                    row_fingerprint=row_fp,
                    evidence_quality=ConfidenceLevel.HIGH,
                    parse_confidence=ConfidenceLevel.HIGH,
                    provenance=SourceProvenanceContract(
                        source_document_id=source.source_document_id,
                        raw_locator=f"page:1:row:{idx+1}",
                        page_number=1,
                        row_index=idx + 1,
                        raw_text=row.description_raw,
                    ),
                    payload=cm_evidence,
                )
            )

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=parse_status,
            period_status=source.period_status,
            events=tuple(envelopes),
            diagnostics=tuple(diagnostics),
        )


__all__ = [
    "GoPayEStatementAdapter",
    "GOPAY_SOURCE_REGISTRY_ID",
    "GOPAY_TEMPLATE_ID",
    "GOPAY_PARSER_VERSION",
    "GOPAY_ADAPTER_ID",
    "GOPAY_TEMPLATE_FINGERPRINT",
]
