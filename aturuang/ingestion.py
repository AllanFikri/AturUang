"""
Money Tracks V12 — Ingestion Engine, Staging, and Review Pipeline

Canonical Pipeline:
SOURCE → RAW EVENT → PARSED CANDIDATE → DEDUPLICATION → REVIEW → TRANSACTION
"""
from __future__ import annotations

import abc
import datetime as dt
import hashlib
import json
import re
import sqlite3
from typing import Any

try:
    from aturuang import services
except ImportError:
    import services

# =====================================================================
# A. STAGING SCHEMA DEFINITION
# =====================================================================

STAGING_SCHEMA_SQL = """
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


def init_staging_schema(con: sqlite3.Connection) -> None:
    """Idempotently applies the staging schema to the SQLite connection."""
    try:
        con.executescript(STAGING_SCHEMA_SQL)
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        for stmt in STAGING_SCHEMA_SQL.split(";"):
            s = stmt.strip()
            if s:
                try:
                    con.execute(s)
                except (sqlite3.DatabaseError, sqlite3.OperationalError):
                    pass


# =====================================================================
# B. REDACTION & NOMINAL PARSING UTILITIES
# =====================================================================

def sanitize_payload(text: str) -> str:
    """Redacts sensitive credentials, card numbers, OTPs, and tokens from raw payload."""
    if not text:
        return ""
    # Redact 13-19 digit card numbers (preserves last 4 digits)
    text = re.sub(
        r'\b(?:\d[ -]*?){13,19}\b',
        lambda m: '•••• ' + re.sub(r'\D', '', m.group(0))[-4:],
        text
    )
    # Redact OTP / Verification codes
    text = re.sub(
        r'(?i)\b(otp|kode verifikasi|verification code|one time password)[:\s]*([0-9]{4,8})\b',
        r'\1: [REDACTED]',
        text
    )
    # Redact auth tokens / passwords / PINs
    text = re.sub(
        r'(?i)\b(token|bearer|password|pin|secret)[:\s]*([a-zA-Z0-9_\-\.]{6,})\b',
        r'\1: [REDACTED]',
        text
    )
    return text[:2000].strip()


def parse_nominal(val_str: Any) -> float:
    """Parses various currency string formats into a float rounded to 2 decimal places."""
    if isinstance(val_str, (int, float)):
        return round(float(val_str), 2)
    s = str(val_str).strip()
    if not s:
        return 0.0

    # Remove currency prefixes, debit/credit indicators, etc.
    s = re.sub(r'(?i)\b(?:rp|idr|db|cr|debit|credit|dr|idr\.)\b', '', s).strip()
    s = s.replace('Rp', '').replace('rp', '').replace('RP', '').strip()

    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            # Indonesian format: 67.821,80
            s = s.replace('.', '').replace(',', '.')
        else:
            # English format: 67,821.80
            s = s.replace(',', '')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    elif '.' in s:
        parts = s.split('.')
        if len(parts) == 2 and len(parts[1]) <= 2:
            pass
        else:
            s = s.replace('.', '')

    s = re.sub(r'[^0-9\.]', '', s)
    if not s:
        return 0.0
    return round(float(s), 2)


# =====================================================================
# C. PARSER CONTRACT & PROVIDER IMPLEMENTATIONS
# =====================================================================

class BaseProviderParser(abc.ABC):
    provider_name: str = "base"
    parser_version: str = "1.0"

    @abc.abstractmethod
    def can_parse(self, source_metadata: dict, raw_payload: str) -> bool:
        """Determines if this provider parser can process the given payload."""
        pass

    @abc.abstractmethod
    def parse(self, raw_event_data: dict) -> dict | None:
        """Parses raw event data into candidate fields dictionary.
        Returns None if parsing fails."""
        pass

    def build_fingerprint(self, candidate_data: dict) -> str:
        """Constructs deterministic fingerprint for duplicate prevention."""
        norm_desc = re.sub(r'[^a-z0-9]', '', candidate_data.get("description", "").lower())
        raw_key = (
            f"{candidate_data.get('date', '')}|"
            f"{candidate_data.get('time', '')[:5]}|"
            f"{candidate_data.get('account_from', '')}|"
            f"{candidate_data.get('account_to', '')}|"
            f"{candidate_data.get('transaction_type', '')}|"
            f"{float(candidate_data.get('amount', 0.0)):.2f}|"
            f"{norm_desc}"
        )
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    def explain(self, candidate_data: dict) -> str:
        """Provides human-readable summary of how the candidate was parsed."""
        return (
            f"Diproses oleh {self.provider_name} (v{self.parser_version}): "
            f"{candidate_data.get('transaction_type')} {candidate_data.get('amount')} "
            f"pada {candidate_data.get('date')} ({candidate_data.get('description')})"
        )


class BCAEmailParser(BaseProviderParser):
    provider_name = "bca_email"
    parser_version = "1.0"

    def can_parse(self, source_metadata: dict, raw_payload: str) -> bool:
        provider = source_metadata.get("provider", "").lower()
        if provider:
            return provider in ("bca_email", "bca", "bca_qris")
        p_lower = raw_payload.lower()
        return ("bca" in p_lower or "klikbca" in p_lower or "mybca" in p_lower) and ("," not in raw_payload or "\n" in raw_payload)

    def parse(self, raw_event_data: dict) -> dict | None:
        payload = raw_event_data.get("raw_payload", "")
        # Structured JSON payload check
        if payload.strip().startswith("{") and payload.strip().endswith("}"):
            try:
                data = json.loads(payload)
                tx_type = data.get("type", "Expense")
                amt = parse_nominal(data.get("amount", 0.0))
                date_str = data.get("date", "")
                time_str = data.get("time", "12:00")
                desc = data.get("description", "Transaksi BCA")
                acc = data.get("account", "BCA Main")
                cat = data.get("category", "Other / Miscellaneous")
                return {
                    "date": date_str,
                    "time": time_str,
                    "transaction_type": tx_type,
                    "amount": amt,
                    "account_from": acc if tx_type in ("Expense", "Transfer") else "",
                    "account_to": acc if tx_type == "Income" else "",
                    "description": desc,
                    "category": cat,
                    "money_context": "Personal",
                    "confidence": "high",
                }
            except Exception:
                pass

        # Text notification pattern extraction
        amt_match = re.search(r'(?i)(?:sebesar|nominal|jumlah|rp\.?|idr)\s*([0-9\.,]+)', payload)
        date_match = re.search(r'\b(202[0-9]-[0-1][0-9]-[0-3][0-9])\b', payload)
        time_match = re.search(r'\b([0-2][0-9]:[0-5][0-9])\b', payload)
        
        if not amt_match or not date_match:
            return None

        amt = parse_nominal(amt_match.group(1))
        date_str = date_match.group(1)
        time_str = time_match.group(1) if time_match else "12:00"

        is_inflow = bool(re.search(r'(?i)\b(?:dana masuk|cr|credit|transfer masuk|terima)\b', payload))
        tx_type = "Income" if is_inflow else "Expense"

        desc_match = re.search(r'(?i)(?:di|ke|merchant|penerima|keterangan)[:\s]+([^,\.\n]+)', payload)
        desc = desc_match.group(1).strip() if desc_match else "Transaksi BCA"

        cat = "Food & Dining" if ("kopi" in desc.lower() or "makan" in desc.lower()) else "Other / Miscellaneous"

        return {
            "date": date_str,
            "time": time_str,
            "transaction_type": tx_type,
            "amount": amt,
            "account_from": "BCA Main" if tx_type == "Expense" else "",
            "account_to": "BCA Main" if tx_type == "Income" else "",
            "description": desc,
            "category": cat,
            "money_context": "Personal",
            "confidence": "high" if amt > 0 and date_str else "medium",
        }


class ShopeePayEmailParser(BaseProviderParser):
    provider_name = "shopeepay_email"
    parser_version = "1.0"

    def can_parse(self, source_metadata: dict, raw_payload: str) -> bool:
        provider = source_metadata.get("provider", "").lower()
        if provider:
            return provider in ("shopeepay_email", "shopeepay", "shopee")
        p_lower = raw_payload.lower()
        return ("shopeepay" in p_lower or "shopee" in p_lower) and ("," not in raw_payload or "\n" in raw_payload)

    def parse(self, raw_event_data: dict) -> dict | None:
        payload = raw_event_data.get("raw_payload", "")
        if payload.strip().startswith("{") and payload.strip().endswith("}"):
            try:
                data = json.loads(payload)
                tx_type = data.get("type", "Expense")
                amt = parse_nominal(data.get("amount", 0.0))
                return {
                    "date": data.get("date", ""),
                    "time": data.get("time", "12:00"),
                    "transaction_type": tx_type,
                    "amount": amt,
                    "account_from": "ShopeePay" if tx_type in ("Expense", "Transfer") else "",
                    "account_to": "ShopeePay" if tx_type == "Income" else "",
                    "description": data.get("description", "Pembayaran ShopeePay"),
                    "category": data.get("category", "Online Shopping"),
                    "money_context": "Personal",
                    "confidence": "high",
                }
            except Exception:
                pass

        amt_match = re.search(r'(?i)(?:sebesar|nominal|rp\.?|idr)\s*([0-9\.,]+)', payload)
        date_match = re.search(r'\b(202[0-9]-[0-1][0-9]-[0-3][0-9])\b', payload)
        time_match = re.search(r'\b([0-2][0-9]:[0-5][0-9])\b', payload)

        if not amt_match or not date_match:
            return None

        amt = parse_nominal(amt_match.group(1))
        date_str = date_match.group(1)
        time_str = time_match.group(1) if time_match else "12:00"

        is_topup = bool(re.search(r'(?i)\b(?:top up|isi saldo|terima)\b', payload))
        tx_type = "Income" if is_topup else "Expense"

        return {
            "date": date_str,
            "time": time_str,
            "transaction_type": tx_type,
            "amount": amt,
            "account_from": "ShopeePay" if tx_type == "Expense" else "",
            "account_to": "ShopeePay" if tx_type == "Income" else "",
            "description": "Pembayaran ShopeePay",
            "category": "Online Shopping",
            "money_context": "Personal",
            "confidence": "high",
        }


class JagoEmailParser(BaseProviderParser):
    provider_name = "jago_email"
    parser_version = "1.0"

    def can_parse(self, source_metadata: dict, raw_payload: str) -> bool:
        provider = source_metadata.get("provider", "").lower()
        if provider:
            return provider in ("jago_email", "jago", "bank_jago")
        p_lower = raw_payload.lower()
        return "bank jago" in p_lower or "kantong jago" in p_lower

    def parse(self, raw_event_data: dict) -> dict | None:
        payload = raw_event_data.get("raw_payload", "")
        if payload.strip().startswith("{") and payload.strip().endswith("}"):
            try:
                data = json.loads(payload)
                tx_type = data.get("type", "Expense")
                amt = parse_nominal(data.get("amount", 0.0))
                return {
                    "date": data.get("date", ""),
                    "time": data.get("time", "12:00"),
                    "transaction_type": tx_type,
                    "amount": amt,
                    "account_from": data.get("account", "Bank Jago") if tx_type in ("Expense", "Transfer") else "",
                    "account_to": data.get("account", "Bank Jago") if tx_type == "Income" else "",
                    "description": data.get("description", "Transaksi Jago"),
                    "category": data.get("category", "Other / Miscellaneous"),
                    "money_context": "Personal",
                    "confidence": "high",
                }
            except Exception:
                pass
        return None


class CSVGenericParser(BaseProviderParser):
    provider_name = "csv_generic"
    parser_version = "1.0"

    def can_parse(self, source_metadata: dict, raw_payload: str) -> bool:
        provider = source_metadata.get("provider", "").lower()
        if provider:
            return provider in ("csv_generic", "csv", "csv_import")
        source_type = source_metadata.get("source_type", "").lower()
        return "csv" in source_type or ("," in raw_payload and "\n" not in raw_payload)

    def parse(self, raw_event_data: dict) -> dict | None:
        payload = raw_event_data.get("raw_payload", "")
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) < 4:
            return None
        date_str = parts[0]
        tx_type = parts[1] if parts[1] in ("Income", "Expense", "Transfer", "Adjustment") else "Expense"
        amt = parse_nominal(parts[2])
        acc = parts[3]
        desc = parts[4] if len(parts) > 4 else "Impor CSV"
        cat = parts[5] if len(parts) > 5 else "Other / Miscellaneous"

        return {
            "date": date_str,
            "time": "12:00",
            "transaction_type": tx_type,
            "amount": amt,
            "account_from": acc if tx_type in ("Expense", "Transfer") else "",
            "account_to": acc if tx_type == "Income" else "",
            "description": desc,
            "category": cat,
            "money_context": "Personal",
            "confidence": "high",
        }


PARSER_REGISTRY: list[BaseProviderParser] = [
    BCAEmailParser(),
    ShopeePayEmailParser(),
    JagoEmailParser(),
    CSVGenericParser(),
]

def get_parser_for(source_metadata: dict, raw_payload: str) -> BaseProviderParser | None:
    for p in PARSER_REGISTRY:
        if p.can_parse(source_metadata, raw_payload):
            return p
    return None


# =====================================================================
# D. INGESTION & STAGING PIPELINE EXECUTION
# =====================================================================

def create_import_batch(
    con: sqlite3.Connection,
    source_type: str,
    source_name: str,
    raw_content: str = "",
) -> dict:
    """Creates a new import batch header."""
    init_staging_schema(con)
    source_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    cur = con.execute("""
        INSERT INTO import_batches (
            source_type, source_name, source_hash, status
        ) VALUES (?, ?, ?, 'IN_PROGRESS')
    """, (source_type, source_name, source_hash))
    batch_id = cur.lastrowid
    return {
        "status": "ok",
        "batch_id": batch_id,
        "source_type": source_type,
        "source_name": source_name,
    }


def process_single_raw_item(
    con: sqlite3.Connection,
    batch_id: int,
    item: dict,
) -> dict:
    """Processes a single raw event item through parsing, deduplication, and staging insertion."""
    init_staging_schema(con)
    provider = item.get("provider", "generic")
    ext_id = str(item.get("external_event_id", "")).strip()
    raw_payload = item.get("raw_payload", "")
    occurred_at = item.get("occurred_at", "")

    sanitized = sanitize_payload(raw_payload)
    payload_hash = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()

    # 1. Insert Raw Event (Tier 1 Duplicate Detection via DB Constraint)
    try:
        cur = con.execute("""
            INSERT INTO raw_import_events (
                batch_id, provider, external_event_id, occurred_at,
                payload_hash, minimal_raw_payload, parser_version, parse_status
            ) VALUES (?, ?, ?, ?, ?, ?, '1.0', 'PENDING')
        """, (batch_id, provider, ext_id, occurred_at, payload_hash, sanitized))
        raw_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return {
            "status": "duplicate",
            "reason": f"External event '{ext_id}' from provider '{provider}' already exists in staging.",
        }

    # 2. Parse candidate
    parser = get_parser_for({"provider": provider, "source_type": item.get("source_type", "")}, raw_payload)
    if not parser:
        con.execute("UPDATE raw_import_events SET parse_status='ERROR' WHERE id=?", (raw_id,))
        return {"status": "error", "reason": "No parser found for provider"}

    candidate = parser.parse({"raw_payload": raw_payload, "occurred_at": occurred_at})
    if not candidate or candidate.get("amount", 0.0) <= 0 or not candidate.get("date"):
        con.execute("UPDATE raw_import_events SET parse_status='ERROR' WHERE id=?", (raw_id,))
        return {"status": "error", "reason": "Parser returned invalid candidate data"}

    # 3. Deduplication Check
    fingerprint = parser.build_fingerprint(candidate)
    dup_status = "None"
    matched_tx_id = None
    review_status = "Pending"
    review_notes = ""

    # Priority 2: Canonical source_refs check
    src_ref_query = f"%{provider}:{ext_id}%"
    tx_match = con.execute(
        "SELECT id FROM transactions WHERE is_deleted=0 AND source_refs LIKE ?",
        (src_ref_query,)
    ).fetchone()

    if tx_match:
        dup_status = "Exact"
        matched_tx_id = tx_match["id"]
        review_status = "Duplicate"
        review_notes = f"Duplikat eksak transaksi #{matched_tx_id} via referensi sumber."
    else:
        # Priority 3: Exact match on date, amount, type, account
        acc = candidate["account_from"] or candidate["account_to"]
        exact_tx = con.execute("""
            SELECT id FROM transactions
            WHERE is_deleted=0
              AND date = ?
              AND transaction_type = ?
              AND abs(amount - ?) < 0.005
              AND (account_from = ? OR account_to = ?)
        """, (candidate["date"], candidate["transaction_type"], candidate["amount"], acc, acc)).fetchone()

        if exact_tx:
            dup_status = "Exact"
            matched_tx_id = exact_tx["id"]
            review_status = "Duplicate"
            review_notes = f"Kecocokan eksak dengan transaksi riil #{matched_tx_id}."
        else:
            # Priority 4: Probable duplicate (date +-1 day, same amount & account)
            prob_tx = con.execute("""
                SELECT id FROM transactions
                WHERE is_deleted=0
                  AND (date BETWEEN date(?, '-1 day') AND date(?, '+1 day'))
                  AND abs(amount - ?) < 0.005
                  AND (account_from = ? OR account_to = ?)
            """, (candidate["date"], candidate["date"], candidate["amount"], acc, acc)).fetchone()

            if prob_tx:
                dup_status = "Probable"
                matched_tx_id = prob_tx["id"]
                review_status = "Pending"
                review_notes = f"Kemungkinan duplikat dengan transaksi #{matched_tx_id} (tanggal & nominal mirip)."

    # 4. Insert Candidate
    try:
        cur_c = con.execute("""
            INSERT INTO import_candidates (
                raw_event_id, fingerprint, date, time, transaction_type,
                amount, account_from, account_to, description, category,
                money_context, confidence, duplicate_status, matched_transaction_id,
                review_status, review_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            raw_id, fingerprint, candidate["date"], candidate.get("time", "12:00"),
            candidate["transaction_type"], candidate["amount"],
            candidate.get("account_from", ""), candidate.get("account_to", ""),
            candidate.get("description", ""), candidate.get("category", "Other / Miscellaneous"),
            candidate.get("money_context", "Personal"), candidate.get("confidence", "high"),
            dup_status, matched_tx_id, review_status, review_notes
        ))
        cand_id = cur_c.lastrowid
        con.execute("UPDATE raw_import_events SET parse_status='PARSED' WHERE id=?", (raw_id,))
        return {
            "status": "ok",
            "candidate_id": cand_id,
            "duplicate_status": dup_status,
            "review_status": review_status,
        }
    except sqlite3.IntegrityError:
        con.execute("UPDATE raw_import_events SET parse_status='DUPLICATE' WHERE id=?", (raw_id,))
        return {
            "status": "duplicate",
            "reason": "Candidate fingerprint collision in staging.",
        }


