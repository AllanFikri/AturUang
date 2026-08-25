-- 0002_staging_schema.sql: Skema Staging Ingestion untuk Cloudflare D1
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
    total_events INTEGER NOT NULL DEFAULT 0,
    parsed_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    summary_notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS raw_import_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    external_event_id TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    minimal_raw_payload TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '1.0',
    parse_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(parse_status IN ('PENDING', 'PARSED', 'DUPLICATE', 'ERROR')),
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(batch_id) REFERENCES import_batches(id) ON DELETE CASCADE,
    UNIQUE(provider, external_event_id)
);

CREATE TABLE IF NOT EXISTS import_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    date TEXT NOT NULL,
    time TEXT NOT NULL DEFAULT '12:00',
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('Income', 'Expense', 'Transfer', 'Adjustment')),
    amount REAL NOT NULL CHECK(amount >= 0),
    account_from TEXT NOT NULL DEFAULT '',
    account_to TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Other / Miscellaneous',
    money_context TEXT NOT NULL DEFAULT 'Personal',
    confidence TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
    duplicate_status TEXT NOT NULL DEFAULT 'None' CHECK(duplicate_status IN ('None', 'Exact', 'Probable')),
    matched_transaction_id INTEGER,
    review_status TEXT NOT NULL DEFAULT 'Pending' CHECK(review_status IN ('Pending', 'Approved', 'Rejected', 'Duplicate', 'Error')),
    reviewer_notes TEXT NOT NULL DEFAULT '',
    approved_transaction_id INTEGER,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(raw_event_id) REFERENCES raw_import_events(id) ON DELETE CASCADE,
    FOREIGN KEY(matched_transaction_id) REFERENCES transactions(id) ON DELETE SET NULL,
    FOREIGN KEY(approved_transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_import_candidates_status ON import_candidates(review_status);
CREATE INDEX IF NOT EXISTS idx_import_candidates_date ON import_candidates(date);

INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (2, '0002_staging_schema');
