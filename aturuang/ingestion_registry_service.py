from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

from aturuang.ingestion_contracts import (
    DocumentIdentityStatus,
    SourceDocumentIdentity,
    TemplateMatchStatus,
)
from aturuang.ingestion_registry import REGISTRY_TABLES


class RegistryServiceError(RuntimeError):
    """Base error for Universal Ingestion registry service operations."""


class RegistryNotInitializedError(RegistryServiceError):
    """Raised when registry service is used before registry schema exists."""


class RegistryConflictError(RegistryServiceError):
    """Raised when an idempotent registry operation conflicts with existing state."""


class RegistryAmbiguousError(RegistryServiceError):
    """Raised when a supposedly unique registry resolution is ambiguous."""


@dataclass(frozen=True)
class TemplateResolution:
    status: TemplateMatchStatus
    template_id: str | None = None
    parser_version: str | None = None


@dataclass(frozen=True)
class DocumentRegistrationResult:
    identity_status: DocumentIdentityStatus
    source_document_id: str
    related_document_id: str | None
    inserted: bool


@dataclass(frozen=True)
class SourceDocumentReadRecord:
    source_document_id: str
    source_registry_id: str
    content_sha256: str
    natural_document_key: str
    semantic_sha256: str | None


@dataclass(frozen=True)
class AccountResolutionResult:
    status: str
    account_id: str | None = None
    parent_account_id: str | None = None


