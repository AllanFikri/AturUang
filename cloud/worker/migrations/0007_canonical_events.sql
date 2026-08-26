-- Migration 0007: Canonical Financial Events & Multi-Email Evidence Graph
-- Additive, idempotent, and backward-compatible D1 schema migration

CREATE TABLE IF NOT EXISTS canonical_financial_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at_wib TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected', 'AutoApproved', 'Ignored', 'Applied')),
    event_kind TEXT NOT NULL CHECK(event_kind IN (
        'MERCHANT_PAYMENT', 'EXTERNAL_TRANSFER', 'INCOMING_TRANSFER', 'OWN_TRANSFER',
        'TOPUP', 'CASH_WITHDRAWAL', 'ALLOCATION_MOVEMENT', 'INVESTMENT_MOVEMENT',
        'REFUND', 'SUBSCRIPTION_CHARGE', 'DIGITAL_PURCHASE', 'INVOICE_EVIDENCE',
        'FAILED_ATTEMPT', 'NON_TRANSACTION'
    )),
    financial_class TEXT NOT NULL CHECK(financial_class IN (
        'Expense', 'Income', 'Internal Transfer', 'Top-up', 'Cash Withdrawal',
        'Investment Movement', 'Receivable', 'Payable', 'Refund', 'Pending Review', 'Ignore'
    )),
    financial_direction TEXT NOT NULL DEFAULT 'Debit' CHECK(financial_direction IN ('Debit', 'Credit', 'Neutral')),
    amount REAL NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL DEFAULT 'IDR',
    fee_amount REAL NOT NULL DEFAULT 0.0 CHECK(fee_amount >= 0),
    source_account_alias TEXT,
    destination_account_alias TEXT,
    destination_owner_type TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(destination_owner_type IN ('SELF', 'OTHER_PERSON', 'MERCHANT', 'INSTITUTION', 'UNKNOWN')),
    merchant_normalized TEXT,
    merchant_pan TEXT,
    merchant_location TEXT,
    counterparty_normalized TEXT,
    description_normalized TEXT,
    transaction_reference TEXT,
    external_order_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    recommended_action TEXT,
    review_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_canonical_events_event_id ON canonical_financial_events(event_id);
CREATE INDEX IF NOT EXISTS idx_canonical_events_occurred_at ON canonical_financial_events(occurred_at_wib);
CREATE INDEX IF NOT EXISTS idx_canonical_events_status ON canonical_financial_events(status);
CREATE INDEX IF NOT EXISTS idx_canonical_events_tx_ref ON canonical_financial_events(transaction_reference);
CREATE INDEX IF NOT EXISTS idx_canonical_events_order_id ON canonical_financial_events(external_order_id);

CREATE TABLE IF NOT EXISTS canonical_event_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_event_id INTEGER NOT NULL REFERENCES canonical_financial_events(id) ON DELETE CASCADE,
    raw_event_id INTEGER NOT NULL REFERENCES raw_events(id) ON DELETE CASCADE,
    candidate_id INTEGER REFERENCES ingestion_candidates(id) ON DELETE SET NULL,
    evidence_role TEXT NOT NULL CHECK(evidence_role IN ('PRIMARY_PAYMENT', 'SECONDARY_RECEIPT', 'INVOICE', 'LIFECYCLE_STATUS', 'INTERMEDIARY')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(canonical_event_id, raw_event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_evidence_event_id ON canonical_event_evidence(canonical_event_id);
CREATE INDEX IF NOT EXISTS idx_event_evidence_raw_id ON canonical_event_evidence(raw_event_id);
