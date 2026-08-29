from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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

BLU_MUTATION_SOURCE_REGISTRY_ID = "blu_mutation"
BLU_MUTATION_TEMPLATE_ID = "blu_account_mutation_v1"
BLU_MUTATION_PARSER_VERSION = "parser-v1"
BLU_MUTATION_ADAPTER_ID = "blu-account-mutation-v1"
BLU_MUTATION_TEMPLATE_FINGERPRINT = (
    "2226278f9614625f9174151dad8b8da7851a7455eeec48e0cd9017a96acbcbfc"
)

BLU_PORTFOLIO_SUBACCOUNT_IDENTITY_UNRESOLVED = True

_MONTHS: dict[str, tuple[int, int]] = {
    "JAN": (1, 31),
    "FEB": (2, 28),
    "MAR": (3, 31),
    "APR": (4, 30),
    "MAY": (5, 31),
    "MEI": (5, 31),
    "JUN": (6, 30),
    "JUL": (7, 31),
    "AUG": (8, 31),
    "AGU": (8, 31),
    "AGT": (8, 31),
    "SEP": (9, 30),
    "OCT": (10, 31),
    "OKT": (10, 31),
    "NOV": (11, 30),
    "DEC": (12, 31),
    "DES": (12, 31),
    "JANUARI": (1, 31),
    "FEBRUARI": (2, 28),
    "MARET": (3, 31),
    "APRIL": (4, 30),
    "JUNI": (6, 30),
    "JULI": (7, 31),
    "AGUSTUS": (8, 31),
    "SEPTEMBER": (9, 30),
    "OKTOBER": (10, 31),
    "NOVEMBER": (11, 30),
    "DESEMBER": (12, 31),
    "JANUARY": (1, 31),
    "FEBRUARY": (2, 28),
    "MARCH": (3, 31),
    "JUNE": (6, 30),
    "JULY": (7, 31),
    "AUGUST": (8, 31),
    "OCTOBER": (10, 31),
    "DECEMBER": (12, 31),
}

_DATE_HEADER_RE = re.compile(r"^(\d{2}\s+[A-Za-z]{3,9}\s+\d{4})$")
_TIME_DETAIL_RE = re.compile(r"^(\d{2}:\d{2})\s+(.*)$")
_AMOUNT_BALANCE_RE = re.compile(
    r"(-?\s*[0-9]+(?:[.,][0-9]{3})*(?:,[0-9]{2}|\.[0-9]{2}))\s+([0-9]+(?:[.,][0-9]{3})*(?:,[0-9]{2}|\.[0-9]{2}))$"
)


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
    try:
        val = Decimal(cleaned)
        if not val.is_finite():
            raise AdapterContractError("non-finite decimal")
        return -val if is_neg else val
    except (InvalidOperation, ValueError) as exc:
        raise AdapterContractError("invalid decimal format") from exc


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


@dataclass(frozen=True)
class _BluMutationSummary:
    period_str: str
    period_start: str
    period_end: str
    opening_balance: Decimal
    closing_balance: Decimal
    total_credit: Decimal
    total_debit: Decimal
    account_number: str | None = field(default=None, repr=False)
    account_holder: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _ParsedBluMutationRow:
    page_number: int
    date_obj: date
    time_str: str
    occurred_at: str
    direction: EventDirection
    direction_raw: str
    amount: Decimal
    balance_after: Decimal
    description_raw: str = field(repr=False)
    provider_transaction_id_raw: str | None = field(default=None, repr=False)


