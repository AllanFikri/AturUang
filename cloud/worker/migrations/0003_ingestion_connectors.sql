-- Migration 0003: Ingestion Connectors (Gmail Relay, Telegram Bot, Staging Queue)
-- Idempotent and reversible D1 schema migration

CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    occurred_at TEXT DEFAULT NULL,
    payload_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'Pending' CHECK(state IN ('Pending', 'Parsed', 'Ignored', 'Error')),
    minimal_raw_payload TEXT,
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_events_source_ext ON raw_events(source, external_id);
CREATE INDEX IF NOT EXISTS idx_raw_events_state ON raw_events(state);

CREATE TABLE IF NOT EXISTS ingestion_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id INTEGER NOT NULL REFERENCES raw_events(id) ON DELETE CASCADE,
    tx_type TEXT NOT NULL CHECK(tx_type IN ('Income', 'Expense', 'Transfer')),
    amount REAL NOT NULL CHECK(amount > 0),
    account TEXT NOT NULL,
    to_account TEXT DEFAULT NULL,
    category TEXT NOT NULL,
    money_context TEXT NOT NULL DEFAULT 'Personal' CHECK(money_context IN ('Personal', 'Pass-through', 'Historical Research', 'Third-party')),
    person_name TEXT DEFAULT NULL,
    date TEXT NOT NULL,
    time TEXT DEFAULT NULL,
    confidence_score REAL NOT NULL DEFAULT 1.0 CHECK(confidence_score >= 0.0 AND confidence_score <= 1.0),
    status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected', 'AutoApproved', 'Applied')),
    reasons TEXT,
    reviewed_at TEXT DEFAULT NULL,
    applied_transaction_id INTEGER DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingestion_candidates_raw_event ON ingestion_candidates(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_candidates_status ON ingestion_candidates(status);
CREATE INDEX IF NOT EXISTS idx_ingestion_candidates_date ON ingestion_candidates(date);

CREATE TABLE IF NOT EXISTS source_sync_state (
    source TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_event_at TEXT,
    cursor TEXT,
    status TEXT NOT NULL DEFAULT 'OK' CHECK(status IN ('OK', 'ERROR', 'STALE')),
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES ingestion_candidates(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingestion_audit_candidate ON ingestion_audit_log(candidate_id);

INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (3, '0003_ingestion_connectors');
