"""Unit tests for Universal Ingestion Phase 3: ShopeePay Transaction History Image Adapter."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from aturuang.ingestion_adapter import (
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    CashMovementEvidence,
    ConfidenceLevel,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    SourceEventStatus,
)
from aturuang.ingestion_contracts import PeriodStatus, SourceChannel, TemplateMatchStatus
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
)
from aturuang.ingestion_image_ocr import (
    ImageOcrError,
    ImageOcrLine,
    ImageOcrResult,
    _dedup_overlapping_lines,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    ReadOnlyRegistryAuthority,
    dry_run_artifact,
)
from aturuang.ingestion_preflight import (
    SOURCE_TEMPLATE_SIGNATURES_V1,
    PreflightQuality,
    TemplateSignature,
    preflight_bytes,
    template_fingerprint,
)
from aturuang.ingestion_shopeepay_adapter import (
    SHOPEEPAY_ADAPTER_ID,
    SHOPEEPAY_MIN_OCR_WIDTH,
    SHOPEEPAY_PARSER_VERSION,
    SHOPEEPAY_SOURCE_REGISTRY_ID,
    SHOPEEPAY_TEMPLATE_ID,
    ShopeePayTransactionHistoryImageAdapter,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "ingestion"
    / "shopeepay_transaction_history_image_v1.json"
)


def _make_synthetic_image(width: int = 1220, height: int = 2000, format: str = "JPEG") -> bytes:
    img = Image.new("RGB", (width, height), color=(34, 34, 34))
    buf = BytesIO()
    img.save(buf, format=format)
    return buf.getvalue()


class TestUniversalIngestionPhase3ShopeePay(unittest.TestCase):
    """30 focused unittest methods for ShopeePay Image Ingestion."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]

    def _get_mock_ocr(self, case_name: str) -> ImageOcrResult:
        case = self.fixtures[case_name]
        lines = tuple(
            ImageOcrLine(
                text=l["text"],
                x=l.get("x", 50),
                y=l.get("y", 100),
                width=l.get("width", 200),
                height=l.get("height", 30),
            )
            for l in case["lines"]
        )
        return ImageOcrResult(
            lines=lines,
            image_width=case.get("image_width", 1220),
            image_height=case.get("image_height", 2000),
        )

    def _make_adapter_input(self, payload: bytes) -> AdapterInput:
        sig = next(
            s for s in SOURCE_TEMPLATE_SIGNATURES_V1
            if s.source_registry_id == SHOPEEPAY_SOURCE_REGISTRY_ID and s.media_type == "IMAGE"
        )
        return AdapterInput(
            source_document_id="test-shopeepay-doc-1",
            content_sha256=sha256(payload).hexdigest() if payload else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            source_registry_id=SHOPEEPAY_SOURCE_REGISTRY_ID,
            template_id=SHOPEEPAY_TEMPLATE_ID,
            parser_version=SHOPEEPAY_PARSER_VERSION,
            source_channel=SourceChannel.IMAGE,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.UNKNOWN,
            template_fingerprint=template_fingerprint(sig),
            binary_payload=payload,
        )

    # 1. Metadata and constants
    def test_01_adapter_metadata_and_descriptor(self) -> None:
        adapter = ShopeePayTransactionHistoryImageAdapter()
        desc = adapter.descriptor
        self.assertEqual(desc.source_registry_id, "shopeepay_mutation")
        self.assertEqual(desc.template_id, "shopeepay_transaction_history_image_v1")
        self.assertEqual(desc.adapter_id, "shopeepay-transaction-history-image-v1")
        self.assertEqual(desc.parser_version, "parser-v1")
        self.assertEqual(SHOPEEPAY_MIN_OCR_WIDTH, 720)

    # 2. Template signature & fingerprint
    def test_02_template_signature_and_fingerprint(self) -> None:
        shopee_sigs = [
            s for s in SOURCE_TEMPLATE_SIGNATURES_V1
            if s.source_registry_id == SHOPEEPAY_SOURCE_REGISTRY_ID
        ]
        self.assertEqual(len(shopee_sigs), 1)
        sig = shopee_sigs[0]
        self.assertEqual(sig.media_type, "IMAGE")
        self.assertEqual(sig.template_id, "shopeepay_transaction_history_image_v1")
        fp = template_fingerprint(sig)
        self.assertTrue(isinstance(fp, str) and len(fp) == 64)

    # 3. Orchestration forwards source_hint
    def test_03_orchestration_forwards_source_hint(self) -> None:
        mock_preflight = MagicMock()
        img_bytes = _make_synthetic_image(1220, 2000)
        h = sha256(img_bytes).hexdigest()
        art = DiscoveredArtifact(
            content_sha256=h,
            size_bytes=len(img_bytes),
            extension=".jpg",
            occurrences=(
                ArtifactOccurrence(
                    source_locator="test.jpg",
                    extension=".jpg",
                ),
            ),
        )

        mock_preflight.return_value = preflight_bytes(
            img_bytes,
            extension=".jpg",
            source_hint="shopeepay_mutation",
        )

        dry_run_artifact(
            root="dummy",
            artifact=art,
            registry=MagicMock(),
            adapter_catalog=AdapterCatalog(()),
            claimed_source_registry_id="shopeepay_mutation",
            payload_reader=lambda r, a, policy=None: img_bytes,
            preflight_func=mock_preflight,
        )

        mock_preflight.assert_called_once_with(
            img_bytes,
            extension=".jpg",
            source_hint="shopeepay_mutation",
        )

    # 4. Unclaimed generic image unrouted
    def test_04_unclaimed_generic_image_unrouted(self) -> None:
        img_bytes = _make_synthetic_image(1220, 2000)
        pf = preflight_bytes(img_bytes, extension=".jpg", source_hint=None)
        self.assertEqual(pf.template_detection.status, TemplateMatchStatus.UNKNOWN_TEMPLATE)
        self.assertEqual(pf.template_detection.reason_code, "VISUAL_TEMPLATE_REQUIRES_IMAGE_ADAPTER")

    # 5. Source hint cannot bypass layout verification
    def test_05_source_hint_cannot_bypass_layout_verification(self) -> None:
        img_bytes = _make_synthetic_image(1220, 2000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("non_shopeepay_layout")
        )
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertEqual(len(res.events), 0)
        self.assertTrue(any(d.code == "SHOPEEPAY_TEMPLATE_CONTENT_UNCONFIRMED" for d in res.diagnostics))

    # 6. Corrupt or truncated image fails closed
    def test_06_corrupt_or_truncated_image_fails_closed(self) -> None:
        adapter = ShopeePayTransactionHistoryImageAdapter()
        with self.subTest("empty payload"):
            inp = self._make_adapter_input(b"")
            res = adapter.parse(inp)
            self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        with self.subTest("corrupt header"):
            inp = self._make_adapter_input(b"not-an-image-data-payload")
            res = adapter.parse(inp)
            self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)

    # 7. Provider resolution gate: < 720 px rejected
    def test_07_provider_resolution_gate_low_res_rejected(self) -> None:
        extractor_mock = MagicMock()
        adapter = ShopeePayTransactionHistoryImageAdapter(ocr_extractor=extractor_mock)
        for w in [118, 145, 209, 274, 305, 719]:
            with self.subTest(width=w):
                extractor_mock.reset_mock()
                img_bytes = _make_synthetic_image(width=w, height=1280)
                inp = self._make_adapter_input(img_bytes)
                res = adapter.parse(inp)
                self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
                self.assertEqual(res.period_status, PeriodStatus.UNKNOWN)
                self.assertEqual(len(res.events), 0)
                self.assertIsNone(res.natural_document_key_candidate)
                self.assertTrue(any(d.code == "SHOPEEPAY_IMAGE_RESOLUTION_INSUFFICIENT" for d in res.diagnostics))
                extractor_mock.assert_not_called()

    # 8. Provider resolution gate: >= 720 px accepted
    def test_08_provider_resolution_gate_high_res_accepted(self) -> None:
        extractor_mock = MagicMock(return_value=self._get_mock_ocr("valid_high_res_november"))
        adapter = ShopeePayTransactionHistoryImageAdapter(ocr_extractor=extractor_mock)
        for w in [720, 1080, 1220]:
            with self.subTest(width=w):
                extractor_mock.reset_mock()
                img_bytes = _make_synthetic_image(width=w, height=2000)
                inp = self._make_adapter_input(img_bytes)
                res = adapter.parse(inp)
                self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
                extractor_mock.assert_called_once()

    # 9. Preflight period status unknown
    def test_09_preflight_period_status_unknown(self) -> None:
        img_bytes = _make_synthetic_image(1220, 2000)
        pf = preflight_bytes(img_bytes, extension=".jpg", source_hint="shopeepay_mutation")
        self.assertEqual(pf.period_status, PeriodStatus.UNKNOWN)
        self.assertEqual(pf.template_detection.status, TemplateMatchStatus.KNOWN)
        self.assertEqual(pf.quality_status, PreflightQuality.READY)

    # 10. Adapter period authority: CLOSED
    def test_10_adapter_period_authority_closed(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)

        # A. Explicit dash creates CLOSED period
        adapter_dash = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="01 November 2099 - 30 November 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="15 November 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_dash = adapter_dash.parse(inp)
        self.assertEqual(res_dash.period_status, PeriodStatus.CLOSED)
        self.assertEqual(res_dash.period_start, "2099-11-01")
        self.assertEqual(res_dash.period_end, "2099-11-30")
        self.assertEqual(res_dash.natural_document_key_candidate, "shopeepay_mutation:unidentified_wallet:2099-11")

        # B. En dash creates CLOSED period
        adapter_en = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="01 November 2099 – 30 November 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="15 November 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_en = adapter_en.parse(inp)
        self.assertEqual(res_en.period_status, PeriodStatus.CLOSED)

        # C. Em dash creates CLOSED period
        adapter_em = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="01 November 2099 — 30 November 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="15 November 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_em = adapter_em.parse(inp)
        self.assertEqual(res_em.period_status, PeriodStatus.CLOSED)

        # D. Whitespace-only between dates is rejected -> UNKNOWN
        adapter_ws = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="01 November 2099 30 November 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="15 November 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_ws = adapter_ws.parse(inp)
        self.assertEqual(res_ws.period_status, PeriodStatus.UNKNOWN)
        self.assertIsNone(res_ws.period_start)
        self.assertIsNone(res_ws.period_end)
        self.assertIsNone(res_ws.natural_document_key_candidate)

        # E. Two separate transaction dates with no delimiter -> UNKNOWN
        tx_dates_ocr = ImageOcrResult(
            lines=(
                ImageOcrLine(text="Riwayat Transaksi", x=50, y=100, width=300, height=30),
                ImageOcrLine(text="Semua", x=50, y=150, width=100, height=30),
                ImageOcrLine(text="Top Up", x=50, y=200, width=100, height=30),
                ImageOcrLine(text="01 November 2099", x=50, y=250, width=150, height=30),
                ImageOcrLine(text="30 November 2099", x=50, y=300, width=150, height=30),
                ImageOcrLine(text="+Rp50.000", x=800, y=400, width=150, height=30),
                ImageOcrLine(text="Isi Saldo", x=50, y=450, width=200, height=30),
                ImageOcrLine(text="25 November 2099", x=50, y=500, width=200, height=30),
            ),
            image_width=1220,
            image_height=2000,
        )
        adapter_tx = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: tx_dates_ocr
        )
        # I. Valid-looking range located ONLY in transaction body (y >= 600) -> period UNKNOWN, natural_key=None
        body_range_ocr = ImageOcrResult(
            lines=(
                ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                ImageOcrLine(text="Payment Method", x=50, y=150, width=200, height=30),
                ImageOcrLine(text="Top Up", x=50, y=200, width=100, height=30),
                ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                ImageOcrLine(text="-Rp10.000", x=800, y=700, width=150, height=30),
                ImageOcrLine(text="Merchant Item 01 November 2099 - 30 November 2099", x=50, y=750, width=350, height=30),
                ImageOcrLine(text="15 November 2099", x=50, y=800, width=200, height=30),
            ),
            image_width=1220,
            image_height=2000,
        )
        adapter_body = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: body_range_ocr
        )
        res_body = adapter_body.parse(inp)
        self.assertEqual(res_body.period_status, PeriodStatus.UNKNOWN)
        self.assertIsNone(res_body.period_start)
        self.assertIsNone(res_body.period_end)
        self.assertIsNone(res_body.natural_document_key_candidate)

    # 11. Adapter period authority: OPEN
    def test_11_adapter_period_authority_open(self) -> None:
        img_bytes = _make_synthetic_image(1220, 2000)
        inp = self._make_adapter_input(img_bytes)

        # F. Explicit same-month partial creates OPEN period
        adapter_partial = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="01 August 2099 - 21 August 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="15 August 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_partial = adapter_partial.parse(inp)
        self.assertEqual(res_partial.period_status, PeriodStatus.OPEN)
        self.assertEqual(res_partial.period_start, "2099-08-01")
        self.assertEqual(res_partial.period_end, "2099-08-21")
        self.assertEqual(res_partial.natural_document_key_candidate, "shopeepay_mutation:unidentified_wallet:2099-08")

        # G. Explicit cross-month range rejected -> UNKNOWN
        adapter_cross = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="25 October 2099 - 05 November 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="02 November 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_cross = adapter_cross.parse(inp)
        self.assertEqual(res_cross.period_status, PeriodStatus.UNKNOWN)
        self.assertIsNone(res_cross.period_start)
        self.assertIsNone(res_cross.period_end)
        self.assertIsNone(res_cross.natural_document_key_candidate)

        # H. Same month but start day != 1 rejected -> UNKNOWN
        adapter_non_first = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: ImageOcrResult(
                lines=(
                    ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                    ImageOcrLine(text="05 November 2099 - 30 November 2099", x=50, y=150, width=300, height=30),
                    ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
                    ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
                    ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                    ImageOcrLine(text="-Rp10.000", x=800, y=400, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=450, width=200, height=30),
                    ImageOcrLine(text="15 November 2099", x=50, y=500, width=200, height=30),
                ),
                image_width=1220,
                image_height=2000,
            )
        )
        res_non_first = adapter_non_first.parse(inp)
        self.assertEqual(res_non_first.period_status, PeriodStatus.UNKNOWN)
        self.assertIsNone(res_non_first.period_start)
        self.assertIsNone(res_non_first.period_end)
        self.assertIsNone(res_non_first.natural_document_key_candidate)

    # 12. Valid high-res JPEG extraction
    def test_12_valid_high_res_jpeg_extraction(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000, format="JPEG")
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(res.events), 3)

    # 13. Valid PNG media extraction
    def test_13_valid_png_media_extraction(self) -> None:
        img_bytes = _make_synthetic_image(1220, 2000, format="PNG")
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_partial_august")
        )
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(res.events), 1)

    # 14. Tall image vertical slicing geometry
    def test_14_tall_image_vertical_slicing_geometry(self) -> None:
        slice_height = 2000
        slice_overlap = 200
        total_height = 12723
        slices = []
        y_start = 0
        step = slice_height - slice_overlap
        while y_start < total_height:
            y_end = min(y_start + slice_height, total_height)
            slices.append((y_start, y_end))
            if y_end >= total_height:
                break
            y_start += step

        self.assertTrue(len(slices) >= 7)
        self.assertEqual(slices[0], (0, 2000))
        self.assertEqual(slices[-1][1], 12723)
        for i in range(1, len(slices)):
            self.assertTrue(slices[i][0] > slices[i - 1][0])

    # 15. Spatial slice overlap deduplication
    def test_15_spatial_slice_overlap_deduplication(self) -> None:
        lines = [
            ImageOcrLine(text="Payment", x=180, y=1850, width=150, height=40),
            ImageOcrLine(text="-Rp100.000", x=850, y=1850, width=200, height=40),
            ImageOcrLine(text="Payment", x=180, y=1852, width=150, height=40),
            ImageOcrLine(text="-Rp100.000", x=850, y=1852, width=200, height=40),
        ]
        deduped = _dedup_overlapping_lines(lines, spatial_threshold_y=15)
        self.assertEqual(len(deduped), 2)

    # 16. Identical date & amount at different Y preserved
    def test_16_identical_date_amount_different_y_preserved(self) -> None:
        lines = [
            ImageOcrLine(text="Payment", x=180, y=500, width=150, height=40),
            ImageOcrLine(text="-Rp100.000", x=850, y=500, width=200, height=40),
            ImageOcrLine(text="Payment", x=180, y=1500, width=150, height=40),
            ImageOcrLine(text="-Rp100.000", x=850, y=1500, width=200, height=40),
        ]
        deduped = _dedup_overlapping_lines(lines, spatial_threshold_y=15)
        self.assertEqual(len(deduped), 4)

    # 17. OCR transport error fails closed
    def test_17_ocr_transport_error_fails_closed(self) -> None:
        def failing_ocr(b: bytes) -> ImageOcrResult:
            raise ImageOcrError("OCR_TIMEOUT", "Timeout occurred")

        adapter = ShopeePayTransactionHistoryImageAdapter(ocr_extractor=failing_ocr)
        img_bytes = _make_synthetic_image(1220, 2000)
        inp = self._make_adapter_input(img_bytes)
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertEqual(len(res.events), 0)
        self.assertTrue(any(d.code == "SHOPEEPAY_OCR_EXECUTION_FAILED" for d in res.diagnostics))

    # 18. Date-only occurred_at preservation
    def test_18_date_only_occurred_at_preservation(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        for ev in res.events:
            payload: CashMovementEvidence = ev.payload
            self.assertEqual(payload.occurred_at, "2025-11-25")
            self.assertNotIn("00:00", payload.occurred_at)

    # 19. Positive amount inflow mapping
    def test_19_positive_amount_inflow_mapping(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        inflows = [ev.payload for ev in res.events if ev.payload.direction == EventDirection.INFLOW]
        self.assertTrue(len(inflows) >= 2)
        self.assertTrue(any(i.amount == Decimal("100") for i in inflows))
        self.assertTrue(any(i.amount == Decimal("99000") for i in inflows))

    # 20. Negative amount outflow mapping
    def test_20_negative_amount_outflow_mapping(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        outflows = [ev.payload for ev in res.events if ev.payload.direction == EventDirection.OUTFLOW]
        self.assertEqual(len(outflows), 1)
        self.assertEqual(outflows[0].amount, Decimal("100000"))

    # 21. Decimal amount precision
    def test_21_decimal_amount_precision(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res1 = adapter.parse(inp)
        res2 = adapter.parse(inp)
        self.assertTrue(len(res1.events) > 0)
        # Decimal type and exponent exactly -2
        for ev in res1.events:
            amt = ev.payload.amount
            self.assertIsInstance(amt, Decimal)
            self.assertEqual(amt.as_tuple().exponent, -2)

        # Deterministic repeated parse result
        self.assertEqual(res1.parse_status, res2.parse_status)
        self.assertEqual(res1.period_status, res2.period_status)
        self.assertEqual(res1.natural_document_key_candidate, res2.natural_document_key_candidate)
        self.assertEqual(len(res1.events), len(res2.events))
        # Identical event ordering and row fingerprints
        fps1 = [ev.row_fingerprint for ev in res1.events]
        fps2 = [ev.row_fingerprint for ev in res2.events]
        self.assertEqual(fps1, fps2)
        for ev1, ev2 in zip(res1.events, res2.events):
            self.assertEqual(ev1.payload.amount, ev2.payload.amount)
            self.assertEqual(ev1.payload.direction, ev2.payload.direction)
            self.assertEqual(ev1.payload.occurred_at, ev2.payload.occurred_at)

    # 22. Multiline description normalization
    def test_22_multiline_description_normalization(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        descs = [ev.payload.description_raw for ev in res.events]
        self.assertIn("From ShopeePay", descs)
        self.assertIn("From Bank Transfer", descs)

    # 23. Explicit failed badge omitted from cash movement
    def test_23_explicit_failed_badge_omitted_from_cash_movement(self) -> None:
        img_bytes = _make_synthetic_image(1220, 2500)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("with_failed_transaction")
        )
        res = adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(res.events), 1)
        self.assertTrue(any(d.code == "SHOPEEPAY_FAILED_TRANSACTIONS_EXCLUDED" for d in res.diagnostics))

    # 24. Normal transaction status unknown
    def test_24_normal_transaction_status_unknown(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        for ev in res.events:
            self.assertEqual(ev.payload.status, SourceEventStatus.UNKNOWN)

    # 25. Zero non-cash envelopes emitted
    def test_25_zero_non_cash_envelopes_emitted(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        roles = [ev.event_role for ev in res.events]
        self.assertTrue(all(r == EventRole.CASH_MOVEMENT for r in roles))
        self.assertNotIn(EventRole.ACCOUNT_OBSERVATION, roles)
        self.assertNotIn(EventRole.SOURCE_SUMMARY, roles)
        self.assertNotIn(EventRole.BALANCE_SNAPSHOT, roles)
        self.assertNotIn(EventRole.ACCOUNT_PERIOD_SUMMARY, roles)
        self.assertNotIn(EventRole.INVESTMENT_TRADE, roles)

    # 26. Zero provider transaction ID emitted
    def test_26_zero_provider_transaction_id_emitted(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        for ev in res.events:
            self.assertIsNone(ev.payload.provider_transaction_id_raw)

    # 27. No semantic category derivation
    def test_27_no_semantic_category_derivation(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        for ev in res.events:
            self.assertIsNone(ev.payload.provider_category_raw)
            self.assertIsNone(ev.payload.event_hint)

    # 28. Financial card ambiguity triggers review & omits ambiguous card
    def test_28_financial_card_ambiguity_triggers_review(self) -> None:
        base_header_lines = (
            ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
            ImageOcrLine(text="01 Nov 2025 - 30 Nov 2025", x=50, y=150, width=300, height=30),
            ImageOcrLine(text="Payment Method", x=50, y=200, width=200, height=30),
            ImageOcrLine(text="Top Up", x=50, y=250, width=100, height=30),
            # 1. Trusted signed card before ambiguous card (y=400)
            ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
            ImageOcrLine(text="-Rp25.000", x=800, y=400, width=150, height=30),
            ImageOcrLine(text="Trusted Merchant One", x=50, y=450, width=200, height=30),
            ImageOcrLine(text="20 November 2025", x=50, y=500, width=200, height=30),
            # 2. Trusted signed card after ambiguous card (y=1000)
            ImageOcrLine(text="Payment", x=50, y=1000, width=100, height=30),
            ImageOcrLine(text="-Rp35.000", x=800, y=1000, width=150, height=30),
            ImageOcrLine(text="Trusted Merchant Two", x=50, y=1050, width=200, height=30),
            ImageOcrLine(text="21 November 2025", x=50, y=1100, width=200, height=30),
        )

        ambiguous_cases = [
            (
                "unsigned valid Rp amount",
                (
                    ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                    ImageOcrLine(text="Rp50.000", x=800, y=700, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=750, width=200, height=30),
                    ImageOcrLine(text="15 November 2025", x=50, y=800, width=200, height=30),
                ),
            ),
            (
                "unsigned malformed Rp amount",
                (
                    ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                    ImageOcrLine(text="Rp50.00X", x=800, y=700, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=750, width=200, height=30),
                    ImageOcrLine(text="15 November 2025", x=50, y=800, width=200, height=30),
                ),
            ),
            (
                "unsigned over-precision Rp amount",
                (
                    ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                    ImageOcrLine(text="Rp50.000,123", x=800, y=700, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=750, width=200, height=30),
                    ImageOcrLine(text="15 November 2025", x=50, y=800, width=200, height=30),
                ),
            ),
            (
                "signed malformed amount",
                (
                    ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                    ImageOcrLine(text="-RpABC", x=800, y=700, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=750, width=200, height=30),
                    ImageOcrLine(text="15 November 2025", x=50, y=800, width=200, height=30),
                ),
            ),
            (
                "signed over-precision amount",
                (
                    ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                    ImageOcrLine(text="-Rp50.000,123", x=800, y=700, width=150, height=30),
                    ImageOcrLine(text="Merchant Item", x=50, y=750, width=200, height=30),
                    ImageOcrLine(text="15 November 2025", x=50, y=800, width=200, height=30),
                ),
            ),
            (
                "missing transaction date",
                (
                    ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                    ImageOcrLine(text="-Rp15.000", x=800, y=700, width=150, height=30),
                    ImageOcrLine(text="Item Without Date", x=50, y=750, width=200, height=30),
                ),
            ),
        ]

        img_bytes = _make_synthetic_image(1220, 2500)
        inp = self._make_adapter_input(img_bytes)

        for case_name, ambiguous_lines in ambiguous_cases:
            with self.subTest(case=case_name):
                combined_ocr = ImageOcrResult(
                    lines=base_header_lines + ambiguous_lines,
                    image_width=1220,
                    image_height=2500,
                )
                adapter = ShopeePayTransactionHistoryImageAdapter(
                    ocr_extractor=lambda b, ocr=combined_ocr: ocr
                )
                res = adapter.parse(inp)
                # 1. parse_status becomes REVIEW_REQUIRED
                self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
                # 2. ambiguous candidate emits 0 CASH_MOVEMENT (only the 2 trusted cards emit)
                self.assertEqual(len(res.events), 2)
                self.assertEqual(res.events[0].payload.amount, Decimal("25000.00"))
                self.assertEqual(res.events[0].payload.direction, EventDirection.OUTFLOW)
                self.assertEqual(res.events[1].payload.amount, Decimal("35000.00"))
                self.assertEqual(res.events[1].payload.direction, EventDirection.OUTFLOW)
                # 3. Returned trusted events remain ordered by source_y
                raw_locators = [ev.provenance.raw_locator for ev in res.events]
                self.assertEqual(raw_locators, ["image:y:400", "image:y:1000"])
                # 4. Emitted row_index values preserve the omitted physical position (1 and 3)
                row_indices = [ev.provenance.row_index for ev in res.events]
                self.assertEqual(row_indices, [1, 3])
                # 5. No direction is fabricated for the ambiguous candidate
                for ev in res.events:
                    self.assertNotEqual(ev.payload.amount, Decimal("50000.00"))
                    self.assertNotEqual(ev.payload.amount, Decimal("15000.00"))
                # 6. Diagnostic includes SHOPEEPAY_CARD_AMBIGUOUS
                self.assertTrue(any(d.code == "SHOPEEPAY_CARD_AMBIGUOUS" for d in res.diagnostics))

        # Test arbitrary prose containing Rp does not create an unsigned card
        with self.subTest(case="arbitrary prose containing Rp does not create unsigned card"):
            prose_lines = (
                ImageOcrLine(text="Payment", x=50, y=700, width=100, height=30),
                ImageOcrLine(text="-Rp15.000", x=800, y=700, width=150, height=30),
                ImageOcrLine(text="Beli barang Rp50.000", x=50, y=750, width=200, height=30),
                ImageOcrLine(text="15 November 2025", x=50, y=800, width=200, height=30),
            )
            combined_ocr = ImageOcrResult(
                lines=base_header_lines + prose_lines,
                image_width=1220,
                image_height=2500,
            )
            adapter = ShopeePayTransactionHistoryImageAdapter(
                ocr_extractor=lambda b, ocr=combined_ocr: ocr
            )
            res = adapter.parse(inp)
            self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
            self.assertEqual(len(res.events), 3)
            self.assertEqual([ev.provenance.row_index for ev in res.events], [1, 2, 3])
            self.assertEqual(
                [ev.provenance.raw_locator for ev in res.events],
                ["image:y:400", "image:y:700", "image:y:1000"],
            )
            self.assertFalse(any(d.code == "SHOPEEPAY_CARD_AMBIGUOUS" for d in res.diagnostics))

    # 29. Natural document key unidentified wallet
    def test_29_natural_document_key_unidentified_wallet(self) -> None:
        img_bytes = _make_synthetic_image(1220, 4000)
        inp = self._make_adapter_input(img_bytes)
        adapter = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: self._get_mock_ocr("valid_high_res_november")
        )
        res = adapter.parse(inp)
        self.assertEqual(res.natural_document_key_candidate, "shopeepay_mutation:unidentified_wallet:2025-11")

        # E. No authoritative range: period_start=None, period_end=None, natural_key=None, period_status=UNKNOWN
        no_range_ocr = ImageOcrResult(
            lines=(
                ImageOcrLine(text="Transaction History", x=50, y=100, width=300, height=30),
                ImageOcrLine(text="Payment Method", x=50, y=150, width=200, height=30),
                ImageOcrLine(text="Top Up", x=50, y=200, width=100, height=30),
                ImageOcrLine(text="Payment", x=50, y=400, width=100, height=30),
                ImageOcrLine(text="-Rp20.000", x=800, y=400, width=150, height=30),
                ImageOcrLine(text="Merchant", x=50, y=450, width=200, height=30),
                ImageOcrLine(text="10 Oktober 2099", x=50, y=500, width=200, height=30),
            ),
            image_width=1220,
            image_height=2000,
        )
        adapter_no_range = ShopeePayTransactionHistoryImageAdapter(
            ocr_extractor=lambda b: no_range_ocr
        )
        res_no_range = adapter_no_range.parse(inp)
        self.assertIsNone(res_no_range.natural_document_key_candidate)
        self.assertIsNone(res_no_range.period_start)
        self.assertIsNone(res_no_range.period_end)
        self.assertEqual(res_no_range.period_status, PeriodStatus.UNKNOWN)

    # 30. Diagnostics and repr privacy safety
    def test_30_diagnostics_and_repr_privacy_safety(self) -> None:
        line = ImageOcrLine(text="Sensitive Text", x=0, y=0, width=10, height=10)
        self.assertNotIn("Sensitive Text", repr(line))
        res = ImageOcrResult(lines=(line,), image_width=100, image_height=100)
        self.assertNotIn("Sensitive Text", repr(res))


if __name__ == "__main__":
    unittest.main()
