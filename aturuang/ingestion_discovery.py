from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable
import zipfile


class DiscoveryError(RuntimeError):
    """Base error for Universal Ingestion file discovery."""


class ArchiveSafetyError(DiscoveryError):
    """Raised when an archive violates fail-closed safety policy."""


class ArtifactPayloadError(DiscoveryError):
    """Raised when discovered artifact bytes cannot be safely replayed."""


@dataclass(frozen=True)
class DiscoveryPolicy:
    allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".csv",
    )
    archive_extensions: tuple[str, ...] = (".zip",)
    max_archive_depth: int = 4
    max_files: int = 5000
    max_single_file_bytes: int = 128 * 1024 * 1024
    max_total_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 150.0

    def __post_init__(self) -> None:
        if self.max_archive_depth < 0:
            raise ValueError("max_archive_depth must be >= 0")
        if self.max_files <= 0:
            raise ValueError("max_files must be > 0")
        if self.max_single_file_bytes <= 0:
            raise ValueError("max_single_file_bytes must be > 0")
        if self.max_total_uncompressed_bytes <= 0:
            raise ValueError("max_total_uncompressed_bytes must be > 0")
        if self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be > 0")

        normalized_allowed = tuple(
            sorted({_normalize_extension(ext) for ext in self.allowed_extensions})
        )
        normalized_archives = tuple(
            sorted({_normalize_extension(ext) for ext in self.archive_extensions})
        )
        object.__setattr__(self, "allowed_extensions", normalized_allowed)
        object.__setattr__(self, "archive_extensions", normalized_archives)


@dataclass(frozen=True)
class ArchiveHop:
    parent_archive_sha256: str
    member_path: str
    archive_depth: int


