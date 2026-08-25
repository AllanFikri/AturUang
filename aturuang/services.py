"""
Money Tracks V12 — Business Logic & Financial Calculations

Contains calculations for safe-to-spend, budget views, dashboard KPIs,
category normalizations, validation, and update manifest checking.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import urllib.request
import statistics
from statistics import median
from collections import Counter, defaultdict
import ast
import operator
from ai_key_manager import get_gemini_api_key, get_ai_status, save_gemini_api_key, delete_gemini_api_key, test_gemini_connection

try:
    from zoneinfo import ZoneInfo
    JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
except Exception:
    import pytz
    JAKARTA_TZ = pytz.timezone("Asia/Jakarta")

def get_wib_now() -> dt.datetime:
    """Returns current datetime in Asia/Jakarta (WIB) timezone."""
    return dt.datetime.now(JAKARTA_TZ)

def get_wib_today_str() -> str:
    """Returns YYYY-MM-DD in Asia/Jakarta timezone."""
    return get_wib_now().strftime("%Y-%m-%d")

from db import (
    CATEGORIES,
    FOR_WITH_WHOM,
    FREQUENT_SAFE_DAILY,
    MONEY_CONTEXTS,
    OCCASIONAL_DEFAULT_OFF,
    STATUSES,
    VERSION,
    load_update_source,
    seed_plan,
)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    """Returns (start_date, end_date) for a YYYY-MM string."""
    y, m = map(int, month.split("-"))
    start = dt.date(y, m, 1)
    if m == 12:
        end = dt.date(y + 1, 1, 1) - dt.timedelta(days=1)
    else:
        end = dt.date(y, m + 1, 1) - dt.timedelta(days=1)
    return start, end


def valid_month(month: str) -> bool:
    """Checks if string is a valid YYYY-MM month."""
    try:
        month_bounds(month)
        return len(month) == 7
    except Exception:
        return False


def get_time_context(con: sqlite3.Connection | None = None) -> dict:
    """Returns central time machine information in WIB (Asia/Jakarta)."""
    from db import now_wib
    now = now_wib()
    today_str = now.strftime("%Y-%m-%d")
    current_month_str = now.strftime("%Y-%m")

    y, m = now.year, now.month
    next_y, next_m = (y, m + 1) if m < 12 else (y + 1, 1)
    planning_month_str = f"{next_y:04d}-{next_m:02d}"

    last_tx_date = today_str
    if con:
        r = con.execute("SELECT MAX(date) FROM transactions WHERE date<>'' AND date<=? AND is_deleted=0", (today_str,)).fetchone()
        if r and r[0]:
            last_tx_date = r[0]

    _, end = month_bounds(current_month_str)

    days_id = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months_id = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]

    day_name = days_id[now.weekday()]
    month_name = months_id[now.month]
    formatted_wib = f"{day_name}, {now.day} {month_name} {now.year} · {now.strftime('%H.%M')} WIB"

    return {
        "now_iso": now.isoformat(),
        "today": today_str,
        "current_month": current_month_str,
        "planning_month": planning_month_str,
        "last_tx_date": last_tx_date,
        "day_of_month": now.day,
        "days_in_month": end.day,
        "formatted_wib": formatted_wib,
    }



def rowdict(r: sqlite3.Row) -> dict:
    """Converts a sqlite3.Row to a standard python dict."""
    return {k: r[k] for k in r.keys()}


def compute_request_hash(payload: dict | list | None) -> str:
    """Computes stable canonical SHA-256 hash of a request payload excluding idempotency key fields."""
    if payload is None:
        return hashlib.sha256(b"null").hexdigest()

    def clean_payload(obj):
        if isinstance(obj, dict):
            return {k: clean_payload(v) for k, v in sorted(obj.items()) if k not in ("Idempotency-Key", "idempotency_key", "operation_key")}
        elif isinstance(obj, list):
            return [clean_payload(v) for v in obj]
        return obj

    cleaned = clean_payload(payload)
    canon_json = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon_json.encode("utf-8")).hexdigest()


def handle_idempotent_request(
    con: sqlite3.Connection,
    endpoint_scope: str,
    idempotency_key: str | None,
    payload: dict,
    execute_fn: callable,
) -> tuple[int, dict]:
    """Wraps an atomic mutation with the global idempotency_requests table."""
    key = str(idempotency_key or "").strip()
    if not key:
        return 200, execute_fn()

    req_hash = compute_request_hash(payload)

    # Check existing request record
    existing = con.execute(
        "SELECT * FROM idempotency_requests WHERE endpoint_scope=? AND idempotency_key=?",
        (endpoint_scope, key),
    ).fetchone()

    if existing:
        if existing["request_hash"] != req_hash:
            raise ValueError(f"HTTP 409: Idempotency-Key '{key}' sudah digunakan untuk permintaan berbeda (payload mismatch)")
        if existing["state"] == "COMPLETED":
            resp_body = json.loads(existing["response_json"]) if existing["response_json"] else {}
            return int(existing["http_status"] or 200), resp_body
        if existing["state"] == "IN_PROGRESS":
            raise ValueError(f"HTTP 409: Permintaan dengan Idempotency-Key '{key}' sedang diproses. Silakan coba kembali sesaat lagi.")

    # Reserve IN_PROGRESS
    try:
        con.execute(
            """INSERT INTO idempotency_requests (endpoint_scope, idempotency_key, request_hash, state)
               VALUES (?, ?, ?, 'IN_PROGRESS')""",
            (endpoint_scope, key, req_hash),
        )
        con.commit()
    except sqlite3.IntegrityError:
        existing = con.execute(
            "SELECT * FROM idempotency_requests WHERE endpoint_scope=? AND idempotency_key=?",
            (endpoint_scope, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != req_hash:
                raise ValueError(f"HTTP 409: Idempotency-Key '{key}' sudah digunakan untuk permintaan berbeda (payload mismatch)")
            if existing["state"] == "COMPLETED":
                resp_body = json.loads(existing["response_json"]) if existing["response_json"] else {}
                return int(existing["http_status"] or 200), resp_body
            if existing["state"] == "IN_PROGRESS":
                raise ValueError(f"HTTP 409: Permintaan dengan Idempotency-Key '{key}' sedang diproses. Silakan coba kembali sesaat lagi.")

    try:
        res = execute_fn()
        status_code = 200
        con.execute(
            """UPDATE idempotency_requests
               SET state='COMPLETED', http_status=?, response_json=?, completed_at=CURRENT_TIMESTAMP
               WHERE endpoint_scope=? AND idempotency_key=?""",
            (status_code, json.dumps(res, ensure_ascii=False), endpoint_scope, key),
        )
        con.commit()
        return status_code, res
    except Exception as err:
        con.execute(
            "DELETE FROM idempotency_requests WHERE endpoint_scope=? AND idempotency_key=?",
            (endpoint_scope, key),
        )
        con.commit()
        raise err


def available_months(con: sqlite3.Connection) -> list[str]:
    """Returns sorted list of all available YYYY-MM months in descending order."""
    months = {
        r[0]
        for r in con.execute("SELECT DISTINCT substr(date,1,7) FROM transactions WHERE date<>'' AND is_deleted=0")
        if r[0]
    }
    months.update({r[0] for r in con.execute("SELECT month FROM monthly_plans")})
    time_ctx = get_time_context(con)
    months.add(time_ctx["current_month"])
    months.add(time_ctx["planning_month"])
    return sorted(months, reverse=True)


def previous_months(month: str, n: int = 6) -> list[str]:
    """Returns the n months preceding the target YYYY-MM."""
    y, m = map(int, month.split("-"))
    current = dt.date(y, m, 1)
    result = []
    for _ in range(n):
        current = (current - dt.timedelta(days=1)).replace(day=1)
        result.append(current.strftime("%Y-%m"))
    return result


def normalized_category(description: str, category: str, subtype: str = "") -> str:
    """Maps vendor names/keywords to standard category names if category is generic."""
    if category and category not in {"Other / Miscellaneous", "Lain-lain"}:
        return category
    text = f"{description} {subtype}".upper()
    if any(k in text for k in ["INDOMARET", "IDM INDOMA", "ALFAMART", "ALFAMIDI"]):
        return "Groceries & Daily Needs"
    if any(k in text for k in ["SPBU", "PERTAMINA"]):
        return "Fuel"
    if any(k in text for k in ["TELKOMSEL", "BY.U", "BYU", "XL AXIATA", "INDOSAT", "SMARTFREN"]):
        return "Phone & Internet"
    if any(k in text for k in ["SPOTIFY", "NETFLIX", "YOUTUBE PREMIUM", "ICLOUD"]):
        return "Subscriptions"
    if any(k in text for k in ["COFFEE", "KOPI", "BOBA", "CHATIME", "MIXUE", "ROTI O"]):
        return "Cafe & Drinks"
    if any(k in text for k in ["KANTIN", "WARTEG", "RESTAU", "BAKPAO", "HISANA", "SOTO ", "BAKSO", "NASI ", "AYAM ", "STEAK", "BURGER", "BENTO"]):
        return "Main Meals"
    return category or "Other / Miscellaneous"


def personal_expense_rows(con: sqlite3.Connection, month: str | None = None) -> list[sqlite3.Row]:
    """Retrieves personal confirmed/auto-classified expense transaction rows."""
    sql = """SELECT * FROM transactions
             WHERE transaction_type='Expense' AND money_context='Personal'
               AND status IN ('Confirmed','Auto-classified') AND is_deleted=0
               AND description NOT LIKE '[Rekonsiliasi]%'"""
    params: list[object] = []
    if month:
        sql += " AND substr(date,1,7)=?"
        params.append(month)
    return con.execute(sql, params).fetchall()


def personal_income_rows(con: sqlite3.Connection, month: str | None = None) -> list[sqlite3.Row]:
    """Retrieves personal confirmed/auto-classified income transaction rows."""
    sql = """SELECT * FROM transactions
             WHERE transaction_type='Income' AND money_context='Personal'
               AND status IN ('Confirmed','Auto-classified') AND is_deleted=0
               AND description NOT LIKE '[Rekonsiliasi]%'"""
    params: list[object] = []
    if month:
        sql += " AND substr(date,1,7)=?"
        params.append(month)
    return con.execute(sql, params).fetchall()


def effective_budget_spend(row: dict | sqlite3.Row) -> float:
    """Calculates canonical budget impact for a transaction row.
    - Legacy rows (budget_rule_version == 'legacy'): uses stored budget_effect directly (frozen).
    - Derived rows (budget_rule_version == 'derived'):
      amount for Expense + Personal + Confirmed/Auto-classified + is_deleted=0 + exclude_from_budget=0.
      0 for all others.
    """
    rule_ver = row.get("budget_rule_version") if isinstance(row, dict) else (row["budget_rule_version"] if "budget_rule_version" in row.keys() else "legacy")
    if rule_ver == "legacy" or not rule_ver:
        return float(row.get("budget_effect") if isinstance(row, dict) else row["budget_effect"] or 0.0)

    # Derived rule
    typ = row.get("transaction_type") if isinstance(row, dict) else row["transaction_type"]
    ctx = row.get("money_context") if isinstance(row, dict) else row["money_context"]
    status = row.get("status") if isinstance(row, dict) else row["status"]
    is_del = bool(row.get("is_deleted", 0) if isinstance(row, dict) else (row["is_deleted"] if "is_deleted" in row.keys() else 0))
    excluded = bool(row.get("exclude_from_budget", 0) if isinstance(row, dict) else (row["exclude_from_budget"] if "exclude_from_budget" in row.keys() else 0))

    if typ == "Expense" and ctx == "Personal" and status in ("Confirmed", "Auto-classified") and not is_del and not excluded:
        amt = row.get("amount") if isinstance(row, dict) else row["amount"]
        return round(float(amt or 0.0), 2)
    return 0.0


def compute_budget_suggestions(con: sqlite3.Connection, target_month: str) -> dict[str, float]:
    """Calculates suggested budgets for all categories based on historical spending."""
    prev = previous_months(target_month, 6)
    time_ctx = get_time_context(con)
    current_m = time_ctx["current_month"]
    day_of_m = time_ctx["day_of_month"]
    days_in_m = time_ctx["days_in_month"]

    monthly_by_cat: dict[str, dict[str, float]] = {c: {} for c in CATEGORIES}
    for m in prev:
        for r in personal_expense_rows(con, m):
            cat = normalized_category(r["description"], r["category"], r["subtype"])
            val = effective_budget_spend(r)
            if m == current_m and day_of_m < days_in_m and day_of_m > 0:
                val = val * (days_in_m / day_of_m)
            monthly_by_cat.setdefault(cat, {})[m] = (
                monthly_by_cat.setdefault(cat, {}).get(m, 0.0) + val
            )

    out: dict[str, float] = {}
    routine = {
        "Main Meals", "Snacks", "Cafe & Drinks", "Groceries & Daily Needs", "Fuel",
        "Parking/Toll", "Transport / Other Transport", "Personal Care", "Health",
        "Campus & Organization", "Phone & Internet", "Subscriptions", "Gifts & Giving",
    }
    for cat in CATEGORIES:
        if cat == "Research — Historical Only":
            continue
        if cat in OCCASIONAL_DEFAULT_OFF:
            out[cat] = 0
            continue
        if cat == "Vehicle Service":
            out[cat] = 50000
            continue
        if cat == "Education":
            out[cat] = 0
            continue
        vals = [max(0.0, monthly_by_cat.get(cat, {}).get(m, 0.0)) for m in prev[:3]]
        nonzero = [v for v in vals if v > 0]
        if not nonzero:
            out[cat] = 0
            continue
        if cat in routine:
            med = median(vals)
            weighted = vals[0] * 0.50 + vals[1] * 0.30 + vals[2] * 0.20
            base = max(med, weighted * 0.85)
            if cat == "Health":
                base = max(50000, min(base, 150000))
            amount = math.ceil(base / 10000) * 10000
            out[cat] = amount
        else:
            out[cat] = math.ceil(median(nonzero) / 10000) * 10000

    routine_total = sum(v for k, v in out.items() if k != "Other / Miscellaneous")
    out["Other / Miscellaneous"] = (
        math.ceil((routine_total * 0.10) / 10000) * 10000 if routine_total else 0
    )
    return out


def mutate_account_balance(con: sqlite3.Connection, account_name: str, delta: float, tx_date: str | None = None) -> None:
    """Adjusts current balance for an account by delta ONLY if tx_date >= account.balance_date."""
    if not account_name:
        return
    acc = con.execute("SELECT current_balance, balance_date, kind FROM accounts WHERE name=?", (account_name,)).fetchone()
    if not acc:
        return
    if acc["kind"] in {"External", "Investment"}:
        return
    if acc["current_balance"] is None:
        return

    date_str = tx_date or dt.date.today().isoformat()
    bal_date = acc["balance_date"] or ""

    # If transaction is backdated prior to the anchor snapshot date, it was already accounted for in that physical snapshot
    if bal_date and date_str < bal_date:
        return

    old_bal = float(acc["current_balance"])
    new_bal = old_bal + delta
    con.execute(
        "UPDATE accounts SET current_balance=? WHERE name=?",
        (new_bal, account_name),
    )
    con.execute(
        "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?,?,?,'mutation')",
        (account_name, new_bal, date_str),
    )


def create_reversal_transaction(con: sqlite3.Connection, tx_id: int, replacement_data: dict | None = None, reason: str = "") -> dict:
    """Enforces canonical double-entry immutability: Reverses a POSTED transaction and optionally creates a replacement."""
    tx = con.execute("SELECT * FROM transactions WHERE id=? AND is_deleted=0", (tx_id,)).fetchone()
    if not tx:
        raise ValueError("Transaksi tidak ditemukan.")
    tx_dict = rowdict(tx)

    if tx_dict["status"] not in {"Confirmed", "Auto-classified"}:
        # Draft or Provisional Neutral transactions can be updated/deleted directly
        if replacement_data is None:
            con.execute("UPDATE transactions SET is_deleted=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (tx_id,))
            return {"status": "deleted", "id": tx_id}
        else:
            con.execute(
                """UPDATE transactions SET
                    date=?, time=?, transaction_type=?, amount=?, account_from=?, account_to=?,
                    description=?, category=?, for_with_whom=?, money_context=?, status=?, notes=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    replacement_data.get("date", tx_dict["date"]),
                    replacement_data.get("time", tx_dict["time"]),
                    replacement_data.get("transaction_type", tx_dict["transaction_type"]),
                    float(replacement_data.get("amount", tx_dict["amount"])),
                    replacement_data.get("account_from", tx_dict["account_from"]),
                    replacement_data.get("account_to", tx_dict["account_to"]),
                    replacement_data.get("description", tx_dict["description"]),
                    replacement_data.get("category", tx_dict["category"]),
                    replacement_data.get("for_with_whom", tx_dict["for_with_whom"]),
                    replacement_data.get("money_context", tx_dict["money_context"]),
                    replacement_data.get("status", tx_dict["status"]),
                    replacement_data.get("notes", tx_dict["notes"]),
                    tx_id
                )
            )
            return {"status": "updated", "id": tx_id}

    # POSTED transaction reversal workflow:
    today_str = dt.date.today().isoformat()
    rev_desc = f"[Koreksi Reversal] {tx_dict['description']} ({reason or 'Pembatalan transaksi posted'})"

    rev_type = "Income" if tx_dict["transaction_type"] == "Expense" else ("Expense" if tx_dict["transaction_type"] == "Income" else "Transfer")
    rev_from = tx_dict["account_to"] if tx_dict["transaction_type"] == "Transfer" else tx_dict["account_from"]
    rev_to = tx_dict["account_from"] if tx_dict["transaction_type"] == "Transfer" else tx_dict["account_to"]

    cur = con.execute(
        """INSERT INTO transactions (
            date, time, transaction_type, amount, account_from, account_to,
            description, category, for_with_whom, money_context, status, reversal_of_id, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Confirmed', ?, ?)""",
        (
            today_str,
            tx_dict["time"],
            rev_type,
            tx_dict["amount"],
            rev_from,
            rev_to,
            rev_desc,
            tx_dict["category"],
            tx_dict["for_with_whom"],
            tx_dict["money_context"],
            tx_id,
            f"Offsetting reversal of Transaction #{tx_id}"
        )
    )
    rev_id = cur.lastrowid

    # Update account balances for reversal
    if tx_dict["transaction_type"] == "Expense" and tx_dict["account_from"]:
        mutate_account_balance(con, tx_dict["account_from"], tx_dict["amount"], today_str)
    elif tx_dict["transaction_type"] == "Income" and tx_dict["account_to"]:
        mutate_account_balance(con, tx_dict["account_to"], -tx_dict["amount"], today_str)
    elif tx_dict["transaction_type"] == "Transfer":
        if tx_dict["account_from"]:
            mutate_account_balance(con, tx_dict["account_from"], tx_dict["amount"], today_str)
        if tx_dict["account_to"]:
            mutate_account_balance(con, tx_dict["account_to"], -tx_dict["amount"], today_str)

    con.execute(
        "UPDATE transactions SET notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (f"{tx_dict['notes']} [Dikoreksi oleh Reversal #{rev_id}]".strip(), tx_id)
    )

    new_tx_id = None
    if replacement_data:
        cur2 = con.execute(
            """INSERT INTO transactions (
                date, time, transaction_type, amount, account_from, account_to,
                description, category, for_with_whom, money_context, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Confirmed', ?)""",
            (
                replacement_data.get("date", today_str),
                replacement_data.get("time", ""),
                replacement_data.get("transaction_type", "Expense"),
                float(replacement_data.get("amount", 0)),
                replacement_data.get("account_from", ""),
                replacement_data.get("account_to", ""),
                replacement_data.get("description", ""),
                replacement_data.get("category", "Other / Miscellaneous"),
                replacement_data.get("for_with_whom", "Personal / Self"),
                replacement_data.get("money_context", "Personal"),
                f"[Transaksi Pengganti Tx #{tx_id}] {replacement_data.get('notes', '')}".strip()
            )
        )
        new_tx_id = cur2.lastrowid
        amt = float(replacement_data.get("amount", 0))
        ttype = replacement_data.get("transaction_type", "Expense")
        afrom = replacement_data.get("account_from", "")
        ato = replacement_data.get("account_to", "")
        if ttype == "Expense" and afrom:
            mutate_account_balance(con, afrom, -amt, replacement_data.get("date", today_str))
        elif ttype == "Income" and ato:
            mutate_account_balance(con, ato, amt, replacement_data.get("date", today_str))
        elif ttype == "Transfer":
            if afrom: mutate_account_balance(con, afrom, -amt, replacement_data.get("date", today_str))
            if ato: mutate_account_balance(con, ato, amt, replacement_data.get("date", today_str))

    return {"status": "reversed", "reversal_id": rev_id, "replacement_id": new_tx_id}


def reconstruct_account_balance(con: sqlite3.Connection, account_name: str) -> dict:
    """Reconstructs the expected account balance independently from the baseline snapshot
    and subsequent transaction mutations.

    Returns:
    {
        "status": "ok" | "unverifiable",
        "expected_balance": float | None,
        "cached_balance": float,
        "difference": float | None,
        "anchor_date": str | None,
        "reason": str,
    }
    """
    acc = con.execute("SELECT * FROM accounts WHERE name=?", (account_name,)).fetchone()
    if not acc:
        raise ValueError(f"Rekening '{account_name}' tidak ditemukan.")

    acc_dict = rowdict(acc) if isinstance(acc, sqlite3.Row) else dict(acc)
    cached_bal = float(acc_dict["current_balance"] or 0) if acc_dict["current_balance"] is not None else 0.0
    bal_date = acc_dict["balance_date"] or ""

    # Check whether snapshot_kind column exists in balance_snapshots
    snap_cols = [r[1] for r in con.execute("PRAGMA table_info(balance_snapshots)").fetchall()]
    has_kind = "snapshot_kind" in snap_cols

    # 1. Search for latest explicit trusted anchor (manual_anchor or initial_anchor)
    anchor_snap = None
    if has_kind:
        anchor_snap = con.execute(
            """SELECT id, balance, snapshot_date, created_at, snapshot_kind
               FROM balance_snapshots
               WHERE account_name=? AND snapshot_kind IN ('manual_anchor', 'initial_anchor')
               ORDER BY id DESC LIMIT 1""",
            (account_name,),
        ).fetchone()

    # 2. Fallback to legacy snapshot if no explicit anchor found
    if not anchor_snap:
        if has_kind:
            anchor_snap = con.execute(
                """SELECT id, balance, snapshot_date, created_at, snapshot_kind
                   FROM balance_snapshots
                   WHERE account_name=? AND (snapshot_kind='legacy' OR snapshot_kind IS NULL)
                   ORDER BY id ASC LIMIT 1""",
                (account_name,),
            ).fetchone()
        else:
            anchor_snap = con.execute(
                """SELECT id, balance, snapshot_date, created_at
                   FROM balance_snapshots
                   WHERE account_name=?
                   ORDER BY id ASC LIMIT 1""",
                (account_name,),
            ).fetchone()

    if not anchor_snap:
        return {
            "status": "unverifiable",
            "expected_balance": None,
            "cached_balance": cached_bal,
            "difference": None,
            "anchor_date": bal_date or None,
            "reason": "Tidak ada data snapshot saldo (anchor) tepercaya untuk akun ini.",
        }

    anchor_bal = float(anchor_snap["balance"])
    anchor_date = anchor_snap["snapshot_date"]
    anchor_created_at = anchor_snap["created_at"]

    # Check for timestamp collision on the selected anchor:
    collision_count = con.execute(
        """SELECT COUNT(*) FROM transactions
           WHERE is_deleted=0
             AND (account_from=? OR account_to=?)
             AND date=?
             AND created_at=?""",
        (account_name, account_name, anchor_date, anchor_created_at),
    ).fetchone()[0]

    if collision_count > 0:
        return {
            "status": "unverifiable",
            "expected_balance": None,
            "cached_balance": cached_bal,
            "difference": None,
            "anchor_date": anchor_date,
            "reason": "urutan transaksi dan snapshot pada waktu anchor tidak dapat dipastikan",
        }

    # Sum subsequent transaction mutations strictly after the chosen anchor
    tx_rows = con.execute(
        """SELECT amount, account_from, account_to
           FROM transactions
           WHERE is_deleted=0
             AND (account_from=? OR account_to=?)
             AND (date > ? OR (date = ? AND created_at > ?))""",
        (account_name, account_name, anchor_date, anchor_date, anchor_created_at),
    ).fetchall()

    in_mutations = round(sum(float(r["amount"]) for r in tx_rows if r["account_to"] == account_name), 2)
    out_mutations = round(sum(float(r["amount"]) for r in tx_rows if r["account_from"] == account_name), 2)

    expected_bal = round(anchor_bal + in_mutations - out_mutations, 2)
    diff = round(expected_bal - cached_bal, 2)

    if abs(diff) >= 0.005:
        reason = f"Selisih saldo terdeteksi: tercatat {cached_bal:,.2f} vs hitungan mutasi {expected_bal:,.2f} (selisih {diff:+,.2f})"
    else:
        reason = "Saldo terverifikasi sesuai snapshot & mutasi"

    return {
        "status": "ok",
        "expected_balance": expected_bal,
        "cached_balance": cached_bal,
        "difference": diff,
        "anchor_date": anchor_date,
        "reason": reason,
    }


def reconcile_account_balance(
    con: sqlite3.Connection,
    account_name: str,
    actual_balance: float,
    notes: str = "",
    force_anchor: bool = False,
) -> dict:
    """Performs account reconciliation audit workflow:
    - If account status is unverifiable and force_anchor is False: rejects reconciliation.
    - If account status is unverifiable and force_anchor is True: establishes explicit anchor baseline.
    - If account status is ok:
        E = reconstructed expected_balance
        C = current_balance cache
        A = confirmed actual_balance
        ledger_adjustment = A - E
        - If abs(ledger_adjustment) >= 0.005: creates exactly 1 [Rekonsiliasi] transaction for ledger_adjustment,
          then updates cache to A (without adding delta to stale cache).
        - If abs(ledger_adjustment) < 0.005 and abs(C - A) >= 0.005: rebuilds cache to A without fake financial transaction.
        - If abs(ledger_adjustment) < 0.005 and abs(C - A) < 0.005: records confirmation audit log.
    """
    acc = con.execute("SELECT * FROM accounts WHERE name=?", (account_name,)).fetchone()
    if not acc:
        raise ValueError(f"Rekening '{account_name}' tidak ditemukan.")

    today_str = dt.date.today().isoformat()
    now_time = dt.datetime.now().strftime("%H:%M")
    actual_bal = round(float(actual_balance), 2)

    recon = reconstruct_account_balance(con, account_name)

    if recon["status"] == "unverifiable":
        if not force_anchor:
            raise ValueError(
                f"Rekening '{account_name}' belum memiliki anchor saldo yang terverifikasi ({recon['reason']}). "
                "Harap konfirmasi penetapan anchor saldo awal terlebih dahulu."
            )
        # Establish explicit trusted anchor baseline without creating fake income/expense
        con.execute(
            "UPDATE accounts SET current_balance=?, balance_date=?, last_reconciled_at=? WHERE name=?",
            (actual_bal, today_str, today_str, account_name),
        )
        con.execute(
            "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?, ?, ?, 'manual_anchor')",
            (account_name, actual_bal, today_str),
        )
        audit_note = f"Penetapan anchor saldo awal tepercaya ({notes or 'Inisialisasi fisik'})"
        con.execute(
            "INSERT INTO reconciliations (account_name, book_balance, actual_balance, difference, notes, date_created) VALUES (?, ?, ?, ?, ?, ?)",
            (account_name, actual_bal, actual_bal, 0.0, audit_note, today_str),
        )
        return {
            "account_name": account_name,
            "book_balance": actual_bal,
            "actual_balance": actual_bal,
            "difference": 0.0,
            "adjustment_tx_id": None,
            "reconciled_at": today_str,
            "action": "anchor_established",
        }

    expected_bal = recon["expected_balance"]
    cached_bal = recon["cached_balance"]
    ledger_adjustment = round(actual_bal - expected_bal, 2)
    cache_drift = round(actual_bal - cached_bal, 2)

    adj_tx_id = None

    if abs(ledger_adjustment) >= 0.005:
        ttype = "Adjustment"
        afrom = "" if ledger_adjustment > 0 else account_name
        ato = account_name if ledger_adjustment > 0 else ""
        desc = f"[Rekonsiliasi] Penyesuaian Saldo Audit ({notes or 'Selisih fisik'})"

        cur = con.execute(
            """INSERT INTO transactions (
                date, time, transaction_type, amount, account_from, account_to,
                description, category, for_with_whom, money_context, subtype, status, budget_effect, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Other / Miscellaneous', 'Personal / Self', 'Personal', 'Balance Reconciliation', 'Confirmed', 0, ?)""",
            (today_str, now_time, ttype, abs(ledger_adjustment), afrom, ato, desc, f"Selisih saldo audit: {ledger_adjustment:+} IDR"),
        )
        adj_tx_id = cur.lastrowid

        con.execute("UPDATE accounts SET current_balance=?, last_reconciled_at=? WHERE name=?", (actual_bal, today_str, account_name))
        con.execute("INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?, ?, ?, 'reconciliation')", (account_name, actual_bal, today_str))

        con.execute(
            "INSERT INTO reconciliations (account_name, book_balance, actual_balance, difference, notes, date_created) VALUES (?, ?, ?, ?, ?, ?)",
            (account_name, expected_bal, actual_bal, ledger_adjustment, notes or "Penyesuaian transaksi rekonsiliasi", today_str),
        )
        action = "ledger_adjusted"

    elif abs(cache_drift) >= 0.005:
        con.execute("UPDATE accounts SET current_balance=?, last_reconciled_at=? WHERE name=?", (actual_bal, today_str, account_name))
        con.execute("INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?, ?, ?, 'reconciliation')", (account_name, actual_bal, today_str))

        audit_note = f"Perbaikan cache saldo tanpa mutasi ledger ({notes or 'Sinkronisasi cache'})"
        con.execute(
            "INSERT INTO reconciliations (account_name, book_balance, actual_balance, difference, notes, date_created) VALUES (?, ?, ?, ?, ?, ?)",
            (account_name, expected_bal, actual_bal, 0.0, audit_note, today_str),
        )
        action = "cache_repaired"

    else:
        con.execute("UPDATE accounts SET last_reconciled_at=? WHERE name=?", (today_str, account_name))
        con.execute(
            "INSERT INTO reconciliations (account_name, book_balance, actual_balance, difference, notes, date_created) VALUES (?, ?, ?, ?, ?, ?)",
            (account_name, expected_bal, actual_bal, 0.0, notes or "Audit konfirmasi saldo (cocok)", today_str),
        )
        action = "confirmed_in_sync"

    return {
        "account_name": account_name,
        "book_balance": expected_bal,
        "actual_balance": actual_bal,
        "difference": ledger_adjustment,
        "adjustment_tx_id": adj_tx_id,
        "reconciled_at": today_str,
        "action": action,
    }


def save_account_balance(con: sqlite3.Connection, payload: dict) -> dict:
    """Creates or updates an account profile, routing balance adjustments for existing accounts through reconciliation."""
    from db import inferred_account_kind

    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("Nama rekening diperlukan.")

    bal = payload.get("current_balance")
    bal_value = None if bal in {None, ""} else float(bal)
    date = str(payload.get("balance_date", "")).strip() or dt.date.today().isoformat()
    protected = int(bool(payload.get("protected", False)))
    protected_amount = max(0.0, float(payload.get("protected_amount", 0) or 0))
    active = int(bool(payload.get("active", True)))
    if not active and protected_amount > 0:
        raise ValueError(f"Rekening '{name}' tidak dapat dinonaktifkan karena memiliki dana dijaga (Rp{protected_amount:,.2f}). Pindahkan atau lepaskan dana dijaga terlebih dahulu.")
    kind = str(payload.get("kind", inferred_account_kind(name)))
    if kind not in {"Owned", "Receivable", "Suspense", "Investment"}:
        kind = "Owned"
    force_anchor = bool(payload.get("force_anchor", False))

    existing = con.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
    if existing is None:
        con.execute(
            """INSERT INTO accounts(name, kind, current_balance, balance_date, protected, protected_amount, active)
               VALUES (?,?,?,?,?,?,?)""",
            (name, kind, bal_value, date, protected, protected_amount, active),
        )
        if bal_value is not None:
            con.execute(
                "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?,?,?, 'initial_anchor')",
                (name, bal_value, date),
            )
    else:
        con.execute(
            """UPDATE accounts SET
                   kind=?, balance_date=?, protected=?,
                   protected_amount=?, active=?
               WHERE name=?""",
            (kind, date, protected, protected_amount, active, name),
        )
        if bal_value is not None:
            if existing["current_balance"] is None:
                con.execute(
                    "UPDATE accounts SET current_balance=? WHERE name=?",
                    (bal_value, name),
                )
                con.execute(
                    "INSERT INTO balance_snapshots(account_name, balance, snapshot_date, snapshot_kind) VALUES (?,?,?, 'initial_anchor')",
                    (name, bal_value, date),
                )
            else:
                old_bal = float(existing["current_balance"] or 0)
                if abs(bal_value - old_bal) >= 0.005 or force_anchor:
                    reconcile_account_balance(con, name, bal_value, "Koreksi saldo manual", force_anchor=force_anchor)

    return {"status": "success"}


def verify_balances(con: sqlite3.Connection) -> list[dict]:
    """Verifies each account's current_balance cache against an independent anchor snapshot
    and subsequent transaction mutations using reconstruct_account_balance.

    Returns a list of issue reports for accounts with:
    - status='unverifiable': No independent snapshot anchor exists, or ordering is ambiguous (timestamp collision).
    - status='discrepancy': Reconstructed balance differs from cached current_balance by >= 0.005.

    Does NOT automatically modify or fix account balances.
    """
    issues = []
    accounts = con.execute(
        "SELECT * FROM accounts WHERE active=1 AND current_balance IS NOT NULL"
    ).fetchall()

    for acc in accounts:
        name = acc["name"]
        recon = reconstruct_account_balance(con, name)
        if recon["status"] == "unverifiable":
            issues.append({
                "account_name": name,
                "status": "unverifiable",
                "reason": recon["reason"],
                "cached_balance": recon["cached_balance"],
                "expected_balance": None,
                "difference": None,
                "anchor_date": recon["anchor_date"],
            })
        elif abs(recon["difference"]) >= 0.005:
            issues.append({
                "account_name": name,
                "status": "discrepancy",
                "reason": recon["reason"],
                "cached_balance": recon["cached_balance"],
                "expected_balance": recon["expected_balance"],
                "difference": recon["difference"],
                "anchor_date": recon["anchor_date"],
            })

    return issues


def plan_data(con: sqlite3.Connection, month: str) -> dict:
    """Retrieves plan parameters and category budgets for a given month."""
    seed_plan(con, month)
    plan = rowdict(con.execute("SELECT * FROM monthly_plans WHERE month=?", (month,)).fetchone())
    budgets = [
        rowdict(r)
        for r in con.execute("SELECT * FROM budget_categories WHERE month=? ORDER BY category", (month,)).fetchall()
    ]
    return {"plan": plan, "budgets": budgets}


def owned_accounts(con: sqlite3.Connection) -> set[str]:
    """Returns active owned account names."""
    return {r[0] for r in con.execute("SELECT name FROM accounts WHERE kind='Owned' AND active=1")}


def pass_through_historical_net(con: sqlite3.Connection) -> float:
    """Calculates audit historical pass-through net movement."""
    owned = owned_accounts(con)
    total = 0.0
    rows = con.execute(
        """SELECT transaction_type, amount, account_from, account_to
           FROM transactions
           WHERE money_context='Pass-through' AND status<>'Provisional Neutral'"""
    ).fetchall()
    for r in rows:
        a = float(r["amount"])
        src_owned = r["account_from"] in owned
        dst_owned = r["account_to"] in owned
        if dst_owned and not src_owned:
            total += a
        elif src_owned and not dst_owned:
            total -= a
    return total


def get_account_protected_contribution(con: sqlite3.Connection, account_name: str) -> float:
    """Calculates protected savings contribution for a given account.
    If protected_amount > 0: returns protected_amount (explicit target/purpose independent of cash location).
    Else if protected is True: returns max(0, current_balance) (legacy fallback).
    Else: 0.0.
    """
    row = con.execute("SELECT current_balance, protected, protected_amount FROM accounts WHERE name=?", (account_name,)).fetchone()
    if not row:
        return 0.0
    p_amt = max(0.0, float(row["protected_amount"] or 0.0))
    if p_amt > 0:
        return round(p_amt, 2)
    elif bool(row["protected"]):
        bal = max(0.0, float(row["current_balance"] or 0.0))
        return round(bal, 2)
    return 0.0


def reduce_account_protected_contribution(con: sqlite3.Connection, account_name: str, amount_to_reduce: float) -> None:
    """Reduces the protected_amount (or disables full protection) of an account when an allocation is Spent or Released."""
    row = con.execute("SELECT current_balance, protected, protected_amount FROM accounts WHERE name=?", (account_name,)).fetchone()
    if not row or amount_to_reduce <= 0:
        return
    bal = max(0.0, float(row["current_balance"] or 0.0))
    p_amt = float(row["protected_amount"] or 0.0)
    is_prot = bool(row["protected"])

    if p_amt > 0:
        new_p_amt = max(0.0, round(p_amt - amount_to_reduce, 2))
        con.execute("UPDATE accounts SET protected_amount=? WHERE name=?", (new_p_amt, account_name))
    elif is_prot:
        new_p_amt = max(0.0, round(bal - amount_to_reduce, 2))
        con.execute("UPDATE accounts SET protected=0, protected_amount=? WHERE name=?", (new_p_amt, account_name))


# Re-export migration helper from db
from db import migrate_allocation_schema


def get_total_allocated_savings(con: sqlite3.Connection) -> float:
    """Calculates total active allocated savings from allocation_goals if populated, or legacy protected accounts."""
    has_goals = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='allocation_goals'").fetchone()[0] > 0
    if has_goals:
        goal_count = con.execute("SELECT COUNT(*) FROM allocation_goals").fetchone()[0]
        if goal_count > 0:
            row = con.execute("SELECT COALESCE(SUM(allocated_amount), 0.0) FROM allocation_goals WHERE status='Active'").fetchone()
            return round(float(row[0] or 0.0), 2)

    # Legacy fallback: accounts.protected_amount
    protected = 0.0
    for r in con.execute("SELECT name FROM accounts WHERE active=1 AND kind='Owned'").fetchall():
        protected += get_account_protected_contribution(con, r["name"])
    return round(protected, 2)


def get_allocation_summary(con: sqlite3.Connection) -> dict:
    """Calculates allocation breakdown across Emergency, Goals, and General."""
    has_goals = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='allocation_goals'").fetchone()[0] > 0
    if not has_goals:
        legacy_protected = get_total_allocated_savings(con)
        return {
            "emergency_allocated": legacy_protected,
            "goals_allocated": 0.0,
            "general_allocated": 0.0,
            "total_allocated": legacy_protected,
            "goals": [],
        }

    rows = [rowdict(r) for r in con.execute("SELECT * FROM allocation_goals ORDER BY CASE status WHEN 'Active' THEN 0 ELSE 1 END, priority, id").fetchall()]
    if not rows:
        legacy_protected = get_total_allocated_savings(con)
        return {
            "emergency_allocated": legacy_protected,
            "goals_allocated": 0.0,
            "general_allocated": 0.0,
            "total_allocated": legacy_protected,
            "goals": [],
        }

    emergency = sum(float(r["allocated_amount"] or 0) for r in rows if r["status"] == "Active" and r["kind"] == "Emergency")
    goals = sum(float(r["allocated_amount"] or 0) for r in rows if r["status"] == "Active" and r["kind"] == "Goal")
    general = sum(float(r["allocated_amount"] or 0) for r in rows if r["status"] == "Active" and r["kind"] == "General")
    total = round(emergency + goals + general, 2)
    return {
        "emergency_allocated": round(emergency, 2),
        "goals_allocated": round(goals, 2),
        "general_allocated": round(general, 2),
        "total_allocated": total,
        "goals": rows,
    }


def create_allocation_goal(
    con: sqlite3.Connection,
    name: str,
    kind: str,
    target_amount: float = 0.0,
    initial_funding: float = 0.0,
    target_date: str = "",
    priority: int = 1,
    preferred_account: str = "",
    notes: str = "",
) -> dict:
    """Creates a new allocation goal or emergency reserve."""
    if not name or not name.strip():
        raise ValueError("Nama alokasi / tujuan keuangan wajib diisi")
    if kind not in {"Emergency", "Goal", "General"}:
        raise ValueError(f"Jenis alokasi tidak valid: {kind}")
    target_amount = round(float(target_amount or 0.0), 2)
    if target_amount < 0:
        raise ValueError("Target nominal alokasi tidak boleh negatif")
    initial_funding = round(float(initial_funding or 0.0), 2)
    if initial_funding < 0:
        raise ValueError("Nominal pendanaan awal tidak boleh negatif")

    cur = con.execute(
        """INSERT INTO allocation_goals (name, kind, target_amount, allocated_amount, target_date, priority, status, preferred_account, notes)
           VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?)""",
        (name.strip(), kind, target_amount, initial_funding, target_date.strip(), priority, preferred_account.strip(), notes.strip()),
    )
    goal_id = cur.lastrowid
    return {
        "status": "success",
        "goal_id": goal_id,
        "id": goal_id,
        "name": name.strip(),
        "kind": kind,
        "target_amount": target_amount,
        "allocated_amount": initial_funding,
    }


def fund_allocation_goal(con: sqlite3.Connection, goal_id: int, amount: float, notes: str = "") -> dict:
    """Adds funding to an existing active allocation goal."""
    amount = round(float(amount or 0.0), 2)
    if amount <= 0:
        raise ValueError("Nominal pendanaan harus lebih besar dari nol")

    goal = con.execute("SELECT * FROM allocation_goals WHERE id=?", (goal_id,)).fetchone()
    if not goal:
        raise ValueError(f"Tujuan alokasi #{goal_id} tidak ditemukan")
    if goal["status"] != "Active":
        raise ValueError(f"Tujuan alokasi #{goal_id} tidak aktif ({goal['status']})")

    new_allocated = round(float(goal["allocated_amount"] or 0.0) + amount, 2)
    con.execute("UPDATE allocation_goals SET allocated_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_allocated, goal_id))
    return {"status": "success", "goal_id": goal_id, "allocated_amount": new_allocated, "added": amount}


def release_allocation_goal(con: sqlite3.Connection, goal_id: int, amount: float | None = None, target_status: str | None = None) -> dict:
    """Releases allocated funds back to unallocated/spendable pool."""
    goal = con.execute("SELECT * FROM allocation_goals WHERE id=?", (goal_id,)).fetchone()
    if not goal:
        raise ValueError(f"Tujuan alokasi #{goal_id} tidak ditemukan")

    cur_allocated = round(float(goal["allocated_amount"] or 0.0), 2)
    if amount is None or amount == 0.0:
        if cur_allocated == 0.0:
            return {"status": "success", "goal_id": goal_id, "allocated_amount": 0.0, "released": 0.0, "idempotent": True}
        release_amt = cur_allocated
    else:
        release_amt = round(float(amount), 2)

    if release_amt < 0 or release_amt > (cur_allocated + 0.005):
        raise ValueError(f"Nominal pelepasan ({release_amt}) melebihi dana teralokasi ({cur_allocated})")

    new_allocated = max(0.0, round(cur_allocated - release_amt, 2))
    new_status = goal["status"]
    if target_status and target_status in {"Released", "Cancelled", "Active", "Achieved"}:
        new_status = target_status
    elif new_allocated == 0.0 and target_status == "Released":
        new_status = "Released"

    con.execute("UPDATE allocation_goals SET allocated_amount=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_allocated, new_status, goal_id))
    return {"status": "success", "goal_id": goal_id, "allocated_amount": new_allocated, "released": release_amt, "status_goal": new_status}


def spend_allocation_goal(
    con: sqlite3.Connection,
    goal_id: int,
    amount: float,
    account_name: str = "",
    description: str = "",
    category: str = "Other / Miscellaneous",
    tx_date: str | None = None,
    is_upcoming_settlement: bool = False,
) -> dict:
    """Spends from an allocation goal, optionally creating an Expense transaction if not settlement."""
    goal = con.execute("SELECT * FROM allocation_goals WHERE id=?", (goal_id,)).fetchone()
    if not goal:
        raise ValueError(f"Tujuan alokasi #{goal_id} tidak ditemukan")
    if goal["status"] != "Active":
        raise ValueError(f"Tujuan alokasi #{goal_id} tidak aktif ({goal['status']})")

    amount = round(float(amount or 0.0), 2)
    if amount <= 0:
        raise ValueError("Nominal pengeluaran harus lebih besar dari nol")

    cur_allocated = round(float(goal["allocated_amount"] or 0.0), 2)
    if amount > (cur_allocated + 0.005):
        raise ValueError(f"Nominal pengeluaran ({amount}) melebihi dana teralokasi ({cur_allocated})")

    new_allocated = max(0.0, round(cur_allocated - amount, 2))
    con.execute("UPDATE allocation_goals SET allocated_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_allocated, goal_id))

    tx_id = None
    if not is_upcoming_settlement:
        if not account_name:
            account_name = goal["preferred_account"] or "BCA Main"
        p_date = tx_date or get_wib_today_str()
        desc = description or f"Penggunaan Dana Tujuan: {goal['name']}"
        cur_tx = con.execute(
            """INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, category, for_with_whom, money_context, status, budget_effect)
               VALUES (?, ?, 'Expense', ?, ?, '', ?, ?, 'Personal / Self', 'Personal', 'Confirmed', ?)""",
            (p_date, get_wib_now().strftime("%H:%M"), amount, account_name, desc, category, amount),
        )
        tx_id = cur_tx.lastrowid
        mutate_account_balance(con, account_name, -amount, p_date)

    return {"status": "success", "goal_id": goal_id, "allocated_amount": new_allocated, "spent": amount, "transaction_id": tx_id}


def link_goal_to_upcoming(con: sqlite3.Connection, goal_id: int, upcoming_id: int) -> dict:
    """Links an active allocation goal to an upcoming commitment."""
    goal = con.execute("SELECT * FROM allocation_goals WHERE id=?", (goal_id,)).fetchone()
    if not goal:
        raise ValueError(f"Tujuan alokasi #{goal_id} tidak ditemukan")
    if goal["status"] != "Active":
        raise ValueError("Hanya alokasi berstatus 'Active' yang dapat ditautkan ke kewajiban")

    u = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    if not u:
        raise ValueError(f"Kewajiban #{upcoming_id} tidak ditemukan")

    con.execute("UPDATE upcoming SET linked_goal_id=? WHERE id=?", (goal_id, upcoming_id))
    return {"status": "success", "goal_id": goal_id, "upcoming_id": upcoming_id}


def unlink_goal_from_upcoming(con: sqlite3.Connection, upcoming_id: int) -> dict:
    """Unlinks an allocation goal from an upcoming commitment."""
    con.execute("UPDATE upcoming SET linked_goal_id=NULL WHERE id=?", (upcoming_id,))
    return {"status": "success", "upcoming_id": upcoming_id}


def set_upcoming_status_and_reservation(
    con: sqlite3.Connection,
    upcoming_id: int,
    status: str | None = None,
    reserve_now: int | None = None,
    linked_goal_id: int | None = None,
) -> dict:
    """Updates upcoming status, reserve_now flag, or linked goal atomically."""
    u = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    if not u:
        raise ValueError(f"Kewajiban #{upcoming_id} tidak ditemukan")

    updates = []
    params = []
    if status is not None:
        if status not in {"Upcoming", "Confirmed", "Tentative", "Paid", "Cancelled", "Skipped"}:
            raise ValueError(f"Status kewajiban tidak valid: {status}")
        updates.append("status=?")
        params.append(status)
    if reserve_now is not None:
        if reserve_now not in (0, 1):
            raise ValueError(f"Nilai reserve_now harus 0 atau 1: {reserve_now}")
        updates.append("reserve_now=?")
        params.append(reserve_now)
    if linked_goal_id is not None:
        updates.append("linked_goal_id=?")
        params.append(linked_goal_id)

    if updates:
        params.append(upcoming_id)
        con.execute(f"UPDATE upcoming SET {', '.join(updates)} WHERE id=?", tuple(params))

    updated = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    return {"status": "success", "upcoming": rowdict(updated)}


def get_upcoming_active_coverage(con: sqlite3.Connection, upcoming_id: int | None = None) -> float | dict[int, float]:
    """Calculates active protected allocation and goal coverage for upcoming commitments."""
    cov_map: dict[int, float] = {}

    # 1. Coverage from protected_allocations (legacy)
    has_pa = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='protected_allocations'").fetchone()[0] > 0
    if has_pa:
        cols = [r[1] for r in con.execute("PRAGMA table_info(protected_allocations)").fetchall()]
        if "covers_upcoming_id" in cols:
            rows = con.execute(
                "SELECT covers_upcoming_id, COALESCE(SUM(amount), 0.0) FROM protected_allocations WHERE status='Active' AND covers_upcoming_id IS NOT NULL GROUP BY covers_upcoming_id"
            ).fetchall()
            for r in rows:
                cov_map[r[0]] = round(cov_map.get(r[0], 0.0) + float(r[1] or 0.0), 2)

    # 2. Coverage from allocation_goals (new engine)
    has_goals = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='allocation_goals'").fetchone()[0] > 0
    u_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
    if has_goals and "linked_goal_id" in u_cols:
        rows_g = con.execute(
            """SELECT u.id, COALESCE(ag.allocated_amount, 0.0)
               FROM upcoming u
               JOIN allocation_goals ag ON u.linked_goal_id = ag.id
               WHERE ag.status='Active' AND u.linked_goal_id IS NOT NULL"""
        ).fetchall()
        for r in rows_g:
            cov_map[r[0]] = round(cov_map.get(r[0], 0.0) + float(r[1] or 0.0), 2)

    if upcoming_id is not None:
        return cov_map.get(upcoming_id, 0.0)
    return cov_map


def get_protected_allocations(con: sqlite3.Connection) -> list[dict]:
    """Retrieves protected allocations list with optional linked upcoming title and amount."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(protected_allocations)").fetchall()]
    if "covers_upcoming_id" in cols:
        query = """
            SELECT pa.*, u.title AS covers_upcoming_title, u.amount AS covers_upcoming_amount
            FROM protected_allocations pa
            LEFT JOIN upcoming u ON pa.covers_upcoming_id = u.id
            ORDER BY CASE pa.status WHEN 'Active' THEN 0 ELSE 1 END, pa.target_date, pa.id
        """
    else:
        query = "SELECT * FROM protected_allocations ORDER BY CASE status WHEN 'Active' THEN 0 ELSE 1 END, target_date, id"
    return [rowdict(r) for r in con.execute(query).fetchall()]


def set_protected_allocation_status(con: sqlite3.Connection, allocation_id: int, target_status: str, is_upcoming_settlement: bool = False) -> dict:
    """Centralized, idempotent lifecycle transition for protected allocations."""
    if target_status not in {"Active", "Spent", "Released"}:
        raise ValueError(f"Status alokasi tidak valid: {target_status}")

    alloc = con.execute("SELECT * FROM protected_allocations WHERE id=?", (allocation_id,)).fetchone()
    if not alloc:
        raise ValueError(f"Alokasi #{allocation_id} tidak ditemukan")

    cur_status = alloc["status"]
    if cur_status == target_status:
        return {"status": "success", "id": allocation_id, "allocation_status": target_status, "idempotent": True}

    if cur_status in {"Spent", "Released"}:
        raise ValueError(f"Alokasi #{allocation_id} sudah berstatus terminal ({cur_status}) dan tidak dapat diubah lagi")

    amt = float(alloc["amount"] or 0)
    acc_name = alloc["account"] or "BCA Poket: Tabungan"

    if target_status in {"Spent", "Released"}:
        reduce_account_protected_contribution(con, acc_name, amt)

        if target_status == "Spent" and not is_upcoming_settlement:
            tx_date = get_wib_today_str()
            con.execute(
                """INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, category, for_with_whom, money_context, status, budget_effect)
                   VALUES (?, ?, 'Expense', ?, ?, '', ?, 'Other / Miscellaneous', 'Personal / Self', 'Personal', 'Confirmed', ?)""",
                (tx_date, get_wib_now().strftime("%H:%M"), amt, acc_name, f"Penggunaan Dana Dijaga: {alloc['title']}", amt),
            )
            mutate_account_balance(con, acc_name, -amt, tx_date)

    con.execute("UPDATE protected_allocations SET status=? WHERE id=?", (target_status, allocation_id))
    return {"status": "success", "id": allocation_id, "allocation_status": target_status}


def link_protected_allocation(con: sqlite3.Connection, allocation_id: int, upcoming_id: int) -> dict:
    """Links an active protected allocation to an upcoming commitment."""
    alloc = con.execute("SELECT * FROM protected_allocations WHERE id=?", (allocation_id,)).fetchone()
    if not alloc:
        raise ValueError(f"Alokasi #{allocation_id} tidak ditemukan")
    if alloc["status"] != "Active":
        raise ValueError("Hanya alokasi berstatus 'Active' yang dapat ditautkan ke kewajiban")

    u = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    if not u:
        raise ValueError(f"Kewajiban #{upcoming_id} tidak ditemukan")
    if u["status"] != "Upcoming":
        raise ValueError("Hanya kewajiban berstatus 'Upcoming' yang dapat ditautkan ke alokasi dana")

    current_cov = float(con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM protected_allocations WHERE status='Active' AND covers_upcoming_id=? AND id != ?",
        (upcoming_id, allocation_id),
    ).fetchone()[0] or 0)

    alloc_amt = float(alloc["amount"] or 0)
    new_total_cov = round(current_cov + alloc_amt, 2)
    upcoming_amt = round(float(u["amount"] or 0), 2)
    if new_total_cov > (upcoming_amt + 0.005):
        raise ValueError(f"Total alokasi penutup ({new_total_cov}) melebihi nominal kewajiban ({upcoming_amt})")

    con.execute("UPDATE protected_allocations SET covers_upcoming_id=? WHERE id=?", (upcoming_id, allocation_id))
    return {"status": "success", "allocation_id": allocation_id, "upcoming_id": upcoming_id, "coverage": new_total_cov}


def unlink_protected_allocation(con: sqlite3.Connection, allocation_id: int) -> dict:
    """Unlinks a protected allocation from its upcoming commitment."""
    alloc = con.execute("SELECT * FROM protected_allocations WHERE id=?", (allocation_id,)).fetchone()
    if not alloc:
        raise ValueError(f"Alokasi #{allocation_id} tidak ditemukan")
    con.execute("UPDATE protected_allocations SET covers_upcoming_id=NULL WHERE id=?", (allocation_id,))
    return {"status": "success", "allocation_id": allocation_id}


def balance_snapshot(con: sqlite3.Connection) -> dict:
    """Provides snapshot of balances and protected savings using single-source-of-truth helper."""
    rows = [rowdict(r) for r in con.execute("SELECT * FROM accounts ORDER BY kind, name").fetchall()]
    active_owned = [r for r in rows if r["active"] and r["kind"] == "Owned"]
    set_rows = [r for r in active_owned if r["current_balance"] is not None]
    complete = len(set_rows) == len(active_owned) and len(active_owned) > 0
    total = round(sum(float(r["current_balance"] or 0) for r in set_rows), 2) if complete else None
    protected = None
    if complete:
        protected = get_total_allocated_savings(con)
    return {
        "accounts": rows,
        "complete": complete,
        "total_balance": total,
        "protected_savings": protected,
        "count_set": len(set_rows),
        "count_active_owned": len(active_owned),
    }


def account_management(con: sqlite3.Connection) -> dict:
    """Retrieves full account management data including allocations."""
    snap = balance_snapshot(con)
    allocations = get_protected_allocations(con)
    active_alloc_sum = sum(float(a["amount"]) for a in allocations if a.get("status") == "Active")
    unallocated = (snap["protected_savings"] - active_alloc_sum) if snap["protected_savings"] is not None else None
    return {
        "accounts": snap["accounts"],
        "protected_allocations": allocations,
        "current_pass_through_outstanding": current_pass_through_outstanding(con),
        "historical_pass_through_net": pass_through_historical_net(con),
        "protected_unallocated": round(max(0.0, unallocated), 2) if unallocated is not None else None,
    }


def current_pass_through_outstanding(con: sqlite3.Connection) -> float:
    """Returns active custody outstanding as replacement for legacy manual setting."""
    return total_custody_outstanding(con)


def reconstruct_debt_outstanding(con: sqlite3.Connection, debt_id: int) -> float:
    """Calculates outstanding balance of a debt position from its signed events ledger."""
    row = con.execute(
        "SELECT COALESCE(SUM(effect * amount), 0.0) FROM debt_events WHERE debt_id=?",
        (debt_id,),
    ).fetchone()
    return round(float(row[0] or 0.0), 2)


def update_debt_status(con: sqlite3.Connection, debt_id: int) -> str:
    """Calculates outstanding and automatically updates debt status (Active, Settled, WrittenOff)."""
    outstanding = reconstruct_debt_outstanding(con, debt_id)
    if outstanding < -0.005:
        raise ValueError(f"Outstanding posisi #{debt_id} bernilai negatif ({outstanding})")

    # Check if there is a WriteOff event
    has_writeoff = bool(con.execute(
        "SELECT id FROM debt_events WHERE debt_id=? AND event_type='WriteOff'",
        (debt_id,),
    ).fetchone())

    if abs(outstanding) < 0.005:
        status = "WrittenOff" if has_writeoff else "Settled"
    else:
        status = "Active"

    con.execute("UPDATE debts SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, debt_id))
    return status


def total_custody_outstanding(con: sqlite3.Connection) -> float:
    """Returns total active Custody (Titipan) from event ledger."""
    rows = con.execute("SELECT id FROM debts WHERE kind='Custody' AND status='Active'").fetchall()
    total = sum(reconstruct_debt_outstanding(con, r["id"]) for r in rows)
    return round(max(0.0, total), 2)


def total_receivable_outstanding(con: sqlite3.Connection) -> float:
    """Returns total active Receivable (Piutang) from event ledger."""
    rows = con.execute("SELECT id FROM debts WHERE kind='Receivable' AND status='Active'").fetchall()
    total = sum(reconstruct_debt_outstanding(con, r["id"]) for r in rows)
    return round(max(0.0, total), 2)


def total_payable_outstanding(con: sqlite3.Connection) -> float:
    """Returns total active Payable (Utang) from event ledger."""
    rows = con.execute("SELECT id FROM debts WHERE kind='Payable' AND status='Active'").fetchall()
    total = sum(reconstruct_debt_outstanding(con, r["id"]) for r in rows)
    return round(max(0.0, total), 2)


def current_pass_through_outstanding(con: sqlite3.Connection) -> float:
    """Retrieves current pass-through (Custody / Titipan) outstanding calculated from Event Ledger."""
    return total_custody_outstanding(con)


def get_payable_effective_commitments(con: sqlite3.Connection) -> tuple[float, float]:
    """Calculates U_eff (effective Payables) and X_eff (effective regular Upcomings).
       Formula:
       - For each Active Payable: U_eff = max(0, outstanding - active_protected_coverage_on_its_upcomings).
       - For each Upcoming without debt (debt_id IS NULL): X_eff = max(0, amount - active_coverage).
       - Upcoming with debt_id is excluded from X_eff to prevent double deduction.
    """
    # 1. Calculate U_eff for all Active Payables
    active_payables = con.execute("SELECT id FROM debts WHERE kind='Payable' AND status='Active'").fetchall()
    total_u_eff = 0.0
    for p in active_payables:
        pid = p["id"]
        out = reconstruct_debt_outstanding(con, pid)
        # Active protected coverage for upcomings linked to this Payable
        cov_row = con.execute(
            """SELECT COALESCE(SUM(pa.amount), 0.0)
               FROM protected_allocations pa
               JOIN upcoming u ON pa.covers_upcoming_id = u.id
               WHERE u.debt_id=? AND pa.status='Active' AND u.status='Upcoming'""",
            (pid,),
        ).fetchone()
        cov = float(cov_row[0] or 0.0)
        u_eff = max(0.0, out - cov)
        total_u_eff += u_eff

    # 2. Calculate X_eff for non-debt Upcomings
    u_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
    if "reserve_now" in u_cols:
        non_debt_upcomings = con.execute(
            """SELECT id, amount, status, COALESCE(reserve_now, 0) AS reserve_now
               FROM upcoming
               WHERE (debt_id IS NULL OR debt_id = 0)
                 AND status IN ('Upcoming', 'Confirmed', 'Tentative')"""
        ).fetchall()
    else:
        non_debt_upcomings = con.execute(
            """SELECT id, amount, status, 0 AS reserve_now
               FROM upcoming
               WHERE (debt_id IS NULL OR debt_id = 0)
                 AND status IN ('Upcoming', 'Confirmed')"""
        ).fetchall()

    cov_map = get_upcoming_active_coverage(con)
    assert isinstance(cov_map, dict)
    total_x_eff = 0.0
    for u in non_debt_upcomings:
        uid = u["id"]
        status = u["status"]
        reserve_now = int(u["reserve_now"] or 0)
        # Tentative obligations are only deducted if reserve_now == 1
        if status == "Tentative" and reserve_now == 0:
            continue
        amt = float(u["amount"] or 0.0)
        cov = cov_map.get(uid, 0.0)
        x_eff = max(0.0, amt - cov)
        total_x_eff += x_eff

    return round(total_u_eff, 2), round(total_x_eff, 2)


def current_commitments(con: sqlite3.Connection, through_date: str | None = None) -> float:
    """Sum of all upcoming commitments for STS calculation: U_eff (Payables) + X_eff (Regular Upcomings)."""
    u_eff, x_eff = get_payable_effective_commitments(con)
    return round(u_eff + x_eff, 2)


def list_debts(con: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    """Lists all debt/custody/receivable positions with their calculated outstanding and events count."""
    query = "SELECT * FROM debts"
    params: list = []
    if kind:
        query += " WHERE kind=?"
        params.append(kind)
    query += " ORDER BY CASE status WHEN 'Active' THEN 0 WHEN 'Settled' THEN 1 ELSE 2 END, updated_at DESC, id DESC"

    rows = con.execute(query, tuple(params)).fetchall()
    result = []
    for r in rows:
        d = rowdict(r)
        did = d["id"]
        d["outstanding"] = reconstruct_debt_outstanding(con, did)

        # Last event date
        last_ev = con.execute(
            "SELECT event_date, event_type, amount FROM debt_events WHERE debt_id=? ORDER BY event_date DESC, id DESC LIMIT 1",
            (did,),
        ).fetchone()
        d["last_event_date"] = last_ev["event_date"] if last_ev else ""
        d["last_event_type"] = last_ev["event_type"] if last_ev else ""

        # Linked upcomings for Payables
        if d["kind"] == "Payable":
            up_rows = con.execute(
                "SELECT id, title, amount, due_date, status FROM upcoming WHERE debt_id=? ORDER BY due_date",
                (did,),
            ).fetchall()
            d["linked_upcomings"] = [rowdict(u) for u in up_rows]
        else:
            d["linked_upcomings"] = []

        result.append(d)
    return result


def get_debt_detail(con: sqlite3.Connection, debt_id: int) -> dict | None:
    """Retrieves full details of a debt position including all its events and linked upcomings."""
    debt = con.execute("SELECT * FROM debts WHERE id=?", (debt_id,)).fetchone()
    if not debt:
        return None
    d = rowdict(debt)
    d["outstanding"] = reconstruct_debt_outstanding(con, debt_id)

    ev_rows = con.execute(
        """SELECT de.*, t.transaction_type, t.account_from, t.account_to
           FROM debt_events de
           LEFT JOIN transactions t ON de.transaction_id = t.id
           WHERE de.debt_id=?
           ORDER BY de.event_date DESC, de.id DESC""",
        (debt_id,),
    ).fetchall()
    d["events"] = [rowdict(e) for e in ev_rows]

    if d["kind"] == "Payable":
        up_rows = con.execute(
            "SELECT id, title, amount, due_date, status FROM upcoming WHERE debt_id=? ORDER BY due_date",
            (debt_id,),
        ).fetchall()
        d["linked_upcomings"] = [rowdict(u) for u in up_rows]
    else:
        d["linked_upcomings"] = []
    return d


def create_debt_position(
    con: sqlite3.Connection,
    person_name: str,
    kind: str,
    initial_amount: float,
    event_date: str,
    notes: str = "",
    account: str | None = None,
    is_cash: bool = False,
    operation_key: str | None = None,
) -> dict:
    """Creates a new third-party debt/custody/receivable position and its Opening event atomically."""
    if kind not in {"Custody", "Receivable", "Payable"}:
        raise ValueError(f"Jenis posisi tidak valid: {kind}")
    initial_amount = round(float(initial_amount), 2)
    if initial_amount <= 0:
        raise ValueError("Nominal posisi awal harus lebih besar dari nol")
    if not person_name.strip():
        raise ValueError("Nama pihak / orang wajib diisi")

    # Idempotency guard
    if operation_key:
        existing = con.execute("SELECT * FROM debt_events WHERE operation_key=?", (operation_key,)).fetchone()
        if existing:
            return {"status": "success", "idempotent": True, "debt_id": existing["debt_id"], "event_id": existing["id"], "transaction_id": existing["transaction_id"]}

    if is_cash:
        if not account or not account.strip():
            raise ValueError("Rekening wajib dipilih untuk pergerakan kas")
        acc = con.execute("SELECT kind, active FROM accounts WHERE name=?", (account,)).fetchone()
        if not acc or acc["kind"] != "Owned" or not acc["active"]:
            raise ValueError(f"Rekening '{account}' tidak valid atau tidak aktif")

    # Create debt record
    cur = con.execute(
        "INSERT INTO debts (person_name, kind, status, notes) VALUES (?, ?, 'Active', ?)",
        (person_name.strip(), kind, notes.strip()),
    )
    debt_id = cur.lastrowid

    tx_id = None
    if is_cash:
        # Determine transaction type and direction
        if kind == "Custody":  # Terima Titipan -> Cash in (Income), Third-party
            tx_type = "Income"
            acc_from, acc_to = "", account
            delta = initial_amount
            settlement_kind = "Custody Deposit"
            desc = f"Penerimaan Titipan dari {person_name}: {notes}".strip(": ")
        elif kind == "Receivable":  # Beri Pinjaman -> Cash out (Expense), Third-party
            tx_type = "Expense"
            acc_from, acc_to = account, ""
            delta = -initial_amount
            settlement_kind = "Receivable Loan"
            desc = f"Pemberian Pinjaman kepada {person_name}: {notes}".strip(": ")
        elif kind == "Payable":  # Terima Pinjaman Utang -> Cash in (Income), Third-party
            tx_type = "Income"
            acc_from, acc_to = "", account
            delta = initial_amount
            settlement_kind = "Payable Borrow"
            desc = f"Penerimaan Pinjaman dari {person_name}: {notes}".strip(": ")

        cur_tx = con.execute(
            """INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, category, for_with_whom, money_context, settlement_kind, status, budget_effect)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Other / Miscellaneous', 'Personal / Self', 'Third-party', ?, 'Confirmed', 0.0)""",
            (event_date, get_wib_now().strftime("%H:%M"), tx_type, initial_amount, acc_from, acc_to, desc, settlement_kind),
        )
        tx_id = cur_tx.lastrowid
        mutate_account_balance(con, account, delta, event_date)

    # Insert Opening event (effect = +1)
    cur_ev = con.execute(
        """INSERT INTO debt_events (debt_id, transaction_id, event_type, effect, amount, event_date, operation_key, notes)
           VALUES (?, ?, 'Opening', 1, ?, ?, ?, ?)""",
        (debt_id, tx_id, initial_amount, event_date, operation_key, notes.strip()),
    )
    event_id = cur_ev.lastrowid

    update_debt_status(con, debt_id)
    return {"status": "success", "debt_id": debt_id, "event_id": event_id, "transaction_id": tx_id, "outstanding": initial_amount}


def add_debt_event(
    con: sqlite3.Connection,
    debt_id: int,
    event_type: str,
    amount: float,
    event_date: str,
    notes: str = "",
    account: str | None = None,
    is_cash: bool = False,
    operation_key: str | None = None,
) -> dict:
    """Adds a mutation event (Increase, Settlement, WriteOff) to an existing debt position."""
    if event_type not in {"Increase", "Settlement", "WriteOff"}:
        raise ValueError(f"Jenis event tidak valid: {event_type}")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError("Nominal event harus lebih besar dari nol")

    debt = con.execute("SELECT * FROM debts WHERE id=?", (debt_id,)).fetchone()
    if not debt:
        raise ValueError(f"Posisi #{debt_id} tidak ditemukan")

    # Idempotency guard
    if operation_key:
        existing = con.execute("SELECT * FROM debt_events WHERE operation_key=?", (operation_key,)).fetchone()
        if existing:
            return {"status": "success", "idempotent": True, "debt_id": debt_id, "event_id": existing["id"], "transaction_id": existing["transaction_id"]}

    kind = debt["kind"]
    current_out = reconstruct_debt_outstanding(con, debt_id)

    if event_type == "WriteOff":
        if kind == "Custody":
            raise ValueError("Titipan tidak dapat dihapusbukukan (Write-Off dilarang untuk Titipan)")
        if is_cash:
            raise ValueError("Write-off / penghapusbukuan adalah aksi non-kas")
        if amount > (current_out + 0.005):
            raise ValueError(f"Nominal write-off ({amount}) melebihi sisa outstanding ({current_out})")
        effect = -1

    elif event_type == "Settlement":
        if amount > (current_out + 0.005):
            raise ValueError(f"Nominal pelunasan ({amount}) melebihi sisa outstanding ({current_out})")
        # Check Payable active coverage guard
        if kind == "Payable":
            rem_after = max(0.0, current_out - amount)
            cov_row = con.execute(
                """SELECT COALESCE(SUM(pa.amount), 0.0)
                   FROM protected_allocations pa
                   JOIN upcoming u ON pa.covers_upcoming_id = u.id
                   WHERE u.debt_id=? AND pa.status='Active' AND u.status='Upcoming'""",
                (debt_id,),
            ).fetchone()
            active_cov = float(cov_row[0] or 0.0)
            if rem_after < (active_cov - 0.005):
                raise ValueError(
                    f"Pelunasan membuat sisa utang ({rem_after}) lebih kecil dari Dana Dijaga aktif ({active_cov}). "
                    "Gunakan alur bayar jadwal Upcoming terkait atau lepas alokasi dana terlebih dahulu."
                )
        effect = -1

    elif event_type == "Increase":
        effect = 1

    tx_id = None
    if is_cash and event_type != "WriteOff":
        if not account or not account.strip():
            raise ValueError("Rekening wajib dipilih untuk pergerakan kas")
        acc = con.execute("SELECT kind, active FROM accounts WHERE name=?", (account,)).fetchone()
        if not acc or acc["kind"] != "Owned" or not acc["active"]:
            raise ValueError(f"Rekening '{account}' tidak valid atau tidak aktif")

        person = debt["person_name"]
        if event_type == "Settlement":
            if kind == "Custody":  # Kembalikan Titipan -> Cash out (Expense)
                tx_type = "Expense"
                acc_from, acc_to = account, ""
                delta = -amount
                settlement_kind = "Custody Return"
                desc = f"Pengembalian Titipan kepada {person}: {notes}".strip(": ")
            elif kind == "Receivable":  # Terima Pelunasan Piutang -> Cash in (Income)
                tx_type = "Income"
                acc_from, acc_to = "", account
                delta = amount
                settlement_kind = "Receivable Repayment"
                desc = f"Penerimaan Pelunasan Piutang dari {person}: {notes}".strip(": ")
            elif kind == "Payable":  # Bayar Pokok Utang -> Cash out (Expense)
                tx_type = "Expense"
                acc_from, acc_to = account, ""
                delta = -amount
                settlement_kind = "Payable Principal Settlement"
                desc = f"Pembayaran Pokok Utang kepada {person}: {notes}".strip(": ")
        else:  # Increase
            if kind == "Custody":
                tx_type = "Income"
                acc_from, acc_to = "", account
                delta = amount
                settlement_kind = "Custody Increase"
                desc = f"Penambahan Titipan dari {person}: {notes}".strip(": ")
            elif kind == "Receivable":
                tx_type = "Expense"
                acc_from, acc_to = account, ""
                delta = -amount
                settlement_kind = "Receivable Increase"
                desc = f"Penambahan Piutang kepada {person}: {notes}".strip(": ")
            elif kind == "Payable":
                tx_type = "Income"
                acc_from, acc_to = "", account
                delta = amount
                settlement_kind = "Payable Increase"
                desc = f"Penambahan Utang dari {person}: {notes}".strip(": ")

        cur_tx = con.execute(
            """INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, category, for_with_whom, money_context, settlement_kind, status, budget_effect)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'Other / Miscellaneous', 'Personal / Self', 'Third-party', ?, 'Confirmed', 0.0)""",
            (event_date, get_wib_now().strftime("%H:%M"), tx_type, amount, acc_from, acc_to, desc, settlement_kind),
        )
        tx_id = cur_tx.lastrowid
        mutate_account_balance(con, account, delta, event_date)

    cur_ev = con.execute(
        """INSERT INTO debt_events (debt_id, transaction_id, event_type, effect, amount, event_date, operation_key, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (debt_id, tx_id, event_type, effect, amount, event_date, operation_key, notes.strip()),
    )
    event_id = cur_ev.lastrowid

    new_status = update_debt_status(con, debt_id)
    new_out = reconstruct_debt_outstanding(con, debt_id)
    return {"status": "success", "debt_id": debt_id, "event_id": event_id, "transaction_id": tx_id, "outstanding": new_out, "debt_status": new_status}


def reverse_debt_event(con: sqlite3.Connection, event_id: int, notes: str = "", operation_key: str | None = None) -> dict:
    """Reverses an existing debt event by inserting a Correction event and reversing its cash transaction."""
    orig = con.execute("SELECT * FROM debt_events WHERE id=?", (event_id,)).fetchone()
    if not orig:
        raise ValueError(f"Event #{event_id} tidak ditemukan")

    if orig["event_type"] == "Correction":
        raise ValueError("Event koreksi/reversal tidak dapat direversal lagi")

    # Check if already reversed
    already = con.execute("SELECT id FROM debt_events WHERE reversal_of_id=?", (event_id,)).fetchone()
    if already:
        raise ValueError(f"Event #{event_id} sudah pernah direversal oleh event #{already['id']}")

    # Idempotency guard
    if operation_key:
        existing = con.execute("SELECT * FROM debt_events WHERE operation_key=?", (operation_key,)).fetchone()
        if existing:
            return {"status": "success", "idempotent": True, "debt_id": orig["debt_id"], "event_id": existing["id"]}

    debt_id = orig["debt_id"]
    rev_effect = -orig["effect"]
    rev_amount = float(orig["amount"])

    # Reverse transaction if exists
    tx_id = None
    if orig["transaction_id"]:
        orig_tx = con.execute("SELECT * FROM transactions WHERE id=?", (orig["transaction_id"],)).fetchone()
        if orig_tx:
            # Create opposite reversal transaction
            tx_type = "Expense" if orig_tx["transaction_type"] == "Income" else "Income"
            acc_from = orig_tx["account_to"]
            acc_to = orig_tx["account_from"]
            acc_to_mutate = orig_tx["account_to"] if orig_tx["account_to"] else orig_tx["account_from"]
            delta = -rev_amount if tx_type == "Expense" else rev_amount
            today_str = get_wib_today_str()

            cur_tx = con.execute(
                """INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to, description, category, for_with_whom, money_context, settlement_kind, status, budget_effect)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Other / Miscellaneous', 'Personal / Self', 'Third-party', 'Correction Reversal', 'Confirmed', 0.0)""",
                (today_str, get_wib_now().strftime("%H:%M"), tx_type, rev_amount, acc_from, acc_to, f"[Reversal Event #{event_id}] {orig_tx['description']}"),
            )
            tx_id = cur_tx.lastrowid
            if acc_to_mutate:
                mutate_account_balance(con, acc_to_mutate, delta, today_str)

    today_str = get_wib_today_str()
    cur_ev = con.execute(
        """INSERT INTO debt_events (debt_id, transaction_id, event_type, effect, amount, event_date, reversal_of_id, operation_key, notes)
           VALUES (?, ?, 'Correction', ?, ?, ?, ?, ?, ?)""",
        (debt_id, tx_id, rev_effect, rev_amount, today_str, event_id, operation_key, notes.strip() or f"Koreksi atas event #{event_id}"),
    )
    rev_event_id = cur_ev.lastrowid

    new_status = update_debt_status(con, debt_id)
    new_out = reconstruct_debt_outstanding(con, debt_id)
    return {"status": "success", "debt_id": debt_id, "event_id": rev_event_id, "outstanding": new_out, "debt_status": new_status}


