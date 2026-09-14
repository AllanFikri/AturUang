from __future__ import annotations

import calendar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
import unicodedata

from pypdf import PdfReader
from pypdf.errors import PdfReadError

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
    validate_adapter_input,
)
from .ingestion_account_discovery import AccountDiscoveryObservation
from .ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    OwnershipState,
    PeriodStatus,
    SourceChannel,
    SourceProvenanceContract,
)


BCA_TEMPLATE_FINGERPRINT = (
    "75dfe6b0d68ce4a4312f258536edaa86"
    "a28535bd0d1d0f9ef6fb22e4858d574c"
)

BCA_DESCRIPTOR = AdapterDescriptor(
    adapter_id="bca-monthly-statement-v1",
    source_registry_id="bca_statement",
    template_id="bca_monthly_statement_v1",
    parser_version="parser-v1",
    source_channel=SourceChannel.PDF,
)

_MONTHS = {
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
}

_PERIOD_WORD_RE = re.compile(
    r"\bPERIODE\s*:?\s*("
    + "|".join(_MONTHS)
    + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
_PERIOD_NUM_RE = re.compile(
    r"\bPERIODE\s*:?\s*(0?[1-9]|1[0-2])[/.-](\d{4})\b",
    re.IGNORECASE,
)
_PERIOD_RANGE_RE = re.compile(
    r"\bPERIODE\s*:?\s*"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s*-\s*"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\b",
    re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(
    r"\bNO\.?\s*REKENING\s*:?\s*([A-Z0-9 .-]{5,64})\b",
    re.IGNORECASE,
)
_OPENING_RE = re.compile(
    r"\bSALDO\s+AWAL\s*:?\s*(?:RP\.?\s*)?([0-9][0-9.,]*)\b",
    re.IGNORECASE,
)
_OPENING_DATE_ROW_RE = re.compile(
    r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+SALDO\s+AWAL\s+(?:RP\.?\s*)?([0-9][0-9.,]*)",
    re.IGNORECASE,
)
_CLOSING_RE = re.compile(
    r"\bSALDO\s+AKHIR\s*:?\s*(?:RP\.?\s*)?([0-9][0-9.,]*)\b",
    re.IGNORECASE,
)

_EFFECTIVE_RE = re.compile(
    r"\b(?:TGL|TANGGAL)\s+(?:EFEKTIF|TRANSAKSI)\s*:?\s*"
    r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.IGNORECASE,
)
_EFFECTIVE_EN_RE = re.compile(
    r"\bEFFECTIVE\s+DATE\s*:?\s*"
    r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.IGNORECASE,
)
_REFERENCE_RE = re.compile(
    r"^(?:NO\.?\s*)?(?:REF|REFERENSI)\s*:?\s*(.+)$",
    re.IGNORECASE,
)
_COUNTERPARTY_RE = re.compile(
    r"^(?:COUNTERPARTY|PENERIMA)\s*:?\s*(.+)$",
    re.IGNORECASE,
)
_POKET_KEY_RE = re.compile(
    r"^POKET\s+(?:ID|SLOT)\s*:?\s*([A-Z0-9_-]{2,64})\s*$",
    re.IGNORECASE,
)
_POKET_NAME_RE = re.compile(
    r"^POKET\s+NAME\s*:?\s*(.+)$",
    re.IGNORECASE,
)

_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:SALDO\s+AKHIR|SALDO\s+AWAL\s*:|MUTASI\s+(?:DB|CR|DEBET|KREDIT)|"
    r"BERSAMBUNG|HALAMAN\b|CATATAN|FASILITAS|KETERANGAN\s*:|TANGGAL\s+KETERANGAN|"
    r"NO\.?\s*REKENING|PERIODE\b|REKENING\s+TAHAPAN|MATA\s+UANG|INDONESIA|"
    r"KCP\s+|MALANG\s+\d+|JL\s+|RT\d+)",
    re.IGNORECASE,
)

_NUM_RE = r"[0-9][0-9.,]*"

_RE_AMT_DB_BAL = re.compile(
    rf"^(?:(?P<prefix>.*?)\s+)?(?P<amount>{_NUM_RE})\s+(?P<direction>DB|DEBIT)\s+(?P<balance>{_NUM_RE})$",
    re.IGNORECASE,
)
_RE_AMT_DB = re.compile(
    rf"^(?:(?P<prefix>.*?)\s+)?(?P<amount>{_NUM_RE})\s+(?P<direction>DB|DEBIT)$",
    re.IGNORECASE,
)
_RE_AMT_CR_BAL = re.compile(
    rf"^(?:(?P<prefix>.*?)\s+)?(?P<amount>{_NUM_RE})\s+(?P<direction>CR|KREDIT|CREDIT|KR)\s+(?P<balance>{_NUM_RE})$",
    re.IGNORECASE,
)
_RE_AMT_CR = re.compile(
    rf"^(?:(?P<prefix>.*?)\s+)?(?P<amount>{_NUM_RE})\s+(?P<direction>CR|KREDIT|CREDIT|KR)$",
    re.IGNORECASE,
)
_RE_AMT_BAL = re.compile(
    rf"^(?:(?P<prefix>.*?)\s+)?(?P<amount>{_NUM_RE})\s+(?P<balance>{_NUM_RE})$",
    re.IGNORECASE,
)
_RE_AMT_ONLY = re.compile(
    rf"^(?:(?P<prefix>.*?)\s+)?(?P<amount>{_NUM_RE})$",
    re.IGNORECASE,
)

_RE_1LINE = re.compile(
    rf"^(?P<date>\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?)\s+"
    rf"(?P<body>.+?)\s+"
    rf"(?P<amount>{_NUM_RE})\s*"
    rf"(?:(?P<direction>DB|CR|DEBIT|KREDIT|CREDIT|KR))"
    rf"(?:\s+(?P<balance>{_NUM_RE}))?\s*$",
    re.IGNORECASE,
)

_DATE_PREFIX_RE = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(?P<rest>.+)$"
)


