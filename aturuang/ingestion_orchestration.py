from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import sqlite3
import unicodedata
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .ingestion_adapter import (
    AdapterContractError,
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    AdapterResult,
    DiagnosticSeverity,
    NormalizedEventEnvelope,
    SafeDiagnostic,
    UniversalSourceAdapter,
    validate_adapter_input,
)
from .ingestion_contracts import (
    DocumentIdentityStatus,
    LifecycleState,
    SourceChannel,
    TemplateMatchStatus,
)
from .ingestion_account_discovery import (
    AccountDiscoveryDiagnostic,
    AccountDiscoveryObservation,
    AccountDiscoveryPlan,
    AccountDiscoveryResolver,
    AccountResolution,
    ExistingAccountState,
)
from .ingestion_discovery import (
    ArtifactOccurrence,
    ArtifactPayloadError,
    DiscoveredArtifact,
    DiscoveryPolicy,
    DiscoveryResult,
    read_discovered_artifact,
)
from .ingestion_preflight import (
    DocumentPreflight,
    PreflightQuality,
    preflight_bytes,
)
from .ingestion_registry_service import (
    RegistryNotInitializedError,
    SourceDocumentReadRecord,
    TemplateResolution,
    get_source_capability,
    lookup_source_documents_by_content_sha,
    lookup_source_documents_by_natural_key,
    resolve_template,
)


class OrchestrationError(RuntimeError):
    pass


class AdapterCatalogError(OrchestrationError):
    pass


class DryRunDisposition(str, Enum):
    READY_FOR_STAGING = "READY_FOR_STAGING"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class DryRunBatchStatus(str, Enum):
    COMPLETED = "COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class DryRunStage(str, Enum):
    DISCOVERY = "DISCOVERY"
    PAYLOAD = "PAYLOAD"
    PREFLIGHT = "PREFLIGHT"
    AUTHORITY = "AUTHORITY"
    ADAPTER = "ADAPTER"
    IDENTITY = "IDENTITY"
    ACCOUNT_DISCOVERY = "ACCOUNT_DISCOVERY"
    BATCH = "BATCH"


@dataclass(frozen=True)
class DryRunDiagnostic:
    code: str
    stage: DryRunStage
    severity: DiagnosticSeverity
    message: str
    source_document_id: str | None = None
    locator_token: str | None = None


@dataclass(frozen=True)
class DryRunDocumentResult:
    source_document_id: str
    content_sha256: str
    size_bytes: int
    occurrence_tokens: tuple[str, ...]
    observed_extensions: tuple[str, ...]
    disposition: DryRunDisposition
    identity_status: DocumentIdentityStatus | None
    semantic_sha256: str | None = None
    related_existing_document_id: str | None = None
    preflight: DocumentPreflight | None = None
    selected_adapter: AdapterDescriptor | None = None
    adapter_result: AdapterResult | None = field(default=None, repr=False)
    natural_document_key_candidate: str | None = field(
        default=None,
        repr=False,
    )
    occurrences: tuple[ArtifactOccurrence, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    diagnostics: tuple[DryRunDiagnostic, ...] = field(
        default_factory=tuple,
    )
    account_discovery_plan: AccountDiscoveryPlan | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True)
class DryRunBatchResult:
    unique_artifact_count: int
    occurrence_count: int
    exact_duplicate_count: int
    semantic_duplicate_count: int
    ready_for_staging_count: int
    review_required_count: int
    failed_count: int
    documents: tuple[DryRunDocumentResult, ...]
    diagnostics: tuple[DryRunDiagnostic, ...]
    overall_status: DryRunBatchStatus


class ReadOnlyRegistryAuthority(Protocol):
    def lookup_exact_documents(
        self,
        content_sha256: str,
    ) -> tuple[SourceDocumentReadRecord, ...]:
        ...

    def lookup_natural_documents(
        self,
        source_registry_id: str,
        natural_document_key: str,
    ) -> tuple[SourceDocumentReadRecord, ...]:
        ...

    def source_capability(
        self,
        source_registry_id: str,
    ) -> Mapping[str, object] | None:
        ...

    def template_authority(
        self,
        *,
        source_registry_id: str,
        template_fingerprint: str,
    ) -> TemplateResolution:
        ...


@dataclass(frozen=True)
class SqliteRegistryAuthority:
    connection: sqlite3.Connection = field(repr=False)

    def lookup_exact_documents(
        self,
        content_sha256: str,
    ) -> tuple[SourceDocumentReadRecord, ...]:
        return lookup_source_documents_by_content_sha(
            self.connection,
            content_sha256=content_sha256,
        )

    def lookup_natural_documents(
        self,
        source_registry_id: str,
        natural_document_key: str,
    ) -> tuple[SourceDocumentReadRecord, ...]:
        return lookup_source_documents_by_natural_key(
            self.connection,
            source_registry_id=source_registry_id,
            natural_document_key=natural_document_key,
        )

    def source_capability(
        self,
        source_registry_id: str,
    ) -> Mapping[str, object] | None:
        return get_source_capability(
            self.connection,
            source_registry_id,
        )

    def template_authority(
        self,
        *,
        source_registry_id: str,
        template_fingerprint: str,
    ) -> TemplateResolution:
        return resolve_template(
            self.connection,
            source_registry_id=source_registry_id,
            template_fingerprint=template_fingerprint,
        )


AdapterKey = tuple[str, str, str, SourceChannel]


