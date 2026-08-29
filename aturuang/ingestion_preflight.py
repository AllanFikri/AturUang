from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import struct
from typing import Sequence

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .ingestion_contracts import (
    PeriodStatus,
    TemplateMatchStatus,
)


class PreflightQuality(str, Enum):
    READY = "READY"
    EMPTY = "EMPTY"
    CORRUPT = "CORRUPT"
    MEDIA_MISMATCH = "MEDIA_MISMATCH"
    ENCRYPTED_LOCKED = "ENCRYPTED_LOCKED"
    NO_TEXT_LAYER = "NO_TEXT_LAYER"
    LOW_RESOLUTION = "LOW_RESOLUTION"
    UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA"


class PdfEncryptionState(str, Enum):
    NOT_PDF = "NOT_PDF"
    UNENCRYPTED = "UNENCRYPTED"
    ENCRYPTED_READABLE = "ENCRYPTED_READABLE"
    ENCRYPTED_LOCKED = "ENCRYPTED_LOCKED"


class DetectionMethod(str, Enum):
    NONE = "NONE"
    TEXT_MARKERS = "TEXT_MARKERS"


@dataclass(frozen=True)
class TemplateSignature:
    source_registry_id: str
    template_id: str
    media_type: str
    required_markers: tuple[str, ...]
    optional_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        source_registry_id = self.source_registry_id.strip()
        template_id = self.template_id.strip()
        media_type = self.media_type.strip().upper()

        if not source_registry_id:
            raise ValueError("source_registry_id must not be empty")
        if not template_id:
            raise ValueError("template_id must not be empty")
        if media_type not in {"PDF", "IMAGE"}:
            raise ValueError("media_type must be PDF or IMAGE")
        if not self.required_markers:
            raise ValueError("required_markers must not be empty")

        required = tuple(
            _normalize_marker(marker)
            for marker in self.required_markers
        )
        optional = tuple(
            _normalize_marker(marker)
            for marker in self.optional_markers
        )

        if len(set(required)) != len(required):
            raise ValueError("required_markers must be unique")

        object.__setattr__(self, "source_registry_id", source_registry_id)
        object.__setattr__(self, "template_id", template_id)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "required_markers", required)
        object.__setattr__(self, "optional_markers", optional)


@dataclass(frozen=True)
class TemplateDetection:
    status: TemplateMatchStatus
    source_registry_id: str | None
    template_id: str | None
    template_fingerprint: str | None
    method: DetectionMethod
    required_marker_matches: int
    required_marker_total: int
    reason_code: str


@dataclass(frozen=True)
class DocumentPreflight:
    content_sha256: str
    size_bytes: int
    extension: str
    media_type: str
    mime_type: str
    quality_status: PreflightQuality
    page_count: int | None
    image_dimensions: tuple[int, int] | None
    text_layer_available: bool
    pdf_encryption_state: PdfEncryptionState
    period_status: PeriodStatus
    template_detection: TemplateDetection

    @property
    def adapter_ready(self) -> bool:
        return (
            self.quality_status is PreflightQuality.READY
            and self.template_detection.status is TemplateMatchStatus.KNOWN
        )


@dataclass(frozen=True)
class PreflightPolicy:
    max_pdf_text_pages: int = 3
    min_image_width: int = 300
    min_image_height: int = 300

    def __post_init__(self) -> None:
        if self.max_pdf_text_pages <= 0:
            raise ValueError("max_pdf_text_pages must be > 0")
        if self.min_image_width <= 0:
            raise ValueError("min_image_width must be > 0")
        if self.min_image_height <= 0:
            raise ValueError("min_image_height must be > 0")


def _normalize_marker(value: str) -> str:
    normalized = " ".join(str(value).split()).casefold()
    if not normalized:
        raise ValueError("template marker must not be empty")
    return normalized


def _normalized_text(value: str) -> str:
    return " ".join(str(value).split()).casefold()


