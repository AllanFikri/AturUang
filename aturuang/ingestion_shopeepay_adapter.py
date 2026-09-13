"""ShopeePay transaction history image ingestion adapter for AturUang.

Parses raster ShopeePay transaction history screenshots into trusted CashMovementEvidence
using local deterministic OCR and layout analysis.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
from typing import Callable, Sequence

from PIL import Image

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
)
from .ingestion_image_ocr import (
    ImageOcrError,
    ImageOcrLine,
    ImageOcrResult,
    extract_image_text,
)

SHOPEEPAY_SOURCE_REGISTRY_ID = "shopeepay_mutation"
SHOPEEPAY_TEMPLATE_ID = "shopeepay_transaction_history_image_v1"
SHOPEEPAY_ADAPTER_ID = "shopeepay-transaction-history-image-v1"
SHOPEEPAY_PARSER_VERSION = "parser-v1"
SHOPEEPAY_MIN_OCR_WIDTH = 720

_MONTH_NAME_TO_INT: dict[str, int] = {
    "januari": 1,
    "january": 1,
    "jan": 1,
    "februari": 2,
    "february": 2,
    "feb": 2,
    "maret": 3,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mei": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "jun": 6,
    "juli": 7,
    "july": 7,
    "jul": 7,
    "agustus": 8,
    "august": 8,
    "agu": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "desember": 12,
    "december": 12,
    "des": 12,
    "dec": 12,
}

_AMOUNT_RE = re.compile(
    r"([+-])\s*(?:Rp|RP|rp|RPI|rpi|RP1|rp1)\s*([^\n\r]+)",
    re.IGNORECASE,
)
_UNSIGNED_AMOUNT_LINE_RE = re.compile(
    r"^\s*(?:Rp|RP|rp|RPI|rpi|RP1|rp1)\s*([0-9IOol|][^\n\r]*?)\s*$",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b",
)
_DATE_FILTER_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*[-–—]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
)
_MONTH_HEADER_RE = re.compile(
    r"\b([A-Za-z]+)\s+(\d{4})\b",
)


@dataclass(frozen=True)
class _ParsedCard:
    y: int
    title: str
    description: str
    date_str: str
    direction: EventDirection | None
    direction_raw: str | None
    amount: Decimal
    is_failed: bool
    is_ambiguous: bool = False
    ambiguity_reason: str | None = None


def _parse_idr_amount(raw_str: str) -> Decimal | None:
    cleaned = re.sub(r"^[+-]\s*(?:Rp|RP|rp|RPI|rpi|RP1|rp1)?", "", str(raw_str), flags=re.I).strip()
    cleaned = cleaned.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1").replace("|", "1").replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) != 2:
            return None
        int_part, frac_part = parts
        int_part = int_part.replace(".", "")
        if not int_part.isdigit() or not frac_part.isdigit():
            return None
        if len(frac_part) > 2:
            return None  # Over-precision fails closed
        if len(frac_part) == 1:
            frac_part = frac_part + "0"
        elif len(frac_part) == 0:
            frac_part = "00"
        val_str = f"{int_part}.{frac_part}"
    else:
        int_part = cleaned.replace(".", "")
        if not int_part.isdigit():
            return None
        val_str = f"{int_part}.00"
    try:
        val = Decimal(val_str)
        if val < Decimal("0.00") or val.as_tuple().exponent != -2:
            return None
        return val
    except InvalidOperation:
        return None


def _clean_text(val: str) -> str:
    return " ".join(str(val).split()).strip()


class ShopeePayTransactionHistoryImageAdapter(UniversalSourceAdapter):
    """Universal Ingestion Phase 3 adapter for ShopeePay screenshot captures."""

    descriptor: AdapterDescriptor = AdapterDescriptor(
        adapter_id=SHOPEEPAY_ADAPTER_ID,
        source_registry_id=SHOPEEPAY_SOURCE_REGISTRY_ID,
        template_id=SHOPEEPAY_TEMPLATE_ID,
        parser_version=SHOPEEPAY_PARSER_VERSION,
        source_channel=SourceChannel.IMAGE,
    )

    def __init__(
        self,
        *,
        ocr_extractor: Callable[[bytes], ImageOcrResult] | None = None,
    ) -> None:
        self._ocr_extractor = ocr_extractor or extract_image_text

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        payload = source.binary_payload
        if not payload:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_EMPTY_PAYLOAD",
                        severity=DiagnosticSeverity.ERROR,
                        message="Image payload is empty.",
                    ),
                ),
            )

        # 1. Probe image dimensions
        try:
            img = Image.open(BytesIO(payload))
            img_width, img_height = img.size
        except Exception:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_MEDIA_CORRUPT",
                        severity=DiagnosticSeverity.ERROR,
                        message="Failed to decode image header or dimensions.",
                    ),
                ),
            )

        if img_width <= 0 or img_height <= 0:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_MEDIA_INVALID_DIMENSIONS",
                        severity=DiagnosticSeverity.ERROR,
                        message="Image dimensions must be positive.",
                    ),
                ),
            )

        # 2. Provider Resolution Gate: width >= 720 px
        if img_width < SHOPEEPAY_MIN_OCR_WIDTH:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.REVIEW_REQUIRED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_IMAGE_RESOLUTION_INSUFFICIENT",
                        severity=DiagnosticSeverity.WARNING,
                        message="Image width is below minimum threshold for trusted OCR.",
                    ),
                ),
                natural_document_key_candidate=None,
            )

        # 3. Local OCR Extraction
        try:
            ocr_result = self._ocr_extractor(payload)
        except ImageOcrError:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_OCR_EXECUTION_FAILED",
                        severity=DiagnosticSeverity.ERROR,
                        message="Local OCR execution failed or timed out.",
                    ),
                ),
            )
        except Exception:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_OCR_EXECUTION_FAILED",
                        severity=DiagnosticSeverity.ERROR,
                        message="Unexpected error during local OCR extraction.",
                    ),
                ),
            )

        lines = ocr_result.lines
        if not lines:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.REVIEW_REQUIRED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_TEMPLATE_CONTENT_UNCONFIRMED",
                        severity=DiagnosticSeverity.WARNING,
                        message="No readable text lines extracted from image.",
                    ),
                ),
            )

        # 4. Content Template Gate
        sorted_lines = sorted(lines, key=lambda l: (l.y, l.x))
        full_text = " ".join(line.text for line in sorted_lines).casefold()
        has_header = (
            "transaction history" in full_text
            or "transaksi terakhir" in full_text
            or "riwayat transaksi" in full_text
        )
        has_period_structure = bool(
            _DATE_FILTER_RE.search(full_text)
            or _DATE_FILTER_RE.search(ocr_result.text)
            or _MONTH_HEADER_RE.search(full_text)
            or _MONTH_HEADER_RE.search(ocr_result.text)
        )
        has_tab_or_action = (
            "top up" in full_text
            or "isi saldo" in full_text
            or "payment" in full_text
            or "pembayaran" in full_text
            or "transfer" in full_text
            or "shopeepay" in full_text
        )

        if not (has_header and has_period_structure and has_tab_or_action):
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.REVIEW_REQUIRED,
                period_status=PeriodStatus.UNKNOWN,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEEPAY_TEMPLATE_CONTENT_UNCONFIRMED",
                        severity=DiagnosticSeverity.WARNING,
                        message="OCR text does not match expected ShopeePay layout markers.",
                    ),
                ),
            )

        # 5. Period Parsing
        period_start_str: str | None = None
        period_end_str: str | None = None
        period_status = PeriodStatus.UNKNOWN
        natural_key: str | None = None

        header_filter_lines = [
            l.text for l in sorted_lines
            if l.y < 600 and l.x < 500
        ]
        header_filter_text = " ".join(header_filter_lines)

        date_filter_match = _DATE_FILTER_RE.search(header_filter_text)
        if date_filter_match:
            d1, m1_str, y1, d2, m2_str, y2 = date_filter_match.groups()
            m1 = _MONTH_NAME_TO_INT.get(m1_str.lower())
            m2 = _MONTH_NAME_TO_INT.get(m2_str.lower())
            if m1 and m2:
                try:
                    y1_int, y2_int = int(y1), int(y2)
                    d1_int, d2_int = int(d1), int(d2)
                except ValueError:
                    y1_int = y2_int = d1_int = d2_int = 0

                # Strictly validate monthly period authority:
                # - start year == end year
                # - start month == end month
                # - start day == 1
                # - end day valid for that calendar month (1 <= d2 <= last_day)
                if (
                    y1_int > 0
                    and y1_int == y2_int
                    and m1 == m2
                    and d1_int == 1
                ):
                    last_day_of_month = calendar.monthrange(y1_int, m1)[1]
                    if 1 <= d2_int <= last_day_of_month:
                        period_start_str = f"{y1_int:04d}-{m1:02d}-{d1_int:02d}"
                        period_end_str = f"{y2_int:04d}-{m2:02d}-{d2_int:02d}"
                        natural_key = f"shopeepay_mutation:unidentified_wallet:{y1_int:04d}-{m1:02d}"

                        if d2_int == last_day_of_month:
                            period_status = PeriodStatus.CLOSED
                        else:
                            period_status = PeriodStatus.OPEN

        # 6. Card Parsing
        parsed_cards = self._parse_transaction_cards(sorted_lines)

        emitted_events: list[NormalizedEventEnvelope] = []
        diagnostics: list[SafeDiagnostic] = []
        has_ambiguous = False
        failed_card_count = 0

        for ordinal, card in enumerate(parsed_cards, start=1):
            if card.is_failed:
                failed_card_count += 1
                continue

            if card.is_ambiguous:
                has_ambiguous = True
                continue

            evidence = CashMovementEvidence(
                amount=card.amount,
                currency="IDR",
                direction=card.direction,
                status=SourceEventStatus.UNKNOWN,
                occurred_at=card.date_str,
                description_raw=card.description or card.title,
                direction_raw=card.direction_raw,
                provider_transaction_id_raw=None,
                balance_after=None,
                payment_method_raw=None,
                provider_category_raw=None,
                event_hint=None,
            )

            row_fp = sha256(
                f"{source.source_document_id}:card:{ordinal}:{card.date_str}:{card.direction.value}:{card.amount}".encode("utf-8")
            ).hexdigest()

            emitted_events.append(
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
                    parse_confidence=ConfidenceLevel.MEDIUM,
                    provenance=SourceProvenanceContract(
                        source_document_id=source.source_document_id,
                        raw_locator=f"image:y:{card.y}",
                        row_index=ordinal,
                        raw_text=card.description or card.title,
                    ),
                    payload=evidence,
                )
            )

        if has_ambiguous:
            diagnostics.append(
                SafeDiagnostic(
                    code="SHOPEEPAY_CARD_AMBIGUOUS",
                    severity=DiagnosticSeverity.WARNING,
                    message="One or more transaction cards were ambiguous and omitted.",
                )
            )

        if failed_card_count > 0:
            diagnostics.append(
                SafeDiagnostic(
                    code="SHOPEEPAY_FAILED_TRANSACTIONS_EXCLUDED",
                    severity=DiagnosticSeverity.INFO,
                    message=f"Excluded {failed_card_count} failed transaction cards from cash movement.",
                )
            )

        final_status = (
            AdapterParseStatus.REVIEW_REQUIRED
            if has_ambiguous
            else AdapterParseStatus.COMPLETED
        )

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=final_status,
            period_status=period_status,
            period_start=period_start_str,
            period_end=period_end_str,
            events=tuple(emitted_events),
            diagnostics=tuple(diagnostics),
            natural_document_key_candidate=natural_key,
        )

    def _parse_transaction_cards(
        self,
        lines: Sequence[ImageOcrLine],
    ) -> list[_ParsedCard]:
        """Groups OCR lines into transaction cards ordered by absolute Y position."""
        sorted_lines = sorted(lines, key=lambda l: (l.y, l.x))
        cards: list[_ParsedCard] = []

        raw_amount_indices: list[int] = []
        unsigned_amount_indices: list[int] = []
        for idx, line in enumerate(sorted_lines):
            clean_text = line.text.casefold()
            if "%" in line.text or _DATE_FILTER_RE.search(line.text) or _DATE_RE.search(line.text):
                continue
            if clean_text in {
                "transaction history", "transaksi terakhir", "riwayat transaksi",
                "all", "transfer", "top up", "payment", "semua", "metode pembayaran", "payment method",
            }:
                continue
            if _AMOUNT_RE.search(line.text):
                raw_amount_indices.append(idx)
            elif _UNSIGNED_AMOUNT_LINE_RE.match(line.text):
                unsigned_amount_indices.append(idx)

        amount_indices: list[int] = []
        for a_idx in raw_amount_indices:
            if not amount_indices:
                amount_indices.append(a_idx)
                continue
            prev_idx = amount_indices[-1]
            if abs(sorted_lines[a_idx].y - sorted_lines[prev_idx].y) < 40:
                if len(sorted_lines[a_idx].text) > len(sorted_lines[prev_idx].text):
                    amount_indices[-1] = a_idx
            else:
                amount_indices.append(a_idx)

        used_indices: set[int] = set()

        for loop_idx, a_idx in enumerate(amount_indices):
            if a_idx in used_indices:
                continue

            a_line = sorted_lines[a_idx]
            match = _AMOUNT_RE.search(a_line.text)
            if not match:
                continue

            sign_str, amount_str = match.groups()
            direction = EventDirection.INFLOW if sign_str == "+" else EventDirection.OUTFLOW
            direction_raw = sign_str + "Rp" + amount_str
            amount = _parse_idr_amount(amount_str)

            if amount is None:
                cards.append(
                    _ParsedCard(
                        y=a_line.y,
                        title="",
                        description="",
                        date_str="",
                        direction=None,
                        direction_raw=None,
                        amount=Decimal("0.00"),
                        is_failed=False,
                        is_ambiguous=True,
                        ambiguity_reason="INVALID_AMOUNT",
                    )
                )
                continue

            prev_y = sorted_lines[amount_indices[loop_idx - 1]].y if loop_idx > 0 else 0
            next_y = sorted_lines[amount_indices[loop_idx + 1]].y if loop_idx + 1 < len(amount_indices) else 999999
            min_y = max(a_line.y - 40, (a_line.y + prev_y) / 2) if loop_idx > 0 else a_line.y - 40
            max_y = min(a_line.y + 180, (a_line.y + next_y) / 2) if loop_idx + 1 < len(amount_indices) else a_line.y + 180

            window_lines = [
                l for l in sorted_lines
                if l.y >= min_y and l.y <= max_y
            ]

            window_text = " ".join(l.text for l in window_lines)
            is_failed = "failed" in window_text.casefold() or "gagal" in window_text.casefold()

            date_match = _DATE_RE.search(window_text)
            date_str = ""
            if date_match:
                d_day, d_month_str, d_year = date_match.groups()
                m_val = _MONTH_NAME_TO_INT.get(d_month_str.lower())
                if m_val:
                    date_str = f"{int(d_year):04d}-{m_val:02d}-{int(d_day):02d}"

            title_parts: list[str] = []
            desc_parts: list[str] = []

            for l in window_lines:
                clean_l = _clean_text(l.text)
                if not clean_l:
                    continue
                if (
                    _AMOUNT_RE.search(clean_l)
                    or _DATE_RE.search(clean_l)
                    or _MONTH_HEADER_RE.search(clean_l)
                    or _DATE_FILTER_RE.search(clean_l)
                ):
                    continue
                if clean_l.casefold() in {
                    "failed", "gagal", "reward claimed", "reward expired",
                    "transaction history", "transaksi terakhir", "riwayat transaksi",
                    "all", "top up", "payment", "transfer", "semua", "isi saldo", "pembayaran",
                    "payment method", "metode pembayaran",
                }:
                    continue
                if clean_l.startswith("<") or "%" in clean_l:
                    continue

                if clean_l.casefold() in {"payment", "top up", "cashback bonus", "transfer sent", "send to bank", "refund"}:
                    title_parts.append(clean_l)
                else:
                    desc_parts.append(clean_l)

            title = " ".join(title_parts).strip() or "Transaction"
            description = " ".join(desc_parts).strip() or title

            if not date_str:
                cards.append(
                    _ParsedCard(
                        y=a_line.y,
                        title=title,
                        description=description,
                        date_str="",
                        direction=None,
                        direction_raw=None,
                        amount=amount,
                        is_failed=is_failed,
                        is_ambiguous=True,
                        ambiguity_reason="MISSING_DATE",
                    )
                )
            else:
                cards.append(
                    _ParsedCard(
                        y=a_line.y,
                        title=title,
                        description=description,
                        date_str=date_str,
                        direction=direction,
                        direction_raw=direction_raw,
                        amount=amount,
                        is_failed=is_failed,
                        is_ambiguous=False,
                    )
                )

        for u_idx in unsigned_amount_indices:
            u_line = sorted_lines[u_idx]
            if any(abs(u_line.y - sorted_lines[a_idx].y) < 40 for a_idx in amount_indices):
                continue
            match = _UNSIGNED_AMOUNT_LINE_RE.match(u_line.text)
            raw_amt_str = match.group(1) if match else u_line.text
            parsed_amt = _parse_idr_amount(raw_amt_str)
            cards.append(
                _ParsedCard(
                    y=u_line.y,
                    title="",
                    description="",
                    date_str="",
                    direction=None,
                    direction_raw=None,
                    amount=parsed_amt if parsed_amt is not None else Decimal("0.00"),
                    is_failed=False,
                    is_ambiguous=True,
                    ambiguity_reason="UNSIGNED_AMOUNT" if parsed_amt is not None else "INVALID_UNSIGNED_AMOUNT",
                )
            )

        cards.sort(key=lambda c: c.y)
        return cards
