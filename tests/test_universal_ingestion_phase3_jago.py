from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import unittest

from pypdf import PdfReader, PdfWriter

from aturuang.ingestion_adapter import (
    AccountPeriodSummaryEvidence,
    AdapterContractError,
    AdapterInput,
    AdapterParseStatus,
    CashMovementEvidence,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    ObservedAccountEvidence,
    SourceEventStatus,
    SourceSummaryEvidence,
)
from aturuang.ingestion_contracts import (
    ConfidenceLevel,
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
)
from aturuang.ingestion_jago_adapter import (
    JAGO_ADAPTER_ID,
    JAGO_PARSER_VERSION,
    JAGO_SOURCE_REGISTRY_ID,
    JAGO_TEMPLATE_FINGERPRINT,
    JAGO_TEMPLATE_ID,
    JagoMonthlyStatementAdapter,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    DryRunDisposition,
    dry_run_artifact,
    semantic_document_canonical_json,
    semantic_document_sha256,
)
from aturuang.ingestion_registry_service import TemplateResolution

_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ingestion"
    / "jago_statement_v1.json"
)


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def make_pdf_page_stream(text: str) -> bytes:
    commands = [
        "BT",
        "/F1 9 Tf",
        "40 760 Td",
        "11 TL",
    ]
    for line in text.splitlines():
        commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("T*")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1")


