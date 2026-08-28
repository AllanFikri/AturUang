from __future__ import annotations

import sqlite3


REGISTRY_SCHEMA_VERSION = 1

REGISTRY_TABLES = (
    "registry_commerce_line_allocations",
    "registry_commerce_orders",
    "registry_account_lifecycle_events",
    "registry_account_observations",
    "registry_legacy_account_links",
    "registry_source_document_occurrences",
    "registry_source_documents",
    "registry_import_batches",
    "registry_accounts",
    "registry_source_templates",
    "registry_source_capabilities",
    "registry_sources",
    "registry_institutions",
)

REGISTRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registry_institutions (
    institution_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    institution_type TEXT NOT NULL
        CHECK(institution_type IN ('BANK','WALLET','BROKER','COMMERCE','CASH','OTHER')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS registry_sources (
    source_registry_id TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    channel TEXT NOT NULL
        CHECK(channel IN ('PDF','IMAGE','EMAIL','CSV','API','MANUAL')),
    institution_id TEXT REFERENCES registry_institutions(institution_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_registry_sources_provider
    ON registry_sources(provider_key);

CREATE TABLE IF NOT EXISTS registry_source_capabilities (
    source_registry_id TEXT PRIMARY KEY
        REFERENCES registry_sources(source_registry_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    document_kind TEXT NOT NULL,
    completeness_role TEXT NOT NULL
        CHECK(completeness_role IN (
            'COMPLETE','PARTIAL','SNAPSHOT','ENRICHMENT','NEAR_REALTIME','UNKNOWN'
        )),
    has_transactions INTEGER NOT NULL DEFAULT 0 CHECK(has_transactions IN (0,1)),
    has_balances INTEGER NOT NULL DEFAULT 0 CHECK(has_balances IN (0,1)),
    has_running_balance INTEGER NOT NULL DEFAULT 0 CHECK(has_running_balance IN (0,1)),
    has_native_event_id INTEGER NOT NULL DEFAULT 0 CHECK(has_native_event_id IN (0,1)),
    has_account_hierarchy INTEGER NOT NULL DEFAULT 0 CHECK(has_account_hierarchy IN (0,1)),
    supports_reconciliation INTEGER NOT NULL DEFAULT 0 CHECK(supports_reconciliation IN (0,1)),
    supports_commerce_enrichment INTEGER NOT NULL DEFAULT 0 CHECK(supports_commerce_enrichment IN (0,1)),
    near_realtime INTEGER NOT NULL DEFAULT 0 CHECK(near_realtime IN (0,1))
);

CREATE TABLE IF NOT EXISTS registry_source_templates (
    template_id TEXT PRIMARY KEY,
    source_registry_id TEXT NOT NULL
        REFERENCES registry_sources(source_registry_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    template_version TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    document_kind TEXT NOT NULL,
    input_type TEXT NOT NULL
        CHECK(input_type IN ('PDF','IMAGE','EMAIL','CSV','API','MANUAL')),
    template_fingerprint TEXT NOT NULL,
    text_layer_required INTEGER NOT NULL DEFAULT 0
        CHECK(text_layer_required IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_registry_id, template_fingerprint)
);

CREATE TABLE IF NOT EXISTS registry_accounts (
    account_id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL
        REFERENCES registry_institutions(institution_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    display_name TEXT NOT NULL,
    account_type TEXT NOT NULL
        CHECK(account_type IN (
            'TRANSACTIONAL','SAVINGS','SUBACCOUNT','WALLET','RDN',
            'INVESTMENT','CASH','COMMERCE','OTHER'
        )),
    ownership_state TEXT NOT NULL
        CHECK(ownership_state IN ('OWNED','THIRD_PARTY','UNKNOWN')),
    ownership_confidence TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK(ownership_confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    parent_account_id TEXT
        REFERENCES registry_accounts(account_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    provider_account_key TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(parent_account_id IS NULL OR parent_account_id <> account_id)
);

CREATE INDEX IF NOT EXISTS idx_registry_accounts_institution
    ON registry_accounts(institution_id);

CREATE INDEX IF NOT EXISTS idx_registry_accounts_parent
    ON registry_accounts(parent_account_id);

CREATE INDEX IF NOT EXISTS idx_registry_accounts_provider_key
    ON registry_accounts(institution_id, provider_account_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_active_provider_slot
    ON registry_accounts(institution_id, provider_account_key)
    WHERE active = 1
      AND provider_account_key IS NOT NULL
      AND provider_account_key <> '';

CREATE TRIGGER IF NOT EXISTS trg_registry_account_parent_same_institution_insert
BEFORE INSERT ON registry_accounts
WHEN NEW.parent_account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.parent_account_id
        ) IS NULL
        THEN RAISE(ABORT, 'parent account not found')
        WHEN (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.parent_account_id
        ) <> NEW.institution_id
        THEN RAISE(ABORT, 'parent account institution mismatch')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_registry_account_parent_same_institution_update
BEFORE UPDATE OF parent_account_id, institution_id ON registry_accounts
WHEN NEW.parent_account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.parent_account_id
        ) IS NULL
        THEN RAISE(ABORT, 'parent account not found')
        WHEN (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.parent_account_id
        ) <> NEW.institution_id
        THEN RAISE(ABORT, 'parent account institution mismatch')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_registry_account_parent_cycle_update
BEFORE UPDATE OF parent_account_id ON registry_accounts
WHEN NEW.parent_account_id IS NOT NULL
BEGIN
    WITH RECURSIVE ancestors(account_id, parent_account_id) AS (
        SELECT account_id, parent_account_id
        FROM registry_accounts
        WHERE account_id = NEW.parent_account_id

        UNION ALL

        SELECT a.account_id, a.parent_account_id
        FROM registry_accounts AS a
        JOIN ancestors AS x
          ON a.account_id = x.parent_account_id
        WHERE x.parent_account_id IS NOT NULL
    )
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM ancestors
            WHERE account_id = NEW.account_id
        )
        THEN RAISE(ABORT, 'account hierarchy cycle')
    END;
END;

CREATE TABLE IF NOT EXISTS registry_legacy_account_links (
    legacy_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL
        REFERENCES registry_accounts(account_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    legacy_account_name TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_registry_legacy_links_account
    ON registry_legacy_account_links(account_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_active_legacy_account_name
    ON registry_legacy_account_links(legacy_account_name)
    WHERE active = 1;

CREATE TABLE IF NOT EXISTS registry_import_batches (
    import_batch_id TEXT PRIMARY KEY,
    source_registry_id TEXT NOT NULL
        REFERENCES registry_sources(source_registry_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    mode TEXT NOT NULL CHECK(mode IN ('DRY_RUN','STAGING')),
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS'
        CHECK(status IN ('IN_PROGRESS','COMPLETED','FAILED','REVIEW_REQUIRED')),
    idempotency_key TEXT NOT NULL,
    document_count INTEGER NOT NULL DEFAULT 0 CHECK(document_count >= 0),
    legacy_import_batch_id INTEGER,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    UNIQUE(source_registry_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS registry_source_documents (
    source_document_id TEXT PRIMARY KEY,
    source_registry_id TEXT NOT NULL
        REFERENCES registry_sources(source_registry_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    import_batch_id TEXT NOT NULL
        REFERENCES registry_import_batches(import_batch_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    content_sha256 TEXT NOT NULL UNIQUE
        CHECK(
            length(content_sha256) = 64
            AND content_sha256 = lower(content_sha256)
            AND content_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    natural_document_key TEXT NOT NULL,
    semantic_sha256 TEXT
        CHECK(
            semantic_sha256 IS NULL
            OR (
                length(semantic_sha256) = 64
                AND semantic_sha256 = lower(semantic_sha256)
                AND semantic_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
    template_id TEXT NOT NULL,
    template_fingerprint TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    template_match_status TEXT NOT NULL
        CHECK(template_match_status IN ('KNOWN','UNKNOWN_TEMPLATE','TEMPLATE_DRIFT')),
    period_status TEXT NOT NULL
        CHECK(period_status IN ('CLOSED','OPEN','PARTIAL','UNKNOWN')),
    period_start TEXT,
    period_end TEXT,
    identity_status TEXT NOT NULL DEFAULT 'NEW'
        CHECK(identity_status IN (
            'NEW','SEMANTIC_DUPLICATE','REVISION_OR_CONFLICT','AMBIGUOUS'
        )),
    related_document_id TEXT
        REFERENCES registry_source_documents(source_document_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        identity_status NOT IN ('SEMANTIC_DUPLICATE','REVISION_OR_CONFLICT')
        OR related_document_id IS NOT NULL
    )
);

CREATE TRIGGER IF NOT EXISTS trg_registry_document_batch_source_insert
BEFORE INSERT ON registry_source_documents
BEGIN
    SELECT CASE
        WHEN (
            SELECT source_registry_id
            FROM registry_import_batches
            WHERE import_batch_id = NEW.import_batch_id
        ) IS NULL
        THEN RAISE(ABORT, 'import batch not found')
        WHEN (
            SELECT source_registry_id
            FROM registry_import_batches
            WHERE import_batch_id = NEW.import_batch_id
        ) <> NEW.source_registry_id
        THEN RAISE(ABORT, 'document source does not match import batch source')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_registry_document_batch_source_update
BEFORE UPDATE OF import_batch_id, source_registry_id ON registry_source_documents
BEGIN
    SELECT CASE
        WHEN (
            SELECT source_registry_id
            FROM registry_import_batches
            WHERE import_batch_id = NEW.import_batch_id
        ) IS NULL
        THEN RAISE(ABORT, 'import batch not found')
        WHEN (
            SELECT source_registry_id
            FROM registry_import_batches
            WHERE import_batch_id = NEW.import_batch_id
        ) <> NEW.source_registry_id
        THEN RAISE(ABORT, 'document source does not match import batch source')
    END;
END;

CREATE INDEX IF NOT EXISTS idx_registry_documents_natural_key
    ON registry_source_documents(source_registry_id, natural_document_key);

CREATE INDEX IF NOT EXISTS idx_registry_documents_semantic_sha
    ON registry_source_documents(semantic_sha256);

CREATE INDEX IF NOT EXISTS idx_registry_documents_period
    ON registry_source_documents(source_registry_id, period_start, period_end);

CREATE TABLE IF NOT EXISTS registry_source_document_occurrences (
    occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id TEXT NOT NULL
        REFERENCES registry_source_documents(source_document_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    import_batch_id TEXT NOT NULL
        REFERENCES registry_import_batches(import_batch_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    source_locator TEXT NOT NULL,
    parent_archive_sha256 TEXT
        CHECK(
            parent_archive_sha256 IS NULL
            OR (
                length(parent_archive_sha256) = 64
                AND parent_archive_sha256 = lower(parent_archive_sha256)
                AND parent_archive_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
    archive_depth INTEGER NOT NULL DEFAULT 0 CHECK(archive_depth >= 0),
    member_path TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(import_batch_id, source_locator)
);

CREATE INDEX IF NOT EXISTS idx_registry_occurrences_document
    ON registry_source_document_occurrences(source_document_id);

CREATE TABLE IF NOT EXISTS registry_account_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id TEXT NOT NULL
        REFERENCES registry_source_documents(source_document_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    institution_id TEXT NOT NULL
        REFERENCES registry_institutions(institution_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    observed_account_key TEXT NOT NULL,
    display_name_raw TEXT NOT NULL,
    ownership_state TEXT NOT NULL
        CHECK(ownership_state IN ('OWNED','THIRD_PARTY','UNKNOWN')),
    lifecycle_state TEXT NOT NULL
        CHECK(lifecycle_state IN (
            'NEW','RENAMED','CLOSED','REUSED','UNCHANGED','UNVERIFIED'
        )),
    lifecycle_evidence TEXT NOT NULL,
    parent_observed_account_key TEXT,
    parent_account_id TEXT
        REFERENCES registry_accounts(account_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    resolved_account_id TEXT
        REFERENCES registry_accounts(account_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_registry_account_observations_key
    ON registry_account_observations(institution_id, observed_account_key);

CREATE TABLE IF NOT EXISTS registry_account_lifecycle_events (
    lifecycle_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL
        REFERENCES registry_accounts(account_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    event_type TEXT NOT NULL
        CHECK(event_type IN (
            'NEW','RENAMED','CLOSED','REUSED','UNCHANGED','UNVERIFIED'
        )),
    effective_at TEXT NOT NULL,
    source_document_id TEXT NOT NULL
        REFERENCES registry_source_documents(source_document_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    previous_alias TEXT,
    new_alias TEXT,
    related_account_id TEXT
        REFERENCES registry_accounts(account_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        event_type <> 'RENAMED'
        OR (
            previous_alias IS NOT NULL
            AND new_alias IS NOT NULL
            AND previous_alias <> new_alias
        )
    ),
    CHECK(
        event_type <> 'REUSED'
        OR (
            related_account_id IS NOT NULL
            AND related_account_id <> account_id
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_registry_lifecycle_account
    ON registry_account_lifecycle_events(account_id, effective_at);

CREATE TRIGGER IF NOT EXISTS trg_registry_lifecycle_reuse_integrity_insert
BEFORE INSERT ON registry_account_lifecycle_events
WHEN NEW.event_type = 'REUSED'
BEGIN
    SELECT CASE
        WHEN (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.account_id
        ) <> (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.related_account_id
        )
        THEN RAISE(ABORT, 'reused accounts must share institution')
        WHEN COALESCE((
            SELECT provider_account_key
            FROM registry_accounts
            WHERE account_id = NEW.account_id
        ), '') = ''
        THEN RAISE(ABORT, 'reused account requires provider slot')
        WHEN (
            SELECT provider_account_key
            FROM registry_accounts
            WHERE account_id = NEW.account_id
        ) <> (
            SELECT provider_account_key
            FROM registry_accounts
            WHERE account_id = NEW.related_account_id
        )
        THEN RAISE(ABORT, 'reused accounts must share provider slot')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_registry_lifecycle_reuse_integrity_update
BEFORE UPDATE OF account_id, event_type, related_account_id
ON registry_account_lifecycle_events
WHEN NEW.event_type = 'REUSED'
BEGIN
    SELECT CASE
        WHEN (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.account_id
        ) <> (
            SELECT institution_id
            FROM registry_accounts
            WHERE account_id = NEW.related_account_id
        )
        THEN RAISE(ABORT, 'reused accounts must share institution')
        WHEN COALESCE((
            SELECT provider_account_key
            FROM registry_accounts
            WHERE account_id = NEW.account_id
        ), '') = ''
        THEN RAISE(ABORT, 'reused account requires provider slot')
        WHEN (
            SELECT provider_account_key
            FROM registry_accounts
            WHERE account_id = NEW.account_id
        ) <> (
            SELECT provider_account_key
            FROM registry_accounts
            WHERE account_id = NEW.related_account_id
        )
        THEN RAISE(ABORT, 'reused accounts must share provider slot')
    END;
END;

CREATE TABLE IF NOT EXISTS registry_commerce_orders (
    commerce_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_registry_id TEXT NOT NULL
        REFERENCES registry_sources(source_registry_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    source_document_id TEXT NOT NULL
        REFERENCES registry_source_documents(source_document_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    order_key TEXT NOT NULL,
    commerce_actor TEXT NOT NULL,
    recipient TEXT,
    payer_actor TEXT,
    economic_owner_actor TEXT,
    economic_owner TEXT NOT NULL
        CHECK(economic_owner IN ('SELF','THIRD_PARTY','MIXED','UNKNOWN')),
    payer_responsibility TEXT NOT NULL
        CHECK(payer_responsibility IN (
            'SELF','THIRD_PARTY_DIRECT','SELF_REIMBURSABLE','UNKNOWN'
        )),
    payment_match_status TEXT NOT NULL
        CHECK(payment_match_status IN (
            'MATCHED','AMBIGUOUS','UNMATCHED','NOT_APPLICABLE'
        )),
    personal_spending_effect TEXT NOT NULL
        CHECK(personal_spending_effect IN (
            'PERSONAL_EXPENSE','NO_PERSONAL_EXPENSE',
            'PASS_THROUGH_RECEIVABLE','MIXED_ALLOCATION','REVIEW_REQUIRED'
        )),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_registry_id, order_key),
    CHECK(
        payer_responsibility <> 'THIRD_PARTY_DIRECT'
        OR personal_spending_effect = 'NO_PERSONAL_EXPENSE'
    ),
    CHECK(
        payer_responsibility <> 'SELF_REIMBURSABLE'
        OR personal_spending_effect = 'PASS_THROUGH_RECEIVABLE'
    ),
    CHECK(
        economic_owner <> 'MIXED'
        OR personal_spending_effect = 'MIXED_ALLOCATION'
    ),
    CHECK(
        NOT (
            economic_owner = 'UNKNOWN'
            OR payer_responsibility = 'UNKNOWN'
            OR payment_match_status IN ('AMBIGUOUS','UNMATCHED')
        )
        OR personal_spending_effect = 'REVIEW_REQUIRED'
    )
);

CREATE TABLE IF NOT EXISTS registry_commerce_line_allocations (
    line_allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commerce_order_id INTEGER NOT NULL
        REFERENCES registry_commerce_orders(commerce_order_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    line_key TEXT NOT NULL,
    economic_owner TEXT NOT NULL
        CHECK(economic_owner IN ('SELF','THIRD_PARTY','UNKNOWN')),
    payer_responsibility TEXT NOT NULL
        CHECK(payer_responsibility IN (
            'SELF','THIRD_PARTY_DIRECT','SELF_REIMBURSABLE','UNKNOWN'
        )),
    payer_actor TEXT,
    economic_owner_actor TEXT,
    UNIQUE(commerce_order_id, line_key)
);
"""


def init_registry_schema(con: sqlite3.Connection) -> None:
    """Creates only Universal Ingestion registry tables on the supplied connection."""
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(REGISTRY_SCHEMA_SQL)


def drop_registry_schema(con: sqlite3.Connection) -> None:
    """Drops only Universal Ingestion registry objects from the supplied connection."""
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        DROP TRIGGER IF EXISTS trg_registry_lifecycle_reuse_integrity_update;
        DROP TRIGGER IF EXISTS trg_registry_lifecycle_reuse_integrity_insert;
        DROP TRIGGER IF EXISTS trg_registry_document_batch_source_update;
        DROP TRIGGER IF EXISTS trg_registry_document_batch_source_insert;
        DROP TRIGGER IF EXISTS trg_registry_account_parent_cycle_update;
        DROP TRIGGER IF EXISTS trg_registry_account_parent_same_institution_update;
        DROP TRIGGER IF EXISTS trg_registry_account_parent_same_institution_insert;

        DROP TABLE IF EXISTS registry_commerce_line_allocations;
        DROP TABLE IF EXISTS registry_commerce_orders;
        DROP TABLE IF EXISTS registry_account_lifecycle_events;
        DROP TABLE IF EXISTS registry_account_observations;
        DROP TABLE IF EXISTS registry_source_document_occurrences;
        DROP TABLE IF EXISTS registry_source_documents;
        DROP TABLE IF EXISTS registry_import_batches;
        DROP TABLE IF EXISTS registry_legacy_account_links;
        DROP TABLE IF EXISTS registry_accounts;
        DROP TABLE IF EXISTS registry_source_templates;
        DROP TABLE IF EXISTS registry_source_capabilities;
        DROP TABLE IF EXISTS registry_sources;
        DROP TABLE IF EXISTS registry_institutions;
        """
    )


def registry_table_names(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name LIKE 'registry_%'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}