def link_upcoming_to_debt(con: sqlite3.Connection, upcoming_id: int, debt_id: int) -> dict:
    """Links an Upcoming schedule to a Payable debt position."""
    u = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    if not u:
        raise ValueError(f"Kewajiban #{upcoming_id} tidak ditemukan")
    if u["status"] != "Upcoming":
        raise ValueError("Hanya kewajiban berstatus 'Upcoming' yang dapat ditautkan ke utang")

    debt = con.execute("SELECT * FROM debts WHERE id=?", (debt_id,)).fetchone()
    if not debt:
        raise ValueError(f"Posisi utang #{debt_id} tidak ditemukan")
    if debt["kind"] != "Payable":
        raise ValueError("Kewajiban jadwal hanya dapat ditautkan ke posisi Utang (Payable)")
    if debt["status"] != "Active":
        raise ValueError("Posisi Utang harus berstatus Active")

    # Validate that sum of active linked upcoming amounts does not exceed debt outstanding
    current_up_sum = float(con.execute(
        "SELECT COALESCE(SUM(amount), 0.0) FROM upcoming WHERE debt_id=? AND status='Upcoming' AND id != ?",
        (debt_id, upcoming_id),
    ).fetchone()[0] or 0.0)

    u_amt = float(u["amount"] or 0.0)
    debt_out = reconstruct_debt_outstanding(con, debt_id)
    if round(current_up_sum + u_amt, 2) > (debt_out + 0.005):
        raise ValueError(f"Total jadwal cicilan ({round(current_up_sum + u_amt, 2)}) melebihi sisa pokok utang ({debt_out})")

    con.execute("UPDATE upcoming SET debt_id=? WHERE id=?", (debt_id, upcoming_id))
    return {"status": "success", "upcoming_id": upcoming_id, "debt_id": debt_id}


