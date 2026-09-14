"""
Pure account discovery resolver for universal ingestion.

Performs deterministic in-memory account discovery, lifecycle planning,
hierarchy validation, and ownership preservation under Contract B.
Operates purely without database writes or ledger mutations.
Immediately protects all raw account identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import re
from typing import Any, Mapping, Sequence

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    LifecycleState,
    OwnershipState,
)
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
)


@dataclass(frozen=True)
class AccountDiscoveryObservation:
    institution_id: str
    source_registry_id: str
    raw_account_key: str = field(repr=False)
    display_name: str
    account_type: AccountType | str
    effective_date: str
    ownership_state: OwnershipState | str = OwnershipState.UNKNOWN
    parent_raw_account_key: str | None = field(default=None, repr=False)
    parent_protected_account_key: str | None = None
    explicit_lifecycle_signal: str | None = None
    lifecycle_evidence: str | None = None
    ownership_confidence: ConfidenceLevel | str = ConfidenceLevel.UNKNOWN

    def __repr__(self) -> str:
        return (
            f"AccountDiscoveryObservation("
            f"institution_id={self.institution_id!r}, "
            f"source_registry_id={self.source_registry_id!r}, "
            f"display_name={self.display_name!r}, "
            f"account_type={self.account_type!r}, "
            f"effective_date={self.effective_date!r}, "
            f"ownership_state={self.ownership_state!r}, "
            f"parent_protected_account_key={self.parent_protected_account_key!r}, "
            f"explicit_lifecycle_signal={self.explicit_lifecycle_signal!r})"
        )


@dataclass(frozen=True)
class ExistingAccountState:
    protected_account_key: str
    institution_id: str
    source_registry_id: str
    display_name: str
    account_type: AccountType | str
    ownership_state: OwnershipState | str
    lifecycle_state: LifecycleState | str
    effective_date: str
    parent_protected_account_key: str | None = None
    ownership_confidence: ConfidenceLevel | str = ConfidenceLevel.UNKNOWN


@dataclass(frozen=True)
class AccountDiscoveryDiagnostic:
    code: str
    field_name: str | None
    message: str


@dataclass(frozen=True)
class AccountResolution:
    protected_account_key: str
    institution_id: str
    source_registry_id: str
    display_name: str
    account_type: AccountType | str
    ownership_state: OwnershipState | str
    lifecycle_state: LifecycleState | str
    effective_date: str
    parent_protected_account_key: str | None = None
    ownership_confidence: ConfidenceLevel | str = ConfidenceLevel.UNKNOWN
    lifecycle_evidence: str | None = None
    diagnostics: tuple[AccountDiscoveryDiagnostic, ...] = ()


@dataclass(frozen=True)
class AccountDiscoveryPlan:
    resolutions: tuple[AccountResolution, ...] = ()
    diagnostics: tuple[AccountDiscoveryDiagnostic, ...] = ()

    @property
    def has_diagnostics(self) -> bool:
        return len(self.diagnostics) > 0 or any(len(r.diagnostics) > 0 for r in self.resolutions)

    @property
    def new_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.NEW)

    @property
    def unchanged_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.UNCHANGED)

    @property
    def renamed_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.RENAMED)

    @property
    def closed_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.CLOSED)

    @property
    def reused_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.REUSED)

    @property
    def unverified_accounts(self) -> tuple[AccountResolution, ...]:
        return tuple(r for r in self.resolutions if r.lifecycle_state == LifecycleState.UNVERIFIED)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T\s].*)?$")


def _is_valid_date(d: str) -> bool:
    if not d or not isinstance(d, str):
        return False
    return bool(_DATE_RE.match(d.strip()))


class AccountDiscoveryResolver:
    """
    Pure in-memory account discovery resolver.

    Takes an injected AccountIdentityProtector, immediately protects raw
    identifiers, validates hierarchy and lifecycle, and returns an immutable
    deterministic plan containing only protected identifiers.
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
        if not observations:
            return AccountDiscoveryPlan(resolutions=(), diagnostics=())

        existing_map: dict[str, ExistingAccountState] = {}
        if existing_accounts:
            for ex in existing_accounts:
                existing_map[ex.protected_account_key] = ex

        # Step 1: Input validation and grouping
        raw_obs_map: dict[str, list[AccountDiscoveryObservation]] = {}
        plan_diagnostics: list[AccountDiscoveryDiagnostic] = []

        valid_observations: list[AccountDiscoveryObservation] = []
        invalid_resolutions: list[AccountResolution] = []

        for obs in observations:
            obs_diagnostics: list[AccountDiscoveryDiagnostic] = []

            # Check raw_account_key
            if not obs.raw_account_key or not str(obs.raw_account_key).strip():
                obs_diagnostics.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_RAW_KEY",
                        field_name="raw_account_key",
                        message="Missing or empty raw provider account key",
                    )
                )

            # Check institution_id
            if not obs.institution_id or not str(obs.institution_id).strip():
                obs_diagnostics.append(
                    AccountDiscoveryDiagnostic(
                        code="MISSING_INSTITUTION",
                        field_name="institution_id",
                        message="Institution scope is required",
                    )
                )

            # Check source_registry_id
            if not obs.source_registry_id or not str(obs.source_registry_id).strip():
                obs_diagnostics.append(
                    AccountDiscoveryDiagnostic(
                        code="MISSING_SOURCE_REGISTRY",
                        field_name="source_registry_id",
                        message="Source registry scope is required",
                    )
                )

            # Check account_type
            try:
                AccountType(obs.account_type)
            except Exception:
                obs_diagnostics.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_ACCOUNT_TYPE",
                        field_name="account_type",
                        message="Invalid account type",
                    )
                )

            # Check effective_date
            if not _is_valid_date(obs.effective_date):
                obs_diagnostics.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_EFFECTIVE_DATE",
                        field_name="effective_date",
                        message="Invalid effective date",
                    )
                )

            # Check ownership_state
            try:
                OwnershipState(obs.ownership_state)
            except Exception:
                obs_diagnostics.append(
                    AccountDiscoveryDiagnostic(
                        code="INVALID_OWNERSHIP_STATE",
                        field_name="ownership_state",
                        message="Invalid ownership state",
                    )
                )

            # Check unsupported lifecycle signal
            if obs.explicit_lifecycle_signal is not None:
                supported_signals = {"CLOSED", "REUSED", "NEW", "UNCHANGED", "RENAMED"}
                if obs.explicit_lifecycle_signal not in supported_signals:
                    obs_diagnostics.append(
                        AccountDiscoveryDiagnostic(
                            code="UNSUPPORTED_LIFECYCLE_SIGNAL",
                            field_name="explicit_lifecycle_signal",
                            message="Unsupported lifecycle signal",
                        )
                    )

            if obs_diagnostics:
                plan_diagnostics.extend(obs_diagnostics)
                fallback_key = "v1:unverified"
                if obs.raw_account_key and obs.institution_id and obs.source_registry_id:
                    try:
                        fallback_key = self._protector.protect_account_key(
                            obs.institution_id,
                            obs.source_registry_id,
                            str(getattr(obs.account_type, "value", obs.account_type)),
                            obs.raw_account_key,
                            None,
                        )
                    except Exception:
                        pass

                invalid_resolutions.append(
                    AccountResolution(
                        protected_account_key=fallback_key,
                        institution_id=obs.institution_id or "",
                        source_registry_id=obs.source_registry_id or "",
                        display_name=obs.display_name or "",
                        account_type=obs.account_type or AccountType.OTHER,
                        ownership_state=OwnershipState.UNKNOWN,
                        lifecycle_state=LifecycleState.UNVERIFIED,
                        effective_date=obs.effective_date or "",
                        diagnostics=tuple(obs_diagnostics),
                    )
                )
            else:
                valid_observations.append(obs)
                raw_obs_map.setdefault(obs.raw_account_key, []).append(obs)

        # Step 2: Hierarchy validation and immediate protected key derivation
        hierarchy_diagnostics: dict[str, list[AccountDiscoveryDiagnostic]] = {}

        for raw_key, obs_list in raw_obs_map.items():
            rep = obs_list[0]
            p_raw = rep.parent_raw_account_key
            p_prot = rep.parent_protected_account_key

            if p_raw:
                if p_raw == raw_key:
                    diag = AccountDiscoveryDiagnostic(
                        code="SELF_PARENTING",
                        field_name="parent_account_key",
                        message="Self-parenting is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

                # Detect cycles
                visited = {raw_key}
                curr = p_raw
                has_cycle = False
                while curr:
                    if curr in visited:
                        has_cycle = True
                        break
                    visited.add(curr)
                    p_obs_list = raw_obs_map.get(curr)
                    if p_obs_list:
                        curr = p_obs_list[0].parent_raw_account_key
                    else:
                        break

                if has_cycle:
                    diag = AccountDiscoveryDiagnostic(
                        code="HIERARCHY_CYCLE",
                        field_name="parent_account_key",
                        message="Hierarchy cycle detected",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

                # Check if parent exists
                if p_raw not in raw_obs_map:
                    diag = AccountDiscoveryDiagnostic(
                        code="MISSING_PARENT",
                        field_name="parent_account_key",
                        message="Missing parent identity for a required child",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

                parent_rep = raw_obs_map[p_raw][0]
                if parent_rep.institution_id != rep.institution_id:
                    diag = AccountDiscoveryDiagnostic(
                        code="CROSS_INSTITUTION_PARENT",
                        field_name="parent_account_key",
                        message="Cross-institution parent relationship is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

                if parent_rep.source_registry_id != rep.source_registry_id:
                    diag = AccountDiscoveryDiagnostic(
                        code="CROSS_PROVIDER_PARENT",
                        field_name="parent_account_key",
                        message="Cross-provider parent relationship is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

            elif p_prot:
                if p_prot not in existing_map:
                    diag = AccountDiscoveryDiagnostic(
                        code="MISSING_PARENT",
                        field_name="parent_account_key",
                        message="Missing parent identity for a required child",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

                parent_ex = existing_map[p_prot]
                if parent_ex.institution_id != rep.institution_id:
                    diag = AccountDiscoveryDiagnostic(
                        code="CROSS_INSTITUTION_PARENT",
                        field_name="parent_account_key",
                        message="Cross-institution parent relationship is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

                if parent_ex.source_registry_id != rep.source_registry_id:
                    diag = AccountDiscoveryDiagnostic(
                        code="CROSS_PROVIDER_PARENT",
                        field_name="parent_account_key",
                        message="Cross-provider parent relationship is not permitted",
                    )
                    hierarchy_diagnostics.setdefault(raw_key, []).append(diag)
                    plan_diagnostics.append(diag)
                    continue

        # Compute protected keys topologically
        computed_protected_keys: dict[str, str] = {}
        prot_to_parent_prot: dict[str, str | None] = {}

        def get_or_compute_protected_key(rk: str) -> str:
            if rk in computed_protected_keys:
                return computed_protected_keys[rk]
            rep_obs = raw_obs_map[rk][0]
            p_rk = rep_obs.parent_raw_account_key
            p_prot_key = None

            if rk in hierarchy_diagnostics:
                p_prot_key = None
            elif p_rk and p_rk in raw_obs_map and p_rk not in hierarchy_diagnostics:
                p_prot_key = get_or_compute_protected_key(p_rk)
            elif rep_obs.parent_protected_account_key:
                p_prot_key = rep_obs.parent_protected_account_key

            prot_key = self._protector.protect_account_key(
                rep_obs.institution_id,
                rep_obs.source_registry_id,
                rep_obs.account_type,
                rk,
                p_prot_key,
            )
            computed_protected_keys[rk] = prot_key
            prot_to_parent_prot[prot_key] = p_prot_key
            return prot_key

        for rk in raw_obs_map:
            get_or_compute_protected_key(rk)

        # Step 3: Group observations by protected_account_key
        # After this point, raw keys are never referenced or retained!
        protected_groups: dict[str, list[AccountDiscoveryObservation]] = {}
        for rk, obs_list in raw_obs_map.items():
            prot_key = computed_protected_keys[rk]
            protected_groups.setdefault(prot_key, []).extend(obs_list)

        # Map hierarchy diagnostics to protected key
        protected_hierarchy_diags: dict[str, list[AccountDiscoveryDiagnostic]] = {}
        for rk, diags in hierarchy_diagnostics.items():
            prot_key = computed_protected_keys[rk]
            protected_hierarchy_diags.setdefault(prot_key, []).extend(diags)

        # Clean local raw mappings to guarantee no raw retention
        del raw_obs_map
        del computed_protected_keys
        del hierarchy_diagnostics

        # Step 4: Resolve each protected account group deterministically
        resolutions: list[AccountResolution] = list(invalid_resolutions)

        for prot_key, obs_group in protected_groups.items():
            group_diagnostics = list(protected_hierarchy_diags.get(prot_key, []))

            # Sort observations deterministically by effective_date
            sorted_obs = sorted(
                obs_group,
                key=lambda o: (o.effective_date, o.display_name, str(o.account_type)),
            )

            # Check for conflicting observations on the same effective_date
            by_date: dict[str, list[AccountDiscoveryObservation]] = {}
            for o in sorted_obs:
                by_date.setdefault(o.effective_date, []).append(o)

            has_conflict = False
            for d, d_obs in by_date.items():
                if len(d_obs) > 1:
                    first = d_obs[0]
                    for other in d_obs[1:]:
                        if (
                            other.display_name != first.display_name
                            or other.account_type != first.account_type
                            or other.ownership_state != first.ownership_state
                            or other.explicit_lifecycle_signal != first.explicit_lifecycle_signal
                        ):
                            has_conflict = True
                            diag = AccountDiscoveryDiagnostic(
                                code="CONFLICTING_OBSERVATIONS",
                                field_name="effective_date",
                                message="Conflicting observations detected for the same effective date",
                            )
                            group_diagnostics.append(diag)
                            plan_diagnostics.append(diag)
                            break
                    if has_conflict:
                        break

            latest_obs = sorted_obs[-1]
            existing = existing_map.get(prot_key)

            # Determine ownership
            if latest_obs.ownership_state in (OwnershipState.OWNED, OwnershipState.THIRD_PARTY):
                res_ownership = latest_obs.ownership_state
                res_conf = (
                    latest_obs.ownership_confidence
                    if latest_obs.ownership_confidence != ConfidenceLevel.UNKNOWN
                    else ConfidenceLevel.HIGH
                )
            else:
                res_ownership = OwnershipState.UNKNOWN
                res_conf = ConfidenceLevel.UNKNOWN

            # Determine parent protected key
            res_parent_prot = (
                prot_to_parent_prot.get(prot_key)
                or latest_obs.parent_protected_account_key
            )
            if not res_parent_prot and existing:
                res_parent_prot = existing.parent_protected_account_key

            # Determine lifecycle
            res_evidence = latest_obs.lifecycle_evidence

            if group_diagnostics or has_conflict:
                res_lifecycle = LifecycleState.UNVERIFIED
            elif existing is None:
                if latest_obs.explicit_lifecycle_signal == "CLOSED":
                    res_lifecycle = LifecycleState.CLOSED
                else:
                    res_lifecycle = LifecycleState.NEW
            else:
                # Existing account present
                # Out-of-order check: historical observation must not overwrite newer confirmed state
                if latest_obs.effective_date < existing.effective_date:
                    res_lifecycle = LifecycleState.UNCHANGED
                else:
                    if existing.lifecycle_state == LifecycleState.CLOSED:
                        if (
                            latest_obs.explicit_lifecycle_signal == "REUSED"
                            and latest_obs.lifecycle_evidence
                        ):
                            res_lifecycle = LifecycleState.REUSED
                        else:
                            res_lifecycle = LifecycleState.UNVERIFIED
                            diag = AccountDiscoveryDiagnostic(
                                code="AMBIGUOUS_REUSE",
                                field_name="lifecycle_state",
                                message="Ambiguous provider account slot reuse requires verification",
                            )
                            group_diagnostics.append(diag)
                            plan_diagnostics.append(diag)
                    elif latest_obs.explicit_lifecycle_signal == "CLOSED":
                        res_lifecycle = LifecycleState.CLOSED
                    elif latest_obs.display_name != existing.display_name:
                        res_lifecycle = LifecycleState.RENAMED
                    else:
                        res_lifecycle = LifecycleState.UNCHANGED

            # Determine display name
            if existing and latest_obs.effective_date < existing.effective_date:
                final_display_name = existing.display_name
            else:
                final_display_name = latest_obs.display_name

            resolutions.append(
                AccountResolution(
                    protected_account_key=prot_key,
                    institution_id=latest_obs.institution_id,
                    source_registry_id=latest_obs.source_registry_id,
                    display_name=final_display_name,
                    account_type=latest_obs.account_type,
                    ownership_state=res_ownership,
                    lifecycle_state=res_lifecycle,
                    effective_date=latest_obs.effective_date,
                    parent_protected_account_key=res_parent_prot,
                    ownership_confidence=res_conf,
                    lifecycle_evidence=res_evidence,
                    diagnostics=tuple(group_diagnostics),
                )
            )

        # Sort resolutions deterministically
        sorted_resolutions = tuple(
            sorted(
                resolutions,
                key=lambda r: (r.institution_id, r.source_registry_id, r.protected_account_key),
            )
        )

        return AccountDiscoveryPlan(
            resolutions=sorted_resolutions,
            diagnostics=tuple(plan_diagnostics),
        )
