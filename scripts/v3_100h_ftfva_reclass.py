import sqlite3, shutil, re, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")

ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-ftfva-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()

# VA prefix -> (ttype, category, exclude, reason)
VA_RULES = {
    # E-wallet topup = Transfer / Allocation Movement, exclude from budget
    "12208": ("Transfer", "Allocation Movement", 1, "ShopeePay topup (VA 12208)"),
    "70001": ("Transfer", "Allocation Movement", 1, "GoPay topup (VA 70001)"),
    "39010": ("Transfer", "Allocation Movement", 1, "DANA topup (VA 39010)"),
    # Expense sejati (biarkan, tapi pastikan type/cat/exclude benar)
    "12608": ("Expense", "Belanja Online", 0, "Shopee Order (VA 12608)"),
    "12008": ("Expense", "Belanja Online", 0, "Shopee Order (VA 12008)"),
    "39988": ("Expense", "Pulsa & Data", 0, "BY.U (VA 39988)"),
    "80777": ("Expense", "Belanja Online", 0, "Tokopedia (VA 80777)"),
    "71117": ("Expense", "Internet & Utilitas", 0, "Biznet Home (VA 71117)"),
    "19009": ("Expense", "Langganan Digital", 0, "Google (VA 19009)"),
    "20555": ("Expense", "Belanja Harian", 0, "Klik Indomaret (VA 20555)"),
    # VA 70027 belum jelas -> skip
}

# Get all FTFVA tx
rows = cur.execute("""
    SELECT id, date, amount, transaction_type, category, exclude_from_budget, description
    FROM transactions
    WHERE source_refs LIKE 'bca_statement%' AND description LIKE '%FTFVA%' AND is_deleted = 0
""").fetchall()

print(f"Total FTFVA tx: {len(rows)}")
print()

pattern = re.compile(r"FTFVA/WS\d+\s+(\d+)/")
now = dt.datetime.now().isoformat(timespec="seconds")

preview = []
for tid, date, amount, ttype_old, cat_old, excl_old, desc in rows:
    m = pattern.search(desc)
    if not m: continue
    va = m.group(1)
    if va not in VA_RULES: continue
    
    ttype_new, cat_new, excl_new, reason = VA_RULES[va]
    
    needs_update = (
        ttype_old != ttype_new or
        cat_old != cat_new or
        excl_old != excl_new
    )
    if needs_update:
        preview.append((tid, date, float(amount), va, ttype_old, ttype_new, cat_old, cat_new, excl_old, excl_new, reason))

print(f"Tx perlu update: {len(preview)}")
print()

# Summary
from collections import defaultdict
by_va = defaultdict(lambda: {"n": 0, "total": 0.0})
for p in preview:
    by_va[p[3]]["n"] += 1
    by_va[p[3]]["total"] += p[2]

print(f"{'VA':<8} {'Tx':>4} {'Total':>14}  {'Note'}")
print("-" * 90)
for va in sorted(by_va.keys()):
    v = by_va[va]
    note = VA_RULES[va][3]
    print(f"{va:<8} {v['n']:>4} Rp{v['total']:>14,.2f}  {note}")

print()
print("=== Sample 5 ===")
for p in preview[:5]:
    tid, date, amount, va, to, tn, co, cn, eo, en, reason = p
    print(f"  {date}  Rp{amount:>10,.0f}  VA={va}")
    print(f"    {to} → {tn} | {co} → {cn} | excl {eo} → {en}")

# APPLY
print()
print("=== APPLY ===")
updated = 0
for p in preview:
    tid, date, amount, va, to, tn, co, cn, eo, en, reason = p
    cur.execute("""
        UPDATE transactions
        SET transaction_type = ?,
            category = ?,
            exclude_from_budget = ?,
            budget_exclusion_reason = ?,
            notes = COALESCE(notes, '') || ?,
            updated_at = ?
        WHERE id = ?
    """, (tn, cn, en, reason if en == 1 else "",
          f" | [V3-STEP-100H: reclass by VA {va}]", now, tid))
    updated += 1

con.commit()
print(f"Updated: {updated} rows")
print()

# Verify
print("=== Kategori FTFVA setelah reklasifikasi ===")
for ttype, cat, excl, n, tot in cur.execute("""
    SELECT transaction_type, category, exclude_from_budget, COUNT(*), SUM(amount)
    FROM transactions
    WHERE source_refs LIKE 'bca_statement%' AND description LIKE '%FTFVA%' AND is_deleted = 0
    GROUP BY transaction_type, category, exclude_from_budget
    ORDER BY SUM(amount) DESC
"""):
    print(f"  {ttype:<10} | {cat:<25} | excl={excl} | {n:>3} tx | Rp{tot:>14,.2f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
