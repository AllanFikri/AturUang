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

JAGO_SOURCE_REGISTRY_ID = "jago_statement"
JAGO_TEMPLATE_ID = "jago_monthly_statement_v1"
JAGO_PARSER_VERSION = "parser-v1"
JAGO_ADAPTER_ID = "jago-monthly-statement-v1"
JAGO_TEMPLATE_FINGERPRINT = (
    "854403d679306f21bdfcb16efa0c5fa89cc714d0b545e871bbae384196be18fb"
)

_MONTHS: dict[str, tuple[int, int]] = {
    "JANUARI": (1, 31),
    "FEBRUARI": (2, 28),
    "MARET": (3, 31),
    "APRIL": (4, 30),
    "MEI": (5, 31),
    "JUNI": (6, 30),
    "JULI": (7, 31),
    "AGUSTUS": (8, 31),
    "SEPTEMBER": (9, 30),
    "OKTOBER": (10, 31),
    "NOVEMBER": (11, 30),
    "DESEMBER": (12, 31),
}

_TX_MONTHS: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MEI": 5,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGU": 8,
    "AGT": 8,
    "AUG": 8,
    "SEP": 9,
    "OKT": 10,
    "OCT": 10,
    "NOV": 11,
    "DES": 12,
    "DEC": 12,
}

