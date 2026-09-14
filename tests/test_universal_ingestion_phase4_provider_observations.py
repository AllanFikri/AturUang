"""
Tests for Universal Ingestion Phase 4 Provider Account Observations.
Verifies account-observation gap closure across all 8 provider adapters
while strictly enforcing identity privacy, fail-closed boundaries,
and isolation invariants.

Contains exactly 48 focused test methods (6 per provider family x 8 providers).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import unittest
from typing import Any

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    LifecycleState,
    OwnershipState,
    PeriodStatus,
    SourceChannel,
)
from aturuang.ingestion_adapter import (
    AdapterDescriptor,
    AdapterParseStatus,
    AdapterResult,
    CashMovementEvidence,
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    NormalizedEventEnvelope,
    ObservedAccountEvidence,
    SafeDiagnostic,
    SourceEventStatus,
    SourceProvenanceContract,
)
from aturuang.ingestion_account_discovery import (
    AccountDiscoveryObservation,
    AccountDiscoveryResolver,
    AccountResolution,
    ExistingAccountState,
)
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
)

import aturuang.ingestion_jago_adapter as jago_mod
import aturuang.ingestion_seabank_adapter as seabank_mod
import aturuang.ingestion_blu_mutation_adapter as blu_mod
import aturuang.ingestion_gopay_adapter as gopay_mod
import aturuang.ingestion_bca_adapter as bca_mod
import aturuang.ingestion_stockbit_adapter as stockbit_mod
import aturuang.ingestion_shopeepay_adapter as shopeepay_mod
import aturuang.ingestion_shopee_orders_adapter as shopee_orders_mod

TEST_SECRET = "synthetic-test-secret-at-least-32-chars-long-strictly-mocked!"


def _make_envelope(
    source_document_id: str,
    source_registry_id: str,
    template_id: str,
    parser_version: str,
    source_channel: SourceChannel,
    event_role: EventRole,
    payload: Any,
    source_event_id: str = "evt-1",
) -> NormalizedEventEnvelope:
    return NormalizedEventEnvelope(
        source_document_id=source_document_id,
        source_registry_id=source_registry_id,
        template_id=template_id,
        parser_version=parser_version,
        source_channel=source_channel,
        event_role=event_role,
        source_event_id=source_event_id,
        row_fingerprint="0" * 64,
        evidence_quality=ConfidenceLevel.HIGH,
        parse_confidence=ConfidenceLevel.HIGH,
        provenance=SourceProvenanceContract(
            source_document_id=source_document_id,
            raw_locator="locator-1",
        ),
        payload=payload,
    )


class TestUniversalIngestionPhase4ProviderObservations(unittest.TestCase):
    """
    Focused test suite for Provider Account Observations (Phase 4.3).
    Contains exactly 48 test methods.
    """

    def setUp(self) -> None:
        self.protector = AccountIdentityProtector(secret=TEST_SECRET)
        self.resolver = AccountDiscoveryResolver(protector=self.protector)

    def assert_sentinel_absent(self, obj: Any, sentinel: str, path: str = "root") -> None:
        repr_str = repr(obj)
        self.assertNotIn(sentinel, repr_str, f"Sentinel found in repr at {path}")
        if isinstance(obj, str):
            self.assertNotIn(sentinel, obj, f"Sentinel found in string value at {path}")
        elif isinstance(obj, (list, tuple)):
            for idx, item in enumerate(obj):
                self.assert_sentinel_absent(item, sentinel, f"{path}[{idx}]")
        elif isinstance(obj, dict):
            for k, v in obj.items():
                self.assert_sentinel_absent(k, sentinel, f"{path}.key({k})")
                self.assert_sentinel_absent(v, sentinel, f"{path}[{k}]")
        elif hasattr(obj, "__dict__"):
            for k, v in vars(obj).items():
                self.assert_sentinel_absent(v, sentinel, f"{path}.{k}")

    # =========================================================================
    # JAGO (Tests 01-06)
    # =========================================================================

    def test_01_jago_root_observation_extraction_from_valid_statement(self) -> None:
        adapter = jago_mod.JagoMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-jago-1",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="1000-0001-0001",
                parent_observed_key="1000-0001-0001",
                display_name_raw="Kantong Utama",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-jago-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="jago-2026-03",
            events=(obs_event,),
        )
        obs_list = jago_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        root = obs_list[0]
        self.assertEqual(root.institution_id, "jago")
        self.assertEqual(root.source_registry_id, adapter.descriptor.source_registry_id)
        self.assertEqual(root.raw_account_key, "1000-0001-0001")
        self.assertEqual(root.account_type, AccountType.TRANSACTIONAL)
        self.assertIsNone(root.parent_raw_account_key)
        self.assertEqual(root.display_name_safe, "Kantong Utama")
        self.assertEqual(root.ownership_state, OwnershipState.OWNED)
        self.assertEqual(root.ownership_confidence, ConfidenceLevel.HIGH)

    def test_02_jago_pocket_observations_linked_to_root(self) -> None:
        adapter = jago_mod.JagoMonthlyStatementAdapter()
        events = (
            _make_envelope(
                source_document_id="doc-jago-2",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="1000-0001-0001",
                    parent_observed_key="1000-0001-0001",
                    display_name_raw="Kantong Utama",
                ),
                source_event_id="evt-root",
            ),
            _make_envelope(
                source_document_id="doc-jago-2",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="1000-0001-0002",
                    parent_observed_key="1000-0001-0001",
                    display_name_raw="Kantong Tabungan",
                ),
                source_event_id="evt-pkt",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-jago-2",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="jago-2026-03-2",
            events=events,
        )
        obs_list = jago_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 2)
        root, child = obs_list[0], obs_list[1]
        self.assertEqual(child.raw_account_key, "1000-0001-0002")
        self.assertEqual(child.parent_raw_account_key, root.raw_account_key)
        self.assertEqual(child.account_type, AccountType.SAVINGS)
        self.assertEqual(child.parent_account_type, AccountType.TRANSACTIONAL)

    def test_03_jago_neutral_pocket_display_names(self) -> None:
        adapter = jago_mod.JagoMonthlyStatementAdapter()
        events = (
            _make_envelope(
                source_document_id="doc-1",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="1000-0001-0001",
                    parent_observed_key="1000-0001-0001",
                    display_name_raw="Kantong Utama",
                ),
                source_event_id="evt-1",
            ),
            _make_envelope(
                source_document_id="doc-1",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="1000-0001-0002",
                    parent_observed_key="1000-0001-0001",
                    display_name_raw="Kantong Rahasia Si Budi",
                ),
                source_event_id="evt-2",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="cand-1",
            events=events,
        )
        obs_list = jago_mod.extract_account_observations(res)
        child = obs_list[1]
        self.assertEqual(child.display_name_safe, "Jago Kantong")
        self.assertNotIn("Budi", child.display_name_safe)
        self.assertNotIn("Rahasia", child.display_name_safe)

    def test_04_jago_pocket_balance_tracking_when_present(self) -> None:
        adapter = jago_mod.JagoMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-jago-bal",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="1000-0001-0001",
                parent_observed_key="1000-0001-0001",
                display_name_raw="Kantong Utama",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-jago-bal",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-04-01",
            period_end="2026-04-30",
            natural_document_key_candidate="jago-2026-04",
            events=(obs_event,),
        )
        obs_list = jago_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assertEqual(obs_list[0].effective_date, "2026-04-01")

    def test_05_jago_fail_closed_when_root_account_missing(self) -> None:
        adapter = jago_mod.JagoMonthlyStatementAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-fail",
            parse_status=AdapterParseStatus.FAILED,
            period_status=PeriodStatus.UNKNOWN,
            period_start=None,
            period_end=None,
            natural_document_key_candidate=None,
            events=(),
        )
        obs_list = jago_mod.extract_account_observations(res)
        self.assertEqual(obs_list, [])

    def test_06_jago_zero_leakage_check_on_personal_identities(self) -> None:
        sentinel_name = "SENTINEL_NASABAH_BUDI_SANTOSO"
        sentinel_nik = "3171012345678901"
        sentinel_addr = "SENTINEL_JL_SUDIRMAN_JAKARTA"

        adapter = jago_mod.JagoMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-sentinel",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="1000-0001-0001",
                parent_observed_key="1000-0001-0001",
                display_name_raw=f"Kantong {sentinel_name}",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sentinel",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="jago-sentinel",
            events=(obs_event,),
        )
        obs_list = jago_mod.extract_account_observations(res)
        for obs in obs_list:
            self.assert_sentinel_absent(obs, sentinel_name)
            self.assert_sentinel_absent(obs, sentinel_nik)
            self.assert_sentinel_absent(obs, sentinel_addr)

    # =========================================================================
    # SEABANK (Tests 07-12)
    # =========================================================================

    def test_07_seabank_standalone_savings_observation_from_valid_statement(self) -> None:
        adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-sea-1",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="901234567890",
                display_name_raw="SeaBank Account",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sea-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="seabank-2026-03",
            events=(obs_event,),
        )
        obs_list = seabank_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.institution_id, "seabank")
        self.assertEqual(obs.account_type, AccountType.SAVINGS)
        self.assertIsNone(obs.parent_raw_account_key)

    def test_08_seabank_authoritative_account_number_extraction(self) -> None:
        adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-sea-2",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="901234567890",
                display_name_raw="SeaBank Account",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sea-2",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="seabank-2026-03",
            events=(obs_event,),
        )
        obs_list = seabank_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].raw_account_key, "901234567890")

    def test_09_seabank_neutral_display_name(self) -> None:
        adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-sea-3",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="901234567890",
                display_name_raw="SeaBank Account",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sea-3",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="seabank-2026-03",
            events=(obs_event,),
        )
        obs_list = seabank_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].display_name_safe, "SeaBank Account")

    def test_10_seabank_owner_name_excluded_from_observation(self) -> None:
        sentinel_owner = "SENTINEL_OWNER_JANE_DOE"
        adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-sea-4",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="901234567890",
                display_name_raw="SeaBank Account",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sea-4",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="seabank-2026-03",
            events=(obs_event,),
        )
        obs_list = seabank_mod.extract_account_observations(res)
        self.assert_sentinel_absent(obs_list[0], sentinel_owner)

    def test_11_seabank_fail_closed_on_corrupt_header(self) -> None:
        adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sea-corrupt",
            parse_status=AdapterParseStatus.FAILED,
            period_status=PeriodStatus.UNKNOWN,
            period_start=None,
            period_end=None,
            natural_document_key_candidate=None,
            events=(),
        )
        obs_list = seabank_mod.extract_account_observations(res)
        self.assertEqual(obs_list, [])

    def test_12_seabank_zero_counterparty_account_leakage(self) -> None:
        sentinel_cp = "SENTINEL_CP_ACCOUNT_88887777"
        adapter = seabank_mod.SeaBankMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-sea-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="901234567890",
                display_name_raw="SeaBank Account",
            ),
            source_event_id="evt-sea-obs",
        )
        tx_event = _make_envelope(
            source_document_id="doc-sea-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("50000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="901234567890",
                destination_account_key_raw=sentinel_cp,
                occurred_at="2026-03-05T10:00:00+07:00",
            ),
            source_event_id="evt-sea-tx",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sea-5",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="seabank-2026-03",
            events=(obs_event, tx_event),
        )
        obs_list = seabank_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assert_sentinel_absent(obs_list[0], sentinel_cp)

    # =========================================================================
    # BLU (Tests 13-18)
    # =========================================================================

    def test_13_blu_mutation_observation_with_valid_account_number(self) -> None:
        adapter = blu_mod.BluAccountMutationAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-blu-1",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="0011223344",
                display_name_raw="blu Account",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-blu-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="blu-cand-1",
            events=(obs_event,),
        )
        obs_list = blu_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.institution_id, "blu")
        self.assertEqual(obs.raw_account_key, "0011223344")
        self.assertEqual(obs.ownership_state, OwnershipState.OWNED)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.HIGH)

    def test_14_blu_fallback_behavior_when_account_number_missing(self) -> None:
        adapter = blu_mod.BluAccountMutationAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-blu-missing",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="blu-cand-missing",
            events=(),
        )
        obs_list = blu_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.raw_account_key, "")
        self.assertEqual(obs.ownership_state, OwnershipState.UNKNOWN)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_15_blu_neutral_display_name(self) -> None:
        adapter = blu_mod.BluAccountMutationAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-blu-3",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="0011223344",
                display_name_raw="blu Account",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-blu-3",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="blu-cand-3",
            events=(obs_event,),
        )
        obs_list = blu_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].display_name_safe, "blu Account")

    def test_16_blu_counterparty_accounts_excluded(self) -> None:
        sentinel_cp = "SENTINEL_CP_ACCOUNT_99990000"
        adapter = blu_mod.BluAccountMutationAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-blu-4",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="0011223344",
                display_name_raw="blu Account",
            ),
            source_event_id="evt-blu-obs",
        )
        tx_event = _make_envelope(
            source_document_id="doc-blu-4",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("25000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="0011223344",
                destination_account_key_raw=sentinel_cp,
                occurred_at="2026-03-05T10:00:00+07:00",
            ),
            source_event_id="evt-blu-tx",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-blu-4",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="blu-cand-4",
            events=(obs_event, tx_event),
        )
        obs_list = blu_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assert_sentinel_absent(obs_list[0], sentinel_cp)

    def test_17_blu_multiple_transaction_consistency(self) -> None:
        adapter = blu_mod.BluAccountMutationAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-blu-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="0011223344",
                display_name_raw="blu Account",
            ),
            source_event_id="evt-blu-obs",
        )
        tx1 = _make_envelope(
            source_document_id="doc-blu-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("10000.00"),
                currency="IDR",
                direction=EventDirection.INFLOW,
                status=SourceEventStatus.POSTED,
                destination_account_key_raw="0011223344",
                occurred_at="2026-03-02T10:00:00+07:00",
            ),
            source_event_id="tx1",
        )
        tx2 = _make_envelope(
            source_document_id="doc-blu-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("20000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="0011223344",
                occurred_at="2026-03-03T11:00:00+07:00",
            ),
            source_event_id="tx2",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-blu-5",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="blu-cand-5",
            events=(obs_event, tx1, tx2),
        )
        obs_list = blu_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assertEqual(obs_list[0].raw_account_key, "0011223344")

    def test_18_blu_fail_closed_on_unparseable_payload(self) -> None:
        adapter = blu_mod.BluAccountMutationAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-blu-fail",
            parse_status=AdapterParseStatus.FAILED,
            period_status=PeriodStatus.UNKNOWN,
            period_start=None,
            period_end=None,
            natural_document_key_candidate=None,
            events=(),
        )
        obs_list = blu_mod.extract_account_observations(res)
        self.assertEqual(obs_list, [])

    # =========================================================================
    # GOPAY (Tests 19-24)
    # =========================================================================

    def test_19_gopay_wallet_observation_with_valid_phone_key(self) -> None:
        adapter = gopay_mod.GoPayEStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-gp-1",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="08123456789",
                display_name_raw="GoPay Wallet",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-gp-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="gopay-cand-1",
            events=(obs_event,),
        )
        obs_list = gopay_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.institution_id, "gopay")
        self.assertEqual(obs.raw_account_key, "08123456789")
        self.assertEqual(obs.account_type, AccountType.WALLET)
        self.assertEqual(obs.ownership_state, OwnershipState.OWNED)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.HIGH)

    def test_20_gopay_counterparty_phone_numbers_excluded(self) -> None:
        sentinel_cp = "SENTINEL_CP_PHONE_08999999999"
        adapter = gopay_mod.GoPayEStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-gp-2",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="08123456789",
                display_name_raw="GoPay Wallet",
            ),
            source_event_id="evt-gp-obs",
        )
        tx_event = _make_envelope(
            source_document_id="doc-gp-2",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("15000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="08123456789",
                destination_account_key_raw=sentinel_cp,
                occurred_at="2026-03-05T10:00:00+07:00",
            ),
            source_event_id="evt-gp-tx",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-gp-2",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="gp-cand-2",
            events=(obs_event, tx_event),
        )
        obs_list = gopay_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assert_sentinel_absent(obs_list[0], sentinel_cp)

    def test_21_gopay_neutral_display_name(self) -> None:
        adapter = gopay_mod.GoPayEStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-gp-3",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="08123456789",
                display_name_raw="GoPay Wallet",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-gp-3",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="gp-cand-3",
            events=(obs_event,),
        )
        obs_list = gopay_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].display_name_safe, "GoPay Wallet")

    def test_22_gopay_balance_tracking_when_present(self) -> None:
        adapter = gopay_mod.GoPayEStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-gp-4",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="08123456789",
                display_name_raw="GoPay Wallet",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-gp-4",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-05-01",
            period_end="2026-05-31",
            natural_document_key_candidate="gp-cand-4",
            events=(obs_event,),
        )
        obs_list = gopay_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].effective_date, "2026-05-01")

    def test_23_gopay_fail_closed_when_wallet_key_missing(self) -> None:
        adapter = gopay_mod.GoPayEStatementAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-gp-missing",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="gp-cand-missing",
            events=(),
        )
        obs_list = gopay_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.raw_account_key, "")
        self.assertEqual(obs.ownership_state, OwnershipState.UNKNOWN)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_24_gopay_zero_merchant_account_leakage(self) -> None:
        sentinel_merchant = "SENTINEL_MERCHANT_KOPI_KENANGAN"
        adapter = gopay_mod.GoPayEStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-gp-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="08123456789",
                display_name_raw="GoPay Wallet",
            ),
            source_event_id="evt-gp-obs5",
        )
        tx_event = _make_envelope(
            source_document_id="doc-gp-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("18000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="08123456789",
                destination_account_key_raw=sentinel_merchant,
                occurred_at="2026-03-05T10:00:00+07:00",
            ),
            source_event_id="evt-gp-merchant",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-gp-5",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="gp-cand-5",
            events=(obs_event, tx_event),
        )
        obs_list = gopay_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assert_sentinel_absent(obs_list[0], sentinel_merchant)

    # =========================================================================
    # BCA (Tests 25-30)
    # =========================================================================

    def test_25_bca_root_checking_observation_from_valid_statement(self) -> None:
        adapter = bca_mod.BCAMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-bca-1",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="1234567890",
                parent_observed_key="1234567890",
                display_name_raw="BCA Tahapan",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-bca-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="bca-cand-1",
            events=(obs_event,),
        )
        obs_list = bca_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        root = obs_list[0]
        self.assertEqual(root.institution_id, "bca")
        self.assertEqual(root.raw_account_key, "1234567890")
        self.assertEqual(root.account_type, AccountType.TRANSACTIONAL)
        self.assertIsNone(root.parent_raw_account_key)
        self.assertEqual(root.display_name_safe, "BCA Tahapan")

    def test_26_bca_pocket_child_observation_linked_to_root(self) -> None:
        adapter = bca_mod.BCAMonthlyStatementAdapter()
        events = (
            _make_envelope(
                source_document_id="doc-bca-2",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="1234567890",
                    parent_observed_key="1234567890",
                    display_name_raw="BCA Tahapan",
                ),
                source_event_id="evt-bca-root",
            ),
            _make_envelope(
                source_document_id="doc-bca-2",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="9876543210",
                    parent_observed_key="1234567890",
                    display_name_raw="BCA Poket",
                ),
                source_event_id="evt-bca-poket",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-bca-2",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="bca-cand-2",
            events=events,
        )
        obs_list = bca_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 2)
        root, poket = obs_list[0], obs_list[1]
        self.assertEqual(poket.raw_account_key, "9876543210")
        self.assertEqual(poket.parent_raw_account_key, root.raw_account_key)
        self.assertEqual(poket.account_type, AccountType.SAVINGS)
        self.assertEqual(poket.parent_account_type, AccountType.TRANSACTIONAL)

    def test_27_bca_neutral_display_name(self) -> None:
        adapter = bca_mod.BCAMonthlyStatementAdapter()
        events = (
            _make_envelope(
                source_document_id="doc-bca-3",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="1234567890",
                    parent_observed_key="1234567890",
                    display_name_raw="BCA Tahapan",
                ),
                source_event_id="evt-bca-root3",
            ),
            _make_envelope(
                source_document_id="doc-bca-3",
                source_registry_id=adapter.descriptor.source_registry_id,
                template_id=adapter.descriptor.template_id,
                parser_version=adapter.descriptor.parser_version,
                source_channel=adapter.descriptor.source_channel,
                event_role=EventRole.ACCOUNT_OBSERVATION,
                payload=ObservedAccountEvidence(
                    observed_provider_account_key="9876543210",
                    parent_observed_key="1234567890",
                    display_name_raw="Poket Pribadi Budi",
                ),
                source_event_id="evt-bca-poket3",
            ),
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-bca-3",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="bca-cand-3",
            events=events,
        )
        obs_list = bca_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].display_name_safe, "BCA Tahapan")
        self.assertEqual(obs_list[1].display_name_safe, "BCA Poket")
        self.assertNotIn("Budi", obs_list[1].display_name_safe)

    def test_28_bca_counterparty_accounts_excluded(self) -> None:
        sentinel_cp = "SENTINEL_CP_ACCOUNT_55554444"
        adapter = bca_mod.BCAMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-bca-4",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="1234567890",
                parent_observed_key="1234567890",
                display_name_raw="BCA Tahapan",
            ),
            source_event_id="evt-bca-obs4",
        )
        tx_event = _make_envelope(
            source_document_id="doc-bca-4",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("500000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="1234567890",
                destination_account_key_raw=sentinel_cp,
                occurred_at="2026-03-05T10:00:00+07:00",
            ),
            source_event_id="evt-bca-tx",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-bca-4",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="bca-cand-4",
            events=(obs_event, tx_event),
        )
        obs_list = bca_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assert_sentinel_absent(obs_list[0], sentinel_cp)

    def test_29_bca_fail_closed_when_root_account_missing(self) -> None:
        adapter = bca_mod.BCAMonthlyStatementAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-bca-missing",
            parse_status=AdapterParseStatus.REVIEW_REQUIRED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="bca-cand-missing",
            events=(),
            diagnostics=(
                SafeDiagnostic(
                    code="BCA_HEADER_INCOMPLETE",
                    severity=DiagnosticSeverity.WARNING,
                    message="Header incomplete",
                ),
            ),
        )
        obs_list = bca_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.raw_account_key, "")
        self.assertEqual(obs.ownership_state, OwnershipState.UNKNOWN)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_30_bca_zero_recipient_leakage(self) -> None:
        sentinel_recipient = "SENTINEL_RECIPIENT_AGUS_WIDODO"
        adapter = bca_mod.BCAMonthlyStatementAdapter()
        obs_event = _make_envelope(
            source_document_id="doc-bca-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.ACCOUNT_OBSERVATION,
            payload=ObservedAccountEvidence(
                observed_provider_account_key="1234567890",
                parent_observed_key="1234567890",
                display_name_raw="BCA Tahapan",
            ),
            source_event_id="evt-bca-obs5",
        )
        tx_event = _make_envelope(
            source_document_id="doc-bca-5",
            source_registry_id=adapter.descriptor.source_registry_id,
            template_id=adapter.descriptor.template_id,
            parser_version=adapter.descriptor.parser_version,
            source_channel=adapter.descriptor.source_channel,
            event_role=EventRole.CASH_MOVEMENT,
            payload=CashMovementEvidence(
                amount=Decimal("100000.00"),
                currency="IDR",
                direction=EventDirection.OUTFLOW,
                status=SourceEventStatus.POSTED,
                source_account_key_raw="1234567890",
                description_raw=f"TRSF E-BANKING KE {sentinel_recipient}",
                occurred_at="2026-03-06T10:00:00+07:00",
            ),
            source_event_id="evt-bca-tx2",
        )
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-bca-5",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="bca-cand-5",
            events=(obs_event, tx_event),
        )
        obs_list = bca_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assert_sentinel_absent(obs_list[0], sentinel_recipient)

    # =========================================================================
    # STOCKBIT (Tests 31-36)
    # =========================================================================

    def test_31_stockbit_investment_observation_from_valid_report(self) -> None:
        adapter = stockbit_mod.StockbitStatementAdapter()
        identity = stockbit_mod.StockbitDocumentIdentity(
            client_code_raw="XL1234",
            period_start="2026-03-01",
            period_end="2026-03-31",
        )
        res = stockbit_mod.StockbitAdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-stockbit-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="stockbit-cand-1",
            events=(),
            document_identity=identity,
        )
        obs_list = stockbit_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.institution_id, "stockbit")
        self.assertEqual(obs.raw_account_key, "XL1234")
        self.assertEqual(obs.account_type, AccountType.INVESTMENT)
        self.assertEqual(obs.ownership_state, OwnershipState.OWNED)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.HIGH)

    def test_32_stockbit_rdn_cash_account_distinct_evidence(self) -> None:
        adapter = stockbit_mod.StockbitStatementAdapter()
        identity = stockbit_mod.StockbitDocumentIdentity(
            client_code_raw="XL1234",
            rdn_bank="BCA",
            period_start="2026-03-01",
            period_end="2026-03-31",
        )
        res = stockbit_mod.StockbitAdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-stockbit-2",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="stockbit-cand-2",
            events=(),
            document_identity=identity,
        )
        obs_list = stockbit_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.institution_id, "stockbit")
        self.assertEqual(obs.account_type, AccountType.INVESTMENT)
        self.assertNotEqual(obs.institution_id, "bca")

    def test_33_stockbit_no_automatic_merge_with_non_investment_accounts(self) -> None:
        sb_obs = AccountDiscoveryObservation(
            institution_id="stockbit",
            source_registry_id="stockbit_statement",
            raw_account_key="XL1234",
            display_name_safe="Stockbit Securities",
            account_type=AccountType.INVESTMENT,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        bca_obs = AccountDiscoveryObservation(
            institution_id="bca",
            source_registry_id="bca_statement",
            raw_account_key="XL1234",
            display_name_safe="BCA Tahapan",
            account_type=AccountType.TRANSACTIONAL,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([sb_obs, bca_obs])
        self.assertEqual(len(plan.resolutions), 2)
        r_sb, r_bca = plan.resolutions[0], plan.resolutions[1]
        self.assertNotEqual(r_sb.protected_account_key, r_bca.protected_account_key)
        self.assertNotEqual(r_sb.institution_id, r_bca.institution_id)

    def test_34_stockbit_neutral_display_name(self) -> None:
        adapter = stockbit_mod.StockbitStatementAdapter()
        identity = stockbit_mod.StockbitDocumentIdentity(
            client_code_raw="XL1234",
            period_start="2026-03-01",
            period_end="2026-03-31",
        )
        res = stockbit_mod.StockbitAdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-stockbit-3",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="stockbit-cand-3",
            events=(),
            document_identity=identity,
        )
        obs_list = stockbit_mod.extract_account_observations(res)
        self.assertEqual(obs_list[0].display_name_safe, "Stockbit Securities")

    def test_35_stockbit_fail_closed_when_client_code_missing(self) -> None:
        adapter = stockbit_mod.StockbitStatementAdapter()
        identity = stockbit_mod.StockbitDocumentIdentity(
            client_code_raw=None,
            period_start="2026-03-01",
            period_end="2026-03-31",
        )
        res = stockbit_mod.StockbitAdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-stockbit-missing",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="stockbit-cand-missing",
            events=(),
            document_identity=identity,
        )
        obs_list = stockbit_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.raw_account_key, "")
        self.assertEqual(obs.ownership_state, OwnershipState.UNKNOWN)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_36_stockbit_portfolio_balances_isolated(self) -> None:
        sentinel_ticker = "BBCA"
        adapter = stockbit_mod.StockbitStatementAdapter()
        identity = stockbit_mod.StockbitDocumentIdentity(
            client_code_raw="XL1234",
            period_start="2026-03-01",
            period_end="2026-03-31",
        )
        res = stockbit_mod.StockbitAdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-stockbit-4",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="stockbit-cand-4",
            events=(),
            document_identity=identity,
        )
        obs_list = stockbit_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        self.assertNotEqual(obs_list[0].raw_account_key, sentinel_ticker)
        self.assertEqual(obs_list[0].raw_account_key, "XL1234")

    # =========================================================================
    # SHOPEEPAY (Tests 37-42)
    # =========================================================================

    def test_37_shopeepay_blocked_unverified_fail_closed_behavior(self) -> None:
        adapter = shopeepay_mod.ShopeePayTransactionHistoryImageAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sp-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="sp-cand-1",
            events=(),
        )
        obs_list = shopeepay_mod.extract_account_observations(res)
        self.assertEqual(len(obs_list), 1)
        obs = obs_list[0]
        self.assertEqual(obs.raw_account_key, "")
        self.assertEqual(obs.ownership_state, OwnershipState.UNKNOWN)
        self.assertEqual(obs.ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_38_shopeepay_no_speculative_account_creation(self) -> None:
        obs_list = shopeepay_mod.extract_account_observations(None)
        self.assertEqual(len(obs_list), 1)
        plan = self.resolver.resolve(obs_list)
        self.assertEqual(len(plan.resolutions), 1)
        self.assertFalse(plan.resolutions[0].persistence_eligible)
        self.assertIsNone(plan.resolutions[0].protected_account_key)
        self.assertGreaterEqual(len(plan.diagnostics), 1)

    def test_39_shopeepay_neutral_display_name(self) -> None:
        obs_list = shopeepay_mod.extract_account_observations(None)
        self.assertEqual(obs_list[0].display_name_safe, "ShopeePay")

    def test_40_shopeepay_no_transaction_counterparty_leakage(self) -> None:
        sentinel_merchant = "SENTINEL_INDOMARET_SHOPEE"
        adapter = shopeepay_mod.ShopeePayTransactionHistoryImageAdapter()
        res = AdapterResult(
            descriptor=adapter.descriptor,
            source_document_id="doc-sp-tx",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-31",
            natural_document_key_candidate="sp-cand-tx",
            events=(),
        )
        obs_list = shopeepay_mod.extract_account_observations(res)
        self.assert_sentinel_absent(obs_list[0], sentinel_merchant)

    def test_41_shopeepay_order_ids_not_treated_as_accounts(self) -> None:
        sentinel_order_id = "SENTINEL_ORDER_260301ABCDEF"
        obs_list = shopeepay_mod.extract_account_observations(None)
        self.assertEqual(obs_list[0].raw_account_key, "")
        self.assert_sentinel_absent(obs_list[0], sentinel_order_id)

    def test_42_shopeepay_explicit_status_tracking(self) -> None:
        obs_list = shopeepay_mod.extract_account_observations(None)
        obs = obs_list[0]
        self.assertEqual(obs.institution_id, "shopeepay")
        self.assertEqual(obs.account_type, AccountType.WALLET)

    # =========================================================================
    # SHOPEE ORDERS (Tests 43-48)
    # =========================================================================

    def test_43_shopee_orders_verified_not_applicable_status(self) -> None:
        adapter = shopee_orders_mod.ShopeeOrdersReceiptAdapter()
        self.assertEqual(adapter.descriptor.source_registry_id, "shopee_orders")
        self.assertEqual(adapter.descriptor.source_channel, SourceChannel.PDF)

    def test_44_shopee_orders_returns_empty_observations_list(self) -> None:
        adapter = shopee_orders_mod.ShopeeOrdersReceiptAdapter()
        obs_list = adapter.extract_account_observations(None)
        self.assertEqual(obs_list, [])
        obs_list_mod = shopee_orders_mod.extract_account_observations(None)
        self.assertEqual(obs_list_mod, [])

    def test_45_shopee_orders_no_order_seller_accounts_created(self) -> None:
        sentinel_seller = "SENTINEL_TOKO_BUDI_OFFICIAL"
        sentinel_order = "SENTINEL_SHOPEE_ORDER_998877"
        identity = shopee_orders_mod.ShopeeOrderDocumentIdentity(
            order_number=sentinel_order,
            seller_name=sentinel_seller,
            order_date="2026-03-01",
        )
        res = shopee_orders_mod.ShopeeOrderAdapterResult(
            descriptor=shopee_orders_mod.ShopeeOrdersReceiptAdapter().descriptor,
            source_document_id="doc-shopee-order-1",
            parse_status=AdapterParseStatus.COMPLETED,
            period_status=PeriodStatus.CLOSED,
            period_start="2026-03-01",
            period_end="2026-03-01",
            natural_document_key_candidate="cand-order-1",
            events=(),
            diagnostics=(),
            order_reconciliation_status="MATCHED",
            reconciliation_difference=Decimal("0.00"),
            order_identity=identity,
        )
        obs_list = shopee_orders_mod.extract_account_observations(res)
        self.assertEqual(obs_list, [])

    def test_46_shopee_orders_payment_method_metadata_not_converted_to_account(self) -> None:
        adapter = shopee_orders_mod.ShopeeOrdersReceiptAdapter()
        obs_list = adapter.extract_account_observations(None)
        self.assertEqual(obs_list, [])

    def test_47_shopee_orders_shipping_metadata_excluded(self) -> None:
        adapter = shopee_orders_mod.ShopeeOrdersReceiptAdapter()
        obs_list = adapter.extract_account_observations(None)
        self.assertEqual(obs_list, [])

    def test_48_shopee_orders_idempotent_empty_result(self) -> None:
        adapter = shopee_orders_mod.ShopeeOrdersReceiptAdapter()
        res1 = adapter.extract_account_observations(None)
        res2 = adapter.extract_account_observations(None)
        res3 = shopee_orders_mod.extract_account_observations("dummy")
        self.assertEqual(res1, [])
        self.assertEqual(res2, [])
        self.assertEqual(res3, [])


if __name__ == "__main__":
    unittest.main()