SOURCE_TEMPLATE_SIGNATURES_V1: tuple[TemplateSignature, ...] = (
    TemplateSignature(
        source_registry_id="bca_statement",
        template_id="bca_monthly_statement_v1",
        media_type="PDF",
        required_markers=(
            "REKENING TAHAPAN XPRESI",
            "NO. REKENING",
            "PERIODE",
            "TANGGAL KETERANGAN CBG MUTASI SALDO",
        ),
    ),
    TemplateSignature(
        source_registry_id="jago_statement",
        template_id="jago_monthly_statement_v1",
        media_type="PDF",
        required_markers=(
            "Laporan Keuangan Bulanan",
            "RINGKASAN SALDO DALAM RUPIAH",
            "KANTONG PERSONAL",
            "Tanggal & Waktu",
        ),
    ),
    TemplateSignature(
        source_registry_id="seabank_statement",
        template_id="seabank_estatement_v1",
        media_type="PDF",
        required_markers=(
            "REKENING KORAN",
            "RINGKASAN REKENING",
            "TABUNGAN - RINCIAN TRANSAKSI",
        ),
    ),
    TemplateSignature(
        source_registry_id="blu_mutation",
        template_id="blu_account_mutation_v1",
        media_type="PDF",
        required_markers=(
            "bluAccount",
            "Periode / Period",
            "Keterangan / Remarks",
            "Nominal",
            "Sisa Saldo",
        ),
    ),
    TemplateSignature(
        source_registry_id="blu_portfolio",
        template_id="blu_portfolio_v1",
        media_type="PDF",
        required_markers=(
            "Laporan Portofolio",
            "Ringkasan / Summary",
            "bluSaving/bluGether",
        ),
    ),
    TemplateSignature(
        source_registry_id="gopay_statement",
        template_id="gopay_estatement_v1",
        media_type="PDF",
        required_markers=(
            "E-statement",
            "Periode transaksi",
            "Tanggal Transaksi",
            "ID transaksi",
            "Metode pembayaran",
            "Jumlah",
        ),
    ),
    TemplateSignature(
        source_registry_id="stockbit_soa",
        template_id="stockbit_soa_v1",
        media_type="PDF",
        required_markers=(
            "Statement of Account",
            "Tr. Date",
            "Due Date",
            "Reference",
            "Db Amount",
            "Cr Amount",
            "Ending Balance",
        ),
    ),
    TemplateSignature(
        source_registry_id="shopee_orders",
        template_id="shopee_order_receipt_v1",
        media_type="PDF",
        required_markers=(
            "Nama Penjual",
            "No. Pesanan",
            "Tanggal Transaksi",
            "Metode Pembayaran",
            "Rincian Pesanan",
            "Total Pembayaran",
        ),
    ),
    TemplateSignature(
        source_registry_id="shopeepay_mutation",
        template_id="shopeepay_transaction_history_image_v1",
        media_type="IMAGE",
        required_markers=(
            "Transaction History",
        ),
    ),
)


