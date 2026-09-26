import sys
sys.path.insert(0, r"C:\A User Main Storage\Documents\GitHub\AturUang")

from pathlib import Path
from hashlib import sha256
import sqlite3
import datetime as dt

from aturuang.ingestion_adapter import (
    AdapterInput, SourceChannel, TemplateMatchStatus, PeriodStatus,
)
from aturuang.ingestion_stockbit_adapter import (
    StockbitStatementAdapter,
    STOCKBIT_SOURCE_REGISTRY_ID,
    STOCKBIT_TEMPLATE_ID,
    STOCKBIT_PARSER_VERSION,
)

APPLY = "--apply" in sys.argv

SOA_DIR = Path(r"H:\My Drive\Money Tracks\Stockbit\Statement of Account")

adapter = StockbitStatementAdapter()
con = sqlite3.connect("runtime/money_tracks.db")
cur = con.cursor()

files = sorted(SOA_DIR.glob("*.pdf"))
print(f"Files: {len(files)}")

all_rows = []
total_inserted = 0
total_amount = 0.0

for pdf in files:
    binary = pdf.read_bytes()
    content_sha = sha256(binary).hexdigest()

    inp = AdapterInput(
        source_document_id=f"stockbit-soa-{content_sha[:12]}",
        content_sha256=content_sha,
        source_registry_id=STOCKBIT_SOURCE_REGISTRY_ID,
        template_id=STOCKBIT_TEMPLATE_ID,
        parser_version=STOCKBIT_PARSER_VERSION,
        source_channel=SourceChannel.PDF,
        template_match_status=TemplateMatchStatus.KNOWN,
        period_status=PeriodStatus.CLOSED,
        binary_payload=binary,
    )

    result = adapter.parse(inp)
    cash = [ev for ev in result.events if ev.event_role.name == "CASH_MOVEMENT"]

    if not cash:
        print(f"  {pdf.name}: 0 events (skip)")
        continue

    period = result.period_start[:7] if result.period_start else "unknown"
    import_id = f"stockbit_soa:{period}:{content_sha[:16]}"

    for ev in cash:
        p = ev.payload
        amt = float(p.amount) if p.amount else 0.0
        direction = p.direction.name if p.direction else "UNKNOWN"

        occurred = p.occurred_at or p.posted_at or result.period_start
        if hasattr(occurred, 'isoformat'):
            s = occurred.isoformat()
        else:
            s = str(occurred) if occurred else f"{period}-01"
        if 'T' in s:
            date_str, time_str = s.split('T', 1)
            time_str = time_str[:8]
        else:
            date_str = s
            time_str = "00:00:00"

        if direction == "INFLOW":
            tx_type = "Income"
        elif direction == "OUTFLOW":
            tx_type = "Expense"
        else:
            tx_type = "Transfer"

        desc = (getattr(p, 'description_raw', None) or "Stockbit SOA row")[:200]

        all_rows.append({
            "date": date_str,
            "time": time_str,
            "transaction_type": tx_type,
            "amount": amt,
            "account_from": "Stockbit RDN" if tx_type == "Expense" else "",
            "account_to": "Stockbit RDN" if tx_type == "Income" else "",
            "description": f"[Stockbit SOA] {desc}",
            "category": "Investment Movement",
            "for_with_whom": "Personal / Self",
            "money_context": "Personal",
            "settlement_kind": "",
            "status": "Confirmed",
            "confidence": "medium",
            "budget_effect": 0.0,
            "exclude_from_budget": 1,
            "budget_exclusion_reason": "Investment movement — owned to owned (V3-STEP-100D)",
            "budget_rule_version": "legacy",
            "subtype": "",
            "source_refs": import_id,
            "notes": f"[V3-STEP-100D: Stockbit SOA {period}; sha={content_sha[:16]}]",
            "manual_edited": 0,
            "is_deleted": 0,
            "created_at": dt.datetime.now().isoformat(timespec='seconds'),
            "updated_at": dt.datetime.now().isoformat(timespec='seconds'),
        })

    print(f"  {pdf.name[:45]:<47} {len(cash):<4} Rp{sum(float(ev.payload.amount) for ev in cash):>12,.2f}")
    total_inserted += len(cash)
    total_amount += sum(float(ev.payload.amount) for ev in cash)

print()
print(f"TOTAL: {total_inserted} row, Rp{total_amount:,.2f}")

if APPLY:
    print()
    print("=== APPLYING ===")
    for r in all_rows:
        cur.execute("""
            INSERT INTO transactions (
                canonical_id, date, time, transaction_type, amount,
                account_from, account_to, description, category, for_with_whom,
                money_context, settlement_kind, status, confidence, budget_effect,
                exclude_from_budget, budget_exclusion_reason, budget_rule_version,
                subtype, source_refs, notes, manual_edited, is_deleted,
                created_at, updated_at
            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r['date'], r['time'], r['transaction_type'], r['amount'],
            r['account_from'], r['account_to'], r['description'], r['category'], r['for_with_whom'],
            r['money_context'], r['settlement_kind'], r['status'], r['confidence'], r['budget_effect'],
            r['exclude_from_budget'], r['budget_exclusion_reason'], r['budget_rule_version'],
            r['subtype'], r['source_refs'], r['notes'], r['manual_edited'], r['is_deleted'],
            r['created_at'], r['updated_at'],
        ))
    con.commit()
    print(f"Inserted {len(all_rows)} rows")

    r = cur.execute("""
        SELECT COUNT(*) as n, SUM(amount) as t
        FROM transactions WHERE source_refs LIKE 'stockbit_soa%' AND is_deleted=0
    """).fetchone()
    print(f"Verify: {r[0]} row, Rp{r[1]:,.2f}")

    print()
    print("INTEGRITY:", cur.execute("PRAGMA integrity_check").fetchone()[0])
else:
    print()
    print("DRY-RUN ONLY — jalankan dengan --apply")

con.close()