class AdapterCatalog:
    def __init__(
        self,
        adapters: Iterable[UniversalSourceAdapter],
    ) -> None:
        entries: dict[
            AdapterKey,
            tuple[AdapterDescriptor, UniversalSourceAdapter],
        ] = {}

        for adapter in adapters:
            descriptor = adapter.descriptor
            if not isinstance(descriptor, AdapterDescriptor):
                raise AdapterCatalogError(
                    "adapter descriptor is invalid"
                )

            key = self.key_for_descriptor(descriptor)
            if key in entries:
                raise AdapterCatalogError(
                    "duplicate exact adapter catalog key"
                )
            entries[key] = (descriptor, adapter)

        self._entries = entries

    @staticmethod
    def key_for_descriptor(
        descriptor: AdapterDescriptor,
    ) -> AdapterKey:
        return (
            descriptor.source_registry_id,
            descriptor.template_id,
            descriptor.parser_version,
            descriptor.source_channel,
        )

    def select(
        self,
        *,
        source_registry_id: str,
        template_id: str,
        parser_version: str,
        source_channel: SourceChannel,
    ) -> UniversalSourceAdapter | None:
        key = (
            source_registry_id,
            template_id,
            parser_version,
            source_channel,
        )
        entry = self._entries.get(key)
        if entry is None:
            return None

        frozen_descriptor, adapter = entry
        current_descriptor = adapter.descriptor
        if (
            not isinstance(current_descriptor, AdapterDescriptor)
            or current_descriptor != frozen_descriptor
            or self.key_for_descriptor(current_descriptor) != key
        ):
            raise AdapterCatalogError(
                "adapter descriptor changed after catalog registration"
            )
        return adapter


def build_default_adapter_catalog() -> AdapterCatalog:
    """Builds the default application adapter catalog with all supported provider adapters."""
    from .ingestion_bca_adapter import BCAMonthlyStatementAdapter
    from .ingestion_blu_mutation_adapter import BluAccountMutationAdapter
    from .ingestion_gopay_adapter import GoPayEStatementAdapter
    from .ingestion_jago_adapter import JagoMonthlyStatementAdapter
    from .ingestion_seabank_adapter import SeaBankMonthlyStatementAdapter
    from .ingestion_shopee_orders_adapter import ShopeeOrdersReceiptAdapter
    from .ingestion_shopeepay_adapter import ShopeePayTransactionHistoryImageAdapter
    from .ingestion_stockbit_adapter import StockbitStatementAdapter

    return AdapterCatalog(
        (
            BCAMonthlyStatementAdapter(),
            JagoMonthlyStatementAdapter(),
            BluAccountMutationAdapter(),
            GoPayEStatementAdapter(),
            SeaBankMonthlyStatementAdapter(),
            ShopeePayTransactionHistoryImageAdapter(),
            StockbitStatementAdapter(),
            ShopeeOrdersReceiptAdapter(),
        )
    )


get_default_adapter_catalog = build_default_adapter_catalog
build_application_adapter_catalog = build_default_adapter_catalog


PreflightFunction = Callable[..., DocumentPreflight]


def _dryrun_document_id(content_sha256: str) -> str:
    return "dryrun-" + content_sha256


def _occurrence_token(locator: str) -> str:
    return sha256(
        locator.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]


def _base_result_kwargs(
    artifact: DiscoveredArtifact,
) -> dict[str, object]:
    return {
        "content_sha256": artifact.content_sha256,
        "size_bytes": artifact.size_bytes,
        "occurrence_tokens": tuple(
            sorted(
                _occurrence_token(item.source_locator)
                for item in artifact.occurrences
            )
        ),
        "observed_extensions": artifact.observed_extensions,
        "occurrences": artifact.occurrences,
    }


def _diagnostic(
    code: str,
    stage: DryRunStage,
    severity: DiagnosticSeverity,
    message: str,
    *,
    source_document_id: str | None = None,
    locator_token: str | None = None,
) -> DryRunDiagnostic:
    return DryRunDiagnostic(
        code=code,
        stage=stage,
        severity=severity,
        message=message,
        source_document_id=source_document_id,
        locator_token=locator_token,
    )


def _review_result(
    artifact: DiscoveredArtifact,
    *,
    source_document_id: str,
    code: str,
    stage: DryRunStage,
    message: str,
    preflight: DocumentPreflight | None = None,
    selected_adapter: AdapterDescriptor | None = None,
    diagnostics: tuple[DryRunDiagnostic, ...] = (),
) -> DryRunDocumentResult:
    return DryRunDocumentResult(
        source_document_id=source_document_id,
        disposition=DryRunDisposition.REVIEW_REQUIRED,
        identity_status=None,
        preflight=preflight,
        selected_adapter=selected_adapter,
        diagnostics=diagnostics
        + (
            _diagnostic(
                code,
                stage,
                DiagnosticSeverity.WARNING,
                message,
                source_document_id=source_document_id,
            ),
        ),
        **_base_result_kwargs(artifact),
    )


def _failed_result(
    artifact: DiscoveredArtifact,
    *,
    source_document_id: str,
    code: str,
    stage: DryRunStage,
    message: str,
    preflight: DocumentPreflight | None = None,
    selected_adapter: AdapterDescriptor | None = None,
) -> DryRunDocumentResult:
    return DryRunDocumentResult(
        source_document_id=source_document_id,
        disposition=DryRunDisposition.FAILED,
        identity_status=None,
        preflight=preflight,
        selected_adapter=selected_adapter,
        diagnostics=(
            _diagnostic(
                code,
                stage,
                DiagnosticSeverity.ERROR,
                message,
                source_document_id=source_document_id,
            ),
        ),
        **_base_result_kwargs(artifact),
    )


def _adapter_diagnostics(
    diagnostics: tuple[SafeDiagnostic, ...],
    *,
    source_document_id: str,
) -> tuple[DryRunDiagnostic, ...]:
    return tuple(
        DryRunDiagnostic(
            code=item.code,
            stage=DryRunStage.ADAPTER,
            severity=item.severity,
            message=item.message,
            source_document_id=source_document_id,
            locator_token=item.locator_token,
        )
        for item in diagnostics
    )


DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTOR_MISSING = "ACCOUNT_OBSERVATION_EXTRACTOR_MISSING"
DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID = "ACCOUNT_OBSERVATION_RESULT_INVALID"
DIAGNOSTIC_ACCOUNT_OBSERVATION_EMPTY = "ACCOUNT_OBSERVATION_EMPTY"
DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTION_FAILED = "ACCOUNT_OBSERVATION_EXTRACTION_FAILED"
DIAGNOSTIC_ACCOUNT_DISCOVERY_RESOLUTION_FAILED = "ACCOUNT_DISCOVERY_RESOLUTION_FAILED"
DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED = "ACCOUNT_DISCOVERY_REVIEW_REQUIRED"