def unlink_upcoming_from_debt(con: sqlite3.Connection, upcoming_id: int) -> dict:
    """Unlinks an Upcoming schedule from its debt position."""
    u = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    if not u:
        raise ValueError(f"Kewajiban #{upcoming_id} tidak ditemukan")
    con.execute("UPDATE upcoming SET debt_id=NULL WHERE id=?", (upcoming_id,))
    return {"status": "success", "upcoming_id": upcoming_id}


def pay_upcoming_atomic(
    con: sqlite3.Connection,
    upcoming_id: int,
    account: str | None = None,
    pay_date: str | None = None,
    operation_key: str | None = None,
) -> dict:
    """Atomically executes payment of an upcoming item (regular personal or third-party payable installment),
    mutates cash balance, creates settlement debt_event if payable, consumes linked protected allocations,
    and marks upcoming as Paid.
    """
    u = con.execute("SELECT * FROM upcoming WHERE id=?", (upcoming_id,)).fetchone()
    if not u:
        raise ValueError(f"Kewajiban #{upcoming_id} tidak ditemukan")
    if u["status"] == "Paid":
        return {"status": "already_paid", "alreadyPaid": True, "transaction_id": u["transaction_id"], "upcoming_id": upcoming_id}
    if u["status"] != "Upcoming":
        raise ValueError(f"Kewajiban berstatus '{u['status']}' tidak dapat dibayar")

    # Idempotency check on operation_key
    if operation_key:
        existing_ev = con.execute("SELECT * FROM debt_events WHERE operation_key=?", (operation_key,)).fetchone()
        if existing_ev:
            return {"status": "success", "idempotent": True, "transaction_id": existing_ev["transaction_id"], "upcoming_id": upcoming_id}

    acc_name = (account or "").strip()
    if not acc_name:
        acc_name = str(u["account"] or "").strip()
    if not acc_name:
        raise ValueError("Rekening pembayaran wajib dipilih")

    acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (acc_name,)).fetchone()
    if not acc_row:
        raise ValueError(f"Rekening '{acc_name}' tidak ditemukan")
    if not acc_row["active"]:
        raise ValueError(f"Rekening '{acc_name}' tidak aktif")
    if acc_row["kind"] != "Owned":
        raise ValueError(f"Rekening '{acc_name}' bukan rekening milik")

    p_date = (pay_date or "").strip()
    if not p_date:
        p_date = str(u["due_date"] or "").strip()
    if not p_date:
        p_date = get_wib_today_str()
    try:
        dt.date.fromisoformat(p_date)
    except Exception:
        raise ValueError("Tanggal pembayaran harus berformat YYYY-MM-DD")

    pay_amount = float(u["amount"])
    debt_id = u["debt_id"] if "debt_id" in u.keys() else None

    # Atomic execution
    if debt_id:
        # 1. Create Third-party Expense transaction
        cur = con.execute(
            """INSERT INTO transactions
               (date, time, transaction_type, amount, account_from, account_to, description,
                category, for_with_whom, money_context, status, confidence, budget_effect, subtype, settlement_kind, notes, manual_edited)
               VALUES (?, ?, 'Expense', ?, ?, '', ?, ?, ?, 'Third-party', 'Confirmed', 'high', 0.0, 'Payable Installment Payment', 'Payable Installment Settlement', ?, 1)""",
            (
                p_date,
                get_wib_now().strftime("%H:%M"),
                pay_amount,
                acc_name,
                u["title"],
                u["category"],
                u["for_with_whom"],
                u["notes"],
            ),
        )
        tx_id = cur.lastrowid
        mutate_account_balance(con, acc_name, -pay_amount, p_date)

        # 2. Insert Settlement debt event
        con.execute(
            """INSERT INTO debt_events (debt_id, transaction_id, event_type, effect, amount, event_date, operation_key, notes)
               VALUES (?, ?, 'Settlement', -1, ?, ?, ?, ?)""",
            (debt_id, tx_id, pay_amount, p_date, operation_key, f"Pembayaran cicilan #{upcoming_id}: {u['title']}"),
        )
        update_debt_status(con, debt_id)
    else:
        # 1. Create Regular Personal Expense transaction
        cur = con.execute(
            """INSERT INTO transactions
               (date, time, transaction_type, amount, account_from, account_to, description,
                category, for_with_whom, money_context, status, confidence, budget_effect, subtype, notes, manual_edited)
               VALUES (?, ?, 'Expense', ?, ?, '', ?, ?, ?, 'Personal', 'Confirmed', 'high', ?, 'Upcoming Payment', ?, 1)""",
            (
                p_date,
                get_wib_now().strftime("%H:%M"),
                pay_amount,
                acc_name,
                u["title"],
                u["category"],
                u["for_with_whom"],
                pay_amount,
                u["notes"],
            ),
        )
        tx_id = cur.lastrowid
        mutate_account_balance(con, acc_name, -pay_amount, p_date)

    # 3. Mark all Active linked protected allocations or allocation goals as Spent
    linked_allocs = con.execute(
        "SELECT id FROM protected_allocations WHERE covers_upcoming_id=? AND status='Active'",
        (upcoming_id,),
    ).fetchall()
    for la in linked_allocs:
        set_protected_allocation_status(con, la["id"], "Spent", is_upcoming_settlement=True)

    u_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
    if "linked_goal_id" in u_cols and u["linked_goal_id"]:
        try:
            spend_allocation_goal(con, u["linked_goal_id"], pay_amount, is_upcoming_settlement=True)
        except ValueError:
            pass

    # 4. Mark upcoming as Paid
    con.execute("UPDATE upcoming SET status='Paid', transaction_id=? WHERE id=?", (tx_id, upcoming_id))
    con.commit()
    return {"status": "success", "transaction_id": tx_id, "upcoming_id": upcoming_id}



