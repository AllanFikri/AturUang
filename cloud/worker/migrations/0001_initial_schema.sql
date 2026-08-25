-- 0001_initial_schema.sql: Migration Skema Utama AturUang untuk Cloudflare D1
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Table: accounts
CREATE TABLE IF NOT EXISTS "accounts" (
                    name TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('Owned','Receivable','Suspense','External','Investment')),
                    current_balance REAL,
                    balance_date TEXT,
                    protected INTEGER NOT NULL DEFAULT 0,
                    protected_amount REAL NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT ''
                , last_reconciled_at TEXT NOT NULL DEFAULT '');

-- Table: ai_chat_history
CREATE TABLE IF NOT EXISTS ai_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT 'default',
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                intent TEXT,
                action_proposal TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

-- Table: allocation_goals
CREATE TABLE IF NOT EXISTS allocation_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('Emergency','Goal','General')),
            target_amount REAL NOT NULL DEFAULT 0.0 CHECK(target_amount >= 0),
            allocated_amount REAL NOT NULL DEFAULT 0.0 CHECK(allocated_amount >= 0),
            target_date TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Achieved','Released','Cancelled')),
            preferred_account TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

-- Table: attachments
CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                mime_type TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id)
            );

-- Table: balance_snapshots
CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                balance REAL NOT NULL,
                snapshot_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, snapshot_kind TEXT NOT NULL DEFAULT 'legacy',
                FOREIGN KEY(account_name) REFERENCES accounts(name)
            );

-- Table: budget_categories
CREATE TABLE IF NOT EXISTS budget_categories (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                original_budget REAL NOT NULL DEFAULT 0,
                current_budget REAL NOT NULL DEFAULT 0,
                suggested_budget REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                rollover INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(month, category)
            );

-- Table: budget_reallocations
CREATE TABLE IF NOT EXISTS budget_reallocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                from_category TEXT NOT NULL,
                to_category TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

-- Table: debt_events
CREATE TABLE IF NOT EXISTS debt_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL,
                transaction_id INTEGER,
                event_type TEXT NOT NULL CHECK(event_type IN ('Opening','Increase','Settlement','Correction','WriteOff')),
                effect INTEGER NOT NULL CHECK(effect IN (-1, 1)),
                amount REAL NOT NULL CHECK(amount > 0),
                event_date TEXT NOT NULL,
                reversal_of_id INTEGER,
                operation_key TEXT UNIQUE,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(debt_id) REFERENCES debts(id) ON DELETE CASCADE,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL,
                FOREIGN KEY(reversal_of_id) REFERENCES debt_events(id)
            );

-- Table: debts
CREATE TABLE IF NOT EXISTS debts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('Custody','Receivable','Payable')),
                    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Settled','WrittenOff')),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

-- Table: financial_notes
CREATE TABLE IF NOT EXISTS financial_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                target_type TEXT NOT NULL DEFAULT 'general',
                target_id INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT ''
            );

-- Table: goals
CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_amount REAL NOT NULL,
                current_saved REAL NOT NULL DEFAULT 0,
                target_date TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'Medium',
                linked_account TEXT NOT NULL DEFAULT '',
                protected INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Active',
                notes TEXT NOT NULL DEFAULT ''
            );

-- Table: idempotency_requests
CREATE TABLE IF NOT EXISTS idempotency_requests (
                endpoint_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('IN_PROGRESS','COMPLETED')),
                http_status INTEGER DEFAULT NULL,
                response_json TEXT DEFAULT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT DEFAULT NULL,
                PRIMARY KEY (endpoint_scope, idempotency_key)
            );

-- Table: monthly_plans
CREATE TABLE IF NOT EXISTS monthly_plans (
                month TEXT PRIMARY KEY,
                guaranteed_income REAL NOT NULL DEFAULT 0,
                expected_additional_income REAL NOT NULL DEFAULT 0,
                savings_rate REAL NOT NULL DEFAULT 0.20,
                notes TEXT NOT NULL DEFAULT ''
            );

-- Table: protected_allocations
CREATE TABLE IF NOT EXISTS protected_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                account TEXT NOT NULL DEFAULT '',
                target_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Spent','Released')),
                notes TEXT NOT NULL DEFAULT ''
            , covers_upcoming_id INTEGER DEFAULT NULL REFERENCES upcoming(id) ON DELETE SET NULL);

-- Table: reconciliations
CREATE TABLE IF NOT EXISTS reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                book_balance REAL NOT NULL,
                actual_balance REAL NOT NULL,
                difference REAL NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                date_created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_name) REFERENCES accounts(name)
            );

-- Table: settings
CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

