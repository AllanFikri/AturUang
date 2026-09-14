"""
Pure account discovery resolver for universal ingestion.

Performs deterministic in-memory account discovery, lifecycle planning,
scoped hierarchy validation, and ownership preservation under Contract B.
Operates purely without database writes or ledger mutations.
Immediately protects all raw account identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
from typing import Any, Mapping, Sequence

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    LifecycleState,
    OwnershipState,
)
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
    is_valid_protected_key,
)

# Whitelist of allowed lifecycle evidence codes
ALLOWED_CLOSURE_EVIDENCE_CODES = frozenset({
    "OFFICIAL_CLOSURE_STATEMENT",
    "ACCOUNT_TERMINATION_NOTICE",
    "CUSTOMER_INITIATED_CLOSURE",
    "REGULATORY_REVOCATION",
})

ALLOWED_REUSE_EVIDENCE_CODES = frozenset({
    "EXPLICIT_PROVIDER_REASSIGNMENT",
    "RECYCLED_NUMBER_NEW_HOLDER_NOTICE",
})


def is_valid_calendar_date(d: Any) -> bool:
    """Strict canonical calendar date validator for YYYY-MM-DD."""
    if not isinstance(d, str):
        return False
    if len(d) != 10:
        return False
    parts = d.split("-")
    if (
        len(parts) != 3
        or len(parts[0]) != 4
        or len(parts[1]) != 2
        or len(parts[2]) != 2
    ):
        return False
    try:
        y, m, day = int(parts[0]), int(parts[1]), int(parts[2])
        datetime.date(y, m, day)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class AccountDiscoveryObservation:
    institution_id: str
    source_registry_id: str
    raw_account_key: str = field(repr=False)
    display_name_safe: str
    account_type: AccountType | str
    effective_date: str
    ownership_state: OwnershipState | str = OwnershipState.UNKNOWN
    ownership_confidence: ConfidenceLevel | str = ConfidenceLevel.UNKNOWN
    parent_raw_account_key: str | None = field(default=None, repr=False)
    parent_account_type: AccountType | str | None = None
    parent_protected_account_key: str | None = None
    explicit_lifecycle_signal: str | None = None
    lifecycle_authoritative: bool = False
    lifecycle_evidence_code: str | None = None

    def __repr__(self) -> str:
        return (
            f"AccountDiscoveryObservation("
            f"institution_id={self.institution_id!r}, "
            f"source_registry_id={self.source_registry_id!r}, "
            f"display_name_safe={self.display_name_safe!r}, "
            f"account_type={self.account_type!r}, "
            f"effective_date={self.effective_date!r}, "
            f"ownership_state={self.ownership_state!r}, "
            f"ownership_confidence={self.ownership_confidence!r}, "
            f"parent_account_type={self.parent_account_type!r}, "
            f"parent_protected_account_key={self.parent_protected_account_key!r}, "
            f"explicit_lifecycle_signal={self.explicit_lifecycle_signal!r}, "
            f"lifecycle_authoritative={self.lifecycle_authoritative!r}, "
            f"lifecycle_evidence_code={self.lifecycle_evidence_code!r})"
        )


@dataclass(frozen=True)
class ExistingAccountState:
    protected_account_key: str
    institution_id: str
    source_registry_id: str
    display_name_safe: str
    account_type: AccountType | str
    ownership_state: OwnershipState | str
    ownership_confidence: ConfidenceLevel | str
    lifecycle_state: LifecycleState | str
    effective_date: str
    parent_protected_account_key: str | None = None
    lifecycle_authoritative: bool = False
    lifecycle_evidence_code: str | None = None


@dataclass(frozen=True)
class AccountDiscoveryDiagnostic:
    code: str
    field_name: str | None
    message: str


@dataclass(frozen=True)
class AccountResolution:
    protected_account_key: str | None
    institution_id: str
    source_registry_id: str
    display_name_safe: str
    account_type: AccountType | str
    ownership_state: OwnershipState | str
    ownership_confidence: ConfidenceLevel | str
    lifecycle_state: LifecycleState | str
    effective_date: str
    parent_protected_account_key: str | None = None
    lifecycle_authoritative: bool = False
    lifecycle_evidence_code: str | None = None
    persistence_eligible: bool = False
    diagnostics: tuple[AccountDiscoveryDiagnostic, ...] = ()


@dataclass(frozen=True)
class AccountDiscoveryPlan:
    resolutions: tuple[AccountResolution, ...] = ()
    diagnostics: tuple[AccountDiscoveryDiagnostic, ...] = ()

    @property
    def has_diagnostics(self) -> bool:
        return len(self.diagnostics) > 0 or any(len(r.diagnostics) > 0 for r in self.resolutions)

    @property
    def persistence_eligible_resolutions(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.persistence_eligible)

    @property
    def new_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.NEW and r.persistence_eligible)

    @property
    def unchanged_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.UNCHANGED and r.persistence_eligible)

    @property
    def renamed_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.RENAMED and r.persistence_eligible)

    @property
    def closed_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.CLOSED and r.persistence_eligible)

    @property
    def reused_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.REUSED and r.persistence_eligible)

    @property
    def unverified_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.UNVERIFIED)


def _get_parent_scope(obs: AccountDiscoveryObservation) -> tuple[Any, ...] | None:
    if obs.parent_raw_account_key is not None:
        p_type_str = str(getattr(obs.parent_account_type, "value", obs.parent_account_type))
        return ("RAW", p_type_str, obs.parent_raw_account_key)
    elif obs.parent_protected_account_key is not None:
        return ("PROT", obs.parent_protected_account_key)
    return None


class AccountDiscoveryResolver:
    """
    Pure in-memory account discovery resolver with composite scope isolation
    and fail-closed security.
    """

    def __init__(self, protector: AccountIdentityProtector) -> None:
        if protector is None or not isinstance(protector, AccountIdentityProtector):
            raise ValueError("protector must be an AccountIdentityProtector instance")
        self._protector: AccountIdentityProtector = protector

    def resolve(
        self,
        observations: Sequence[AccountDiscoveryObservation],
        existing_accounts: Sequence[ExistingAccountState] | None = None,
    ) -> AccountDiscoveryPlan:
        plan_diagnostics: list[AccountDiscoveryDiagnostic] = []

        # =====================================================================
        # Phase 1: Existing-State Validation (Correction 8)
        # =====================================================================
        existing_map: dict[str, ExistingAccountState] = {}
        conflicting_existing_keys: set[str] = set()

        if existing_accounts:
            sorted_existing = sorted(
                existing_accounts,
                key=lambda e: (
                    str(e.protected_account_key),
                    str(e.institution_id),
                    str(e.source_registry_id),
                    str(e.display_name_safe),
                    str(e.account_type),
                    str(e.effective_date),
                ),
            )

            grouped_existing: dict[str, list[ExistingAccountState]] = {}
            for ex in sorted_existing:
                ex_diags: list[AccountDiscoveryDiagnostic] = []

                if not is_valid_protected_key(ex.protected_account_key):
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_PROTECTED_KEY",
                            field_name="protected_account_key",
                            message="Protected key format is invalid",
                        )
                    )

                if ex.parent_protected_account_key is not None and not is_valid_protected_key(ex.parent_protected_account_key):
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_PROTECTED_KEY",
                            field_name="parent_protected_account_key",
                            message="Parent protected key format is invalid",
                        )
                    )

                if not is_valid_calendar_date(ex.effective_date):
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_CALENDAR_DATE",
                            field_name="effective_date",
                            message="Canonical calendar date required",
                        )
                    )

                try:
                    AccountType(ex.account_type)
                except Exception:
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_ACCOUNT_TYPE",
                            field_name="account_type",
                            message="Invalid account type",
                        )
                    )

                try:
                    OwnershipState(ex.ownership_state)
                except Exception:
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_OWNERSHIP_STATE",
                            field_name="ownership_state",
                            message="Invalid ownership state",
                        )
                    )

                try:
                    ConfidenceLevel(ex.ownership_confidence)
                except Exception:
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_CONFIDENCE_LEVEL",
                            field_name="ownership_confidence",
                            message="Invalid confidence level",
                        )
                    )

                try:
                    LifecycleState(ex.lifecycle_state)
                except Exception:
                    ex_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="INVALID_LIFECYCLE_STATE",
                            field_name="lifecycle_state",
                            message="Invalid lifecycle state",
                        )
                    )

                if ex_diags:
                    plan_diagnostics.extend(ex_diags)
                    conflicting_existing_keys.add(ex.protected_account_key)
                else:
                    grouped_existing.setdefault(ex.protected_account_key, []).append(ex)

            # Check duplicate existing states
            for prot_key, states in grouped_existing.items():
                if len(states) == 1:
                    existing_map[prot_key] = states[0]
                else:
                    first = states[0]
                    is_conflict = False
                    for other in states[1:]:
                        if (
                            other.institution_id != first.institution_id
                            or other.source_registry_id != first.source_registry_id
                            or other.display_name_safe != first.display_name_safe
                            or other.account_type != first.account_type
                            or other.ownership_state != first.ownership_state
                            or other.ownership_confidence != first.ownership_confidence
                            or other.lifecycle_state != first.lifecycle_state
                            or other.effective_date != first.effective_date
                            or other.parent_protected_account_key != first.parent_protected_account_key
                            or other.lifecycle_authoritative != first.lifecycle_authoritative
                            or other.lifecycle_evidence_code != first.lifecycle_evidence_code
                        ):
                            is_conflict = True
                            break

                    if is_conflict:
                        diag = AccountDiscoveryDiagnostic(
                            code="CONFLICTING_EXISTING_STATE",
                            field_name="protected_account_key",
                            message="Conflicting duplicate existing account states detected",
                        )
                        plan_diagnostics.append(diag)
                        conflicting_existing_keys.add(prot_key)
                    else:
                        existing_map[prot_key] = first

        if not observations:
            return AccountDiscoveryPlan(resolutions=(), diagnostics=tuple(plan_diagnostics))

        # =====================================================================
        # Phase 2: Observation Validation & Composite Scope Isolation (Corrections 1, 3, 5, 6, 7)
        # =====================================================================
        valid_observations: list[AccountDiscoveryObservation] = []
        invalid_resolutions: list[AccountResolution] = []

        for obs in observations:
            obs_diags: list[AccountDiscoveryDiagnostic] = []

            if not obs.raw_account_key or not isinstance(obs.raw_account_key, str) or not obs.raw_account_key.strip():
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_RAW_KEY",
                        field_name="raw_account_key",
                        message="Missing or empty raw provider account key",
                    )
                )

            if not obs.institution_id or not isinstance(obs.institution_id, str) or not obs.institution_id.strip():
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="MISSING_INSTITUTION",
                        field_name="institution_id",
                        message="Institution scope is required",
                    )
                )

            if not obs.source_registry_id or not isinstance(obs.source_registry_id, str) or not obs.source_registry_id.strip():
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="MISSING_SOURCE_REGISTRY",
                        field_name="source_registry_id",
                        message="Source registry scope is required",
                    )
                )

            try:
                AccountType(obs.account_type)
            except Exception:
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_ACCOUNT_TYPE",
                        field_name="account_type",
                        message="Invalid account type",
                    )
                )

            if not obs.display_name_safe or not isinstance(obs.display_name_safe, str) or not obs.display_name_safe.strip():
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_DISPLAY_NAME",
                        field_name="display_name_safe",
                        message="Safe display name is required",
                    )
                )

            if not is_valid_calendar_date(obs.effective_date):
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_CALENDAR_DATE",
                        field_name="effective_date",
                        message="Canonical calendar date required",
                    )
                )

            try:
                OwnershipState(obs.ownership_state)
            except Exception:
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_OWNERSHIP_STATE",
                        field_name="ownership_state",
                        message="Invalid ownership state",
                    )
                )

            try:
                ConfidenceLevel(obs.ownership_confidence)
            except Exception:
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_CONFIDENCE_LEVEL",
                        field_name="ownership_confidence",
                        message="Invalid confidence level",
                    )
                )

            if obs.explicit_lifecycle_signal is not None:
                supported_signals = {"CLOSED", "REUSED", "NEW", "UNCHANGED", "RENAMED"}
                if obs.explicit_lifecycle_signal not in supported_signals:
                    obs_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="UNSUPPORTED_LIFECYCLE_SIGNAL",
                            field_name="explicit_lifecycle_signal",
                            message="Unsupported lifecycle signal",
                        )
                    )

            if obs.parent_protected_account_key is not None and not is_valid_protected_key(obs.parent_protected_account_key):
                obs_diags.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_PROTECTED_KEY",
                        field_name="parent_protected_account_key",
                        message="Parent protected key format is invalid",
                    )
                )

            if obs.parent_raw_account_key is not None:
                if not obs.parent_account_type:
                    obs_diags.append(
                        AccountDiscoveryDiagnostic(
                            code="MISSING_PARENT_SCOPE",
                            field_name="parent_account_type",
                            message="Parent account type is required for raw parent reference",
                        )
                    )
                else:
                    try:
                        AccountType(obs.parent_account_type)
                    except Exception:
                        obs_diags.append(
                            AccountDiscoveryDiagnostic(
                                code="INVALID_ACCOUNT_TYPE",
                                field_name="parent_account_type",
                                message="Invalid parent account type",
                            )
                        )

            if obs_diags:
                plan_diagnostics.extend(obs_diags)
                invalid_resolutions.append(
                    AccountResolution(
                        protected_account_key=None,
                        institution_id=obs.institution_id or "",
                        source_registry_id=obs.source_registry_id or "",
                        display_name_safe=obs.display_name_safe or "",
                        account_type=obs.account_type or AccountType.OTHER,
                        ownership_state=OwnershipState.UNKNOWN,
                        ownership_confidence=ConfidenceLevel.UNKNOWN,
                        lifecycle_state=LifecycleState.UNVERIFIED,
                        effective_date=obs.effective_date or "",
                        parent_protected_account_key=None,
                        persistence_eligible=False,
                        diagnostics=tuple(obs_diags),
                    )
                )
            else:
                valid_observations.append(obs)

        # =====================================================================
        # Phase 3: Scoped Hierarchy & Deterministic Fingerprinting (Corrections 1, 2, 9)
        # =====================================================================
        composite_obs_map: dict[tuple[str, str, str, str, tuple[Any, ...] | None], list[AccountDiscoveryObservation]] = {}
        for obs in valid_observations:
            acct_type_str = str(getattr(obs.account_type, "value", obs.account_type))
            parent_scope = _get_parent_scope(obs)
            key = (obs.institution_id, obs.source_registry_id, acct_type_str, obs.raw_account_key, parent_scope)
            composite_obs_map.setdefault(key, []).append(obs)

        computed_protected_keys: dict[tuple[str, str, str, str, tuple[Any, ...] | None], str | None] = {}
        prot_key_to_parent_prot: dict[str, str | None] = {}
        hierarchy_diagnostics: dict[tuple[str, str, str, str, tuple[Any, ...] | None], list[AccountDiscoveryDiagnostic]] = {}

        def resolve_scope_key(
            scope_key: tuple[str, str, str, str, tuple[Any, ...] | None],
            visited_chain: list[tuple[str, str, str, str, tuple[Any, ...] | None]],
        ) -> str | None:
            if scope_key in computed_protected_keys:
                return computed_protected_keys[scope_key]

            rep_obs = composite_obs_map[scope_key][0]
            inst_id, src_id, acct_t_str, raw_k, p_scope = scope_key

            parent_prot_key: str | None = None

            if rep_obs.parent_raw_account_key is not None:
                p_raw = rep_obs.parent_raw_account_key
                p_type_str = str(getattr(rep_obs.parent_account_type, "value", rep_obs.parent_account_type))

                # Self-parenting check
                if p_raw == raw_k and p_type_str == acct_t_str:
                    diag = AccountDiscoveryDiagnostic(
                        code="SELF_PARENTING",
                        field_name="parent_account_key",
                        message="Self-parenting is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                # Find candidate parent scopes matching (inst_id, src_id, p_type_str, p_raw)
                matching_parents = [
                    sk for sk in composite_obs_map.keys()
                    if sk[0] == inst_id and sk[1] == src_id and sk[2] == p_type_str and sk[3] == p_raw
                ]

                if not matching_parents:
                    diag = AccountDiscoveryDiagnostic(
                        code="MISSING_PARENT",
                        field_name="parent_account_key",
                        message="Missing parent identity for a required child",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                parent_scope_key = matching_parents[0]

                if parent_scope_key in visited_chain:
                    diag = AccountDiscoveryDiagnostic(
                        code="HIERARCHY_CYCLE",
                        field_name="parent_account_key",
                        message="Hierarchy cycle detected",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                parent_prot_key = resolve_scope_key(parent_scope_key, visited_chain + [scope_key])
                if parent_prot_key is None:
                    diag = AccountDiscoveryDiagnostic(
                        code="PARENT_RESOLUTION_FAILED",
                        field_name="parent_account_key",
                        message="Parent account resolution failed",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

            elif rep_obs.parent_protected_account_key is not None:
                p_prot = rep_obs.parent_protected_account_key
                if not is_valid_protected_key(p_prot):
                    diag = AccountDiscoveryDiagnostic(
                        code="INVALID_PROTECTED_KEY",
                        field_name="parent_protected_account_key",
                        message="Parent protected key format is invalid",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                parent_ex = existing_map.get(p_prot)
                if not parent_ex:
                    diag = AccountDiscoveryDiagnostic(
                        code="MISSING_PARENT",
                        field_name="parent_protected_account_key",
                        message="Missing parent identity for a required child",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                if parent_ex.institution_id != inst_id:
                    diag = AccountDiscoveryDiagnostic(
                        code="CROSS_INSTITUTION_PARENT",
                        field_name="parent_account_key",
                        message="Cross-institution parent relationship is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                if parent_ex.source_registry_id != src_id:
                    diag = AccountDiscoveryDiagnostic(
                        code="CROSS_PROVIDER_PARENT",
                        field_name="parent_account_key",
                        message="Cross-provider parent relationship is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(scope_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    computed_protected_keys[scope_key] = None
                    return None

                parent_prot_key = p_prot

            prot_key = self._protector.protect_account_key(
                inst_id,
                src_id,
                acct_t_str,
                raw_k,
                parent_prot_key,
            )
            computed_protected_keys[scope_key] = prot_key
            prot_key_to_parent_prot[prot_key] = parent_prot_key
            return prot_key

        for sk in list(composite_obs_map.keys()):
            resolve_scope_key(sk, [])

        # =====================================================================
        # Phase 4: Grouping by Protected Key & Ephemeral Raw Eviction
        # =====================================================================
        resolved_groups: dict[str, list[AccountDiscoveryObservation]] = {}
        failed_scopes: list[tuple[tuple[str, str, str, str, tuple[Any, ...] | None], list[AccountDiscoveryObservation]]] = []

        for sk, obs_list in composite_obs_map.items():
            prot_k = computed_protected_keys.get(sk)
            if prot_k is None:
                failed_scopes.append((sk, obs_list))
            else:
                resolved_groups.setdefault(prot_k, []).extend(obs_list)

        del composite_obs_map
        del computed_protected_keys

        resolutions: list[AccountResolution] = list(invalid_resolutions)

        for sk, obs_list in failed_scopes:
            inst_id, src_id, acct_t_str, _, _ = sk
            rep = obs_list[0]
            diags = hierarchy_diagnostics.get(sk, [])
            resolutions.append(
                AccountResolution(
                    protected_account_key=None,
                    institution_id=inst_id,
                    source_registry_id=src_id,
                    display_name_safe=rep.display_name_safe,
                    account_type=rep.account_type,
                    ownership_state=rep.ownership_state,
                    ownership_confidence=rep.ownership_confidence,
                    lifecycle_state=LifecycleState.UNVERIFIED,
                    effective_date=rep.effective_date,
                    parent_protected_account_key=None,
                    persistence_eligible=False,
                    diagnostics=tuple(diags),
                )
            )

        del failed_scopes
        del hierarchy_diagnostics

        # =====================================================================
        # Phase 5: Same-Date Conflicts & Lifecycle Planning (Corrections 4, 5, 10, 11)
        # =====================================================================
        for prot_key, obs_group in resolved_groups.items():
            group_diagnostics: list[AccountDiscoveryDiagnostic] = []

            sorted_obs = sorted(
                obs_group,
                key=lambda o: (
                    o.effective_date,
                    o.display_name_safe,
                    str(getattr(o.account_type, "value", o.account_type)),
                    str(o.ownership_state),
                    str(o.ownership_confidence),
                ),
            )

            by_date: dict[str, list[AccountDiscoveryObservation]] = {}
            for o in sorted_obs:
                by_date.setdefault(o.effective_date, []).append(o)

            has_same_date_conflict = False
            for d, d_obs in by_date.items():
                if len(d_obs) > 1:
                    first = d_obs[0]
                    for other in d_obs[1:]:
                        if (
                            other.display_name_safe != first.display_name_safe
                            or other.account_type != first.account_type
                            or other.ownership_state != first.ownership_state
                            or other.ownership_confidence != first.ownership_confidence
                            or other.explicit_lifecycle_signal != first.explicit_lifecycle_signal
                            or other.lifecycle_authoritative != first.lifecycle_authoritative
                            or other.lifecycle_evidence_code != first.lifecycle_evidence_code
                            or other.parent_protected_account_key != first.parent_protected_account_key
                            or other.parent_account_type != first.parent_account_type
                        ):
                            has_same_date_conflict = True
                            diag = AccountDiscoveryDiagnostic(
                                code="CONFLICTING_OBSERVATIONS",
                                field_name="effective_date",
                                message="Conflicting observations detected for the same effective date",
                            )
                            group_diagnostics.append(diag)
                            plan_diagnostics.append(diag)
                            break
                    if has_same_date_conflict:
                        break

            latest_obs = sorted_obs[-1]
            existing = existing_map.get(prot_key)

            if prot_key in conflicting_existing_keys or has_same_date_conflict:
                resolutions.append(
                    AccountResolution(
                        protected_account_key=None,
                        institution_id=latest_obs.institution_id,
                        source_registry_id=latest_obs.source_registry_id,
                        display_name_safe=latest_obs.display_name_safe,
                        account_type=latest_obs.account_type,
                        ownership_state=latest_obs.ownership_state,
                        ownership_confidence=latest_obs.ownership_confidence,
                        lifecycle_state=LifecycleState.UNVERIFIED,
                        effective_date=latest_obs.effective_date,
                        parent_protected_account_key=None,
                        persistence_eligible=False,
                        diagnostics=tuple(group_diagnostics),
                    )
                )
                continue

            # Out-of-order check (Correction 4)
            if existing is not None and latest_obs.effective_date < existing.effective_date:
                resolutions.append(
                    AccountResolution(
                        protected_account_key=existing.protected_account_key,
                        institution_id=existing.institution_id,
                        source_registry_id=existing.source_registry_id,
                        display_name_safe=existing.display_name_safe,
                        account_type=existing.account_type,
                        ownership_state=existing.ownership_state,
                        ownership_confidence=existing.ownership_confidence,
                        lifecycle_state=LifecycleState.UNCHANGED,
                        effective_date=existing.effective_date,
                        parent_protected_account_key=existing.parent_protected_account_key,
                        lifecycle_authoritative=existing.lifecycle_authoritative,
                        lifecycle_evidence_code=existing.lifecycle_evidence_code,
                        persistence_eligible=True,
                        diagnostics=(),
                    )
                )
                continue

            res_parent_prot: str | None = (
                prot_key_to_parent_prot.get(prot_key)
                or latest_obs.parent_protected_account_key
            )
            if not res_parent_prot and existing:
                res_parent_prot = existing.parent_protected_account_key

            res_ownership = latest_obs.ownership_state
            res_confidence = latest_obs.ownership_confidence

            res_lifecycle: LifecycleState
            res_authoritative = latest_obs.lifecycle_authoritative
            res_evidence_code = latest_obs.lifecycle_evidence_code
            persistence_eligible = True

            if existing is None:
                if latest_obs.explicit_lifecycle_signal == "CLOSED":
                    if latest_obs.lifecycle_authoritative and latest_obs.lifecycle_evidence_code in ALLOWED_CLOSURE_EVIDENCE_CODES:
                        res_lifecycle = LifecycleState.CLOSED
                    else:
                        res_lifecycle = LifecycleState.UNVERIFIED
                        persistence_eligible = False
                        diag = AccountDiscoveryDiagnostic(
                            code="UNAUTHORITATIVE_LIFECYCLE_SIGNAL",
                            field_name="lifecycle_evidence_code",
                            message="Closure or reuse signal requires authoritative evidence code",
                        )
                        group_diagnostics.append(diag)
                        plan_diagnostics.append(diag)
                else:
                    res_lifecycle = LifecycleState.NEW
            else:
                if existing.lifecycle_state == LifecycleState.CLOSED:
                    if (
                        latest_obs.explicit_lifecycle_signal == "REUSED"
                        and latest_obs.lifecycle_authoritative
                        and latest_obs.lifecycle_evidence_code in ALLOWED_REUSE_EVIDENCE_CODES
                    ):
                        res_lifecycle = LifecycleState.REUSED
                    else:
                        res_lifecycle = LifecycleState.UNVERIFIED
                        persistence_eligible = False
                        diag = AccountDiscoveryDiagnostic(
                            code="AMBIGUOUS_REUSE",
                            field_name="lifecycle_state",
                            message="Ambiguous provider account slot reuse requires verification",
                        )
                        group_diagnostics.append(diag)
                        plan_diagnostics.append(diag)
                elif latest_obs.explicit_lifecycle_signal == "CLOSED":
                    if (
                        latest_obs.lifecycle_authoritative
                        and latest_obs.lifecycle_evidence_code in ALLOWED_CLOSURE_EVIDENCE_CODES
                    ):
                        res_lifecycle = LifecycleState.CLOSED
                    else:
                        res_lifecycle = LifecycleState.UNVERIFIED
                        persistence_eligible = False
                        diag = AccountDiscoveryDiagnostic(
                            code="UNAUTHORITATIVE_LIFECYCLE_SIGNAL",
                            field_name="lifecycle_evidence_code",
                            message="Closure or reuse signal requires authoritative evidence code",
                        )
                        group_diagnostics.append(diag)
                        plan_diagnostics.append(diag)
                elif latest_obs.display_name_safe != existing.display_name_safe:
                    res_lifecycle = LifecycleState.RENAMED
                else:
                    res_lifecycle = LifecycleState.UNCHANGED

            resolutions.append(
                AccountResolution(
                    protected_account_key=prot_key if persistence_eligible else None,
                    institution_id=latest_obs.institution_id,
                    source_registry_id=latest_obs.source_registry_id,
                    display_name_safe=latest_obs.display_name_safe,
                    account_type=latest_obs.account_type,
                    ownership_state=res_ownership,
                    ownership_confidence=res_confidence,
                    lifecycle_state=res_lifecycle,
                    effective_date=latest_obs.effective_date,
                    parent_protected_account_key=res_parent_prot,
                    lifecycle_authoritative=res_authoritative,
                    lifecycle_evidence_code=res_evidence_code,
                    persistence_eligible=persistence_eligible,
                    diagnostics=tuple(group_diagnostics),
                )
            )

        sorted_resolutions = tuple(
            sorted(
                resolutions,
                key=lambda r: (
                    r.institution_id,
                    r.source_registry_id,
                    r.protected_account_key or "",
                    r.display_name_safe,
                ),
            )
        )

        return AccountDiscoveryPlan(
            resolutions=sorted_resolutions,
            diagnostics=tuple(plan_diagnostics),
        )