def get_month_actual_by_category(con: sqlite3.Connection, month: str) -> dict[str, float]:
    """Calculates actual spending per category for a given month."""
    result: dict[str, float] = {}
    for r in personal_expense_rows(con, month):
        cat = normalized_category(r["description"], r["category"], r["subtype"])
        result[cat] = round(result.get(cat, 0.0) + effective_budget_spend(r), 2)
    return result


def budget_view(con: sqlite3.Connection, month: str) -> dict:
    """Prepares budget status table, category spending status, and funding gap metrics."""
    data = plan_data(con, month)
    plan = data["plan"]
    rows = data["budgets"]
    actual = get_month_actual_by_category(con, month)
    start, end = month_bounds(month)
    today = dt.date.today()
    if start <= today <= end:
        remaining_days = max(1, (end - today).days + 1)
    elif today < start:
        remaining_days = end.day
    else:
        remaining_days = 0

    view = []
    active_total = 0.0
    for b in rows:
        cat = b["category"]
        a = actual.get(cat, 0.0)
        current = float(b["current_budget"])
        active = bool(b["active"])
        if active:
            active_total += current
        if current <= 0 and a > 0:
            status = "Unplanned Spending"
        elif current > 0 and a > current:
            status = "Over"
        elif current > 0 and a / current >= 0.80:
            status = "Watch"
        else:
            status = "Safe"
        remaining = current - a
        safe_daily = (
            max(0.0, remaining / remaining_days)
            if remaining_days and cat in FREQUENT_SAFE_DAILY and active
            else None
        )
        view.append({**b, "actual": a, "remaining": remaining, "status_label": status, "safe_daily": safe_daily})

    total_spent = round(sum(actual.values()), 2)
    has_budget_plan = active_total > 0
    remaining_budget = round(active_total - total_spent, 2) if has_budget_plan else None
    upcoming = round(
        float(
            con.execute(
                "SELECT COALESCE(SUM(amount),0) FROM upcoming WHERE substr(due_date,1,7)=? AND status='Upcoming'",
                (month,),
            ).fetchone()[0]
        ),
        2,
    )
    free_after_upcoming = round(remaining_budget - upcoming, 2) if has_budget_plan else None
    expected_income = round(float(plan["guaranteed_income"]) + float(plan["expected_additional_income"]), 2)
    protected_savings_target = round(expected_income * float(plan["savings_rate"]), 2)
    spendable_after_savings = round(expected_income - protected_savings_target, 2)
    funding_gap = round(spendable_after_savings - active_total, 2)
    return {
        "plan": plan,
        "rows": view,
        "active_total": active_total,
        "spent": total_spent,
        "has_budget_plan": has_budget_plan,
        "remaining_budget": remaining_budget,
        "upcoming": upcoming,
        "free_after_upcoming": free_after_upcoming,
        "expected_income": expected_income,
        "protected_savings_target": protected_savings_target,
        "spendable_after_savings": spendable_after_savings,
        "funding_gap": funding_gap,
        "remaining_days": remaining_days,
    }