ACCOUNT_BEARING_REGISTRIES = frozenset({
    "jago_statement",
    "seabank_statement",
    "blu_mutation",
    "gopay_statement",
    "bca_statement",
    "stockbit_soa",
    "shopeepay_mutation",
})

ACCOUNT_BEARING_INSTITUTIONS = frozenset({
    "jago",
    "seabank",
    "blu",
    "gopay",
    "bca",
    "stockbit",
    "shopeepay",
})


def _is_account_bearing(
    adapter: UniversalSourceAdapter,
    source_registry_id: str,
) -> bool:
    if source_registry_id == "shopee_orders":
        return False
    if source_registry_id in ACCOUNT_BEARING_REGISTRIES:
        return True
    descriptor = getattr(adapter, "descriptor", None)
    if descriptor is not None:
        inst_id = getattr(descriptor, "institution_id", None)
        if inst_id in ACCOUNT_BEARING_INSTITUTIONS:
            return True
    return False


def _validate_account_discovery_plan(plan: Any) -> AccountDiscoveryPlan:
    if plan is None:
        raise ValueError("Resolver returned None")
    if not isinstance(plan, AccountDiscoveryPlan):
        raise TypeError("Resolver returned foreign object")
    if not isinstance(plan.resolutions, (tuple, list)):
        raise TypeError("Plan resolutions is not a sequence")
    for res in plan.resolutions:
        if not isinstance(res, AccountResolution):
            raise TypeError("Plan resolution entry is not an AccountResolution")
        if not isinstance(res.diagnostics, (tuple, list)):
            raise TypeError("Resolution diagnostics is not a sequence")
        for diag in res.diagnostics:
            if not isinstance(diag, AccountDiscoveryDiagnostic):
                raise TypeError(
                    "Resolution diagnostic entry is not an AccountDiscoveryDiagnostic"
                )
    if not isinstance(plan.diagnostics, (tuple, list)):
        raise TypeError("Plan diagnostics is not a sequence")
    for diag in plan.diagnostics:
        if not isinstance(diag, AccountDiscoveryDiagnostic):
            raise TypeError(
                "Plan diagnostic entry is not an AccountDiscoveryDiagnostic"
            )
    return plan


