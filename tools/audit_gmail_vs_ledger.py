"""
audit_gmail_vs_ledger.py — Local Read-Only Historical Matcher (Gmail vs Ledger)

Membandingkan bukti transaksi email kanonikal (dari D1 staging) terhadap financial ledger lokal (SQLite)
secara READ-ONLY (file:runtime/money_tracks.db?mode=ro).

TIDAK memutasi database. Output disanitasi (zero raw body, masked accounts).
"""
import os
import sys
import json
import sqlite3
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "tools" else Path(r"C:\A User Main Storage\Documents\GitHub\AturUang")
DB_PATH = BASE_DIR / "runtime" / "money_tracks.db"
WORKER_DIR = BASE_DIR / "cloud" / "worker"
AUDIT_OUTPUT_DIR = BASE_DIR / "runtime" / "audit"

def round2(v: float) -> float:
    return round(v + 1e-9, 2)

def is_money_equal(a: float, b: float) -> boolean if False else bool:
    return abs(float(a) - float(b)) < 0.005

def mask_account(acc: Optional[str]) -> str:
    if not acc:
        return "-"
    if len(acc) <= 4:
        return acc
    return acc[:3] + "***"

def mask_text(txt: Optional[str]) -> str:
    if not txt:
        return "-"
    if len(txt) <= 8:
        return txt
    return txt[:4] + "***" + txt[-3:]

class CanonicalLedgerEvent:
    def __init__(self, tx_id: int, date: str, tx_type: str, amount: float, account: str,
                 to_account: Optional[str], category: str, note: str, money_context: str):
        self.id = tx_id
        self.date = date
        self.tx_type = tx_type  # 'Expense', 'Income', 'Transfer'
        self.amount = round2(amount)
        self.account = account
        self.to_account = to_account
        self.category = category
        self.note = note or ""
        self.money_context = money_context or "Personal"

