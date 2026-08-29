from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re

from pypdf import PdfReader

from .ingestion_adapter import (
    AccountPeriodSummaryEvidence,
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

SEABANK_SOURCE_REGISTRY_ID = "seabank_statement"
SEABANK_TEMPLATE_ID = "seabank_estatement_v1"
SEABANK_PARSER_VERSION = "parser-v1"
SEABANK_ADAPTER_ID = "seabank-monthly-statement-v1"
SEABANK_TEMPLATE_FINGERPRINT = (
    "fc7add5e7b3a321692fda3196b25083e3620bfcf15f4eb63313fdc3de4a99656"
)

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
}

_DATE_TOKEN_RE = re.compile(r"^(\d{1,2})?\s*([A-Za-z]{3})$")
_ACCOUNT_HEADER_RE = re.compile(
    r"NO\.\s*REKENING\s*SEABANK\s*:\s*([0-9]{8,32})", re.I
)
_PERIOD_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s+sampai\s+(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})",
    re.I,
)


def _parse_decimal(text: str) -> Decimal:
    cleaned = re.sub(r"[^\d,.-]", "", text).strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        val = Decimal(cleaned)
        if not val.is_finite():
            raise AdapterContractError("non-finite decimal")
        return val
    except (InvalidOperation, ValueError) as exc:
        raise AdapterContractError("invalid decimal format") from exc


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _get_last_day_of_month(year: int, month: int) -> int:
    if month == 2 and _is_leap_year(year):
        return 29
    for _, (m, d) in _MONTHS.items():
        if m == month:
            return d
    return 30


@dataclass(frozen=True)
class _ParsedSeaBankRow:
    page_number: int
    date_str: str
    occurred_at: str
    direction: EventDirection
    direction_raw: str
    amount: Decimal
    description_raw: str
    category_raw: str
    balance_after: Decimal | None


@dataclass(frozen=True)
class _SeaBankDocumentSummary:
    account_number: str
    account_holder: str
    period_start: str
    period_end: str
    period_str: str
    opening_balance: Decimal
    total_debit: Decimal
    total_credit: Decimal
    closing_balance: Decimal
    is_5_column_layout: bool