def dry_run_artifact(
    root: str | object,
    artifact: DiscoveredArtifact,
    *,
    registry: ReadOnlyRegistryAuthority,
    adapter_catalog: AdapterCatalog,
    claimed_source_registry_id: str | None = None,
    discovery_policy: DiscoveryPolicy | None = None,
    payload_reader: Callable[..., bytes] = read_discovered_artifact,
    preflight_func: PreflightFunction = preflight_bytes,
    account_resolver: AccountDiscoveryResolver | None = None,
    existing_accounts: Sequence[ExistingAccountState] | None = None,
) -> DryRunDocumentResult:
    temporary_document_id = _dryrun_document_id(
        artifact.content_sha256
    )

    try:
        exact_documents = registry.lookup_exact_documents(
            artifact.content_sha256
        )
    except RegistryNotInitializedError:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="REGISTRY_AUTHORITY_UNAVAILABLE",
            stage=DryRunStage.AUTHORITY,
            message="Registry authority is unavailable for dry-run.",
        )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="EXACT_SHA_LOOKUP_FAILED",
            stage=DryRunStage.IDENTITY,
            message="Exact source-document identity lookup failed.",
        )

    if len(exact_documents) > 1:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="EXACT_SHA_AMBIGUOUS",
            stage=DryRunStage.IDENTITY,
            message="Exact content identity is ambiguous.",
        )

    if len(exact_documents) == 1:
        existing = exact_documents[0]
        return DryRunDocumentResult(
            source_document_id=existing.source_document_id,
            disposition=DryRunDisposition.SKIPPED_DUPLICATE,
            identity_status=DocumentIdentityStatus.EXACT_DUPLICATE,
            related_existing_document_id=existing.source_document_id,
            diagnostics=(
                _diagnostic(
                    "EXACT_DUPLICATE",
                    DryRunStage.IDENTITY,
                    DiagnosticSeverity.INFO,
                    "Exact source-document content already exists.",
                    source_document_id=existing.source_document_id,
                ),
            ),
            **_base_result_kwargs(artifact),
        )

    if len(artifact.observed_extensions) != 1 or not artifact.extension:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="CONFLICTING_OCCURRENCE_EXTENSIONS",
            stage=DryRunStage.DISCOVERY,
            message="Exact content was observed with conflicting extensions.",
        )

    try:
        if discovery_policy is None:
            payload = payload_reader(root, artifact)
        else:
            payload = payload_reader(
                root,
                artifact,
                policy=discovery_policy,
            )
    except ArtifactPayloadError:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="PAYLOAD_INTEGRITY_FAILED",
            stage=DryRunStage.PAYLOAD,
            message="Discovered artifact payload could not be verified.",
        )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="PAYLOAD_REPLAY_FAILED",
            stage=DryRunStage.PAYLOAD,
            message="Discovered artifact payload replay failed.",
        )

    try:
        if claimed_source_registry_id is not None:
            preflight = preflight_func(
                payload,
                extension=artifact.extension,
                source_hint=claimed_source_registry_id,
            )
        else:
            preflight = preflight_func(
                payload,
                extension=artifact.extension,
            )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="PREFLIGHT_FAILED",
            stage=DryRunStage.PREFLIGHT,
            message="Content preflight failed.",
        )

    if (
        preflight.content_sha256 != artifact.content_sha256
        or preflight.size_bytes != artifact.size_bytes
    ):
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="PREFLIGHT_IDENTITY_MISMATCH",
            stage=DryRunStage.PREFLIGHT,
            message="Preflight content identity disagrees with discovery.",
            preflight=preflight,
        )

    if preflight.quality_status is not PreflightQuality.READY:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="PREFLIGHT_NOT_READY",
            stage=DryRunStage.PREFLIGHT,
            message="Source media is not ready for adapter execution.",
            preflight=preflight,
        )

    detection = preflight.template_detection

    if detection.status is TemplateMatchStatus.TEMPLATE_DRIFT:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="TEMPLATE_DRIFT",
            stage=DryRunStage.PREFLIGHT,
            message="Content template drift requires review.",
            preflight=preflight,
        )

    if detection.status is not TemplateMatchStatus.KNOWN:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="UNKNOWN_TEMPLATE",
            stage=DryRunStage.PREFLIGHT,
            message="No exact content template is available.",
            preflight=preflight,
        )

    if (
        detection.source_registry_id is None
        or detection.template_id is None
        or detection.template_fingerprint is None
    ):
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="DETECTION_INVARIANT_FAILED",
            stage=DryRunStage.PREFLIGHT,
            message="Known template detection is internally incomplete.",
            preflight=preflight,
        )

    if (
        claimed_source_registry_id is not None
        and claimed_source_registry_id
        != detection.source_registry_id
    ):
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="SOURCE_CLAIM_CONFLICT",
            stage=DryRunStage.AUTHORITY,
            message="Source claim conflicts with content evidence.",
            preflight=preflight,
        )

    source_registry_id = detection.source_registry_id

    try:
        capability = registry.source_capability(
            source_registry_id
        )
        authority = registry.template_authority(
            source_registry_id=source_registry_id,
            template_fingerprint=detection.template_fingerprint,
        )
    except RegistryNotInitializedError:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="REGISTRY_AUTHORITY_UNAVAILABLE",
            stage=DryRunStage.AUTHORITY,
            message="Registry authority is unavailable for dry-run.",
            preflight=preflight,
        )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="REGISTRY_AUTHORITY_FAILED",
            stage=DryRunStage.AUTHORITY,
            message="Registry authority resolution failed.",
            preflight=preflight,
        )

    if capability is None:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="SOURCE_AUTHORITY_UNAVAILABLE",
            stage=DryRunStage.AUTHORITY,
            message="Source registry authority is unavailable.",
            preflight=preflight,
        )

    if authority.status is not TemplateMatchStatus.KNOWN:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="REGISTRY_TEMPLATE_NOT_KNOWN",
            stage=DryRunStage.AUTHORITY,
            message="Registry does not confirm the detected template.",
            preflight=preflight,
        )

    if (
        authority.template_id != detection.template_id
        or authority.parser_version is None
    ):
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="REGISTRY_TEMPLATE_MISMATCH",
            stage=DryRunStage.AUTHORITY,
            message="Registry template authority conflicts with content evidence.",
            preflight=preflight,
        )

    try:
        source_channel = SourceChannel(
            str(capability["channel"])
        )
    except (KeyError, TypeError, ValueError):
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="SOURCE_CHANNEL_INVALID",
            stage=DryRunStage.AUTHORITY,
            message="Registry source channel is invalid.",
            preflight=preflight,
        )

    try:
        adapter = adapter_catalog.select(
            source_registry_id=source_registry_id,
            template_id=authority.template_id,
            parser_version=authority.parser_version,
            source_channel=source_channel,
        )
    except AdapterCatalogError:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_CATALOG_INVARIANT_FAILED",
            stage=DryRunStage.ADAPTER,
            message="Adapter catalog invariant failed.",
            preflight=preflight,
        )

    if adapter is None:
        return _review_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_NOT_AVAILABLE",
            stage=DryRunStage.ADAPTER,
            message="No exact adapter is available for this source template.",
            preflight=preflight,
        )

    selected_descriptor = adapter.descriptor

    source = AdapterInput(
        source_document_id=temporary_document_id,
        content_sha256=artifact.content_sha256,
        source_registry_id=source_registry_id,
        template_id=authority.template_id,
        parser_version=authority.parser_version,
        source_channel=source_channel,
        template_match_status=TemplateMatchStatus.KNOWN,
        period_status=preflight.period_status,
        template_fingerprint=detection.template_fingerprint,
        binary_payload=payload,
    )

    try:
        validate_adapter_input(
            selected_descriptor,
            source,
        )
    except AdapterContractError:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_INPUT_MISMATCH",
            stage=DryRunStage.ADAPTER,
            message="Adapter input does not match selected authority.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    try:
        adapter_result = adapter.parse(source)
    except AdapterContractError:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_CONTRACT_FAILED",
            stage=DryRunStage.ADAPTER,
            message="Adapter contract validation failed.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_EXECUTION_FAILED",
            stage=DryRunStage.ADAPTER,
            message="Adapter execution failed.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    if not isinstance(adapter_result, AdapterResult):
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_RESULT_INVALID",
            stage=DryRunStage.ADAPTER,
            message="Adapter returned an invalid result type.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    if (
        adapter_result.descriptor != selected_descriptor
        or adapter_result.source_document_id
        != temporary_document_id
    ):
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="ADAPTER_RESULT_MISMATCH",
            stage=DryRunStage.ADAPTER,
            message="Adapter result identity does not match the request.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    converted_diagnostics = _adapter_diagnostics(
        adapter_result.diagnostics,
        source_document_id=temporary_document_id,
    )

    if adapter_result.parse_status is AdapterParseStatus.FAILED:
        return DryRunDocumentResult(
            source_document_id=temporary_document_id,
            disposition=DryRunDisposition.FAILED,
            identity_status=None,
            preflight=preflight,
            selected_adapter=selected_descriptor,
            adapter_result=adapter_result,
            natural_document_key_candidate=(
                adapter_result.natural_document_key_candidate
            ),
            diagnostics=converted_diagnostics,
            **_base_result_kwargs(artifact),
        )

    natural_key = adapter_result.natural_document_key_candidate
    try:
        semantic_sha = semantic_document_sha256(
            source_registry_id,
            natural_key,
            adapter_result,
        )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="SEMANTIC_CANONICALIZATION_FAILED",
            stage=DryRunStage.IDENTITY,
            message="Semantic document canonicalization failed.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    adapter_requires_review = (
        adapter_result.parse_status
        is AdapterParseStatus.REVIEW_REQUIRED
        or adapter_result.review_required
    )

    account_discovery_plan: AccountDiscoveryPlan | None = None
    account_discovery_diagnostics: list[DryRunDiagnostic] = []
    account_discovery_requires_review = False
    contract_failure_diagnostic: DryRunDiagnostic | None = None

    if account_resolver is not None:
        if source_registry_id == "shopee_orders":
            account_discovery_plan = AccountDiscoveryPlan(
                resolutions=(),
                diagnostics=(),
            )
        elif hasattr(adapter, "extract_account_observations") and callable(
            getattr(adapter, "extract_account_observations")
        ):
            try:
                raw_obs = adapter.extract_account_observations(adapter_result)
            except Exception:
                contract_failure_diagnostic = _diagnostic(
                    DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTION_FAILED,
                    DryRunStage.ACCOUNT_DISCOVERY,
                    DiagnosticSeverity.ERROR,
                    "Account observation extractor execution failed.",
                    source_document_id=temporary_document_id,
                )
                raw_obs = None

            if contract_failure_diagnostic is None:
                if not isinstance(raw_obs, (list, tuple)):
                    contract_failure_diagnostic = _diagnostic(
                        DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID,
                        DryRunStage.ACCOUNT_DISCOVERY,
                        DiagnosticSeverity.ERROR,
                        "Account observation extractor returned an invalid result type.",
                        source_document_id=temporary_document_id,
                    )
                elif any(
                    not isinstance(item, AccountDiscoveryObservation)
                    for item in raw_obs
                ):
                    contract_failure_diagnostic = _diagnostic(
                        DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID,
                        DryRunStage.ACCOUNT_DISCOVERY,
                        DiagnosticSeverity.ERROR,
                        "Account observation extractor returned a foreign item type.",
                        source_document_id=temporary_document_id,
                    )
                elif len(raw_obs) == 0:
                    account_discovery_plan = AccountDiscoveryPlan(
                        resolutions=(),
                        diagnostics=(),
                    )
                    if _is_account_bearing(adapter, source_registry_id):
                        account_discovery_diagnostics.append(
                            _diagnostic(
                                DIAGNOSTIC_ACCOUNT_OBSERVATION_EMPTY,
                                DryRunStage.ACCOUNT_DISCOVERY,
                                DiagnosticSeverity.WARNING,
                                "Account-bearing source document yielded no account observations.",
                                source_document_id=temporary_document_id,
                            )
                        )
                        account_discovery_requires_review = True
                else:
                    validated_plan: AccountDiscoveryPlan | None = None
                    try:
                        resolved_plan = account_resolver.resolve(
                            raw_obs,
                            existing_accounts=existing_accounts,
                        )
                        validated_plan = _validate_account_discovery_plan(
                            resolved_plan
                        )
                    except Exception:
                        contract_failure_diagnostic = _diagnostic(
                            DIAGNOSTIC_ACCOUNT_DISCOVERY_RESOLUTION_FAILED,
                            DryRunStage.ACCOUNT_DISCOVERY,
                            DiagnosticSeverity.ERROR,
                            "Account discovery resolution execution failed.",
                            source_document_id=temporary_document_id,
                        )
                        account_discovery_plan = None
                        validated_plan = None

                    if (
                        contract_failure_diagnostic is None
                        and validated_plan is not None
                    ):
                        account_discovery_plan = validated_plan
                        has_unverified_or_non_eligible = any(
                            (not r.persistence_eligible)
                            or (r.lifecycle_state == LifecycleState.UNVERIFIED)
                            for r in validated_plan.resolutions
                        )
                        has_resolver_diagnostics = (
                            len(validated_plan.diagnostics) > 0
                            or any(
                                len(r.diagnostics) > 0
                                for r in validated_plan.resolutions
                            )
                        )
                        if (
                            has_unverified_or_non_eligible
                            or has_resolver_diagnostics
                        ):
                            account_discovery_diagnostics.append(
                                _diagnostic(
                                    DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED,
                                    DryRunStage.ACCOUNT_DISCOVERY,
                                    DiagnosticSeverity.WARNING,
                                    "Account discovery resolution requires review.",
                                    source_document_id=temporary_document_id,
                                )
                            )
                            account_discovery_requires_review = True
        elif _is_account_bearing(adapter, source_registry_id):
            account_discovery_diagnostics.append(
                _diagnostic(
                    DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTOR_MISSING,
                    DryRunStage.ACCOUNT_DISCOVERY,
                    DiagnosticSeverity.WARNING,
                    "Account-bearing source adapter lacks account observation extractor.",
                    source_document_id=temporary_document_id,
                )
            )
            account_discovery_requires_review = True
            account_discovery_plan = AccountDiscoveryPlan(
                resolutions=(),
                diagnostics=(),
            )
        else:
            account_discovery_plan = AccountDiscoveryPlan(
                resolutions=(),
                diagnostics=(),
            )

    if contract_failure_diagnostic is not None:
        return DryRunDocumentResult(
            source_document_id=temporary_document_id,
            disposition=DryRunDisposition.FAILED,
            identity_status=None,
            semantic_sha256=semantic_sha,
            preflight=preflight,
            selected_adapter=selected_descriptor,
            adapter_result=adapter_result,
            natural_document_key_candidate=natural_key,
            diagnostics=converted_diagnostics
            + (contract_failure_diagnostic,),
            account_discovery_plan=None,
            **_base_result_kwargs(artifact),
        )

    if natural_key is None:
        return DryRunDocumentResult(
            source_document_id=temporary_document_id,
            disposition=DryRunDisposition.REVIEW_REQUIRED,
            identity_status=DocumentIdentityStatus.AMBIGUOUS,
            semantic_sha256=semantic_sha,
            preflight=preflight,
            selected_adapter=selected_descriptor,
            adapter_result=adapter_result,
            natural_document_key_candidate=None,
            diagnostics=converted_diagnostics
            + tuple(account_discovery_diagnostics)
            + (
                _diagnostic(
                    "NATURAL_KEY_MISSING",
                    DryRunStage.IDENTITY,
                    DiagnosticSeverity.WARNING,
                    "Natural document identity is unavailable.",
                    source_document_id=temporary_document_id,
                ),
            ),
            account_discovery_plan=account_discovery_plan,
            **_base_result_kwargs(artifact),
        )

    try:
        natural_records = registry.lookup_natural_documents(
            source_registry_id,
            natural_key,
        )
    except RegistryNotInitializedError:
        return DryRunDocumentResult(
            source_document_id=temporary_document_id,
            disposition=DryRunDisposition.REVIEW_REQUIRED,
            identity_status=DocumentIdentityStatus.AMBIGUOUS,
            semantic_sha256=semantic_sha,
            preflight=preflight,
            selected_adapter=selected_descriptor,
            adapter_result=adapter_result,
            natural_document_key_candidate=natural_key,
            diagnostics=converted_diagnostics
            + tuple(account_discovery_diagnostics)
            + (
                _diagnostic(
                    "REGISTRY_AUTHORITY_UNAVAILABLE",
                    DryRunStage.IDENTITY,
                    DiagnosticSeverity.WARNING,
                    "Registry identity authority is unavailable.",
                    source_document_id=temporary_document_id,
                ),
            ),
            account_discovery_plan=account_discovery_plan,
            **_base_result_kwargs(artifact),
        )
    except Exception:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="NATURAL_IDENTITY_LOOKUP_FAILED",
            stage=DryRunStage.IDENTITY,
            message="Natural document identity lookup failed.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    try:
        identity_status, related_document_id = _classify_semantic_identity(
            natural_records,
            source_registry_id=source_registry_id,
            natural_document_key=natural_key,
            semantic_sha256=semantic_sha,
        )
    except OrchestrationError:
        return _failed_result(
            artifact,
            source_document_id=temporary_document_id,
            code="NATURAL_IDENTITY_INVARIANT_FAILED",
            stage=DryRunStage.IDENTITY,
            message="Natural document identity authority is inconsistent.",
            preflight=preflight,
            selected_adapter=selected_descriptor,
        )

    identity_diagnostics: tuple[DryRunDiagnostic, ...] = ()
    if identity_status is DocumentIdentityStatus.SEMANTIC_DUPLICATE:
        disposition = DryRunDisposition.SKIPPED_DUPLICATE
        identity_diagnostics = (
            _diagnostic(
                "SEMANTIC_DUPLICATE",
                DryRunStage.IDENTITY,
                DiagnosticSeverity.INFO,
                "Equivalent normalized source document already exists.",
                source_document_id=temporary_document_id,
            ),
        )
    elif identity_status is DocumentIdentityStatus.REVISION_OR_CONFLICT:
        disposition = DryRunDisposition.REVIEW_REQUIRED
        identity_diagnostics = (
            _diagnostic(
                "REVISION_OR_CONFLICT",
                DryRunStage.IDENTITY,
                DiagnosticSeverity.WARNING,
                "Natural document identity has different semantic evidence.",
                source_document_id=temporary_document_id,
            ),
        )
    elif identity_status is DocumentIdentityStatus.AMBIGUOUS:
        disposition = DryRunDisposition.REVIEW_REQUIRED
        identity_diagnostics = (
            _diagnostic(
                "SEMANTIC_IDENTITY_AMBIGUOUS",
                DryRunStage.IDENTITY,
                DiagnosticSeverity.WARNING,
                "Existing semantic identity evidence is incomplete.",
                source_document_id=temporary_document_id,
            ),
        )
    else:
        if adapter_requires_review or account_discovery_requires_review:
            disposition = DryRunDisposition.REVIEW_REQUIRED
        else:
            disposition = DryRunDisposition.READY_FOR_STAGING

    return DryRunDocumentResult(
        source_document_id=temporary_document_id,
        disposition=disposition,
        identity_status=identity_status,
        semantic_sha256=semantic_sha,
        related_existing_document_id=related_document_id,
        preflight=preflight,
        selected_adapter=selected_descriptor,
        adapter_result=adapter_result,
        natural_document_key_candidate=natural_key,
        diagnostics=converted_diagnostics
        + identity_diagnostics
        + tuple(account_discovery_diagnostics),
        account_discovery_plan=account_discovery_plan,
        **_base_result_kwargs(artifact),
    )


