-- Migration 0006: Atomic Review Guard & Unique Audit Operation Key
ALTER TABLE ingestion_audit_log ADD COLUMN operation_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_audit_op_key ON ingestion_audit_log(operation_key) WHERE operation_key IS NOT NULL;

INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (6, '0006_atomic_audit_guard');