def dashboard(con: sqlite3.Connection, month: str | None = None) -> dict:
    """Prepares main dashboard payload including Live Safe-to-Spend KPI."""
    time_ctx = get_time_context(con)
    if not month or not valid_month(month):
        month = time_ctx["current_month"]

    b = budget_view(con, month)
    snap = balance_snapshot(con)
    pt = current_pass_through_outstanding(con)
    pt_historical = pass_through_historical_net(con)
    total_balance = snap["total_balance"]
    protected = snap["protected_savings"]

    # Calculate pending expenses from provisional neutral or review queue
    pending_expenses = round(
        float(
            con.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE is_deleted=0 AND status='Provisional Neutral' AND (transaction_type='Expense' OR (transaction_type='' AND amount > 0))"
            ).fetchone()[0]
        ),
        2,
    )

    # Micro-info metrics:
    today_dt = dt.date.today()
    in_7_days = (today_dt + dt.timedelta(days=7)).isoformat()
    upcoming_7_days_count = con.execute(
        "SELECT COUNT(*) FROM upcoming WHERE status='Upcoming' AND due_date<>'' AND due_date<=?", (in_7_days,)
    ).fetchone()[0]
    upcoming_7_days_amount = round(
        float(
            con.execute(
                "SELECT COALESCE(SUM(amount),0) FROM upcoming WHERE status='Upcoming' AND due_date<>'' AND due_date<=?", (in_7_days,)
            ).fetchone()[0]
        ),
        2,
    )

    last_rec = con.execute("SELECT MAX(date_created) FROM reconciliations").fetchone()
    last_rec_date = last_rec[0] if last_rec and last_rec[0] else "Belum Pernah"

    # Dana Tersedia Digunakan formula:
    # Aset Likuid - Dana Dijaga - Uang Titipan - Kewajiban Belum Dibayar - Estimasi Pengeluaran Pending
    dana_tersedia = None
    status_kondisi = "Belum Diatur"
    status_level = "info"
    live_commitments = current_commitments(con, time_ctx["today"])
    if snap["complete"]:
        dana_tersedia = round(float(total_balance or 0) - float(protected or 0) - max(0.0, pt) - live_commitments - pending_expenses, 2)
        sisa_anggaran = b.get("remaining_budget") or 0.0
        if dana_tersedia < 0:
            status_kondisi = "Defisit"
            status_level = "bad"
        elif dana_tersedia < sisa_anggaran:
            status_kondisi = "Perlu Perhatian"
            status_level = "warn"
        else:
            status_kondisi = "Terkendali"
            status_level = "good"

    txs = [
        rowdict(r)
        for r in con.execute(
            "SELECT * FROM transactions WHERE is_deleted=0 AND substr(date,1,7)=? ORDER BY date DESC, time DESC, id DESC LIMIT 250",
            (month,),
        ).fetchall()
    ]
    category_spend = sorted(
        ((k, v) for k, v in get_month_actual_by_category(con, month).items()),
        key=lambda x: x[1],
        reverse=True,
    )
    provisional_count = con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=0 AND status='Provisional Neutral'").fetchone()[0]
    provisional_gross = round(
        float(
            con.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE is_deleted=0 AND status='Provisional Neutral'").fetchone()[0]
        ),
        2,
    )
    research_total = round(
        float(
            con.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE is_deleted=0 AND transaction_type='Expense' AND money_context='Historical Research'"
            ).fetchone()[0]
        ),
        2,
    )

    insights = []
    if b["has_budget_plan"] and b["funding_gap"] < 0:
        insights.append({
            "level": "warn",
            "title": "Rencana belum terdanai penuh",
            "text": f"Setelah menyisihkan tabungan, rencana saat ini masih membutuhkan Rp {abs(round(b['funding_gap'])):,} pemasukan nyata tambahan. Kurangi anggaran atau tambahkan perkiraan pemasukan hanya jika memang benar-benar diharapkan.",
        })
    elif b["has_budget_plan"]:
        insights.append({
            "level": "good",
            "title": "Rencana sudah terdanai",
            "text": f"Pemasukan yang bisa dipakai setelah menyisihkan tabungan menutup rencana dan masih menyisakan ruang Rp {round(b['funding_gap']):,}.",
        })
    else:
        insights.append({
            "level": "info",
            "title": "Bulan Riwayat / Tanpa Anggaran Rencana",
            "text": f"Bulan {month} adalah riwayat transaksi tanpa batas anggaran rencana. Pengeluaran pribadi bulan ini adalah {idr(b['spent'])}.",
        })

    over = [r for r in b["rows"] if r["status_label"] in {"Over", "Unplanned Spending"} and r["actual"] > 0]
    if over and b["has_budget_plan"]:
        r = max(over, key=lambda x: x["actual"])
        insights.append({
            "level": "warn",
            "title": "Anggaran perlu perhatian",
            "text": f"{label_category(r['category'])} sudah terpakai Rp {round(r['actual']):,} dari anggaran Rp {round(r['current_budget']):,}. Money Tracks tidak akan memindahkan anggaran secara otomatis.",
        })
    if not snap["complete"]:
        insights.append({
            "level": "info",
            "title": "Uang Aman perlu saldo terkini",
            "text": "Perbarui saldo semua rekening milikmu. Sebelum itu, Money Tracks hanya menampilkan ruang anggaran dan tidak akan menebak Uang Aman untuk Dipakai.",
        })
    elif live_commitments > 0:
        insights.append({
            "level": "info",
            "title": "Kewajiban aktif",
            "text": f"Rp {round(live_commitments):,} disisihkan untuk kewajiban yang belum dibayar. Belum dianggap pengeluaran sampai dibayar, tetapi sudah mengurangi Uang Aman untuk Dipakai saat ini.",
        })
    balance_discrepancies = verify_balances(con)
    if balance_discrepancies:
        disc_names = [d["account_name"] for d in balance_discrepancies if d.get("status") == "discrepancy"]
        unv_names = [d["account_name"] for d in balance_discrepancies if d.get("status") == "unverifiable"]
        if disc_names:
            insights.insert(0, {
                "level": "warn",
                "title": "Perbedaan Saldo Terdeteksi",
                "text": f"Terdeteksi selisih antara mutasi transaksi dan saldo tercatat pada {', '.join(disc_names)}. Harap lakukan rekonsiliasi saldo.",
            })
        if unv_names:
            insights.insert(0, {
                "level": "warn",
                "title": "Saldo Belum Terverifikasi",
                "text": f"Akun {', '.join(unv_names)} belum memiliki snapshot anchor awal untuk verifikasi mutasi.",
            })
    insights = insights[:3]

    total_income = round(
        float(
            con.execute(
                """SELECT COALESCE(SUM(amount),0) FROM transactions
                   WHERE is_deleted=0 AND transaction_type='Income' AND money_context='Personal'
                     AND status IN ('Confirmed','Auto-classified') AND substr(date,1,7)=?
                     AND description NOT LIKE '[Rekonsiliasi]%'""",
                (month,),
            ).fetchone()[0]
        ),
        2,
    )

    alloc_summary = get_allocation_summary(con)
    rec_out = total_receivable_outstanding(con)

    u_cols = [r[1] for r in con.execute("PRAGMA table_info(upcoming)").fetchall()]
    tentative_cnt = 0
    tentative_res = 0.0
    if "reserve_now" in u_cols:
        tentative_cnt = con.execute("SELECT COUNT(*) FROM upcoming WHERE status='Tentative'").fetchone()[0]
        row_tres = con.execute("SELECT COALESCE(SUM(amount), 0.0) FROM upcoming WHERE status='Tentative' AND reserve_now=1").fetchone()
        tentative_res = round(float(row_tres[0] or 0.0), 2)

    return {
        "time_machine": time_ctx,
        "month": month,
        "months": available_months(con),
        "balance_discrepancies": balance_discrepancies,
        "kpis": {
            "totalBalance": total_balance,
            "balanceComplete": snap["complete"],
            "protectedSavings": protected,
            "emergencyAllocated": alloc_summary["emergency_allocated"],
            "goalsAllocated": alloc_summary["goals_allocated"],
            "generalAllocated": alloc_summary["general_allocated"],
            "reservedTotal": alloc_summary["total_allocated"],
            "passThroughOutstanding": pt,
            "passThroughHistoricalNet": pt_historical,
            "receivablesOutstanding": rec_out,
            "income": total_income,
            "spent": b["spent"],
            "hasBudgetPlan": b["has_budget_plan"],
            "remainingBudget": b["remaining_budget"],
            "freeAfterUpcoming": b["free_after_upcoming"],
            "safeToSpend": dana_tersedia,
            "statusKondisi": status_kondisi,
            "statusLevel": status_level,
            "upcoming7DaysCount": upcoming_7_days_count,
            "upcoming7DaysAmount": upcoming_7_days_amount,
            "lastRecDate": last_rec_date,
            "upcoming": b["upcoming"],
            "commitments": b["upcoming"],
            "currentCommitments": live_commitments,
            "effectiveConfirmedCommitments": live_commitments,
            "tentativeCount": tentative_cnt,
            "tentativeReserved": tentative_res,
            "pendingExpenses": pending_expenses,
            "budgetTotal": b["active_total"],
            "expectedIncome": b["expected_income"],
            "savingsTarget": b["protected_savings_target"],
            "fundingGap": b["funding_gap"],
            "provisionalCount": provisional_count,
            "provisionalGross": provisional_gross,
            "historicalResearchSpent": research_total,
        },
        "allocationSummary": alloc_summary,
        "budget": b,
        "categorySpend": [{"name": k, "amount": v} for k, v in category_spend],
        "transactions": txs,
        "insights": insights,
        "accounts": snap["accounts"],
        "categories": CATEGORIES,
        "forWithWhom": FOR_WITH_WHOM,
        "contexts": MONEY_CONTEXTS,
    }



