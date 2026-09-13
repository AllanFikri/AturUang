"""Shopee Orders receipt PDF ingestion adapter for AturUang.

Phase 3.8.3 Full PDF Parser and Commerce Evidence Foundation.
Extracts authoritative order identity, detailed commerce line items,
complete payment breakdown components, and exact decimal reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
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
    AmountComponentEvidence,
    CommerceLineItemEvidence,
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

TABLE_HEADER_KEYWORDS: frozenset[str] = frozenset(
    {"no.", "produk", "variasi", "harga produk", "kuantitas", "subtotal", "rincian pesanan"}
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


@dataclass(frozen=True)
class ShopeeOrderAdapterResult(AdapterResult):
    order_reconciliation_status: str = "MATCHED"
    reconciliation_difference: Decimal = Decimal("0.00")
    order_identity: ShopeeOrderDocumentIdentity | None = None


def parse_id_amount(raw: str) -> Decimal:
    """Parse Indonesian formatted currency string (e.g. 'Rp187.345', '-Rp10.000', or 'Rp 187.345,00') into Decimal."""
    s = raw.strip()
    is_neg = s.startswith("-")
    cleaned = re.sub(r"[^\d,\.]", "", s).strip()
    if not cleaned:
        raise ValueError("Cannot parse nominal from invalid amount value")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "." in cleaned and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    elif "," in cleaned and len(cleaned.split(",")[-1]) == 2:
        cleaned = cleaned.replace(",", ".")
    val = Decimal(cleaned)
    return -val if is_neg else val


def parse_order_date(raw: str) -> str | None:
    """Parse Indonesian order date (DD/MM/YYYY or YYYY-MM-DD) into ISO YYYY-MM-DD string with strict calendar validation."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        d_str, mth_str, y_str = m.groups()
        try:
            d_val, mth_val, y_val = int(d_str), int(mth_str), int(y_str)
            return datetime.date(y_val, mth_val, d_val).strftime("%Y-%m-%d")
        except ValueError:
            return None
    m2 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m2:
        y_str, mth_str, d_str = m2.groups()
        try:
            y_val, mth_val, d_val = int(y_str), int(mth_str), int(d_str)
            return datetime.date(y_val, mth_val, d_val).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def build_natural_document_key(order_number: str | None) -> str | None:
    """Deterministic natural document key for Shopee Orders:
    shopee_orders:receipt:<order_identity_token>
    where order_identity_token = SHA256("shopee_orders|receipt|" + normalized_order_id)
    and normalized_order_id = trim + uppercase.
    """
    if not order_number or not order_number.strip():
        return None
    normalized_order_id = order_number.strip().upper()
    identity_token = sha256(
        f"{SHOPEE_ORDERS_SOURCE_REGISTRY_ID}|receipt|{normalized_order_id}".encode("utf-8")
    ).hexdigest()
    return f"{SHOPEE_ORDERS_SOURCE_REGISTRY_ID}:receipt:{identity_token}"


