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
        for path in sorted(root_path.rglob("*"), key=lambda item: item.as_posix()):
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