def report_data(con: sqlite3.Connection, month: str | None = None) -> dict:
    """Prepares historical reports data (monthly, trimester, and yearly income vs expense vs research)."""
    months = sorted({r[0] for r in con.execute("SELECT DISTINCT substr(date,1,7) FROM transactions WHERE date<>'' AND is_deleted=0")})
    monthly = []
    trimester_dict: dict[str, dict[str, float]] = {}
    yearly_dict: dict[str, dict[str, float]] = {}

    for m in months:
        income = round(
            float(
                con.execute(
                    """SELECT COALESCE(SUM(amount),0) FROM transactions
                       WHERE substr(date,1,7)=? AND transaction_type='Income' AND money_context='Personal'
                         AND status<>'Provisional Neutral' AND is_deleted=0
                         AND description NOT LIKE '[Rekonsiliasi]%'""",
                    (m,),
                ).fetchone()[0]
            ),
            2,
        )
        expense = round(
            float(
                con.execute(
                    """SELECT COALESCE(SUM(budget_effect),0) FROM transactions
                       WHERE substr(date,1,7)=? AND transaction_type='Expense' AND money_context='Personal'
                         AND status<>'Provisional Neutral' AND is_deleted=0
                         AND description NOT LIKE '[Rekonsiliasi]%'""",
                    (m,),
                ).fetchone()[0]
            ),
            2,
        )
        research = round(
            float(
                con.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE substr(date,1,7)=? AND transaction_type='Expense' AND money_context='Historical Research' AND is_deleted=0",
                    (m,),
                ).fetchone()[0]
            ),
            2,
        )
        provisional = round(
            float(
                con.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE substr(date,1,7)=? AND status='Provisional Neutral' AND is_deleted=0",
                    (m,),
                ).fetchone()[0]
            ),
            2,
        )
        monthly.append({
            "month": m,
            "income": income,
            "expense": expense,
            "surplus": round(income - expense, 2),
            "research": research,
            "provisional": provisional,
        })

        # Trimester / Caturwulan aggregation (T1: Jan-Apr, T2: May-Aug, T3: Sep-Dec)
        yr, mo = m[:4], int(m[5:7])
        t_idx = (mo - 1) // 4 + 1
        t_names = {1: "Jan–Apr", 2: "Mei–Agt", 3: "Sep–Des"}
        t_key = f"{yr} T{t_idx} ({t_names[t_idx]})"

        if t_key not in trimester_dict:
            trimester_dict[t_key] = {"income": 0.0, "expense": 0.0, "research": 0.0, "provisional": 0.0}
        trimester_dict[t_key]["income"] = round(trimester_dict[t_key]["income"] + income, 2)
        trimester_dict[t_key]["expense"] = round(trimester_dict[t_key]["expense"] + expense, 2)
        trimester_dict[t_key]["research"] = round(trimester_dict[t_key]["research"] + research, 2)
        trimester_dict[t_key]["provisional"] = round(trimester_dict[t_key]["provisional"] + provisional, 2)

        # Yearly aggregation
        if yr not in yearly_dict:
            yearly_dict[yr] = {"income": 0.0, "expense": 0.0, "research": 0.0, "provisional": 0.0}
        yearly_dict[yr]["income"] = round(yearly_dict[yr]["income"] + income, 2)
        yearly_dict[yr]["expense"] = round(yearly_dict[yr]["expense"] + expense, 2)
        yearly_dict[yr]["research"] = round(yearly_dict[yr]["research"] + research, 2)
        yearly_dict[yr]["provisional"] = round(yearly_dict[yr]["provisional"] + provisional, 2)

    trimester = [
        {"period": k, "income": v["income"], "expense": v["expense"], "surplus": round(v["income"] - v["expense"], 2), "research": v["research"], "provisional": v["provisional"]}
        for k, v in trimester_dict.items()
    ]
    yearly = [
        {"period": k, "income": v["income"], "expense": v["expense"], "surplus": round(v["income"] - v["expense"], 2), "research": v["research"], "provisional": v["provisional"]}
        for k, v in yearly_dict.items()
    ]

    cats: dict[str, float] = {}
    for r in personal_expense_rows(con, month):
        cat = normalized_category(r["description"], r["category"], r["subtype"])
        cats[cat] = round(cats.get(cat, 0.0) + float(r["budget_effect"]), 2)

    daily_trend = []
    if month and valid_month(month):
        start, end = month_bounds(month)
        days_in_m = end.day
        daily_sums: dict[int, float] = {d: 0.0 for d in range(1, days_in_m + 1)}
        for r in personal_expense_rows(con, month):
            try:
                day_num = int(r["date"][8:10])
                daily_sums[day_num] = round(daily_sums.get(day_num, 0.0) + float(r["budget_effect"]), 2)
            except Exception:
                pass
        daily_trend = [{"day": d, "amount": daily_sums[d]} for d in range(1, days_in_m + 1)]

    return {
        "monthly": monthly,
        "trimester": trimester,
        "yearly": yearly,
        "daily_trend": daily_trend,
        "categories": sorted(
            ({"name": k, "amount": v} for k, v in cats.items()),
            key=lambda x: x["amount"],
            reverse=True,
        ),
    }


def statement_data(con: sqlite3.Connection, month: str) -> dict:
    """Prepares structured E-Statement data for report generation."""
    if not valid_month(month):
        raise ValueError("Bulan tidak valid")
    start, end = month_bounds(month)
    tx = [
        rowdict(r)
        for r in con.execute(
            "SELECT * FROM transactions WHERE substr(date,1,7)=? AND is_deleted=0 ORDER BY date, time, id",
            (month,),
        ).fetchall()
    ]
    income = round(
        sum(
            float(r["amount"])
            for r in tx
            if r["transaction_type"] == "Income" and r["money_context"] == "Personal" and r["status"] != "Provisional Neutral"
        ),
        2,
    )
    expense = round(
        sum(
            float(r["budget_effect"])
            for r in tx
            if r["transaction_type"] == "Expense" and r["money_context"] == "Personal" and r["status"] != "Provisional Neutral"
        ),
        2,
    )
    research = round(
        sum(
            float(r["amount"])
            for r in tx
            if r["transaction_type"] == "Expense" and r["money_context"] == "Historical Research"
        ),
        2,
    )
    provisional = round(sum(float(r["amount"]) for r in tx if r["status"] == "Provisional Neutral"), 2)
    transfer_gross = round(
        sum(
            float(r["amount"])
            for r in tx
            if r["transaction_type"] == "Transfer" and r["status"] != "Provisional Neutral"
        ),
        2,
    )

    owned_names = {r[0] for r in con.execute("SELECT name FROM accounts WHERE kind='Owned'")}
    acc = {}
    for r in tx:
        a = float(r["amount"])
        src = r["account_from"] or ""
        dst = r["account_to"] or ""
        if src in owned_names:
            d = acc.setdefault(src, {"name": src, "in": 0.0, "out": 0.0, "count": 0})
            d["out"] += a
            d["count"] += 1
        if dst in owned_names and dst != src:
            d = acc.setdefault(dst, {"name": dst, "in": 0.0, "out": 0.0, "count": 0})
            d["in"] += a
            d["count"] += 1
    accounts = sorted(acc.values(), key=lambda x: x["in"] + x["out"], reverse=True)
    coverage_end = max((r["date"] for r in tx), default=start.isoformat())
    return {
        "month": month,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "coverage_end": coverage_end,
        "income": income,
        "expense": expense,
        "surplus": income - expense,
        "research": research,
        "provisional": provisional,
        "transfer_gross": transfer_gross,
        "transactions": tx,
        "accounts": accounts,
    }


