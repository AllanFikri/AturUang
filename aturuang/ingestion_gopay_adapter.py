from __future__ import annotations

from dataclasses import dataclass, field
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
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


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
    return f"{yr:04d}-{mon:02d}-{day:02d}"


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
    description_raw: str = field(repr=False)
    provider_transaction_id_raw: str | None = field(default=None, repr=False)
    payment_method_raw: str | None = field(default=None, repr=False)
    is_coins_loyalty: bool = False


class GoPayEStatementAdapter(UniversalSourceAdapter):
    descriptor: AdapterDescriptor = AdapterDescriptor(
        adapter_id=GOPAY_ADAPTER_ID,
        source_registry_id=GOPAY_SOURCE_REGISTRY_ID,
        template_id=GOPAY_TEMPLATE_ID,
        parser_version=GOPAY_PARSER_VERSION,
        source_channel=SourceChannel.PDF,
    )

    def _extract_account_candidates(
        self, header_lines: Sequence[str]
    ) -> tuple[str | None, bool]:
        candidates: set[str] = set()
        for line in header_lines:
            m = _PHONE_RE.match(line.strip())
            if m:
                candidates.add(m.group(0))

        if len(candidates) == 0:
            return None, False
        if len(candidates) == 1:
            return next(iter(candidates)), False
        return None, True

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

        # Extract identity from header
        acc_num, is_ambiguous = self._extract_account_candidates(lines[1:5])
        if is_ambiguous:
            return None, [
                SafeDiagnostic(
                    code="GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS",
                    message="Multiple distinct wallet identities found in header",
                    severity=DiagnosticSeverity.ERROR,
                )
            ]

        acc_holder = lines[1] if len(lines) > 1 and lines[1] != acc_num else None

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
        self, lines: Sequence[str]
    ) -> tuple[list[_ParsedGoPayRow], list[SafeDiagnostic]]:
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
            ]

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

        for blk in tx_blocks:
            if len(blk) < 3:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_STRUCTURE_TRUNCATED",
                        message="Incomplete transaction block",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics

            d_str = blk[0]
            d_parts = d_str.split("/")
            if len(d_parts) != 3:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_DATETIME_INVALID",
                        message="Invalid transaction date format",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics

            try:
                occurred_date = f"{int(d_parts[2]):04d}-{int(d_parts[1]):02d}-{int(d_parts[0]):02d}"
            except ValueError:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_DATETIME_INVALID",
                        message="Invalid transaction date values",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics

            t_str = blk[1] if len(blk) > 1 and _TIME_RE.match(blk[1]) else "00:00"
            occurred_at = f"{occurred_date}T{t_str}:00"

            desc_line = blk[2] if len(blk) > 2 else ""
            id_line = blk[3] if len(blk) > 3 else ""

            pm_lines = blk[4:] if len(blk) > 4 else [blk[-1]]
            full_pm_text = " ".join(pm_lines)

            # Check if GoPay Coins loyalty event without cash
            if "GoPay Coins" in full_pm_text and "Rp" not in full_pm_text:
                rows.append(
                    _ParsedGoPayRow(
                        occurred_at=occurred_at,
                        direction=EventDirection.INFLOW,
                        direction_raw="COINS",
                        amount=Decimal("0"),
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
                return [], diagnostics

            sign = m_amt.group(1)
            raw_amt_str = m_amt.group(2).replace(".", "")
            try:
                amt_dec = Decimal(raw_amt_str)
            except Exception:
                diagnostics.append(
                    SafeDiagnostic(
                        code="GOPAY_ROW_AMOUNT_INVALID",
                        message="Invalid IDR amount format in transaction row",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics

            if sign == "-":
                direction = EventDirection.OUTFLOW
                direction_raw = "KELUAR"
            else:
                direction = EventDirection.INFLOW
                direction_raw = "MASUK"

            pm_raw = " ".join(l for l in pm_lines if "Rp" not in l).strip()
            if not pm_raw:
                if "BCA VA" in full_pm_text:
                    pm_raw = "BCA VA"
                elif "GoPay Saldo" in full_pm_text:
                    pm_raw = "GoPay Saldo"

            rows.append(
                _ParsedGoPayRow(
                    occurred_at=occurred_at,
                    direction=direction,
                    direction_raw=direction_raw,
                    amount=amt_dec,
                    description_raw=desc_line,
                    provider_transaction_id_raw=id_line or None,
                    payment_method_raw=pm_raw or None,
                    is_coins_loyalty=False,
                )
            )

        return rows, []

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
            full_text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
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

        # Check global identity conflict in full document
        all_phone_matches = set(_PHONE_RE.findall(full_text))
        if len(all_phone_matches) > 1:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=(
                    SafeDiagnostic(
                        code="GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS",
                        message="Multiple conflicting wallet identities found in document",
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
        rows, row_diagnostics = self._parse_transaction_rows(lines)
        if row_diagnostics:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=tuple(row_diagnostics),
            )

        # 3. Sum Validation against Source Totals
        cash_rows = [r for r in rows if not r.is_coins_loyalty]
        calc_inflow = sum(
            (r.amount for r in cash_rows if r.direction == EventDirection.INFLOW),
            Decimal("0"),
        )
        calc_outflow = sum(
            (r.amount for r in cash_rows if r.direction == EventDirection.OUTFLOW),
            Decimal("0"),
        )

        if (
            calc_inflow > summary.incoming_total
            or (calc_outflow - Decimal(summary.coins_used)) > summary.outgoing_total
        ):
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=(
                    SafeDiagnostic(
                        code="GOPAY_SUMMARY_MISMATCH",
                        message="Transaction sums exceed statement summary totals",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
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
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=source.period_status,
            events=tuple(envelopes),
            diagnostics=(),
        )


__all__ = [
    "GoPayEStatementAdapter",
    "GOPAY_SOURCE_REGISTRY_ID",
    "GOPAY_TEMPLATE_ID",
    "GOPAY_PARSER_VERSION",
    "GOPAY_ADAPTER_ID",
    "GOPAY_TEMPLATE_FINGERPRINT",
]