def process_raw_items(
    con: sqlite3.Connection,
    batch_id: int,
    raw_items: list[dict],
) -> dict:
    """Processes a batch of raw items through the pipeline and updates batch stats."""
    total = len(raw_items)
    parsed = 0
    duplicate = 0
    error = 0

    for item in raw_items:
        res = process_single_raw_item(con, batch_id, item)
        if res.get("status") == "ok":
            if res.get("duplicate_status") == "Exact":
                duplicate += 1
            else:
                parsed += 1
        elif res.get("status") == "duplicate":
            duplicate += 1
        else:
            error += 1

    now_str = dt.datetime.now().isoformat()
    con.execute("""
        UPDATE import_batches
        SET status='COMPLETED',
            completed_at=?,
            total_count=total_count + ?,
            parsed_count=parsed_count + ?,
            duplicate_count=duplicate_count + ?,
            error_count=error_count + ?
        WHERE id=?
    """, (now_str, total, parsed, duplicate, error, batch_id))

    return {
        "status": "ok",
        "batch_id": batch_id,
        "total": total,
        "parsed": parsed,
        "duplicate": duplicate,
        "error": error,
    }


# =====================================================================
# E. REVIEW & APPROVAL ENGINE (ATOMIC & IDEMPOTENT)
# =====================================================================

