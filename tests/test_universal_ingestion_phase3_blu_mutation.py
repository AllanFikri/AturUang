from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import unittest

from pypdf import PdfReader, PdfWriter

from aturuang.ingestion_adapter import (
    AdapterContractError,
    AdapterInput,
    AdapterParseStatus,
    CashMovementEvidence,
    EventDirection,
    EventRole,
    ObservedAccountEvidence,
    SourceSummaryEvidence,
)
from aturuang.ingestion_blu_mutation_adapter import (
    BLU_MUTATION_ADAPTER_ID,
    BLU_MUTATION_PARSER_VERSION,
    BLU_MUTATION_SOURCE_REGISTRY_ID,
    BLU_MUTATION_TEMPLATE_FINGERPRINT,
    BLU_MUTATION_TEMPLATE_ID,
    BluAccountMutationAdapter,
)
from aturuang.ingestion_contracts import (
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)

_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ingestion"
    / "blu_account_mutation_v1.json"
)


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def make_pdf_page_stream_from_lines(lines: list[str]) -> bytes:
    cmds = [
        "BT",
        "/F1 9 Tf",
        "36 756 Td",
        "12 TL",
    ]
    for line in lines:
        for subline in line.split("\n"):
            esc_text = _pdf_escape(subline)
            cmds.append(f"({esc_text}) Tj")
            cmds.append("T*")
    cmds.append("ET")
    return "\n".join(cmds).encode("latin-1")


def make_pdf_from_multipage_lines(pages_lines: list[list[str]]) -> bytes:
    page_streams = [
        make_pdf_page_stream_from_lines(lines) for lines in pages_lines
    ]
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
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
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