@dataclass(frozen=True)
class ArtifactOccurrence:
    source_locator: str
    extension: str
    archive_lineage: tuple[ArchiveHop, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DiscoveredArtifact:
    content_sha256: str
    size_bytes: int
    extension: str
    occurrences: tuple[ArtifactOccurrence, ...]

    @property
    def observed_extensions(self) -> tuple[str, ...]:
        return tuple(sorted({item.extension for item in self.occurrences}))


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    code: str
    locator_token: str
    detail: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    artifacts: tuple[DiscoveredArtifact, ...]
    diagnostics: tuple[DiscoveryDiagnostic, ...]
    archives_seen: int
    physical_files_seen: int
    supported_occurrences: int

    @property
    def unique_artifact_count(self) -> int:
        return len(self.artifacts)


@dataclass
class _ArtifactAccumulator:
    size_bytes: int
    occurrences: list[ArtifactOccurrence]


@dataclass
class _DiscoveryState:
    policy: DiscoveryPolicy
    artifacts: dict[str, _ArtifactAccumulator] = field(default_factory=dict)
    diagnostics: list[DiscoveryDiagnostic] = field(default_factory=list)
    archives_seen: int = 0
    physical_files_seen: int = 0
    supported_occurrences: int = 0
    total_uncompressed_bytes: int = 0


_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _normalize_extension(value: str) -> str:
    value = str(value).strip().lower()
    if not value:
        raise ValueError("extension must not be empty")
    if not value.startswith("."):
        value = "." + value
    return value


def _suffix(name: str) -> str:
    return Path(name).suffix.lower()


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _locator_token(locator: str) -> str:
    return sha256(locator.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def _safe_member_name(raw_name: str) -> str:
    if "\x00" in raw_name:
        raise ArchiveSafetyError("archive member contains NUL byte")

    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise ArchiveSafetyError("archive member path is absolute")
    if _DRIVE_PREFIX_RE.match(normalized):
        raise ArchiveSafetyError("archive member path contains drive prefix")

    parts = PurePosixPath(normalized).parts
    if not parts:
        raise ArchiveSafetyError("archive member path is empty")
    if any(part in ("", ".", "..") for part in parts):
        raise ArchiveSafetyError("archive member path is unsafe")

    return "/".join(parts)


def _record_diagnostic(
    state: _DiscoveryState,
    *,
    code: str,
    locator: str,
    detail: str = "",
) -> None:
    state.diagnostics.append(
        DiscoveryDiagnostic(
            code=code,
            locator_token=_locator_token(locator),
            detail=detail,
        )
    )


def _check_file_budget(
    state: _DiscoveryState,
    *,
    locator: str,
    size_bytes: int,
) -> None:
    state.physical_files_seen += 1
    if state.physical_files_seen > state.policy.max_files:
        raise ArchiveSafetyError(
            f"file-count limit exceeded at token={_locator_token(locator)}"
        )

    if size_bytes > state.policy.max_single_file_bytes:
        raise ArchiveSafetyError(
            f"single-file size limit exceeded at token={_locator_token(locator)}"
        )

    state.total_uncompressed_bytes += size_bytes
    if state.total_uncompressed_bytes > state.policy.max_total_uncompressed_bytes:
        raise ArchiveSafetyError(
            f"total uncompressed-size limit exceeded at token={_locator_token(locator)}"
        )


def _record_artifact(
    state: _DiscoveryState,
    *,
    content_sha256: str,
    size_bytes: int,
    occurrence: ArtifactOccurrence,
) -> None:
    state.supported_occurrences += 1
    existing = state.artifacts.get(content_sha256)

    if existing is None:
        state.artifacts[content_sha256] = _ArtifactAccumulator(
            size_bytes=size_bytes,
            occurrences=[occurrence],
        )
        return

    if existing.size_bytes != size_bytes:
        raise DiscoveryError(
            "SHA-256 collision safety check failed: same digest with different size"
        )

    existing.occurrences.append(occurrence)


def _common_artifact_extension(
    occurrences: Iterable[ArtifactOccurrence],
) -> str:
    observed = {item.extension for item in occurrences}
    if len(observed) == 1:
        return next(iter(observed))
    return ""


def _archive_ratio(info: zipfile.ZipInfo) -> float:
    if info.file_size == 0:
        return 0.0
    if info.compress_size <= 0:
        return float("inf")
    return info.file_size / info.compress_size


def _process_archive_bytes(
    data: bytes,
    *,
    locator: str,
    depth: int,
    lineage: tuple[ArchiveHop, ...],
    state: _DiscoveryState,
) -> None:
    if depth > state.policy.max_archive_depth:
        raise ArchiveSafetyError(
            f"archive recursion depth exceeded at token={_locator_token(locator)}"
        )

    archive_sha = _sha256_bytes(data)
    state.archives_seen += 1

    try:
        with zipfile.ZipFile(BytesIO(data), "r") as archive:
            infos = sorted(archive.infolist(), key=lambda item: item.filename)
            seen_member_paths: set[str] = set()

            for info in infos:
                if info.is_dir():
                    continue

                member_path = _safe_member_name(info.filename)
                member_locator = f"{locator}!/{member_path}"

                if member_path in seen_member_paths:
                    raise ArchiveSafetyError(
                        f"duplicate archive member path at token="
                        f"{_locator_token(member_locator)}"
                    )
                seen_member_paths.add(member_path)

                unix_mode = info.external_attr >> 16
                if info.create_system == 3 and stat.S_ISLNK(unix_mode):
                    raise ArchiveSafetyError(
                        f"archive symlink member unsupported at token="
                        f"{_locator_token(member_locator)}"
                    )

                if info.flag_bits & 0x1:
                    raise ArchiveSafetyError(
                        f"encrypted archive member unsupported at token="
                        f"{_locator_token(member_locator)}"
                    )

                ratio = _archive_ratio(info)
                if ratio > state.policy.max_compression_ratio:
                    raise ArchiveSafetyError(
                        f"compression-ratio limit exceeded at token="
                        f"{_locator_token(member_locator)}"
                    )

                _check_file_budget(
                    state,
                    locator=member_locator,
                    size_bytes=info.file_size,
                )

                try:
                    member_bytes = archive.read(info)
                except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                    raise ArchiveSafetyError(
                        f"archive member read failed at token="
                        f"{_locator_token(member_locator)}"
                    ) from exc

                if len(member_bytes) != info.file_size:
                    raise ArchiveSafetyError(
                        f"archive member size mismatch at token="
                        f"{_locator_token(member_locator)}"
                    )

                member_lineage = lineage + (
                    ArchiveHop(
                        parent_archive_sha256=archive_sha,
                        member_path=member_path,
                        archive_depth=depth,
                    ),
                )

                extension = _suffix(member_path)

                if extension in state.policy.archive_extensions:
                    _process_archive_bytes(
                        member_bytes,
                        locator=member_locator,
                        depth=depth + 1,
                        lineage=member_lineage,
                        state=state,
                    )
                    continue

                if extension not in state.policy.allowed_extensions:
                    _record_diagnostic(
                        state,
                        code="UNSUPPORTED_EXTENSION",
                        locator=member_locator,
                        detail=extension or "<none>",
                    )
                    continue

                _record_artifact(
                    state,
                    content_sha256=_sha256_bytes(member_bytes),
                    size_bytes=len(member_bytes),
                    occurrence=ArtifactOccurrence(
                        source_locator=member_locator,
                        extension=extension,
                        archive_lineage=member_lineage,
                    ),
                )
    except zipfile.BadZipFile as exc:
        raise ArchiveSafetyError(
            f"invalid ZIP archive at token={_locator_token(locator)}"
        ) from exc


def _process_regular_file(
    path: Path,
    *,
    root: Path,
    state: _DiscoveryState,
) -> None:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name

    locator = relative or path.name
    extension = path.suffix.lower()

    if path.is_symlink():
        _record_diagnostic(
            state,
            code="SYMLINK_SKIPPED",
            locator=locator,
        )
        return

    if not path.is_file():
        return

    size_bytes = path.stat().st_size
    _check_file_budget(
        state,
        locator=locator,
        size_bytes=size_bytes,
    )

    if extension in state.policy.archive_extensions:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DiscoveryError(
                f"cannot read archive at token={_locator_token(locator)}"
            ) from exc

        _process_archive_bytes(
            data,
            locator=locator,
            depth=1,
            lineage=(),
            state=state,
        )
        return

    if extension not in state.policy.allowed_extensions:
        _record_diagnostic(
            state,
            code="UNSUPPORTED_EXTENSION",
            locator=locator,
            detail=extension or "<none>",
        )
        return

    try:
        digest = sha256_file(path)
    except OSError as exc:
        raise DiscoveryError(
            f"cannot hash file at token={_locator_token(locator)}"
        ) from exc

    _record_artifact(
        state,
        content_sha256=digest,
        size_bytes=size_bytes,
        occurrence=ArtifactOccurrence(
            source_locator=locator,
            extension=extension,
            archive_lineage=(),
        ),
    )



def _safe_local_locator(raw_name: str) -> str:
    if "\x00" in raw_name:
        raise ArchiveSafetyError("local source locator contains NUL byte")
    if "\\" in raw_name:
        raise ArchiveSafetyError("local source locator is not normalized")
    if raw_name.startswith("/") or raw_name.startswith("//"):
        raise ArchiveSafetyError("local source locator is absolute")
    if _DRIVE_PREFIX_RE.match(raw_name):
        raise ArchiveSafetyError("local source locator contains drive prefix")

    parts = PurePosixPath(raw_name).parts
    if not parts:
        raise ArchiveSafetyError("local source locator is empty")
    if any(part in ("", ".", "..") for part in parts):
        raise ArchiveSafetyError("local source locator is unsafe")

    return "/".join(parts)


def _split_occurrence_locator(
    occurrence: ArtifactOccurrence,
) -> tuple[str, tuple[str, ...]]:
    raw_parts = occurrence.source_locator.split("!/")
    if not raw_parts or any(not part for part in raw_parts):
        raise ArtifactPayloadError(
            "source occurrence locator is malformed"
        )

    local_locator = _safe_local_locator(raw_parts[0])
    archive_members = tuple(
        _safe_member_name(part)
        for part in raw_parts[1:]
    )
    return local_locator, archive_members


def _validate_artifact_replay_contract(
    artifact: DiscoveredArtifact,
    *,
    policy: DiscoveryPolicy,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", artifact.content_sha256):
        raise ArtifactPayloadError(
            "artifact content SHA-256 is invalid"
        )
    if artifact.size_bytes < 0:
        raise ArtifactPayloadError("artifact size must be non-negative")
    if not artifact.occurrences:
        raise ArtifactPayloadError(
            "artifact has no source occurrence"
        )

    seen_locators: set[str] = set()

    for occurrence in artifact.occurrences:
        if occurrence.source_locator in seen_locators:
            raise ArtifactPayloadError(
                "artifact contains duplicate source occurrence metadata"
            )
        seen_locators.add(occurrence.source_locator)

        local_locator, archive_members = _split_occurrence_locator(
            occurrence
        )

        if len(archive_members) != len(occurrence.archive_lineage):
            raise ArtifactPayloadError(
                "source locator and archive lineage disagree"
            )
        if len(occurrence.archive_lineage) > policy.max_archive_depth:
            raise ArchiveSafetyError(
                "archive replay depth exceeds policy"
            )

        if occurrence.archive_lineage:
            if _suffix(local_locator) not in policy.archive_extensions:
                raise ArtifactPayloadError(
                    "archive lineage does not start from an archive"
                )
        elif _suffix(local_locator) not in policy.allowed_extensions:
            raise ArtifactPayloadError(
                "standalone occurrence extension is unsupported"
            )

        final_name = local_locator

        for index, hop in enumerate(
            occurrence.archive_lineage,
            start=1,
        ):
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                hop.parent_archive_sha256,
            ):
                raise ArtifactPayloadError(
                    "archive lineage SHA-256 is invalid"
                )

            normalized_member = _safe_member_name(hop.member_path)
            if normalized_member != hop.member_path:
                raise ArtifactPayloadError(
                    "archive lineage member path is not normalized"
                )

            if hop.archive_depth != index:
                raise ArtifactPayloadError(
                    "archive lineage depth is inconsistent"
                )

            if archive_members[index - 1] != normalized_member:
                raise ArtifactPayloadError(
                    "source locator and archive member path disagree"
                )

            if (
                index < len(occurrence.archive_lineage)
                and _suffix(normalized_member)
                not in policy.archive_extensions
            ):
                raise ArtifactPayloadError(
                    "intermediate archive lineage member is not an archive"
                )

            final_name = normalized_member

        expected_extension = _suffix(final_name)
        if occurrence.extension != expected_extension:
            raise ArtifactPayloadError(
                "occurrence extension disagrees with source locator"
            )
        if occurrence.extension not in policy.allowed_extensions:
            raise ArtifactPayloadError(
                "occurrence extension is unsupported"
            )

    expected_common = _common_artifact_extension(
        artifact.occurrences
    )
    if artifact.extension != expected_common:
        raise ArtifactPayloadError(
            "artifact extension disagrees with occurrence extensions"
        )


def _safe_local_occurrence_path(
    root_path: Path,
    local_locator: str,
    *,
    locator_token: str,
) -> Path:
    if root_path.is_symlink():
        raise ArtifactPayloadError(
            f"discovery root symlink unsupported at token={locator_token}"
        )

    if root_path.is_file():
        parts = PurePosixPath(local_locator).parts
        if len(parts) != 1 or parts[0] != root_path.name:
            raise ArtifactPayloadError(
                f"standalone source occurrence mismatch at token={locator_token}"
            )
        return root_path

    if not root_path.is_dir():
        raise ArtifactPayloadError(
            f"discovery root is unavailable at token={locator_token}"
        )

    candidate = root_path
    for part in PurePosixPath(local_locator).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ArtifactPayloadError(
                f"symlinked local source unsupported at token={locator_token}"
            )

    if not candidate.is_file():
        raise ArtifactPayloadError(
            f"source occurrence is unavailable at token={locator_token}"
        )

    return candidate


def _read_archive_member_for_replay(
    data: bytes,
    *,
    expected_parent_sha256: str,
    expected_member_path: str,
    expected_depth: int,
    locator: str,
    state: _DiscoveryState,
) -> bytes:
    token = _locator_token(locator)

    if expected_depth > state.policy.max_archive_depth:
        raise ArchiveSafetyError(
            f"archive replay depth exceeded at token={token}"
        )

    if _sha256_bytes(data) != expected_parent_sha256:
        raise ArtifactPayloadError(
            f"archive-hop SHA-256 mismatch at token={token}"
        )

    try:
        with zipfile.ZipFile(BytesIO(data), "r") as archive:
            infos = sorted(
                archive.infolist(),
                key=lambda item: item.filename,
            )
            seen_member_paths: set[str] = set()
            selected: zipfile.ZipInfo | None = None

            for info in infos:
                if info.is_dir():
                    continue

                member_path = _safe_member_name(info.filename)
                member_locator = f"{locator}!/{member_path}"

                if member_path in seen_member_paths:
                    raise ArchiveSafetyError(
                        "duplicate archive member path at token="
                        + _locator_token(member_locator)
                    )
                seen_member_paths.add(member_path)

                unix_mode = info.external_attr >> 16
                if (
                    info.create_system == 3
                    and stat.S_ISLNK(unix_mode)
                ):
                    raise ArchiveSafetyError(
                        "archive symlink member unsupported at token="
                        + _locator_token(member_locator)
                    )

                if info.flag_bits & 0x1:
                    raise ArchiveSafetyError(
                        "encrypted archive member unsupported at token="
                        + _locator_token(member_locator)
                    )

                if (
                    _archive_ratio(info)
                    > state.policy.max_compression_ratio
                ):
                    raise ArchiveSafetyError(
                        "compression-ratio limit exceeded at token="
                        + _locator_token(member_locator)
                    )

                _check_file_budget(
                    state,
                    locator=member_locator,
                    size_bytes=info.file_size,
                )

                if member_path == expected_member_path:
                    selected = info

            if selected is None:
                raise ArtifactPayloadError(
                    f"archive member missing at token={token}"
                )

            try:
                member_bytes = archive.read(selected)
            except (
                RuntimeError,
                zipfile.BadZipFile,
                OSError,
            ) as exc:
                raise ArchiveSafetyError(
                    f"archive member read failed at token={token}"
                ) from exc

            if len(member_bytes) != selected.file_size:
                raise ArchiveSafetyError(
                    f"archive member size mismatch at token={token}"
                )

            return member_bytes
    except zipfile.BadZipFile as exc:
        raise ArchiveSafetyError(
            f"invalid ZIP archive at token={token}"
        ) from exc


def read_discovered_artifact(
    root: str | Path,
    artifact: DiscoveredArtifact,
    *,
    policy: DiscoveryPolicy | None = None,
) -> bytes:
    # Replay one discovered artifact without extracting archive content to disk.
    policy = policy or DiscoveryPolicy()
    _validate_artifact_replay_contract(
        artifact,
        policy=policy,
    )

    occurrence = sorted(
        artifact.occurrences,
        key=lambda item: item.source_locator,
    )[0]
    token = _locator_token(occurrence.source_locator)

    root_path = Path(root)
    if root_path.is_symlink():
        raise ArtifactPayloadError(
            f"discovery root symlink unsupported at token={token}"
        )
    if not root_path.exists():
        raise ArtifactPayloadError(
            f"discovery root is unavailable at token={token}"
        )

    local_locator, archive_members = _split_occurrence_locator(
        occurrence
    )
    source_path = _safe_local_occurrence_path(
        root_path,
        local_locator,
        locator_token=token,
    )

    try:
        source_size = source_path.stat().st_size
    except OSError as exc:
        raise ArtifactPayloadError(
            f"cannot stat source occurrence at token={token}"
        ) from exc

    state = _DiscoveryState(policy=policy)
    _check_file_budget(
        state,
        locator=local_locator,
        size_bytes=source_size,
    )

    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise ArtifactPayloadError(
            f"cannot read source occurrence at token={token}"
        ) from exc

    if len(data) != source_size:
        raise ArtifactPayloadError(
            f"source occurrence changed during read at token={token}"
        )

    current = data
    replay_locator = local_locator

    for hop, member_path in zip(
        occurrence.archive_lineage,
        archive_members,
        strict=True,
    ):
        current = _read_archive_member_for_replay(
            current,
            expected_parent_sha256=hop.parent_archive_sha256,
            expected_member_path=member_path,
            expected_depth=hop.archive_depth,
            locator=replay_locator,
            state=state,
        )
        replay_locator = f"{replay_locator}!/{member_path}"

    if len(current) != artifact.size_bytes:
        raise ArtifactPayloadError(
            f"artifact size mismatch at token={token}"
        )

    if _sha256_bytes(current) != artifact.content_sha256:
        raise ArtifactPayloadError(
            f"artifact SHA-256 mismatch at token={token}"
        )

    return current


def discover_files(
    root: str | Path,
    *,
    policy: DiscoveryPolicy | None = None,
) -> DiscoveryResult:
    """
    Discovers supported source files and recursively traverses ZIP archives.

    Safety invariants:
    - no archive content is extracted to disk;
    - zip-slip style member names are rejected;
    - recursion, file-count, size, and compression-ratio limits fail closed;
    - exact byte duplicates collapse to one artifact while every occurrence is
      preserved;
    - diagnostics never contain file contents and identify locations by token.
    """
    policy = policy or DiscoveryPolicy()
    root_path = Path(root)

    if not root_path.exists():
        raise DiscoveryError("discovery root does not exist")

    state = _DiscoveryState(policy=policy)

    if root_path.is_symlink():
        raise DiscoveryError("discovery root must not be a symlink")

    if root_path.is_file():
        parent = root_path.parent
        _process_regular_file(
            root_path,
            root=parent,
            state=state,
        )
    elif root_path.is_dir():
        paths = root_path.rglob(
            "*",
            recurse_symlinks=False,
        )
        for path in sorted(
            paths,
            key=lambda item: item.as_posix(),
        ):
            if path.is_symlink():
                try:
                    locator = path.relative_to(root_path).as_posix()
                except ValueError:
                    locator = path.name

                code = (
                    "SYMLINK_DIRECTORY_SKIPPED"
                    if path.is_dir()
                    else "SYMLINK_SKIPPED"
                )
                _record_diagnostic(
                    state,
                    code=code,
                    locator=locator,
                )
                continue

            if path.is_dir():
                continue

            _process_regular_file(
                path,
                root=root_path,
                state=state,
            )
    else:
        raise DiscoveryError("discovery root must be a regular file or directory")

    artifacts = tuple(
        DiscoveredArtifact(
            content_sha256=digest,
            size_bytes=acc.size_bytes,
            extension=_common_artifact_extension(acc.occurrences),
            occurrences=tuple(
                sorted(
                    acc.occurrences,
                    key=lambda occurrence: occurrence.source_locator,
                )
            ),
        )
        for digest, acc in sorted(state.artifacts.items())
    )

    diagnostics = tuple(
        sorted(
            state.diagnostics,
            key=lambda item: (item.code, item.locator_token, item.detail),
        )
    )

    return DiscoveryResult(
        artifacts=artifacts,
        diagnostics=diagnostics,
        archives_seen=state.archives_seen,
        physical_files_seen=state.physical_files_seen,
        supported_occurrences=state.supported_occurrences,
    )