class _BCAExpectedFailure(RuntimeError):
    pass


def _normalize_space(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _parse_decimal(value: str) -> Decimal:
    text = unicodedata.normalize("NFKC", str(value)).strip().upper()
    text = text.replace("IDR", "").replace("RP", "").replace(" ", "")
    text = re.sub(r"[^0-9,.\-]", "", text)

    if not text:
        raise InvalidOperation

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        left, right = text.rsplit(",", 1)
        if 0 < len(right) <= 2:
            text = left.replace(",", "") + "." + right
        else:
            text = text.replace(",", "")
    elif "." in text:
        parts = text.split(".")
        if len(parts) == 2 and 0 < len(parts[1]) <= 2:
            pass
        else:
            text = "".join(parts)

    result = Decimal(text)
    if not result.is_finite():
        raise InvalidOperation
    return result


def _period_from_text(text: str) -> tuple[int, int] | None:
    match = _PERIOD_WORD_RE.search(text)
    if match:
        return int(match.group(2)), _MONTHS[match.group(1).upper()]

    match = _PERIOD_NUM_RE.search(text)
    if match:
        return int(match.group(2)), int(match.group(1))

    match = _PERIOD_RANGE_RE.search(text)
    if match:
        start_month = int(match.group(2))
        start_year = int(match.group(3))
        end_month = int(match.group(5))
        end_year = int(match.group(6))
        if (start_year, start_month) == (end_year, end_month):
            return start_year, start_month

    return None


def _period_bounds(year: int, month: int) -> tuple[str, str]:
    last_day = calendar.monthrange(year, month)[1]
    return (
        f"{year:04d}-{month:02d}-01",
        f"{year:04d}-{month:02d}-{last_day:02d}",
    )


def _parse_source_date(
    value: str,
    *,
    period_year: int,
    period_month: int,
) -> str:
    parts = [int(item) for item in value.strip().split("/")]

    if len(parts) not in {2, 3}:
        raise ValueError("unsupported date precision")

    day, month = parts[0], parts[1]

    if len(parts) == 3:
        year = parts[2]
        if year < 100:
            year += 2000
    else:
        if month == period_month:
            year = period_year
        elif period_month == 1 and month == 12:
            year = period_year - 1
        else:
            raise ValueError("row date is ambiguous against statement period")

    last_day = calendar.monthrange(year, month)[1]
    if day < 1 or day > last_day:
        raise ValueError("invalid calendar date")

    return f"{year:04d}-{month:02d}-{day:02d}"


def _normalize_account_identity(value: str) -> str:
    normalized = _normalize_space(value)
    normalized = normalized.replace(" ", "").replace(".", "").replace("-", "")
    if not normalized:
        raise ValueError("empty account identity")
    return normalized


def _natural_document_key(
    account_raw: str,
    *,
    year: int,
    month: int,
) -> str:
    normalized = _normalize_account_identity(account_raw)
    account_sha = sha256(normalized.encode("utf-8")).hexdigest()
    return f"bca_statement:{account_sha}:{year:04d}-{month:02d}"


def _row_fingerprint(
    *,
    posted_at: str,
    occurred_at: str | None,
    direction_raw: str,
    amount: Decimal,
    description_raw: str,
    reference_raw: str | None,
    balance_after: Decimal | None,
    row_index: int,
) -> str:
    payload = "\x1f".join(
        (
            "bca-row-v1",
            posted_at,
            occurred_at or "",
            _normalize_space(direction_raw).upper(),
            _canonical_decimal(amount),
            _normalize_space(description_raw),
            _normalize_space(reference_raw or ""),
            _canonical_decimal(balance_after)
            if balance_after is not None
            else "",
            str(row_index),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _summary_fingerprint(
    *,
    period_start: str,
    period_end: str,
    opening_balance: Decimal | None,
    closing_balance: Decimal | None,
) -> str:
    payload = "\x1f".join(
        (
            "bca-summary-v1",
            period_start,
            period_end,
            _canonical_decimal(opening_balance)
            if opening_balance is not None
            else "",
            _canonical_decimal(closing_balance)
            if closing_balance is not None
            else "",
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _safe_diagnostic(
    code: str,
    message: str,
    *,
    severity: DiagnosticSeverity = DiagnosticSeverity.WARNING,
    review_required: bool = True,
) -> SafeDiagnostic:
    return SafeDiagnostic(
        code=code,
        severity=severity,
        message=message,
        review_required=review_required,
    )


def _lifecycle_hint(text: str) -> str | None:
    normalized = _normalize_space(text).upper()

    patterns = (
        (
            "BCA_POKET_INITIAL_FUNDING",
            (
                "POKET INITIAL FUNDING",
                "INITIAL FUNDING POKET",
                "DANA AWAL POKET",
                "SETORAN AWAL POKET",
            ),
        ),
        (
            "BCA_POKET_TOP_UP",
            (
                "POKET TOP UP",
                "TOP UP POKET",
                "TAMBAH DANA POKET",
            ),
        ),
        (
            "BCA_POKET_MOVE",
            (
                "POKET MOVE",
                "PINDAH POKET",
                "PINDAHKAN POKET",
                "MUTASI ANTAR POKET",
            ),
        ),
        (
            "BCA_POKET_SCHEDULED_TRANSFER",
            (
                "POKET SCHEDULED TRANSFER",
                "TRANSFER TERJADWAL POKET",
                "TRF BERKALA POKET",
                "JADWAL POKET",
            ),
        ),
    )

    for code, needles in patterns:
        if any(needle in normalized for needle in needles):
            return code

    return None


def _extract_pdf_pages(payload: bytes) -> tuple[str, ...]:
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
    except (PdfReadError, ValueError, TypeError, OSError) as exc:
        raise _BCAExpectedFailure("BCA_PDF_UNREADABLE") from exc

    if reader.is_encrypted:
        try:
            decrypt_result = reader.decrypt("")
        except Exception as exc:
            raise _BCAExpectedFailure("BCA_PDF_LOCKED") from exc

        if not decrypt_result:
            raise _BCAExpectedFailure("BCA_PDF_LOCKED")

    try:
        pages = tuple((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise _BCAExpectedFailure("BCA_TEXT_EXTRACTION_FAILED") from exc

    if not pages or not any(page.strip() for page in pages):
        raise _BCAExpectedFailure("BCA_TEXT_UNAVAILABLE")

    return pages


def _failure_result(source: AdapterInput, code: str) -> AdapterResult:
    messages = {
        "BCA_PDF_UNREADABLE": "BCA statement PDF is unreadable.",
        "BCA_PDF_LOCKED": "BCA statement PDF cannot be decrypted.",
        "BCA_TEXT_EXTRACTION_FAILED": "BCA statement text extraction failed.",
        "BCA_TEXT_UNAVAILABLE": "BCA statement has no readable text layer.",
    }

    return AdapterResult(
        descriptor=BCA_DESCRIPTOR,
        source_document_id=source.source_document_id,
        parse_status=AdapterParseStatus.FAILED,
        period_status=PeriodStatus.UNKNOWN,
        diagnostics=(
            _safe_diagnostic(
                code,
                messages.get(code, "BCA statement parsing failed."),
                severity=DiagnosticSeverity.ERROR,
                review_required=False,
            ),
        ),
    )


class BCAMonthlyStatementAdapter:
    descriptor = BCA_DESCRIPTOR

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        if source.binary_payload is None:
            raise AdapterContractError(
                "BCA monthly statement adapter requires binary PDF payload"
            )

        if (
            source.template_fingerprint is not None
            and source.template_fingerprint != BCA_TEMPLATE_FINGERPRINT
        ):
            raise AdapterContractError(
                "BCA monthly statement template fingerprint mismatch"
            )

        try:
            pages = _extract_pdf_pages(source.binary_payload)
        except _BCAExpectedFailure as exc:
            return _failure_result(source, str(exc))

        return self._parse_pages(source, pages)

    def _parse_pages(
        self,
        source: AdapterInput,
        pages: tuple[str, ...],
    ) -> AdapterResult:
        page_lines: list[tuple[int, str]] = []
        for page_number, page_text in enumerate(pages, start=1):
            for raw_line in page_text.splitlines():
                line = _normalize_space(raw_line)
                if line:
                    page_lines.append((page_number, line))

        full_text = "\n".join(line for _, line in page_lines)
        diagnostics: list[SafeDiagnostic] = []

        period = _period_from_text(full_text)
        account_raw = self._extract_account(page_lines)

        if period is None:
            diagnostics.append(
                _safe_diagnostic(
                    "BCA_PERIOD_AMBIGUOUS",
                    "BCA statement period is unavailable or ambiguous.",
                )
            )

        if account_raw is None:
            diagnostics.append(
                _safe_diagnostic(
                    "BCA_HEADER_INCOMPLETE",
                    "BCA statement account identity is unavailable.",
                )
            )

        opening_balance, closing_balance = self._extract_summary_balances(
            page_lines
        )

        if opening_balance is None or closing_balance is None:
            diagnostics.append(
                _safe_diagnostic(
                    "BCA_SUMMARY_INCOMPLETE",
                    "BCA statement balance summary is incomplete.",
                )
            )

        period_start = None
        period_end = None
        natural_key = None

        if period is not None:
            period_year, period_month = period
            period_start, period_end = _period_bounds(
                period_year,
                period_month,
            )

            if account_raw is not None:
                natural_key = _natural_document_key(
                    account_raw,
                    year=period_year,
                    month=period_month,
                )
        else:
            period_year = 0
            period_month = 0

        events: list[NormalizedEventEnvelope] = []

        if (
            period_start is not None
            and period_end is not None
            and (opening_balance is not None or closing_balance is not None)
        ):
            summary = SourceSummaryEvidence(
                currency="IDR",
                period_start=period_start,
                period_end=period_end,
                opening_balance=opening_balance,
                closing_balance=closing_balance,
            )
            summary_provenance = SourceProvenanceContract(
                source_document_id=source.source_document_id,
                raw_locator="bca:summary",
                raw_text="BCA statement balance summary",
            )
            events.append(
                NormalizedEventEnvelope(
                    source_document_id=source.source_document_id,
                    source_registry_id=self.descriptor.source_registry_id,
                    template_id=self.descriptor.template_id,
                    parser_version=self.descriptor.parser_version,
                    source_channel=self.descriptor.source_channel,
                    event_role=EventRole.SOURCE_SUMMARY,
                    source_event_id=None,
                    row_fingerprint=_summary_fingerprint(
                        period_start=period_start,
                        period_end=period_end,
                        opening_balance=opening_balance,
                        closing_balance=closing_balance,
                    ),
                    evidence_quality=ConfidenceLevel.HIGH,
                    parse_confidence=ConfidenceLevel.HIGH,
                    provenance=summary_provenance,
                    payload=summary,
                )
            )

        row_events, row_diagnostics = self._parse_transactions(
            source=source,
            page_lines=page_lines,
            account_raw=account_raw,
            period=period,
            period_start=period_start,
            period_end=period_end,
        )
        events.extend(row_events)
        diagnostics.extend(row_diagnostics)

        cash_count = sum(
            1 for event in events
            if event.event_role is EventRole.CASH_MOVEMENT
        )
        if cash_count == 0 and not diagnostics:
            diagnostics.append(
                _safe_diagnostic(
                    "BCA_NO_TRANSACTION_ROWS",
                    "BCA statement contains no trusted transaction rows.",
                )
            )

        review_diagnostics = tuple(
            item for item in diagnostics if item.review_required
        )

        parse_status = (
            AdapterParseStatus.REVIEW_REQUIRED
            if review_diagnostics
            else AdapterParseStatus.COMPLETED
        )

        review_reasons = tuple(
            dict.fromkeys(item.code for item in review_diagnostics)
        )

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=parse_status,
            period_status=(
                PeriodStatus.CLOSED
                if period is not None
                else PeriodStatus.UNKNOWN
            ),
            events=tuple(events),
            diagnostics=tuple(diagnostics),
            review_reasons=review_reasons,
            period_start=period_start,
            period_end=period_end,
            natural_document_key_candidate=natural_key,
        )

    @staticmethod
    def _extract_account(
        page_lines: list[tuple[int, str]],
    ) -> str | None:
        if not page_lines:
            return None

        first_page_num = page_lines[0][0]
        first_page_matches: list[str] = []
        all_matches: list[str] = []

        for p_idx, line in page_lines:
            match = _ACCOUNT_RE.search(line)
            if match:
                val = match.group(1).strip()
                all_matches.append(val)
                if p_idx == first_page_num:
                    first_page_matches.append(val)

        target_matches = first_page_matches or all_matches
        normalized = {
            _normalize_account_identity(value): value
            for value in target_matches
            if value.strip()
        }

        if len(normalized) == 1:
            return next(iter(normalized.values()))

        if all_matches:
            first_val = all_matches[0]
            try:
                _normalize_account_identity(first_val)
                return first_val
            except ValueError:
                return None

        return None

    @staticmethod
    def _extract_summary_balances(
        page_lines: list[tuple[int, str]],
    ) -> tuple[Decimal | None, Decimal | None]:
        main_lines: list[tuple[int, str]] = []
        for p_idx, line in page_lines:
            if "FASILITAS" in line.upper():
                break
            main_lines.append((p_idx, line))

        opening_balance: Decimal | None = None
        for _, line in main_lines:
            m = _OPENING_RE.search(line)
            if m:
                try:
                    opening_balance = _parse_decimal(m.group(1))
                    break
                except (InvalidOperation, ValueError):
                    pass
            m_date = _OPENING_DATE_ROW_RE.search(line)
            if m_date:
                try:
                    opening_balance = _parse_decimal(m_date.group(1))
                    break
                except (InvalidOperation, ValueError):
                    pass

        closing_balance: Decimal | None = None
        for _, line in reversed(main_lines):
            m = _CLOSING_RE.search(line)
            if m:
                try:
                    closing_balance = _parse_decimal(m.group(1))
                    break
                except (InvalidOperation, ValueError):
                    pass

        return opening_balance, closing_balance

    def _parse_transactions(
        self,
        *,
        source: AdapterInput,
        page_lines: list[tuple[int, str]],
        account_raw: str | None,
        period: tuple[int, int] | None,
        period_start: str | None,
        period_end: str | None,
    ) -> tuple[
        list[NormalizedEventEnvelope],
        list[SafeDiagnostic],
    ]:
        events: list[NormalizedEventEnvelope] = []
        diagnostics: list[SafeDiagnostic] = []

        tx_groups: list[list[tuple[int, str]]] = []
        current_tx: list[tuple[int, str]] = []

        for page_number, line in page_lines:
            if _SECTION_BOUNDARY_RE.match(line):
                if current_tx:
                    tx_groups.append(current_tx)
                    current_tx = []
                continue

            date_prefix = _DATE_PREFIX_RE.match(line)
            if date_prefix:
                rest = date_prefix.group("rest")
                if rest.upper().startswith("SALDO AWAL"):
                    if current_tx:
                        tx_groups.append(current_tx)
                        current_tx = []
                    continue

                if current_tx and re.match(r"^WSID\w+", rest, re.IGNORECASE):
                    current_tx.append((page_number, line))
                    continue

                if current_tx:
                    tx_groups.append(current_tx)
                current_tx = [(page_number, line)]
            elif current_tx:
                current_tx.append((page_number, line))

        if current_tx:
            tx_groups.append(current_tx)

        for row_index, group in enumerate(tx_groups, start=1):
            row_events, row_diags = self._finalize_row_group(
                source=source,
                group=group,
                account_raw=account_raw,
                period=period,
                period_start=period_start,
                period_end=period_end,
                row_index=row_index,
            )
            events.extend(row_events)
            diagnostics.extend(row_diags)

        return events, diagnostics

    def _finalize_row_group(
        self,
        *,
        source: AdapterInput,
        group: list[tuple[int, str]],
        account_raw: str | None,
        period: tuple[int, int] | None,
        period_start: str | None,
        period_end: str | None,
        row_index: int,
    ) -> tuple[
        list[NormalizedEventEnvelope],
        list[SafeDiagnostic],
    ]:
        page_number = group[0][0]
        lines = [line for _, line in group]
        first_line = lines[0]

        date_match = _DATE_PREFIX_RE.match(first_line)
        if not date_match:
            return (
                [],
                [
                    _safe_diagnostic(
                        "BCA_STRUCTURE_TRUNCATED",
                        "BCA transaction row is structurally incomplete.",
                    )
                ],
            )

        if period is None:
            return (
                [],
                [
                    _safe_diagnostic(
                        "BCA_ROW_DATE_AMBIGUOUS",
                        "BCA transaction date cannot be resolved without a period.",
                    )
                ],
            )

        period_year, period_month = period

        try:
            posted_at = _parse_source_date(
                date_match.group("date"),
                period_year=period_year,
                period_month=period_month,
            )
        except ValueError:
            return (
                [],
                [
                    _safe_diagnostic(
                        "BCA_ROW_DATE_AMBIGUOUS",
                        "BCA transaction posting date is ambiguous.",
                    )
                ],
            )

        # Direction ambiguity check
        full_row_text = " ".join(lines)
        has_db = bool(re.search(r"\b(DB|DEBIT)\b", full_row_text, re.IGNORECASE))
        has_cr = bool(
            re.search(r"\b(CR|KR|KREDIT|CREDIT)\b", full_row_text, re.IGNORECASE)
        )

        if has_db and has_cr:
            return (
                [],
                [
                    _safe_diagnostic(
                        "BCA_ROW_DIRECTION_AMBIGUOUS",
                        "BCA transaction direction evidence is ambiguous.",
                    )
                ],
            )

        amount_raw: str | None = None
        balance_raw: str | None = None
        direction_raw: str | None = None
        desc_parts: list[str] = []
        continuation_lines: list[str] = []

        # Try Case 1: First line matches single-line transaction regex
        m1 = _RE_1LINE.match(first_line)
        if m1 and m1.group("amount"):
            amount_raw = m1.group("amount")
            balance_raw = m1.group("balance")
            dir_cand = m1.group("direction")
            if dir_cand:
                direction_raw = dir_cand.upper()
            else:
                if has_cr or re.search(
                    r"\b(BUNGA|SETORAN)\b", first_line, re.IGNORECASE
                ):
                    direction_raw = "CR"
                elif has_db:
                    direction_raw = "DB"
                else:
                    return (
                        [],
                        [
                            _safe_diagnostic(
                                "BCA_ROW_DIRECTION_AMBIGUOUS",
                                "BCA transaction direction evidence is ambiguous.",
                            )
                        ],
                    )
            desc_parts.append(m1.group("body").strip())
            continuation_lines = list(lines[1:])
        else:
            # Case 2: Multi-line where last line carries numeric / direction fields
            last_line = lines[-1]
            m_db_bal = _RE_AMT_DB_BAL.match(last_line)
            m_db = _RE_AMT_DB.match(last_line)
            m_cr_bal = _RE_AMT_CR_BAL.match(last_line)
            m_cr = _RE_AMT_CR.match(last_line)
            m_bal = _RE_AMT_BAL.match(last_line)
            m_only = _RE_AMT_ONLY.match(last_line)

            m_last = (
                m_db_bal or m_db or m_cr_bal or m_cr or m_bal or m_only
            )
            if not m_last:
                return (
                    [],
                    [
                        _safe_diagnostic(
                            "BCA_STRUCTURE_TRUNCATED",
                            "BCA transaction row is structurally incomplete.",
                        )
                    ],
                )

            amount_raw = m_last.group("amount")
            balance_raw = (
                m_last.group("balance")
                if "balance" in m_last.groupdict()
                else None
            )

            if m_db_bal or m_db:
                direction_raw = "DB"
            elif m_cr_bal or m_cr:
                direction_raw = "CR"
            else:
                if has_cr or re.search(
                    r"\b(BUNGA|SETORAN)\b", full_row_text, re.IGNORECASE
                ):
                    direction_raw = "CR"
                elif has_db:
                    direction_raw = "DB"
                else:
                    return (
                        [],
                        [
                            _safe_diagnostic(
                                "BCA_ROW_DIRECTION_AMBIGUOUS",
                                "BCA transaction direction evidence is ambiguous.",
                            )
                        ],
                    )

            desc_parts.append(date_match.group("rest").strip())
            continuation_lines = list(lines[1:-1])
            if m_last.group("prefix"):
                continuation_lines.append(
                    m_last.group("prefix").strip()
                )

        try:
            amount = _parse_decimal(amount_raw)
            balance_after = (
                _parse_decimal(balance_raw) if balance_raw else None
            )
        except (InvalidOperation, ValueError):
            return (
                [],
                [
                    _safe_diagnostic(
                        "BCA_ROW_AMOUNT_INVALID",
                        "BCA transaction amount evidence is invalid.",
                    )
                ],
            )

        if direction_raw in {"DB", "DEBIT"}:
            direction = EventDirection.OUTFLOW
        elif direction_raw in {"CR", "KR", "KREDIT", "CREDIT"}:
            direction = EventDirection.INFLOW
        else:
            return (
                [],
                [
                    _safe_diagnostic(
                        "BCA_ROW_DIRECTION_AMBIGUOUS",
                        "BCA transaction direction evidence is ambiguous.",
                    )
                ],
            )

        effective_values: list[str] = []
        reference_values: list[str] = []
        counterparty_values: list[str] = []
        poket_key = None
        poket_name = None
        diagnostics: list[SafeDiagnostic] = []

        eff_m = _EFFECTIVE_RE.search(first_line) or _EFFECTIVE_EN_RE.search(
            first_line
        )
        if eff_m:
            effective_values.append(eff_m.group(1))

        for cline in continuation_lines:
            eff_m = _EFFECTIVE_RE.search(cline) or _EFFECTIVE_EN_RE.search(cline)
            if eff_m:
                effective_values.append(eff_m.group(1))
                continue

            ref_m = _REFERENCE_RE.match(cline)
            if ref_m:
                reference_values.append(ref_m.group(1).strip())
                continue

            cp_m = _COUNTERPARTY_RE.match(cline)
            if cp_m:
                counterparty_values.append(cp_m.group(1).strip())
                continue

            pk_m = _POKET_KEY_RE.match(cline)
            if pk_m:
                poket_key = pk_m.group(1).strip()
                continue

            pn_m = _POKET_NAME_RE.match(cline)
            if pn_m:
                poket_name = pn_m.group(1).strip()
                continue

            desc_parts.append(cline)

        occurred_at = None
        if effective_values:
            parsed_eff: list[str] = []
            for v in effective_values:
                try:
                    parsed_eff.append(
                        _parse_source_date(
                            v,
                            period_year=period_year,
                            period_month=period_month,
                        )
                    )
                except ValueError:
                    diagnostics.append(
                        _safe_diagnostic(
                            "BCA_ROW_DATE_AMBIGUOUS",
                            "BCA transaction effective date is ambiguous.",
                        )
                    )
            unique_eff = tuple(dict.fromkeys(parsed_eff))
            if len(unique_eff) == 1:
                occurred_at = unique_eff[0]
            elif len(unique_eff) > 1:
                diagnostics.append(
                    _safe_diagnostic(
                        "BCA_ROW_DATE_AMBIGUOUS",
                        "BCA transaction effective dates conflict.",
                    )
                )

        description_raw = _normalize_space(" ".join(desc_parts))
        reference_raw = (
            _normalize_space(" ".join(reference_values))
            if reference_values
            else None
        )
        counterparty_raw = (
            _normalize_space(" ".join(counterparty_values))
            if counterparty_values
            else None
        )

        combined_lifecycle_text = " ".join([first_line, *continuation_lines])
        event_hint = _lifecycle_hint(combined_lifecycle_text)

        fingerprint = _row_fingerprint(
            posted_at=posted_at,
            occurred_at=occurred_at,
            direction_raw=direction_raw,
            amount=amount,
            description_raw=description_raw,
            reference_raw=reference_raw,
            balance_after=balance_after,
            row_index=row_index,
        )

        raw_row_text = "\n".join(lines)
        provenance = SourceProvenanceContract(
            source_document_id=source.source_document_id,
            raw_locator=f"bca:page:{page_number}:row:{row_index}",
            page_number=page_number,
            row_index=row_index,
            raw_text=raw_row_text,
            raw_reference=reference_raw or "",
            raw_description=description_raw,
        )

        payload = CashMovementEvidence(
            amount=amount,
            currency="IDR",
            direction=direction,
            status=SourceEventStatus.POSTED,
            source_account_key_raw=account_raw,
            occurred_at=occurred_at,
            posted_at=posted_at,
            settlement_date=None,
            direction_raw=direction_raw,
            counterparty_raw=counterparty_raw,
            description_raw=description_raw,
            provider_transaction_id_raw=None,
            reference_raw=reference_raw,
            balance_after=balance_after,
            event_hint=event_hint,
        )

        events = [
            NormalizedEventEnvelope(
                source_document_id=source.source_document_id,
                source_registry_id=self.descriptor.source_registry_id,
                template_id=self.descriptor.template_id,
                parser_version=self.descriptor.parser_version,
                source_channel=self.descriptor.source_channel,
                event_role=EventRole.CASH_MOVEMENT,
                source_event_id=None,
                row_fingerprint=fingerprint,
                evidence_quality=ConfidenceLevel.HIGH,
                parse_confidence=ConfidenceLevel.HIGH,
                provenance=provenance,
                payload=payload,
            )
        ]

        if poket_key or poket_name:
            if poket_key and poket_name and account_raw:
                account_payload = ObservedAccountEvidence(
                    observed_provider_account_key=poket_key,
                    display_name_raw=poket_name,
                    institution_id="bca",
                    parent_observed_key=account_raw,
                    provider_state_raw=event_hint,
                    observed_at=posted_at,
                    period_start=period_start,
                    period_end=period_end,
                )
                account_fingerprint = sha256(
                    (
                        "bca-account-observation-v1"
                        + "\x1f"
                        + fingerprint
                        + "\x1f"
                        + poket_key
                    ).encode("utf-8")
                ).hexdigest()
                account_provenance = SourceProvenanceContract(
                    source_document_id=source.source_document_id,
                    raw_locator=(
                        f"bca:page:{page_number}:"
                        f"row:{row_index}:poket"
                    ),
                    page_number=page_number,
                    row_index=row_index,
                    raw_text=raw_row_text,
                )
                events.append(
                    NormalizedEventEnvelope(
                        source_document_id=source.source_document_id,
                        source_registry_id=self.descriptor.source_registry_id,
                        template_id=self.descriptor.template_id,
                        parser_version=self.descriptor.parser_version,
                        source_channel=self.descriptor.source_channel,
                        event_role=EventRole.ACCOUNT_OBSERVATION,
                        source_event_id=None,
                        row_fingerprint=account_fingerprint,
                        evidence_quality=ConfidenceLevel.HIGH,
                        parse_confidence=ConfidenceLevel.HIGH,
                        provenance=account_provenance,
                        payload=account_payload,
                    )
                )
            else:
                diagnostics.append(
                    _safe_diagnostic(
                        "BCA_POKET_IDENTITY_UNSTABLE",
                        "BCA Poket evidence lacks a stable provider identity.",
                    )
                )

        return events, diagnostics


    def extract_account_observations(
        self,
        source_or_result: Any,
    ) -> list[AccountDiscoveryObservation]:
        if isinstance(source_or_result, AdapterInput):
            result = self.parse(source_or_result)
        else:
            result = source_or_result

        if result is None or result.parse_status == AdapterParseStatus.FAILED:
            return []

        effective_date = result.period_start or "2026-01-01"

        # Locate root account key from events
        root_key: str | None = None
        for e in (result.events or ()):
            if e.event_role == EventRole.CASH_MOVEMENT and getattr(e.payload, "source_account_key_raw", None):
                root_key = e.payload.source_account_key_raw
                break
            if e.event_role == EventRole.ACCOUNT_OBSERVATION and getattr(e.payload, "parent_observed_key", None):
                root_key = e.payload.parent_observed_key
                break

        # Check for ambiguous or missing root identity
        ambiguous = any(
            d.code in {"BCA_ACCOUNT_IDENTITY_AMBIGUOUS", "BCA_HEADER_INCOMPLETE"}
            for d in (result.diagnostics or ())
        )

        if not root_key or ambiguous:
            return [
                AccountDiscoveryObservation(
                    institution_id="bca",
                    source_registry_id=self.descriptor.source_registry_id,
                    raw_account_key="",
                    display_name_safe="BCA Tahapan",
                    account_type=AccountType.TRANSACTIONAL,
                    effective_date=effective_date,
                    ownership_state=OwnershipState.UNKNOWN,
                    ownership_confidence=ConfidenceLevel.UNKNOWN,
                    parent_raw_account_key=None,
                )
            ]

        observations: list[AccountDiscoveryObservation] = [
            AccountDiscoveryObservation(
                institution_id="bca",
                source_registry_id=self.descriptor.source_registry_id,
                raw_account_key=root_key,
                display_name_safe="BCA Tahapan",
                account_type=AccountType.TRANSACTIONAL,
                effective_date=effective_date,
                ownership_state=OwnershipState.OWNED,
                ownership_confidence=ConfidenceLevel.HIGH,
                parent_raw_account_key=None,
            )
        ]

        seen_pokets: set[str] = set()
        for e in (result.events or ()):
            if e.event_role == EventRole.ACCOUNT_OBSERVATION:
                poket_payload = e.payload
                poket_key = getattr(poket_payload, "observed_provider_account_key", None)
                if poket_key and poket_key not in seen_pokets and poket_key != root_key:
                    seen_pokets.add(poket_key)
                    observations.append(
                        AccountDiscoveryObservation(
                            institution_id="bca",
                            source_registry_id=self.descriptor.source_registry_id,
                            raw_account_key=poket_key,
                            display_name_safe="BCA Poket",
                            account_type=AccountType.SAVINGS,
                            effective_date=effective_date,
                            ownership_state=OwnershipState.OWNED,
                            ownership_confidence=ConfidenceLevel.HIGH,
                            parent_raw_account_key=root_key,
                            parent_account_type=AccountType.TRANSACTIONAL,
                        )
                    )

        return observations


def extract_account_observations(
    source_or_result: Any,
) -> list[AccountDiscoveryObservation]:
    return BCAMonthlyStatementAdapter().extract_account_observations(source_or_result)


__all__ = [
    "BCA_DESCRIPTOR",
    "BCA_TEMPLATE_FINGERPRINT",
    "BCAMonthlyStatementAdapter",
    "extract_account_observations",
]