def validate_tx(raw: dict) -> dict:
    """Validates and cleans raw transaction input payload."""
    date = str(raw.get("date", "")).strip()
    try:
        dt.date.fromisoformat(date)
    except Exception:
        raise ValueError("Date must use YYYY-MM-DD")
    typ = str(raw.get("transaction_type", "Expense")).strip().title()
    if typ not in {"Income", "Expense", "Transfer"}:
        raise ValueError("Type must be Income, Expense, or Transfer")
    try:
        amt = abs(float(raw.get("amount", 0)))
    except Exception:
        raise ValueError("Amount must be numeric")
    if amt <= 0:
        raise ValueError("Amount must be greater than 0")
    context = str(raw.get("money_context", "Personal")).strip()
    if context == "Historical Research":
        raise ValueError("Konteks 'Historical Research' hanya untuk arsip data historis dan tidak dapat dibuat secara manual")
    if context not in {"Personal", "Pass-through", "Third-party"}:
        raise ValueError("Invalid Money Context")
    status = str(raw.get("status", "Confirmed")).strip()
    if status not in STATUSES:
        status = "Confirmed"
    cat = str(raw.get("category", "")).strip()
    if typ != "Expense":
        cat = cat or ""
    elif not cat:
        cat = "Other / Miscellaneous"
    rule_ver = str(raw.get("budget_rule_version", "")).strip()
    if rule_ver != "legacy":
        rule_ver = "derived"

    excluded = 1 if bool(raw.get("exclude_from_budget")) else 0
    exclusion_reason = str(raw.get("budget_exclusion_reason", "")).strip()

    if rule_ver == "legacy":
        effect = raw.get("budget_effect")
        if effect is None or effect == "":
            effect = amt if typ == "Expense" and context == "Personal" and status != "Provisional Neutral" else 0.0
        else:
            effect = float(effect)
    else:
        # Derived: ignore any custom client budget_effect
        if excluded == 1:
            if not exclusion_reason:
                raise ValueError("Alasan pengecualian anggaran (budget_exclusion_reason) wajib diisi saat transaksi dikecualikan dari anggaran")
            effect = 0.0
        else:
            exclusion_reason = ""
            effect = amt if typ == "Expense" and context == "Personal" and status in ("Confirmed", "Auto-classified") else 0.0

    afrom = str(raw.get("account_from", "")).strip()
    ato = str(raw.get("account_to", "")).strip()
    if typ == "Expense" and not afrom:
        raise ValueError("Pengeluaran wajib memiliki rekening asal (account_from)")
    if typ == "Income" and not ato:
        raise ValueError("Pemasukan wajib memiliki rekening tujuan (account_to)")
    if typ == "Transfer":
        if not afrom:
            raise ValueError("Transfer wajib memiliki rekening asal (account_from)")
        if not ato:
            raise ValueError("Transfer wajib memiliki rekening tujuan (account_to)")
        if afrom == ato:
            raise ValueError("Rekening asal dan tujuan transfer tidak boleh sama")

    return {
        "canonical_id": str(raw.get("canonical_id", "")).strip() or None,
        "date": date,
        "time": str(raw.get("time", "")).strip(),
        "transaction_type": typ,
        "amount": amt,
        "account_from": afrom,
        "account_to": ato,
        "description": str(raw.get("description", "")).strip() or "Unspecified",
        "category": cat,
        "for_with_whom": str(raw.get("for_with_whom", "Personal / Self")).strip() or "Personal / Self",
        "money_context": context,
        "settlement_kind": str(raw.get("settlement_kind", "")).strip(),
        "status": status,
        "confidence": str(raw.get("confidence", "high")).strip() or "high",
        "budget_effect": effect,
        "exclude_from_budget": excluded,
        "budget_exclusion_reason": exclusion_reason,
        "budget_rule_version": rule_ver,
        "subtype": str(raw.get("subtype", "")).strip(),
        "source_refs": str(raw.get("source_refs", "")).strip(),
        "notes": str(raw.get("notes", "")).strip(),
    }


def check_update_manifest() -> dict:
    """Queries online manifest URL for remote update availability."""
    source = load_update_source()
    url = source.get("manifest_url", "").strip()
    result = {
        "current_version": VERSION,
        "manifest_url": url,
        "auto_check": bool(source.get("auto_check", True)),
        "configured": bool(url),
        "update_available": False,
    }
    if not url:
        result["message"] = "Sumber pembaruan belum dihubungkan. Aplikasi tetap bisa dipakai normal."
        return result
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MoneyTracks/11"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
        latest = str(manifest.get("version", "")).strip()
        result.update({
            "latest_version": latest,
            "zip_url": manifest.get("zip_url", ""),
            "sha256": manifest.get("sha256", ""),
            "notes": manifest.get("notes", ""),
        })

        def parts(v):
            out = [int(x) for x in re.findall(r"\d+", v)]
            return tuple((out + [0, 0, 0])[:3])

        result["update_available"] = bool(latest) and parts(latest) > parts(VERSION)
        result["message"] = (
            "Pembaruan tersedia. Tutup lalu buka ulang Money Tracks agar launcher memasangnya."
            if result["update_available"]
            else "Money Tracks sudah versi terbaru."
        )
    except Exception as e:
        result["message"] = f"Belum bisa mengecek pembaruan: {e}"
        result["error"] = str(e)
    return result


def idr(value: float | int | None) -> str:
    """Formats numeric value to normalized Indonesian Rupiah currency string."""
    if value is None:
        return "—"
    v = float(value)
    sign = "-" if v < 0 else ""
    abs_v = abs(v)
    has_dec = abs(round(abs_v) - abs_v) >= 0.005
    if has_dec:
        raw = f"{abs_v:,.2f}"
    else:
        raw = f"{int(abs_v):,}"
    return (sign + "Rp" + raw).replace(",", "X").replace(".", ",").replace("X", ".")


def label_category(value: str) -> str:
    """Maps internal category key to Bahasa Indonesia label."""
    return {
        "Main Meals": "Makan Utama", "Snacks": "Camilan", "Cafe & Drinks": "Kafe & Minuman",
        "Groceries & Daily Needs": "Belanja Harian", "Fuel": "Bensin", "Parking/Toll": "Parkir / Tol",
        "Transport / Other Transport": "Transportasi Lain", "Vehicle Service": "Servis Kendaraan",
        "Fashion": "Fashion", "Electronics": "Elektronik", "Personal Care": "Perawatan Diri",
        "Health": "Kesehatan", "Education": "Pendidikan", "Campus & Organization": "Kampus & Organisasi",
        "Phone & Internet": "Pulsa & Internet", "Subscriptions": "Langganan", "Gifts & Giving": "Hadiah & Pemberian",
        "Travel": "Perjalanan", "Other / Miscellaneous": "Lain-lain", "Research — Historical Only": "Riwayat Dana Riset",
    }.get(value or "", value or "-")


def label_context(value: str) -> str:
    """Maps money context key to Bahasa Indonesia label."""
    return {
        "Personal": "Pribadi",
        "Pass-through": "Uang Titipan / Uang Lewat",
        "Historical Research": "Riwayat Dana Riset",
    }.get(value or "", value or "-")


def label_status(value: str) -> str:
    """Maps status key to Bahasa Indonesia label."""
    return {
        "Confirmed": "Terkonfirmasi",
        "Auto-classified": "Diklasifikasikan Otomatis",
        "Provisional Neutral": "Belum Jelas - Tidak Dihitung",
    }.get(value or "", value or "-")


def build_ai_financial_context(con: sqlite3.Connection, month: str | None = None) -> dict:
    """Builds a rich, concise financial context object for the AI Financial Advisor."""
    dash = dashboard(con, month)
    k = dash.get("kpis", {})
    t_ctx = dash.get("time_machine", {})

    cats = dash.get("categorySpend", [])[:5]
    top_cats_str = ", ".join([f"{label_category(c['name'])}: {idr(c['amount'])}" for c in cats]) or "Belum ada pengeluaran"

    allocs = protected_allocations(con)
    active_allocs_str = ", ".join([f"{a['title']} ({idr(a['amount'])})" for a in allocs if a.get("status") == "Active"]) or "Tidak ada"

    upcoming_rows = [rowdict(r) for r in con.execute("SELECT * FROM upcoming WHERE status='Upcoming' ORDER BY due_date, due_time LIMIT 3").fetchall()]
    upcoming_str = ", ".join([f"{u['title']} ({idr(u['amount'])} due {u['due_date']})" for u in upcoming_rows]) or "Tidak ada tagihan 7 hr"

    active_year = (month or dash.get("month", "2026-08"))[:4]
    ytd_spent = float(con.execute(
        "SELECT COALESCE(SUM(budget_effect), 0) FROM transactions WHERE date LIKE ? AND transaction_type='Expense' AND money_context='Personal' AND status<>'Provisional Neutral' AND is_deleted=0",
        (f"{active_year}%",)
    ).fetchone()[0])
    ytd_income = float(con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE date LIKE ? AND transaction_type='Income' AND money_context='Personal' AND status<>'Provisional Neutral' AND is_deleted=0",
        (f"{active_year}%",)
    ).fetchone()[0])

    return {
        "current_wib": t_ctx.get("formatted_wib"),
        "active_month": dash.get("month"),
        "active_year": active_year,
        "liquid_assets": idr(k.get("totalBalance")),
        "protected_savings": idr(k.get("protectedSavings")),
        "pass_through": idr(k.get("passThroughOutstanding")),
        "safe_to_spend": idr(k.get("safeToSpend")),
        "status_kondisi": k.get("statusKondisi"),
        "monthly_income": idr(k.get("income")),
        "monthly_spent": idr(k.get("spent")),
        "remaining_budget": idr(k.get("remainingBudget")),
        "active_commitments": idr(k.get("currentCommitments")),
        "top_categories": top_cats_str,
        "active_allocations": active_allocs_str,
        "upcoming_commitments": upcoming_str,
        "ytd_spent": idr(ytd_spent),
        "ytd_income": idr(ytd_income),
        "ytd_surplus": idr(ytd_income - ytd_spent),
    }


def process_ai_chat_message(con: sqlite3.Connection, user_message: str, month: str | None = None) -> dict:
    """Processes user live chat prompt through the Dual-Engine AI Router."""
    msg = user_message.strip()
    if not msg:
        raise ValueError("Pesan tidak boleh kosong")

    from ai_router import route_ai_request
    return route_ai_request(con, msg, month)


