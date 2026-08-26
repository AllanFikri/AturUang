-- Migration 0005: Telegram Callback Guard for Durable Idempotency
CREATE TABLE IF NOT EXISTS telegram_callback_guard (
    callback_id TEXT PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tg_cb_cand ON telegram_callback_guard(candidate_id);

INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (5, '0005_telegram_callback_guard');