SEMANTIC_DOCUMENT_VERSION = "semantic-document-v1"

_CASEFOLD_SEMANTIC_FIELDS = {"currency"}
_NON_SEMANTIC_PAYLOAD_FIELDS = {"event_hint"}
_BATCH_FATAL_CODES = {
    "EXACT_SHA_AMBIGUOUS",
    "EXACT_SHA_LOOKUP_FAILED",
    "PAYLOAD_INTEGRITY_FAILED",
    "PAYLOAD_REPLAY_FAILED",
    "PREFLIGHT_IDENTITY_MISMATCH",
    "DETECTION_INVARIANT_FAILED",
    "REGISTRY_AUTHORITY_FAILED",
    "SOURCE_CHANNEL_INVALID",
    "ADAPTER_CATALOG_INVARIANT_FAILED",
    "ADAPTER_INPUT_MISMATCH",
    "ADAPTER_CONTRACT_FAILED",
    "ADAPTER_EXECUTION_FAILED",
    "ADAPTER_RESULT_INVALID",
    "ADAPTER_RESULT_MISMATCH",
    "SEMANTIC_CANONICALIZATION_FAILED",
    "NATURAL_IDENTITY_LOOKUP_FAILED",
    "NATURAL_IDENTITY_INVARIANT_FAILED",
    "ACCOUNT_OBSERVATION_RESULT_INVALID",
    "ACCOUNT_OBSERVATION_EXTRACTION_FAILED",
    "ACCOUNT_DISCOVERY_RESOLUTION_FAILED",
}


