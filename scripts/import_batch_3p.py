import sys
sys.path.insert(0, r"C:\A User Main Storage\Documents\GitHub\AturUang")

from pathlib import Path
from hashlib import sha256
import sqlite3
import datetime as dt

from aturuang.ingestion_adapter import (
    AdapterInput, SourceChannel, TemplateMatchStatus, PeriodStatus,
)
from aturuang.ingestion_jago_adapter import (
    JagoMonthlyStatementAdapter,
    JAGO_SOURCE_REGISTRY_ID, JAGO_TEMPLATE_ID, JAGO_PARSER_VERSION,
)
from aturuang.ingestion_seabank_adapter import (
    SeaBankMonthlyStatementAdapter,
    SEABANK_SOURCE_REGISTRY_ID, SEABANK_TEMPLATE_ID, SEABANK_PARSER_VERSION,
)
from aturuang.ingestion_gopay_adapter import (
    GoPayEStatementAdapter,
    GOPAY_SOURCE_REGISTRY_ID, GOPAY_TEMPLATE_ID, GOPAY_PARSER_VERSION,
)

APPLY = "--apply" in sys.argv

BASE = Path(r"C:\A User Main Storage\Downloads\Mutasi Rekening All Bank")

PROVIDERS = [
    {
        "name": "Jago",
        "label_prefix": "jago_statement",
        "dir": BASE / "Mutasi Rekening Jago-20260827T171525Z-1-001" / "Mutasi Rekening Jago",
        "adapter": JagoMonthlyStatementAdapter(),
        "sr_id": JAGO_SOURCE_REGISTRY_ID,
        "tm_id": JAGO_TEMPLATE_ID,
        "pv": JAGO_PARSER_VERSION,
        "account": "Jago Main",
    },
    {
        "name": "SeaBank",
        "label_prefix": "seabank_statement",
        "dir": BASE / "Mutasi Seabank-20260827T170905Z-1-001" / "Mutasi Seabank",
        "adapter": SeaBankMonthlyStatementAdapter(),
        "sr_id": SEABANK_SOURCE_REGISTRY_ID,
        "tm_id": SEABANK_TEMPLATE_ID,
        "pv": SEABANK_PARSER_VERSION,
        "account": "SeaBank",
    },
    {
        "name": "GoPay",
        "label_prefix": "gopay_statement",
        "dir": BASE / "Gopay-20260827T171715Z-1-001" / "Gopay",
        "adapter": GoPayEStatementAdapter(),
        "sr_id": GOPAY_SOURCE_REGISTRY_ID,
        "tm_id": GOPAY_TEMPLATE_ID,
        "pv": GOPAY_PARSER_VERSION,
        "account": "GoPay",
    },
]

con = sqlite3.connect("runtime/money_tracks.db")
cur = con.cursor()

total_inserted = 0
total_amount = 0.0
all_rows = []

seen_hashes = set()
seen_provider_period = set()

for provider in PROVIDERS:
    name = provider["name"]
    print(f"\n=== {name} ===")

    provider_dir = provider["dir"]
    if not provider_dir.exists():
        print(f"  SKIP: dir tidak ada")
        continue

    files = sorted(provider_dir.glob("*.pdf"))
    print(f"  {len(files)} file ditemukan")

    for pdf in files:
        binary = pdf.read_bytes()
        content_sha = sha256(binary).hexdigest()

        if content_sha in seen_hashes:
            print(f"  SKIP (dup sha): {pdf.name}")
            continue
        seen_hashes.add(content_sha)

        inp = AdapterInput(
            source_document_id=f"{provider['label_prefix']}-{content_sha[:12]}",
            content_sha256=content_sha,
            source_registry_id=provider["sr_id"],
            template_id=provider["tm_id"],
            parser_version=provider["pv"],
            source_channel=SourceChannel.PDF,
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            binary_payload=binary,
        )

        try:
            result = provider["adapter"].parse(inp)
        except Exception as e:
            print(f"  ERROR {pdf.name}: {type(e).__name__}: {e}")
            continue

        cash = [ev for ev in result.events if ev.event_role.name == "CASH_MOVEMENT"]

        period = None
        if result.period_start:
            period = result.period_start[:7]
        elif cash:
            dates = [ev.payload.occurred_at for ev in cash if getattr(ev.payload, 'occurred_at', None)]
            if dates:
                period = min(str(d) for d in dates)[:7]

        period_key = (name, period)
        if period and period_key in seen_provider_period:
            print(f"  SKIP (dup period {period}): {pdf.name}")
            continue
        if period:
            seen_provider_period.add(period_key)

        period_label = period or content_sha[:8]
        import_id = f"{provider['label_prefix']}:{period_label}:{content_sha[:16]}"

        month_rows = []
        month_total = 0.0

        for ev in cash:
            p = ev.payload
            amt = float(p.amount) if p.amount else 0.0
            direction = p.direction.name if p.direction else "UNKNOWN"

            posted = getattr(p, 'occurred_at', None) or getattr(p, 'posted_at', None) or period
            if posted is None:
                date_str = f"{period or '2025-01'}-01"
                time_str = "00:00:00"
            else:
                s = posted.isoformat() if hasattr(posted, 'isoformat') else str(posted)
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

            desc = (getattr(p, 'description_raw', None) or "statement row")[:200]

            month_rows.append({
                "date": date_str,
                "time": time_str,
                "transaction_type": tx_type,
                "amount": amt,
                "account_from": provider["account"] if tx_type != "Income" else "",
                "account_to": provider["account"] if tx_type == "Income" else "",
                "description": f"[{name} Statement] {desc}",
                "category": "Other / Miscellaneous",
                "for_with_whom": "Personal / Self",
                "money_context": "Personal",
                "settlement_kind": "",
                "status": "Confirmed",
                "confidence": "medium",
                "budget_effect": 0.0,
                "exclude_from_budget": 1,
                "budget_exclusion_reason": f"Pending reconciliation (V3-STEP-100C, {name})",
                "budget_rule_version": "legacy",
                "subtype": "",
                "source_refs": import_id,
                "notes": f"[V3-STEP-100C: imported from {name} statement {period_label}; sha={content_sha[:16]}]",
                "manual_edited": 0,
                "is_deleted": 0,
                "created_at": dt.datetime.now().isoformat(timespec='seconds'),
                "updated_at": dt.datetime.now().isoformat(timespec='seconds'),
            })
            month_total += amt

        all_rows.extend(month_rows)
        total_inserted += len(month_rows)
        total_amount += month_total

        print(f"  {pdf.name[:55]:<55} {len(month_rows):<4} Rp{month_total:>12,.2f}  {period or '-'}")

print()
print("=" * 70)
print(f"TOTAL: {total_inserted} row, Rp{total_amount:,.2f}")
print("=" * 70)

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

    print()
    print("=== Verify per provider ===")
    for prefix in ('jago_statement', 'seabank_statement', 'gopay_statement'):
        r = cur.execute("""
            SELECT COUNT(*) as n, SUM(amount) as t
            FROM transactions
            WHERE source_refs LIKE ? AND is_deleted=0
        """, (prefix + '%',)).fetchone()
        print(f"  {prefix}: {r[0]} row, Rp{r[1]:,.2f}")

    print()
    print("INTEGRITY:", cur.execute("PRAGMA integrity_check").fetchone()[0])
else:
    print()
    print("DRY-RUN ONLY — jalankan dengan --apply untuk commit")

con.close()