def template_fingerprint(signature: TemplateSignature) -> str:
    payload = "\x1f".join(
        (
            "source-template-signature-v1",
            signature.source_registry_id,
            signature.template_id,
            signature.media_type,
            *signature.required_markers,
            "--optional--",
            *signature.optional_markers,
        )
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def detect_template(
    text: str,
    *,
    media_type: str,
    signatures: Sequence[TemplateSignature] = SOURCE_TEMPLATE_SIGNATURES_V1,
    source_hint: str | None = None,
) -> TemplateDetection:
    media_type = str(media_type).strip().upper()
    normalized_text = _normalized_text(text)

    candidates: list[tuple[TemplateSignature, int]] = []
    media_signatures = [
        signature
        for signature in signatures
        if signature.media_type == media_type
    ]

    for signature in media_signatures:
        matched = sum(
            1
            for marker in signature.required_markers
            if marker in normalized_text
        )
        if matched == len(signature.required_markers):
            candidates.append((signature, matched))

    if len(candidates) == 1:
        signature, matched = candidates[0]

        if source_hint and source_hint != signature.source_registry_id:
            return TemplateDetection(
                status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
                source_registry_id=None,
                template_id=None,
                template_fingerprint=None,
                method=DetectionMethod.TEXT_MARKERS,
                required_marker_matches=matched,
                required_marker_total=len(signature.required_markers),
                reason_code="SOURCE_HINT_CONFLICT",
            )

        return TemplateDetection(
            status=TemplateMatchStatus.KNOWN,
            source_registry_id=signature.source_registry_id,
            template_id=signature.template_id,
            template_fingerprint=template_fingerprint(signature),
            method=DetectionMethod.TEXT_MARKERS,
            required_marker_matches=matched,
            required_marker_total=len(signature.required_markers),
            reason_code="EXACT_REQUIRED_MARKERS",
        )

    if len(candidates) > 1:
        return TemplateDetection(
            status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
            source_registry_id=None,
            template_id=None,
            template_fingerprint=None,
            method=DetectionMethod.TEXT_MARKERS,
            required_marker_matches=0,
            required_marker_total=0,
            reason_code="AMBIGUOUS_SIGNATURE",
        )

    if source_hint:
        hinted = [
            signature
            for signature in media_signatures
            if signature.source_registry_id == source_hint
        ]
        if hinted:
            matched, signature = max(
                (
                    (
                        sum(
                            1
                            for marker in signature.required_markers
                            if marker in normalized_text
                        ),
                        signature,
                    )
                    for signature in hinted
                ),
                key=lambda item: item[0],
            )
            return TemplateDetection(
                status=TemplateMatchStatus.TEMPLATE_DRIFT,
                source_registry_id=source_hint,
                template_id=None,
                template_fingerprint=None,
                method=DetectionMethod.TEXT_MARKERS,
                required_marker_matches=matched,
                required_marker_total=len(signature.required_markers),
                reason_code="SOURCE_HINT_NO_EXACT_TEMPLATE",
            )

    return TemplateDetection(
        status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
        source_registry_id=None,
        template_id=None,
        template_fingerprint=None,
        method=DetectionMethod.TEXT_MARKERS,
        required_marker_matches=0,
        required_marker_total=0,
        reason_code="NO_EXACT_TEMPLATE",
    )


def _empty_detection(reason_code: str) -> TemplateDetection:
    return TemplateDetection(
        status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
        source_registry_id=None,
        template_id=None,
        template_fingerprint=None,
        method=DetectionMethod.NONE,
        required_marker_matches=0,
        required_marker_total=0,
        reason_code=reason_code,
    )


def _sniff_media_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"%PDF-"):
        return "PDF", "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "IMAGE", "image/png"
    if data.startswith(b"\xff\xd8"):
        return "IMAGE", "image/jpeg"
    return "UNKNOWN", "application/octet-stream"


def _expected_media_from_extension(extension: str) -> str:
    extension = extension.lower()
    if extension == ".pdf":
        return "PDF"
    if extension in {".png", ".jpg", ".jpeg"}:
        return "IMAGE"
    return "UNKNOWN"


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG header")
    if data[12:16] != b"IHDR":
        raise ValueError("PNG missing IHDR")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return width, height


_JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
}


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG header")

    offset = 2
    length = len(data)

    while offset < length:
        while offset < length and data[offset] == 0xFF:
            offset += 1
        if offset >= length:
            break

        marker = data[offset]
        offset += 1

        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if offset + 2 > length:
            raise ValueError("truncated JPEG segment")

        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2:
            raise ValueError("invalid JPEG segment length")

        if marker in _JPEG_SOF_MARKERS:
            if offset + 7 > length:
                raise ValueError("truncated JPEG SOF segment")
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if width <= 0 or height <= 0:
                raise ValueError("JPEG dimensions must be positive")
            return width, height

        offset += segment_length

    raise ValueError("JPEG dimensions not found")


def _image_dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        return _png_dimensions(data)
    if mime_type == "image/jpeg":
        return _jpeg_dimensions(data)
    raise ValueError("unsupported image mime type")


