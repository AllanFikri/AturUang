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
from aturuang.ingestion_orchestration import (
    semantic_document_canonical_json,
    semantic_document_sha256,
)
from aturuang.ingestion_seabank_adapter import (
    SEABANK_ADAPTER_ID,
    SEABANK_PARSER_VERSION,
    SEABANK_SOURCE_REGISTRY_ID,
    SEABANK_TEMPLATE_FINGERPRINT,
    SEABANK_TEMPLATE_ID,
    SeaBankMonthlyStatementAdapter,
)

_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ingestion"
    / "seabank_statement_v1.json"
)


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def make_pdf_page_stream(chunks: list[tuple[float, float, str]]) -> bytes:
    cmds: list[str] = []
    for x, y, text in chunks:
        esc_t = _pdf_escape(text)
        cmds.append(
            f"q 1 0 0 1 {x} {y} cm BT /F1 9 Tf 0 0 Td ({esc_t}) Tj ET Q"
        )
    return "\n".join(cmds).encode("latin-1")


def make_multipage_pdf(pages_chunks: list[list[tuple[float, float, str]]]) -> bytes:
    page_streams = [make_pdf_page_stream(chunks) for chunks in pages_chunks]
    total_pages = len(page_streams)

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
        (
            catalog_obj_num,
            f"<< /Type /Catalog /Pages {pages_obj_num} 0 R >>".encode("ascii"),
        )
    )
    objects.append(
        (
            pages_obj_num,
            f"<< /Type /Pages /Kids [{page_refs}] /Count {total_pages} >>".encode(
                "ascii"
            ),
        )
    )

    for i in range(total_pages):
        p_num = first_page_obj_num + i
        c_num = first_content_obj_num + i
        objects.append(
            (
                p_num,
                (
                    f"<< /Type /Page /Parent {pages_obj_num} 0 R "
                    f"/MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
                    f"/Contents {c_num} 0 R >>"
                ).encode("ascii"),
            )
        )

    objects.append(
        (
            font_obj_num,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        )
    )

    for i, s in enumerate(page_streams):
        c_num = first_content_obj_num + i
        objects.append(
            (
                c_num,
                f"<< /Length {len(s)} >>\nstream\n".encode("ascii")
                + s
                + b"\nendstream",
            )
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num, b in sorted(objects, key=lambda x: x[0]):
        offsets[num] = len(output)
        output.extend(f"{num} 0 obj\n".encode("ascii"))
        output.extend(b)
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


class TestUniversalIngestionPhase3SeaBank(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.adapter = SeaBankMonthlyStatementAdapter()

    def _make_input(
        self,
        payload_bytes: bytes,
        *,
        doc_id: str = "doc-seabank-test-01",
    ) -> AdapterInput:
        return AdapterInput(
            source_document_id=doc_id,
            content_sha256=sha256(payload_bytes).hexdigest(),
            source_registry_id=SEABANK_SOURCE_REGISTRY_ID,
            template_id=SEABANK_TEMPLATE_ID,
            parser_version=SEABANK_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            template_fingerprint=SEABANK_TEMPLATE_FINGERPRINT,
            binary_payload=payload_bytes,
        )

    def _make_synth_4col_chunks(self) -> list[tuple[float, float, str]]:
        return [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 JAN 2025 sampai 31 JAN 2025"),
            (
                48.5,
                543.8,
                "REKENING SALDO AWAL (IDR) TRANSAKSI KELUAR (IDR) TRANSAKSI MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 512.8, "TABUNGAN 0 50.000 100.000 50.000"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
            (48.5, 402.4, "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR)"),
            (48.5, 368.9, "03 JAN"),
            (173.6, 368.9, "Synthetic Sender Transfer"),
            (517.1, 368.9, "100.000"),
            (48.5, 328.4, "05 JAN"),
            (173.6, 328.4, "Synthetic Store Pembayaran"),
            (392.1, 328.4, "50.000"),
            (173.6, 211.3, "TABUNGAN - RINCIAN BUNGA & PAJAK"),
        ]

    def _make_synth_5col_chunks(self) -> list[tuple[float, float, str]]:
        return [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 APR 2025 sampai 30 APR 2025"),
            (
                48.5,
                543.8,
                "REKENING SALDO AWAL (IDR) TRANSAKSI KELUAR (IDR) TRANSAKSI MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 512.8, "TABUNGAN 50.000 20.000 70.000 100.000"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
            (
                48.5,
                402.4,
                "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 368.9, "02 APR"),
            (173.6, 368.9, "Synthetic TopUp Top Up - Pulsa"),
            (320.0, 368.9, "20.000"),
            (520.0, 368.9, "30.000"),
            (48.5, 328.4, "05 APR"),
            (173.6, 328.4, "Synthetic Client Transfer"),
            (440.0, 328.4, "70.000"),
            (520.0, 328.4, "100.000"),
            (173.6, 211.3, "TABUNGAN - RINCIAN BUNGA & PAJAK"),
        ]

    def test_01_exact_descriptor_template_fingerprint_and_channel(self):
        desc = self.adapter.descriptor
        self.assertEqual(desc.adapter_id, SEABANK_ADAPTER_ID)
        self.assertEqual(desc.source_registry_id, SEABANK_SOURCE_REGISTRY_ID)
        self.assertEqual(desc.template_id, SEABANK_TEMPLATE_ID)
        self.assertEqual(desc.parser_version, SEABANK_PARSER_VERSION)
        self.assertIs(desc.source_channel, SourceChannel.PDF)
        self.assertEqual(
            SEABANK_TEMPLATE_FINGERPRINT,
            "fc7add5e7b3a321692fda3196b25083e3620bfcf15f4eb63313fdc3de4a99656",
        )

    def test_02_binary_payload_only_adapter_contract(self):
        inp = AdapterInput(
            source_document_id="doc-text-invalid",
            content_sha256=sha256(b"abc").hexdigest(),
            source_registry_id=SEABANK_SOURCE_REGISTRY_ID,
            template_id=SEABANK_TEMPLATE_ID,
            parser_version=SEABANK_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            text_payload="Some text",
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    def test_03_sanitized_normal_statement_completes_with_expected_roles(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(res.events), 4)

        roles = [e.event_role for e in res.events]
        self.assertIn(EventRole.SOURCE_SUMMARY, roles)
        self.assertIn(EventRole.ACCOUNT_OBSERVATION, roles)
        self.assertEqual(roles.count(EventRole.CASH_MOVEMENT), 2)

    def test_04_unsupported_encrypted_pdf_fails_closed(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        enc_bytes = make_encrypted_pdf(pdf_bytes)

        res = self.adapter.parse(self._make_input(enc_bytes))
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(
                d.code == "SEABANK_PDF_ENCRYPTED_UNSUPPORTED"
                for d in res.diagnostics
            )
        )

    def test_05_exact_closed_statement_period_extraction(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.period_start, "2025-01-01")
        self.assertEqual(res.period_end, "2025-01-31")
        self.assertEqual(res.period_status, PeriodStatus.CLOSED)

    def test_06_provider_anchored_stable_account_identity_positive(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        ao = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION][0]
        self.assertEqual(ao.payload.observed_provider_account_key, "901236971867")
        self.assertEqual(ao.payload.institution_id, "seabank")

    def test_07_identity_negative_decoy_rejection(self):
        chunks = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "Telepon 1500 130"),
            (40.0, 680.0, "S/N S01-250131BVENXANI"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 JAN 2025 sampai 31 JAN 2025"),
            (48.5, 512.8, "TABUNGAN 0 0 0 0"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
        ]
        pdf_bytes = make_multipage_pdf([chunks])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "SEABANK_HEADER_INCOMPLETE" for d in res.diagnostics)
        )

    def test_08_identity_conflict_or_ambiguity_fails_closed(self):
        chunks = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 690.0, "NO. REKENING SEABANK: 901299999999"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 JAN 2025 sampai 31 JAN 2025"),
            (48.5, 512.8, "TABUNGAN 0 0 0 0"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
        ]
        pdf_bytes = make_multipage_pdf([chunks])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "SEABANK_HEADER_INCOMPLETE" for d in res.diagnostics)
        )

    def test_09_natural_key_determinism(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        expected_hash = sha256("901236971867".encode("utf-8")).hexdigest()
        expected_key = f"seabank_statement:{expected_hash}:2025-01"
        self.assertEqual(res.natural_document_key_candidate, expected_key)

    def test_10_document_level_source_summary_mapping(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        summary_event = [
            e for e in res.events if e.event_role == EventRole.SOURCE_SUMMARY
        ][0]
        payload = summary_event.payload
        self.assertEqual(payload.currency, "IDR")
        self.assertEqual(payload.opening_balance, Decimal("0"))
        self.assertEqual(payload.outgoing_total, Decimal("50000"))
        self.assertEqual(payload.incoming_total, Decimal("100000"))
        self.assertEqual(payload.closing_balance, Decimal("50000"))

    def test_11_opening_credit_debit_closing_decimal_typing_and_equation(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_5col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        summary_event = [
            e for e in res.events if e.event_role == EventRole.SOURCE_SUMMARY
        ][0]
        p = summary_event.payload
        self.assertIsInstance(p.opening_balance, Decimal)
        self.assertIsInstance(p.outgoing_total, Decimal)
        self.assertIsInstance(p.incoming_total, Decimal)
        self.assertIsInstance(p.closing_balance, Decimal)
        self.assertEqual(
            p.opening_balance - p.outgoing_total + p.incoming_total,
            p.closing_balance,
        )

    def test_12_account_observation_mapping(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        ao = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION][0]
        self.assertEqual(ao.payload.observed_provider_account_key, "901236971867")
        self.assertEqual(ao.payload.display_name_raw, "SYNTHETIC CUSTOMER")

    def test_13_basic_transaction_row_parsed_exactly_once(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 2)

    def test_14_exact_source_date_normalization(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(cm_events[0].payload.occurred_at, "2025-01-03T00:00:00")
        self.assertEqual(cm_events[1].payload.occurred_at, "2025-01-05T00:00:00")

    def test_15_source_direction_evidence_controls_inflow_outflow(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(cm_events[0].payload.direction, EventDirection.INFLOW)
        self.assertEqual(cm_events[0].payload.direction_raw, "MASUK")
        self.assertEqual(cm_events[1].payload.direction, EventDirection.OUTFLOW)
        self.assertEqual(cm_events[1].payload.direction_raw, "KELUAR")

    def test_16_absolute_decimal_amount_mapping(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(cm_events[0].payload.amount, Decimal("100000"))
        self.assertEqual(cm_events[1].payload.amount, Decimal("50000"))

    def test_17_raw_description_and_counterparty_reconstruction(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(
            cm_events[0].payload.counterparty_raw,
            "Synthetic Sender Transfer",
        )
        self.assertEqual(
            cm_events[1].payload.counterparty_raw,
            "Synthetic Store Pembayaran",
        )

    def test_18_raw_provider_tx_id_policy_and_source_event_id_none(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        for e in cm_events:
            self.assertIsNone(e.source_event_id)
            self.assertIsNone(e.payload.provider_transaction_id_raw)

    def test_19_running_balance_mapping_and_none_boundary(self):
        # 4-col has None running balance
        pdf_4col = make_multipage_pdf([self._make_synth_4col_chunks()])
        res_4col = self.adapter.parse(self._make_input(pdf_4col))
        cm_4col = [e for e in res_4col.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertIsNone(cm_4col[0].payload.balance_after)

        # 5-col has Decimal running balance
        pdf_5col = make_multipage_pdf([self._make_synth_5col_chunks()])
        res_5col = self.adapter.parse(self._make_input(pdf_5col))
        cm_5col = [e for e in res_5col.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(cm_5col[0].payload.balance_after, Decimal("30000"))
        self.assertEqual(cm_5col[1].payload.balance_after, Decimal("100000"))

    def test_20_wrapped_multiline_row_handling(self):
        chunks = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 JAN 2025 sampai 31 JAN 2025"),
            (
                48.5,
                543.8,
                "REKENING SALDO AWAL (IDR) TRANSAKSI KELUAR (IDR) TRANSAKSI MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 512.8, "TABUNGAN 0 0 100.000 100.000"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
            (48.5, 402.4, "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR)"),
            (48.5, 368.9, "03 JAN"),
            (173.6, 380.0, "LONG COUNTERPARTY NAME LINE 1"),
            (173.6, 368.9, "LONG COUNTERPARTY NAME LINE 2"),
            (173.6, 355.0, "Transfer"),
            (517.1, 368.9, "100.000"),
            (173.6, 211.3, "TABUNGAN - RINCIAN BUNGA & PAJAK"),
        ]
        pdf_bytes = make_multipage_pdf([chunks])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT][0]
        self.assertIn("LONG COUNTERPARTY NAME", cm.payload.counterparty_raw)

    def test_21_page_boundary_continuation_and_repeated_headers(self):
        p1 = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 JAN 2025 sampai 31 JAN 2025"),
            (
                48.5,
                543.8,
                "REKENING SALDO AWAL (IDR) TRANSAKSI KELUAR (IDR) TRANSAKSI MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 512.8, "TABUNGAN 0 50.000 50.000 0"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
            (48.5, 402.4, "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR)"),
            (48.5, 368.9, "03 JAN"),
            (173.6, 368.9, "Page 1 Tx Transfer"),
            (517.1, 368.9, "50.000"),
        ]
        p2 = [
            (459.8, 801.6, "REKENING KORAN"),
            (48.5, 726.3, "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR)"),
            (48.5, 680.0, "05 JAN"),
            (173.6, 680.0, "Page 2 Tx Pembayaran"),
            (392.1, 680.0, "50.000"),
            (173.6, 400.0, "TABUNGAN - RINCIAN BUNGA & PAJAK"),
        ]
        pdf_bytes = make_multipage_pdf([p1, p2])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 2)

    def test_22_zero_activity_statement_accepted_without_cash_movement(self):
        chunks = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 MEI 2025 sampai 31 MEI 2025"),
            (
                48.5,
                543.8,
                "REKENING SALDO AWAL (IDR) TRANSAKSI KELUAR (IDR) TRANSAKSI MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 512.8, "TABUNGAN 100.000 0 0 100.000"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
            (
                48.5,
                402.4,
                "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (173.6, 350.0, "TIDAK ADA TRANSAKSI"),
            (173.6, 211.3, "TABUNGAN - RINCIAN BUNGA & PAJAK"),
        ]
        pdf_bytes = make_multipage_pdf([chunks])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 0)
        self.assertEqual(len(res.events), 2)

    def test_23_four_column_layout_family_a_support(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm), 2)
        self.assertIsNone(cm[0].payload.balance_after)

    def test_24_five_column_layout_family_b_support(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_5col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm), 2)
        self.assertIsNotNone(cm[0].payload.balance_after)

    def test_25_malformed_amount_fails_closed(self):
        chunks = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "01 JAN 2025 sampai 31 JAN 2025"),
            (
                48.5,
                543.8,
                "REKENING SALDO AWAL (IDR) TRANSAKSI KELUAR (IDR) TRANSAKSI MASUK (IDR) SALDO AKHIR (IDR)",
            ),
            (48.5, 512.8, "TABUNGAN 0 50.000 100.000 50.000"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
            (48.5, 402.4, "TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR)"),
            (48.5, 368.9, "03 JAN"),
            (173.6, 368.9, "Synthetic Sender Transfer"),
            (517.1, 368.9, "INVALID_AMOUNT"),
            (173.6, 211.3, "TABUNGAN - RINCIAN BUNGA & PAJAK"),
        ]
        pdf_bytes = make_multipage_pdf([chunks])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(
                d.code in ("SEABANK_ROW_AMOUNT_INVALID", "SEABANK_SUMMARY_MISMATCH")
                for d in res.diagnostics
            )
        )

    def test_26_malformed_ambiguous_period_fails_closed(self):
        chunks = [
            (459.8, 801.6, "REKENING KORAN"),
            (40.0, 704.8, "NO. REKENING SEABANK: 901236971867"),
            (40.0, 723.8, "SYNTHETIC CUSTOMER"),
            (224.0, 590.7, "RINGKASAN REKENING"),
            (221.0, 574.2, "PERIODE TIDAK VALID"),
            (48.5, 512.8, "TABUNGAN 0 0 0 0"),
            (189.2, 433.3, "TABUNGAN - RINCIAN TRANSAKSI"),
        ]
        pdf_bytes = make_multipage_pdf([chunks])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "SEABANK_PERIOD_AMBIGUOUS" for d in res.diagnostics)
        )

    def test_27_truncated_pdf_stream_fails_closed(self):
        corrupt_bytes = b"%PDF-1.4\ncorrupted incomplete stream"
        res = self.adapter.parse(self._make_input(corrupt_bytes))

        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "SEABANK_STRUCTURE_TRUNCATED" for d in res.diagnostics)
        )

    def test_28_deterministic_replay_and_privacy_safe_repr(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res1 = self.adapter.parse(self._make_input(pdf_bytes))
        res2 = self.adapter.parse(self._make_input(pdf_bytes))

        self.assertEqual(len(res1.events), len(res2.events))
        rendered = repr(res1)
        self.assertNotIn("SYNTHETIC SENDER", rendered)

    def test_29_row_fingerprint_determinism_and_content_coverage(self):
        pdf_bytes = make_multipage_pdf([self._make_synth_4col_chunks()])
        res = self.adapter.parse(self._make_input(pdf_bytes))

        fps = [e.row_fingerprint for e in res.events]
        self.assertEqual(len(fps), len(set(fps)))
        for fp in fps:
            self.assertEqual(len(fp), 64)

    def test_30_dryrun_semantic_identity_classification(self):
        pdf1 = make_multipage_pdf([self._make_synth_4col_chunks()])
        pdf2 = make_multipage_pdf([self._make_synth_4col_chunks()])

        res1 = self.adapter.parse(self._make_input(pdf1, doc_id="doc-1"))
        res2 = self.adapter.parse(self._make_input(pdf2, doc_id="doc-2"))

        nat_key = res1.natural_document_key_candidate
        sha1 = semantic_document_sha256(SEABANK_SOURCE_REGISTRY_ID, nat_key, res1)
        sha2 = semantic_document_sha256(SEABANK_SOURCE_REGISTRY_ID, nat_key, res2)
        self.assertEqual(sha1, sha2)


if __name__ == "__main__":
    unittest.main()