def normalize_counterparty(desc: str) -> str:
    """Deterministic cleaner for transaction descriptions to identify recurrent counterparties / merchants."""
    if not desc:
        return ""
    d = desc.lower().strip()
    # Remove transfer/payment action verbs at the beginning
    d = re.sub(r"^(transfer ke|transfer dari|top up|bayar|beli|pembayaran|terima|pemberian)\s+", "", d)
    # Remove dates and timestamps (e.g. 22/08, 2026-08-22, 13:37:00)
    d = re.sub(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", "", d)
    d = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", d)
    d = re.sub(r"\b\d{1,2}[:.]\d{2}([:.]\d{2})?\b", "", d)
    # Remove reference numbers, IDs, account suffixes
    d = re.sub(r"\b(trx|ref|id|rek|va|no|bca|gopay|spay|shopee|ovo|dana)?\s*#?\d{5,}\b", "", d)
    # Remove nominal strings inside description
    d = re.sub(r"rp\s*[\d.,]+", "", d)
    d = re.sub(r"\[.*?\]", "", d)
    d = re.sub(r"[^a-z0-9\s]", " ", d)
    tokens = [t for t in d.split() if len(t) > 1 and not t.isdigit()]
    return " ".join(tokens)


def suggest_transaction_draft(
    con: sqlite3.Connection,
    description: str,
    transaction_type: str = "Expense",
    draft_date: str | None = None,
    min_support: int = 3,
    min_confidence: float = 0.80,
) -> dict:
    """Read-only deterministic rule-based suggestion service for category and accounts."""
    if not description or not description.strip():
        return {"status": "ok", "suggestions": {}}

    norm_key = normalize_counterparty(description)
    if not norm_key or len(norm_key) < 2:
        return {"status": "ok", "suggestions": {}}

    # Validate transaction type
    if transaction_type not in {"Expense", "Income", "Transfer"}:
        transaction_type = "Expense"

    # Default draft date to now if not supplied
    if not draft_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", draft_date):
        draft_date = get_wib_now().strftime("%Y-%m-%d")

    # Fetch active owned accounts for validation
    active_accounts = {
        r[0] for r in con.execute("SELECT name FROM accounts WHERE kind='Owned' AND active=1").fetchall()
    }

    # Fetch eligible history strictly prior to or on draft_date
    # Exclude: deleted, provisional, Adjustment, Historical Research, Pass-through, Third-party, reversals
    query = """
        SELECT description, category, account_from, account_to, transaction_type
        FROM transactions
        WHERE is_deleted=0
          AND status IN ('Confirmed', 'Auto-classified')
          AND money_context='Personal'
          AND transaction_type=?
          AND transaction_type<>'Adjustment'
          AND (description NOT LIKE '%[Rekonsiliasi]%' AND description NOT LIKE '%[Penyesuaian]%')
          AND (reversal_of_id IS NULL OR reversal_of_id=0)
          AND date <= ?
    """
    rows = con.execute(query, (transaction_type, draft_date)).fetchall()

    cat_counts = Counter()
    acc_from_counts = Counter()
    acc_to_counts = Counter()
    relevant_support = 0

    for r in rows:
        r_desc = r[0] if isinstance(r, (tuple, list)) else r["description"]
        r_cat = r[1] if isinstance(r, (tuple, list)) else r["category"]
        r_af = r[2] if isinstance(r, (tuple, list)) else r["account_from"]
        r_at = r[3] if isinstance(r, (tuple, list)) else r["account_to"]

        r_key = normalize_counterparty(r_desc)
        if r_key == norm_key:
            relevant_support += 1
            if r_cat:
                cat_counts[r_cat] += 1
            if r_af:
                acc_from_counts[r_af] += 1
            if r_at:
                acc_to_counts[r_at] += 1

    if relevant_support < min_support:
        return {"status": "ok", "suggestions": {}}

    suggestions = {}

    # 1. Category (only for Expense and Income)
    if transaction_type in {"Expense", "Income"} and cat_counts:
        top_cats = cat_counts.most_common(2)
        top_cat, top_cnt = top_cats[0]
        is_tie = len(top_cats) > 1 and top_cats[0][1] == top_cats[1][1]
        conf = top_cnt / relevant_support
        if not is_tie and conf >= min_confidence and top_cat in CATEGORIES:
            suggestions["category"] = {
                "value": top_cat,
                "label": label_category(top_cat),
                "confidence": round(conf, 2),
                "support": relevant_support,
                "reason": f"Cocok pada {top_cnt} dari {relevant_support} transaksi serupa.",
            }

    # 2. Account From (for Expense and Transfer)
    if transaction_type in {"Expense", "Transfer"} and acc_from_counts:
        top_froms = acc_from_counts.most_common(2)
        top_af, top_cnt = top_froms[0]
        is_tie = len(top_froms) > 1 and top_froms[0][1] == top_froms[1][1]
        conf = top_cnt / relevant_support
        if not is_tie and conf >= min_confidence and top_af in active_accounts:
            suggestions["account_from"] = {
                "value": top_af,
                "confidence": round(conf, 2),
                "support": relevant_support,
                "reason": f"Dipakai pada {top_cnt} dari {relevant_support} transaksi serupa.",
            }

    # 3. Account To (for Income and Transfer)
    if transaction_type in {"Income", "Transfer"} and acc_to_counts:
        top_tos = acc_to_counts.most_common(2)
        top_at, top_cnt = top_tos[0]
        is_tie = len(top_tos) > 1 and top_tos[0][1] == top_tos[1][1]
        conf = top_cnt / relevant_support
        if not is_tie and conf >= min_confidence and top_at in active_accounts:
            suggested_from = suggestions.get("account_from", {}).get("value")
            if transaction_type != "Transfer" or top_at != suggested_from:
                suggestions["account_to"] = {
                    "value": top_at,
                    "confidence": round(conf, 2),
                    "support": relevant_support,
                    "reason": f"Dipakai pada {top_cnt} dari {relevant_support} transaksi serupa.",
                }

    return {"status": "ok", "suggestions": suggestions}


def detect_recurring_patterns(
    con: sqlite3.Connection,
    as_of_date: str | None = None,
    min_support: int = 3,
) -> list[dict]:
    """Detects recurring transaction patterns from historical personal data (read-only, deterministic)."""
    target_date = as_of_date or get_wib_today_str()
    as_of_d = dt.date.fromisoformat(target_date)

    query = """
        SELECT id, date, transaction_type, amount, account_from, account_to, description, category, subtype, notes
        FROM transactions
        WHERE is_deleted = 0
          AND money_context = 'Personal'
          AND status IN ('Confirmed', 'Auto-classified')
          AND transaction_type IN ('Income', 'Expense')
          AND reversal_of_id IS NULL
          AND description NOT LIKE '%[Rekonsiliasi]%'
          AND date <= ?
        ORDER BY date ASC, id ASC
    """
    rows = con.execute(query, (target_date,)).fetchall()

    # Existing upcoming commitments for anti-duplication
    up_rows = con.execute("SELECT id, title, amount, account, category, status FROM upcoming WHERE status IN ('Upcoming', 'Confirmed', 'Tentative')").fetchall()
    up_normalized = [
        (u, normalize_counterparty(u["title"]) or "", u["title"].lower())
        for u in up_rows
    ]

    groups = {}
    for r in rows:
        norm_merchant = normalize_counterparty(r["description"])
        if not norm_merchant:
            norm_merchant = r["description"].lower().strip()
        acc = r["account_from"] if r["transaction_type"] == "Expense" else r["account_to"]
        key = (norm_merchant, r["transaction_type"], r["category"], acc)
        if key not in groups:
            groups[key] = []
        groups[key].append(r)

    patterns = []
    for (norm_merchant, tx_type, category, acc), txs in groups.items():
        support = len(txs)
        if support < 2:
            continue

        dates = [dt.date.fromisoformat(t["date"]) for t in txs]
        amounts = [float(t["amount"]) for t in txs]

        # Intervals between consecutive transactions
        intervals = [(dates[i] - dates[i-1]).days for i in range(1, len(dates))]
        if not intervals:
            continue

        med_interval = statistics.median(intervals)
        int_mean = statistics.mean(intervals) if intervals else 0
        int_stdev = statistics.stdev(intervals) if len(intervals) > 1 else 0
        int_cv = (int_stdev / int_mean) if int_mean > 0 else 0

        med_amt = round(statistics.median(amounts), 2)
        amt_mean = statistics.mean(amounts) if amounts else 0
        amt_stdev = statistics.stdev(amounts) if len(amounts) > 1 else 0
        amt_cv = (amt_stdev / amt_mean) if amt_mean > 0 else 0

        last_d = dates[-1]
        step = max(int(round(med_interval)), 1)
        next_d = last_d + dt.timedelta(days=step)
        while next_d < as_of_d:
            next_d += dt.timedelta(days=step)

        raw_title = txs[-1]["description"]
        raw_title_lower = raw_title.lower()

        # Classification
        if support >= min_support and amt_cv <= 0.10 and int_cv <= 0.40:
            classification = "Langganan / Tagihan"
            confidence = "high"
            reason = f"Terdeteksi {support}x dengan nominal sangat stabil (median {idr(med_amt)}) setiap ~{step} hari."
        elif support >= min_support:
            classification = "Pola Belanja"
            confidence = "medium"
            reason = f"Pola belanja berulang {support}x (median nominal {idr(med_amt)}, variasi ±{int(amt_cv*100)}%, interval ~{step} hari)."
        else:
            classification = "Abstain"
            confidence = "low"
            reason = f"Bukti transaksi belum cukup ({support} kali, minimal {min_support} kali)."

        # Anti-duplication matching against existing upcoming
        matched_up = None
        for u, u_norm, u_title_lower in up_normalized:
            if (u_norm and u_norm in norm_merchant) or (norm_merchant and norm_merchant in u_norm) or (u_title_lower in raw_title_lower):
                matched_up = u
                break

        patterns.append({
            "normalized_merchant": norm_merchant,
            "title": raw_title,
            "transaction_type": tx_type,
            "category": category,
            "account": acc,
            "support": support,
            "median_interval_days": step,
            "interval_cv": round(int_cv, 2),
            "median_amount": med_amt,
            "amount_cv": round(amt_cv, 2),
            "last_date": last_d.isoformat(),
            "predicted_next_date": next_d.isoformat(),
            "classification": classification,
            "confidence": confidence,
            "reason": reason,
            "has_existing_upcoming": matched_up is not None,
            "matched_upcoming_id": matched_up["id"] if matched_up else None,
            "matched_upcoming_title": matched_up["title"] if matched_up else None,
        })

    conf_order = {"high": 0, "medium": 1, "low": 2}
    patterns.sort(key=lambda p: (conf_order.get(p["confidence"], 3), -p["support"], p["predicted_next_date"]))
    return patterns


def cashflow_forecast(
    con: sqlite3.Connection,
    as_of_date: str | None = None,
    horizon_days: int = 30,
) -> dict:
    """Computes deterministic cashflow forecast for 7 or 30 days horizon."""
    target_date = as_of_date or get_wib_today_str()
    as_of_d = dt.date.fromisoformat(target_date)
    end_d = as_of_d + dt.timedelta(days=horizon_days)
    end_date_str = end_d.isoformat()

    cur_accs = con.execute("SELECT SUM(current_balance) FROM accounts WHERE active=1 AND kind='Owned' AND current_balance IS NOT NULL").fetchone()[0] or 0.0
    opening_balance = round(float(cur_accs), 2)
    em_fund = con.execute("SELECT COALESCE(SUM(allocated_amount), 0) FROM allocation_goals WHERE kind='Emergency' AND status='Active'").fetchone()[0] or 0.0
    emergency_fund = round(float(em_fund), 2)

    # 1. Confirmed Proyeksi Pasti:
    # upcoming with status IN ('Upcoming', 'Confirmed') strictly within horizon
    up_rows = con.execute("""
        SELECT id, due_date, due_time, title, amount, category, account, status, reserve_now
        FROM upcoming
        WHERE status IN ('Upcoming', 'Confirmed')
          AND due_date >= ? AND due_date <= ?
        ORDER BY due_date ASC, id ASC
    """, (target_date, end_date_str)).fetchall()

    confirmed_events = []
    confirmed_outflow = 0.0
    confirmed_inflow = 0.0

    for u in up_rows:
        amt = round(float(u["amount"]), 2)
        confirmed_outflow += amt
        confirmed_events.append({
            "date": u["due_date"],
            "title": u["title"],
            "amount": amt,
            "type": "outflow",
            "source": "upcoming_confirmed",
            "account": u["account"],
            "category": u["category"],
        })

    # Trace confirmed balance curve
    cur_bal_conf = opening_balance
    lowest_conf = cur_bal_conf
    for ev in sorted(confirmed_events, key=lambda x: x["date"]):
        if ev["type"] == "outflow":
            cur_bal_conf -= ev["amount"]
        else:
            cur_bal_conf += ev["amount"]
        if cur_bal_conf < lowest_conf:
            lowest_conf = cur_bal_conf

    confirmed_proj_balance = round(cur_bal_conf, 2)
    lowest_conf = round(lowest_conf, 2)

    # 2. Estimated Proyeksi Perkiraan:
    # confirmed + high-confidence recurring patterns without existing upcoming
    patterns = detect_recurring_patterns(con, as_of_date=target_date, min_support=3)
    estimated_events = list(confirmed_events)
    estimated_outflow = confirmed_outflow
    estimated_inflow = confirmed_inflow

    for p in patterns:
        if p["confidence"] == "high" and not p["has_existing_upcoming"]:
            p_date = p["predicted_next_date"]
            if target_date <= p_date <= end_date_str:
                amt = p["median_amount"]
                if p["transaction_type"] == "Expense":
                    estimated_outflow += amt
                    estimated_events.append({
                        "date": p_date,
                        "title": f"Perkiraan: {p['title']}",
                        "amount": amt,
                        "type": "outflow",
                        "source": "recurring_pattern",
                        "account": p["account"],
                        "category": p["category"],
                    })
                elif p["transaction_type"] == "Income":
                    estimated_inflow += amt
                    estimated_events.append({
                        "date": p_date,
                        "title": f"Perkiraan: {p['title']}",
                        "amount": amt,
                        "type": "income",
                        "source": "recurring_pattern",
                        "account": p["account"],
                        "category": p["category"],
                    })

    # Sort estimated events
    estimated_events.sort(key=lambda x: x["date"])

    cur_bal_est = opening_balance
    lowest_est = cur_bal_est
    for ev in estimated_events:
        if ev["type"] == "outflow":
            cur_bal_est -= ev["amount"]
        else:
            cur_bal_est += ev["amount"]
        if cur_bal_est < lowest_est:
            lowest_est = cur_bal_est

    estimated_proj_balance = round(cur_bal_est, 2)
    lowest_est = round(lowest_est, 2)

    # Warnings
    warnings = []
    if lowest_conf < 0:
        warnings.append(f"PERINGATAN: Saldo kas pasti diproyeksikan defisit hingga {idr(lowest_conf)}.")
    if lowest_est < 0:
        warnings.append(f"PERINGATAN: Saldo kas perkiraan diproyeksikan defisit hingga {idr(lowest_est)}.")
    elif lowest_est < emergency_fund and emergency_fund > 0:
        warnings.append(f"Perhatian: Saldo kas perkiraan ({idr(lowest_est)}) berpotensi menyentuh Dana Darurat ({idr(emergency_fund)}).")

    # Explanation
    explanation = (
        f"Proyeksi kas {horizon_days} hari ke depan (sampai {end_date_str}). "
        f"Saldo awal likuid {idr(opening_balance)}. "
        f"Komitmen pasti sebesar {idr(confirmed_outflow)}, memproyeksikan saldo akhir pasti {idr(confirmed_proj_balance)}. "
        f"Dengan memperhitungkan pola tagihan berulang berkredibilitas tinggi, proyeksi perkiraan arus keluar adalah {idr(estimated_outflow)} "
        f"dengan saldo akhir perkiraan {idr(estimated_proj_balance)}."
    )

    return {
        "as_of_date": target_date,
        "end_date": end_date_str,
        "horizon_days": horizon_days,
        "opening_balance": opening_balance,
        "emergency_fund": emergency_fund,
        "confirmed": {
            "income": round(confirmed_inflow, 2),
            "outflow": round(confirmed_outflow, 2),
            "projected_balance": confirmed_proj_balance,
            "lowest_balance": lowest_conf,
            "events": confirmed_events,
        },
        "estimated": {
            "income": round(estimated_inflow, 2),
            "outflow": round(estimated_outflow, 2),
            "projected_balance": estimated_proj_balance,
            "lowest_balance": lowest_est,
            "events": estimated_events,
        },
        "warnings": warnings,
        "explanation": explanation,
    }


def get_accounts_freshness(
    con: sqlite3.Connection,
    as_of_date: str | None = None,
) -> list[dict]:
    """Computes freshness indicator for all accounts (read-only)."""
    target_date = as_of_date or get_wib_today_str()
    as_of_d = dt.date.fromisoformat(target_date)

    accounts = con.execute("""
        SELECT name, kind, current_balance, balance_date, last_reconciled_at, active
        FROM accounts
        ORDER BY kind ASC, name ASC
    """).fetchall()

    # Batch max transaction date per account
    tx_dates = {}
    for r in con.execute("""
        SELECT account_from as acc, max(date) as max_d
        FROM transactions
        WHERE is_deleted = 0 AND date <= ? AND account_from != ''
        GROUP BY account_from
        UNION ALL
        SELECT account_to as acc, max(date) as max_d
        FROM transactions
        WHERE is_deleted = 0 AND date <= ? AND account_to != ''
        GROUP BY account_to
    """, (target_date, target_date)).fetchall():
        acc = r[0]
        d = r[1]
        if acc and d:
            tx_dates[acc] = max(tx_dates.get(acc, ""), d)

    # Batch snapshots count & max date per account
    snap_info = {}
    for r in con.execute("""
        SELECT account_name, count(*) as cnt, max(snapshot_date) as max_d
        FROM balance_snapshots
        GROUP BY account_name
    """).fetchall():
        snap_info[r[0]] = {"cnt": r[1], "max_d": r[2]}

    result = []
    for a in accounts:
        acc_name = a["name"]
        last_tx = tx_dates.get(acc_name, "")
        s_info = snap_info.get(acc_name, {"cnt": 0, "max_d": ""})

        dates_to_check = [d for d in [a["balance_date"], last_tx, a["last_reconciled_at"], s_info["max_d"]] if d]
        if dates_to_check:
            latest_act = max(dates_to_check)
            try:
                days_since = (as_of_d - dt.date.fromisoformat(latest_act[:10])).days
            except Exception:
                days_since = 999
        else:
            latest_act = ""
            days_since = 999

        if s_info["cnt"] == 0 and not a["last_reconciled_at"]:
            status_label = "Belum pernah diverifikasi"
            badge = "neutral"
            desc = "Rekening belum memiliki titik jangkar rekonsiliasi."
        elif days_since <= 3:
            status_label = "Baru diperbarui"
            badge = "success"
            desc = f"Catatan terakhir {days_since} hari lalu."
        else:
            status_label = "Perlu diperiksa"
            badge = "warning"
            desc = f"Tidak ada catatan baru selama {days_since} hari."

        result.append({
            "name": a["name"],
            "kind": a["kind"],
            "current_balance": a["current_balance"],
            "balance_date": a["balance_date"],
            "last_transaction_date": last_tx or "",
            "last_reconciled_at": a["last_reconciled_at"],
            "days_since_last_record": days_since,
            "status_label": status_label,
            "badge": badge,
            "description": desc,
            "active": a["active"],
        })

    return result


def create_upcoming_from_recurring(
    con: sqlite3.Connection,
    payload: dict,
) -> dict:
    """Creates a new upcoming commitment from a detected recurring pattern (atomic & idempotent)."""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Judul komitmen tidak boleh kosong.")

    try:
        amount = float(payload.get("amount", 0))
        if amount <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Nominal komitmen harus berupa angka positif.")

    due_date = str(payload.get("due_date") or "").strip()
    if not due_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
        raise ValueError("Format tanggal jatuh tempo harus YYYY-MM-DD.")

    category = str(payload.get("category") or "Other / Miscellaneous").strip()
    account = str(payload.get("account") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    reserve_now = 1 if payload.get("reserve_now") in (1, "1", True) else 0

    # Anti-duplication check: avoid exact duplicate upcoming for same title and due_date
    existing = con.execute("""
        SELECT id FROM upcoming
        WHERE title = ? AND due_date = ? AND status IN ('Upcoming', 'Confirmed')
    """, (title, due_date)).fetchone()

    if existing:
        return {
            "status": "ok",
            "upcoming_id": existing["id"],
            "is_duplicate": True,
            "message": f"Komitmen '{title}' untuk tanggal {due_date} sudah ada sebelumnya.",
        }

    with con:
        cur = con.execute("""
            INSERT INTO upcoming (
                due_date, due_time, title, amount, category, account,
                for_with_whom, status, notes, reserve_now
            ) VALUES (?, '12:00', ?, ?, ?, ?, 'Personal / Self', 'Upcoming', ?, ?)
        """, (due_date, title, amount, category, account, notes, reserve_now))
        upcoming_id = cur.lastrowid

    return {
        "status": "ok",
        "upcoming_id": upcoming_id,
        "is_duplicate": False,
        "message": f"Komitmen '{title}' sebesar {idr(amount)} berhasil ditambahkan.",
    }


def detect_anomalies(
    con: sqlite3.Connection,
    as_of_date: str | None = None,
    min_support: int = 3,
    lookback_days: int = 30,
) -> list[dict]:
    """Detects mild nominal anomalies using median and MAD per merchant/pattern (read-only)."""
    target_date = as_of_date or get_wib_today_str()
    as_of_d = dt.date.fromisoformat(target_date)
    start_d = as_of_d - dt.timedelta(days=lookback_days)
    start_date_str = start_d.isoformat()

    query = """
        SELECT id, date, time, transaction_type, amount, account_from, account_to, description, category
        FROM transactions
        WHERE is_deleted = 0
          AND money_context = 'Personal'
          AND status IN ('Confirmed', 'Auto-classified')
          AND transaction_type = 'Expense'
          AND reversal_of_id IS NULL
          AND description NOT LIKE '%[Rekonsiliasi]%'
          AND date <= ?
        ORDER BY date ASC, id ASC
    """
    rows = con.execute(query, (target_date,)).fetchall()

    merchant_txs = defaultdict(list)
    for r in rows:
        norm = normalize_counterparty(r["description"]) or r["description"].lower().strip()
        merchant_txs[norm].append(r)

    anomalies = []
    for norm, txs in merchant_txs.items():
        if len(txs) < min_support:
            continue

        amounts = [float(t["amount"]) for t in txs]
        med_amt = statistics.median(amounts)
        abs_diffs = [abs(a - med_amt) for a in amounts]
        mad = statistics.median(abs_diffs)

        recent_txs = [t for t in txs if t["date"] >= start_date_str]
        for t in recent_txs:
            amt = float(t["amount"])
            diff = abs(amt - med_amt)
            is_anomaly = False
            reason = ""

            if mad == 0:
                if diff >= 10000.0 and (amt > 1.5 * med_amt or amt < 0.5 * med_amt):
                    is_anomaly = True
                    reason = f"Nominal {idr(amt)} berbeda dari histori tetap ({idr(med_amt)})."
            else:
                if mad > 0 and (diff / mad) >= 3.0 and diff >= 10000.0:
                    is_anomaly = True
                    ratio = diff / mad
                    reason = f"Nominal {idr(amt)} menyimpang jauh dari median historis ({idr(med_amt)}, deviasi {ratio:.1f}x MAD)."

            if is_anomaly:
                anomalies.append({
                    "transaction_id": t["id"],
                    "date": t["date"],
                    "time": t["time"],
                    "description": t["description"],
                    "amount": amt,
                    "median_amount": med_amt,
                    "difference": diff,
                    "mad": mad,
                    "status_label": "Perlu dicek",
                    "reason": reason,
                })

    anomalies.sort(key=lambda x: (x["date"], x["transaction_id"]), reverse=True)
    return anomalies


def get_source_freshness(
    con: sqlite3.Connection,
    as_of_date: str | None = None,
) -> dict:
    """Computes source freshness and timestamp distance for ledger data (read-only)."""
    target_date = as_of_date or get_wib_today_str()
    wib_now = get_wib_now()

    last_tx = con.execute("""
        SELECT date, time, updated_at
        FROM transactions
        WHERE is_deleted = 0 AND date <= ?
        ORDER BY date DESC, time DESC, id DESC
        LIMIT 1
    """, (target_date,)).fetchone()

    if last_tx and last_tx["date"]:
        last_d_str = last_tx["date"]
        last_t_str = last_tx["time"] or "00:00"
        try:
            last_dt = dt.datetime.fromisoformat(f"{last_d_str}T{last_t_str[:5]}:00").replace(tzinfo=JAKARTA_TZ)
            diff = wib_now - last_dt
            diff_hours = max(0, int(diff.total_seconds() // 3600))
            diff_days = max(0, diff.days)
        except Exception:
            diff_hours = 0
            diff_days = 0
    else:
        last_d_str = target_date
        last_t_str = "00:00"
        diff_hours = 0
        diff_days = 0

    return {
        "latest_transaction_date": last_d_str,
        "latest_transaction_time": last_t_str,
        "days_since_latest": diff_days,
        "hours_since_latest": diff_hours,
        "source_name": "Input manual",
        "status_label": "Input manual",
        "is_sync_connector_active": False,
        "message": f"Catatan transaksi terbaru: {last_d_str} {last_t_str} WIB ({diff_days} hari lalu via input manual).",
    }


def get_all_insights(
    con: sqlite3.Connection,
    as_of_date: str | None = None,
    min_support: int = 3,
) -> dict:
    """Consolidates recurring patterns, cashflow forecasts, anomalies, and freshness indicators."""
    target_date = as_of_date or get_wib_today_str()
    return {
        "as_of_date": target_date,
        "recurring_patterns": detect_recurring_patterns(con, as_of_date=target_date, min_support=min_support),
        "forecast_7d": cashflow_forecast(con, as_of_date=target_date, horizon_days=7),
        "forecast_30d": cashflow_forecast(con, as_of_date=target_date, horizon_days=30),
        "anomalies": detect_anomalies(con, as_of_date=target_date, min_support=min_support),
        "source_freshness": get_source_freshness(con, as_of_date=target_date),
    }