def approve_import_candidate(
    con: sqlite3.Connection,
    candidate_id: int,
    reviewer_notes: str = "",
    actor: str = "import_reviewer",
) -> dict:
    """Atomically approves a candidate into a canonical posted transaction and mutates account balances."""
    init_staging_schema(con)
    cand = con.execute("SELECT * FROM import_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not cand:
        raise ValueError(f"Candidate #{candidate_id} not found")
    if cand["review_status"] == "Approved":
        raise ValueError(f"Candidate #{candidate_id} is already approved (Tx #{cand['approved_transaction_id']})")
    if cand["review_status"] == "Rejected":
        raise ValueError(f"Candidate #{candidate_id} was rejected and cannot be approved directly")

    # Validate accounts
    acc_from = cand["account_from"]
    acc_to = cand["account_to"]
    tx_type = cand["transaction_type"]

    if tx_type in ("Expense", "Transfer") and acc_from:
        acc_row = con.execute("SELECT active FROM accounts WHERE name=?", (acc_from,)).fetchone()
        if not acc_row or not acc_row["active"]:
            raise ValueError(f"Account '{acc_from}' is invalid or inactive")
    if tx_type in ("Income", "Transfer") and acc_to:
        acc_row = con.execute("SELECT active FROM accounts WHERE name=?", (acc_to,)).fetchone()
        if not acc_row or not acc_row["active"]:
            raise ValueError(f"Account '{acc_to}' is invalid or inactive")

    # Construct canonical source reference
    raw_ev = con.execute("SELECT provider, external_event_id FROM raw_import_events WHERE id=?", (cand["raw_event_id"],)).fetchone()
    src_ref = f"import:{cand['id']}:{raw_ev['provider']}:{raw_ev['external_event_id']}" if raw_ev else f"import:{cand['id']}"

    tx_date = cand["date"]
    amt = round(float(cand["amount"]), 2)

    # 1. Insert transaction
    cur = con.execute("""
        INSERT INTO transactions (
            date, time, transaction_type, amount, account_from, account_to,
            description, category, for_with_whom, money_context, status,
            source_refs, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Personal / Self', ?, 'Confirmed', ?, ?)
    """, (
        tx_date, cand["time"] or "12:00", tx_type, amt, acc_from, acc_to,
        cand["description"], cand["category"], cand["money_context"],
        src_ref, reviewer_notes or cand["review_notes"]
    ))
    tx_id = cur.lastrowid

    # 2. Mutate account balance
    if tx_type == "Expense" and acc_from:
        services.mutate_account_balance(con, acc_from, -amt, tx_date)
    elif tx_type == "Income" and acc_to:
        services.mutate_account_balance(con, acc_to, +amt, tx_date)
    elif tx_type == "Transfer":
        if acc_from:
            services.mutate_account_balance(con, acc_from, -amt, tx_date)
        if acc_to:
            services.mutate_account_balance(con, acc_to, +amt, tx_date)

    # 3. Insert audit log
    con.execute("""
        INSERT INTO transaction_audit_log (
            transaction_id, action, old_data, new_data
        ) VALUES (?, 'IMPORT_APPROVE', '', ?)
    """, (tx_id, json.dumps({
        "candidate_id": cand["id"],
        "source_refs": src_ref,
        "amount": amt,
        "type": tx_type,
        "actor": actor
    })))

    # 4. Update candidate status
    now_str = dt.datetime.now().isoformat()
    con.execute("""
        UPDATE import_candidates
        SET review_status='Approved',
            approved_transaction_id=?,
            reviewed_at=?,
            review_notes=?
        WHERE id=?
    """, (tx_id, now_str, reviewer_notes or cand["review_notes"], cand["id"]))

    return {
        "status": "ok",
        "candidate_id": cand["id"],
        "transaction_id": tx_id,
        "message": f"Kandidat #{cand['id']} disetujui menjadi Transaksi #{tx_id} ({services.idr(amt)}).",
    }


def reject_import_candidate(
    con: sqlite3.Connection,
    candidate_id: int,
    reviewer_notes: str = "",
) -> dict:
    """Marks a candidate as Rejected."""
    init_staging_schema(con)
    cand = con.execute("SELECT * FROM import_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not cand:
        raise ValueError(f"Candidate #{candidate_id} not found")
    if cand["review_status"] == "Approved":
        raise ValueError(f"Cannot reject candidate #{candidate_id} because it is already approved")

    now_str = dt.datetime.now().isoformat()
    con.execute("""
        UPDATE import_candidates
        SET review_status='Rejected',
            reviewed_at=?,
            review_notes=?
        WHERE id=?
    """, (now_str, reviewer_notes or cand["review_notes"], candidate_id))

    return {
        "status": "ok",
        "candidate_id": candidate_id,
        "message": f"Kandidat #{candidate_id} telah ditolak.",
    }


def bulk_approve_candidates(
    con: sqlite3.Connection,
    candidate_ids: list[int],
    allow_low_confidence: bool = False,
    allow_probable: bool = False,
    actor: str = "import_reviewer",
) -> dict:
    """Bulk approves pending candidates with safety checks."""
    init_staging_schema(con)
    approved_count = 0
    total_amount = 0.0
    affected_accounts = set()
    skipped_ids = []
    errors = []

    for cid in candidate_ids:
        cand = con.execute("SELECT * FROM import_candidates WHERE id=?", (cid,)).fetchone()
        if not cand or cand["review_status"] != "Pending":
            skipped_ids.append(cid)
            continue
        if cand["confidence"] == "low" and not allow_low_confidence:
            skipped_ids.append(cid)
            continue
        if cand["duplicate_status"] == "Probable" and not allow_probable:
            skipped_ids.append(cid)
            continue

        try:
            res = approve_import_candidate(con, cid, reviewer_notes="Bulk approved", actor=actor)
            approved_count += 1
            amt = float(cand["amount"])
            total_amount += amt
            if cand["account_from"]:
                affected_accounts.add(cand["account_from"])
            if cand["account_to"]:
                affected_accounts.add(cand["account_to"])
        except Exception as e:
            errors.append({"candidate_id": cid, "error": str(e)})

    return {
        "status": "ok",
        "approved_count": approved_count,
        "total_amount": round(total_amount, 2),
        "accounts_affected": sorted(list(affected_accounts)),
        "skipped_ids": skipped_ids,
        "errors": errors,
    }


# =====================================================================
# F. READ-ONLY QUERY SERVICES
# =====================================================================

def get_import_batches(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    init_staging_schema(con)
    rows = con.execute("""
        SELECT * FROM import_batches
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_import_candidates(
    con: sqlite3.Connection,
    status: str | None = None,
    batch_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    init_staging_schema(con)
    query = """
        SELECT c.*, r.provider, r.external_event_id, r.occurred_at
        FROM import_candidates c
        JOIN raw_import_events r ON c.raw_event_id = r.id
        WHERE 1=1
    """
    params = []
    if status:
        query += " AND c.review_status = ?"
        params.append(status)
    if batch_id:
        query += " AND r.batch_id = ?"
        params.append(batch_id)

    query += " ORDER BY c.id DESC LIMIT ?"
    params.append(limit)

    rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_import_candidate_by_id(con: sqlite3.Connection, candidate_id: int) -> dict | None:
    init_staging_schema(con)
    row = con.execute("""
        SELECT c.*, r.provider, r.external_event_id, r.occurred_at, r.minimal_raw_payload
        FROM import_candidates c
        JOIN raw_import_events r ON c.raw_event_id = r.id
        WHERE c.id = ?
    """, (candidate_id,)).fetchone()
    return dict(row) if row else None
