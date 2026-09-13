"""Shopee Orders receipt PDF ingestion adapter for AturUang.

Phase 3.8.2 Adapter Skeleton & Commerce Evidence Foundation.
Extracts authoritative order identity, order summary fields, and emits
preliminary CommerceOrderEvidence without cash movement.
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
    CommerceOrderEvidence,
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

SHOPEE_ORDERS_SOURCE_REGISTRY_ID = "shopee_orders"
SHOPEE_ORDERS_TEMPLATE_ID = "shopee_order_receipt_v1"
SHOPEE_ORDERS_ADAPTER_ID = "shopee-orders-pdf-v1"
SHOPEE_ORDERS_PARSER_VERSION = "parser-v1"
SHOPEE_ORDERS_TEMPLATE_FINGERPRINT = (
    "236728f9f2577d58324a986960137c799b1dfaf2258934a0ac622e9a435e21d4"
)

REQUIRED_TEMPLATE_MARKERS: tuple[str, ...] = (
    "nama penjual",
    "no. pesanan",
    "tanggal transaksi",
    "metode pembayaran",
    "rincian pesanan",
    "total pembayaran",
)


class ShopeeOrderEvidenceRole(str, Enum):
    COMMERCE_ORDER = "COMMERCE_ORDER"
    ORDER_SUMMARY = "ORDER_SUMMARY"


@dataclass(frozen=True)
class ShopeeOrderDocumentIdentity:
    source_type: str = SHOPEE_ORDERS_SOURCE_REGISTRY_ID
    order_number: str | None = None
    order_date: str | None = None
    seller_name: str | None = None
    payment_method: str | None = None
    shipping_service: str | None = None
    recipient_name: str | None = None
    total_payment: Decimal | None = None
    currency: str = "IDR"
    document_sha256: str = ""
    natural_document_key_candidate: str | None = None
    source_file_name: str | None = None
    review_required: bool = False
    review_reason: str | None = None


def parse_id_amount(raw: str) -> Decimal:
    """Parse Indonesian formatted currency string (e.g. 'Rp187.345' or 'Rp 187.345,00') into Decimal."""
    cleaned = re.sub(r"[^\d,\.]", "", raw).strip()
    if not cleaned:
        raise ValueError(f"Cannot parse nominal from empty or invalid raw value: {raw!r}")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "." in cleaned and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    elif "," in cleaned and len(cleaned.split(",")[-1]) == 2:
        cleaned = cleaned.replace(",", ".")
    return Decimal(cleaned)


def parse_order_date(raw: str) -> str | None:
    """Parse Indonesian order date (DD/MM/YYYY or YYYY-MM-DD) into ISO YYYY-MM-DD string."""
    raw = raw.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        d, mth, y = m.groups()
        return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
    m2 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m2:
        y, mth, d = m2.groups()
        return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
    return None


def build_natural_document_key(order_number: str | None) -> str | None:
    """Deterministic natural document key for Shopee Orders: provider:receipt:<order_number>."""
    if not order_number or not order_number.strip():
        return None
    return f"{SHOPEE_ORDERS_SOURCE_REGISTRY_ID}:receipt:{order_number.strip()}"


def extract_order_regions(lines: Sequence[str]) -> tuple[dict[str, list[str]], list[SafeDiagnostic]]:
    """Extract bounded textual regions from Shopee order receipt lines."""
    diagnostics: list[SafeDiagnostic] = []
    
    header_lines: list[str] = []
    summary_lines: list[str] = []
    payment_lines: list[str] = []
    buyer_lines: list[str] = []
    item_lines: list[str] = []
    
    idx_seller = None
    idx_pesanan = None
    idx_payment = None
    idx_buyer = None
    idx_items = None
    
    for i, line in enumerate(lines):
        ll = line.lower()
        if idx_seller is None and "nama penjual" in ll:
            idx_seller = i
        elif idx_pesanan is None and ("no. pesanan" in ll or "no.pesanan" in ll):
            idx_pesanan = i
        elif idx_payment is None and ("total pembayaran" in ll or "subtotal pesanan" in ll):
            idx_payment = i
        elif idx_buyer is None and "nama pembeli" in ll:
            idx_buyer = i
        elif idx_items is None and "rincian pesanan" in ll:
            idx_items = i

    n = len(lines)
    s_end = idx_pesanan if idx_pesanan is not None else n
    header_lines = list(lines[0:s_end])
    
    if idx_pesanan is not None:
        p_end = idx_payment if idx_payment is not None else (idx_buyer if idx_buyer is not None else n)
        summary_lines = list(lines[idx_pesanan:p_end])
        
    if idx_payment is not None:
        b_end = idx_buyer if idx_buyer is not None else (idx_items if idx_items is not None else n)
        payment_lines = list(lines[idx_payment:b_end])
        
    if idx_buyer is not None:
        i_end = idx_items if idx_items is not None else n
        buyer_lines = list(lines[idx_buyer:i_end])
        
    if idx_items is not None:
        item_lines = list(lines[idx_items:])
        
    regions = {
        "header": header_lines,
        "summary": summary_lines,
        "payment": payment_lines,
        "buyer": buyer_lines,
        "items": item_lines,
    }
    return regions, diagnostics


def extract_order_identity(
    lines: Sequence[str],
    document_sha256: str = "",
    source_file_name: str | None = None,
) -> tuple[ShopeeOrderDocumentIdentity, list[SafeDiagnostic]]:
    """Extract authoritative order identity strictly from bounded regions."""
    diagnostics: list[SafeDiagnostic] = []
    regions, region_diags = extract_order_regions(lines)
    diagnostics.extend(region_diags)
    
    summary_lines = regions["summary"]
    header_lines = regions["header"]
    payment_lines = regions["payment"]
    buyer_lines = regions["buyer"]
    
    # 1. Seller Extraction
    seller_name: str | None = None
    for i, l in enumerate(header_lines):
        if "nama penjual" in l.lower():
            m_s = re.search(r"nama\s*penjual\s*:\s*(.+)$", l, re.IGNORECASE)
            val = m_s.group(1).strip() if m_s else ""
            if val and not val.lower().startswith("no."):
                seller_name = val
            elif i + 1 < len(header_lines):
                nxt = header_lines[i + 1].strip()
                if nxt and not nxt.lower().startswith("no."):
                    seller_name = nxt
            break

    # 2. Order Summary Extraction (No. Pesanan, Tanggal Transaksi, Metode Pembayaran, Jasa Kirim)
    order_id: str | None = None
    order_date_raw: str | None = None
    payment_method: str | None = None
    shipping_service: str | None = None
    
    candidate_order_ids: list[str] = []
    candidate_dates: list[str] = []
    
    # Inline check
    for l in summary_lines:
        m_oid = re.search(r"no\.?\s*pesanan\s*[:]\s*([a-zA-Z0-9]+)", l, re.IGNORECASE)
        if m_oid:
            candidate_order_ids.append(m_oid.group(1).strip())
        m_dt = re.search(r"tanggal\s*transaksi\s*[:]\s*([0-9/\-]+)", l, re.IGNORECASE)
        if m_dt:
            candidate_dates.append(m_dt.group(1).strip())
        m_pm = re.search(r"metode\s*pembayaran\s*[:]\s*([^\n\r]+)", l, re.IGNORECASE)
        if m_pm:
            payment_method = m_pm.group(1).strip()
        m_ss = re.search(r"jasa\s*kirim\s*[:]\s*([^\n\r]+)", l, re.IGNORECASE)
        if m_ss:
            shipping_service = m_ss.group(1).strip()
            
    # Columnar table layout check
    if not candidate_order_ids or not candidate_dates:
        headers = ["no. pesanan", "tanggal transaksi", "metode pembayaran", "jasa kirim"]
        header_indices = []
        for h in headers:
            for idx, l in enumerate(summary_lines):
                if l.lower() == h:
                    header_indices.append(idx)
                    break
        if len(header_indices) >= 2:
            val_start = max(header_indices) + 1
            if val_start < len(summary_lines):
                val_oid = summary_lines[val_start].strip()
                if re.match(r"^[a-zA-Z0-9]{10,30}$", val_oid):
                    candidate_order_ids.append(val_oid)
            if val_start + 1 < len(summary_lines):
                val_dt = summary_lines[val_start + 1].strip()
                if re.match(r"^[0-9/\-]{8,10}$", val_dt):
                    candidate_dates.append(val_dt)
            if val_start + 2 < len(summary_lines) and not payment_method:
                payment_method = summary_lines[val_start + 2].strip()
            if val_start + 3 < len(summary_lines) and not shipping_service:
                shipping_service = summary_lines[val_start + 3].strip()

    # Conflicting checks in order summary
    unique_candidate_oids = list(dict.fromkeys(candidate_order_ids))
    unique_candidate_dates = list(dict.fromkeys(candidate_dates))
    
    review_required = False
    review_reason = None
    
    if len(unique_candidate_oids) > 1:
        review_required = True
        review_reason = "CONFLICTING_ORDER_IDS"
        diagnostics.append(
            SafeDiagnostic(
                code="CONFLICTING_ORDER_IDS",
                severity=DiagnosticSeverity.WARNING,
                message="Multiple conflicting order IDs found in summary region.",
            )
        )
        order_id = None
    elif len(unique_candidate_oids) == 1:
        order_id = unique_candidate_oids[0]
    else:
        order_id = None
        
    if len(unique_candidate_dates) > 1:
        review_required = True
        review_reason = "CONFLICTING_ORDER_DATES"
        diagnostics.append(
            SafeDiagnostic(
                code="CONFLICTING_ORDER_DATES",
                severity=DiagnosticSeverity.WARNING,
                message="Multiple conflicting dates found in summary region.",
            )
        )
        order_date_raw = None
    elif len(unique_candidate_dates) == 1:
        order_date_raw = unique_candidate_dates[0]
    else:
        order_date_raw = None

    parsed_order_date = parse_order_date(order_date_raw) if order_date_raw else None

    # 3. Total Payment Extraction
    total_payment: Decimal | None = None
    search_scope = payment_lines if payment_lines else lines
    for i, l in enumerate(search_scope):
        if l.lower() == "total pembayaran":
            if i + 1 < len(search_scope):
                nxt = search_scope[i + 1].strip()
                if "rp" in nxt.lower():
                    try:
                        total_payment = parse_id_amount(nxt)
                    except Exception:
                        pass
            break
        elif "total pembayaran" in l.lower():
            m_tot = re.search(r"total\s*pembayaran\s*[:]?\s*(?:Rp\.?|IDR)?\s*([0-9\.,]+)", l, re.IGNORECASE)
            if m_tot:
                try:
                    total_payment = parse_id_amount(m_tot.group(1))
                except Exception:
                    pass
            break

    # 4. Recipient Extraction
    recipient_name: str | None = None
    for i, l in enumerate(buyer_lines):
        if "nama pembeli" in l.lower():
            m_b = re.search(r"nama\s*pembeli\s*:\s*(.+)$", l, re.IGNORECASE)
            val = m_b.group(1).strip() if m_b else ""
            if val and not val.lower().startswith("alamat"):
                recipient_name = val
            elif i + 1 < len(buyer_lines):
                nxt = buyer_lines[i + 1].strip()
                if nxt and not nxt.lower().startswith("alamat"):
                    recipient_name = nxt
            break

    # Identity Validations & Review Requirements
    if not order_id:
        review_required = True
        if not review_reason:
            review_reason = "MISSING_ORDER_IDENTITY"
        diagnostics.append(
            SafeDiagnostic(
                code="MISSING_ORDER_IDENTITY",
                severity=DiagnosticSeverity.WARNING,
                message="Authoritative order ID missing from summary region.",
            )
        )
    if not parsed_order_date:
        review_required = True
        if not review_reason:
            review_reason = "MISSING_ORDER_DATE"
        diagnostics.append(
            SafeDiagnostic(
                code="MISSING_ORDER_DATE",
                severity=DiagnosticSeverity.WARNING,
                message="Authoritative order date missing from summary region.",
            )
        )
    if total_payment is None:
        review_required = True
        if not review_reason:
            review_reason = "MISSING_TOTAL_PAYMENT"
        diagnostics.append(
            SafeDiagnostic(
                code="MISSING_TOTAL_PAYMENT",
                severity=DiagnosticSeverity.WARNING,
                message="Total payment amount missing from payment region.",
            )
        )

    nat_key = build_natural_document_key(order_id)

    identity = ShopeeOrderDocumentIdentity(
        source_type=SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
        order_number=order_id,
        order_date=parsed_order_date,
        seller_name=seller_name,
        payment_method=payment_method,
        shipping_service=shipping_service,
        recipient_name=recipient_name,
        total_payment=total_payment,
        currency="IDR",
        document_sha256=document_sha256,
        natural_document_key_candidate=nat_key,
        source_file_name=source_file_name,
        review_required=review_required,
        review_reason=review_reason,
    )
    return identity, diagnostics


class ShopeeOrdersReceiptAdapter(UniversalSourceAdapter):
    """Universal Source Adapter for Shopee Orders receipt PDFs."""

    def __init__(
        self,
        *,
        pdf_reader_factory: Callable[[BytesIO], PdfReader] | None = None,
    ) -> None:
        self._pdf_reader_factory = pdf_reader_factory or PdfReader

    @property
    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=SHOPEE_ORDERS_ADAPTER_ID,
            source_registry_id=SHOPEE_ORDERS_SOURCE_REGISTRY_ID,
            template_id=SHOPEE_ORDERS_TEMPLATE_ID,
            parser_version=SHOPEE_ORDERS_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
        )

    def parse(self, source: AdapterInput) -> AdapterResult:
        validate_adapter_input(self.descriptor, source)

        diagnostics: list[SafeDiagnostic] = []

        # 1. PDF Parsing & Text Layer Extraction
        pages_text: list[str] = []
        full_text = ""

        if source.binary_payload is not None:
            try:
                reader = self._pdf_reader_factory(BytesIO(source.binary_payload))
                if reader.is_encrypted:
                    return AdapterResult(
                        descriptor=self.descriptor,
                        source_document_id=source.source_document_id,
                        parse_status=AdapterParseStatus.FAILED,
                        period_status=PeriodStatus.UNKNOWN,
                        period_start=None,
                        period_end=None,
                        natural_document_key_candidate=None,
                        events=(),
                        diagnostics=(
                            SafeDiagnostic(
                                code="SHOPEE_PDF_ENCRYPTED",
                                severity=DiagnosticSeverity.ERROR,
                                message="Encrypted PDF cannot be read without password.",
                            ),
                        ),
                    )
                if not reader.pages:
                    return AdapterResult(
                        descriptor=self.descriptor,
                        source_document_id=source.source_document_id,
                        parse_status=AdapterParseStatus.FAILED,
                        period_status=PeriodStatus.UNKNOWN,
                        period_start=None,
                        period_end=None,
                        natural_document_key_candidate=None,
                        events=(),
                        diagnostics=(
                            SafeDiagnostic(
                                code="SHOPEE_EMPTY_PAYLOAD",
                                severity=DiagnosticSeverity.ERROR,
                                message="PDF document contains no pages.",
                            ),
                        ),
                    )
                pages_text = [p.extract_text() or "" for p in reader.pages]
                full_text = "\n".join(pages_text)
            except Exception:
                return AdapterResult(
                    descriptor=self.descriptor,
                    source_document_id=source.source_document_id,
                    parse_status=AdapterParseStatus.FAILED,
                    period_status=PeriodStatus.UNKNOWN,
                    period_start=None,
                    period_end=None,
                    natural_document_key_candidate=None,
                    events=(),
                    diagnostics=(
                        SafeDiagnostic(
                            code="SHOPEE_PDF_MALFORMED",
                            severity=DiagnosticSeverity.ERROR,
                            message="Failed to read PDF payload.",
                        ),
                    ),
                )
        elif source.text_payload is not None:
            pages_text = [source.text_payload]
            full_text = source.text_payload
        else:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                period_start=None,
                period_end=None,
                natural_document_key_candidate=None,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEE_UNSUPPORTED_PAYLOAD",
                        severity=DiagnosticSeverity.ERROR,
                        message="Unsupported payload kind for Shopee Orders PDF adapter.",
                    ),
                ),
            )

        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        if not lines:
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED,
                period_status=PeriodStatus.UNKNOWN,
                period_start=None,
                period_end=None,
                natural_document_key_candidate=None,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code="SHOPEE_PDF_NO_TEXT_LAYER",
                        severity=DiagnosticSeverity.ERROR,
                        message="PDF has no extractable text layer.",
                    ),
                ),
            )

        # 2. Marker Verification
        lower_full_text = " ".join(lines).lower()
        matched_markers = [m for m in REQUIRED_TEMPLATE_MARKERS if m in lower_full_text]
        if len(matched_markers) < len(REQUIRED_TEMPLATE_MARKERS):
            code = "SHOPEE_UNKNOWN_TEMPLATE" if len(matched_markers) <= 1 else "SHOPEE_TEMPLATE_DRIFT"
            return AdapterResult(
                descriptor=self.descriptor,
                source_document_id=source.source_document_id,
                parse_status=AdapterParseStatus.FAILED if code == "SHOPEE_UNKNOWN_TEMPLATE" else AdapterParseStatus.REVIEW_REQUIRED,
                period_status=PeriodStatus.UNKNOWN,
                period_start=None,
                period_end=None,
                natural_document_key_candidate=None,
                events=(),
                diagnostics=(
                    SafeDiagnostic(
                        code=code,
                        severity=DiagnosticSeverity.ERROR,
                        message=f"Missing required template markers ({len(matched_markers)}/{len(REQUIRED_TEMPLATE_MARKERS)}).",
                    ),
                ),
            )

        # 3. Extract Identity & Authoritative Fields
        identity, ident_diags = extract_order_identity(
            lines=lines,
            document_sha256=source.content_sha256,
            source_file_name=None,
        )
        diagnostics.extend(ident_diags)

        # Temporary limitation indicator for skeleton stage:
        diagnostics.append(
            SafeDiagnostic(
                code="ORDER_DETAILS_NOT_PARSED",
                severity=DiagnosticSeverity.INFO,
                message="Preliminary order skeleton parsed; detailed line items deferred to P3.8.3.",
            )
        )

        events: list[NormalizedEventEnvelope] = []
        if (
            identity.order_number
            and identity.order_date
            and identity.total_payment is not None
        ):
            # Emit preliminary CommerceOrderEvidence
            evidence = CommerceOrderEvidence(
                order_native_id_raw=identity.order_number,
                order_date=identity.order_date,
                order_total=identity.total_payment,
                currency="IDR",
                seller_raw=identity.seller_name,
                payment_method_raw=identity.payment_method,
                shipping_service_raw=identity.shipping_service,
                recipient_raw=identity.recipient_name,
                line_items=(),
                amount_components=(),
            )

            row_fp = sha256(
                f"{source.source_document_id}:order:{identity.order_number}".encode("utf-8")
            ).hexdigest()

            events.append(
                NormalizedEventEnvelope(
                    source_document_id=source.source_document_id,
                    source_registry_id=self.descriptor.source_registry_id,
                    template_id=self.descriptor.template_id,
                    parser_version=self.descriptor.parser_version,
                    source_channel=self.descriptor.source_channel,
                    event_role=EventRole.COMMERCE_ORDER,
                    source_event_id=identity.order_number,
                    row_fingerprint=row_fp,
                    evidence_quality=ConfidenceLevel.HIGH,
                    parse_confidence=ConfidenceLevel.HIGH,
                    provenance=SourceProvenanceContract(
                        source_document_id=source.source_document_id,
                        raw_locator="order_summary:region",
                        row_index=0,
                        raw_text=None,
                    ),
                    payload=evidence,
                )
            )

        # Determine final parse status
        if identity.review_required or not events:
            final_status = AdapterParseStatus.REVIEW_REQUIRED
        else:
            final_status = AdapterParseStatus.REVIEW_REQUIRED

        return AdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=final_status,
            period_status=PeriodStatus.CLOSED if identity.order_date else PeriodStatus.UNKNOWN,
            period_start=identity.order_date,
            period_end=identity.order_date,
            natural_document_key_candidate=identity.natural_document_key_candidate,
            events=tuple(events),
            diagnostics=tuple(diagnostics),
        )


__all__ = [
    "SHOPEE_ORDERS_ADAPTER_ID",
    "SHOPEE_ORDERS_PARSER_VERSION",
    "SHOPEE_ORDERS_SOURCE_REGISTRY_ID",
    "SHOPEE_ORDERS_TEMPLATE_FINGERPRINT",
    "SHOPEE_ORDERS_TEMPLATE_ID",
    "REQUIRED_TEMPLATE_MARKERS",
    "ShopeeOrderDocumentIdentity",
    "ShopeeOrderEvidenceRole",
    "ShopeeOrdersReceiptAdapter",
    "build_natural_document_key",
    "extract_order_identity",
    "extract_order_regions",
    "parse_id_amount",
    "parse_order_date",
]
