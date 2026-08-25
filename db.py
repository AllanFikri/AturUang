"""
Money Tracks V12 — Database Layer

Handles SQLite connections, schema initialization, migrations, seeding,
and database backups.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "money_tracks.db"
SEED_CSV = BASE_DIR / "canonical_ledger_seed.csv"
BACKUP_DIR = BASE_DIR / "backups"
UPDATE_SOURCE_FILE = BASE_DIR / "update_source.json"
HTML_FILE = BASE_DIR / "index.html"
VERSION = "12.0.0"

CATEGORIES = [
    "Main Meals", "Snacks", "Cafe & Drinks", "Groceries & Daily Needs",
    "Fuel", "Parking/Toll", "Transport / Other Transport", "Vehicle Service",
    "Fashion", "Electronics", "Personal Care", "Health", "Education",
    "Campus & Organization", "Phone & Internet", "Subscriptions",
    "Gifts & Giving", "Travel", "Other / Miscellaneous", "Research — Historical Only",
]

FOR_WITH_WHOM = [
    "Personal / Self", "Pacar / Partner", "Keluarga / Family", "Teman / Friends",
    "Shared / Group", "Sedekah / Charity", "Other",
]

MONEY_CONTEXTS = ["Personal", "Pass-through", "Historical Research", "Third-party"]
STATUSES = ["Confirmed", "Auto-classified", "Provisional Neutral"]

FREQUENT_SAFE_DAILY = {"Main Meals", "Snacks", "Cafe & Drinks"}
OCCASIONAL_DEFAULT_OFF = {"Fashion", "Electronics", "Travel"}
ROLLOVER_DEFAULT_ON = {"Vehicle Service"}

OWNED_PREFIXES = (
    "BCA Main", "BCA Poket:", "Cash", "ShopeePay", "GoPay", "Jago:", "Jago Main",
)

SEPTEMBER_SEED_BUDGETS = {
    "Main Meals": 920000,
    "Snacks": 0,
    "Cafe & Drinks": 50000,
    "Groceries & Daily Needs": 340000,
    "Fuel": 80000,
    "Parking/Toll": 0,
    "Transport / Other Transport": 0,
    "Vehicle Service": 50000,
    "Fashion": 0,
    "Electronics": 0,
    "Personal Care": 80000,
    "Health": 50000,
    "Education": 0,
    "Campus & Organization": 200000,
    "Phone & Internet": 160000,
    "Subscriptions": 0,
    "Gifts & Giving": 80000,
    "Travel": 0,
    "Other / Miscellaneous": 210000,
}


WIB = dt.timezone(dt.timedelta(hours=7))


def now_wib() -> dt.datetime:
    """Returns current timezone-aware datetime in WIB (Asia/Jakarta, UTC+7)."""
    return dt.datetime.now(WIB)


def get_default_planning_month() -> str:
    """Returns dynamic current or next month for planning based on current WIB time."""
    now = now_wib()
    # Default planning month is next month
    y, m = now.year, now.month
    next_y, next_m = (y, m + 1) if m < 12 else (y + 1, 1)
    return f"{next_y:04d}-{next_m:02d}"


def db_connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Creates a connection to the SQLite database with Row factory enabled."""
    target = Path(db_path) if db_path else DB_FILE
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def is_owned_name(name: str | None) -> bool:
    """Checks if an account name belongs to owned liquid assets."""
    if not name:
        return False
    n = name.strip()
    if n == "Own Account":
        return False  # legacy ambiguous label; intentionally excluded from owned balances
    if "Stockbit" in n or "RDN" in n or "Sekuritas" in n:
        return False  # Investment account, not liquid spending money
    return any(n == p or n.startswith(p) for p in OWNED_PREFIXES)


def inferred_account_kind(name: str) -> str:
    """Infers account classification category from name string."""
    n = name.strip() if name else ""
    if "Stockbit" in n or "RDN" in n or "Sekuritas" in n or "Investasi" in n:
        return "Investment"
    if n.startswith("Receivable:"):
        return "Receivable"
    if n.startswith("Suspense:") or n in {"Own Account", "Unknown Payment Account", "SeaBank"}:
        return "Suspense"
    if is_owned_name(n):
        return "Owned"
    return "External"


