import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-taskC-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

rows = cur.execute("""
    SELECT id, date, amount FROM transactions
    WHERE description LIKE '%Dana masuk dari pihak lain%' AND is_deleted = 0
    ORDER BY date
""").fetchall()

matched = 0
used_stmt = set()
for tid, d, a in rows:
    # Build exclusion string
    excl = ",".join(str(x) for x in used_stmt) if used_stmt else "0"
    other = cur.execute(f"""
        SELECT id, description FROM transactions
        WHERE date = ? AND amount = ? AND id != ?
          AND source_refs LIKE 'bca_statement%' AND is_deleted = 0
          AND id NOT IN ({excl})
        LIMIT 1
    """, (d, a, tid)).fetchone()
    
    if other:
        oid, odesc = other
        used_stmt.add(oid)
        cur.execute("""
            UPDATE transactions
            SET category = 'Duplikat (Evidence)',
                transaction_type = 'Transfer',
                exclude_from_budget = 1,
                budget_exclusion_reason = ?,
                notes = COALESCE(notes,'') || ?,
                updated_at = ?
            WHERE id = ?
        """, (f'Duplikat dengan BCA statement #{oid}',
              f' | [TASK-C: dup of #{oid}]',
              now, tid))
        matched += 1

con.commit()
print(f"Demoted (matched with stmt): {matched}")
print()

n = cur.execute("""
    SELECT COUNT(*) FROM transactions
    WHERE description LIKE '%Dana masuk dari pihak lain%' AND is_deleted = 0
""").fetchone()[0]
print(f"Sisa 'Dana masuk dari pihak lain': {n} tx")

if n > 0:
    print()
    print("=== Sisa (butuh review manual) ===")
    for tid, d, a in cur.execute("""
        SELECT id, date, amount FROM transactions
        WHERE description LIKE '%Dana masuk dari pihak lain%' AND is_deleted = 0
        ORDER BY date
    """):
        print(f"  id={tid} {d} Rp{float(a):>10,.0f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