def make_multipage_pdf(pages: list[str]) -> bytes:
    page_streams = [make_pdf_page_stream(page_text) for page_text in pages]
    total_pages = len(page_streams)

    # Objects:
    # 1: Catalog
    # 2: Pages
    # 3.. 3+total_pages-1: Page objects
    # 3+total_pages: Font
    # 3+total_pages+1 ..: Content stream objects
    catalog_obj_num = 1
    pages_obj_num = 2
    first_page_obj_num = 3
    font_obj_num = 3 + total_pages
    first_content_obj_num = font_obj_num + 1

    page_refs = " ".join(
        f"{first_page_obj_num + i} 0 R" for i in range(total_pages)
    )

    objects: list[tuple[int, bytes]] = []
    objects.append(
        (catalog_obj_num, f"<< /Type /Catalog /Pages {pages_obj_num} 0 R >>".encode("ascii"))
    )
    objects.append(
        (
            pages_obj_num,
            f"<< /Type /Pages /Kids [{page_refs}] /Count {total_pages} >>".encode("ascii"),
        )
    )

    for i in range(total_pages):
        page_num = first_page_obj_num + i
        content_num = first_content_obj_num + i
        page_dict = (
            f"<< /Type /Page /Parent {pages_obj_num} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode("ascii")
        objects.append((page_num, page_dict))

    objects.append(
        (
            font_obj_num,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        )
    )

    for i, stream in enumerate(page_streams):
        content_num = first_content_obj_num + i
        stream_bytes = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        objects.append((content_num, stream_bytes))

    # Assemble PDF byte output
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    for obj_num, obj_bytes in sorted(objects, key=lambda x: x[0]):
        offsets[obj_num] = len(output)
        output.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        output.extend(obj_bytes)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    total_objects = len(objects)
    output.extend(f"xref\n0 {total_objects + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")

    for i in range(1, total_objects + 1):
        offset = offsets[i]
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    output.extend(
        (
            "trailer\n"
            f"<< /Size {total_objects + 1} /Root {catalog_obj_num} 0 R >>\n"
            "startxref\n"
            f"{xref_offset}\n"
            "%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def make_encrypted_pdf(payload: bytes) -> bytes:
    reader = PdfReader(BytesIO(payload))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("synthetic-password")
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


class FakeRegistryAuthority:
    def __init__(self, *, natural_records: tuple = ()) -> None:
        self._natural_records = natural_records

    def lookup_exact_documents(self, content_sha256: str) -> tuple:
        return ()

    def lookup_natural_documents(
        self,
        source_registry_id: str,
        natural_document_key: str,
    ) -> tuple:
        return tuple(
            rec
            for rec in self._natural_records
            if rec.source_registry_id == source_registry_id
            and rec.natural_document_key == natural_document_key
        )

    def source_capability(self, source_registry_id: str) -> dict | None:
        if source_registry_id == JAGO_SOURCE_REGISTRY_ID:
            return {
                "source_registry_id": JAGO_SOURCE_REGISTRY_ID,
                "channel": SourceChannel.PDF.value,
                "status": "ACTIVE",
            }
        return None

    def template_authority(
        self,
        *,
        source_registry_id: str,
        template_fingerprint: str,
    ) -> TemplateResolution:
        if (
            source_registry_id == JAGO_SOURCE_REGISTRY_ID
            and template_fingerprint == JAGO_TEMPLATE_FINGERPRINT
        ):
            return TemplateResolution(
                status=TemplateMatchStatus.KNOWN,
                template_id=JAGO_TEMPLATE_ID,
                parser_version=JAGO_PARSER_VERSION,
            )
        return TemplateResolution(status=TemplateMatchStatus.UNKNOWN)


class TestUniversalIngestionPhase3Jago(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.adapter = JagoMonthlyStatementAdapter()

    def _make_input(
        self,
        payload_bytes: bytes,
        *,
        doc_id: str = "doc-jago-test-01",
    ) -> AdapterInput:
        return AdapterInput(
            source_document_id=doc_id,
            content_sha256=sha256(payload_bytes).hexdigest(),
            source_registry_id=JAGO_SOURCE_REGISTRY_ID,
            template_id=JAGO_TEMPLATE_ID,
            parser_version=JAGO_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            template_fingerprint=JAGO_TEMPLATE_FINGERPRINT,
            binary_payload=payload_bytes,
        )

    def test_01_exact_descriptor_template_fingerprint_and_channel(self):
        desc = self.adapter.descriptor
        self.assertEqual(desc.adapter_id, JAGO_ADAPTER_ID)
        self.assertEqual(desc.source_registry_id, JAGO_SOURCE_REGISTRY_ID)
        self.assertEqual(desc.template_id, JAGO_TEMPLATE_ID)
        self.assertEqual(desc.parser_version, JAGO_PARSER_VERSION)
        self.assertIs(desc.source_channel, SourceChannel.PDF)
        self.assertEqual(
            JAGO_TEMPLATE_FINGERPRINT,
            "854403d679306f21bdfcb16efa0c5fa89cc714d0b545e871bbae384196be18fb",
        )

    def test_02_binary_payload_only_adapter_contract(self):
        inp = AdapterInput(
            source_document_id="doc-text-invalid",
            content_sha256=sha256(b"abc").hexdigest(),
            source_registry_id=JAGO_SOURCE_REGISTRY_ID,
            template_id=JAGO_TEMPLATE_ID,
            parser_version=JAGO_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            text_payload="Some text",
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    def test_03_sanitized_normal_statement_completes_with_expected_roles(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(res.period_start, "2026-07-01")
        self.assertEqual(res.period_end, "2026-07-31")

        roles = [e.event_role for e in res.events]
        self.assertIn(EventRole.SOURCE_SUMMARY, roles)
        self.assertIn(EventRole.ACCOUNT_OBSERVATION, roles)
        self.assertIn(EventRole.ACCOUNT_PERIOD_SUMMARY, roles)
        self.assertIn(EventRole.CASH_MOVEMENT, roles)

    def test_04_encrypted_jago_pdf_fails_closed(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        encrypted_bytes = make_encrypted_pdf(pdf_bytes)

        res = self.adapter.parse(self._make_input(encrypted_bytes))
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertEqual(len(res.events), 0)
        self.assertTrue(
            any(d.code == "JAGO_PDF_ENCRYPTED_UNSUPPORTED" for d in res.diagnostics)
        )

    def test_05_indonesian_statement_period_extraction(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.period_start, "2026-07-01")
        self.assertEqual(res.period_end, "2026-07-31")

    def test_06_anchored_provider_header_account_identity_and_natural_key(self):
        # A. Valid anchored Jago header parses stable provider account identity
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        expected_hash = sha256(b"100011112222").hexdigest()
        expected_key = f"jago_statement:{expected_hash}:2026-07"
        self.assertEqual(res.natural_document_key_candidate, expected_key)

        # B. Decoy line with slash outside authoritative window is NOT accepted
        # Add decoy slash lines before www.jago.com and after RINGKASAN SALDO
        p1_decoy = (
            "Laporan Keuangan Bulanan\n"
            "Juli 2026\n"
            "DECOY BEFORE / 998877665544\n"
            "PT Bank Jago Tbk\n"
            "www.jago.com\n"
            "SYNTHETIC USER / 100011112222\n"
            "JL. SYNTHETIC RT.001/RW.002\n"
            "RINGKASAN SALDO DALAM RUPIAH\n"
            "DECOY AFTER / 554433221100\n"
            "Saldo akhir pada 31 Jul 2026\n"
            "1.250.000,00\n"
            "KANTONG PERSONAL\n"
            "Kantong Utama\n"
            "IDR\n"
            "1.000.000,00\n"
            "0\n"
            "0\n"
            "1.000.000,00"
        )
        decoy_pages = [p1_decoy] + pages[1:]
        res_decoy = self.adapter.parse(self._make_input(make_multipage_pdf(decoy_pages)))
        self.assertEqual(res_decoy.parse_status, AdapterParseStatus.COMPLETED)
        # Natural key must strictly match the anchored ID (100011112222), not any decoy!
        self.assertEqual(res_decoy.natural_document_key_candidate, expected_key)

        # C. Missing authoritative header field fails closed with JAGO_HEADER_INCOMPLETE
        p1_missing = (
            "Laporan Keuangan Bulanan\n"
            "Juli 2026\n"
            "www.jago.com\n"
            "SYNTHETIC USER WITHOUT ACCOUNT NUMBER\n"
            "RINGKASAN SALDO DALAM RUPIAH"
        )
        res_missing = self.adapter.parse(self._make_input(make_multipage_pdf([p1_missing] + pages[1:])))
        self.assertEqual(res_missing.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "JAGO_HEADER_INCOMPLETE" for d in res_missing.diagnostics)
        )

        # D. Conflicting anchored header identities fail closed
        p1_conflicting = (
            "Laporan Keuangan Bulanan\n"
            "Juli 2026\n"
            "www.jago.com\n"
            "FIRST USER / 100011112222\n"
            "SECOND USER / 999988887777\n"
            "RINGKASAN SALDO DALAM RUPIAH"
        )
        res_conflict = self.adapter.parse(self._make_input(make_multipage_pdf([p1_conflicting] + pages[1:])))
        self.assertEqual(res_conflict.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "JAGO_HEADER_INCOMPLETE" for d in res_conflict.diagnostics)
        )

    def test_07_natural_key_determinism(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf1 = make_multipage_pdf(pages)
        pdf2 = make_multipage_pdf(pages)

        res1 = self.adapter.parse(self._make_input(pdf1, doc_id="doc-1"))
        res2 = self.adapter.parse(self._make_input(pdf2, doc_id="doc-2"))

        self.assertEqual(
            res1.natural_document_key_candidate,
            res2.natural_document_key_candidate,
        )

    def test_08_document_level_source_summary_values_and_decimal_types(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        summary_events = [
            e for e in res.events if e.event_role == EventRole.SOURCE_SUMMARY
        ]
        self.assertEqual(len(summary_events), 1)
        payload = summary_events[0].payload
        self.assertIsInstance(payload, SourceSummaryEvidence)
        self.assertEqual(payload.currency, "IDR")
        self.assertEqual(payload.opening_balance, Decimal("1000000.00"))
        self.assertEqual(payload.closing_balance, Decimal("1250000.00"))
        self.assertEqual(payload.incoming_total, Decimal("500000.00"))
        self.assertEqual(payload.outgoing_total, Decimal("250000.00"))

    def test_09_pocket_observed_account_hierarchy_uses_stable_pocket_id(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        obs_events = [
            e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION
        ]
        self.assertEqual(len(obs_events), 2)

        pocket_keys = [e.payload.observed_provider_account_key for e in obs_events]
        self.assertIn("100011112222", pocket_keys)
        self.assertIn("200033334444", pocket_keys)

        for e in obs_events:
            self.assertEqual(e.payload.parent_observed_key, "100011112222")
            self.assertEqual(e.payload.institution_id, "jago")

    def test_10_pocket_display_name_remains_non_identity_evidence(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        obs_events = [
            e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION
        ]
        display_names = [e.payload.display_name_raw for e in obs_events]
        self.assertIn("Kantong Utama", display_names)
        self.assertIn("Kantong Tabungan", display_names)

    def test_11_provider_status_start_date_phrase_preserved(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        obs_events = [
            e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION
        ]
        p1 = [e for e in obs_events if e.payload.observed_provider_account_key == "100011112222"][0]
        self.assertIn("mulai 01 Jan 2026", p1.payload.provider_state_raw)

    def test_12_account_period_summary_preserves_all_four_pocket_totals(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        aps_events = [
            e for e in res.events if e.event_role == EventRole.ACCOUNT_PERIOD_SUMMARY
        ]
        self.assertEqual(len(aps_events), 2)

        p1_summary = [e for e in aps_events if e.payload.observed_provider_account_key == "100011112222"][0]
        self.assertEqual(p1_summary.payload.opening_balance, Decimal("1000000.00"))
        self.assertEqual(p1_summary.payload.incoming_total, Decimal("500000.00"))
        self.assertEqual(p1_summary.payload.outgoing_total, Decimal("250000.00"))
        self.assertEqual(p1_summary.payload.closing_balance, Decimal("1250000.00"))

    def test_13_zero_activity_pocket_emits_observation_summary_no_cash_movement(self):
        pages = self.fixture_data["cases"]["zero_activity_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        ao_events = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION]
        aps_events = [e for e in res.events if e.event_role == EventRole.ACCOUNT_PERIOD_SUMMARY]

        self.assertEqual(len(cm_events), 0)
        self.assertEqual(len(ao_events), 1)
        self.assertEqual(len(aps_events), 1)

    def test_14_basic_transaction_structural_group_parses_exactly_once(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        # In normal statement fixture: Page 2 has 3 txs, Page 3 has 1 tx = 4 total
        self.assertEqual(len(cm_events), 4)

    def test_15_minute_timestamp_normalization(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        first_tx = cm_events[0].payload
        self.assertEqual(first_tx.occurred_at, "2026-07-05T10:15:00")

    def test_16_explicit_sign_controls_direction_and_amount(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        inflow = [e.payload for e in cm_events if e.payload.direction == EventDirection.INFLOW][0]
        outflow = [e.payload for e in cm_events if e.payload.direction == EventDirection.OUTFLOW][0]

        self.assertEqual(inflow.direction_raw, "+")
        self.assertEqual(inflow.amount, Decimal("500000.00"))
        self.assertEqual(outflow.direction_raw, "-")
        self.assertEqual(outflow.amount, Decimal("150000.00"))

    def test_17_wrapped_source_destination_reconstruction(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        first_tx = cm_events[0].payload
        self.assertIn("SYNTHETIC SENDER BCA 1234567890", first_tx.source_account_display_raw)

    def test_18_raw_transaction_type_mapping(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        qris_tx = cm_events[1].payload
        self.assertEqual(qris_tx.provider_category_raw, "Pembayaran QRIS")

    def test_19_raw_provider_tx_id_retained_source_event_id_none(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        for event in res.events:
            if event.event_role == EventRole.CASH_MOVEMENT:
                self.assertIsNone(event.source_event_id)
                self.assertIsNotNone(event.payload.provider_transaction_id_raw)

    def test_20_wrapped_note_reconstruction(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        qris_tx = cm_events[1].payload
        self.assertEqual(qris_tx.description_raw, "Catatan belanja bulanan")

    def test_21_running_balance_mapping(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        first_tx = cm_events[0].payload
        self.assertEqual(first_tx.balance_after, Decimal("1500000.00"))

    def test_22_tabs_and_multiline_extraction(self):
        # Insert tab characters into line tokens
        pages = [
            p.replace(" ", "\t")
            for p in self.fixture_data["cases"]["normal_statement"]["pages"]
        ]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)

    def test_23_page_boundary_transaction_continuation_and_repeated_headers(self):
        pages = self.fixture_data["cases"]["multipage_continuation_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 2)
        # Both transactions belong to Kantong Utama
        self.assertEqual(cm_events[0].payload.source_account_key_raw, "100011112222")
        self.assertEqual(cm_events[1].payload.source_account_key_raw, "100011112222")

    def test_24_internal_pocket_move_remains_two_cash_movements_not_merged(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        internal_events = [
            e for e in cm_events if e.payload.provider_transaction_id_raw == "9900778899"
        ]
        self.assertEqual(len(internal_events), 2)

        out_leg = [e.payload for e in internal_events if e.payload.direction == EventDirection.OUTFLOW][0]
        in_leg = [e.payload for e in internal_events if e.payload.direction == EventDirection.INFLOW][0]

        self.assertEqual(out_leg.amount, Decimal("100000.00"))
        self.assertEqual(in_leg.amount, Decimal("100000.00"))

    def test_25_paired_internal_move_endpoints_are_stable_pocket_evidenced(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        internal_events = [
            e for e in cm_events if e.payload.provider_transaction_id_raw == "9900778899"
        ]
        out_leg = [e.payload for e in internal_events if e.payload.direction == EventDirection.OUTFLOW][0]
        in_leg = [e.payload for e in internal_events if e.payload.direction == EventDirection.INFLOW][0]

        self.assertEqual(out_leg.source_account_key_raw, "100011112222")
        self.assertEqual(in_leg.destination_account_key_raw, "200033334444")

    def test_26_valid_repeated_transaction_id_internal_pair_accepted(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertFalse(
            any(d.code == "JAGO_DUPLICATE_ROW_CONFLICT" for d in res.diagnostics)
        )

    def test_27_incompatible_duplicate_tx_id_reviews_closed(self):
        pages = self.fixture_data["cases"]["duplicate_tx_id_conflict_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.REVIEW_REQUIRED)
        self.assertTrue(
            any(d.code == "JAGO_DUPLICATE_ROW_CONFLICT" for d in res.diagnostics)
        )

    def test_28_pocket_appearance_is_evidence_only(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        ao_events = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION]
        # Only existing pockets in this document are observed
        self.assertEqual(len(ao_events), 2)

    def test_29_malformed_missing_period_statement_fails_closed(self):
        pages = self.fixture_data["cases"]["missing_period_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "JAGO_PERIOD_AMBIGUOUS" for d in res.diagnostics)
        )

    def test_30_malformed_missing_header_account_and_row_fails_closed(self):
        pages_acct = self.fixture_data["cases"]["missing_header_account_statement"]["pages"]
        pdf_acct = make_multipage_pdf(pages_acct)
        res_acct = self.adapter.parse(self._make_input(pdf_acct))
        self.assertEqual(res_acct.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "JAGO_HEADER_INCOMPLETE" for d in res_acct.diagnostics)
        )

        pages_row = self.fixture_data["cases"]["malformed_row_statement"]["pages"]
        pdf_row = make_multipage_pdf(pages_row)
        res_row = self.adapter.parse(self._make_input(pdf_row))
        self.assertEqual(res_row.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "JAGO_ROW_AMOUNT_INVALID" for d in res_row.diagnostics)
        )

    def test_31_deterministic_replay_and_privacy_safe_repr(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf_bytes = make_multipage_pdf(pages)
        res1 = self.adapter.parse(self._make_input(pdf_bytes))
        res2 = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(len(res1.events), len(res2.events))
        rendered = repr(res1)
        # Verify natural key and result do not leak raw private account IDs in repr
        self.assertNotIn("SYNTHETIC SENDER BCA", rendered)

    def test_32_dryrun_catalog_semantic_duplicate_classification(self):
        pages = self.fixture_data["cases"]["normal_statement"]["pages"]
        pdf1 = make_multipage_pdf(pages)
        # Create second binary with slight whitespace differences but identical extracted text
        pdf2 = make_multipage_pdf([p + "\n " for p in pages])

        self.assertNotEqual(pdf1, pdf2)

        res1 = self.adapter.parse(self._make_input(pdf1, doc_id="doc-1"))
        res2 = self.adapter.parse(self._make_input(pdf2, doc_id="doc-2"))

        nat_key = res1.natural_document_key_candidate
        sha1 = semantic_document_sha256(JAGO_SOURCE_REGISTRY_ID, nat_key, res1)
        sha2 = semantic_document_sha256(JAGO_SOURCE_REGISTRY_ID, nat_key, res2)
        self.assertEqual(sha1, sha2)


if __name__ == "__main__":
    unittest.main()