_REQUIRED_SERVICE_TABLES = {
    "registry_accounts",
    "registry_legacy_account_links",
    "registry_sources",
    "registry_source_capabilities",
    "registry_source_templates",
    "registry_import_batches",
    "registry_source_documents",
    "registry_source_document_occurrences",
    "registry_account_observations",
}


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _require_registry_schema(con: sqlite3.Connection) -> None:
    rows = con.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name LIKE 'registry_%'
        """
    ).fetchall()
    names = {str(row[0]) for row in rows}
    missing = sorted(_REQUIRED_SERVICE_TABLES - names)
    if missing:
        raise RegistryNotInitializedError(
            "registry schema is not initialized; missing: "
            + ", ".join(missing)
        )


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def get_source_capability(
    con: sqlite3.Connection,
    source_registry_id: str,
) -> dict[str, Any] | None:
    """Returns one source capability record without mutating any ledger state."""
    _require_registry_schema(con)
    row = con.execute(
        """
        SELECT s.source_registry_id,
               s.provider_key,
               s.display_name,
               s.channel,
               s.institution_id,
               c.document_kind,
               c.completeness_role,
               c.has_transactions,
               c.has_balances,
               c.has_running_balance,
               c.has_native_event_id,
               c.has_account_hierarchy,
               c.supports_reconciliation,
               c.supports_commerce_enrichment,
               c.near_realtime
        FROM registry_sources AS s
        LEFT JOIN registry_source_capabilities AS c
          ON c.source_registry_id = s.source_registry_id
        WHERE s.source_registry_id=?
          AND s.active=1
        """,
        (source_registry_id,),
    ).fetchone()

    result = _rowdict(row)
    if result is None:
        return None

    for key in (
        "has_transactions",
        "has_balances",
        "has_running_balance",
        "has_native_event_id",
        "has_account_hierarchy",
        "supports_reconciliation",
        "supports_commerce_enrichment",
        "near_realtime",
    ):
        if result[key] is not None:
            result[key] = bool(result[key])

    return result


def resolve_template(
    con: sqlite3.Connection,
    *,
    source_registry_id: str,
    template_fingerprint: str,
) -> TemplateResolution:
    """
    Resolves a template fail-closed.

    KNOWN:
        exactly matching active source/fingerprint contract.
    TEMPLATE_DRIFT:
        source is known and has active templates, but fingerprint changed.
    UNKNOWN_TEMPLATE:
        source itself has no active template contract.
    """
    _require_registry_schema(con)

    row = con.execute(
        """
        SELECT template_id, parser_version
        FROM registry_source_templates
        WHERE source_registry_id=?
          AND template_fingerprint=?
          AND active=1
        """,
        (source_registry_id, template_fingerprint),
    ).fetchone()

    if row is not None:
        return TemplateResolution(
            status=TemplateMatchStatus.KNOWN,
            template_id=str(row["template_id"]),
            parser_version=str(row["parser_version"]),
        )

    known_source_template = con.execute(
        """
        SELECT 1
        FROM registry_source_templates
        WHERE source_registry_id=?
          AND active=1
        LIMIT 1
        """,
        (source_registry_id,),
    ).fetchone()

    if known_source_template is not None:
        return TemplateResolution(
            status=TemplateMatchStatus.TEMPLATE_DRIFT,
        )

    return TemplateResolution(
        status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
    )


def lookup_source_documents_by_content_sha(
    con: sqlite3.Connection,
    *,
    content_sha256: str,
) -> tuple[SourceDocumentReadRecord, ...]:
    """Returns existing document identity rows by exact SHA without writes."""
    _require_registry_schema(con)

    rows = con.execute(
        """
        SELECT source_document_id,
               source_registry_id,
               content_sha256,
               natural_document_key,
               semantic_sha256
        FROM registry_source_documents
        WHERE content_sha256=?
        ORDER BY source_document_id
        """,
        (content_sha256,),
    ).fetchall()

    return tuple(
        SourceDocumentReadRecord(
            source_document_id=str(row["source_document_id"]),
            source_registry_id=str(row["source_registry_id"]),
            content_sha256=str(row["content_sha256"]),
            natural_document_key=str(row["natural_document_key"]),
            semantic_sha256=(
                str(row["semantic_sha256"])
                if row["semantic_sha256"]
                else None
            ),
        )
        for row in rows
    )


def lookup_source_documents_by_natural_key(
    con: sqlite3.Connection,
    *,
    source_registry_id: str,
    natural_document_key: str,
) -> tuple[SourceDocumentReadRecord, ...]:
    """Returns same-source natural identity candidates without writes."""
    _require_registry_schema(con)
    rows = con.execute(
        """
        SELECT source_document_id,
               source_registry_id,
               content_sha256,
               natural_document_key,
               semantic_sha256
        FROM registry_source_documents
        WHERE source_registry_id=?
          AND natural_document_key=?
        ORDER BY source_document_id
        """,
        (source_registry_id, natural_document_key),
    ).fetchall()
    return tuple(
        SourceDocumentReadRecord(
            source_document_id=str(row["source_document_id"]),
            source_registry_id=str(row["source_registry_id"]),
            content_sha256=str(row["content_sha256"]),
            natural_document_key=str(row["natural_document_key"]),
            semantic_sha256=(
                str(row["semantic_sha256"])
                if row["semantic_sha256"]
                else None
            ),
        )
        for row in rows
    )


def create_or_get_import_batch(
    con: sqlite3.Connection,
    *,
    import_batch_id: str,
    source_registry_id: str,
    mode: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """
    Creates one registry import batch idempotently.

    This writes only registry_import_batches. It never creates legacy transactions,
    never mutates account balances, and never initializes registry schema itself.
    """
    _require_registry_schema(con)

    existing = con.execute(
        """
        SELECT *
        FROM registry_import_batches
        WHERE source_registry_id=?
          AND idempotency_key=?
        """,
        (source_registry_id, idempotency_key),
    ).fetchone()

    if existing is not None:
        if str(existing["mode"]) != mode:
            raise RegistryConflictError(
                "same source/idempotency_key already exists with different mode"
            )
        return _rowdict(existing) or {}

    try:
        con.execute(
            """
            INSERT INTO registry_import_batches (
                import_batch_id,
                source_registry_id,
                mode,
                idempotency_key
            ) VALUES (?, ?, ?, ?)
            """,
            (
                import_batch_id,
                source_registry_id,
                mode,
                idempotency_key,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryConflictError(str(exc)) from exc

    row = con.execute(
        """
        SELECT *
        FROM registry_import_batches
        WHERE import_batch_id=?
        """,
        (import_batch_id,),
    ).fetchone()
    return _rowdict(row) or {}


def _existing_document_rows(
    con: sqlite3.Connection,
    candidate: SourceDocumentIdentity,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT *
        FROM registry_source_documents
        WHERE content_sha256=?
           OR (
                source_registry_id=?
                AND natural_document_key=?
           )
        ORDER BY created_at, source_document_id
        """,
        (
            candidate.content_sha256,
            candidate.source_registry_id,
            candidate.natural_document_key,
        ),
    ).fetchall()


