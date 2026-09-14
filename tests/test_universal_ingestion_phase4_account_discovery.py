"""
Tests for Universal Ingestion Phase 4 Account Discovery and Identity Privacy.
Hardened for P4.2-R1 with composite scope isolation and fail-closed security.

Contains exactly 44 focused test methods.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import sys
import unittest
from typing import Any

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    LifecycleState,
    OwnershipState,
)
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
    canonical_encoding,
    is_valid_protected_key,
)
from aturuang.ingestion_account_discovery import (
    AccountDiscoveryDiagnostic,
    AccountDiscoveryObservation,
    AccountDiscoveryPlan,
    AccountDiscoveryResolver,
    AccountResolution,
    ExistingAccountState,
    is_valid_calendar_date,
)

TEST_SECRET = "synthetic-test-secret-at-least-32-chars-long-strictly-mocked!"


class TestUniversalIngestionPhase4AccountDiscovery(unittest.TestCase):
    """
    Focused test suite for Phase 4 Account Discovery pure resolver.
    Contains exactly 44 test methods.
    """

    def setUp(self) -> None:
        self.protector = AccountIdentityProtector(secret=TEST_SECRET)
        self.resolver = AccountDiscoveryResolver(protector=self.protector)

    def assert_sentinel_absent(self, obj: Any, sentinel: str, path: str = "root") -> None:
        """
        Recursively verifies that sentinel string is absent from any field,
        nested object, dictionary, sequence, exception string, and repr().
        Does not print sentinel in failure message to prevent test leaks.
        """
        repr_str = repr(obj)
        self.assertNotIn(sentinel, repr_str, f"Sentinel found in repr at {path}")

        if isinstance(obj, str):
            self.assertNotIn(sentinel, obj, f"Sentinel found in string value at {path}")
        elif is_dataclass(obj):
            for f in fields(obj):
                val = getattr(obj, f.name)
                self.assert_sentinel_absent(val, sentinel, f"{path}.{f.name}")
        elif isinstance(obj, dict):
            for k, v in obj.items():
                self.assert_sentinel_absent(k, sentinel, f"{path}.key")
                self.assert_sentinel_absent(v, sentinel, f"{path}[key]")
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for i, item in enumerate(obj):
                self.assert_sentinel_absent(item, sentinel, f"{path}[{i}]")
        elif isinstance(obj, BaseException):
            self.assertNotIn(sentinel, str(obj), f"Sentinel found in exception at {path}")

    # =========================================================================
    # Security Cases (1 - 8)
    # =========================================================================

    def test_01_security_raw_identifier_absent_from_returned_fields(self) -> None:
        sentinel = "SYNTHETIC_RAW_SECRET_ACCOUNT_KEY_99999"
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=sentinel,
            display_name_safe="Main Savings",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-15",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertTrue(res.persistence_eligible)
        self.assertIsNotNone(res.protected_account_key)
        self.assertTrue(res.protected_account_key.startswith("v1:"))
        self.assert_sentinel_absent(plan, sentinel)

    def test_02_security_raw_identifier_absent_from_diagnostics_and_exceptions(self) -> None:
        sentinel = "SYNTHETIC_RAW_SECRET_ACCOUNT_KEY_88888"
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=sentinel,
            parent_raw_account_key=sentinel,
            parent_account_type=AccountType.SAVINGS,
            display_name_safe="Self Parent",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-15",
        )
        plan = self.resolver.resolve([obs])
        self.assertTrue(len(plan.diagnostics) >= 1)
        self.assertTrue(any(d.code == "SELF_PARENTING" for d in plan.diagnostics))
        self.assert_sentinel_absent(plan, sentinel)

        # Exception check in protector
        with self.assertRaises(ValueError) as ctx:
            self.protector.protect_account_key("", "src_1", AccountType.SAVINGS, sentinel)
        self.assert_sentinel_absent(ctx.exception, sentinel)

    def test_03_security_raw_identifier_absent_from_repr(self) -> None:
        sentinel = "SYNTHETIC_RAW_SECRET_ACCOUNT_KEY_77777"
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=sentinel,
            display_name_safe="Secret Acct",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-15",
        )
        self.assertNotIn(sentinel, repr(obs))
        self.assertNotIn(TEST_SECRET, repr(self.protector))
        self.assertIn("secret='***'", repr(self.protector))

        plan = self.resolver.resolve([obs])
        self.assertNotIn(sentinel, repr(plan))
        self.assertNotIn(sentinel, repr(plan.resolutions[0]))

    def test_04_security_different_institutions_produce_different_protected_keys(self) -> None:
        raw_key = "1234567890"
        key_a = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        key_b = self.protector.protect_account_key("inst_b", "src_1", AccountType.SAVINGS, raw_key)
        self.assertNotEqual(key_a, key_b)

    def test_05_security_identical_scoped_input_produces_stable_protected_key(self) -> None:
        raw_key = "1234567890"
        key_1 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        key_2 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        self.assertEqual(key_1, key_2)

    def test_06_security_missing_or_weak_protector_secret_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            AccountIdentityProtector(secret=None)  # type: ignore
        with self.assertRaises(ValueError):
            AccountIdentityProtector(secret="")
        with self.assertRaises(ValueError):
            AccountIdentityProtector(secret="too-short-secret-under-32-chars")
        with self.assertRaises(ValueError):
            AccountDiscoveryResolver(protector=None)  # type: ignore

    def test_07_security_display_name_change_preserves_protected_identity(self) -> None:
        raw_key = "1234567890"
        key_before = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        existing = ExistingAccountState(
            protected_account_key=key_before,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Old Account Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs_renamed = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=raw_key,
            display_name_safe="New Account Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs_renamed], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.protected_account_key, key_before)
        self.assertEqual(res.lifecycle_state, LifecycleState.RENAMED)
        self.assertEqual(res.display_name_safe, "New Account Name")
        self.assertTrue(res.persistence_eligible)

    def test_08_security_owner_name_change_preserves_protected_identity(self) -> None:
        raw_key = "1234567890"
        key_1 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        # Contract has no owner_name field, ensuring identity cannot incorporate owner name
        self.assertFalse(hasattr(AccountDiscoveryObservation, "owner_name"))
        self.assertFalse(hasattr(ExistingAccountState, "owner_name"))
        self.assertFalse(hasattr(AccountResolution, "owner_name"))
        key_2 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        self.assertEqual(key_1, key_2)

    # =========================================================================
    # Cryptographic Scope & Foundation Cases (9 - 15)
    # =========================================================================

    def test_09_crypto_hmac_key_versioning(self) -> None:
        prot_v1 = AccountIdentityProtector(secret=TEST_SECRET, key_version=1)
        prot_v2 = AccountIdentityProtector(secret=TEST_SECRET, key_version=2)
        key_v1 = prot_v1.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "12345")
        key_v2 = prot_v2.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "12345")
        self.assertTrue(key_v1.startswith("v1:"))
        self.assertTrue(key_v2.startswith("v2:"))
        self.assertNotEqual(key_v1, key_v2)

    def test_10_crypto_domain_separation(self) -> None:
        prot_dom1 = AccountIdentityProtector(secret=TEST_SECRET, domain="domain:one")
        prot_dom2 = AccountIdentityProtector(secret=TEST_SECRET, domain="domain:two")
        k1 = prot_dom1.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "12345")
        k2 = prot_dom2.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "12345")
        self.assertNotEqual(k1, k2)

    def test_11_crypto_source_scope_separation(self) -> None:
        k_src1 = self.protector.protect_account_key("inst_a", "source_alpha", AccountType.SAVINGS, "12345")
        k_src2 = self.protector.protect_account_key("inst_a", "source_beta", AccountType.SAVINGS, "12345")
        self.assertNotEqual(k_src1, k_src2)

    def test_12_crypto_account_type_scope_separation(self) -> None:
        k_sav = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "12345")
        k_inv = self.protector.protect_account_key("inst_a", "src_1", AccountType.INVESTMENT, "12345")
        self.assertNotEqual(k_sav, k_inv)

    def test_13_crypto_parent_scope_separation(self) -> None:
        p_key1 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "parent_1")
        p_key2 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "parent_2")
        k_root = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "sub_1", None)
        k_sub1 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "sub_1", p_key1)
        k_sub2 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "sub_1", p_key2)
        self.assertNotEqual(k_root, k_sub1)
        self.assertNotEqual(k_sub1, k_sub2)

    def test_14_crypto_canonical_encoding_collision_resistance(self) -> None:
        enc1 = canonical_encoding("a", "bc")
        enc2 = canonical_encoding("ab", "c")
        self.assertNotEqual(enc1, enc2)
        self.assertEqual(enc1, b"1:a|2:bc")
        self.assertEqual(enc2, b"2:ab|1:c")
        # Strict rejection of unsupported types
        with self.assertRaises(TypeError):
            canonical_encoding(12345)  # type: ignore

    def test_15_crypto_constant_time_comparison(self) -> None:
        self.assertTrue(AccountIdentityProtector.constant_time_equals("v1:abc123def456", "v1:abc123def456"))
        self.assertFalse(AccountIdentityProtector.constant_time_equals("v1:abc123def456", "v1:xyz987uvw543"))
        self.assertFalse(AccountIdentityProtector.constant_time_equals("v1:abc123def456", 12345))  # type: ignore

    # =========================================================================
    # Lifecycle & Resolution Cases (16 - 26)
    # =========================================================================

    def test_16_resolver_lifecycle_new_account(self) -> None:
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="First Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.NEW)
        self.assertTrue(res.persistence_eligible)
        self.assertIsNotNone(res.protected_account_key)

    def test_17_resolver_lifecycle_unchanged_account(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Current Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Current Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNCHANGED)
        self.assertTrue(res.persistence_eligible)

    def test_18_resolver_lifecycle_renamed_account(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Old Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Brand New Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.RENAMED)
        self.assertEqual(res.display_name_safe, "Brand New Name")
        self.assertTrue(res.persistence_eligible)

    def test_19_resolver_lifecycle_explicit_closed(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Active Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Active Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            explicit_lifecycle_signal="CLOSED",
            lifecycle_authoritative=True,
            lifecycle_evidence_code="OFFICIAL_CLOSURE_STATEMENT",
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.CLOSED)
        self.assertTrue(res.persistence_eligible)
        self.assertEqual(res.lifecycle_evidence_code, "OFFICIAL_CLOSURE_STATEMENT")

    def test_20_resolver_lifecycle_absence_does_not_emit_closed(self) -> None:
        prot_key_existing = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_absent")
        existing = ExistingAccountState(
            protected_account_key=prot_key_existing,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Unobserved Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs_other = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_active",
            display_name_safe="Active Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs_other], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].display_name_safe, "Active Account")
        self.assertEqual(len(plan.closed_accounts), 0)

    def test_21_resolver_lifecycle_ambiguous_reuse_becomes_unverified(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing_closed = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Closed Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.CLOSED,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Reappeared Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-04-01",
        )
        plan = self.resolver.resolve([obs], [existing_closed])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(res.persistence_eligible)
        self.assertIsNone(res.protected_account_key)
        self.assertTrue(any(d.code == "AMBIGUOUS_REUSE" for d in plan.diagnostics))

    def test_22_resolver_lifecycle_explicit_reuse_supported(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing_closed = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Closed Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.CLOSED,
            effective_date="2026-01-01",
        )
        obs_reused = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Reassigned Slot",
            account_type=AccountType.SAVINGS,
            effective_date="2026-05-01",
            explicit_lifecycle_signal="REUSED",
            lifecycle_authoritative=True,
            lifecycle_evidence_code="EXPLICIT_PROVIDER_REASSIGNMENT",
        )
        plan = self.resolver.resolve([obs_reused], [existing_closed])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.REUSED)
        self.assertTrue(res.persistence_eligible)
        self.assertEqual(res.lifecycle_evidence_code, "EXPLICIT_PROVIDER_REASSIGNMENT")

    def test_23_resolver_lifecycle_out_of_order_history_safe(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing_newer = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Newer Display Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-06-01",
        )
        obs_older = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Older Historical Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs_older], [existing_newer])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNCHANGED)
        self.assertEqual(res.display_name_safe, "Newer Display Name")
        self.assertEqual(res.effective_date, "2026-06-01")
        self.assertTrue(res.persistence_eligible)

    def test_24_resolver_lifecycle_conflicting_equal_date_fails_closed(self) -> None:
        # Conflict 1: display name conflict on same date
        obs1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_conflict",
            display_name_safe="Conflicting Name A",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_conflict",
            display_name_safe="Conflicting Name B",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        plan1 = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan1.resolutions), 1)
        res1 = plan1.resolutions[0]
        self.assertEqual(res1.lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(res1.persistence_eligible)
        self.assertIsNone(res1.protected_account_key)
        self.assertTrue(any(d.code == "CONFLICTING_OBSERVATIONS" for d in plan1.diagnostics))

        # Conflict 2: ownership confidence conflict on same date
        obs_conf1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_conf_test",
            display_name_safe="Conf Test",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        obs_conf2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_conf_test",
            display_name_safe="Conf Test",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.LOW,
        )
        plan2 = self.resolver.resolve([obs_conf1, obs_conf2])
        self.assertEqual(plan2.resolutions[0].lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(plan2.resolutions[0].persistence_eligible)
        self.assertTrue(any(d.code == "CONFLICTING_OBSERVATIONS" for d in plan2.diagnostics))

    def test_25_resolver_duplicate_replay_stable(self) -> None:
        obs1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_dup",
            display_name_safe="Identical Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_dup",
            display_name_safe="Identical Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.NEW)
        self.assertTrue(res.persistence_eligible)
        self.assertEqual(len(plan.diagnostics), 0)

    def test_26_resolver_input_reordering_deterministic(self) -> None:
        obs_a = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_a",
            display_name_safe="Account Alpha",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_b = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_b",
            display_name_safe="Account Beta",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-02",
        )
        obs_c = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_c",
            display_name_safe="Account Gamma",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-03",
        )
        plan_1 = self.resolver.resolve([obs_a, obs_b, obs_c])
        plan_2 = self.resolver.resolve([obs_c, obs_a, obs_b])
        plan_3 = self.resolver.resolve([obs_b, obs_c, obs_a])
        self.assertEqual(plan_1.resolutions, plan_2.resolutions)
        self.assertEqual(plan_2.resolutions, plan_3.resolutions)

    # =========================================================================
    # Ownership & Hierarchy Cases (27 - 32)
    # =========================================================================

    def test_27_resolver_ownership_preservation(self) -> None:
        obs_owned = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_owned",
            display_name_safe="Owned Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        obs_third = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_third",
            display_name_safe="Third Party Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.THIRD_PARTY,
            ownership_confidence=ConfidenceLevel.MEDIUM,
        )
        plan = self.resolver.resolve([obs_owned, obs_third])
        res_by_name = {r.display_name_safe: r for r in plan.resolutions}
        self.assertEqual(res_by_name["Owned Account"].ownership_state, OwnershipState.OWNED)
        self.assertEqual(res_by_name["Owned Account"].ownership_confidence, ConfidenceLevel.HIGH)
        self.assertEqual(res_by_name["Third Party Account"].ownership_state, OwnershipState.THIRD_PARTY)
        self.assertEqual(res_by_name["Third Party Account"].ownership_confidence, ConfidenceLevel.MEDIUM)

    def test_28_resolver_ownership_default_unknown(self) -> None:
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_default",
            display_name_safe="Unknown Owner Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(plan.resolutions[0].ownership_state, OwnershipState.UNKNOWN)
        self.assertEqual(plan.resolutions[0].ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_29_resolver_hierarchy_sibling_name_collision_distinct(self) -> None:
        obs_parent = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_parent",
            display_name_safe="Parent Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_child1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_child_1",
            parent_raw_account_key="raw_parent",
            parent_account_type=AccountType.SAVINGS,
            display_name_safe="Shared Pocket Name",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        obs_child2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_child_2",
            parent_raw_account_key="raw_parent",
            parent_account_type=AccountType.SAVINGS,
            display_name_safe="Shared Pocket Name",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_parent, obs_child1, obs_child2])
        self.assertEqual(len(plan.resolutions), 3)
        child_res = [r for r in plan.resolutions if r.display_name_safe == "Shared Pocket Name"]
        self.assertEqual(len(child_res), 2)
        self.assertNotEqual(child_res[0].protected_account_key, child_res[1].protected_account_key)

    def test_30_resolver_hierarchy_cross_provider_parent_rejected(self) -> None:
        p_key = self.protector.protect_account_key("inst_a", "src_provider_one", AccountType.SAVINGS, "raw_p1")
        existing_parent = ExistingAccountState(
            protected_account_key=p_key,
            institution_id="inst_a",
            source_registry_id="src_provider_one",
            display_name_safe="Provider One Parent",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs_child = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_provider_two",  # Different provider scope
            raw_account_key="raw_c1",
            parent_protected_account_key=p_key,
            display_name_safe="Cross Provider Child",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_child], [existing_parent])
        self.assertTrue(any(d.code == "CROSS_PROVIDER_PARENT" for d in plan.diagnostics))
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(res.persistence_eligible)
        self.assertIsNone(res.protected_account_key)

    def test_31_resolver_hierarchy_cycle_rejected(self) -> None:
        obs_node1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_cycle_1",
            parent_raw_account_key="raw_cycle_2",
            parent_account_type=AccountType.SUBACCOUNT,
            display_name_safe="Cycle Node 1",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        obs_node2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_cycle_2",
            parent_raw_account_key="raw_cycle_1",
            parent_account_type=AccountType.SUBACCOUNT,
            display_name_safe="Cycle Node 2",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_node1, obs_node2])
        self.assertTrue(any(d.code == "HIERARCHY_CYCLE" for d in plan.diagnostics))
        for r in plan.resolutions:
            self.assertEqual(r.lifecycle_state, LifecycleState.UNVERIFIED)
            self.assertFalse(r.persistence_eligible)
            self.assertIsNone(r.protected_account_key)

    def test_32_resolver_no_database_write_no_ledger_event_immutable_plan(self) -> None:
        self.assertNotIn("sqlite3", sys.modules.get("aturuang.ingestion_account_discovery", {}).__dict__)
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_clean",
            display_name_safe="Pure Memory Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs])
        self.assertIsInstance(plan.resolutions, tuple)
        self.assertIsInstance(plan.diagnostics, tuple)
        with self.assertRaises((TypeError, AttributeError)):
            plan.resolutions.append(None)  # type: ignore

    # =========================================================================
    # P4.2-R1 Hardened Cases (33 - 44)
    # =========================================================================

    def test_33_composite_scope_different_institutions_produce_distinct_identities(self) -> None:
        """1. Same raw key across two institutions stays distinct."""
        raw_key = "COMMON_NUMBER_123"
        obs1 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_common",
            raw_account_key=raw_key,
            display_name_safe="Alpha Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="bank_beta",
            source_registry_id="src_common",
            raw_account_key=raw_key,
            display_name_safe="Beta Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan.resolutions), 2)
        res1, res2 = plan.resolutions
        self.assertNotEqual(res1.protected_account_key, res2.protected_account_key)
        self.assertNotEqual(res1.institution_id, res2.institution_id)
        self.assertTrue(res1.persistence_eligible)
        self.assertTrue(res2.persistence_eligible)

    def test_34_composite_scope_different_sources_produce_distinct_identities(self) -> None:
        """2. Same raw key across two source registries stays distinct."""
        raw_key = "COMMON_NUMBER_456"
        obs1 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="source_pdf",
            raw_account_key=raw_key,
            display_name_safe="PDF Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="source_api",
            raw_account_key=raw_key,
            display_name_safe="API Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan.resolutions), 2)
        res1, res2 = plan.resolutions
        self.assertNotEqual(res1.protected_account_key, res2.protected_account_key)
        self.assertNotEqual(res1.source_registry_id, res2.source_registry_id)
        self.assertTrue(res1.persistence_eligible)
        self.assertTrue(res2.persistence_eligible)

    def test_35_composite_scope_different_account_types_produce_distinct_identities(self) -> None:
        """3. Same raw key across two account types stays distinct."""
        raw_key = "COMMON_NUMBER_789"
        obs1 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key=raw_key,
            display_name_safe="Savings Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key=raw_key,
            display_name_safe="RDN Account",
            account_type=AccountType.RDN,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan.resolutions), 2)
        res1, res2 = plan.resolutions
        self.assertNotEqual(res1.protected_account_key, res2.protected_account_key)
        self.assertNotEqual(res1.account_type, res2.account_type)
        self.assertTrue(res1.persistence_eligible)
        self.assertTrue(res2.persistence_eligible)

    def test_36_scoped_hierarchy_child_key_under_different_parents_distinct(self) -> None:
        """4. Same child raw key under different parents stays distinct and parent resolution is scoped."""
        obs_p1 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key="parent_alpha",
            display_name_safe="Parent Alpha",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_p2 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key="parent_beta",
            display_name_safe="Parent Beta",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_c1 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key="child_sub",
            parent_raw_account_key="parent_alpha",
            parent_account_type=AccountType.SAVINGS,
            display_name_safe="Child Pocket Alpha",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        obs_c2 = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key="child_sub",
            parent_raw_account_key="parent_beta",
            parent_account_type=AccountType.SAVINGS,
            display_name_safe="Child Pocket Beta",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_p1, obs_p2, obs_c1, obs_c2])
        self.assertEqual(len(plan.resolutions), 4)
        children = [r for r in plan.resolutions if r.account_type == AccountType.SUBACCOUNT]
        self.assertEqual(len(children), 2)
        self.assertNotEqual(children[0].protected_account_key, children[1].protected_account_key)
        self.assertNotEqual(children[0].parent_protected_account_key, children[1].parent_protected_account_key)

    def test_37_strict_calendar_date_validation_rejects_impossible_dates_and_timestamps(self) -> None:
        """5. Impossible dates and timestamp/junk suffixes are rejected."""
        self.assertFalse(is_valid_calendar_date("2026-02-30"))
        self.assertFalse(is_valid_calendar_date("2026-13-01"))
        self.assertFalse(is_valid_calendar_date("2026-01-01T12:00:00"))
        self.assertFalse(is_valid_calendar_date("2026-01-01 "))
        self.assertFalse(is_valid_calendar_date("2026-01-01_junk"))
        self.assertFalse(is_valid_calendar_date("2026/01/01"))
        self.assertTrue(is_valid_calendar_date("2026-01-31"))

        obs_bad = AccountDiscoveryObservation(
            institution_id="bank_alpha",
            source_registry_id="src_1",
            raw_account_key="raw_date_fail",
            display_name_safe="Date Fail Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-30",
        )
        plan = self.resolver.resolve([obs_bad])
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(res.persistence_eligible)
        self.assertIsNone(res.protected_account_key)
        self.assertTrue(any(d.code == "INVALID_CALENDAR_DATE" for d in plan.diagnostics))

    def test_38_out_of_order_preserves_complete_latest_existing_state_and_effective_date(self) -> None:
        """6. Out-of-order input preserves the complete latest existing state and effective date."""
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Latest Confirmed Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.RENAMED,
            effective_date="2026-08-01",
            parent_protected_account_key=None,
            lifecycle_authoritative=True,
            lifecycle_evidence_code="OFFICIAL_CLOSURE_STATEMENT",
        )
        # Older historical observation arrives
        obs_old = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name_safe="Outdated Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.UNKNOWN,
            ownership_confidence=ConfidenceLevel.UNKNOWN,
        )
        plan = self.resolver.resolve([obs_old], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.effective_date, "2026-08-01")
        self.assertEqual(res.display_name_safe, "Latest Confirmed Name")
        self.assertEqual(res.lifecycle_state, LifecycleState.UNCHANGED)
        self.assertEqual(res.ownership_state, OwnershipState.OWNED)
        self.assertEqual(res.ownership_confidence, ConfidenceLevel.HIGH)
        self.assertTrue(res.lifecycle_authoritative)
        self.assertEqual(res.lifecycle_evidence_code, "OFFICIAL_CLOSURE_STATEMENT")
        self.assertTrue(res.persistence_eligible)

    def test_39_unauthoritative_closed_signal_becomes_non_persistable_unverified(self) -> None:
        """7. CLOSED without structured authority becomes non-persistable UNVERIFIED."""
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_unauth_close",
            display_name_safe="Fake Close Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            explicit_lifecycle_signal="CLOSED",
            lifecycle_authoritative=False,  # Not authoritative!
            lifecycle_evidence_code=None,
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(res.persistence_eligible)
        self.assertIsNone(res.protected_account_key)
        self.assertTrue(any(d.code == "UNAUTHORITATIVE_LIFECYCLE_SIGNAL" for d in plan.diagnostics))

    def test_40_authoritative_closed_with_allowed_evidence_produces_closed(self) -> None:
        """8. Authoritative CLOSED with an allowed evidence code produces CLOSED."""
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_auth_close")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Active Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_auth_close",
            display_name_safe="Active Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-05-01",
            explicit_lifecycle_signal="CLOSED",
            lifecycle_authoritative=True,
            lifecycle_evidence_code="ACCOUNT_TERMINATION_NOTICE",
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.CLOSED)
        self.assertTrue(res.persistence_eligible)
        self.assertEqual(res.protected_account_key, prot_key)

    def test_41_ownership_confidence_unknown_preserved_never_upgraded(self) -> None:
        """9. UNKNOWN ownership confidence remains UNKNOWN."""
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_owned_unknown_conf",
            display_name_safe="Owned Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.UNKNOWN,  # Must not be upgraded to HIGH!
        )
        plan = self.resolver.resolve([obs])
        res = plan.resolutions[0]
        self.assertEqual(res.ownership_state, OwnershipState.OWNED)
        self.assertEqual(res.ownership_confidence, ConfidenceLevel.UNKNOWN)

    def test_42_malformed_existing_or_protected_parent_keys_fail_closed(self) -> None:
        """10. Malformed existing or protected-parent keys fail closed."""
        self.assertFalse(is_valid_protected_key("v0:12345"))
        self.assertFalse(is_valid_protected_key("v1:NOT_HEX_CHARS"))
        self.assertFalse(is_valid_protected_key("raw_account_number"))
        self.assertFalse(is_valid_protected_key(""))
        self.assertFalse(is_valid_protected_key(None))

        # Malformed parent key in observation
        obs_bad_parent = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_child",
            parent_protected_account_key="malformed_raw_parent_key",
            display_name_safe="Malformed Parent Child",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_bad_parent])
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertFalse(res.persistence_eligible)
        self.assertIsNone(res.protected_account_key)
        self.assertIsNone(res.parent_protected_account_key)

    def test_43_conflicting_duplicate_existing_states_fail_identically_under_reordering(self) -> None:
        """11. Conflicting duplicate existing states fail identically under reordered input."""
        prot_k = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_dup_test")
        ex1 = ExistingAccountState(
            protected_account_key=prot_k,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Name Variant 1",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        ex2 = ExistingAccountState(
            protected_account_key=prot_k,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name_safe="Name Variant 2",  # Conflict!
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_dup_test",
            display_name_safe="Name Variant 1",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
        )
        plan_order1 = self.resolver.resolve([obs], [ex1, ex2])
        plan_order2 = self.resolver.resolve([obs], [ex2, ex1])
        self.assertEqual(plan_order1.resolutions, plan_order2.resolutions)
        self.assertEqual(plan_order1.diagnostics, plan_order2.diagnostics)
        self.assertFalse(plan_order1.resolutions[0].persistence_eligible)
        self.assertEqual(plan_order1.resolutions[0].lifecycle_state, LifecycleState.UNVERIFIED)

    def test_44_security_raw_and_private_sentinels_absent_across_all_surfaces(self) -> None:
        """12. Raw/private sentinels cannot appear in output, diagnostics, exceptions, or repr."""
        sentinel_raw = "CONFIDENTIAL_RAW_KEY_SENTINEL_55555"
        sentinel_owner = "CONFIDENTIAL_OWNER_NAME_SENTINEL_66666"

        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=sentinel_raw,
            display_name_safe="Safe Account Label",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        plan = self.resolver.resolve([obs])
        self.assert_sentinel_absent(plan, sentinel_raw)
        self.assert_sentinel_absent(plan, sentinel_owner)
        self.assertNotIn(sentinel_raw, repr(obs))
        self.assertNotIn(sentinel_raw, repr(plan))


if __name__ == "__main__":
    unittest.main()