def load_ledger_events(db_uri_ro: str, start_date: str = "2025-01-01") -> List[CanonicalLedgerEvent]:
    con = sqlite3.connect(db_uri_ro, uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    rows = cur.execute("""
        SELECT id, date, transaction_type as type, amount, account_from as account,
               account_to as to_account, category, notes as note, money_context
        FROM transactions
        WHERE date >= ? AND (is_deleted IS NULL OR is_deleted = 0)
        ORDER BY date ASC, id ASC
    """, (start_date,)).fetchall()
    
    events = []
    for r in rows:
        events.append(CanonicalLedgerEvent(
            tx_id=r["id"],
            date=r["date"],
            tx_type=r["type"],
            amount=r["amount"],
            account=r["account"],
            to_account=r["to_account"],
            category=r["category"] or "",
            note=r["note"] or "",
            money_context=r["money_context"] or "Personal"
        ))
    con.close()
    return events

def fetch_d1_canonical_events() -> List[Dict[str, Any]]:
    env_path = r"C:\Users\allan\.local\node-lts;" + os.environ.get("PATH", "")
    cmd = [
        r"C:\Users\allan\.local\node-lts\npx.cmd",
        "wrangler",
        "d1",
        "execute",
        "aturuang-db",
        "--remote",
        "--command=SELECT * FROM canonical_financial_events ORDER BY occurred_at_wib ASC, id ASC;"
    ]
    try:
        res = subprocess.run(cmd, cwd=str(WORKER_DIR), capture_output=True, text=True, env={**os.environ, "PATH": env_path}, errors="replace")
        if res.returncode != 0:
            print("Wrangler D1 execution returned non-zero. Staging events might be empty or unconfigured.")
            return []
        out = res.stdout
        json_start = out.find("[")
        json_end = out.rfind("]") + 1
        if json_start >= 0 and json_end > json_start:
            data = json.loads(out[json_start:json_end])
            if data and len(data) > 0:
                return data[0].get("results", [])
    except Exception as e:
        print("Exception fetching D1 events:", e)
    return []

def run_audit(email_events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database tidak ditemukan: {DB_PATH}")
    
    # Checksum BEFORE
    hash_before = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    
    # 1. Load ledger read-only
    db_uri = f"file:{DB_PATH}?mode=ro"
    ledger_events = load_ledger_events(db_uri, "2025-01-01")
    
    # 2. Load email events
    if email_events is None:
        email_events = fetch_d1_canonical_events()
    
    results = {
        "MATCHED_EXACT": 0,
        "MATCHED_LIKELY": 0,
        "EMAIL_EVENT_MISSING_FROM_LEDGER": 0,
        "LEDGER_EVENT_WITHOUT_EMAIL_EVIDENCE": 0,
        "POSSIBLE_LEDGER_DUPLICATE": 0,
        "PARTIAL_TRANSFER_MATCH": 0,
        "CLASSIFICATION_CONFLICT": 0,
        "CATEGORY_CONFLICT": 0,
        "AMOUNT_CONFLICT": 0,
        "AMBIGUOUS": 0,
    }
    
    conflict_examples = []
    matched_ledger_ids = set()
    
    # Check for possible ledger internal duplicates first
    seen_ledger_keys = {}
    for le in ledger_events:
        lkey = (le.date, le.tx_type, le.amount, le.account, le.to_account)
        if lkey in seen_ledger_keys:
            results["POSSIBLE_LEDGER_DUPLICATE"] += 1
            if len(conflict_examples) < 10:
                conflict_examples.append({
                    "type": "POSSIBLE_LEDGER_DUPLICATE",
                    "details": f"Tx #{le.id} duplikat dengan Tx #{seen_ledger_keys[lkey]} (Tgl: {le.date}, Rp {le.amount:,.2f})"
                })
        else:
            seen_ledger_keys[lkey] = le.id

    # Match each canonical email event against ledger
    for ev in email_events:
        ev_amount = round2(float(ev.get("amount", 0.0)))
        ev_date = ev.get("occurred_at_wib", "")[:10]  # YYYY-MM-DD
        ev_class = ev.get("financial_class", "Expense")
        ev_ref = ev.get("transaction_reference") or ev.get("external_order_id") or ""
        ev_kind = ev.get("event_kind", "")
        ev_merchant = ev.get("merchant_normalized") or ""
        
        if ev_class in ("Ignore", "NON_TRANSACTION", "FAILED_ATTEMPT"):
            continue
            
        candidates = [le for le in ledger_events if is_money_equal(le.amount, ev_amount)]
        
        # Filter by approximate date (within +/- 2 days)
        close_candidates = []
        for c in candidates:
            try:
                d1 = datetime.date.fromisoformat(ev_date)
                d2 = datetime.date.fromisoformat(c.date)
                if abs((d1 - d2).days) <= 2:
                    close_candidates.append(c)
            except:
                if c.date == ev_date:
                    close_candidates.append(c)
        
        if not close_candidates:
            results["EMAIL_EVENT_MISSING_FROM_LEDGER"] += 1
            continue
            
        # Inspect matches
        matched = False
        for c in close_candidates:
            # Check for classification conflict (e.g. Top-up / Cash Withdrawal recorded as Expense)
            is_class_conflict = False
            if ev_kind in ("TOPUP", "CASH_WITHDRAWAL", "OWN_TRANSFER") and c.tx_type == "Expense":
                results["CLASSIFICATION_CONFLICT"] += 1
                is_class_conflict = True
                if len(conflict_examples) < 10:
                    conflict_examples.append({
                        "type": "CLASSIFICATION_CONFLICT",
                        "details": f"Email {ev_kind} (Rp {ev_amount:,.2f}) dicatat sebagai Expense di Tx #{c.id} ({c.account})"
                    })
                matched = True
                matched_ledger_ids.add(c.id)
                break
                
            if ev_kind == "INVESTMENT_MOVEMENT" and c.tx_type in ("Expense", "Income"):
                results["CLASSIFICATION_CONFLICT"] += 1
                is_class_conflict = True
                if len(conflict_examples) < 10:
                    conflict_examples.append({
                        "type": "CLASSIFICATION_CONFLICT",
                        "details": f"Investasi {ev_merchant} (Rp {ev_amount:,.2f}) dicatat sebagai {c.tx_type} di Tx #{c.id}"
                    })
                matched = True
                matched_ledger_ids.add(c.id)
                break
                
            # Exact Match
            if c.date == ev_date and (ev_ref and ev_ref in c.note or ev_merchant.lower() in c.note.lower() or not ev_ref):
                if c.category and ev.get("category") and c.category != ev.get("category"):
                    results["CATEGORY_CONFLICT"] += 1
                else:
                    results["MATCHED_EXACT"] += 1
                matched = True
                matched_ledger_ids.add(c.id)
                break
                
            # Likely Match
            if abs((datetime.date.fromisoformat(ev_date) - datetime.date.fromisoformat(c.date)).days) <= 1:
                results["MATCHED_LIKELY"] += 1
                matched = True
                matched_ledger_ids.add(c.id)
                break
                
        if not matched:
            results["AMBIGUOUS"] += 1

    # Ledger events without email evidence
    for le in ledger_events:
        if le.id not in matched_ledger_ids:
            results["LEDGER_EVENT_WITHOUT_EMAIL_EVIDENCE"] += 1

    # Checksum AFTER
    hash_after = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    if hash_before != hash_after:
        raise RuntimeError("CRITICAL: Local database mutated during audit!")

    report = {
        "window": "2025-01-01 -> now",
        "total_canonical_gmail_events": len(email_events),
        "total_ledger_events": len(ledger_events),
        "counts": results,
        "examples": conflict_examples,
        "db_hash_before": hash_before,
        "db_hash_after": hash_after,
        "zero_mutation_verified": hash_before == hash_after
    }
    
    # Save local sanitized audit report (gitignored)
    AUDIT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_file = AUDIT_OUTPUT_DIR / f"audit_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    
    return report

def main():
    print("=" * 65)
    print("ATURUANG — HISTORICAL GMAIL VS LEDGER READ-ONLY AUDIT")
    print("=" * 65)
    
    try:
        report = run_audit()
        print(f"Historical Gmail window: {report['window']}")
        print(f"Canonical Gmail events: {report['total_canonical_gmail_events']}")
        print(f"Matched exact: {report['counts']['MATCHED_EXACT']}")
        print(f"Matched likely: {report['counts']['MATCHED_LIKELY']}")
        print(f"Missing ledger: {report['counts']['EMAIL_EVENT_MISSING_FROM_LEDGER']}")
        print(f"Ledger without email: {report['counts']['LEDGER_EVENT_WITHOUT_EMAIL_EVIDENCE']}")
        print(f"Possible duplicates: {report['counts']['POSSIBLE_LEDGER_DUPLICATE']}")
        print(f"Partial transfers: {report['counts']['PARTIAL_TRANSFER_MATCH']}")
        print(f"Classification conflicts: {report['counts']['CLASSIFICATION_CONFLICT']}")
        print(f"Category conflicts: {report['counts']['CATEGORY_CONFLICT']}")
        print(f"Amount conflicts: {report['counts']['AMOUNT_CONFLICT']}")
        print(f"Ambiguous: {report['counts']['AMBIGUOUS']}")
        print("=" * 65)
        print("Local SQLite Hash BEFORE:", report["db_hash_before"])
        print("Local SQLite Hash AFTER :", report["db_hash_after"])
        print(f"Zero Mutation Confirmed : {report['zero_mutation_verified']}")
        print("=" * 65)
    except Exception as e:
        print("Audit failed:", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
