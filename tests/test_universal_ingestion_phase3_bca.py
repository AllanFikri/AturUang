from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import unittest

from pypdf import PdfReader, PdfWriter

from aturuang.ingestion_adapter import (
    AdapterContractError,
    AdapterInput,
    AdapterParseStatus,
    EventDirection,
    EventRole,
)
from aturuang.ingestion_bca_adapter import (
    BCA_DESCRIPTOR,
    BCA_TEMPLATE_FINGERPRINT,
    BCAMonthlyStatementAdapter,
)
from aturuang.ingestion_contracts import (
    PeriodStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
)
from aturuang.ingestion_orchestration import (
    AdapterCatalog,
    DryRunDisposition,
    dry_run_artifact,
)
from aturuang.ingestion_registry_service import TemplateResolution


_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ingestion"
    / "bca_statement_v1.json"
)


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def make_text_pdf(text: str) -> bytes:
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
    stream = "\n".join(commands).encode("latin-1")

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
        (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        ),
    ]

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]

    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(
        f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    )
    output.extend(b"0000000000 65535 f \n")

    for offset in offsets[1:]:
        output.extend(
            f"{offset:010d} 00000 n \n".encode("ascii")
        )

    output.extend(
        (
            "trailer\n"
            f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_offset}\n"
            "%%EOF\n"
        ).encode("ascii")
    )

    return bytes(output)


def encrypt_pdf(
    payload: bytes,
    *,
    user_password: str,
) -> bytes:
    reader = PdfReader(BytesIO(payload), strict=False)
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt(
        user_password=user_password,
        owner_password="owner-synthetic",
    )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def content_sha(payload: bytes) -> str:
    return sha256(payload).hexdigest()


class FakeRegistry:
    def lookup_exact_documents(self, content_sha256):
        return ()

    def lookup_natural_documents(
        self,
        source_registry_id,
        natural_document_key,
    ):
        return ()

    def source_capability(self, source_registry_id):
        return {
            "source_registry_id": source_registry_id,
            "channel": "PDF",
        }

    def template_authority(
        self,
        *,
        source_registry_id,
        template_fingerprint,
    ):
        return TemplateResolution(
            status=TemplateMatchStatus.KNOWN,
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
        )


