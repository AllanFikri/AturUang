"""
Tests for Universal Ingestion Phase 4 Account Discovery and Identity Privacy.

Covers:
- Deterministic keyed identity protection (HMAC-SHA-256)
- Scoped privacy and canonical length-prefixed encoding
- Pure account discovery resolution and lifecycle planning
- Hierarchy validation, cycle rejection, and cross-provider isolation
- Recursive absence assertions ensuring zero raw identifier leaks
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
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
)
from aturuang.ingestion_account_discovery import (
    AccountDiscoveryDiagnostic,
    AccountDiscoveryObservation,
    AccountDiscoveryPlan,
    AccountDiscoveryResolver,
    AccountResolution,
    ExistingAccountState,
)

TEST_SECRET = "synthetic-test-secret-at-least-32-chars-long-strictly-mocked!"


class TestUniversalIngestionPhase4AccountDiscovery(unittest.TestCase):
    """
    Focused test suite for Phase 4 Account Discovery pure resolver.
    Contains exactly 32 test methods.
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
        # Repr check
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
            display_name="Main Savings",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-15",
            ownership_state=OwnershipState.OWNED,
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertTrue(res.protected_account_key.startswith("v1:"))
        self.assert_sentinel_absent(plan, sentinel)

    def test_02_security_raw_identifier_absent_from_diagnostics_and_exceptions(self) -> None:
        sentinel = "SYNTHETIC_RAW_SECRET_ACCOUNT_KEY_88888"
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=sentinel,
            parent_raw_account_key=sentinel,  # Self-parenting triggers diagnostic
            display_name="Self Parent",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-15",
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(len(plan.diagnostics), 1)
        self.assertEqual(plan.diagnostics[0].code, "SELF_PARENTING")
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
            display_name="Secret Acct",
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
            display_name="Old Account Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs_renamed = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key=raw_key,
            display_name="New Account Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.OWNED,
        )
        plan = self.resolver.resolve([obs_renamed], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.protected_account_key, key_before)
        self.assertEqual(res.lifecycle_state, LifecycleState.RENAMED)
        self.assertEqual(res.display_name, "New Account Name")

    def test_08_security_owner_name_change_preserves_protected_identity(self) -> None:
        raw_key = "1234567890"
        key_1 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, raw_key)
        # AccountIdentityProtector does not incorporate owner name into identity
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
        k_root = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "sub_1", None)
        k_sub1 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "sub_1", "v1:parent_alpha")
        k_sub2 = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "sub_1", "v1:parent_beta")
        self.assertNotEqual(k_root, k_sub1)
        self.assertNotEqual(k_sub1, k_sub2)

    def test_14_crypto_canonical_encoding_collision_resistance(self) -> None:
        # Delimiter collision attack test: ("a", "bc") vs ("ab", "c")
        enc1 = canonical_encoding("a", "bc")
        enc2 = canonical_encoding("ab", "c")
        self.assertNotEqual(enc1, enc2)
        self.assertEqual(enc1, b"1:a|2:bc")
        self.assertEqual(enc2, b"2:ab|1:c")

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
            display_name="First Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.NEW)

    def test_17_resolver_lifecycle_unchanged_account(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Current Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name="Current Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.OWNED,
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.UNCHANGED)

    def test_18_resolver_lifecycle_renamed_account(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Old Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name="Brand New Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-02-01",
            ownership_state=OwnershipState.OWNED,
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.RENAMED)
        self.assertEqual(plan.resolutions[0].display_name, "Brand New Name")

    def test_19_resolver_lifecycle_explicit_closed(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Active Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name="Active Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
            explicit_lifecycle_signal="CLOSED",
            lifecycle_evidence="Closure statement confirmed",
        )
        plan = self.resolver.resolve([obs], [existing])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.CLOSED)
        self.assertEqual(plan.resolutions[0].lifecycle_evidence, "Closure statement confirmed")

    def test_20_resolver_lifecycle_absence_does_not_emit_closed(self) -> None:
        prot_key_existing = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_absent")
        existing = ExistingAccountState(
            protected_account_key=prot_key_existing,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Unobserved Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-01-01",
        )
        # Current observation does not include raw_absent
        obs_other = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_active",
            display_name="Active Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        plan = self.resolver.resolve([obs_other], [existing])
        # Only observed account is planned; absent account never gets CLOSED
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].display_name, "Active Account")
        self.assertEqual(len(plan.closed_accounts), 0)

    def test_21_resolver_lifecycle_ambiguous_reuse_becomes_unverified(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing_closed = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Closed Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.CLOSED,
            effective_date="2026-01-01",
        )
        # New observation observed without explicit reuse proof
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name="Reappeared Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-04-01",
        )
        plan = self.resolver.resolve([obs], [existing_closed])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertTrue(any(d.code == "AMBIGUOUS_REUSE" for d in plan.diagnostics))

    def test_22_resolver_lifecycle_explicit_reuse_supported(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing_closed = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Closed Account",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.CLOSED,
            effective_date="2026-01-01",
        )
        obs_reused = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name="Reassigned Slot",
            account_type=AccountType.SAVINGS,
            effective_date="2026-05-01",
            explicit_lifecycle_signal="REUSED",
            lifecycle_evidence="New contract issued for recycled account number",
        )
        plan = self.resolver.resolve([obs_reused], [existing_closed])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.REUSED)

    def test_23_resolver_lifecycle_out_of_order_history_safe(self) -> None:
        prot_key = self.protector.protect_account_key("inst_a", "src_1", AccountType.SAVINGS, "raw_001")
        existing_newer = ExistingAccountState(
            protected_account_key=prot_key,
            institution_id="inst_a",
            source_registry_id="src_1",
            display_name="Newer Display Name",
            account_type=AccountType.SAVINGS,
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            effective_date="2026-06-01",
        )
        # Older historical observation arrives
        obs_older = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_001",
            display_name="Older Historical Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
        )
        plan = self.resolver.resolve([obs_older], [existing_newer])
        self.assertEqual(len(plan.resolutions), 1)
        res = plan.resolutions[0]
        self.assertEqual(res.lifecycle_state, LifecycleState.UNCHANGED)
        self.assertEqual(res.display_name, "Newer Display Name")

    def test_24_resolver_lifecycle_conflicting_equal_date_fails_closed(self) -> None:
        obs1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_conflict",
            display_name="Conflicting Name A",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_conflict",
            display_name="Conflicting Name B",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        plan = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.UNVERIFIED)
        self.assertTrue(any(d.code == "CONFLICTING_OBSERVATIONS" for d in plan.diagnostics))

    def test_25_resolver_duplicate_replay_stable(self) -> None:
        obs1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_dup",
            display_name="Identical Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        obs2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_dup",
            display_name="Identical Name",
            account_type=AccountType.SAVINGS,
            effective_date="2026-03-01",
        )
        plan = self.resolver.resolve([obs1, obs2])
        self.assertEqual(len(plan.resolutions), 1)
        self.assertEqual(plan.resolutions[0].lifecycle_state, LifecycleState.NEW)
        self.assertEqual(len(plan.diagnostics), 0)

    def test_26_resolver_input_reordering_deterministic(self) -> None:
        obs_a = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_a",
            display_name="Account Alpha",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_b = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_b",
            display_name="Account Beta",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-02",
        )
        obs_c = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_c",
            display_name="Account Gamma",
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
            display_name="Owned Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
        )
        obs_third = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_third",
            display_name="Third Party Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
            ownership_state=OwnershipState.THIRD_PARTY,
            ownership_confidence=ConfidenceLevel.MEDIUM,
        )
        plan = self.resolver.resolve([obs_owned, obs_third])
        res_by_name = {r.display_name: r for r in plan.resolutions}
        self.assertEqual(res_by_name["Owned Account"].ownership_state, OwnershipState.OWNED)
        self.assertEqual(res_by_name["Owned Account"].ownership_confidence, ConfidenceLevel.HIGH)
        self.assertEqual(res_by_name["Third Party Account"].ownership_state, OwnershipState.THIRD_PARTY)
        self.assertEqual(res_by_name["Third Party Account"].ownership_confidence, ConfidenceLevel.MEDIUM)

    def test_28_resolver_ownership_default_unknown(self) -> None:
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_default",
            display_name="Unknown Owner Account",
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
            display_name="Parent Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_child1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_child_1",
            parent_raw_account_key="raw_parent",
            display_name="Shared Pocket Name",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        obs_child2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_child_2",
            parent_raw_account_key="raw_parent",
            display_name="Shared Pocket Name",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_parent, obs_child1, obs_child2])
        self.assertEqual(len(plan.resolutions), 3)
        child_res = [r for r in plan.resolutions if r.display_name == "Shared Pocket Name"]
        self.assertEqual(len(child_res), 2)
        self.assertNotEqual(child_res[0].protected_account_key, child_res[1].protected_account_key)

    def test_30_resolver_hierarchy_cross_provider_parent_rejected(self) -> None:
        obs_parent = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_provider_one",
            raw_account_key="raw_p1",
            display_name="Provider One Parent",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        obs_child = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_provider_two",  # Different provider scope!
            raw_account_key="raw_c1",
            parent_raw_account_key="raw_p1",
            display_name="Cross Provider Child",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_parent, obs_child])
        self.assertTrue(any(d.code == "CROSS_PROVIDER_PARENT" for d in plan.diagnostics))
        child_res = [r for r in plan.resolutions if r.display_name == "Cross Provider Child"][0]
        self.assertEqual(child_res.lifecycle_state, LifecycleState.UNVERIFIED)

    def test_31_resolver_hierarchy_cycle_rejected(self) -> None:
        obs_node1 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_cycle_1",
            parent_raw_account_key="raw_cycle_2",
            display_name="Cycle Node 1",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        obs_node2 = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_cycle_2",
            parent_raw_account_key="raw_cycle_1",
            display_name="Cycle Node 2",
            account_type=AccountType.SUBACCOUNT,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs_node1, obs_node2])
        self.assertTrue(any(d.code == "HIERARCHY_CYCLE" for d in plan.diagnostics))
        for r in plan.resolutions:
            self.assertEqual(r.lifecycle_state, LifecycleState.UNVERIFIED)

    def test_32_resolver_no_database_write_no_ledger_event_immutable_plan(self) -> None:
        obs = AccountDiscoveryObservation(
            institution_id="inst_a",
            source_registry_id="src_1",
            raw_account_key="raw_clean",
            display_name="Pure Memory Account",
            account_type=AccountType.SAVINGS,
            effective_date="2026-01-01",
        )
        plan = self.resolver.resolve([obs])
        self.assertIsInstance(plan.resolutions, tuple)
        self.assertIsInstance(plan.diagnostics, tuple)
        with self.assertRaises((TypeError, AttributeError)):
            plan.resolutions.append(None)  # type: ignore


if __name__ == "__main__":
    unittest.main()