-- Table: statement_archives
CREATE TABLE IF NOT EXISTS statement_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL UNIQUE,
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                summary_json TEXT NOT NULL DEFAULT '{}',
                html_ref TEXT NOT NULL DEFAULT '',
                hash TEXT NOT NULL DEFAULT '',
                is_revised INTEGER NOT NULL DEFAULT 0
            );

-- Table: subscriptions
CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                amount REAL NOT NULL,
                billing_cycle TEXT NOT NULL DEFAULT 'Monthly',
                next_payment TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Subscriptions',
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT ''
            );

-- Table: tags
CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT NOT NULL DEFAULT '#10b981'
            );

-- Table: transaction_audit_log
CREATE TABLE IF NOT EXISTS transaction_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                old_data TEXT NOT NULL DEFAULT '',
                new_data TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

-- Table: transaction_tags
CREATE TABLE IF NOT EXISTS transaction_tags (
                transaction_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY(transaction_id, tag_id),
                FOREIGN KEY(transaction_id) REFERENCES transactions(id),
                FOREIGN KEY(tag_id) REFERENCES tags(id)
            );

-- Table: transactions
CREATE TABLE IF NOT EXISTS "transactions" (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT UNIQUE,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL DEFAULT '',
                    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('Income','Expense','Transfer','Adjustment')),
                    amount REAL NOT NULL CHECK(amount >= 0),
                    account_from TEXT NOT NULL DEFAULT '',
                    account_to TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    for_with_whom TEXT NOT NULL DEFAULT 'Personal / Self',
                    money_context TEXT NOT NULL DEFAULT 'Personal' CHECK(money_context IN ('Personal','Pass-through','Historical Research','Third-party')),
                    settlement_kind TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Confirmed' CHECK(status IN ('Confirmed','Auto-classified','Provisional Neutral')),
                    confidence TEXT NOT NULL DEFAULT 'high',
                    budget_effect REAL NOT NULL DEFAULT 0,
                    exclude_from_budget INTEGER NOT NULL DEFAULT 0,
                    budget_exclusion_reason TEXT NOT NULL DEFAULT '',
                    budget_rule_version TEXT NOT NULL DEFAULT 'legacy' CHECK(budget_rule_version IN ('legacy','derived')),
                    subtype TEXT NOT NULL DEFAULT '',
                    source_refs TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    manual_edited INTEGER NOT NULL DEFAULT 0,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    reversal_of_id INTEGER DEFAULT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

-- Table: upcoming
CREATE TABLE IF NOT EXISTS "upcoming" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                due_date TEXT NOT NULL DEFAULT '',
                due_time TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                category TEXT NOT NULL DEFAULT 'Other / Miscellaneous',
                account TEXT NOT NULL DEFAULT '',
                for_with_whom TEXT NOT NULL DEFAULT 'Personal / Self',
                status TEXT NOT NULL DEFAULT 'Upcoming' CHECK(status IN ('Upcoming','Confirmed','Tentative','Paid','Skipped','Cancelled')),
                transaction_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                debt_id INTEGER DEFAULT NULL REFERENCES debts(id) ON DELETE SET NULL,
                reserve_now INTEGER NOT NULL DEFAULT 0,
                linked_goal_id INTEGER DEFAULT NULL REFERENCES allocation_goals(id) ON DELETE SET NULL, recurrence_days INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id)
            );

-- Table: warranties
CREATE TABLE IF NOT EXISTS warranties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                merchant TEXT NOT NULL DEFAULT '',
                purchase_date TEXT NOT NULL DEFAULT '',
                purchase_price REAL NOT NULL DEFAULT 0,
                warranty_end TEXT NOT NULL DEFAULT '',
                serial_number TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            );

-- Table: wishlist
CREATE TABLE IF NOT EXISTS wishlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                estimated_price REAL NOT NULL,
                priority TEXT NOT NULL DEFAULT 'Medium',
                target_date TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'General',
                notes TEXT NOT NULL DEFAULT ''
            );

CREATE INDEX IF NOT EXISTS idx_allocation_goals_status ON allocation_goals(status, kind);
CREATE INDEX IF NOT EXISTS idx_debt_events_debt ON debt_events(debt_id);
CREATE INDEX IF NOT EXISTS idx_debt_events_operation_key ON debt_events(operation_key);
CREATE INDEX IF NOT EXISTS idx_protected_allocations_covers_upcoming ON protected_allocations(covers_upcoming_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_money_context ON transactions(money_context);
CREATE INDEX IF NOT EXISTS idx_upcoming_debt ON upcoming(debt_id);

INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (1, '0001_initial_schema');