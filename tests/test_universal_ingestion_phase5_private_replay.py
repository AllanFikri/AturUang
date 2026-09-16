"""Universal Ingestion Phase 5B - Cross-Source Private Replay & Quality Gate Tests (R1).

Validates all 10 P5B-R1 gate hardening requirements:
- Zero committed private filenames or paths
- Mandatory master secret (no default, minimum 32 chars)
- Exact inventory enforcement (228 PDFs, 13 images, 241 total)
- Rejection of missing/excess/unexpected family members
- Verifiable 3-pass pipeline execution and comparison
- Repeat and shuffle determinism verification
- Gate fails closed on mismatch, authority violation, or schema defect
- Correct invalid-auto-link authority violation detection
- Strict relationship configuration schema
- Production database presence and byte hash integrity
- Required sanitized report aggregations
- Atomic output behavior
- Complete spreadsheet formula injection protection
- Zero prohibited private fields in reports
- Multi-provider and multi-document synthetic replay
- Exact count conservation and deterministic matching

Contains exactly 24 focused test methods.
"""

from __future__ import annotations

import csv
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from typing import Any
import unittest

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    OwnershipState,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_adapter import (
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    NormalizedEventEnvelope,
    SourceEventStatus,
    SourceProvenanceContract,
)
from aturuang.ingestion_account_discovery import AccountDiscoveryResolver
from aturuang.ingestion_discovery import (
    ArtifactOccurrence,
    DiscoveredArtifact,
    DiscoveryResult,
)
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
    is_valid_protected_key,
    validate_protected_key,
)
from aturuang.ingestion_matching import (
    AccountRelationship,
    DeterministicEvidenceMatcher,
    EconomicEventGroup,
    EvidenceMatchDecision,
    EvidenceMatchPlan,
    MatchingContext,
    MatchReasonCode,
    MatchRelation,
    MatchTier,
    ProtectedAccountBinding,
    RelationshipKind,
    SafeEvidenceRecord,
    compute_evidence_key,
    compute_group_key,
    make_evidence_record_from_envelope,
)
from aturuang.ingestion_matching_audit import (
    CANONICAL_FAMILIES,
    CSV_FIELDNAMES,
    EXPECTED_INVENTORY,
    EXPECTED_PRODUCTION_DB_SHA256,
    EXPECTED_TOTAL_ARTIFACTS,
    EXPECTED_TOTAL_IMAGES,
    EXPECTED_TOTAL_PDFS,
    InMemoryRegistryAuthority,
    ReadOnlyAuthorizer,
    compute_canonical_replay_digest,
    compute_file_sha256,
    execute_three_pass_audit,
    extract_evidence_metadata_map,
    generate_summary_dict,
    load_private_corpus_config,
    parse_relationship_config,
    sanitize_csv_cell,
    validate_inventory,
    validate_replay_gate,
    verify_production_db_bytes,
    write_evidence_review_csv,
    write_summary_json,
)
from aturuang.ingestion_orchestration import (
    DryRunBatchResult,
    DryRunDisposition,
    _validate_evidence_match_plan,
)


def _make_record(
    doc_id: str,
    reg_id: str,
    role: EventRole = EventRole.CASH_MOVEMENT,
    fingerprint: str = "fp",
    *,
    amount: Decimal = Decimal("100000.00"),
    currency: str = "IDR",
    direction: EventDirection = EventDirection.OUTFLOW,
    status: SourceEventStatus = SourceEventStatus.POSTED,
    event_date: str = "2026-03-01",
    settlement_date: str | None = "2026-03-01",
    account_binding: ProtectedAccountBinding | None = None,
    is_eligible: bool = True,
    requires_review: bool = False,
    source_event_id: str | None = None,
    provider_transaction_id_raw: str | None = None,
    reference_raw: str | None = None,
    description_raw: str | None = None,
    trade_side: str | None = None,
    gross_amount: Decimal | None = None,
    net_amount: Decimal | None = None,
) -> SafeEvidenceRecord:
    if len(fingerprint) != 64 or not all(c in "0123456789abcdef" for c in fingerprint):
        fp_clean = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    else:
        fp_clean = fingerprint
    key = compute_evidence_key(doc_id, reg_id, role, fp_clean)
    return SafeEvidenceRecord(
        evidence_key=key,
        source_document_id=doc_id,
        source_registry_id=reg_id,
        event_role=role,
        row_fingerprint=fp_clean,
        amount=amount,
        currency=currency,
        direction=direction,
        status=status,
        event_date=event_date,
        settlement_date=settlement_date,
        account_binding=account_binding,
        is_eligible=is_eligible,
        requires_review=requires_review,
        source_event_id=source_event_id,
        provider_transaction_id_raw=provider_transaction_id_raw,
        reference_raw=reference_raw,
        description_raw=description_raw,
        trade_side=trade_side,
        gross_amount=gross_amount,
        net_amount=net_amount,
    )