def _pdf_preflight(
    data: bytes,
    *,
    extension: str,
    policy: PreflightPolicy,
    signatures: Sequence[TemplateSignature],
    source_hint: str | None,
) -> DocumentPreflight:
    digest = sha256(data).hexdigest()

    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except (PdfReadError, ValueError, TypeError, OSError):
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=len(data),
            extension=extension,
            media_type="PDF",
            mime_type="application/pdf",
            quality_status=PreflightQuality.CORRUPT,
            page_count=None,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=PdfEncryptionState.UNENCRYPTED,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("CORRUPT_PDF"),
        )

    encryption_state = PdfEncryptionState.UNENCRYPTED

    if reader.is_encrypted:
        try:
            decrypt_result = reader.decrypt("")
        except Exception:
            decrypt_result = 0

        if not decrypt_result:
            return DocumentPreflight(
                content_sha256=digest,
                size_bytes=len(data),
                extension=extension,
                media_type="PDF",
                mime_type="application/pdf",
                quality_status=PreflightQuality.ENCRYPTED_LOCKED,
                page_count=None,
                image_dimensions=None,
                text_layer_available=False,
                pdf_encryption_state=PdfEncryptionState.ENCRYPTED_LOCKED,
                period_status=PeriodStatus.UNKNOWN,
                template_detection=_empty_detection("ENCRYPTED_LOCKED"),
            )
        encryption_state = PdfEncryptionState.ENCRYPTED_READABLE

    try:
        page_count = len(reader.pages)
    except Exception:
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=len(data),
            extension=extension,
            media_type="PDF",
            mime_type="application/pdf",
            quality_status=PreflightQuality.CORRUPT,
            page_count=None,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=encryption_state,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("PAGE_COUNT_FAILED"),
        )

    text_parts: list[str] = []
    try:
        for index in range(min(page_count, policy.max_pdf_text_pages)):
            text = reader.pages[index].extract_text() or ""
            if text.strip():
                text_parts.append(text)
    except Exception:
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=len(data),
            extension=extension,
            media_type="PDF",
            mime_type="application/pdf",
            quality_status=PreflightQuality.CORRUPT,
            page_count=page_count,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=encryption_state,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("TEXT_PREFLIGHT_FAILED"),
        )

    text_layer_available = bool(text_parts)

    if page_count == 0:
        quality = PreflightQuality.EMPTY
    elif not text_layer_available:
        quality = PreflightQuality.NO_TEXT_LAYER
    else:
        quality = PreflightQuality.READY

    detection = (
        detect_template(
            "\n".join(text_parts),
            media_type="PDF",
            signatures=signatures,
            source_hint=source_hint,
        )
        if text_layer_available
        else _empty_detection("NO_TEXT_LAYER")
    )

    return DocumentPreflight(
        content_sha256=digest,
        size_bytes=len(data),
        extension=extension,
        media_type="PDF",
        mime_type="application/pdf",
        quality_status=quality,
        page_count=page_count,
        image_dimensions=None,
        text_layer_available=text_layer_available,
        pdf_encryption_state=encryption_state,
        period_status=PeriodStatus.UNKNOWN,
        template_detection=detection,
    )


def _image_preflight(
    data: bytes,
    *,
    extension: str,
    mime_type: str,
    policy: PreflightPolicy,
    signatures: Sequence[TemplateSignature] = SOURCE_TEMPLATE_SIGNATURES_V1,
    source_hint: str | None = None,
) -> DocumentPreflight:
    digest = sha256(data).hexdigest()

    try:
        dimensions = _image_dimensions(data, mime_type)
    except ValueError:
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=len(data),
            extension=extension,
            media_type="IMAGE",
            mime_type=mime_type,
            quality_status=PreflightQuality.CORRUPT,
            page_count=None,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=PdfEncryptionState.NOT_PDF,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("IMAGE_HEADER_INVALID"),
        )

    width, height = dimensions

    normalized_hint = _normalize_marker(source_hint) if source_hint and str(source_hint).strip() else None
    if normalized_hint == "shopeepay_mutation":
        shopee_sig = None
        for sig in signatures:
            if sig.source_registry_id == "shopeepay_mutation" and sig.media_type == "IMAGE":
                shopee_sig = sig
                break

        if shopee_sig is not None:
            return DocumentPreflight(
                content_sha256=digest,
                size_bytes=len(data),
                extension=extension,
                media_type="IMAGE",
                mime_type=mime_type,
                quality_status=PreflightQuality.READY,
                page_count=None,
                image_dimensions=dimensions,
                text_layer_available=False,
                pdf_encryption_state=PdfEncryptionState.NOT_PDF,
                period_status=PeriodStatus.UNKNOWN,
                template_detection=TemplateDetection(
                    status=TemplateMatchStatus.KNOWN,
                    source_registry_id=shopee_sig.source_registry_id,
                    template_id=shopee_sig.template_id,
                    template_fingerprint=template_fingerprint(shopee_sig),
                    method=DetectionMethod.SOURCE_HINT,
                    required_marker_matches=0,
                    required_marker_total=len(shopee_sig.required_markers),
                    reason_code="SOURCE_HINT_IMAGE_ROUTING",
                ),
            )

    quality = (
        PreflightQuality.LOW_RESOLUTION
        if width < policy.min_image_width or height < policy.min_image_height
        else PreflightQuality.READY
    )

    return DocumentPreflight(
        content_sha256=digest,
        size_bytes=len(data),
        extension=extension,
        media_type="IMAGE",
        mime_type=mime_type,
        quality_status=quality,
        page_count=None,
        image_dimensions=dimensions,
        text_layer_available=False,
        pdf_encryption_state=PdfEncryptionState.NOT_PDF,
        period_status=PeriodStatus.UNKNOWN,
        template_detection=_empty_detection("VISUAL_TEMPLATE_REQUIRES_IMAGE_ADAPTER"),
    )