def build_stable_line_key(
    order_token: str,
    product_name_raw: str,
    variation_raw: str | None,
    quantity: Decimal,
    line_subtotal: Decimal,
    occurrence_index: int,
) -> str:
    """Deterministic stable line key for CommerceLineItemEvidence.
    Uses privacy-safe natural order token, normalized product, normalized variation,
    quantity, line subtotal, and occurrence index.
    """
    norm_p = " ".join(product_name_raw.strip().lower().split())
    norm_v = " ".join(variation_raw.strip().lower().split()) if variation_raw else ""
    raw_sig = f"{order_token}|{norm_p}|{norm_v}|{quantity}|{line_subtotal}|{occurrence_index}"
    token = sha256(raw_sig.encode("utf-8")).hexdigest()
    return f"shopee_orders:line:{token}"


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
        if order_date_raw:
            code = "INVALID_ORDER_DATE"
            msg = "Authoritative order date is invalid or impossible calendar date."
            if not review_reason:
                review_reason = "INVALID_ORDER_DATE"
        else:
            code = "MISSING_ORDER_DATE"
            msg = "Authoritative order date missing from summary region."
            if not review_reason:
                review_reason = "MISSING_ORDER_DATE"
        diagnostics.append(
            SafeDiagnostic(
                code=code,
                severity=DiagnosticSeverity.WARNING,
                message=msg,
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


def extract_order_line_items(
    lines: Sequence[str],
    order_token: str,
    page_elements: Sequence[Sequence[tuple[float, float, str]]] | None = None,
) -> tuple[tuple[CommerceLineItemEvidence, ...], list[SafeDiagnostic], bool, str | None]:
    """Extract physical line items from Rincian Pesanan section."""
    diagnostics: list[SafeDiagnostic] = []
    review_required = False
    review_reason = None

    idx_r = None
    for i, l in enumerate(lines):
        if l.lower() == "rincian pesanan":
            idx_r = i
            break

    if idx_r is None:
        return (), diagnostics, False, None

    # Slice table lines up to section endings
    table_lines: list[str] = []
    for l in lines[idx_r + 1:]:
        ll = l.lower()
        if any(term in ll for term in ["nota pesanan", "pt shopee international", "catatan pembeli", "faktur pesanan"]):
            break
        table_lines.append(l)

    clean_lines = [l for l in table_lines if l.lower() not in TABLE_HEADER_KEYWORDS]

    # Coordinate lookup if available
    all_pel: list[tuple[float, float, str]] = []
    if page_elements:
        for pel in page_elements:
            all_pel.extend(pel)

    items: list[CommerceLineItemEvidence] = []
    occurrence_tracker: dict[tuple[str, str, str, str], int] = {}

    i = 0
    while i < len(clean_lines):
        if i < len(clean_lines) and clean_lines[i].isdigit() and int(clean_lines[i]) == len(items) + 1:
            ord_val = int(clean_lines[i])
            i += 1
            k = i
            found = False
            while k + 2 < len(clean_lines):
                if (
                    "rp" in clean_lines[k].lower()
                    and clean_lines[k + 1].isdigit()
                    and "rp" in clean_lines[k + 2].lower()
                ):
                    try:
                        p_amt = parse_id_amount(clean_lines[k])
                        q_amt = Decimal(clean_lines[k + 1])
                        s_amt = parse_id_amount(clean_lines[k + 2])

                        # Arithmetic verification
                        if p_amt * q_amt != s_amt:
                            review_required = True
                            if not review_reason:
                                review_reason = "UNPARSED_ORDER_ITEM"
                            diagnostics.append(
                                SafeDiagnostic(
                                    code="UNPARSED_ORDER_ITEM",
                                    severity=DiagnosticSeverity.WARNING,
                                    message="Item subtotal does not match unit price multiplied by quantity.",
                                )
                            )

                        mid = clean_lines[i:k]
                        prod_parts: list[str] = []
                        var_parts: list[str] = []

                        # Separate using coordinates when present
                        if all_pel:
                            for ml in mid:
                                if "\t" in ml:
                                    p_s, v_s = ml.split("\t", 1)
                                    prod_parts.append(p_s.strip())
                                    var_parts.append(v_s.strip())
                                elif ml.lower().startswith("variasi:"):
                                    var_parts.append(ml[len("variasi:"):].strip())
                                else:
                                    xs = [el[1] for el in all_pel if el[2] == ml]
                                    if xs:
                                        x_val = xs[0]
                                        if 600 <= x_val < 750:
                                            var_parts.append(ml)
                                        else:
                                            prod_parts.append(ml)
                                    else:
                                        prod_parts.append(ml)
                        else:
                            # Fallback when no coordinates (e.g. synthetic fixtures)
                            for ml in mid:
                                if "\t" in ml:
                                    p_s, v_s = ml.split("\t", 1)
                                    prod_parts.append(p_s.strip())
                                    var_parts.append(v_s.strip())
                                elif ml.lower().startswith("variasi:"):
                                    var_parts.append(ml[len("variasi:"):].strip())
                                else:
                                    prod_parts.append(ml)

                        p_name = " ".join(prod_parts).strip()
                        v_name = " ".join(var_parts).strip() if var_parts else None

                        if not p_name:
                            p_name = f"Item {ord_val}"

                        # Stable line key calculation
                        norm_p = " ".join(p_name.strip().lower().split())
                        norm_v = " ".join(v_name.strip().lower().split()) if v_name else ""
                        sig_tuple = (norm_p, norm_v, str(q_amt), str(s_amt))
                        occurrence_tracker[sig_tuple] = occurrence_tracker.get(sig_tuple, 0) + 1
                        occ_idx = occurrence_tracker[sig_tuple]

                        line_key = build_stable_line_key(
                            order_token=order_token,
                            product_name_raw=p_name,
                            variation_raw=v_name,
                            quantity=q_amt,
                            line_subtotal=s_amt,
                            occurrence_index=occ_idx,
                        )

                        items.append(
                            CommerceLineItemEvidence(
                                line_key=line_key,
                                product_name_raw=p_name,
                                quantity=q_amt,
                                line_subtotal=s_amt,
                                currency="IDR",
                                variation_raw=v_name,
                            )
                        )
                        i = k + 3
                        found = True
                        break
                    except Exception:
                        review_required = True
                        if not review_reason:
                            review_reason = "UNPARSED_ORDER_ITEM"
                        diagnostics.append(
                            SafeDiagnostic(
                                code="UNPARSED_ORDER_ITEM",
                                severity=DiagnosticSeverity.WARNING,
                                message="Failed to parse item price or quantity.",
                            )
                        )
                k += 1
            if not found:
                review_required = True
                if not review_reason:
                    review_reason = "UNPARSED_ORDER_ITEM"
                diagnostics.append(
                    SafeDiagnostic(
                        code="UNPARSED_ORDER_ITEM",
                        severity=DiagnosticSeverity.WARNING,
                        message="Numbered order item row could not be parsed safely.",
                    )
                )
                i += 1
        else:
            i += 1

    if not items:
        review_required = True
        if not review_reason:
            review_reason = "UNPARSED_ORDER_ITEM"
        diagnostics.append(
            SafeDiagnostic(
                code="UNPARSED_ORDER_ITEM",
                severity=DiagnosticSeverity.WARNING,
                message="Rincian pesanan section found but no valid order item could be parsed.",
            )
        )

    return tuple(items), diagnostics, review_required, review_reason


def extract_order_amount_components(
    lines: Sequence[str],
) -> tuple[tuple[AmountComponentEvidence, ...], list[SafeDiagnostic], bool, str | None]:
    """Extract payment breakdown components and validate unknown items."""
    diagnostics: list[SafeDiagnostic] = []
    review_required = False
    review_reason = None

    idx_start = None
    idx_end = None
    for i, l in enumerate(lines):
        ll = l.lower()
        if idx_start is None and "total pembayaran" in ll:
            idx_start = i
        if idx_start is not None and i > idx_start:
            if any(k in ll for k in ["biaya-biaya yang ditagihkan", "nama pembeli", "rincian pesanan"]):
                idx_end = i
                break

    pay_lines = lines[idx_start:idx_end] if idx_start is not None and idx_end is not None else []
    if not pay_lines and idx_start is not None:
        pay_lines = list(lines[idx_start:])

    raw_pairs: list[tuple[str, str]] = []
    j = 0
    while j < len(pay_lines):
        line = pay_lines[j].strip()
        m = re.match(r"^([^:\n]+)[:]\s*(.*)$", line, re.IGNORECASE)
        if m and any(k in m.group(1).lower() for k in ["total", "subtotal", "biaya", "voucher", "diskon", "koin", "promosi"]):
            raw_pairs.append((m.group(1).strip(), m.group(2).strip()))
            j += 1
        elif m and ("rp" in m.group(2).lower() or any(c.isdigit() for c in m.group(2))):
            raw_pairs.append((m.group(1).strip(), m.group(2).strip()))
            j += 1
        elif j + 1 < len(pay_lines) and any(k in line.lower() for k in ["total", "subtotal", "biaya", "voucher", "diskon", "koin", "promosi"]):
            raw_pairs.append((line, pay_lines[j + 1].strip()))
            j += 2
        elif j + 1 < len(pay_lines) and ("rp" in pay_lines[j + 1].lower() or "-rp" in pay_lines[j + 1].lower()):
            raw_pairs.append((line, pay_lines[j + 1].strip()))
            j += 2
        else:
            j += 1

    components: list[AmountComponentEvidence] = []
    for lbl, raw_amt_str in raw_pairs:
        try:
            raw_amt = parse_id_amount(raw_amt_str)
        except Exception:
            review_required = True
            if not review_reason:
                review_reason = "MALFORMED_AMOUNT_COMPONENT"
            diagnostics.append(
                SafeDiagnostic(
                    code="MALFORMED_AMOUNT_COMPONENT",
                    severity=DiagnosticSeverity.WARNING,
                    message="Priced order amount component could not be parsed.",
                )
            )
            continue

        ll = lbl.lower()

        # Classification
        if any(k in ll for k in ["subtotal pesanan", "subtotal produk"]):
            amt = abs(raw_amt)
        elif any(k in ll for k in ["subtotal pengiriman"]):
            amt = abs(raw_amt)
        elif any(k in ll for k in ["biaya layanan", "biaya penanganan", "total proteksi produk"]):
            amt = abs(raw_amt)
        elif any(k in ll for k in ["voucher toko", "voucher penjual", "diskon voucher toko", "diskon penjual"]):
            amt = -abs(raw_amt)
        elif any(k in ll for k in ["voucher shopee", "diskon voucher shopee", "diskon shopee", "promosi metode pembayaran"]):
            amt = -abs(raw_amt)
        elif any(k in ll for k in ["diskon pengiriman", "potongan ongkir", "voucher diskon pengiriman"]):
            amt = -abs(raw_amt)
        elif "koin shopee" in ll:
            amt = -abs(raw_amt)
        elif "total pembayaran" in ll:
            amt = abs(raw_amt)
        else:
            amt = raw_amt
            review_required = True
            if not review_reason:
                review_reason = "UNKNOWN_ORDER_AMOUNT_COMPONENT"
            diagnostics.append(
                SafeDiagnostic(
                    code="UNKNOWN_ORDER_AMOUNT_COMPONENT",
                    severity=DiagnosticSeverity.WARNING,
                    message="Unknown order amount component encountered in payment breakdown.",
                )
            )

        components.append(
            AmountComponentEvidence(
                label_raw=lbl,
                amount=amt,
                currency="IDR",
                direction_raw="CREDIT" if amt < 0 else "DEBIT",
            )
        )

    return tuple(components), diagnostics, review_required, review_reason


def reconcile_order(
    total_payment: Decimal | None,
    components: Sequence[AmountComponentEvidence],
) -> tuple[str, Decimal, list[SafeDiagnostic], bool, str | None]:
    """Perform exact Decimal order reconciliation equation.
    item subtotal + shipping fee + service fees - discounts - coins == total payment
    """
    diagnostics: list[SafeDiagnostic] = []

    if total_payment is None:
        return "INSUFFICIENT_SOURCE_DETAIL", Decimal("0.00"), diagnostics, True, "MISSING_TOTAL_PAYMENT"

    # Find subtotal
    subtotal = None
    shipping = Decimal("0.00")
    service_fees = Decimal("0.00")
    discounts = Decimal("0.00")
    coins = Decimal("0.00")

    has_subtotal = False

    for c in components:
        ll = c.label_raw.lower()
        if any(k in ll for k in ["subtotal pesanan", "subtotal produk"]):
            subtotal = c.amount
            has_subtotal = True
        elif any(k in ll for k in ["subtotal pengiriman"]):
            shipping += c.amount
        elif any(k in ll for k in ["biaya layanan", "biaya penanganan", "total proteksi produk"]):
            service_fees += c.amount
        elif any(k in ll for k in ["voucher toko", "voucher penjual", "diskon voucher toko", "voucher shopee", "diskon voucher shopee", "diskon shopee", "promosi metode pembayaran", "diskon pengiriman", "potongan ongkir"]):
            discounts += abs(c.amount)
        elif "koin shopee" in ll:
            coins += abs(c.amount)

    if not has_subtotal or subtotal is None:
        return "INSUFFICIENT_SOURCE_DETAIL", Decimal("0.00"), diagnostics, False, None

    calculated_total = subtotal + shipping + service_fees - discounts - coins
    diff = calculated_total - total_payment

    if diff == Decimal("0.00"):
        return "MATCHED", Decimal("0.00"), diagnostics, False, None
    else:
        diagnostics.append(
            SafeDiagnostic(
                code="ORDER_TOTAL_MISMATCH",
                severity=DiagnosticSeverity.WARNING,
                message="Order total does not match sum of breakdown components.",
            )
        )
        return "MISMATCH", diff, diagnostics, True, "ORDER_TOTAL_MISMATCH"


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

    def parse(self, source: AdapterInput) -> ShopeeOrderAdapterResult:
        validate_adapter_input(self.descriptor, source)

        diagnostics: list[SafeDiagnostic] = []
        page_elements: list[list[tuple[float, float, str]]] = []

        # 1. PDF Parsing & Text Layer Extraction
        pages_text: list[str] = []
        full_text = ""

        if source.binary_payload is not None:
            try:
                reader = self._pdf_reader_factory(BytesIO(source.binary_payload))
                if reader.is_encrypted:
                    return ShopeeOrderAdapterResult(
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
                    return ShopeeOrderAdapterResult(
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

                for page in reader.pages:
                    pel: list[tuple[float, float, str]] = []

                    def visitor(text: str, cm: Sequence[float], tm: Sequence[float], font_dict: object, font_size: float) -> None:
                        t = text.strip()
                        if t:
                            pel.append((tm[5], tm[4], t))

                    page_txt = page.extract_text(visitor_text=visitor) or ""
                    pages_text.append(page_txt)
                    page_elements.append(pel)

                full_text = "\n".join(pages_text)
            except Exception:
                return ShopeeOrderAdapterResult(
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
            return ShopeeOrderAdapterResult(
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
            return ShopeeOrderAdapterResult(
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
            return ShopeeOrderAdapterResult(
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

        # 4. Extract Amount Components
        amt_components, amt_diags, amt_rev, amt_reason = extract_order_amount_components(lines)
        diagnostics.extend(amt_diags)

        # 5. Reconcile Order
        recon_status, recon_diff, recon_diags, recon_rev, recon_reason = reconcile_order(
            total_payment=identity.total_payment,
            components=amt_components,
        )
        diagnostics.extend(recon_diags)

        # 6. Extract Line Items
        normalized_order_id = identity.order_number.strip().upper() if identity.order_number else ""
        order_token = sha256(
            f"{SHOPEE_ORDERS_SOURCE_REGISTRY_ID}|receipt|{normalized_order_id}".encode("utf-8")
        ).hexdigest()

        line_items, item_diags, item_rev, item_reason = extract_order_line_items(
            lines=lines,
            order_token=order_token,
            page_elements=page_elements if page_elements else None,
        )
        diagnostics.extend(item_diags)

        events: list[NormalizedEventEnvelope] = []
        if (
            identity.order_number
            and identity.order_date
            and identity.total_payment is not None
        ):
            evidence = CommerceOrderEvidence(
                order_native_id_raw=identity.order_number,
                order_date=identity.order_date,
                order_total=identity.total_payment,
                currency="IDR",
                seller_raw=identity.seller_name,
                payment_method_raw=identity.payment_method,
                shipping_service_raw=identity.shipping_service,
                recipient_raw=identity.recipient_name,
                line_items=line_items,
                amount_components=amt_components,
                canonical_match_required=True,
                cash_movement_emitted=False,
            )

            # Row fingerprint is stable across duplicate files (no source_document_id)
            row_fp = sha256(
                f"{SHOPEE_ORDERS_SOURCE_REGISTRY_ID}:order:{order_token}".encode("utf-8")
            ).hexdigest()

            events.append(
                NormalizedEventEnvelope(
                    source_document_id=source.source_document_id,
                    source_registry_id=self.descriptor.source_registry_id,
                    template_id=self.descriptor.template_id,
                    parser_version=self.descriptor.parser_version,
                    source_channel=self.descriptor.source_channel,
                    event_role=EventRole.COMMERCE_ORDER,
                    source_event_id=order_token,
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
        has_review = (
            identity.review_required
            or amt_rev
            or recon_rev
            or item_rev
            or not events
            or recon_status != "MATCHED"
        )
        final_status = AdapterParseStatus.REVIEW_REQUIRED if has_review else AdapterParseStatus.COMPLETED

        return ShopeeOrderAdapterResult(
            descriptor=self.descriptor,
            source_document_id=source.source_document_id,
            parse_status=final_status,
            period_status=PeriodStatus.CLOSED if identity.order_date else PeriodStatus.UNKNOWN,
            period_start=identity.order_date,
            period_end=identity.order_date,
            natural_document_key_candidate=identity.natural_document_key_candidate,
            events=tuple(events),
            diagnostics=tuple(diagnostics),
            order_reconciliation_status=recon_status,
            reconciliation_difference=recon_diff,
            order_identity=identity,
        )


__all__ = [
    "REQUIRED_TEMPLATE_MARKERS",
    "SHOPEE_ORDERS_ADAPTER_ID",
    "SHOPEE_ORDERS_PARSER_VERSION",
    "SHOPEE_ORDERS_SOURCE_REGISTRY_ID",
    "SHOPEE_ORDERS_TEMPLATE_FINGERPRINT",
    "SHOPEE_ORDERS_TEMPLATE_ID",
    "TABLE_HEADER_KEYWORDS",
    "ShopeeOrderAdapterResult",
    "ShopeeOrderDocumentIdentity",
    "ShopeeOrderEvidenceRole",
    "ShopeeOrdersReceiptAdapter",
    "build_natural_document_key",
    "build_stable_line_key",
    "extract_order_amount_components",
    "extract_order_identity",
    "extract_order_line_items",
    "extract_order_regions",
    "parse_id_amount",
    "parse_order_date",
    "reconcile_order",
]