def _classify_document_rows(
    rows: list[sqlite3.Row],
    candidate: SourceDocumentIdentity,
) -> tuple[DocumentIdentityStatus, str | None]:
    for row in rows:
        if str(row["content_sha256"]) == candidate.content_sha256:
            return (
                DocumentIdentityStatus.EXACT_DUPLICATE,
                str(row["source_document_id"]),
            )

    same_natural = [
        row
        for row in rows
        if str(row["source_registry_id"]) == candidate.source_registry_id
        and str(row["natural_document_key"])
        == candidate.natural_document_key
    ]

    if not same_natural:
        return (DocumentIdentityStatus.NEW, None)

    if not candidate.semantic_sha256:
        return (DocumentIdentityStatus.AMBIGUOUS, None)

    for row in same_natural:
        existing_semantic = row["semantic_sha256"]
        if (
            existing_semantic
            and str(existing_semantic) == candidate.semantic_sha256
        ):
            return (
                DocumentIdentityStatus.SEMANTIC_DUPLICATE,
                str(row["source_document_id"]),
            )

    if all(row["semantic_sha256"] for row in same_natural):
        return (
            DocumentIdentityStatus.REVISION_OR_CONFLICT,
            str(same_natural[0]["source_document_id"]),
        )

    return (DocumentIdentityStatus.AMBIGUOUS, None)


def _validate_document_template_claim(
    con: sqlite3.Connection,
    candidate: SourceDocumentIdentity,
) -> None:
    resolution = resolve_template(
        con,
        source_registry_id=candidate.source_registry_id,
        template_fingerprint=candidate.template_fingerprint,
    )

    if candidate.template_match_status is TemplateMatchStatus.KNOWN:
        if resolution.status is not TemplateMatchStatus.KNOWN:
            raise RegistryConflictError(
                "document claims KNOWN template but registry does not confirm it"
            )
        if candidate.template_id != resolution.template_id:
            raise RegistryConflictError(
                "document template_id does not match registry resolution"
            )
        if candidate.parser_version != resolution.parser_version:
            raise RegistryConflictError(
                "document parser_version does not match registry template"
            )
        return

    if resolution.status is not candidate.template_match_status:
        raise RegistryConflictError(
            "document template-match status contradicts registry resolution"
        )


def register_source_document(
    con: sqlite3.Connection,
    candidate: SourceDocumentIdentity,
) -> DocumentRegistrationResult:
    """
    Registers source-document identity without invoking any provider parser.

    Exact byte duplicates reuse the original source_document_id and are not
    inserted a second time. Semantic duplicates/revisions retain their own
    source_document_id and link to prior evidence.
    """
    _require_registry_schema(con)

    rows = _existing_document_rows(con, candidate)
    identity_status, related_document_id = _classify_document_rows(
        rows,
        candidate,
    )

    if identity_status is DocumentIdentityStatus.EXACT_DUPLICATE:
        return DocumentRegistrationResult(
            identity_status=identity_status,
            source_document_id=related_document_id or "",
            related_document_id=related_document_id,
            inserted=False,
        )

    _validate_document_template_claim(con, candidate)

    stored_status = identity_status.value
    if stored_status == DocumentIdentityStatus.EXACT_DUPLICATE.value:
        raise RegistryConflictError(
            "EXACT_DUPLICATE must reuse existing document row"
        )

    try:
        con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id,
                source_registry_id,
                import_batch_id,
                content_sha256,
                natural_document_key,
                semantic_sha256,
                template_id,
                template_fingerprint,
                parser_version,
                template_match_status,
                period_status,
                period_start,
                period_end,
                identity_status,
                related_document_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.source_document_id,
                candidate.source_registry_id,
                candidate.import_batch_id,
                candidate.content_sha256,
                candidate.natural_document_key,
                candidate.semantic_sha256,
                candidate.template_id,
                candidate.template_fingerprint,
                candidate.parser_version,
                candidate.template_match_status.value,
                candidate.period_status.value,
                candidate.period_start,
                candidate.period_end,
                stored_status,
                related_document_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryConflictError(str(exc)) from exc

    return DocumentRegistrationResult(
        identity_status=identity_status,
        source_document_id=candidate.source_document_id,
        related_document_id=related_document_id,
        inserted=True,
    )


