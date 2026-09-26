"""
bca_reparse_v2.py — selective per-file BCA PDF parser, pakai production adapter.

Import BCAMonthlyStatementAdapter dari production AturUang (read-only).
Output: events[role=CASH_MOVEMENT] → INSERT/UPDATE ke activation DB.

Usage:
    python bca_reparse_v2.py                            # dry-run folder
    python bca_reparse_v2.py --apply
    python bca_reparse_v2.py --file PATH --apply
    python bca_reparse_v2.py --dir CUSTOM --apply
"""
import sys, argparse, sqlite3, shutil, hashlib, datetime as dt
from pathlib import Path

# Import adapter dari production
PROD_ROOT = r"C:\A User Main Storage\Documents\GitHub\AturUang"
sys.path.insert(0, PROD_ROOT)

from aturuang.ingestion_adapter import (
    AdapterInput, SourceChannel, TemplateMatchStatus, PeriodStatus,
    EventRole, EventDirection, SourceEventStatus,
)
from aturuang.ingestion_bca_adapter import (
    BCAMonthlyStatementAdapter, BCA_DESCRIPTOR, BCA_TEMPLATE_FINGERPRINT,
)

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
DEFAULT_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi Rekening BCA")


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_pdf_adapter(path):
    """Return list of CASH_MOVEMENT events (dict) dari production adapter."""
    binary = path.read_bytes()
    content_sha = hashlib.sha256(binary).hexdigest()
    
    inp = AdapterInput(
        source_document_id=f"bca-{content_sha[:12]}",
        content_sha256=content_sha,
        source_registry_id=BCA_DESCRIPTOR.source_registry_id,
        template_id=BCA_DESCRIPTOR.template_id,
        parser_version=BCA_DESCRIPTOR.parser_version,
        source_channel=SourceChannel.PDF,
        template_match_status=TemplateMatchStatus.KNOWN,
        period_status=PeriodStatus.CLOSED,
        template_fingerprint=BCA_TEMPLATE_FINGERPRINT,
        binary_payload=binary,
    )
    
    adapter = BCAMonthlyStatementAdapter()
    result = adapter.parse(inp)
    
    if str(result.parse_status) != "AdapterParseStatus.COMPLETED":
        return [], result, f"parse_status={result.parse_status}"
    
    rows = []
    for ev in result.events:
        if ev.event_role != EventRole.CASH_MOVEMENT:
            continue
        p = ev.payload
        
        amount = float(p.amount)
        direction = "Income" if p.direction == EventDirection.INFLOW else "Expense"
        if p.direction == EventDirection.NEUTRAL:
            direction = "Transfer"
        
        # Tanggal
        date = p.posted_at or p.occurred_at or result.period_start
        if not date:
            continue
        
        # Deskripsi
        desc_raw = p.description_raw or ""
        desc = f"[BCA Statement] {desc_raw}"
        
        # Deteksi line_type dari description (untuk rules)
        rows.append({
            "date": date,
            "amount": amount,
            "direction": direction,
            "description": desc[:300],
            "raw_desc": desc_raw,
            "source_file": path.name,
            "content_sha": content_sha[:16],
            "period": f"{result.period_start[:7]}" if result.period_start else "unknown",
        })
    
    return rows, result, None


def apply_rows(cur, rows):
    """INSERT rows yang belum ada, UPDATE yang ada. Return (ins, upd, skip)."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    ins = upd = skip = 0
    used_ids = set()
    
    for r in rows:
        # Cek existing by (date, amount, raw_desc prefix)
        existing = cur.execute("""
            SELECT id, description FROM transactions
            WHERE source_refs LIKE 'bca_statement%' AND is_deleted = 0
              AND date = ? AND amount = ?
            ORDER BY id
        """, (r["date"], r["amount"])).fetchall()
        
        match = None
        for tid, desc_db in existing:
            if tid not in used_ids:
                match = (tid, desc_db)
                break
        
        if match:
            tid, old_desc = match
            used_ids.add(tid)
            if old_desc != r["description"]:
                cur.execute("""
                    UPDATE transactions
                    SET description = ?,
                        notes = COALESCE(notes,'') || ' | [BCA v2: ' || ? || ']',
                        updated_at = ?
                    WHERE id = ?
                """, (r["description"], r["source_file"], now, tid))
                upd += 1
            else:
                skip += 1
        else:
            import_id = f"bca_statement:{r['period']}:{r['content_sha']}"
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
                r["date"], "00:00:00", r["direction"], r["amount"],
                "BCA" if r["direction"] == "Expense" else "",
                "BCA" if r["direction"] == "Income" else "",
                r["description"], "Other / Miscellaneous", "Personal / Self",
                "Personal", "", "Confirmed", "high", 0.0,
                1, "BCA statement (pending rules)", "legacy", "", import_id,
                f"[BCA v2: {r['source_file']}]",
                0, 0, now, now,
            ))
            ins += 1
    
    return ins, upd, skip


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--file", type=str)
    p.add_argument("--files", nargs="+")
    p.add_argument("--dir", type=str, default=str(DEFAULT_DIR))
    args = p.parse_args()
    
    if args.file:
        files = [Path(args.file)]
    elif args.files:
        files = [Path(f) for f in args.files]
    else:
        folder = Path(args.dir)
        if not folder.exists():
            print(f"Folder tidak ada: {folder}")
            sys.exit(1)
        files = sorted([f for f in folder.glob("*.pdf")
                        if "_archive" not in str(f)])
    
    # Dedup by file hash
    seen = {}
    unique = []
    for f in files:
        h = file_hash(f)
        if h in seen: continue
        seen[h] = f
        unique.append(f)
    files = unique
    
    print(f"Files: {len(files)}")
    print()
    
    all_rows = []
    global_seen = set()
    for f in files:
        rows, result, err = parse_pdf_adapter(f)
        rows_unique = []
        for r in rows:
            key = (r["date"], r["amount"], r["raw_desc"])
            if key in global_seen: continue
            global_seen.add(key)
            rows_unique.append(r)
        all_rows.extend(rows_unique)
        marker = f"  [dedup: -{len(rows)-len(rows_unique)}]" if len(rows) != len(rows_unique) else ""
        print(f"  {f.name[:50]:<52} {len(rows_unique)}{marker}")
    
    print()
    print(f"Total parsed: {len(all_rows)}")
    print()
    
    if not args.apply:
        print("DRY-RUN. Pakai --apply untuk insert.")
        return
    
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"{DB.name}.bak-bcav2-{ts}")
    shutil.copy2(DB, bak)
    print(f"Backup: {bak.name}")
    print()
    
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    ti = tu = tsk = 0
    for r in all_rows:
        i, u, s = apply_rows(cur, [r])
        ti += i; tu += u; tsk += s
    con.commit()
    
    n_db = cur.execute("""
        SELECT COUNT(*) FROM transactions
        WHERE source_refs LIKE 'bca_statement%' AND is_deleted = 0
    """).fetchone()[0]
    
    print(f"Inserted: {ti}")
    print(f"Updated : {tu}")
    print(f"Skipped : {tsk}")
    print(f"Total BCA di DB: {n_db}")
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()