def preflight_bytes(
    data: bytes,
    *,
    extension: str,
    policy: PreflightPolicy | None = None,
    signatures: Sequence[TemplateSignature] = SOURCE_TEMPLATE_SIGNATURES_V1,
    source_hint: str | None = None,
) -> DocumentPreflight:
    policy = policy or PreflightPolicy()
    extension = str(extension).strip().lower()

    if extension and not extension.startswith("."):
        extension = "." + extension

    digest = sha256(data).hexdigest()

    if not data:
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=0,
            extension=extension,
            media_type="UNKNOWN",
            mime_type="application/octet-stream",
            quality_status=PreflightQuality.EMPTY,
            page_count=None,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=PdfEncryptionState.NOT_PDF,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("EMPTY_FILE"),
        )

    media_type, mime_type = _sniff_media_type(data)
    expected_media = _expected_media_from_extension(extension)

    if expected_media == "UNKNOWN":
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=len(data),
            extension=extension,
            media_type=media_type,
            mime_type=mime_type,
            quality_status=PreflightQuality.UNSUPPORTED_MEDIA,
            page_count=None,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=PdfEncryptionState.NOT_PDF,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("UNSUPPORTED_EXTENSION"),
        )

    if media_type != expected_media:
        return DocumentPreflight(
            content_sha256=digest,
            size_bytes=len(data),
            extension=extension,
            media_type=media_type,
            mime_type=mime_type,
            quality_status=PreflightQuality.MEDIA_MISMATCH,
            page_count=None,
            image_dimensions=None,
            text_layer_available=False,
            pdf_encryption_state=PdfEncryptionState.NOT_PDF,
            period_status=PeriodStatus.UNKNOWN,
            template_detection=_empty_detection("EXTENSION_MAGIC_MISMATCH"),
        )

    if media_type == "PDF":
        return _pdf_preflight(
            data,
            extension=extension,
            policy=policy,
            signatures=signatures,
            source_hint=source_hint,
        )

    if media_type == "IMAGE":
        return _image_preflight(
            data,
            extension=extension,
            mime_type=mime_type,
            policy=policy,
            signatures=signatures,
            source_hint=source_hint,
        )

    return DocumentPreflight(
        content_sha256=digest,
        size_bytes=len(data),
        extension=extension,
        media_type=media_type,
        mime_type=mime_type,
        quality_status=PreflightQuality.UNSUPPORTED_MEDIA,
        page_count=None,
        image_dimensions=None,
        text_layer_available=False,
        pdf_encryption_state=PdfEncryptionState.NOT_PDF,
        period_status=PeriodStatus.UNKNOWN,
        template_detection=_empty_detection("UNSUPPORTED_MEDIA"),
    )


def preflight_file(
    path: str | Path,
    *,
    policy: PreflightPolicy | None = None,
    signatures: Sequence[TemplateSignature] = SOURCE_TEMPLATE_SIGNATURES_V1,
    source_hint: str | None = None,
) -> DocumentPreflight:
    path = Path(path)
    data = path.read_bytes()
    return preflight_bytes(
        data,
        extension=path.suffix,
        policy=policy,
        signatures=signatures,
        source_hint=source_hint,
    )