def _normalize_semantic_text(value: str, *, casefold: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    return normalized.casefold() if casefold else normalized


def _normalize_semantic_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise OrchestrationError("semantic numeric values must be Decimal")
    if not value.is_finite():
        raise OrchestrationError("semantic Decimal values must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_semantic_value(value: object, *, field_name: str | None = None) -> object:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return _normalize_semantic_decimal(value)
    if isinstance(value, str):
        return _normalize_semantic_text(
            value,
            casefold=field_name in _CASEFOLD_SEMANTIC_FIELDS,
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise OrchestrationError(
            "binary float is forbidden in semantic canonicalization"
        )
    if is_dataclass(value):
        result: dict[str, object] = {}
        for item in fields(value):
            if item.name in _NON_SEMANTIC_PAYLOAD_FIELDS:
                continue
            result[item.name] = _canonical_semantic_value(
                getattr(value, item.name),
                field_name=item.name,
            )
        return result
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_semantic_value(item, field_name=str(key))
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_semantic_value(item) for item in value]
    raise OrchestrationError("unsupported semantic canonicalization value")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical_semantic_event(
    event: NormalizedEventEnvelope,
) -> dict[str, object]:
    if not isinstance(event, NormalizedEventEnvelope):
        raise OrchestrationError(
            "semantic event must be NormalizedEventEnvelope"
        )
    payload = _canonical_semantic_value(event.payload)
    if not isinstance(payload, dict):
        raise OrchestrationError(
            "semantic event payload must canonicalize to an object"
        )
    return {
        "event_role": event.event_role.value,
        "payload": payload,
    }


def semantic_document_canonical_json(
    source_registry_id: str,
    natural_document_key: str | None,
    adapter_result: AdapterResult,
) -> str:
    if not isinstance(adapter_result, AdapterResult):
        raise OrchestrationError(
            "semantic document requires AdapterResult"
        )
    events = [
        _canonical_semantic_event(event)
        for event in adapter_result.events
    ]
    events.sort(key=_canonical_json)
    document = {
        "events": events,
        "natural_document_key": (
            _normalize_semantic_text(natural_document_key)
            if natural_document_key is not None
            else None
        ),
        "period_end": (
            _normalize_semantic_text(adapter_result.period_end)
            if adapter_result.period_end is not None
            else None
        ),
        "period_start": (
            _normalize_semantic_text(adapter_result.period_start)
            if adapter_result.period_start is not None
            else None
        ),
        "period_status": adapter_result.period_status.value,
        "source_registry_id": _normalize_semantic_text(
            source_registry_id
        ),
        "version": SEMANTIC_DOCUMENT_VERSION,
    }
    return _canonical_json(document)


def semantic_document_sha256(
    source_registry_id: str,
    natural_document_key: str | None,
    adapter_result: AdapterResult,
) -> str:
    canonical = semantic_document_canonical_json(
        source_registry_id,
        natural_document_key,
        adapter_result,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _classify_semantic_identity(
    records: tuple[SourceDocumentReadRecord, ...],
    *,
    source_registry_id: str,
    natural_document_key: str,
    semantic_sha256: str,
) -> tuple[DocumentIdentityStatus, str | None]:
    for record in records:
        if (
            record.source_registry_id != source_registry_id
            or record.natural_document_key != natural_document_key
        ):
            raise OrchestrationError(
                "natural identity lookup returned conflicting authority"
            )
    if not records:
        return (DocumentIdentityStatus.NEW, None)
    ordered = tuple(
        sorted(records, key=lambda item: item.source_document_id)
    )
    for record in ordered:
        if record.semantic_sha256 == semantic_sha256:
            return (
                DocumentIdentityStatus.SEMANTIC_DUPLICATE,
                record.source_document_id,
            )
    if any(record.semantic_sha256 is None for record in ordered):
        return (DocumentIdentityStatus.AMBIGUOUS, None)
    return (
        DocumentIdentityStatus.REVISION_OR_CONFLICT,
        ordered[0].source_document_id,
    )


def _safe_preflight_view(
    preflight: DocumentPreflight | None,
) -> dict[str, object] | None:
    if preflight is None:
        return None
    detection = preflight.template_detection
    return {
        "content_sha256": preflight.content_sha256,
        "extension": preflight.extension,
        "media_type": preflight.media_type,
        "period_status": preflight.period_status.value,
        "quality_status": preflight.quality_status.value,
        "template_detection": {
            "source_registry_id": detection.source_registry_id,
            "status": detection.status.value,
            "template_id": detection.template_id,
        },
    }


def _safe_account_discovery_view(
    plan: AccountDiscoveryPlan | None,
) -> dict[str, object] | None:
    if plan is None:
        return None

    def _sort_key(res: AccountResolution) -> tuple[str, str, str, str]:
        return (
            res.institution_id,
            res.source_registry_id,
            res.protected_account_key or "",
            res.effective_date,
        )

    resolutions_view = []
    for res in sorted(plan.resolutions, key=_sort_key):
        diag_codes = sorted(d.code for d in res.diagnostics)
        resolutions_view.append(
            {
                "account_type": (
                    res.account_type.value
                    if isinstance(res.account_type, Enum)
                    else str(res.account_type)
                ),
                "diagnostic_codes": diag_codes,
                "diagnostics": diag_codes,
                "display_name_safe": res.display_name_safe,
                "effective_date": res.effective_date,
                "institution_id": res.institution_id,
                "lifecycle_state": (
                    res.lifecycle_state.value
                    if isinstance(res.lifecycle_state, Enum)
                    else str(res.lifecycle_state)
                ),
                "ownership_confidence": (
                    res.ownership_confidence.value
                    if isinstance(res.ownership_confidence, Enum)
                    else str(res.ownership_confidence)
                ),
                "ownership_state": (
                    res.ownership_state.value
                    if isinstance(res.ownership_state, Enum)
                    else str(res.ownership_state)
                ),
                "parent_protected_account_key": res.parent_protected_account_key,
                "persistence_eligible": res.persistence_eligible,
                "protected_account_key": res.protected_account_key,
                "source_registry_id": res.source_registry_id,
            }
        )

    plan_diag_codes = sorted(d.code for d in plan.diagnostics)
    return {
        "diagnostic_codes": plan_diag_codes,
        "diagnostics": plan_diag_codes,
        "resolutions": resolutions_view,
    }


def safe_dry_run_document_view(
    result: DryRunDocumentResult,
) -> dict[str, object]:
    adapter_view = None
    if result.selected_adapter is not None:
        adapter_view = {
            "adapter_id": result.selected_adapter.adapter_id,
            "parser_version": result.selected_adapter.parser_version,
            "source_channel": result.selected_adapter.source_channel.value,
            "source_registry_id": result.selected_adapter.source_registry_id,
            "template_id": result.selected_adapter.template_id,
        }
    return {
        "account_discovery": _safe_account_discovery_view(
            result.account_discovery_plan
        ),
        "content_sha256": result.content_sha256,
        "diagnostics": [
            {
                "code": item.code,
                "locator_token": item.locator_token,
                "message": item.message,
                "severity": item.severity.value,
                "source_document_id": item.source_document_id,
                "stage": item.stage.value,
            }
            for item in result.diagnostics
        ],
        "disposition": result.disposition.value,
        "identity_status": (
            result.identity_status.value
            if result.identity_status is not None
            else None
        ),
        "observed_extensions": list(result.observed_extensions),
        "occurrence_tokens": list(result.occurrence_tokens),
        "preflight": _safe_preflight_view(result.preflight),
        "related_existing_document_id": (
            result.related_existing_document_id
        ),
        "selected_adapter": adapter_view,
        "semantic_sha256": result.semantic_sha256,
        "size_bytes": result.size_bytes,
        "source_document_id": result.source_document_id,
    }


def safe_dry_run_batch_json(result: DryRunBatchResult) -> str:
    return _canonical_json(
        {
            "diagnostics": [
                {
                    "code": item.code,
                    "locator_token": item.locator_token,
                    "message": item.message,
                    "severity": item.severity.value,
                    "source_document_id": item.source_document_id,
                    "stage": item.stage.value,
                }
                for item in result.diagnostics
            ],
            "documents": [
                safe_dry_run_document_view(item)
                for item in result.documents
            ],
            "exact_duplicate_count": result.exact_duplicate_count,
            "failed_count": result.failed_count,
            "occurrence_count": result.occurrence_count,
            "overall_status": result.overall_status.value,
            "ready_for_staging_count": result.ready_for_staging_count,
            "review_required_count": result.review_required_count,
            "semantic_duplicate_count": result.semantic_duplicate_count,
            "unique_artifact_count": result.unique_artifact_count,
        }
    )


def _diagnostic_sort_key(
    item: DryRunDiagnostic,
) -> tuple[str, str, str, str]:
    return (
        item.stage.value,
        item.code,
        item.source_document_id or "",
        item.locator_token or "",
    )


def dry_run_batch(
    root: str | object,
    discovery: DiscoveryResult,
    *,
    registry: ReadOnlyRegistryAuthority,
    adapter_catalog: AdapterCatalog,
    claimed_source_registry_ids: Mapping[str, str] | None = None,
    discovery_policy: DiscoveryPolicy | None = None,
    payload_reader: Callable[..., bytes] = read_discovered_artifact,
    preflight_func: PreflightFunction = preflight_bytes,
    account_resolver: AccountDiscoveryResolver | None = None,
    existing_accounts: Sequence[ExistingAccountState] | None = None,
) -> DryRunBatchResult:
    claims = dict(claimed_source_registry_ids or {})
    documents = tuple(
        dry_run_artifact(
            root,
            artifact,
            registry=registry,
            adapter_catalog=adapter_catalog,
            claimed_source_registry_id=claims.get(
                artifact.content_sha256
            ),
            discovery_policy=discovery_policy,
            payload_reader=payload_reader,
            preflight_func=preflight_func,
            account_resolver=account_resolver,
            existing_accounts=existing_accounts,
        )
        for artifact in sorted(
            discovery.artifacts,
            key=lambda item: item.content_sha256,
        )
    )

    diagnostics = [
        _diagnostic(
            item.code,
            DryRunStage.DISCOVERY,
            DiagnosticSeverity.WARNING,
            "Discovery reported a safe diagnostic.",
            locator_token=item.locator_token,
        )
        for item in discovery.diagnostics
    ]
    for document in documents:
        diagnostics.extend(document.diagnostics)
    ordered_diagnostics = tuple(
        sorted(diagnostics, key=_diagnostic_sort_key)
    )

    exact_duplicate_count = sum(
        item.identity_status is DocumentIdentityStatus.EXACT_DUPLICATE
        for item in documents
    )
    semantic_duplicate_count = sum(
        item.identity_status is DocumentIdentityStatus.SEMANTIC_DUPLICATE
        for item in documents
    )
    ready_for_staging_count = sum(
        item.disposition is DryRunDisposition.READY_FOR_STAGING
        for item in documents
    )
    review_required_count = sum(
        item.disposition is DryRunDisposition.REVIEW_REQUIRED
        for item in documents
    )
    failed_count = sum(
        item.disposition is DryRunDisposition.FAILED
        for item in documents
    )

    if any(
        item.code in _BATCH_FATAL_CODES
        for item in ordered_diagnostics
    ):
        overall_status = DryRunBatchStatus.FAILED
    elif review_required_count or failed_count:
        overall_status = DryRunBatchStatus.REVIEW_REQUIRED
    else:
        overall_status = DryRunBatchStatus.COMPLETED

    return DryRunBatchResult(
        unique_artifact_count=len(documents),
        occurrence_count=sum(
            len(item.occurrences) for item in documents
        ),
        exact_duplicate_count=exact_duplicate_count,
        semantic_duplicate_count=semantic_duplicate_count,
        ready_for_staging_count=ready_for_staging_count,
        review_required_count=review_required_count,
        failed_count=failed_count,
        documents=documents,
        diagnostics=ordered_diagnostics,
        overall_status=overall_status,
    )




__all__ = [
    "AdapterCatalog",
    "AdapterCatalogError",
    "DIAGNOSTIC_ACCOUNT_DISCOVERY_RESOLUTION_FAILED",
    "DIAGNOSTIC_ACCOUNT_DISCOVERY_REVIEW_REQUIRED",
    "DIAGNOSTIC_ACCOUNT_OBSERVATION_EMPTY",
    "DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTION_FAILED",
    "DIAGNOSTIC_ACCOUNT_OBSERVATION_EXTRACTOR_MISSING",
    "DIAGNOSTIC_ACCOUNT_OBSERVATION_RESULT_INVALID",
    "DryRunBatchResult",
    "DryRunBatchStatus",
    "DryRunDiagnostic",
    "DryRunDisposition",
    "DryRunDocumentResult",
    "DryRunStage",
    "OrchestrationError",
    "ReadOnlyRegistryAuthority",
    "SEMANTIC_DOCUMENT_VERSION",
    "SqliteRegistryAuthority",
    "build_application_adapter_catalog",
    "build_default_adapter_catalog",
    "dry_run_artifact",
    "dry_run_batch",
    "get_default_adapter_catalog",
    "safe_dry_run_batch_json",
    "safe_dry_run_document_view",
    "semantic_document_canonical_json",
    "semantic_document_sha256",
]