@dataclass(frozen=True)
class SeaBankMonthlyStatementAdapter(UniversalSourceAdapter):
    descriptor: AdapterDescriptor = AdapterDescriptor(
        adapter_id=SEABANK_ADAPTER_ID,
        source_registry_id=SEABANK_SOURCE_REGISTRY_ID,
        template_id=SEABANK_TEMPLATE_ID,
        parser_version=SEABANK_PARSER_VERSION,
        source_channel=SourceChannel.PDF,
    )

    @staticmethod
    def _extract_header_account_id(page_1_text: str) -> str | None:
        matches = _ACCOUNT_HEADER_RE.findall(page_1_text)
        if not matches:
            return None
        unique_accounts = list(dict.fromkeys(m.strip() for m in matches if m.strip()))
        if len(unique_accounts) != 1:
            return None
        return unique_accounts[0]

    def _extract_document_summary(
        self, page_1_text: str
    ) -> _SeaBankDocumentSummary | None:
        lines = [
            re.sub(r"\s+", " ", l).strip()
            for l in page_1_text.splitlines()
            if l.strip()
        ]

        account_number = self._extract_header_account_id(page_1_text)
        if not account_number:
            return None

        # Account holder
        account_holder = "TABUNGAN"
        for l in lines:
            if "REKENING KORAN" in l:
                continue
            if "S/N S01-" in l or "halaman" in l:
                continue
            if re.match(r"^[A-Z\s.,'-]{3,60}$", l) and "RINGKASAN" not in l:
                account_holder = l.strip()
                break

        # Period
        m_per = _PERIOD_RE.search(page_1_text)
        if not m_per:
            return None

        s_d = int(m_per.group(1))
        s_m_str = m_per.group(2).upper()
        s_y = int(m_per.group(3))
        e_d = int(m_per.group(4))
        e_m_str = m_per.group(5).upper()
        e_y = int(m_per.group(6))

        if s_m_str not in _MONTHS or e_m_str not in _MONTHS:
            return None

        s_m, _ = _MONTHS[s_m_str]
        e_m, _ = _MONTHS[e_m_str]

        period_start = f"{s_y:04d}-{s_m:02d}-{s_d:02d}"
        period_end = f"{e_y:04d}-{e_m:02d}-{e_d:02d}"
        period_str = f"{e_y:04d}-{e_m:02d}"

        # Layout check
        is_5_col = False
        in_hdr = False
        for l in lines:
            if "TABUNGAN - RINCIAN TRANSAKSI" in l:
                in_hdr = True
            elif in_hdr and ("TANGGAL TRANSAKSI" in l or "KELUAR (IDR)" in l):
                if "SALDO AKHIR" in l:
                    is_5_col = True
                in_hdr = False

        # Summary amounts
        opening_balance = Decimal("0")
        total_debit = Decimal("0")
        total_credit = Decimal("0")
        closing_balance = Decimal("0")

        summary_found = False
        for l in lines:
            if l.startswith("TABUNGAN "):
                parts = l.split()
                if len(parts) == 5:
                    try:
                        opening_balance = _parse_decimal(parts[1])
                        total_debit = _parse_decimal(parts[2])
                        total_credit = _parse_decimal(parts[3])
                        closing_balance = _parse_decimal(parts[4])
                        summary_found = True
                        break
                    except Exception:
                        return None

        if not summary_found:
            return None

        # Equation validation
        calc_closing = opening_balance - total_debit + total_credit
        if abs(calc_closing - closing_balance) >= Decimal("0.005"):
            return None

        return _SeaBankDocumentSummary(
            account_number=account_number,
            account_holder=account_holder,
            period_start=period_start,
            period_end=period_end,
            period_str=period_str,
            opening_balance=opening_balance,
            total_debit=total_debit,
            total_credit=total_credit,
            closing_balance=closing_balance,
            is_5_column_layout=is_5_col,
        )

    def _parse_seabank_transactions(
        self,
        reader: PdfReader,
        summary: _SeaBankDocumentSummary,
    ) -> tuple[list[_ParsedSeaBankRow], list[SafeDiagnostic]]:
        rows: list[_ParsedSeaBankRow] = []
        diagnostics: list[SafeDiagnostic] = []

        doc_txs_raw: list[tuple[int, list[tuple[float, float, str]]]] = []
        in_tx_section = False
        curr_tx_chunks: list[tuple[float, float, str]] = []

        for p_idx, page in enumerate(reader.pages):
            chunks: list[tuple[float, float, str]] = []

            def visitor(
                text: str,
                cm: list[float],
                tm: list[float],
                font_dict: dict,
                font_size: float,
            ) -> None:
                t = text.strip()
                if t:
                    chunks.append(
                        (
                            round(cm[4] + tm[4], 1),
                            round(cm[5] + tm[5], 1),
                            t,
                        )
                    )

            try:
                page.extract_text(visitor_text=visitor)
            except Exception:
                diagnostics.append(
                    SafeDiagnostic(
                        code="SEABANK_STRUCTURE_TRUNCATED",
                        message="Failed to extract text from PDF page stream",
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                return [], diagnostics

            for c in chunks:
                if "TABUNGAN - RINCIAN TRANSAKSI" in c[2]:
                    in_tx_section = True
                    continue
                if in_tx_section and any(
                    k in c[2]
                    for k in [
                        "TABUNGAN - RINCIAN BUNGA",
                        "DEPOSITO - RINCIAN",
                        "Ketentuan Umum",
                        "TIDAK ADA TRANSAKSI",
                    ]
                ):
                    in_tx_section = False
                    if curr_tx_chunks:
                        doc_txs_raw.append((p_idx + 1, curr_tx_chunks))
                        curr_tx_chunks = []
                    continue

                if in_tx_section:
                    # Vertical margin filtering
                    if c[1] < 100:
                        continue
                    if p_idx == 0 and c[1] > 420:
                        continue
                    if p_idx > 0 and c[1] > 710:
                        continue

                    # Header keyword filtering
                    if any(
                        k in c[2]
                        for k in [
                            "Hubungi kami",
                            "Telepon",
                            "Luar Negeri",
                            "Email cs@",
                            "live chat",
                            "NO. REKENING SEABANK:",
                            "JL MONDOROKO",
                            "BANJARARUM",
                            "SINGOSARI",
                            "KABUPATEN MALANG",
                            "INDONESIA",
                        ]
                    ):
                        continue
                    if c[2].startswith("S/N S01-") or re.match(
                        r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}$", c[2]
                    ):
                        continue

                    # Transaction anchor: 45 <= x <= 55 and matches date token
                    if 45.0 <= c[0] <= 55.0 and _DATE_TOKEN_RE.match(c[2]):
                        if curr_tx_chunks:
                            doc_txs_raw.append((p_idx + 1, curr_tx_chunks))
                        curr_tx_chunks = [c]
                    else:
                        # Append only if inside an active transaction block
                        if curr_tx_chunks:
                            curr_tx_chunks.append(c)

        if curr_tx_chunks:
            doc_txs_raw.append((len(reader.pages), curr_tx_chunks))

        st_year = int(summary.period_end[:4])

        for page_num, tx_c in doc_txs_raw:
            d_token = tx_c[0][2]
            m_date = _DATE_TOKEN_RE.match(d_token)
            if not m_date:
                continue

            day_str = m_date.group(1)
            mon_str = m_date.group(2).upper()
            if mon_str not in _MONTHS:
                continue

            mon_num, _ = _MONTHS[mon_str]
            if day_str:
                day_num = int(day_str)
                occurred_at = f"{st_year:04d}-{mon_num:02d}-{day_num:02d}"
            else:
                last_day = _get_last_day_of_month(st_year, mon_num)
                occurred_at = f"{st_year:04d}-{mon_num:02d}-{last_day:02d}"

            if not summary.is_5_column_layout:
                # 4-column layout
                out_c = [
                    c
                    for c in tx_c
                    if 380 <= c[0] <= 450 and re.match(r"^\d[\d.,]*$", c[2])
                ]
                in_c = [
                    c
                    for c in tx_c
                    if 500 <= c[0] <= 570 and re.match(r"^\d[\d.,]*$", c[2])
                ]
                desc_c = [
                    c[2]
                    for c in tx_c[1:]
                    if not re.match(r"^\d[\d.,]*$", c[2]) and c[0] > 0
                ]

                try:
                    amt_out = _parse_decimal(out_c[-1][2]) if out_c else None
                    amt_in = _parse_decimal(in_c[-1][2]) if in_c else None
                except Exception:
                    diagnostics.append(
                        SafeDiagnostic(
                            code="SEABANK_ROW_AMOUNT_INVALID",
                            message="Invalid transaction row amount",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    )
                    return [], diagnostics

                if amt_in is not None:
                    amt = amt_in
                    direction = EventDirection.INFLOW
                    dir_raw = "MASUK"
                elif amt_out is not None:
                    amt = amt_out
                    direction = EventDirection.OUTFLOW
                    dir_raw = "KELUAR"
                else:
                    diagnostics.append(
                        SafeDiagnostic(
                            code="SEABANK_ROW_AMOUNT_INVALID",
                            message="Missing transaction amount in row",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    )
                    return [], diagnostics

                cat_raw = desc_c[-1] if desc_c else ""
                desc_raw = " ".join(desc_c) if desc_c else ""

                rows.append(
                    _ParsedSeaBankRow(
                        page_number=page_num,
                        date_str=d_token,
                        occurred_at=occurred_at,
                        direction=direction,
                        direction_raw=dir_raw,
                        amount=amt,
                        description_raw=desc_raw,
                        category_raw=cat_raw,
                        balance_after=None,
                    )
                )
            else:
                # 5-column layout
                out_c = [
                    c
                    for c in tx_c
                    if 290 <= c[0] <= 370 and re.match(r"^\d[\d.,]*$", c[2])
                ]
                in_c = [
                    c
                    for c in tx_c
                    if 400 <= c[0] <= 480 and re.match(r"^\d[\d.,]*$", c[2])
                ]
                bal_c = [
                    c
                    for c in tx_c
                    if 500 <= c[0] <= 570 and re.match(r"^\d[\d.,]*$", c[2])
                ]
                desc_c = [
                    c[2]
                    for c in tx_c[1:]
                    if not re.match(r"^\d[\d.,]*$", c[2]) and c[0] > 0
                ]

                try:
                    amt_out = _parse_decimal(out_c[-1][2]) if out_c else None
                    amt_in = _parse_decimal(in_c[-1][2]) if in_c else None
                    bal = _parse_decimal(bal_c[-1][2]) if bal_c else None
                except Exception:
                    diagnostics.append(
                        SafeDiagnostic(
                            code="SEABANK_ROW_AMOUNT_INVALID",
                            message="Invalid transaction row amount",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    )
                    return [], diagnostics

                if amt_in is not None:
                    amt = amt_in
                    direction = EventDirection.INFLOW
                    dir_raw = "MASUK"
                elif amt_out is not None:
                    amt = amt_out
                    direction = EventDirection.OUTFLOW
                    dir_raw = "KELUAR"
                else:
                    diagnostics.append(
                        SafeDiagnostic(
                            code="SEABANK_ROW_AMOUNT_INVALID",
                            message="Missing transaction amount in row",
                            severity=DiagnosticSeverity.ERROR,
                        )
                    )
                    return [], diagnostics

                cat_raw = desc_c[-1] if desc_c else ""
                desc_raw = " ".join(desc_c) if desc_c else ""

                rows.append(
                    _ParsedSeaBankRow(
                        page_number=page_num,
                        date_str=d_token,
                        occurred_at=occurred_at,
                        direction=direction,
                        direction_raw=dir_raw,
                        amount=amt,
                        description_raw=desc_raw,
                        category_raw=cat_raw,
                        balance_after=bal,
                    )
                )

        return rows, diagnostics

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        if source.binary_payload is None:
            raise AdapterContractError(
                "SeaBankMonthlyStatementAdapter requires binary_payload"
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
                            code="SEABANK_PDF_ENCRYPTED_UNSUPPORTED",
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
                            code="SEABANK_STRUCTURE_TRUNCATED",
                            message="PDF has 0 pages",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            p1_text = reader.pages[0].extract_text()
        except Exception:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="SEABANK_STRUCTURE_TRUNCATED",
                        message="Failed to parse PDF binary payload",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # 1. Summary & Period Extraction
        summary = self._extract_document_summary(p1_text)
        if not summary:
            acc = self._extract_header_account_id(p1_text)
            if not acc:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    diagnostics=(
                        SafeDiagnostic(
                            code="SEABANK_HEADER_INCOMPLETE",
                            message="Missing or invalid NO. REKENING SEABANK header",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            m_per = _PERIOD_RE.search(p1_text)
            if not m_per:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    diagnostics=(
                        SafeDiagnostic(
                            code="SEABANK_PERIOD_AMBIGUOUS",
                            message="Missing or unparseable statement period",
                            severity=DiagnosticSeverity.ERROR,
                        ),
                    ),
                )
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.CLOSED,
                diagnostics=(
                    SafeDiagnostic(
                        code="SEABANK_SUMMARY_INCOMPLETE",
                        message="Malformed or mathematically inconsistent RINGKASAN REKENING",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # 2. Extract Transactions
        rows, row_diagnostics = self._parse_seabank_transactions(
            reader, summary
        )
        if row_diagnostics:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.CLOSED,
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
                period_status=PeriodStatus.CLOSED,
                diagnostics=(
                    SafeDiagnostic(
                        code="SEABANK_SUMMARY_MISMATCH",
                        message="Calculated transaction sum does not match document summary",
                        severity=DiagnosticSeverity.ERROR,
                    ),
                ),
            )

        # Natural Document Key
        acc_hash = sha256(summary.account_number.encode("utf-8")).hexdigest()
        natural_key = (
            f"{SEABANK_SOURCE_REGISTRY_ID}:{acc_hash}:{summary.period_str}"
        )

        provenance = SourceProvenanceContract(
            source_document_id=source.source_document_id,
            raw_locator="page:1:document_summary",
            page_number=1,
            row_index=1,
            raw_text="RINGKASAN REKENING",
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

        # Evidence 2: ACCOUNT_OBSERVATION
        account_evidence = ObservedAccountEvidence(
            observed_provider_account_key=summary.account_number,
            institution_id="seabank",
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
                    raw_text="NO. REKENING SEABANK",
                ),
                payload=account_evidence,
            )
        )

        # Evidence 3: CASH_MOVEMENT
        for seq, r in enumerate(rows):
            fp = sha256(
                f"{summary.period_str}:{summary.account_number}:{r.occurred_at}:{r.direction.value}:{r.amount}:{r.category_raw}:{r.description_raw}:{r.balance_after}:{seq}".encode(
                    "utf-8"
                )
            ).hexdigest()

            cash_evidence = CashMovementEvidence(
                amount=r.amount,
                currency="IDR",
                direction=r.direction,
                status=SourceEventStatus.POSTED,
                occurred_at=f"{r.occurred_at}T00:00:00",
                posted_at=f"{r.occurred_at}T00:00:00",
                settlement_date=r.occurred_at,
                direction_raw=r.direction_raw,
                status_raw="POSTED",
                counterparty_raw=r.description_raw or None,
                provider_category_raw=r.category_raw or None,
                description_raw=r.description_raw or None,
                balance_after=r.balance_after,
            )

            envelopes.append(
                NormalizedEventEnvelope(
                    source_document_id=source.source_document_id,
                    source_registry_id=self.descriptor.source_registry_id,
                    template_id=self.descriptor.template_id,
                    parser_version=self.descriptor.parser_version,
                    source_channel=self.descriptor.source_channel,
                    event_role=EventRole.CASH_MOVEMENT,
                    source_event_id=None,
                    row_fingerprint=fp,
                    evidence_quality=ConfidenceLevel.HIGH,
                    parse_confidence=ConfidenceLevel.HIGH,
                    provenance=SourceProvenanceContract(
                        source_document_id=source.source_document_id,
                        raw_locator=f"page:{r.page_number}:row:{seq+1}",
                        page_number=r.page_number,
                        row_index=seq + 1,
                        raw_text=r.description_raw or f"row {seq+1}",
                    ),
                    payload=cash_evidence,
                )
            )

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            events=tuple(envelopes),
            diagnostics=(),
            review_reasons=(),
            period_start=summary.period_start,
            period_end=summary.period_end,
            natural_document_key_candidate=natural_key,
        )


def parse_seabank_monthly_statement(source: AdapterInput) -> AdapterResult:
    return SeaBankMonthlyStatementAdapter().parse(source)


__all__ = [
    "SEABANK_ADAPTER_ID",
    "SEABANK_PARSER_VERSION",
    "SEABANK_SOURCE_REGISTRY_ID",
    "SEABANK_TEMPLATE_FINGERPRINT",
    "SEABANK_TEMPLATE_ID",
    "SeaBankMonthlyStatementAdapter",
    "parse_seabank_monthly_statement",
]
