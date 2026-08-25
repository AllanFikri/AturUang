#!/usr/bin/env python3
"""
export_sqlite_to_d1.py
Deterministic read-only exporter from SQLite local to Cloudflare D1 shadow SQL package.
Preserves parent-child ordering, handles NULLs safely, and enforces idempotency.
"""

import sqlite3
import hashlib
import json
from pathlib import Path
import sys

def quote_sql(val):
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, (bytes, bytearray)):
        return f"X'{val.hex()}'"
    s = str(val).replace("'", "''")
    return f"'{s}'"

def export_d1_sql(db_path: Path, output_sql_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"Database {db_path} tidak ditemukan.")

    db_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    tables_order = [
        "settings",
        "accounts",
        "allocation_goals",
        "protected_allocations",
        "monthly_plans",
        "budget_categories",
        "budget_reallocations",
        "debts",
        "transactions",
        "balance_snapshots",
        "debt_events",
        "upcoming",
        "transaction_audit_log",
        "reconciliations",
        "idempotency_requests",
        "tags",
        "transaction_tags",
        "financial_notes",
        "goals",
        "warranties",
        "wishlist",
        "subscriptions",
        "statement_archives",
        "attachments",
        "ai_chat_history",
        "import_batches",
        "raw_import_events",
        "import_candidates",
    ]

    lines = [
        "-- ==========================================================================",
        "-- ATURUANG D1 SHADOW SEED PACKAGE (EXPORTED FROM SQLITE CANONICAL)",
        f"-- Source Hash SHA-256: {db_hash}",
        "-- Mode: Shadow Read-Only",
        "-- ==========================================================================",
        "",
    ]

    stats = {}
    total_rows = 0

    for table in tables_order:
        check = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not check:
            continue

        rows = cur.execute(f'SELECT * FROM "{table}" ORDER BY ROWID ASC').fetchall()
        row_cnt = len(rows)
        stats[table] = row_cnt
        total_rows += row_cnt

        if row_cnt == 0:
            continue

        cols = [col[0] for col in cur.description]
        cols_str = ", ".join([f'"{c}"' for c in cols])

        lines.append(f"-- Table: {table} ({row_cnt} rows)")
        for r in rows:
            vals_str = ", ".join([quote_sql(r[c]) for c in cols])
            lines.append(f'INSERT OR IGNORE INTO "{table}" ({cols_str}) VALUES ({vals_str});')
        lines.append("")

    sql_content = "\n".join(lines)
    output_sql_path.write_text(sql_content, encoding="utf-8")
    sql_hash = hashlib.sha256(sql_content.encode("utf-8")).hexdigest()

    con.close()

    return {
        "source_db_hash": db_hash,
        "exported_sql_hash": sql_hash,
        "total_rows": total_rows,
        "table_stats": stats,
        "output_file": str(output_sql_path)
    }

if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parents[3]
    db_file = base_dir / "money_tracks.db"
    out_file = base_dir / "cloud" / "worker" / "scripts" / "seed_shadow_d1.sql"
    res = export_d1_sql(db_file, out_file)
    print(json.dumps(res, indent=2))