def backup_db() -> Path | None:
    """Creates a timestamped backup of money_tracks.db before mutation, keeping max 20 backups."""
    if not DB_FILE.exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak_path = BACKUP_DIR / f"money_tracks_{stamp}.db"
    try:
        shutil.copy2(DB_FILE, bak_path)
        backups = sorted(
            BACKUP_DIR.glob("money_tracks_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in backups[20:]:
            p.unlink(missing_ok=True)
        return bak_path
    except Exception:
        return None


def init_db(db_path: str | Path | None = None) -> None:
    """Initializes schema, applies migrations, and seeds baseline data if needed."""
    BACKUP_DIR.mkdir(exist_ok=True)
    con = db_connect(db_path)
    try:
        schema_sql = """
            CREATE TABLE IF NOT EXISTS accounts (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('Owned','Receivable','Suspense','External','Investment')),
                current_balance REAL,
                balance_date TEXT,
                protected INTEGER NOT NULL DEFAULT 0,
                protected_amount REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS transactions (
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS monthly_plans (
                month TEXT PRIMARY KEY,
                guaranteed_income REAL NOT NULL DEFAULT 0,
                expected_additional_income REAL NOT NULL DEFAULT 0,
                savings_rate REAL NOT NULL DEFAULT 0.20,
                notes TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS budget_categories (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                original_budget REAL NOT NULL DEFAULT 0,
                current_budget REAL NOT NULL DEFAULT 0,
                suggested_budget REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                rollover INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (month, category)
            );

            CREATE TABLE IF NOT EXISTS budget_reallocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                from_category TEXT NOT NULL,
                to_category TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS protected_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                account TEXT NOT NULL DEFAULT '',
                target_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Spent','Released')),
                notes TEXT NOT NULL DEFAULT '',
                covers_upcoming_id INTEGER DEFAULT NULL REFERENCES upcoming(id) ON DELETE SET NULL
            );

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

            CREATE TABLE IF NOT EXISTS upcoming (
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
                linked_goal_id INTEGER DEFAULT NULL REFERENCES allocation_goals(id) ON DELETE SET NULL,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id)
            );

            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                balance REAL NOT NULL,
                snapshot_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                snapshot_kind TEXT NOT NULL DEFAULT 'mutation' CHECK(snapshot_kind IN ('initial_anchor', 'manual_anchor', 'mutation', 'reconciliation', 'legacy')),
                FOREIGN KEY(account_name) REFERENCES accounts(name)
            );

            CREATE TABLE IF NOT EXISTS transaction_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                old_data TEXT NOT NULL DEFAULT '',
                new_data TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

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

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT 'default',
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                intent TEXT,
                action_proposal TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT NOT NULL DEFAULT '#10b981'
            );

            CREATE TABLE IF NOT EXISTS transaction_tags (
                transaction_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY(transaction_id, tag_id),
                FOREIGN KEY(transaction_id) REFERENCES transactions(id),
                FOREIGN KEY(tag_id) REFERENCES tags(id)
            );

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

            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('Custody','Receivable','Payable')),
                status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Settled','WrittenOff')),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

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

            CREATE TABLE IF NOT EXISTS financial_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                target_type TEXT NOT NULL DEFAULT 'general',
                target_id INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS wishlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                estimated_price REAL NOT NULL,
                priority TEXT NOT NULL DEFAULT 'Medium',
                target_date TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'General',
                notes TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS statement_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL UNIQUE,
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                summary_json TEXT NOT NULL DEFAULT '{}',
                html_ref TEXT NOT NULL DEFAULT '',
                hash TEXT NOT NULL DEFAULT '',
                is_revised INTEGER NOT NULL DEFAULT 0
            );

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

            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'IN_PROGRESS' CHECK(status IN ('IN_PROGRESS','COMPLETED','FAILED')),
                total_count INTEGER NOT NULL DEFAULT 0,
                parsed_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS raw_import_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL DEFAULT '',
                payload_hash TEXT NOT NULL,
                minimal_raw_payload TEXT NOT NULL,
                parser_version TEXT NOT NULL DEFAULT '1.0',
                parse_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(parse_status IN ('PENDING','PARSED','DUPLICATE','ERROR')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(provider, external_event_id)
            );

            CREATE TABLE IF NOT EXISTS import_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_event_id INTEGER NOT NULL REFERENCES raw_import_events(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL UNIQUE,
                date TEXT NOT NULL,
                time TEXT NOT NULL DEFAULT '',
                transaction_type TEXT NOT NULL CHECK(transaction_type IN ('Income','Expense','Transfer','Adjustment')),
                amount REAL NOT NULL CHECK(amount >= 0),
                account_from TEXT NOT NULL DEFAULT '',
                account_to TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Other / Miscellaneous',
                money_context TEXT NOT NULL DEFAULT 'Personal' CHECK(money_context IN ('Personal','Pass-through','Historical Research','Third-party')),
                confidence TEXT NOT NULL DEFAULT 'high' CHECK(confidence IN ('high','medium','low')),
                duplicate_status TEXT NOT NULL DEFAULT 'None' CHECK(duplicate_status IN ('None','Exact','Probable')),
                matched_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
                review_status TEXT NOT NULL DEFAULT 'Pending' CHECK(review_status IN ('Pending','Approved','Rejected','Duplicate','Error')),
                review_notes TEXT NOT NULL DEFAULT '',
                approved_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_import_candidates_status ON import_candidates(review_status);
            CREATE INDEX IF NOT EXISTS idx_import_candidates_fingerprint ON import_candidates(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_raw_import_events_provider_ext ON raw_import_events(provider, external_event_id);
            CREATE INDEX IF NOT EXISTS idx_raw_import_events_batch ON raw_import_events(batch_id);
        """
        try:
            con.executescript(schema_sql)
        except (sqlite3.DatabaseError, sqlite3.OperationalError):
            for stmt in schema_sql.split(';'):
                stmt = stmt.strip()
                if stmt:
                    try:
                        con.execute(stmt)
                    except (sqlite3.DatabaseError, sqlite3.OperationalError):
                        pass

        # Migrations for existing databases
        account_cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)").fetchall()}
        if "protected_amount" not in account_cols:
            con.execute("ALTER TABLE accounts ADD COLUMN protected_amount REAL NOT NULL DEFAULT 0")
        if "last_reconciled_at" not in account_cols:
            con.execute("ALTER TABLE accounts ADD COLUMN last_reconciled_at TEXT NOT NULL DEFAULT ''")

        tx_cols = {r[1] for r in con.execute("PRAGMA table_info(transactions)").fetchall()}
        if "is_deleted" not in tx_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
        if "reversal_of_id" not in tx_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN reversal_of_id INTEGER DEFAULT NULL")
        if "exclude_from_budget" not in tx_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN exclude_from_budget INTEGER NOT NULL DEFAULT 0")
        if "budget_exclusion_reason" not in tx_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN budget_exclusion_reason TEXT NOT NULL DEFAULT ''")
        if "budget_rule_version" not in tx_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN budget_rule_version TEXT NOT NULL DEFAULT 'legacy'")

        # Ensure transactions table CHECK constraint allows Third-party and Adjustment
        tx_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'").fetchone()
        if tx_sql and tx_sql[0] and ("'Third-party'" not in tx_sql[0] or "'Adjustment'" not in tx_sql[0]):
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("""
                CREATE TABLE IF NOT EXISTS transactions_new (
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
                )
            """)
            con.execute("""
                INSERT INTO transactions_new (
                    id, canonical_id, date, time, transaction_type, amount,
                    account_from, account_to, description, category, for_with_whom,
                    money_context, settlement_kind, status, confidence, budget_effect,
                    exclude_from_budget, budget_exclusion_reason, budget_rule_version,
                    subtype, source_refs, notes, manual_edited, is_deleted, reversal_of_id,
                    created_at, updated_at
                )
                SELECT
                    id, canonical_id, date, COALESCE(time, ''), transaction_type, amount,
                    COALESCE(account_from, ''), COALESCE(account_to, ''), COALESCE(description, ''), COALESCE(category, ''), COALESCE(for_with_whom, 'Personal / Self'),
                    COALESCE(money_context, 'Personal'), COALESCE(settlement_kind, ''), COALESCE(status, 'Confirmed'), COALESCE(confidence, 'high'), COALESCE(budget_effect, 0),
                    COALESCE(exclude_from_budget, 0), COALESCE(budget_exclusion_reason, ''), COALESCE(budget_rule_version, 'legacy'),
                    COALESCE(subtype, ''), COALESCE(source_refs, ''), COALESCE(notes, ''), COALESCE(manual_edited, 0), COALESCE(is_deleted, 0), reversal_of_id,
                    created_at, updated_at
                FROM transactions
            """)
            con.execute("DROP TABLE transactions")
            con.execute("ALTER TABLE transactions_new RENAME TO transactions")
            con.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_transactions_money_context ON transactions(money_context)")
            con.execute("PRAGMA foreign_keys=ON")

        upcoming_cols = {r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()}
        if "due_time" not in upcoming_cols:
            con.execute("ALTER TABLE upcoming ADD COLUMN due_time TEXT NOT NULL DEFAULT ''")
        if "recurrence_days" not in upcoming_cols:
            con.execute("ALTER TABLE upcoming ADD COLUMN recurrence_days INTEGER NOT NULL DEFAULT 0")

        # Fix Waroeng Steak 121.001 ID back to Personal / Self if modified
        con.execute(
            "UPDATE transactions SET for_with_whom='Personal / Self', manual_edited=0 WHERE description LIKE '%Waroeng Steak%' AND abs(amount - 121001) < 0.005"
        )

        count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        if count == 0 and SEED_CSV.exists():
            import_seed(con)

        seed_accounts(con)
        plan_month = get_default_planning_month()
        seed_plan(con, "2026-09")
        seed_plan(con, plan_month)
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('default_plan_month',?)", (plan_month,))
        con.execute("UPDATE settings SET value=? WHERE key='default_plan_month'", (plan_month,))
        con.execute("INSERT INTO settings(key,value) VALUES ('schema_version','12') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('current_pass_through_outstanding','0')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('commitment_backlog_note','')")
        con.execute("UPDATE accounts SET active=0 WHERE name='BCA Poket: Uang Riset' AND current_balance IS NULL")

        # Migrate balance_snapshots table to include snapshot_kind if missing
        snap_cols = [r[1] for r in con.execute("PRAGMA table_info(balance_snapshots)").fetchall()]
        if "snapshot_kind" not in snap_cols:
            con.execute("ALTER TABLE balance_snapshots ADD COLUMN snapshot_kind TEXT NOT NULL DEFAULT 'legacy'")

        # Migrate protected_allocations to include covers_upcoming_id if missing
        alloc_cols = [r[1] for r in con.execute("PRAGMA table_info(protected_allocations)").fetchall()]
        if "covers_upcoming_id" not in alloc_cols:
            con.execute("ALTER TABLE protected_allocations ADD COLUMN covers_upcoming_id INTEGER DEFAULT NULL REFERENCES upcoming(id) ON DELETE SET NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_protected_allocations_covers_upcoming ON protected_allocations(covers_upcoming_id)")

        # Migrate upcoming to include debt_id if missing
        up_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
        if "debt_id" not in up_cols:
            con.execute("ALTER TABLE upcoming ADD COLUMN debt_id INTEGER DEFAULT NULL REFERENCES debts(id) ON DELETE SET NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_upcoming_debt ON upcoming(debt_id)")

        # Migrate debts table if old schema (reuse only if empty)
        debts_cols = [r[1] for r in con.execute("PRAGMA table_info(debts)").fetchall()]
        if "kind" not in debts_cols:
            debt_count = con.execute("SELECT COUNT(*) FROM debts").fetchone()[0]
            if debt_count > 0:
                raise RuntimeError("Tabel debts memiliki data lama tak dikenal. Migrasi K2 dibatalkan demi keamanan data.")
            con.execute("DROP TABLE debts")
            con.execute(
                """CREATE TABLE debts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('Custody','Receivable','Payable')),
                    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Settled','WrittenOff')),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

        con.execute(
            """CREATE TABLE IF NOT EXISTS debt_events (
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
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_debt_events_debt ON debt_events(debt_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_debt_events_operation_key ON debt_events(operation_key)")

        # Ensure Stockbit RDN is classified as Investment with constraint migration support
        try:
            con.execute("UPDATE accounts SET kind='Investment' WHERE name LIKE '%Stockbit%' OR name LIKE '%RDN%'")
        except sqlite3.IntegrityError:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute(
                """CREATE TABLE IF NOT EXISTS accounts_new (
                    name TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('Owned','Receivable','Suspense','External','Investment')),
                    current_balance REAL,
                    balance_date TEXT,
                    protected INTEGER NOT NULL DEFAULT 0,
                    protected_amount REAL NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT ''
                )"""
            )
            con.execute(
                """INSERT OR REPLACE INTO accounts_new (name, kind, current_balance, balance_date, protected, protected_amount, active, note)
                   SELECT name, CASE WHEN name LIKE '%Stockbit%' OR name LIKE '%RDN%' THEN 'Investment' ELSE kind END,
                          current_balance, balance_date, protected, protected_amount, active, note
                   FROM accounts"""
            )
            con.execute("DROP TABLE accounts")
            con.execute("ALTER TABLE accounts_new RENAME TO accounts")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("UPDATE accounts SET kind='Investment' WHERE name LIKE '%Stockbit%' OR name LIKE '%RDN%'")
        con.commit()
    finally:
        con.close()


def import_seed(con: sqlite3.Connection) -> None:
    """Imports seed data from canonical_ledger_seed.csv."""
    with SEED_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            status = r.get("status") or "Confirmed"
            if status not in STATUSES:
                status = "Confirmed"
            context = r.get("money_context") or "Personal"
            if context not in MONEY_CONTEXTS:
                context = "Personal"
            typ = r.get("transaction_type") or "Transfer"
            amt = abs(float(r.get("amount_idr") or 0))
            effect = float(r.get("budget_effect_idr") or 0)
            con.execute(
                """INSERT OR IGNORE INTO transactions
                (canonical_id,date,time,transaction_type,amount,account_from,account_to,description,category,
                 for_with_whom,money_context,settlement_kind,status,confidence,budget_effect,subtype,source_refs,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.get("canonical_id") or None, r.get("date"), r.get("time") or "", typ, amt,
                    r.get("account_from") or "", r.get("account_to") or "", r.get("description") or "",
                    r.get("category") or "", r.get("for_with_whom") or "Personal / Self", context,
                    r.get("settlement_kind") or "", status, r.get("confidence") or "medium", effect,
                    r.get("subtype") or "", r.get("source_refs") or "", r.get("notes") or "",
                ),
            )


def seed_accounts(con: sqlite3.Connection) -> None:
    """Populates accounts table from transactions and key default accounts."""
    names = set()
    for row in con.execute("SELECT account_from,account_to FROM transactions"):
        for n in (row["account_from"], row["account_to"]):
            if n:
                names.add(n)
    for n in names:
        kind = inferred_account_kind(n)
        if kind != "External":
            con.execute(
                "INSERT OR IGNORE INTO accounts(name,kind,protected) VALUES (?,?,?)",
                (n, kind, 0),
            )
    for n in ["BCA Main", "Cash", "ShopeePay", "GoPay", "Jago: Kantong Utama", "Jago: Tabungan"]:
        con.execute("INSERT OR IGNORE INTO accounts(name,kind) VALUES (?, 'Owned')", (n,))
    con.execute("INSERT OR IGNORE INTO accounts(name,kind) VALUES ('Jago: Stockbit Sekuritas RDN', 'Investment')")


def seed_plan(con: sqlite3.Connection, month: str) -> None:
    """Ensures monthly_plans and budget_categories records exist for the given month."""
    from services import compute_budget_suggestions

    plan_exists = con.execute("SELECT 1 FROM monthly_plans WHERE month=?", (month,)).fetchone()
    if not plan_exists:
        if month != "2026-09":
            con.execute(
                "INSERT OR IGNORE INTO monthly_plans(month,guaranteed_income,expected_additional_income,savings_rate) VALUES (?,?,?,?)",
                (month, 0, 0, 0.20),
            )
        else:
            con.execute(
                "INSERT OR IGNORE INTO monthly_plans(month,guaranteed_income,expected_additional_income,savings_rate,notes) VALUES (?,?,?,?,?)",
                (month, 1000000, 0, 0.20, "Initial September plan from canonical Jan–Aug history. Education support is protected/dedicated, not free spending."),
            )

    existing = con.execute("SELECT COUNT(*) FROM budget_categories WHERE month=?", (month,)).fetchone()[0]
    if existing == 0:
        if month != "2026-09":
            suggestions = compute_budget_suggestions(con, month)
            for cat in CATEGORIES:
                if cat == "Research — Historical Only":
                    continue
                sug = float(suggestions.get(cat, 0))
                active = 0 if cat in OCCASIONAL_DEFAULT_OFF or sug <= 0 else 1
                roll = 1 if cat in ROLLOVER_DEFAULT_ON else 0
                con.execute(
                    "INSERT INTO budget_categories(month,category,original_budget,current_budget,suggested_budget,active,rollover) VALUES (?,?,?,?,?,?,?)",
                    (month, cat, 0, 0, sug, active, roll),
                )
        else:
            for cat in CATEGORIES:
                if cat == "Research — Historical Only":
                    continue
                amount = float(SEPTEMBER_SEED_BUDGETS.get(cat, 0))
                active = 0 if cat in OCCASIONAL_DEFAULT_OFF or amount <= 0 else 1
                roll = 1 if cat in ROLLOVER_DEFAULT_ON else 0
                con.execute(
                    "INSERT INTO budget_categories(month,category,original_budget,current_budget,suggested_budget,active,rollover) VALUES (?,?,?,?,?,?,?)",
                    (month, cat, amount, amount, amount, active, roll),
                )


def load_update_source() -> dict:
    """Loads update manifest source configuration."""
    default = {"manifest_url": "", "auto_check": True}
    try:
        if UPDATE_SOURCE_FILE.exists():
            data = json.loads(UPDATE_SOURCE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                default.update(data)
    except Exception:
        pass
    return default


def save_update_source(data: dict) -> dict:
    """Saves update manifest source configuration."""
    current = load_update_source()
    current["manifest_url"] = str(data.get("manifest_url", current.get("manifest_url", ""))).strip()
    current["auto_check"] = bool(data.get("auto_check", current.get("auto_check", True)))
    UPDATE_SOURCE_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def rollback_balance_snapshots_migration(db_path: str | Path | None = None) -> None:
    """Rollback helper: removes snapshot_kind column from balance_snapshots preserving all original records."""
    target = Path(db_path) if db_path else DB_FILE
    with db_connect(target) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                balance REAL NOT NULL,
                snapshot_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_name) REFERENCES accounts(name)
            )
        """)
        con.execute("""
            INSERT INTO balance_snapshots_old (id, account_name, balance, snapshot_date, created_at)
            SELECT id, account_name, balance, snapshot_date, created_at FROM balance_snapshots
        """)
        con.execute("DROP TABLE balance_snapshots")
        con.execute("ALTER TABLE balance_snapshots_old RENAME TO balance_snapshots")
        con.execute("PRAGMA foreign_keys=ON")


def rollback_protected_allocations_covers_upcoming(db_path: str | Path | None = None) -> None:
    """Rollback helper: removes covers_upcoming_id column from protected_allocations preserving all records."""
    target = Path(db_path) if db_path else DB_FILE
    with db_connect(target) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("""
            CREATE TABLE IF NOT EXISTS protected_allocations_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                account TEXT NOT NULL DEFAULT '',
                target_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Spent','Released')),
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("""
            INSERT INTO protected_allocations_old (id, title, amount, account, target_date, status, notes)
            SELECT id, title, amount, account, target_date, status, notes FROM protected_allocations
        """)
        con.execute("DROP TABLE protected_allocations")
        con.execute("ALTER TABLE protected_allocations_old RENAME TO protected_allocations")
        con.execute("PRAGMA foreign_keys=ON")


def rollback_k2_event_ledger_migration(db_path: str | Path | None = None) -> None:
    """Rollback helper: drops debt_events, removes debt_id from upcoming, and restores legacy debts schema."""
    target = Path(db_path) if db_path else DB_FILE
    with db_connect(target) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DROP TABLE IF EXISTS debt_events")

        # Rollback upcoming table (remove debt_id)
        con.execute("""
            CREATE TABLE IF NOT EXISTS upcoming_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                due_date TEXT NOT NULL DEFAULT '',
                due_time TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                category TEXT NOT NULL DEFAULT 'Other / Miscellaneous',
                account TEXT NOT NULL DEFAULT '',
                for_with_whom TEXT NOT NULL DEFAULT 'Personal / Self',
                status TEXT NOT NULL DEFAULT 'Upcoming' CHECK(status IN ('Upcoming','Paid','Skipped')),
                transaction_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(transaction_id) REFERENCES transactions(id)
            )
        """)
        con.execute("""
            INSERT INTO upcoming_old (id, due_date, due_time, title, amount, category, account, for_with_whom, status, transaction_id, notes)
            SELECT id, due_date, due_time, title, amount, category, account, for_with_whom, status, transaction_id, notes FROM upcoming
        """)
        con.execute("DROP TABLE upcoming")
        con.execute("ALTER TABLE upcoming_old RENAME TO upcoming")

        # Rollback debts table to legacy structure
        con.execute("DROP TABLE IF EXISTS debts")
        con.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'Receivable',
                principal REAL NOT NULL,
                remaining REAL NOT NULL,
                start_date TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("PRAGMA foreign_keys=ON")


def migrate_allocation_schema(con: sqlite3.Connection) -> dict:
    """Idempotently ensures allocation_goals table, index, and upcoming columns/constraints exist."""
    # 1. Create allocation_goals table
    con.execute("""
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
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_allocation_goals_status ON allocation_goals(status, kind);")

    # 2. Check upcoming table status constraint & columns
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='upcoming'").fetchone()
    if row and row[0] and "Tentative" not in row[0]:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("""
            CREATE TABLE IF NOT EXISTS upcoming_new (
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
                linked_goal_id INTEGER DEFAULT NULL REFERENCES allocation_goals(id) ON DELETE SET NULL,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id)
            );
        """)
        old_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
        common = [c for c in ['id', 'due_date', 'due_time', 'title', 'amount', 'category', 'account', 'for_with_whom', 'status', 'transaction_id', 'notes', 'debt_id', 'reserve_now', 'linked_goal_id'] if c in old_cols]
        cols_str = ', '.join(common)
        con.execute(f"INSERT INTO upcoming_new ({cols_str}) SELECT {cols_str} FROM upcoming")
        con.execute("DROP TABLE upcoming")
        con.execute("ALTER TABLE upcoming_new RENAME TO upcoming")
        con.execute("PRAGMA foreign_keys=ON")
    else:
        u_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
        if "reserve_now" not in u_cols:
            con.execute("ALTER TABLE upcoming ADD COLUMN reserve_now INTEGER NOT NULL DEFAULT 0")
        if "linked_goal_id" not in u_cols:
            con.execute("ALTER TABLE upcoming ADD COLUMN linked_goal_id INTEGER DEFAULT NULL REFERENCES allocation_goals(id) ON DELETE SET NULL")

    return {"status": "success"}


def rollback_allocation_schema(db_path: str | Path | None = None) -> None:
    """Rollback helper: drops allocation_goals and removes reserve_now/linked_goal_id from upcoming."""
    target = Path(db_path) if db_path else DB_FILE
    with db_connect(target) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DROP TABLE IF EXISTS allocation_goals")
        con.execute("PRAGMA foreign_keys=ON")