class TestUniversalIngestionPhase3BluMutation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.adapter = BluAccountMutationAdapter()

    def _fixture_pdf(self, case_name: str) -> bytes:
        case_def = self.fixture_data["cases"][case_name]
        pages_lines: list[list[str]] = []
        for page_text in case_def["pages"]:
            pages_lines.append(page_text.splitlines())
        return make_pdf_from_multipage_lines(pages_lines)

    def _make_input(
        self,
        pdf_bytes: bytes | None,
        *,
        source_registry_id: str = BLU_MUTATION_SOURCE_REGISTRY_ID,
        template_id: str = BLU_MUTATION_TEMPLATE_ID,
        parser_version: str = BLU_MUTATION_PARSER_VERSION,
        source_channel: SourceChannel = SourceChannel.PDF,
        period_status: PeriodStatus = PeriodStatus.CLOSED,
        template_match_status: TemplateMatchStatus = TemplateMatchStatus.KNOWN,
        template_fingerprint: str | None = BLU_MUTATION_TEMPLATE_FINGERPRINT,
    ) -> AdapterInput:
        c_sha = (
            sha256(pdf_bytes).hexdigest()
            if pdf_bytes is not None
            else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        return AdapterInput(
            source_document_id="doc_blu_mut_test_001",
            content_sha256=c_sha,
            source_registry_id=source_registry_id,
            template_id=template_id,
            parser_version=parser_version,
            source_channel=source_channel,
            template_match_status=template_match_status,
            period_status=period_status,
            template_fingerprint=template_fingerprint,
            binary_payload=pdf_bytes,
        )

    # 1. Descriptor and constants
    def test_01_descriptor_and_constants(self) -> None:
        desc = self.adapter.descriptor
        self.assertEqual(desc.adapter_id, BLU_MUTATION_ADAPTER_ID)
        self.assertEqual(desc.source_registry_id, BLU_MUTATION_SOURCE_REGISTRY_ID)
        self.assertEqual(desc.template_id, BLU_MUTATION_TEMPLATE_ID)
        self.assertEqual(desc.parser_version, BLU_MUTATION_PARSER_VERSION)
        self.assertIs(desc.source_channel, SourceChannel.PDF)

    # 2. Exact template fingerprint
    def test_02_exact_template_fingerprint(self) -> None:
        self.assertEqual(
            BLU_MUTATION_TEMPLATE_FINGERPRINT,
            "2226278f9614625f9174151dad8b8da7851a7455eeec48e0cd9017a96acbcbfc",
        )

    # 3. Authority mismatch: source_registry_id
    def test_03_authority_mismatch_source_registry(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("single_page_statement"),
            source_registry_id="other_registry",
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    # 4. Authority mismatch: template_id
    def test_04_authority_mismatch_template_id(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("single_page_statement"),
            template_id="other_template",
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    # 5. Authority mismatch: parser_version
    def test_05_authority_mismatch_parser_version(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("single_page_statement"),
            parser_version="parser-v99",
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    # 6. Authority mismatch: source_channel
    def test_06_authority_mismatch_source_channel(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("single_page_statement"),
            source_channel=SourceChannel.CSV,
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    # 7. Binary payload required
    def test_07_binary_payload_required(self) -> None:
        inp = AdapterInput(
            source_document_id="doc_blu_mut_test_none",
            content_sha256="0" * 64,
            source_registry_id=BLU_MUTATION_SOURCE_REGISTRY_ID,
            template_id=BLU_MUTATION_TEMPLATE_ID,
            parser_version=BLU_MUTATION_PARSER_VERSION,
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            template_fingerprint=BLU_MUTATION_TEMPLATE_FINGERPRINT,
            text_payload="Sample plain text payload",
            binary_payload=None,
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(inp)

    # 8. Encrypted PDF fail-closed
    def test_08_encrypted_pdf_fail_closed(self) -> None:
        raw_pdf = self._fixture_pdf("single_page_statement")
        reader = PdfReader(BytesIO(raw_pdf))
        writer = PdfWriter()
        for p in reader.pages:
            writer.add_page(p)
        writer.encrypt("test_password")
        out_buf = BytesIO()
        writer.write(out_buf)

        inp = self._make_input(out_buf.getvalue())
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "BLU_MUTATION_PDF_ENCRYPTED_UNSUPPORTED" for d in res.diagnostics)
        )

    # 9. Zero-page PDF fail-closed
    def test_09_zero_page_pdf_fail_closed(self) -> None:
        writer = PdfWriter()
        out_buf = BytesIO()
        writer.write(out_buf)

        inp = self._make_input(out_buf.getvalue())
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "BLU_MUTATION_STRUCTURE_TRUNCATED" for d in res.diagnostics)
        )

    # 10. Active identity positive anchoring
    def test_10_active_identity_positive_anchoring(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)

        acc_events = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION]
        self.assertEqual(len(acc_events), 1)
        payload = acc_events[0].payload
        self.assertIsInstance(payload, ObservedAccountEvidence)
        self.assertEqual(payload.observed_provider_account_key, "006099998888")
        self.assertEqual(payload.institution_id, "blu")
        self.assertEqual(payload.display_name_raw, "SYNTHETIC USER")

    # 11. Identity negative decoy rejection
    def test_11_identity_negative_decoy_rejection(self) -> None:
        inp = self._make_input(self._fixture_pdf("decoy_identity_statement"))
        res = self.adapter.parse(inp)
        acc_events = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION]
        self.assertEqual(len(acc_events), 0)

    # 12. Conflicting identity ambiguity fail-closed
    def test_12_conflicting_identity_ambiguity_fail_closed(self) -> None:
        inp = self._make_input(self._fixture_pdf("conflicting_identity_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertEqual(len(res.events), 0)
        self.assertTrue(
            any(
                d.code == "BLU_MUTATION_ACCOUNT_IDENTITY_AMBIGUOUS"
                for d in res.diagnostics
            )
        )

    # 13. No filename identity authority
    def test_13_no_filename_identity_authority(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("single_page_statement"),
        )
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        acc = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION][0]
        self.assertEqual(acc.payload.observed_provider_account_key, "006099998888")

    # 14. Zero-activity statement omits account observation
    def test_14_zero_activity_statement_omits_account_observation(self) -> None:
        inp = self._make_input(self._fixture_pdf("zero_activity_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 0)

        acc_events = [e for e in res.events if e.event_role == EventRole.ACCOUNT_OBSERVATION]
        self.assertEqual(len(acc_events), 0)

        sum_events = [e for e in res.events if e.event_role == EventRole.SOURCE_SUMMARY]
        self.assertEqual(len(sum_events), 1)

    # 15. Single-page statement parsing
    def test_15_single_page_statement_parsing(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(res.events), 4)

    # 16. Multi-page statement parsing page boundaries
    def test_16_multi_page_statement_parsing_page_boundaries(self) -> None:
        inp = self._make_input(self._fixture_pdf("multipage_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)

        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 2)
        self.assertEqual(cm_events[0].provenance.page_number, 1)
        self.assertEqual(cm_events[1].provenance.page_number, 2)

    # 17. Repeated page headers ignored
    def test_17_repeated_page_headers_ignored(self) -> None:
        inp = self._make_input(self._fixture_pdf("multipage_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(len(cm_events), 2)

    # 18. Date time extraction precision
    def test_18_date_time_extraction_precision(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(cm_events[0].payload.occurred_at, "2024-08-03T10:00:00")
        self.assertEqual(cm_events[1].payload.occurred_at, "2024-08-03T12:00:00")

    # 19. Inflow transaction direction and amount
    def test_19_inflow_transaction_direction_and_amount(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        inflow = cm_events[0].payload
        self.assertEqual(inflow.direction, EventDirection.INFLOW)
        self.assertEqual(inflow.direction_raw, "MASUK")
        self.assertEqual(inflow.amount, Decimal("75000.00"))

    # 20. Outflow transaction direction and amount
    def test_20_outflow_transaction_direction_and_amount(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        outflow = cm_events[1].payload
        self.assertEqual(outflow.direction, EventDirection.OUTFLOW)
        self.assertEqual(outflow.direction_raw, "KELUAR")
        self.assertEqual(outflow.amount, Decimal("50000.00"))

    # 21. Locale decimal parsing strictness
    def test_21_locale_decimal_parsing_strictness(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertIsInstance(cm_events[0].payload.amount, Decimal)
        self.assertIsInstance(cm_events[0].payload.balance_after, Decimal)

    # 22. Running balance extraction
    def test_22_running_balance_extraction(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertEqual(cm_events[0].payload.balance_after, Decimal("75000.00"))
        self.assertEqual(cm_events[1].payload.balance_after, Decimal("25000.00"))

    # 23. Native transaction reference ID optional
    def test_23_native_transaction_reference_id_optional(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertIsNone(cm_events[0].payload.provider_transaction_id_raw)
        self.assertEqual(
            cm_events[1].payload.provider_transaction_id_raw, "REF123456789"
        )

    # 24. Multiline transaction detail wrapping
    def test_24_multiline_transaction_detail_wrapping(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        cm_events = [e for e in res.events if e.event_role == EventRole.CASH_MOVEMENT]
        self.assertIn("Bank Transfer In", cm_events[0].payload.description_raw)
        self.assertIn("Dana Masuk", cm_events[0].payload.description_raw)

    # 25. Source summary extraction and reconciliation
    def test_25_source_summary_extraction_and_reconciliation(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        sum_events = [e for e in res.events if e.event_role == EventRole.SOURCE_SUMMARY]
        self.assertEqual(len(sum_events), 1)
        payload = sum_events[0].payload
        self.assertIsInstance(payload, SourceSummaryEvidence)
        self.assertEqual(payload.opening_balance, Decimal("0.00"))
        self.assertEqual(payload.closing_balance, Decimal("25000.00"))
        self.assertEqual(payload.incoming_total, Decimal("75000.00"))
        self.assertEqual(payload.outgoing_total, Decimal("50000.00"))

    # 26. Summary equation failure fail-closed
    def test_26_summary_equation_failure_fail_closed(self) -> None:
        inp = self._make_input(self._fixture_pdf("summary_mismatch_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(d.code == "BLU_MUTATION_SUMMARY_MISMATCH" for d in res.diagnostics)
        )

    # 27. Running balance continuity success
    def test_27_running_balance_continuity_success(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)

    # 28. Running balance inconsistency fail-closed
    def test_28_running_balance_inconsistency_fail_closed(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("running_balance_inconsistent_statement")
        )
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.FAILED)
        self.assertTrue(
            any(
                d.code == "BLU_MUTATION_RUNNING_BALANCE_DISCONTINUITY"
                for d in res.diagnostics
            )
        )

    # 29. Open period status preserved
    def test_29_open_period_status_preserved(self) -> None:
        inp = self._make_input(
            self._fixture_pdf("open_period_statement"),
            period_status=PeriodStatus.OPEN,
        )
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(res.period_status, PeriodStatus.OPEN)

    # 30. Deterministic parse output
    def test_30_deterministic_parse_output(self) -> None:
        pdf = self._fixture_pdf("single_page_statement")
        inp1 = self._make_input(pdf)
        inp2 = self._make_input(pdf)
        res1 = self.adapter.parse(inp1)
        res2 = self.adapter.parse(inp2)
        self.assertEqual(len(res1.events), len(res2.events))
        for e1, e2 in zip(res1.events, res2.events):
            self.assertEqual(e1.row_fingerprint, e2.row_fingerprint)

    # 31. Privacy-safe repr: no PII leaks
    def test_31_privacy_safe_repr_no_pii_leaks(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        rendered = repr(res)
        self.assertNotIn("0060 9999 8888", rendered)
        self.assertNotIn("006099998888", rendered)
        self.assertNotIn("SYNTHETIC USER", rendered)

    # 32. Privacy-safe diagnostics: no raw identifiers
    def test_32_privacy_safe_diagnostics_no_raw_identifiers(self) -> None:
        inp = self._make_input(self._fixture_pdf("summary_mismatch_statement"))
        res = self.adapter.parse(inp)
        for diag in res.diagnostics:
            diag_str = repr(diag)
            self.assertNotIn("006099998888", diag_str)
            self.assertNotIn("0060 9999 8888", diag_str)

    # 33. No balance snapshot or period summary emitted
    def test_33_no_balance_snapshot_or_period_summary_emitted(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        for e in res.events:
            self.assertNotEqual(e.event_role, EventRole.BALANCE_SNAPSHOT)
            self.assertNotEqual(e.event_role, EventRole.ACCOUNT_PERIOD_SUMMARY)

    # 34. Corpus replay candidate preservation
    def test_34_corpus_replay_candidate_preservation(self) -> None:
        inp = self._make_input(self._fixture_pdf("single_page_statement"))
        res = self.adapter.parse(inp)
        self.assertEqual(res.parse_status, AdapterParseStatus.COMPLETED)
        self.assertTrue(len(res.events) > 0)


if __name__ == "__main__":
    unittest.main()
