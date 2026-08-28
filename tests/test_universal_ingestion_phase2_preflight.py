import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter

from aturuang.ingestion_contracts import (
    PeriodStatus,
    TemplateMatchStatus,
)
from aturuang.ingestion_preflight import (
    DetectionMethod,
    PdfEncryptionState,
    PreflightPolicy,
    PreflightQuality,
    SOURCE_TEMPLATE_SIGNATURES_V1,
    TemplateSignature,
    detect_template,
    preflight_bytes,
    preflight_file,
    template_fingerprint,
)


def make_text_pdf(text: str) -> bytes:
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
    stream = f"BT /F1 10 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> "
            b"/Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]

    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_pdf_bytes(password: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(password)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def jpeg_with_dimensions(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xe0\x00\x04AB"
        + b"\xff\xc0\x00\x0b\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00"
        + b"\xff\xd9"
    )


class TestUniversalIngestionPhase2Preflight(unittest.TestCase):
    def test_01_template_signature_normalizes_markers(self):
        signature = TemplateSignature(
            source_registry_id="demo",
            template_id="demo_v1",
            media_type="pdf",
            required_markers=("  HELLO   WORLD  ",),
        )
        self.assertEqual(signature.media_type, "PDF")
        self.assertEqual(signature.required_markers, ("hello world",))

    def test_02_template_signature_requires_markers(self):
        with self.assertRaises(ValueError):
            TemplateSignature(
                source_registry_id="demo",
                template_id="demo_v1",
                media_type="PDF",
                required_markers=(),
            )

    def test_03_fingerprint_is_deterministic(self):
        signature = TemplateSignature(
            source_registry_id="demo",
            template_id="demo_v1",
            media_type="PDF",
            required_markers=("A", "B"),
        )
        self.assertEqual(
            template_fingerprint(signature),
            template_fingerprint(signature),
        )
        self.assertEqual(len(template_fingerprint(signature)), 64)

    def test_04_bca_markers_detect_known_template(self):
        result = detect_template(
            """
            REKENING TAHAPAN XPRESI
            NO. REKENING
            PERIODE
            TANGGAL KETERANGAN CBG MUTASI SALDO
            """,
            media_type="PDF",
        )
        self.assertEqual(result.status, TemplateMatchStatus.KNOWN)
        self.assertEqual(result.source_registry_id, "bca_statement")
        self.assertEqual(result.template_id, "bca_monthly_statement_v1")
        self.assertEqual(result.method, DetectionMethod.TEXT_MARKERS)

    def test_05_marker_detection_is_case_and_whitespace_insensitive(self):
        signature = TemplateSignature(
            source_registry_id="demo",
            template_id="demo_v1",
            media_type="PDF",
            required_markers=("Hello World", "Second Marker"),
        )
        result = detect_template(
            "hello    world\nSECOND marker",
            media_type="pdf",
            signatures=(signature,),
        )
        self.assertEqual(result.status, TemplateMatchStatus.KNOWN)

    def test_06_partial_marker_without_hint_is_unknown(self):
        result = detect_template(
            "REKENING TAHAPAN XPRESI NO. REKENING",
            media_type="PDF",
        )
        self.assertEqual(result.status, TemplateMatchStatus.UNKNOWN_TEMPLATE)

    def test_07_partial_marker_with_source_hint_is_template_drift(self):
        result = detect_template(
            "REKENING TAHAPAN XPRESI NO. REKENING",
            media_type="PDF",
            source_hint="bca_statement",
        )
        self.assertEqual(result.status, TemplateMatchStatus.TEMPLATE_DRIFT)
        self.assertEqual(result.source_registry_id, "bca_statement")

    def test_08_unknown_source_hint_does_not_create_false_drift(self):
        result = detect_template(
            "unrelated text",
            media_type="PDF",
            source_hint="not_registered",
        )
        self.assertEqual(result.status, TemplateMatchStatus.UNKNOWN_TEMPLATE)

    def test_09_multiple_exact_signatures_fail_closed_as_ambiguous(self):
        signatures = (
            TemplateSignature(
                source_registry_id="one",
                template_id="one_v1",
                media_type="PDF",
                required_markers=("SAME",),
            ),
            TemplateSignature(
                source_registry_id="two",
                template_id="two_v1",
                media_type="PDF",
                required_markers=("SAME",),
            ),
        )
        result = detect_template(
            "same",
            media_type="PDF",
            signatures=signatures,
        )
        self.assertEqual(result.status, TemplateMatchStatus.UNKNOWN_TEMPLATE)
        self.assertEqual(result.reason_code, "AMBIGUOUS_SIGNATURE")

    def test_10_default_signatures_have_unique_template_ids(self):
        template_ids = [
            signature.template_id
            for signature in SOURCE_TEMPLATE_SIGNATURES_V1
        ]
        self.assertEqual(len(template_ids), len(set(template_ids)))

    def test_11_text_pdf_has_text_layer_and_page_count(self):
        result = preflight_bytes(
            make_text_pdf("HELLO PREFLIGHT"),
            extension=".pdf",
            signatures=(),
        )
        self.assertEqual(result.page_count, 1)
        self.assertTrue(result.text_layer_available)
        self.assertEqual(result.quality_status, PreflightQuality.READY)
        self.assertEqual(
            result.pdf_encryption_state,
            PdfEncryptionState.UNENCRYPTED,
        )

    def test_12_pdf_known_template_becomes_adapter_ready(self):
        result = preflight_bytes(
            make_text_pdf(
                "REKENING TAHAPAN XPRESI "
                "NO. REKENING "
                "PERIODE "
                "TANGGAL KETERANGAN CBG MUTASI SALDO"
            ),
            extension=".pdf",
        )
        self.assertEqual(
            result.template_detection.status,
            TemplateMatchStatus.KNOWN,
        )
        self.assertTrue(result.adapter_ready)

    def test_13_blank_pdf_has_no_text_layer_and_is_not_adapter_ready(self):
        result = preflight_bytes(blank_pdf_bytes(), extension=".pdf")
        self.assertEqual(result.page_count, 1)
        self.assertFalse(result.text_layer_available)
        self.assertEqual(
            result.quality_status,
            PreflightQuality.NO_TEXT_LAYER,
        )
        self.assertFalse(result.adapter_ready)

    def test_14_corrupt_pdf_fails_closed(self):
        result = preflight_bytes(
            b"%PDF-1.7\nnot-a-real-pdf",
            extension=".pdf",
        )
        self.assertEqual(result.quality_status, PreflightQuality.CORRUPT)
        self.assertFalse(result.adapter_ready)

    def test_15_password_locked_pdf_fails_closed(self):
        result = preflight_bytes(
            encrypted_pdf_bytes("secret"),
            extension=".pdf",
        )
        self.assertEqual(
            result.quality_status,
            PreflightQuality.ENCRYPTED_LOCKED,
        )
        self.assertEqual(
            result.pdf_encryption_state,
            PdfEncryptionState.ENCRYPTED_LOCKED,
        )
        self.assertFalse(result.adapter_ready)

    def test_16_empty_password_encrypted_pdf_is_readable_encryption_state(self):
        result = preflight_bytes(
            encrypted_pdf_bytes(""),
            extension=".pdf",
        )
        self.assertEqual(
            result.pdf_encryption_state,
            PdfEncryptionState.ENCRYPTED_READABLE,
        )
        self.assertEqual(result.page_count, 1)

    def test_17_extension_magic_mismatch_pdf_name_with_png_bytes(self):
        result = preflight_bytes(
            png_header(800, 1200),
            extension=".pdf",
        )
        self.assertEqual(
            result.quality_status,
            PreflightQuality.MEDIA_MISMATCH,
        )
        self.assertFalse(result.adapter_ready)

    def test_18_extension_magic_mismatch_image_name_with_pdf_bytes(self):
        result = preflight_bytes(
            make_text_pdf("hello"),
            extension=".png",
        )
        self.assertEqual(
            result.quality_status,
            PreflightQuality.MEDIA_MISMATCH,
        )

    def test_19_png_dimensions_are_preflighted(self):
        result = preflight_bytes(
            png_header(800, 1200),
            extension=".png",
        )
        self.assertEqual(result.image_dimensions, (800, 1200))
        self.assertEqual(result.quality_status, PreflightQuality.READY)
        self.assertEqual(result.mime_type, "image/png")

    def test_20_jpeg_dimensions_are_preflighted(self):
        result = preflight_bytes(
            jpeg_with_dimensions(1200, 1600),
            extension=".jpg",
        )
        self.assertEqual(result.image_dimensions, (1200, 1600))
        self.assertEqual(result.quality_status, PreflightQuality.READY)

    def test_21_low_resolution_image_is_flagged(self):
        result = preflight_bytes(
            png_header(118, 1036),
            extension=".png",
        )
        self.assertEqual(
            result.quality_status,
            PreflightQuality.LOW_RESOLUTION,
        )
        self.assertFalse(result.adapter_ready)

    def test_22_image_template_never_claims_known_without_visual_adapter(self):
        result = preflight_bytes(
            png_header(800, 1200),
            extension=".png",
            source_hint="shopeepay_history",
        )
        self.assertEqual(
            result.template_detection.status,
            TemplateMatchStatus.UNKNOWN_TEMPLATE,
        )
        self.assertEqual(
            result.template_detection.reason_code,
            "VISUAL_TEMPLATE_REQUIRES_IMAGE_ADAPTER",
        )
        self.assertFalse(result.adapter_ready)

    def test_23_invalid_png_header_fails_closed(self):
        result = preflight_bytes(
            b"\x89PNG\r\n\x1a\n" + b"x" * 20,
            extension=".png",
        )
        self.assertEqual(result.quality_status, PreflightQuality.CORRUPT)

    def test_24_invalid_jpeg_header_fails_closed(self):
        result = preflight_bytes(
            b"\xff\xd8\xff\xd9",
            extension=".jpeg",
        )
        self.assertEqual(result.quality_status, PreflightQuality.CORRUPT)

    def test_25_zero_byte_file_is_empty(self):
        result = preflight_bytes(b"", extension=".pdf")
        self.assertEqual(result.quality_status, PreflightQuality.EMPTY)
        self.assertEqual(result.size_bytes, 0)

    def test_26_unsupported_extension_fails_closed(self):
        result = preflight_bytes(
            b"plain text",
            extension=".txt",
        )
        self.assertEqual(
            result.quality_status,
            PreflightQuality.UNSUPPORTED_MEDIA,
        )

    def test_27_preflight_hash_is_exact_sha256(self):
        data = make_text_pdf("hash me")
        result = preflight_bytes(
            data,
            extension=".pdf",
            signatures=(),
        )
        self.assertEqual(
            result.content_sha256,
            hashlib.sha256(data).hexdigest(),
        )

    def test_28_period_stays_unknown_during_preflight(self):
        result = preflight_bytes(
            make_text_pdf("some text"),
            extension=".pdf",
            signatures=(),
        )
        self.assertEqual(result.period_status, PeriodStatus.UNKNOWN)

    def test_29_preflight_result_does_not_return_extracted_text(self):
        secret = "SYNTHETIC-PRIVATE-CONTENT-DO-NOT-RETURN"
        result = preflight_bytes(
            make_text_pdf(secret),
            extension=".pdf",
            signatures=(),
        )
        self.assertNotIn(secret, repr(result))

    def test_30_preflight_file_reads_ordinary_local_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "statement.pdf"
            path.write_bytes(make_text_pdf("ordinary file"))
            result = preflight_file(path, signatures=())
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.quality_status, PreflightQuality.READY)

    def test_31_policy_limits_pdf_text_pages(self):
        policy = PreflightPolicy(max_pdf_text_pages=1)
        self.assertEqual(policy.max_pdf_text_pages, 1)

    def test_32_policy_rejects_zero_text_page_limit(self):
        with self.assertRaises(ValueError):
            PreflightPolicy(max_pdf_text_pages=0)

    def test_33_template_detection_does_not_return_raw_document_text(self):
        secret = "PRIVATE-VALUE-123"
        result = detect_template(
            secret,
            media_type="PDF",
            signatures=(),
        )
        self.assertNotIn(secret, repr(result))

    def test_34_consistent_source_hint_preserves_known_template(self):
        text = (
            "REKENING TAHAPAN XPRESI NO. REKENING PERIODE "
            "TANGGAL KETERANGAN CBG MUTASI SALDO"
        )
        result = detect_template(
            text,
            media_type="PDF",
            source_hint="bca_statement",
        )
        self.assertEqual(result.status, TemplateMatchStatus.KNOWN)
        self.assertEqual(result.source_registry_id, "bca_statement")
        self.assertEqual(result.reason_code, "EXACT_REQUIRED_MARKERS")

    def test_35_conflicting_source_hint_fails_closed(self):
        text = (
            "REKENING TAHAPAN XPRESI NO. REKENING PERIODE "
            "TANGGAL KETERANGAN CBG MUTASI SALDO"
        )
        result = detect_template(
            text,
            media_type="PDF",
            source_hint="jago_statement",
        )
        self.assertEqual(
            result.status,
            TemplateMatchStatus.UNKNOWN_TEMPLATE,
        )
        self.assertEqual(result.reason_code, "SOURCE_HINT_CONFLICT")
        self.assertIsNone(result.source_registry_id)
        self.assertIsNone(result.template_id)
        self.assertIsNone(result.template_fingerprint)

    def test_36_conflicting_source_hint_is_not_adapter_ready(self):
        result = preflight_bytes(
            make_text_pdf(
                "REKENING TAHAPAN XPRESI NO. REKENING PERIODE "
                "TANGGAL KETERANGAN CBG MUTASI SALDO"
            ),
            extension=".pdf",
            source_hint="jago_statement",
        )
        self.assertEqual(
            result.template_detection.reason_code,
            "SOURCE_HINT_CONFLICT",
        )
        self.assertFalse(result.adapter_ready)

if __name__ == "__main__":
    unittest.main()
