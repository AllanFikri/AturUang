import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-alloc-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# Preview
print()
print("=== Preview: Allocation Movement exclude=0 ===")
for tid, d, a, desc in cur.execute("""
    SELECT id, date, amount, description FROM transactions
    WHERE category = 'Allocation Movement' AND exclude_from_budget = 0 AND is_deleted = 0
    ORDER BY date LIMIT 30
"""):
    print(f"  id={tid}  {d}  Rp{float(a):>12,.0f}  {desc[:70]}")

# Apply
cur.execute("""
    UPDATE transactions
    SET exclude_from_budget = 1,
        budget_exclusion_reason = 'Allocation Movement (internal transfer)',
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: alloc auto-exclude]',
        updated_at = ?
    WHERE category = 'Allocation Movement' AND exclude_from_budget = 0 AND is_deleted = 0
""", (now,))
print()
print(f"Updated: {cur.rowcount} rows")
con.commit()

# Verify
print()
print("=== Verify ===")
for ttype, excl, n, tot in cur.execute("""
    SELECT transaction_type, exclude_from_budget, COUNT(*), SUM(amount)
    FROM transactions
    WHERE category = 'Allocation Movement' AND is_deleted = 0
    GROUP BY transaction_type, exclude_from_budget
"""):
    print(f"  {ttype:<10} excl={excl}  {n:>4} tx  Rp{tot:>14,.2f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