def record_source_document_occurrence(
    con: sqlite3.Connection,
    *,
    source_document_id: str,
    import_batch_id: str,
    source_locator: str,
    parent_archive_sha256: str | None = None,
    archive_depth: int = 0,
    member_path: str = "",
) -> dict[str, Any]:
    """
    Records where one physical occurrence was seen.

    Replaying the same batch+locator is idempotent only when it still points to
    the same source_document_id. A conflicting locator fails closed.
    """
    _require_registry_schema(con)

    existing = con.execute(
        """
        SELECT *
        FROM registry_source_document_occurrences
        WHERE import_batch_id=?
          AND source_locator=?
        """,
        (import_batch_id, source_locator),
    ).fetchone()

    if existing is not None:
        expected_archive_sha = parent_archive_sha256 or None
        existing_archive_sha = (
            str(existing["parent_archive_sha256"])
            if existing["parent_archive_sha256"]
            else None
        )
        if str(existing["source_document_id"]) != source_document_id:
            raise RegistryConflictError(
                "source locator already belongs to a different document"
            )
        if existing_archive_sha != expected_archive_sha:
            raise RegistryConflictError(
                "source locator replay has different parent archive"
            )
        if int(existing["archive_depth"]) != int(archive_depth):
            raise RegistryConflictError(
                "source locator replay has different archive depth"
            )
        if str(existing["member_path"]) != member_path:
            raise RegistryConflictError(
                "source locator replay has different member path"
            )
        return _rowdict(existing) or {}

    try:
        con.execute(
            """
            INSERT INTO registry_source_document_occurrences (
                source_document_id,
                import_batch_id,
                source_locator,
                parent_archive_sha256,
                archive_depth,
                member_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_document_id,
                import_batch_id,
                source_locator,
                parent_archive_sha256,
                archive_depth,
                member_path,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryConflictError(str(exc)) from exc

    row = con.execute(
        """
        SELECT *
        FROM registry_source_document_occurrences
        WHERE import_batch_id=?
          AND source_locator=?
        """,
        (import_batch_id, source_locator),
    ).fetchone()
    return _rowdict(row) or {}


def link_legacy_account_name(
    con: sqlite3.Connection,
    *,
    account_id: str,
    legacy_account_name: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    active: bool = True,
    require_legacy_account_exists: bool = True,
) -> dict[str, Any]:
    """
    Adds an explicit temporal bridge from legacy accounts(name) to stable ID.

    It never creates, renames, activates, deactivates, or changes the balance of
    a legacy account. When the legacy accounts table exists, the default is to
    require the named legacy account to already exist.
    """
    _require_registry_schema(con)

    account = con.execute(
        """
        SELECT account_id
        FROM registry_accounts
        WHERE account_id=?
        """,
        (account_id,),
    ).fetchone()
    if account is None:
        raise RegistryConflictError(
            f"registry account not found: {account_id}"
        )

    if require_legacy_account_exists and _table_exists(con, "accounts"):
        legacy = con.execute(
            """
            SELECT 1
            FROM accounts
            WHERE name=?
            """,
            (legacy_account_name,),
        ).fetchone()
        if legacy is None:
            raise RegistryConflictError(
                f"legacy account not found: {legacy_account_name}"
            )

    existing = con.execute(
        """
        SELECT *
        FROM registry_legacy_account_links
        WHERE account_id=?
          AND legacy_account_name=?
          AND COALESCE(valid_from, '')=COALESCE(?, '')
          AND COALESCE(valid_to, '')=COALESCE(?, '')
          AND active=?
        """,
        (
            account_id,
            legacy_account_name,
            valid_from,
            valid_to,
            1 if active else 0,
        ),
    ).fetchone()
    if existing is not None:
        return _rowdict(existing) or {}

    try:
        cur = con.execute(
            """
            INSERT INTO registry_legacy_account_links (
                account_id,
                legacy_account_name,
                valid_from,
                valid_to,
                active
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                account_id,
                legacy_account_name,
                valid_from,
                valid_to,
                1 if active else 0,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryConflictError(str(exc)) from exc

    row = con.execute(
        """
        SELECT *
        FROM registry_legacy_account_links
        WHERE legacy_link_id=?
        """,
        (cur.lastrowid,),
    ).fetchone()
    return _rowdict(row) or {}


def resolve_legacy_account_name(
    con: sqlite3.Connection,
    *,
    legacy_account_name: str,
    as_of_date: str | None = None,
) -> str | None:
    """
    Resolves legacy accounts(name) to one stable registry account_id.

    Current resolution uses active links. Historical resolution uses validity
    dates and intentionally ignores current active state.
    """
    _require_registry_schema(con)

    if as_of_date is None:
        rows = con.execute(
            """
            SELECT account_id
            FROM registry_legacy_account_links
            WHERE legacy_account_name=?
              AND active=1
            ORDER BY legacy_link_id
            """,
            (legacy_account_name,),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT account_id
            FROM registry_legacy_account_links
            WHERE legacy_account_name=?
              AND (valid_from IS NULL OR valid_from<=?)
              AND (valid_to IS NULL OR valid_to>=?)
            ORDER BY legacy_link_id
            """,
            (
                legacy_account_name,
                as_of_date,
                as_of_date,
            ),
        ).fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        raise RegistryAmbiguousError(
            f"legacy account name resolves to multiple stable IDs: "
            f"{legacy_account_name}"
        )

    return str(rows[0]["account_id"])


def resolve_account_observation(
    con: sqlite3.Connection,
    observation_id: int,
) -> AccountResolutionResult:
    """
    Resolves one source account observation using provider identity only.

    Display name alone is never an auto-resolution key. If the observed provider
    account key is unknown, the observation remains unresolved for later review.
    """
    _require_registry_schema(con)

    observation = con.execute(
        """
        SELECT *
        FROM registry_account_observations
        WHERE observation_id=?
        """,
        (observation_id,),
    ).fetchone()

    if observation is None:
        raise RegistryConflictError(
            f"account observation not found: {observation_id}"
        )

    if observation["resolved_account_id"]:
        return AccountResolutionResult(
            status="ALREADY_RESOLVED",
            account_id=str(observation["resolved_account_id"]),
            parent_account_id=(
                str(observation["parent_account_id"])
                if observation["parent_account_id"]
                else None
            ),
        )

    matches = con.execute(
        """
        SELECT account_id, parent_account_id
        FROM registry_accounts
        WHERE institution_id=?
          AND provider_account_key=?
          AND active=1
        """,
        (
            observation["institution_id"],
            observation["observed_account_key"],
        ),
    ).fetchall()

    if not matches:
        return AccountResolutionResult(
            status="UNRESOLVED",
        )

    if len(matches) > 1:
        raise RegistryAmbiguousError(
            "provider account key resolves to multiple active stable IDs"
        )

    account_id = str(matches[0]["account_id"])
    parent_account_id = (
        str(matches[0]["parent_account_id"])
        if matches[0]["parent_account_id"]
        else None
    )

    parent_observed_key = observation["parent_observed_account_key"]
    if parent_observed_key:
        parent_matches = con.execute(
            """
            SELECT account_id
            FROM registry_accounts
            WHERE institution_id=?
              AND provider_account_key=?
              AND active=1
            """,
            (
                observation["institution_id"],
                parent_observed_key,
            ),
        ).fetchall()

        if len(parent_matches) > 1:
            raise RegistryAmbiguousError(
                "parent provider account key resolves to multiple active stable IDs"
            )
        if len(parent_matches) == 1:
            observed_parent_id = str(parent_matches[0]["account_id"])
            if parent_account_id != observed_parent_id:
                raise RegistryConflictError(
                    "observed parent contradicts registered account hierarchy"
                )

    con.execute(
        """
        UPDATE registry_account_observations
        SET resolved_account_id=?,
            parent_account_id=COALESCE(?, parent_account_id)
        WHERE observation_id=?
        """,
        (
            account_id,
            parent_account_id,
            observation_id,
        ),
    )

    return AccountResolutionResult(
        status="RESOLVED_PROVIDER_KEY",
        account_id=account_id,
        parent_account_id=parent_account_id,
    )


def validate_legacy_bridge_readonly(
    con: sqlite3.Connection,
) -> dict[str, list[str]]:
    """
    Audits bridge links against legacy accounts without mutating either side.

    If no legacy accounts table is present, it reports that explicitly instead
    of creating one.
    """
    _require_registry_schema(con)

    if not _table_exists(con, "accounts"):
        return {
            "missing_legacy_table": ["accounts"],
            "missing_legacy_names": [],
            "dangling_registry_accounts": [],
        }

    missing_names = [
        str(row["legacy_account_name"])
        for row in con.execute(
            """
            SELECT l.legacy_account_name
            FROM registry_legacy_account_links AS l
            LEFT JOIN accounts AS a
              ON a.name = l.legacy_account_name
            WHERE a.name IS NULL
            ORDER BY l.legacy_account_name
            """
        ).fetchall()
    ]

    dangling_accounts = [
        str(row["account_id"])
        for row in con.execute(
            """
            SELECT l.account_id
            FROM registry_legacy_account_links AS l
            LEFT JOIN registry_accounts AS r
              ON r.account_id = l.account_id
            WHERE r.account_id IS NULL
            ORDER BY l.account_id
            """
        ).fetchall()
    ]

    return {
        "missing_legacy_table": [],
        "missing_legacy_names": missing_names,
        "dangling_registry_accounts": dangling_accounts,
    }
