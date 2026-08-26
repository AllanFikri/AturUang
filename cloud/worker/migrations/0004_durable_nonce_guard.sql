-- Migration 0004: Durable Nonce Replay Guard for Gmail Relay
CREATE TABLE IF NOT EXISTS gmail_replay_nonces (
    nonce TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gmail_nonces_ts ON gmail_replay_nonces(timestamp);

INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (4, '0004_durable_nonce_guard');
