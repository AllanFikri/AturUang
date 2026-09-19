"""
Universal Ingestion Stage 7A — Read-Only Query Services.

Provides secure, deterministic, read-only queries for account balances, monthly summaries,
and pending review items directly from canonical SQLite authority.
Strictly enforced at the SQLite and OS connection level via file URI mode=ro.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any
import urllib.request


def get_read_only_connection(db_path: Path | str) -> sqlite3.Connection:
    """Returns an OS and SQLite enforced read-only connection via URI mode=ro."""
    p = Path(db_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Database does not exist: {p}")
    url_path = urllib.request.pathname2url(str(p))
    uri = f"file:{url_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def get_account_balances(db_path: Path | str) -> dict[str, Any]:
    """
    Deterministically computes account balances in read-only mode.
    Queries 'accounts' table if present, or reconciles directly from 'transactions'.
    """
    con = get_read_only_connection(db_path)
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'")
        has_accounts = cur.fetchone() is not None

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
        has_transactions = cur.fetchone() is not None

        accounts_list: list[dict[str, Any]] = []
        total_owned = Decimal("0.00")

        if has_accounts:
            rows = con.execute(
                "SELECT name, kind, current_balance, balance_date, protected, protected_amount, active "
                "FROM accounts ORDER BY kind, name"
            ).fetchall()
            for r in rows:
                bal_val = r["current_balance"]
                bal_dec = Decimal(f"{bal_val:.2f}").quantize(Decimal("0.01")) if bal_val is not None else None
                acc_info = {
                    "name": r["name"],
                    "kind": r["kind"],
                    "current_balance": bal_val,
                    "balance": bal_dec,
                    "active": bool(r["active"]),
                    "balance_date": r["balance_date"],
                    "protected": bool(r["protected"]),
                    "protected_amount": r["protected_amount"],
                }
                accounts_list.append(acc_info)
                if r["active"] and r["kind"] == "Owned" and bal_dec is not None:
                    total_owned += bal_dec
        elif has_transactions:
            # Reconstruct accounts from transactions ledger
            tx_accounts: set[str] = set()
            for row in con.execute("SELECT DISTINCT account_from FROM transactions WHERE account_from != '' AND is_deleted=0"):
                tx_accounts.add(row[0])
            for row in con.execute("SELECT DISTINCT account_to FROM transactions WHERE account_to != '' AND is_deleted=0"):
                tx_accounts.add(row[0])

            for acc_name in sorted(tx_accounts):
                # Inflows to this account
                inflow_row = con.execute(
                    "SELECT COALESCE(SUM(amount), 0.0) FROM transactions "
                    "WHERE is_deleted=0 AND status != 'Provisional Neutral' AND "
                    "(account_to = ? OR (transaction_type='Income' AND account_from=?))",
                    (acc_name, acc_name),
                ).fetchone()
                inflows = Decimal(f"{inflow_row[0]:.2f}").quantize(Decimal("0.01"))

                # Outflows from this account
                outflow_row = con.execute(
                    "SELECT COALESCE(SUM(amount), 0.0) FROM transactions "
                    "WHERE is_deleted=0 AND status != 'Provisional Neutral' AND "
                    "account_from = ? AND transaction_type IN ('Expense', 'Transfer')",
                    (acc_name,),
                ).fetchone()
                outflows = Decimal(f"{outflow_row[0]:.2f}").quantize(Decimal("0.01"))

                net_bal = (inflows - outflows).quantize(Decimal("0.01"))
                accounts_list.append({
                    "name": acc_name,
                    "kind": "Owned",
                    "current_balance": float(net_bal),
                    "balance": net_bal,
                    "active": True,
                    "balance_date": None,
                    "protected": False,
                    "protected_amount": 0.0,
                })
                total_owned += net_bal

        return {
            "accounts": accounts_list,
            "total_balance": total_owned,
            "count": len(accounts_list),
        }
    finally:
        con.close()


def get_monthly_summary(year: int, month: int, db_path: Path | str) -> dict[str, Any]:
    """
    Computes deterministic monthly cash flow summary and category breakdown.
    Read-only enforcement.
    """
    con = get_read_only_connection(db_path)
    try:
        month_prefix = f"{year:04d}-{month:02d}-%"
        rows = con.execute(
            "SELECT transaction_type, category, amount, exclude_from_budget "
            "FROM transactions WHERE is_deleted=0 AND status != 'Provisional Neutral' "
            "AND date LIKE ?",
            (month_prefix,),
        ).fetchall()

        total_income = Decimal("0.00")
        total_expense = Decimal("0.00")
        budget_expense = Decimal("0.00")
        excluded_expense = Decimal("0.00")
        expense_by_category: dict[str, Decimal] = {}

        for r in rows:
            ttype = r["transaction_type"]
            amt = Decimal(f"{r['amount']:.2f}").quantize(Decimal("0.01"))
            cat = r["category"] or "Other / Miscellaneous"
            excluded = bool(r["exclude_from_budget"])

            if ttype == "Income":
                total_income += amt
            elif ttype == "Expense":
                total_expense += amt
                if excluded:
                    excluded_expense += amt
                else:
                    budget_expense += amt
                expense_by_category[cat] = expense_by_category.get(cat, Decimal("0.00")) + amt

        net_cash_flow = (total_income - total_expense).quantize(Decimal("0.01"))

        return {
            "year": year,
            "month": month,
            "period": f"{year:04d}-{month:02d}",
            "total_income": total_income,
            "total_expense": total_expense,
            "net_cash_flow": net_cash_flow,
            "budget_expense": budget_expense,
            "excluded_expense": excluded_expense,
            "expense_by_category": expense_by_category,
            "transaction_count": len(rows),
        }
    finally:
        con.close()


def get_pending_reviews(db_path: Path | str) -> list[dict[str, Any]]:
    """
    Returns pending review queue items from import candidates, safe apply idempotency,
    and provisional transactions.
    """
    con = get_read_only_connection(db_path)
    try:
        pending_items: list[dict[str, Any]] = []
        cur = con.cursor()

        # 1. import_candidates
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='import_candidates'")
        if cur.fetchone():
            cands = con.execute(
                "SELECT id, fingerprint, date, time, transaction_type, amount, account_from, account_to, "
                "description, category, confidence, review_status, review_notes "
                "FROM import_candidates WHERE review_status = 'Pending' ORDER BY date DESC, id DESC"
            ).fetchall()
            for c in cands:
                pending_items.append({
                    "id": str(c["id"]),
                    "source": "import_candidate",
                    "fingerprint": c["fingerprint"],
                    "date": c["date"],
                    "time": c["time"],
                    "transaction_type": c["transaction_type"],
                    "amount": Decimal(f"{c['amount']:.2f}").quantize(Decimal("0.01")),
                    "account_from": c["account_from"],
                    "account_to": c["account_to"],
                    "description": c["description"],
                    "category": c["category"],
                    "status": c["review_status"],
                    "reason": c["review_notes"],
                })

        # 2. safe_apply_idempotency
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='safe_apply_idempotency'")
        if cur.fetchone():
            cands = con.execute(
                "SELECT candidate_id, idempotency_key, preview_hash, state, applied_timestamp "
                "FROM safe_apply_idempotency WHERE state = 'REVIEW_REQUIRED'"
            ).fetchall()
            for c in cands:
                pending_items.append({
                    "id": c["candidate_id"],
                    "source": "safe_apply_candidate",
                    "idempotency_key": c["idempotency_key"],
                    "preview_hash": c["preview_hash"],
                    "status": c["state"],
                    "reason": "REVIEW_REQUIRED",
                })

        # 3. transactions with Provisional Neutral status
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
        if cur.fetchone():
            txs = con.execute(
                "SELECT id, date, time, transaction_type, amount, account_from, account_to, description, category "
                "FROM transactions WHERE status = 'Provisional Neutral' AND is_deleted = 0"
            ).fetchall()
            for t in txs:
                pending_items.append({
                    "id": str(t["id"]),
                    "source": "provisional_transaction",
                    "date": t["date"],
                    "time": t["time"],
                    "transaction_type": t["transaction_type"],
                    "amount": Decimal(f"{t['amount']:.2f}").quantize(Decimal("0.01")),
                    "account_from": t["account_from"],
                    "account_to": t["account_to"],
                    "description": t["description"],
                    "category": t["category"],
                    "status": "Provisional Neutral",
                    "reason": "Provisional Neutral status awaiting confirmation",
                })

        return pending_items
    finally:
        con.close()