class BluAccountMutationAdapter(UniversalSourceAdapter):
    descriptor: AdapterDescriptor = AdapterDescriptor(
        adapter_id=BLU_MUTATION_ADAPTER_ID,
        source_registry_id=BLU_MUTATION_SOURCE_REGISTRY_ID,
        template_id=BLU_MUTATION_TEMPLATE_ID,
        parser_version=BLU_MUTATION_PARSER_VERSION,
        source_channel=SourceChannel.PDF,
    )

    def _extract_period_and_dates(
        self, text: str
    ) -> tuple[str | None, str | None, str | None]:
        m_range = re.search(
            r"(\d{1,2})\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text
        )
        if m_range:
            d1_s, d2_s, m_str, y_str = m_range.groups()
            year = int(y_str)
            m_upper = m_str.upper()
            if m_upper in _MONTHS:
                month, _ = _MONTHS[m_upper]
                d1 = int(d1_s)
                d2 = int(d2_s)
                p_start = f"{year:04d}-{month:02d}-{d1:02d}"
                p_end = f"{year:04d}-{month:02d}-{d2:02d}"
                return f"{year:04d}-{month:02d}", p_start, p_end

        m_month = re.search(
            r"Periode\s*/\s*Period\s*\n\s*([A-Za-z]+)\s+(\d{4})", text
        )
        if not m_month:
            m_month = re.search(r"([A-Za-z]+)\s+(\d{4})", text)
        if m_month:
            m_str, y_str = m_month.groups()
            year = int(y_str)
            m_upper = m_str.upper()
            if m_upper in _MONTHS:
                month, max_day = _MONTHS[m_upper]
                if month == 2 and _is_leap_year(year):
                    max_day = 29
                p_start = f"{year:04d}-{month:02d}-01"
                p_end = f"{year:04d}-{month:02d}-{max_day:02d}"
                return f"{year:04d}-{month:02d}", p_start, p_end

        return None, None, None

    def _extract_account_candidates(
        self, text: str
    ) -> tuple[str | None, bool]:
        """
        Collects ALL structurally anchored bluAccount numeric identity candidates.
        Returns: (unique_account_id, is_ambiguous)
        - 0 unique candidates: returns (None, False)
        - Exactly 1 unique candidate: returns (candidate, False)
        - >1 distinct candidates: returns (None, True)
        """
        pattern = re.compile(
            r"bluAccount\s*-\s*(\d{4}[^\S\r\n]+\d{4}[^\S\r\n]+\d{4}|\d{8,32})"
        )
        matches = pattern.findall(text)
        candidates: set[str] = set()
        for m in matches:
            digits = re.sub(r"\s+", "", m)
            if 8 <= len(digits) <= 32:
                candidates.add(digits)

        if len(candidates) == 0:
            return None, False
        if len(candidates) == 1:
            return next(iter(candidates)), False
        return None, True

    def _extract_account_holder(self, text: str) -> str | None:
        m = re.search(r"Nama\s*/\s*Name\s*\n\s*([^\n]+)", text)
        if m:
            holder = m.group(1).strip()
            if holder and "Rekening" not in holder and "bluAccount" not in holder:
                return holder
        return None

    def _extract_document_summary(
        self, full_text: str, p1_text: str
    ) -> _BluMutationSummary | None:
        period_str, period_start, period_end = self._extract_period_and_dates(
            p1_text
        )
        if not period_str or not period_start or not period_end:
            return None

        lines = [l.strip() for l in p1_text.splitlines()]
        nums: list[str] = []
        capture = False
        for l in lines:
            if "Total Pemasukan" in l:
                capture = True
                continue
            if capture:
                if l.startswith("Rp") or "bluAccount" in l or "Keterangan" in l:
                    if len(nums) >= 4:
                        break
                m = re.match(
                    r"^-?[0-9]+(?:[.,][0-9]{3})*(?:,[0-9]{2}|\.[0-9]{2}|,[0-9]|\.[0-9])?$",
                    l,
                )
                if m:
                    nums.append(l)

        if len(nums) < 4:
            return None

        try:
            sa = _parse_decimal(nums[0])
            sak = _parse_decimal(nums[1])
            tp = abs(_parse_decimal(nums[2]))
            tk = abs(_parse_decimal(nums[3]))
        except AdapterContractError:
            return None

        # Equation check: opening + total_income - total_expense == closing
        if abs((sa + tp - tk) - sak) >= Decimal("0.005"):
            return None

        acc_num, is_ambiguous = self._extract_account_candidates(full_text)
        if is_ambiguous:
            return None

        acc_holder = self._extract_account_holder(p1_text)

        return _BluMutationSummary(
            period_str=period_str,
            period_start=period_start,
            period_end=period_end,
            opening_balance=sa,
            closing_balance=sak,
            total_credit=tp,
            total_debit=tk,
            account_number=acc_num,
            account_holder=acc_holder,
        )

    def _parse_mutation_transactions(
        self,
        reader: PdfReader,
        summary: _BluMutationSummary,
    ) -> tuple[list[_ParsedBluMutationRow], list[SafeDiagnostic]]:
        rows: list[_ParsedBluMutationRow] = []
        diagnostics: list[SafeDiagnostic] = []

        all_lines: list[tuple[int, str]] = []
        for p_idx, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            for l in page_text.splitlines():
                t = l.strip()
                if t:
                    all_lines.append((p_idx + 1, t))

        # Check for explicit zero activity statement
        full_text = " ".join(t for _, t in all_lines)
        if "Tidak ada transaksi pada periode ini" in full_text:
            return [], []

        # Find start of transactions
        in_table = False
        current_date: date | None = None

        pending_time: str | None = None
        pending_desc_parts: list[str] = []
        pending_page: int = 1

        for p_num, line in all_lines:
            if "Keterangan / Remarks" in line or "bluAccount -" in line:
                in_table = True
                continue

            if not in_table:
                continue

            if "Disclaimer" in line or "BCA Digital berizin" in line:
                continue

            if "Total Pemasukan" in line or "Saldo Awal" in line or "Total Pengeluaran" in line:
                continue

            # Check if line is a Date Header (e.g. 03 Aug 2024, 05 Aug 2025)
            m_date = _DATE_HEADER_RE.match(line)
            if m_date:
                d_str = m_date.group(1)
                parts = d_str.split()
                day = int(parts[0])
                mon_str = parts[1].upper()
                year = int(parts[2])
                if mon_str in _MONTHS:
                    month, _ = _MONTHS[mon_str]
                    current_date = date(year, month, day)
                continue

            # Check if line starts with Time (e.g. 10:00 Bank Transfer In)
            m_time = _TIME_DETAIL_RE.match(line)
            if m_time:
                pending_time = m_time.group(1)
                pending_desc_parts = [m_time.group(2).strip()]
                pending_page = p_num
                continue

            # Check if line has Amount and Balance at the end
            m_amt_bal = _AMOUNT_BALANCE_RE.search(line)
            if m_amt_bal and pending_time and current_date:
                amt_str = m_amt_bal.group(1)
                bal_str = m_amt_bal.group(2)

                prefix_desc = line[: m_amt_bal.start()].strip()
                if prefix_desc:
                    pending_desc_parts.append(prefix_desc)

                # Extract optional native transaction reference from the time/header line
                ref_id = None
                if pending_desc_parts and "|" in pending_desc_parts[0]:
                    parts = pending_desc_parts[0].split("|", 1)
                    pending_desc_parts[0] = parts[0].strip()
                    ref_id = parts[1].strip() or None

                raw_desc = " ".join(p for p in pending_desc_parts if p).strip()

                try:
                    dec_amt = _parse_decimal(amt_str)
                    dec_bal = _parse_decimal(bal_str)
                except AdapterContractError:
                    diagnostics.append(
                        SafeDiagnostic(
                            code="BLU_MUTATION_ROW_AMOUNT_INVALID",
                            message="Invalid transaction amount or running balance",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    )
                    return [], diagnostics

                if dec_amt < 0:
                    direction = EventDirection.OUTFLOW
                    dir_raw = "KELUAR"
                    abs_amt = abs(dec_amt)
                else:
                    direction = EventDirection.INFLOW
                    dir_raw = "MASUK"
                    abs_amt = dec_amt

                occurred_at = (
                    f"{current_date.isoformat()}T{pending_time}:00"
                )

                rows.append(
                    _ParsedBluMutationRow(
                        page_number=pending_page,
                        date_obj=current_date,
                        time_str=pending_time,
                        occurred_at=occurred_at,
                        direction=direction,
                        direction_raw=dir_raw,
                        amount=abs_amt,
                        balance_after=dec_bal,
                        description_raw=raw_desc,
                        provider_transaction_id_raw=ref_id,
                    )
                )

                pending_time = None
                pending_desc_parts = []
                continue

            # Multiline detail accumulation
            if pending_time:
                pending_desc_parts.append(line)

        # Running balance continuity check
        if rows:
            running_bal = summary.opening_balance
            for r in rows:
                if r.direction == EventDirection.INFLOW:
                    running_bal += r.amount
                else:
                    running_bal -= r.amount

                if abs(running_bal - r.balance_after) >= Decimal("0.005"):
                    diagnostics.append(
                        SafeDiagnostic(
                            code="BLU_MUTATION_RUNNING_BALANCE_DISCONTINUITY",
                            message="Discontinuity detected in transaction running balance",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    )
                    return [], diagnostics

        return rows, diagnostics

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        if source.binary_payload is None:
            raise AdapterContractError(
                "BluAccountMutationAdapter requires binary_payload"
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
                            code="BLU_MUTATION_PDF_ENCRYPTED_UNSUPPORTED",
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
                            code="BLU_MUTATION_STRUCTURE_TRUNCATED",
                            message="PDF has 0 pages",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            p1_text = reader.pages[0].extract_text() or ""
            full_text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except Exception:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="BLU_MUTATION_STRUCTURE_TRUNCATED",
                        message="Failed to parse PDF binary payload",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # 0. Check Identity Ambiguity
        acc_num, is_ambiguous = self._extract_account_candidates(full_text)
        if is_ambiguous:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=(
                    SafeDiagnostic(
                        code="BLU_MUTATION_ACCOUNT_IDENTITY_AMBIGUOUS",
                        message="Multiple distinct bluAccount numeric identities found in document",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # 1. Summary & Period Extraction
        summary = self._extract_document_summary(full_text, p1_text)
        if not summary:
            period_str, _, _ = self._extract_period_and_dates(p1_text)
            if not period_str:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    diagnostics=(
                        SafeDiagnostic(
                            code="BLU_MUTATION_PERIOD_AMBIGUOUS",
                            message="Missing or unparseable statement period",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=(
                    SafeDiagnostic(
                        code="BLU_MUTATION_SUMMARY_MISMATCH",
                        message="Malformed or mathematically inconsistent document summary",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # 2. Extract Transactions
        rows, row_diagnostics = self._parse_mutation_transactions(
            reader, summary
        )
        if row_diagnostics:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=tuple(row_diagnostics),
            )

        # 3. Sum Validation
        calc_credit = sum(
            (r.amount for r in rows if r.direction == EventDirection.INFLOW),
            Decimal("0"),
        )
        calc_debit = sum(
            (r.amount for r in rows if r.direction == EventDirection.OUTFLOW),
            Decimal("0"),
        )

        if (
            abs(calc_credit - summary.total_credit) >= Decimal("0.005")
            or abs(calc_debit - summary.total_debit) >= Decimal("0.005")
        ):
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=source.period_status,
                diagnostics=(
                    SafeDiagnostic(
                        code="BLU_MUTATION_SUMMARY_MISMATCH",
                        message="Calculated transaction sum does not match document summary",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # Natural Document Key
        acc_part = (
            sha256(summary.account_number.encode("utf-8")).hexdigest()
            if summary.account_number
            else "no_numeric_account"
        )
        natural_key = (
            f"{BLU_MUTATION_SOURCE_REGISTRY_ID}:{acc_part}:{summary.period_str}"
        )

        provenance = SourceProvenanceContract(
            source_document_id=source.source_document_id,
            raw_locator="page:1:document_summary",
            page_number=1,
            row_index=1,
            raw_text="bluAccount Summary",
        )

        envelopes: list[NormalizedEventEnvelope] = []

        # Evidence 1: SOURCE_SUMMARY
        summary_evidence = SourceSummaryEvidence(
            currency="IDR",
            period_start=summary.period_start,
            period_end=summary.period_end,
            opening_balance=summary.opening_balance,
            closing_balance=summary.closing_balance,
            incoming_total=summary.total_credit,
            outgoing_total=summary.total_debit,
        )

        sum_fp = sha256(
            f"SUMMARY:{natural_key}:{summary.opening_balance}:{summary.closing_balance}:{summary.total_credit}:{summary.total_debit}".encode(
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
                provenance=provenance,
                payload=summary_evidence,
            )
        )

        # Evidence 2: ACCOUNT_OBSERVATION (only if numeric account ID is explicitly present)
        if summary.account_number:
            account_evidence = ObservedAccountEvidence(
                observed_provider_account_key=summary.account_number,
                institution_id="blu",
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
                        raw_locator="page:1:account_header",
                        page_number=1,
                        row_index=1,
                        raw_text="bluAccount",
                    ),
                    payload=account_evidence,
                )
            )

        # Evidence 3..N: CASH_MOVEMENT
        for idx, row in enumerate(rows):
            cm_evidence = CashMovementEvidence(
                amount=row.amount,
                currency="IDR",
                direction=row.direction,
                direction_raw=row.direction_raw,
                status=SourceEventStatus.POSTED,
                occurred_at=row.occurred_at,
                description_raw=row.description_raw,
                balance_after=row.balance_after,
                provider_transaction_id_raw=row.provider_transaction_id_raw,
            )

            row_fp = sha256(
                f"CASH_MOVEMENT:{natural_key}:{idx}:{row.occurred_at}:{row.amount}:{row.direction.value}:{row.balance_after}:{row.provider_transaction_id_raw}".encode(
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
                        raw_locator=f"page:{row.page_number}:row:{idx+1}",
                        page_number=row.page_number,
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
    "BluAccountMutationAdapter",
    "BLU_MUTATION_SOURCE_REGISTRY_ID",
    "BLU_MUTATION_TEMPLATE_ID",
    "BLU_MUTATION_PARSER_VERSION",
    "BLU_MUTATION_ADAPTER_ID",
    "BLU_MUTATION_TEMPLATE_FINGERPRINT",
    "BLU_PORTFOLIO_SUBACCOUNT_IDENTITY_UNRESOLVED",
]