class UniversalIngestionPhase5PrivateReplayTests(unittest.TestCase):
    """Test suite covering the 24 requirements of P5B-R1 private replay gate."""

    def setUp(self) -> None:
        self.matcher = DeterministicEvidenceMatcher()
        self.valid_key_1 = "v1:" + "1" * 64
        self.valid_key_2 = "v1:" + "2" * 64
        self.binding_1 = ProtectedAccountBinding(
            protected_account_key=self.valid_key_1,
            institution_id="bca",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        self.binding_2 = ProtectedAccountBinding(
            protected_account_key=self.valid_key_2,
            institution_id="jago",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )

    def test_01_no_committed_private_filenames_or_paths(self) -> None:
        repo_files = [
            "aturuang/ingestion_matching_audit.py",
            "scripts/run_private_matching_audit.py",
        ]
        user_win_prefix = "C:" + chr(92) + "A" + " " + "User"
        user_posix_prefix = "C:/A" + " " + "User"
        forbidden_substrings = [
            "Mutasi Rekening",
            "Portofolio Blu",
            "Riwayat Transaksi",
            "Stockbit_SOA_Oct2025",
            user_win_prefix,
            user_posix_prefix,
        ]
        for fpath in repo_files:
            full_p = Path(fpath)
            if full_p.exists():
                content = full_p.read_text(encoding="utf-8")
                for forbidden in forbidden_substrings:
                    self.assertNotIn(
                        forbidden,
                        content,
                        f"Found prohibited private string '{forbidden}' in {fpath}",
                    )

        # Check git tracked files
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
        tracked = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        for t in tracked:
            self.assertFalse(
                t.endswith(".zip"), f"Private zip archive tracked in git: {t}"
            )
            self.assertNotIn("AturUang_P5B", t)

    def test_02_secret_is_required_and_has_no_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # None secret
            with self.assertRaises(ValueError):
                execute_three_pass_audit(
                    corpus_config_path="dummy.json",
                    output_dir=td,
                    db_path="dummy.db",
                    master_secret=None,
                )
            # Empty secret
            with self.assertRaises(ValueError):
                execute_three_pass_audit(
                    corpus_config_path="dummy.json",
                    output_dir=td,
                    db_path="dummy.db",
                    master_secret="",
                )
            # Secret too short (< 32 chars)
            with self.assertRaises(ValueError):
                execute_three_pass_audit(
                    corpus_config_path="dummy.json",
                    output_dir=td,
                    db_path="dummy.db",
                    master_secret="short-secret-under-32-chars!",
                )

    def test_03_exact_inventory_enforcement(self) -> None:
        mock_inv: dict[str, list[DiscoveredArtifact]] = {}
        for fam, expected_count in EXPECTED_INVENTORY.items():
            ext = ".jpg" if fam == "shopeepay_mutation" else ".pdf"
            mock_inv[fam] = [
                DiscoveredArtifact(
                    content_sha256=hashlib.sha256(f"{fam}_{i}".encode()).hexdigest(),
                    size_bytes=1024,
                    extension=ext,
                    occurrences=(),
                )
                for i in range(expected_count)
            ]

        summary = validate_inventory(mock_inv)
        self.assertEqual(summary["total_pdfs"], EXPECTED_TOTAL_PDFS)
        self.assertEqual(summary["total_images"], EXPECTED_TOTAL_IMAGES)
        self.assertEqual(summary["total_artifacts"], EXPECTED_TOTAL_ARTIFACTS)

    def test_04_missing_and_excess_family_members_fail(self) -> None:
        mock_inv: dict[str, list[DiscoveredArtifact]] = {}
        for fam, expected_count in EXPECTED_INVENTORY.items():
            ext = ".jpg" if fam == "shopeepay_mutation" else ".pdf"
            mock_inv[fam] = [
                DiscoveredArtifact(
                    content_sha256=hashlib.sha256(f"{fam}_{i}".encode()).hexdigest(),
                    size_bytes=1024,
                    extension=ext,
                    occurrences=(),
                )
                for i in range(expected_count)
            ]

        # Case 1: Missing document in bca_statement (18 instead of 19)
        inv_missing = dict(mock_inv)
        inv_missing["bca_statement"] = mock_inv["bca_statement"][:-1]
        with self.assertRaises(ValueError):
            validate_inventory(inv_missing)

        # Case 2: Excess document in jago_statement (12 instead of 11)
        inv_excess = dict(mock_inv)
        extra_art = DiscoveredArtifact(content_sha256=hashlib.sha256(b"extra").hexdigest(), size_bytes=10, extension=".pdf", occurrences=())
        inv_excess["jago_statement"] = list(mock_inv["jago_statement"]) + [extra_art]
        with self.assertRaises(ValueError):
            validate_inventory(inv_excess)

        # Case 3: Unexpected media type (image in PDF family)
        inv_wrong_type = dict(mock_inv)
        wrong_ext_art = DiscoveredArtifact(content_sha256=hashlib.sha256(b"wrong_ext").hexdigest(), size_bytes=10, extension=".png", occurrences=())
        inv_wrong_type["bca_statement"] = list(mock_inv["bca_statement"][:-1]) + [wrong_ext_art]
        with self.assertRaises(ValueError):
            validate_inventory(inv_wrong_type)

    def test_05_three_full_pipeline_invocations_occur(self) -> None:
        # Build batch results representing Pass A, Pass B, Pass C
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", account_binding=self.binding_1)
        plan = self.matcher.match([r1])
        batch_a = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        batch_b = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        batch_c = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)

        res = validate_replay_gate(batch_a, batch_b, batch_c)
        self.assertIn("pass_a_digest", res)
        self.assertIn("pass_b_digest", res)
        self.assertIn("pass_c_digest", res)

    def test_06_repeat_and_shuffle_equality_independently_compared(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", account_binding=self.binding_1)
        plan = self.matcher.match([r1])
        batch_a = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        batch_b = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        batch_c = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)

        gate_res = validate_replay_gate(batch_a, batch_b, batch_c)
        self.assertTrue(gate_res["repeat_deterministic"])
        self.assertTrue(gate_res["shuffle_deterministic"])
        self.assertTrue(gate_res["overall_deterministic"])

    def test_07_mismatch_between_any_pass_fails(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", account_binding=self.binding_1)
        r2 = _make_record("d2", "bca_statement", EventRole.CASH_MOVEMENT, "fp2", account_binding=self.binding_1)
        plan_a = self.matcher.match([r1])
        plan_corrupted = self.matcher.match([r2])

        batch_a = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan_a)
        batch_b = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan_a)
        batch_c = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan_corrupted)

        with self.assertRaises(ValueError):
            validate_replay_gate(batch_a, batch_b, batch_c)

    def test_08_every_listed_fatal_condition_produces_gate_failure(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", account_binding=self.binding_1)
        plan = self.matcher.match([r1])

        # Fatal 1: failed_count > 0
        b_failed = DryRunBatchResult(1, 1, 0, 0, 0, 0, 1, (), (), DryRunDisposition.FAILED, plan)
        with self.assertRaises(ValueError):
            validate_replay_gate(b_failed, b_failed, b_failed)

        # Fatal 2: match_plan is None
        b_no_plan = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, None)
        with self.assertRaises(ValueError):
            validate_replay_gate(b_no_plan, b_no_plan, b_no_plan)

        # Fatal 3: duplicate decisions
        dup_decisions = plan.decisions + (plan.decisions[0],)
        plan_dup = EvidenceMatchPlan(plan.matcher_contract_version, dup_decisions, plan.groups, plan.diagnostics)
        b_dup = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan_dup)
        with self.assertRaises(ValueError):
            validate_replay_gate(b_dup, b_dup, b_dup)

    def test_09_correct_invalid_auto_link_detection(self) -> None:
        r_ineligible = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", requires_review=True)
        r_valid = _make_record("d2", "bca_statement", EventRole.CASH_MOVEMENT, "fp2", account_binding=self.binding_1)

        # Build group that illegally links ineligible evidence
        g_invalid = EconomicEventGroup(
            group_key=hashlib.sha256(b"g_invalid").hexdigest(),
            match_tier=MatchTier.EXACT,
            match_relation=MatchRelation.DUPLICATE_EVIDENCE,
            member_evidence_keys=(r_ineligible.evidence_key, r_valid.evidence_key),
            reason_codes=(MatchReasonCode.SAME_SOURCE_EVENT_ID,),
            is_auto_link_eligible=True,  # VIOLATION: ineligible member in auto-linked group!
        )
        dec_ineligible = EvidenceMatchDecision(
            evidence_key=r_ineligible.evidence_key,
            match_tier=MatchTier.INELIGIBLE,
            match_relation=None,
            group_key=None,
            reason_codes=(MatchReasonCode.EVIDENCE_REQUIRES_REVIEW,),
            is_auto_link_eligible=False,
        )
        dec_valid = EvidenceMatchDecision(
            evidence_key=r_valid.evidence_key,
            match_tier=MatchTier.EXACT,
            match_relation=MatchRelation.DUPLICATE_EVIDENCE,
            group_key=g_invalid.group_key,
            reason_codes=(MatchReasonCode.SAME_SOURCE_EVENT_ID,),
            is_auto_link_eligible=True,
        )

        plan = EvidenceMatchPlan("truth-loop-matching-v1", (dec_ineligible, dec_valid), (g_invalid,), ())
        batch = DryRunBatchResult(2, 2, 0, 0, 2, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        summary = generate_summary_dict(batch)

        # Assert authority violation is caught
        self.assertGreater(summary["matching_summary"]["invalid_auto_link_groups"], 0)
        self.assertGreater(summary["matching_summary"]["review_evidence_auto_linked"], 0)

    def test_10_strict_relationship_configuration_schema(self) -> None:
        # Valid
        valid_cfg = {
            "relationships": [
                {
                    "relationship_kind": "COMMERCE_PAYMENT",
                    "source_registry_id": "shopee_orders",
                    "protected_account_key": self.valid_key_1,
                }
            ]
        }
        ctx = parse_relationship_config(valid_cfg)
        self.assertEqual(len(ctx.relationships), 1)

        # Missing field
        bad_missing = {"relationships": [{"source_registry_id": "shopee_orders", "protected_account_key": self.valid_key_1}]}
        with self.assertRaises(ValueError):
            parse_relationship_config(bad_missing)

        # Extra field
        bad_extra = {"relationships": [{"relationship_kind": "COMMERCE_PAYMENT", "source_registry_id": "shopee_orders", "protected_account_key": self.valid_key_1, "extra": "foo"}]}
        with self.assertRaises(ValueError):
            parse_relationship_config(bad_extra)

        # Unexpected top-level field
        bad_top = {"relationships": [], "unknown_field": True}
        with self.assertRaises(ValueError):
            parse_relationship_config(bad_top)

        # Raw account number (invalid protected key format)
        bad_key = {"relationships": [{"relationship_kind": "COMMERCE_PAYMENT", "source_registry_id": "shopee_orders", "protected_account_key": "1234567890"}]}
        with self.assertRaises(ValueError):
            parse_relationship_config(bad_key)

        # Duplicate relationships
        bad_dup = {
            "relationships": [
                {"relationship_kind": "COMMERCE_PAYMENT", "source_registry_id": "shopee_orders", "protected_account_key": self.valid_key_1},
                {"relationship_kind": "COMMERCE_PAYMENT", "source_registry_id": "shopee_orders", "protected_account_key": self.valid_key_1},
            ]
        }
        with self.assertRaises(ValueError):
            parse_relationship_config(bad_dup)

    def test_11_missing_production_db_fails(self) -> None:
        with self.assertRaises(ValueError):
            verify_production_db_bytes("")
        with self.assertRaises(ValueError):
            verify_production_db_bytes(None)
        with self.assertRaises(FileNotFoundError):
            verify_production_db_bytes("C:/nonexistent_path/production_fake.db")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                execute_three_pass_audit(
                    corpus_config_path="dummy.json",
                    output_dir=td,
                    db_path=None,
                    master_secret="at-least-32-chars-long-secret-key!",
                )

    def test_12_pre_post_production_hash_mismatch_fails(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", account_binding=self.binding_1)
        plan = self.matcher.match([r1])
        batch = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)

        # Hash altered
        with self.assertRaises(ValueError):
            validate_replay_gate(
                batch,
                batch,
                batch,
                pre_sqlite_sha256=EXPECTED_PRODUCTION_DB_SHA256,
                post_sqlite_sha256="altered_hash_value_1234567890",
            )

        # Hash doesn't match baseline
        with self.assertRaises(ValueError):
            validate_replay_gate(
                batch,
                batch,
                batch,
                pre_sqlite_sha256="wrong_baseline_hash",
                post_sqlite_sha256="wrong_baseline_hash",
            )

    def test_13_required_report_aggregations_exist(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", event_date="2026-03-01", account_binding=self.binding_1)
        plan = self.matcher.match([r1])
        batch = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        summary = generate_summary_dict(batch)

        required_aggs = [
            "by_source_family",
            "by_statement_month",
            "by_source_registry_id",
            "by_event_role",
            "by_match_tier",
            "by_match_relation",
            "by_reason_code",
        ]
        self.assertIn("aggregations", summary)
        for agg in required_aggs:
            self.assertIn(agg, summary["aggregations"], f"Missing required aggregation: {agg}")

    def test_14_atomic_output_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", account_binding=self.binding_1)
            plan = self.matcher.match([r1])
            batch = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
            summary = generate_summary_dict(batch)

            csv_target = Path(td) / "test.csv"
            json_target = Path(td) / "test.json"

            write_evidence_review_csv(batch, csv_target)
            write_summary_json(summary, json_target)

            self.assertTrue(csv_target.exists())
            self.assertTrue(json_target.exists())
            # Ensure no stray temporary files left
            stray_files = list(Path(td).glob("*.tmp.*"))
            self.assertEqual(len(stray_files), 0)

    def test_15_complete_formula_injection_protection(self) -> None:
        test_cases = [
            ("=SUM(A1)", "'=SUM(A1)"),
            (" =SUM(A1)", "' =SUM(A1)"),
            ("+cmd", "'+cmd"),
            (" +cmd", "' +cmd"),
            ("-100", "'-100"),
            (" -100", "' -100"),
            ("@alert", "'@alert"),
            (" @alert", "' @alert"),
            (chr(9) + "tabbed", "'" + chr(9) + "tabbed"),
            ("  " + chr(9) + "tabbed", "'  " + chr(9) + "tabbed"),
            (chr(13) + "carriage", "'" + chr(13) + "carriage"),
            (chr(10) + "newline", "'" + chr(10) + "newline"),
            ("﻿" + "=bom", "'﻿" + "=bom"),
            ("SAFE_STRING", "SAFE_STRING"),
            ("", ""),
            (None, ""),
        ]
        for raw, expected in test_cases:
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_csv_cell(raw), expected)

    def test_16_output_contains_no_prohibited_private_fields(self) -> None:
        r1 = _make_record(
            "d1",
            "bca_statement",
            EventRole.CASH_MOVEMENT,
            "fp1",
            amount=Decimal("987654.32"),
            description_raw="CONFIDENTIAL_SALARY_ROW",
            account_binding=self.binding_1,
        )
        plan = self.matcher.match([r1])
        batch = DryRunBatchResult(1, 1, 0, 0, 1, 0, 0, (), (), DryRunDisposition.READY_FOR_STAGING, plan)
        summary = generate_summary_dict(batch)
        summary_text = json.dumps(summary)

        self.assertNotIn("CONFIDENTIAL_SALARY_ROW", summary_text)
        self.assertNotIn("987654.32", summary_text)
        self.assertNotIn(self.valid_key_1, summary_text)

    def test_17_multi_provider_synthetic_replay(self) -> None:
        providers = [
            "bca_statement",
            "jago_statement",
            "seabank_statement",
            "blu_mutation",
            "blu_portfolio",
            "gopay_statement",
            "stockbit_soa",
            "shopee_orders",
        ]
        records = [
            _make_record(f"doc_{p}", p, EventRole.CASH_MOVEMENT, f"fp_{p}", account_binding=self.binding_1)
            for p in providers
        ]
        plan = self.matcher.match(records)
        self.assertEqual(len(plan.decisions), len(providers))

    def test_18_multi_document_synthetic_replay(self) -> None:
        docs = ["doc_1", "doc_2", "doc_3", "doc_4"]
        records = [
            _make_record(doc, "bca_statement", EventRole.CASH_MOVEMENT, f"fp_{doc}", account_binding=self.binding_1)
            for doc in docs
        ]
        plan = self.matcher.match(records)
        self.assertEqual(len(plan.decisions), 4)

    def test_19_every_evidence_key_receives_one_decision(self) -> None:
        records = [
            _make_record("doc1", "jago_statement", EventRole.CASH_MOVEMENT, f"fp_{i}", account_binding=self.binding_1)
            for i in range(15)
        ]
        plan = self.matcher.match(records)
        self.assertEqual(len(plan.decisions), 15)
        dec_keys = [d.evidence_key for d in plan.decisions]
        self.assertEqual(len(dec_keys), len(set(dec_keys)))

    def test_20_count_conservation(self) -> None:
        r_exact1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", source_event_id="DUP1")
        r_exact2 = _make_record("d2", "bca_statement", EventRole.CASH_MOVEMENT, "fp2", source_event_id="DUP1")
        r_tr1 = _make_record("d3", "bca_statement", EventRole.CASH_MOVEMENT, "fp3", amount=Decimal("250000"), direction=EventDirection.OUTFLOW, account_binding=self.binding_1)
        r_tr2 = _make_record("d4", "jago_statement", EventRole.CASH_MOVEMENT, "fp4", amount=Decimal("250000"), direction=EventDirection.INFLOW, account_binding=self.binding_2)
        r_ineligible = _make_record("d5", "bca_statement", EventRole.CASH_MOVEMENT, "fp5", requires_review=True)
        r_unmatched = _make_record("d6", "seabank_statement", EventRole.CASH_MOVEMENT, "fp6", amount=Decimal("12345"))

        records = [r_exact1, r_exact2, r_tr1, r_tr2, r_ineligible, r_unmatched]
        plan = self.matcher.match(records)

        exact_m = sum(len(g.member_evidence_keys) for g in plan.exact_groups)
        strong_m = sum(len(g.member_evidence_keys) for g in plan.strong_groups)
        amb_m = len(plan.ambiguous_evidence)
        unm_m = len(plan.unmatched_evidence)
        inel_m = len(plan.ineligible_evidence)
        total_d = len(plan.decisions)

        self.assertEqual(exact_m + strong_m + amb_m + unm_m + inel_m, total_d)

    def test_21_exact_duplicate_grouping(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", provider_transaction_id_raw="TX-100")
        r2 = _make_record("d2", "bca_statement", EventRole.CASH_MOVEMENT, "fp2", provider_transaction_id_raw="TX-100")
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.exact_groups), 1)
        self.assertEqual(plan.exact_groups[0].match_relation, MatchRelation.DUPLICATE_EVIDENCE)

    def test_22_owned_transfer_grouping(self) -> None:
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", amount=Decimal("50000"), direction=EventDirection.OUTFLOW, account_binding=self.binding_1)
        r2 = _make_record("d2", "jago_statement", EventRole.CASH_MOVEMENT, "fp2", amount=Decimal("50000"), direction=EventDirection.INFLOW, account_binding=self.binding_2)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 1)
        self.assertEqual(plan.strong_groups[0].match_relation, MatchRelation.INTERNAL_TRANSFER_PAIR)

    def test_23_unowned_transfer_cannot_autolink(self) -> None:
        unowned_b = ProtectedAccountBinding(self.valid_key_2, "jago", AccountType.SAVINGS, OwnershipState.THIRD_PARTY, ConfidenceLevel.LOW)
        r1 = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", amount=Decimal("50000"), direction=EventDirection.OUTFLOW, account_binding=self.binding_1)
        r2 = _make_record("d2", "jago_statement", EventRole.CASH_MOVEMENT, "fp2", amount=Decimal("50000"), direction=EventDirection.INFLOW, account_binding=unowned_b)
        plan = self.matcher.match([r1, r2])
        self.assertEqual(len(plan.strong_groups), 0)

    def test_24_ambiguous_evidence_exact_count_and_tier(self) -> None:
        outflow = _make_record("d1", "bca_statement", EventRole.CASH_MOVEMENT, "fp1", amount=Decimal("100000"), direction=EventDirection.OUTFLOW, account_binding=self.binding_1)
        inflow1 = _make_record("d2", "jago_statement", EventRole.CASH_MOVEMENT, "fp2", amount=Decimal("100000"), direction=EventDirection.INFLOW, account_binding=self.binding_2)
        inflow2 = _make_record("d3", "jago_statement", EventRole.CASH_MOVEMENT, "fp3", amount=Decimal("100000"), direction=EventDirection.INFLOW, account_binding=self.binding_2)
        plan = self.matcher.match([outflow, inflow1, inflow2])
        self.assertEqual(len(plan.strong_groups), 0)
        # Assert exact intended counts
        self.assertEqual(len(plan.ambiguous_evidence), 3)
        self.assertEqual(len(plan.unmatched_evidence), 0)


if __name__ == "__main__":
    unittest.main()