class TestUniversalIngestionPhase3BCA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(
            _FIXTURE_PATH.read_text(encoding="utf-8")
        )
        cls.cases = cls.fixture["cases"]

    def setUp(self):
        self.adapter = BCAMonthlyStatementAdapter()

    def case_text(self, name):
        return self.cases[name]["text"]

    def source_for_payload(self, payload):
        return AdapterInput(
            source_document_id="doc-bca-synthetic",
            content_sha256=content_sha(payload),
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.UNKNOWN,
            template_fingerprint=BCA_TEMPLATE_FINGERPRINT,
            binary_payload=payload,
        )

    def parse_case(self, name):
        payload = make_text_pdf(self.case_text(name))
        return self.adapter.parse(self.source_for_payload(payload))

    @staticmethod
    def cash_events(result):
        return [
            event
            for event in result.events
            if event.event_role is EventRole.CASH_MOVEMENT
        ]

    @staticmethod
    def summary_events(result):
        return [
            event
            for event in result.events
            if event.event_role is EventRole.SOURCE_SUMMARY
        ]

    @staticmethod
    def account_events(result):
        return [
            event
            for event in result.events
            if event.event_role is EventRole.ACCOUNT_OBSERVATION
        ]

    def test_01_exact_adapter_descriptor_and_pdf_channel(self):
        self.assertEqual(
            BCA_DESCRIPTOR.adapter_id,
            "bca-monthly-statement-v1",
        )
        self.assertEqual(
            BCA_DESCRIPTOR.source_registry_id,
            "bca_statement",
        )
        self.assertEqual(
            BCA_DESCRIPTOR.template_id,
            "bca_monthly_statement_v1",
        )
        self.assertEqual(
            BCA_DESCRIPTOR.parser_version,
            "parser-v1",
        )
        self.assertIs(
            BCA_DESCRIPTOR.source_channel,
            SourceChannel.PDF,
        )

    def test_02_adapter_input_binary_payload_contract_only(self):
        source = AdapterInput(
            source_document_id="doc-text",
            content_sha256="a" * 64,
            source_registry_id="bca_statement",
            template_id="bca_monthly_statement_v1",
            parser_version="parser-v1",
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.UNKNOWN,
            template_fingerprint=BCA_TEMPLATE_FINGERPRINT,
            text_payload="synthetic",
        )
        with self.assertRaises(AdapterContractError):
            self.adapter.parse(source)

    def test_03_sanitized_normal_statement_parses_completed(self):
        result = self.parse_case("normal_statement")
        self.assertIs(result.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(self.cash_events(result)), 2)
        self.assertEqual(len(self.summary_events(result)), 1)

        with self.subTest("split_multiline_real_layout"):
            split_res = self.parse_case("split_multiline_real_layout")
            self.assertIs(split_res.parse_status, AdapterParseStatus.COMPLETED)
            self.assertEqual(len(self.cash_events(split_res)), 3)
            self.assertEqual(len(self.summary_events(split_res)), 1)

        with self.subTest("multi_section_poket_layout"):
            poket_res = self.parse_case("multi_section_poket_layout")
            self.assertIs(poket_res.parse_status, AdapterParseStatus.COMPLETED)
            self.assertEqual(len(self.cash_events(poket_res)), 2)
            self.assertEqual(len(self.summary_events(poket_res)), 1)

        with self.subTest("date_prefixed_non_row_prevention"):
            non_row_res = self.parse_case("date_prefixed_non_row_prevention")
            self.assertIs(non_row_res.parse_status, AdapterParseStatus.COMPLETED)
            self.assertEqual(len(self.cash_events(non_row_res)), 1)
            self.assertEqual(len(self.summary_events(non_row_res)), 1)

    def test_04_encrypted_readable_sanitized_pdf_parses(self):
        plain = make_text_pdf(
            self.case_text("encrypted_readable_statement")
        )
        encrypted = encrypt_pdf(plain, user_password="")
        result = self.adapter.parse(
            self.source_for_payload(encrypted)
        )
        self.assertIs(result.parse_status, AdapterParseStatus.COMPLETED)
        self.assertEqual(len(self.cash_events(result)), 2)

    def test_05_locked_and_corrupt_pdf_fail_closed(self):
        plain = make_text_pdf(self.case_text("normal_statement"))
        locked = encrypt_pdf(plain, user_password="locked-synthetic")

        with self.subTest("locked"):
            result = self.adapter.parse(
                self.source_for_payload(locked)
            )
            self.assertIs(
                result.parse_status,
                AdapterParseStatus.FAILED,
            )
            self.assertFalse(result.events)

        with self.subTest("corrupt"):
            corrupt = b"%PDF-1.4\nnot-a-valid-pdf"
            result = self.adapter.parse(
                self.source_for_payload(corrupt)
            )
            self.assertIs(
                result.parse_status,
                AdapterParseStatus.FAILED,
            )
            self.assertFalse(result.events)

    def test_06_statement_period_and_closed_status(self):
        result = self.parse_case("normal_statement")
        self.assertIs(result.period_status, PeriodStatus.CLOSED)
        self.assertEqual(result.period_start, "2026-07-01")
        self.assertEqual(result.period_end, "2026-07-31")

    def test_07_natural_key_deterministic_and_private_account_absent(self):
        first = self.parse_case("normal_statement")
        second = self.parse_case("normal_statement")
        key = first.natural_document_key_candidate

        self.assertEqual(key, second.natural_document_key_candidate)
        self.assertRegex(
            key,
            r"^bca_statement:[0-9a-f]{64}:2026-07$",
        )
        self.assertNotIn("9911", key)
        self.assertNotIn("9911-2233-44", repr(first))

    def test_08_opening_balance_evidence(self):
        result = self.parse_case("normal_statement")
        summary = self.summary_events(result)[0].payload
        self.assertEqual(str(summary.opening_balance), "1000000.00")

        with self.subTest("split_multiline_opening_balance"):
            res = self.parse_case("split_multiline_real_layout")
            summ = self.summary_events(res)[0].payload
            self.assertEqual(str(summ.opening_balance), "1000000.00")

    def test_09_closing_balance_evidence(self):
        result = self.parse_case("normal_statement")
        summary = self.summary_events(result)[0].payload
        self.assertEqual(str(summary.closing_balance), "1150000.00")

        with self.subTest("split_multiline_closing_balance"):
            res = self.parse_case("split_multiline_real_layout")
            summ = self.summary_events(res)[0].payload
            self.assertEqual(str(summ.closing_balance), "1025000.00")

    def test_10_debit_row_maps_to_outflow(self):
        result = self.parse_case("debit_row")
        payload = self.cash_events(result)[0].payload
        self.assertIs(payload.direction, EventDirection.OUTFLOW)
        self.assertEqual(payload.direction_raw, "DB")

    def test_11_credit_row_maps_to_inflow(self):
        result = self.parse_case("credit_row")
        payload = self.cash_events(result)[0].payload
        self.assertIs(payload.direction, EventDirection.INFLOW)
        self.assertEqual(payload.direction_raw, "CR")

    def test_12_posting_date_is_preserved(self):
        result = self.parse_case("debit_row")
        payload = self.cash_events(result)[0].payload
        self.assertEqual(payload.posted_at, "2026-07-03")

    def test_13_distinct_effective_date_is_preserved(self):
        result = self.parse_case("effective_date_distinct")
        payload = self.cash_events(result)[0].payload
        self.assertEqual(payload.posted_at, "2026-07-01")
        self.assertEqual(payload.occurred_at, "2026-06-30")

    def test_14_no_effective_date_does_not_invent_occurred_at(self):
        result = self.parse_case("no_effective_date")
        payload = self.cash_events(result)[0].payload
        self.assertIsNone(payload.occurred_at)

    def test_15_multiline_description_continuation(self):
        result = self.parse_case("multiline_description")
        payload = self.cash_events(result)[0].payload
        self.assertIn(
            "LANJUTAN DESKRIPSI SANITASI",
            payload.description_raw,
        )

        with self.subTest("split_multiline_real_rows"):
            res = self.parse_case("split_multiline_real_layout")
            events = self.cash_events(res)
            self.assertIn("NAMA PENERIMA SYNTHETIC", events[0].payload.description_raw)
            self.assertIn("TOKO SYNTHETIC", events[1].payload.description_raw)
            self.assertIn("PLN PREPAID", events[2].payload.description_raw)

    def test_16_multiline_reference_continuation(self):
        result = self.parse_case("multiline_reference")
        payload = self.cash_events(result)[0].payload
        self.assertEqual(
            payload.reference_raw,
            "TEST-REF-PART-A PART-B",
        )

    def test_17_running_balance_parsed_when_printed(self):
        result = self.parse_case("optional_running_balance")
        payload = self.cash_events(result)[0].payload
        self.assertEqual(str(payload.balance_after), "920000.00")

    def test_18_missing_running_balance_is_allowed(self):
        result = self.parse_case("no_running_balance")
        payload = self.cash_events(result)[0].payload
        self.assertIsNone(payload.balance_after)
        self.assertIs(result.parse_status, AdapterParseStatus.COMPLETED)

    def test_19_ambiguous_direction_requires_review(self):
        result = self.parse_case("ambiguous_direction")
        self.assertIs(
            result.parse_status,
            AdapterParseStatus.REVIEW_REQUIRED,
        )
        self.assertIn(
            "BCA_ROW_DIRECTION_AMBIGUOUS",
            {item.code for item in result.diagnostics},
        )

    def test_20_reference_is_not_sole_row_identity(self):
        result = self.parse_case("duplicate_reference_different_row")
        cash = self.cash_events(result)
        self.assertEqual(len(cash), 2)
        self.assertEqual(
            cash[0].payload.reference_raw,
            cash[1].payload.reference_raw,
        )
        self.assertNotEqual(
            cash[0].row_fingerprint,
            cash[1].row_fingerprint,
        )
        self.assertIsNone(cash[0].source_event_id)
        self.assertIsNone(cash[1].source_event_id)

    def test_21_separate_fee_row_remains_separate_event(self):
        result = self.parse_case("separate_fee_row")
        cash = self.cash_events(result)
        self.assertEqual(len(cash), 2)
        self.assertEqual(
            [str(item.payload.amount) for item in cash],
            ["100000.00", "2500.00"],
        )

    def test_22_poket_initial_funding_lifecycle_evidence(self):
        result = self.parse_case("poket_initial_funding")
        cash = self.cash_events(result)[0]
        accounts = self.account_events(result)
        self.assertEqual(
            cash.payload.event_hint,
            "BCA_POKET_INITIAL_FUNDING",
        )
        self.assertEqual(len(accounts), 1)

    def test_23_poket_top_up_lifecycle_evidence(self):
        result = self.parse_case("poket_top_up")
        self.assertEqual(
            self.cash_events(result)[0].payload.event_hint,
            "BCA_POKET_TOP_UP",
        )
        self.assertEqual(len(self.account_events(result)), 1)

    def test_24_poket_move_lifecycle_evidence(self):
        result = self.parse_case("poket_move")
        self.assertEqual(
            self.cash_events(result)[0].payload.event_hint,
            "BCA_POKET_MOVE",
        )
        self.assertEqual(len(self.account_events(result)), 1)

    def test_25_poket_scheduled_transfer_lifecycle_evidence(self):
        result = self.parse_case("poket_scheduled_transfer")
        self.assertEqual(
            self.cash_events(result)[0].payload.event_hint,
            "BCA_POKET_SCHEDULED_TRANSFER",
        )
        self.assertEqual(len(self.account_events(result)), 1)

    def test_26_missing_stable_poket_key_does_not_fabricate_identity(self):
        result = self.parse_case("poket_missing_stable_key")
        self.assertIs(
            result.parse_status,
            AdapterParseStatus.REVIEW_REQUIRED,
        )
        self.assertFalse(self.account_events(result))
        self.assertIn(
            "BCA_POKET_IDENTITY_UNSTABLE",
            {item.code for item in result.diagnostics},
        )

    def test_27_malformed_truncated_statement_is_privacy_safe(self):
        result = self.parse_case("malformed_or_truncated")
        self.assertIs(
            result.parse_status,
            AdapterParseStatus.REVIEW_REQUIRED,
        )
        self.assertIn(
            "BCA_STRUCTURE_TRUNCATED",
            {item.code for item in result.diagnostics},
        )
        combined = " ".join(item.message for item in result.diagnostics)
        self.assertNotIn("9911-2233-44", combined)
        self.assertNotIn("BARIS TERPOTONG", combined)

        with self.subTest("no_false_truncated_on_non_row_date"):
            non_row_res = self.parse_case("date_prefixed_non_row_prevention")
            self.assertIs(non_row_res.parse_status, AdapterParseStatus.COMPLETED)
            self.assertNotIn("BCA_STRUCTURE_TRUNCATED", {d.code for d in non_row_res.diagnostics})

    def test_28_repeated_parse_is_deterministic_and_idempotent(self):
        payload = make_text_pdf(self.case_text("deterministic_reparse"))
        source = self.source_for_payload(payload)
        first = self.adapter.parse(source)
        second = self.adapter.parse(source)
        self.assertEqual(first, second)

    def test_29_exact_catalog_and_orchestration_dry_run_integration(self):
        payload = make_text_pdf(self.case_text("normal_statement"))
        artifact = DiscoveredArtifact(
            content_sha256=content_sha(payload),
            size_bytes=len(payload),
            extension=".pdf",
            occurrences=(
                ArtifactOccurrence(
                    source_locator="fixture/bca.pdf",
                    extension=".pdf",
                ),
            ),
        )

        result = dry_run_artifact(
            "ignored-root",
            artifact,
            registry=FakeRegistry(),
            adapter_catalog=AdapterCatalog((self.adapter,)),
            payload_reader=lambda *args, **kwargs: payload,
        )

        self.assertIs(
            result.disposition,
            DryRunDisposition.READY_FOR_STAGING,
        )
        self.assertEqual(
            result.selected_adapter,
            BCA_DESCRIPTOR,
        )
        self.assertIsNotNone(result.semantic_sha256)

    def test_30_privacy_repr_and_static_authority_boundary(self):
        result = self.parse_case("normal_statement")
        rendered = repr(result)

        for private_value in (
            "9911-2233-44",
            "TEST-REF-A",
            "TRANSFER UJI ALPHA",
        ):
            self.assertNotIn(private_value, rendered)

        module_text = (
            Path(__file__).parents[1]
            / "aturuang"
            / "ingestion_bca_adapter.py"
        ).read_text(encoding="utf-8")

        forbidden = (
            "BCAEmailParser",
            "BaseProviderParser",
            "mutate_account_balance",
            "reconcile_account_balance",
            "create_or_get_import_batch",
            "register_source_document",
            "requests.",
            "urllib.",
            "pytesseract",
            "selenium",
        )

        for token in forbidden:
            self.assertNotIn(token, module_text)

        test_names = [
            name
            for name in dir(type(self))
            if re.fullmatch(r"test_\d{2}_.+", name)
        ]
        self.assertEqual(len(test_names), 30)


if __name__ == "__main__":
    unittest.main()
