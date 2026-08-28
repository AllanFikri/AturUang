from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import sqlite3
from typing import Callable, Iterable, Mapping, Protocol

from .ingestion_adapter import (
    AdapterContractError,
    AdapterDescriptor,
    AdapterInput,
    AdapterParseStatus,
    AdapterResult,
    DiagnosticSeverity,
    SafeDiagnostic,
    UniversalSourceAdapter,
    validate_adapter_input,
)
from .ingestion_contracts import (
    DocumentIdentityStatus,
    SourceChannel,
    TemplateMatchStatus,
)
from .ingestion_discovery import (
    ArtifactOccurrence,
    ArtifactPayloadError,
    DiscoveredArtifact,
    DiscoveryPolicy,
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


class DryRunStage(str, Enum):
    DISCOVERY = "DISCOVERY"
    PAYLOAD = "PAYLOAD"
    PREFLIGHT = "PREFLIGHT"
    AUTHORITY = "AUTHORITY"
    ADAPTER = "ADAPTER"
    IDENTITY = "IDENTITY"
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


class ReadOnlyRegistryAuthority(Protocol):
    def lookup_exact_documents(
        self,
        content_sha256: str,
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
        disposition = DryRunDisposition.FAILED
    elif (
        adapter_result.parse_status
        is AdapterParseStatus.REVIEW_REQUIRED
        or adapter_result.review_required
    ):
        disposition = DryRunDisposition.REVIEW_REQUIRED
    else:
        disposition = DryRunDisposition.READY_FOR_STAGING

    return DryRunDocumentResult(
        source_document_id=temporary_document_id,
        disposition=disposition,
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


__all__ = [
    "AdapterCatalog",
    "AdapterCatalogError",
    "DryRunDiagnostic",
    "DryRunDisposition",
    "DryRunDocumentResult",
    "DryRunStage",
    "OrchestrationError",
    "ReadOnlyRegistryAuthority",
    "SqliteRegistryAuthority",
    "dry_run_artifact",
]
