import sqlite3, shutil, json, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixD-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. SEED Budi Sarosa (mama) ===
print("=== 1. Seed Budi Sarosa (mama) ===")
try:
    cur.execute("""
        INSERT INTO counterparty_accounts
        (bank, account_number, aliases, owner_name, relation, is_internal, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("BCA", "BUDI_SAROSA", "[]", "Budi Sarosa", "family", 0,
          "Rekening yang dipegang mama", now, now))
    print("  Inserted: Budi Sarosa (family)")
except sqlite3.IntegrityError:
    print("  Already exists")

# === 2. Reclass tx Budi Sarosa ===
print()
print("=== 2. Reclass tx Budi Sarosa ===")

# Preview
rows = cur.execute("""
    SELECT id, date, amount, transaction_type, description FROM transactions
    WHERE description LIKE '%BUDI SAR%' AND is_deleted = 0
""").fetchall()

print(f"Total: {len(rows)} tx")
for tid, d, a, ttype, desc in rows:
    print(f"  id={tid} {d} {ttype:<8} Rp{float(a):>10,.0f}  {desc[:80]}")

# Update
cur.execute("""
    UPDATE transactions
    SET category = CASE 
            WHEN transaction_type = 'Income' THEN 'Penerimaan dari Keluarga'
            ELSE 'Transfer ke Keluarga'
        END,
        notes = COALESCE(notes, '') || ' | [V3-STEP-103: Budi Sarosa = mama]',
        updated_at = ?
    WHERE description LIKE '%BUDI SAR%' AND is_deleted = 0
""", (now,))
print(f"  Reclassified: {cur.rowcount} tx")

con.commit()
print()

# === 3. Cek Jago source untuk reparse ===
print("=== 3. Cek Jago statement source ===")
# Cari apakah ada file di disk
import os
candidates = [
    r"H:\My Drive\Money Tracks",
    r"C:\A User Main Storage\Downloads",
    r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\scripts",
]
for base in candidates:
    if not os.path.exists(base): continue
    for root, dirs, files in os.walk(base):
        # Skip .git
        if ".git" in root: continue
        for f in files:
            if "jago" in f.lower() and (f.endswith(".csv") or f.endswith(".pdf") or f.endswith(".json") or f.endswith(".txt")):
                print(f"  Found: {os.path.join(root, f)}")

# Sample 5 statement row
print()
print("=== Sample 5 Jago 'statement row' ===")
for tid, d, a, ttype, notes in cur.execute("""
    SELECT id, date, amount, transaction_type, notes FROM transactions
    WHERE description LIKE '%statement row%' AND is_deleted = 0
    LIMIT 5
"""):
    print(f"  id={tid} {d} {ttype:<8} Rp{float(a):>10,.0f} notes={notes}")

print()
print("=== Sample 5 Jago {botd} Receipt From ===")
for tid, d, a, ttype, notes in cur.execute("""
    SELECT id, date, amount, transaction_type, notes FROM transactions
    WHERE description LIKE '%botd%Receipt%' AND is_deleted = 0
    LIMIT 5
"""):
    print(f"  id={tid} {d} {ttype:<8} Rp{float(a):>10,.0f} notes={notes}")

print()
print("=== Sample 5 Jago {botd} Payment to ===")
for tid, d, a, ttype, notes in cur.execute("""
    SELECT id, date, amount, transaction_type, notes FROM transactions
    WHERE description LIKE '%botd%Payment%' AND is_deleted = 0
    LIMIT 5
"""):
    print(f"  id={tid} {d} {ttype:<8} Rp{float(a):>10,.0f} notes={notes}")

# Cek source_refs pattern Jago
print()
print("=== Source_refs Jago ===")
for src, n in cur.execute("""
    SELECT source_refs, COUNT(*) FROM transactions
    WHERE source_refs LIKE 'jago%' AND is_deleted = 0
    GROUP BY source_refs
"""):
    print(f"  {src:<40} {n} tx")

con.close()