_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$")
_TIME_RE = re.compile(r"^(\d{1,2})[.:](\d{2})$")
_AMT_RE = re.compile(r"^([+-])\s*(?:Rp\s*)?([0-9][0-9.,]*)$")
_BAL_RE = re.compile(r"^(?:Rp\s*)?([0-9][0-9.,]*)$")
_ID_RE = re.compile(r"^ID#?\s*([A-Za-z0-9-]+)$", re.I)
_CUSTOMER_HEADER_RE = re.compile(
    r"^([A-Z\s.,\'-]{2,100}?)\s*/\s*([0-9+]{8,32})$"
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


@dataclass(frozen=True)
class JagoMonthlyStatementAdapter:
    descriptor: AdapterDescriptor = AdapterDescriptor(
        adapter_id=JAGO_ADAPTER_ID,
        source_registry_id=JAGO_SOURCE_REGISTRY_ID,
        template_id=JAGO_TEMPLATE_ID,
        parser_version=JAGO_PARSER_VERSION,
        source_channel=SourceChannel.PDF,
    )

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        if source.binary_payload is None:
            raise AdapterContractError(
                "JagoMonthlyStatementAdapter requires binary_payload"
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
                            code="JAGO_PDF_ENCRYPTED_UNSUPPORTED",
                            severity=DiagnosticSeverity.ERROR,
                            message=(
                                "Encrypted Jago PDF statements are not"
                                " supported."
                            ),
                        ),
                    ),
                )
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="JAGO_STRUCTURE_TRUNCATED",
                        severity=DiagnosticSeverity.ERROR,
                        message="PDF stream could not be parsed as Jago statement.",
                    ),
                ),
            )

        if not pages or len(pages) < 1:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="JAGO_STRUCTURE_TRUNCATED",
                        severity=DiagnosticSeverity.ERROR,
                        message="PDF document contains no readable pages.",
                    ),
                ),
            )

        # Page 1 analysis
        p1_text = pages[0]
        p1_lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in p1_text.splitlines()
            if line.strip()
        ]

        # 1. Statement Period extraction
        period_str: str | None = None
        period_start: str | None = None
        period_end: str | None = None

        for line in p1_lines[:10]:
            upper_line = line.upper()
            for month_name, (month_num, last_day) in _MONTHS.items():
                match = re.search(rf"\b{month_name}\s+(\d{{4}})\b", upper_line)
                if match:
                    year = int(match.group(1))
                    if month_num == 2 and _is_leap_year(year):
                        last_day = 29
                    period_str = f"{year:04d}-{month_num:02d}"
                    period_start = f"{year:04d}-{month_num:02d}-01"
                    period_end = f"{year:04d}-{month_num:02d}-{last_day:02d}"
                    break
            if period_str is not None:
                break

        if period_str is None or period_start is None or period_end is None:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                diagnostics=(
                    SafeDiagnostic(
                        code="JAGO_PERIOD_AMBIGUOUS",
                        severity=DiagnosticSeverity.ERROR,
                        message="Statement period could not be resolved.",
                    ),
                ),
            )

        # 2. Anchored Provider Header Customer / Account ID extraction
        header_account_id = self._extract_header_account_id(p1_lines)

        if not header_account_id:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.CLOSED,
                diagnostics=(
                    SafeDiagnostic(
                        code="JAGO_HEADER_INCOMPLETE",
                        severity=DiagnosticSeverity.ERROR,
                        message=(
                            "Header customer account identity could not be"
                            " resolved."
                        ),
                    ),
                ),
            )

        # Natural document key candidate: jago_statement:<sha256(header_acct)>:<YYYY-MM>
        header_acct_hash = sha256(
            header_account_id.encode("utf-8")
        ).hexdigest()
        natural_key = (
            f"{JAGO_SOURCE_REGISTRY_ID}:{header_acct_hash}:{period_str}"
        )

        events: list[NormalizedEventEnvelope] = []
        diagnostics: list[SafeDiagnostic] = []

        # 3. Document-Level Aggregate Summary extraction (from Page 1)
        doc_opening_balance: Decimal | None = None
        doc_incoming_total: Decimal | None = None
        doc_outgoing_total: Decimal | None = None
        doc_closing_balance: Decimal | None = None

        try:
            # Parse RINGKASAN SALDO DALAM RUPIAH table
            ringkasan_idx = None
            for idx, line in enumerate(p1_lines):
                if "RINGKASAN SALDO DALAM RUPIAH" in line.upper():
                    ringkasan_idx = idx
                    break

            if ringkasan_idx is not None:
                # Look for Saldo akhir pada ...
                for line in p1_lines[ringkasan_idx : ringkasan_idx + 15]:
                    if "SALDO AKHIR PADA" in line.upper():
                        # The following lines hold personal closing, bersama closing, total closing
                        pass

            # Under KANTONG PERSONAL and KANTONG BERSAMA or SOROTAN on page 1:
            # We compute totals from personal + bersama summaries
            personal_opening = Decimal("0")
            personal_incoming = Decimal("0")
            personal_outgoing = Decimal("0")
            personal_closing = Decimal("0")
            has_personal_summary = False

            # Scan Page 1 for pocket summary table lines
            kp_idx = None
            for idx, line in enumerate(p1_lines):
                if "KANTONG PERSONAL" in line.upper():
                    kp_idx = idx
                    break

            if kp_idx is not None:
                sub_lines = p1_lines[kp_idx:]
                # Check for Total Saldo Personal line
                for sub_idx, sline in enumerate(sub_lines):
                    if sline.upper().startswith("TOTAL SALDO PERSONAL"):
                        # Lines around here contain opening date, opening balance, closing date, closing balance
                        nums = []
                        for cand in sub_lines[sub_idx : sub_idx + 8]:
                            cand_clean = (
                                cand.replace("+", "")
                                .replace("-", "")
                                .replace("Rp", "")
                                .strip()
                            )
                            if re.match(r"^\d[\d.,]*$", cand_clean):
                                nums.append(_parse_decimal(cand_clean))
                        if len(nums) >= 2:
                            personal_opening = nums[0]
                            personal_closing = nums[1]
                            has_personal_summary = True
                        break

            # Parse SOROTAN for document-level total incoming and outgoing
            for idx, line in enumerate(p1_lines):
                if "UANG MASUK" in line.upper():
                    # check previous or current line for amount
                    prev = p1_lines[idx - 1] if idx > 0 else ""
                    m = re.search(r"(?:Rp\s*)?([0-9][0-9.,]*)", prev + " " + line)
                    if m:
                        personal_incoming = _parse_decimal(m.group(1))
                if "UANG KELUAR" in line.upper():
                    prev = p1_lines[idx - 1] if idx > 0 else ""
                    m = re.search(r"(?:Rp\s*)?([0-9][0-9.,]*)", prev + " " + line)
                    if m:
                        personal_outgoing = _parse_decimal(m.group(1))

            if has_personal_summary:
                doc_opening_balance = personal_opening
                doc_incoming_total = personal_incoming
                doc_outgoing_total = personal_outgoing
                doc_closing_balance = personal_closing

            # If document aggregate summary is resolved, emit SOURCE_SUMMARY
            if doc_closing_balance is not None:
                doc_summary_payload = SourceSummaryEvidence(
                    currency="IDR",
                    period_start=period_start,
                    period_end=period_end,
                    opening_balance=doc_opening_balance,
                    incoming_total=doc_incoming_total,
                    outgoing_total=doc_outgoing_total,
                    closing_balance=doc_closing_balance,
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
                        row_fingerprint=sha256(
                            f"{natural_key}:SOURCE_SUMMARY:{period_start}:{period_end}:{doc_opening_balance}:{doc_incoming_total}:{doc_outgoing_total}:{doc_closing_balance}".encode(
                                "utf-8"
                            )
                        ).hexdigest(),
                        evidence_quality=ConfidenceLevel.HIGH,
                        parse_confidence=ConfidenceLevel.HIGH,
                        provenance=SourceProvenanceContract(
                            source_document_id=source.source_document_id,
                            raw_locator="page:1:document_summary",
                            page_number=1,
                            row_index=1,
                            raw_text="RINGKASAN SALDO DALAM RUPIAH",
                        ),
                        payload=doc_summary_payload,
                    )
                )
        except Exception:
            diagnostics.append(
                SafeDiagnostic(
                    code="JAGO_SUMMARY_INCOMPLETE",
                    severity=DiagnosticSeverity.WARNING,
                    message="Document-level aggregate summary could not be fully resolved.",
                )
            )

        # 4. Pocket Sections & Transactions parsing across Pages 2..N
        current_pocket_id: str | None = None
        current_pocket_name: str | None = None
        seen_pockets: set[str] = set()
        seen_transactions: dict[tuple[str, str], CashMovementEvidence] = {}
        row_counter = 0

        for page_idx, page_text in enumerate(pages[1:], start=2):
            raw_lines = page_text.splitlines()
            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in raw_lines
                if line.strip()
            ]

            i = 0
            while i < len(lines):
                line = lines[i]

                # Check for Pocket Header: ID Kantong <POCKET_ID>
                match_pocket = re.match(r"^ID\s+Kantong\s+(.+)$", line, re.I)
                if match_pocket:
                    current_pocket_id = match_pocket.group(1).strip()
                    current_pocket_name = (
                        lines[i - 1] if i > 0 else "Unknown Pocket"
                    )

                    # Extract provider status/start date
                    provider_state = "Akun Aktif"
                    if i + 1 < len(lines) and "mulai" in lines[i + 1].lower():
                        provider_state = lines[i + 1]
                        if i + 2 < len(lines) and not lines[
                            i + 2
                        ].upper().startswith("SALDO"):
                            provider_state += " " + lines[i + 2]

                    # Extract Pocket Summary Balances
                    pocket_sub = lines[i : min(len(lines), i + 25)]
                    op_bal, inc_tot, out_tot, cl_bal = (
                        Decimal("0"),
                        Decimal("0"),
                        Decimal("0"),
                        Decimal("0"),
                    )
                    found_pocket_summary = False

                    if "Saldo Akhir" in pocket_sub:
                        sa_idx = pocket_sub.index("Saldo Akhir")
                        vals: list[Decimal] = []
                        for cand in pocket_sub[sa_idx + 1 :]:
                            # Break if next section starts
                            if (
                                "Tanggal & Waktu" in cand
                                or "ID Kantong" in cand
                            ):
                                break
                            cand_clean = (
                                cand.replace("+", "")
                                .replace("-", "")
                                .replace("Rp", "")
                                .strip()
                            )
                            if re.match(r"^\d[\d.,]*$", cand_clean):
                                vals.append(_parse_decimal(cand_clean))
                            if len(vals) == 4:
                                break

                        if len(vals) == 4:
                            op_bal, inc_tot, out_tot, cl_bal = vals
                            found_pocket_summary = True

                    # Emit ACCOUNT_OBSERVATION and ACCOUNT_PERIOD_SUMMARY if not emitted yet for this pocket in this doc
                    if current_pocket_id not in seen_pockets:
                        seen_pockets.add(current_pocket_id)

                        obs_payload = ObservedAccountEvidence(
                            observed_provider_account_key=current_pocket_id,
                            display_name_raw=current_pocket_name,
                            institution_id="jago",
                            parent_observed_key=header_account_id,
                            provider_state_raw=provider_state,
                            period_start=period_start,
                            period_end=period_end,
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
                                row_fingerprint=sha256(
                                    f"{natural_key}:ACCOUNT_OBSERVATION:{current_pocket_id}:{current_pocket_name}:{provider_state}:{period_start}:{period_end}".encode(
                                        "utf-8"
                                    )
                                ).hexdigest(),
                                evidence_quality=ConfidenceLevel.HIGH,
                                parse_confidence=ConfidenceLevel.HIGH,
                                provenance=SourceProvenanceContract(
                                    source_document_id=source.source_document_id,
                                    raw_locator=f"page:{page_idx}:pocket:{current_pocket_id}",
                                    page_number=page_idx,
                                    row_index=1,
                                    raw_text=f"ID Kantong {current_pocket_id}",
                                ),
                                payload=obs_payload,
                            )
                        )

                        if found_pocket_summary:
                            pkt_summary_payload = AccountPeriodSummaryEvidence(
                                observed_provider_account_key=current_pocket_id,
                                currency="IDR",
                                period_start=period_start,
                                period_end=period_end,
                                opening_balance=op_bal,
                                incoming_total=inc_tot,
                                outgoing_total=out_tot,
                                closing_balance=cl_bal,
                            )
                            events.append(
                                NormalizedEventEnvelope(
                                    source_document_id=source.source_document_id,
                                    source_registry_id=self.descriptor.source_registry_id,
                                    template_id=self.descriptor.template_id,
                                    parser_version=self.descriptor.parser_version,
                                    source_channel=self.descriptor.source_channel,
                                    event_role=EventRole.ACCOUNT_PERIOD_SUMMARY,
                                    source_event_id=None,
                                    row_fingerprint=sha256(
                                        f"{natural_key}:ACCOUNT_PERIOD_SUMMARY:{current_pocket_id}:{op_bal}:{inc_tot}:{out_tot}:{cl_bal}".encode(
                                            "utf-8"
                                        )
                                    ).hexdigest(),
                                    evidence_quality=ConfidenceLevel.HIGH,
                                    parse_confidence=ConfidenceLevel.HIGH,
                                    provenance=SourceProvenanceContract(
                                        source_document_id=source.source_document_id,
                                        raw_locator=f"page:{page_idx}:pocket_summary:{current_pocket_id}",
                                        page_number=page_idx,
                                        row_index=2,
                                        raw_text=f"Summary {current_pocket_id}",
                                    ),
                                    payload=pkt_summary_payload,
                                )
                            )

                    i += 1
                    continue

                # Check for Transaction Row start: Date Line + Time Line
                match_date = _DATE_RE.match(line)
                if (
                    match_date
                    and i + 1 < len(lines)
                    and _TIME_RE.match(lines[i + 1])
                ):
                    date_match = match_date
                    time_match = _TIME_RE.match(lines[i + 1])

                    tx_day = int(date_match.group(1))
                    tx_mon_str = date_match.group(2).upper()[:3]
                    tx_year = int(date_match.group(3))
                    tx_month = _TX_MONTHS.get(tx_mon_str, 1)

                    assert time_match is not None
                    tx_hour = int(time_match.group(1))
                    tx_minute = int(time_match.group(2))

                    occurred_at = f"{tx_year:04d}-{tx_month:02d}-{tx_day:02d}T{tx_hour:02d}:{tx_minute:02d}:00"
                    i += 2

                    # Collect group lines until next transaction / pocket / page footer
                    group_lines: list[str] = []
                    while i < len(lines):
                        cur_line = lines[i]
                        if (
                            _DATE_RE.match(cur_line)
                            and i + 1 < len(lines)
                            and _TIME_RE.match(lines[i + 1])
                        ):
                            break
                        if re.match(r"^ID\s+Kantong\s+", cur_line, re.I):
                            break
                        if (
                            "Mata Uang Dalam IDR" in cur_line
                            or "INFO PENTING" in cur_line
                        ):
                            break
                        if (
                            cur_line.startswith("Halaman ")
                            or "Laporan Keuangan" in cur_line
                            or "www.jago.com" in cur_line
                        ):
                            i += 1
                            continue
                        group_lines.append(cur_line)
                        i += 1

                    # Parse group lines for ID, Amount, Balance, Category, Notes, Source/Dest
                    id_idx: int | None = None
                    amt_idx: int | None = None

                    for g_idx, gl in enumerate(group_lines):
                        if _ID_RE.match(gl):
                            id_idx = g_idx
                        if _AMT_RE.match(gl):
                            amt_idx = g_idx

                    if id_idx is None or amt_idx is None or id_idx >= amt_idx:
                        diagnostics.append(
                            SafeDiagnostic(
                                code="JAGO_ROW_AMOUNT_INVALID",
                                severity=DiagnosticSeverity.ERROR,
                                message="Transaction group structure is malformed.",
                                locator_token=sha256(
                                    f"{page_idx}:{row_counter}".encode("utf-8")
                                ).hexdigest()[:16],
                            )
                        )
                        continue

                    # Extract parsed fields
                    before_id = group_lines[:id_idx]
                    id_match = _ID_RE.match(group_lines[id_idx])
                    assert id_match is not None
                    provider_tx_id = id_match.group(1).strip()
                    note_lines = group_lines[id_idx + 1 : amt_idx]

                    amt_match = _AMT_RE.match(group_lines[amt_idx])
                    assert amt_match is not None
                    direction_sign = amt_match.group(1)
                    raw_amt_str = amt_match.group(2)
                    tx_amount = _parse_decimal(raw_amt_str)

                    tx_balance = (
                        _parse_decimal(group_lines[amt_idx + 1])
                        if amt_idx + 1 < len(group_lines)
                        else None
                    )

                    category = before_id[-1] if before_id else "Transaksi"
                    source_dest = (
                        " ".join(before_id[:-1])
                        if len(before_id) > 1
                        else (before_id[0] if before_id else "")
                    )
                    note_text = (
                        " ".join(note_lines).strip() if note_lines else None
                    )

                    is_inflow = direction_sign == "+"
                    direction_enum = (
                        EventDirection.INFLOW
                        if is_inflow
                        else EventDirection.OUTFLOW
                    )

                    src_key_raw = None if is_inflow else current_pocket_id
                    src_disp_raw = source_dest if is_inflow else None
                    dst_key_raw = current_pocket_id if is_inflow else None
                    dst_disp_raw = None if is_inflow else source_dest

                    cm_payload = CashMovementEvidence(
                        amount=abs(tx_amount),
                        currency="IDR",
                        direction=direction_enum,
                        status=SourceEventStatus.POSTED,
                        source_account_key_raw=src_key_raw,
                        source_account_display_raw=src_disp_raw,
                        destination_account_key_raw=dst_key_raw,
                        destination_account_display_raw=dst_disp_raw,
                        occurred_at=occurred_at,
                        posted_at=occurred_at,
                        settlement_date=occurred_at[:10],
                        direction_raw=direction_sign,
                        status_raw="POSTED",
                        provider_transaction_id_raw=provider_tx_id,
                        provider_category_raw=category,
                        description_raw=note_text,
                        balance_after=tx_balance,
                    )

                    # Check for transaction ID reuse / conflict
                    tx_key = (provider_tx_id, current_pocket_id or "")
                    if tx_key in seen_transactions:
                        diagnostics.append(
                            SafeDiagnostic(
                                code="JAGO_DUPLICATE_ROW_CONFLICT",
                                severity=DiagnosticSeverity.WARNING,
                                message="Duplicate transaction row detected for same pocket.",
                                review_required=True,
                            )
                        )
                    seen_transactions[tx_key] = cm_payload

                    row_counter += 1
                    row_fp = sha256(
                        f"{period_str}:{current_pocket_id}:{occurred_at}:{direction_sign}:{abs(tx_amount)}:{provider_tx_id}:{category}:{source_dest}:{note_text}:{tx_balance}".encode(
                            "utf-8"
                        )
                    ).hexdigest()

                    events.append(
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
                                raw_locator=f"page:{page_idx}:row:{row_counter}",
                                page_number=page_idx,
                                row_index=row_counter,
                                raw_text=f"ID# {provider_tx_id}",
                            ),
                            payload=cm_payload,
                        )
                    )
                    continue

                i += 1

        review_reasons: list[str] = []
        parse_status = AdapterParseStatus.COMPLETED

        if any(d.severity is DiagnosticSeverity.ERROR for d in diagnostics):
            parse_status = AdapterParseStatus.FAILED
            events = []
        elif any(
            d.severity is DiagnosticSeverity.WARNING or d.review_required
            for d in diagnostics
        ):
            parse_status = AdapterParseStatus.REVIEW_REQUIRED
            review_reasons.append("Diagnostics require manual review.")

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=parse_status,
            period_status=PeriodStatus.CLOSED,
            events=tuple(events),
            diagnostics=tuple(diagnostics),
            review_reasons=tuple(review_reasons),
            period_start=period_start,
            period_end=period_end,
            natural_document_key_candidate=natural_key,
        )

    @staticmethod
    def _extract_header_account_id(p1_lines: list[str]) -> str | None:
        start_idx: int | None = None
        end_idx: int | None = None

        for idx, line in enumerate(p1_lines):
            if "www.jago.com" in line.lower():
                start_idx = idx + 1
            if "RINGKASAN SALDO" in line.upper() or "RINGKASAN" in line.upper():
                end_idx = idx
                break

        if start_idx is None or end_idx is None or start_idx >= end_idx:
            return None

        candidates: list[str] = []
        for line in p1_lines[start_idx:end_idx]:
            match = _CUSTOMER_HEADER_RE.match(line)
            if match:
                name_part = match.group(1).strip()
                acct_part = match.group(2).strip()
                if not re.search(r"\b(?:RT|RW|JL)\b", name_part, re.I):
                    candidates.append(acct_part)

        if len(candidates) == 1:
            return candidates[0]
        return None

    def extract_account_observations(
        self,
        source_or_result: Any,
    ) -> list[AccountDiscoveryObservation]:
        if isinstance(source_or_result, AdapterInput):
            result = self.parse(source_or_result)
        else:
            result = source_or_result

        if result is None or result.parse_status == AdapterParseStatus.FAILED or not result.events:
            return []

        effective_date = result.period_start or "2026-01-01"
        obs_events = [
            e for e in result.events if e.event_role == EventRole.ACCOUNT_OBSERVATION
        ]
        if not obs_events:
            return []

        root_key = obs_events[0].payload.parent_observed_key
        observations: list[AccountDiscoveryObservation] = []

        if root_key:
            observations.append(
                AccountDiscoveryObservation(
                    institution_id="jago",
                    source_registry_id=self.descriptor.source_registry_id,
                    raw_account_key=root_key,
                    display_name_safe="Kantong Utama",
                    account_type=AccountType.TRANSACTIONAL,
                    effective_date=effective_date,
                    ownership_state=OwnershipState.OWNED,
                    ownership_confidence=ConfidenceLevel.HIGH,
                    parent_raw_account_key=None,
                    parent_account_type=None,
                )
            )

        seen_pocket_keys: set[str] = set()
        for e in obs_events:
            pkt_payload = e.payload
            pkt_key = pkt_payload.observed_provider_account_key
            if pkt_key and pkt_key not in seen_pocket_keys and pkt_key != root_key:
                seen_pocket_keys.add(pkt_key)
                observations.append(
                    AccountDiscoveryObservation(
                        institution_id="jago",
                        source_registry_id=self.descriptor.source_registry_id,
                        raw_account_key=pkt_key,
                        display_name_safe="Jago Kantong",
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
    return JagoMonthlyStatementAdapter().extract_account_observations(source_or_result)


__all__ = [
    "JAGO_ADAPTER_ID",
    "JAGO_PARSER_VERSION",
    "JAGO_SOURCE_REGISTRY_ID",
    "JAGO_TEMPLATE_FINGERPRINT",
    "JAGO_TEMPLATE_ID",
    "JagoMonthlyStatementAdapter",
    "extract_account_observations",
]
