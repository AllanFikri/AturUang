import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixE-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. Jago RDN related -> Investment Movement ===
print("=== 1. Jago RDN ===")
rules_invest = [
    ("%Jago Statement%Pembelian Saham%", "Investment Movement", "Transfer", 1),
    ("%Jago Statement%Dana Masuk%{botd}%", "Investment Movement", "Transfer", 1),
    ("%Jago Statement%Penjualan Saham%", "Investment Movement", "Transfer", 1),
    ("%Jago Statement%Pajak%Bunga%", "Investment Movement", "Transfer", 1),
    ("%Jago Statement%Bunga%Stockbit%", "Investment Movement", "Transfer", 1),
]
for pat, cat, ttype, excl in rules_invest:
    cur.execute("""
        UPDATE transactions
        SET category = ?, transaction_type = ?, exclude_from_budget = ?,
            budget_exclusion_reason = 'Jago RDN Stockbit',
            notes = COALESCE(notes,'') || ' | [V3-STEP-105: jago rdn]',
            updated_at = ?
        WHERE description LIKE ? AND category = 'Other / Miscellaneous' AND is_deleted = 0
    """, (cat, ttype, excl, now, pat))
    if cur.rowcount > 0: print(f"  {pat[:50]:<52} -> {cat} ({cur.rowcount})")

# === 2. Jago "Tambah Uang Kantong" -> Allocation Movement ===
print()
print("=== 2. Jago inter-kantong ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Allocation Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Inter-kantong Jago',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: inter-kantong]',
        updated_at = ?
    WHERE description LIKE '%Tambah Uang Kantong%' 
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Tambah Uang Kantong: {cur.rowcount}")

cur.execute("""
    UPDATE transactions
    SET category = 'Allocation Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Inter-kantong Jago',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: inter-kantong]',
        updated_at = ?
    WHERE description LIKE '%Pindah uang antar Kantong%'
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Pindah uang antar Kantong: {cur.rowcount}")

# === 3. BCA self (SETORAN VIA CDM, BI-FAST ALLAN) -> Allocation ===
print()
print("=== 3. BCA self-transfer ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Allocation Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: self-transfer]',
        updated_at = ?
    WHERE description LIKE '%SETORAN VIA CDM%ALLAN%' AND is_deleted = 0
      AND category = 'Other / Miscellaneous'
""", (now,))
print(f"  SETORAN VIA CDM (self): {cur.rowcount}")

# === 4. Beasiswa BPDPKS ===
print()
print("=== 4. Beasiswa BPDPKS ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Beasiswa',
        transaction_type = 'Income',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: beasiswa BPDPKS]',
        updated_at = ?
    WHERE description LIKE '%BPDPKS%' AND is_deleted = 0
      AND category = 'Other / Miscellaneous'
""", (now,))
print(f"  Beasiswa BPDPKS: {cur.rowcount}")

# === 5. QRIS round 3 (sisa yang belum ke-handle) ===
print()
print("=== 5. QRIS round 3 ===")
QRIS_MAP = [
    ("NKEDAI", "Makanan Berat"),
    ("NDJAYA", "Makanan Berat"),
    ("NWarkop", "Makanan Berat"),
    ("NHisana", "Makanan Berat"),
    ("Nkremes", "Makanan Berat"),
    ("NAROMAT", "Cafe & Minuman"),
    ("NSOLARI", "Makanan Berat"),
    ("NBCS EL", "Toko Retail"),
    ("NSING J", "Toko Retail"),
    ("NJAKART", "Makanan Berat"),
    ("NMC MAL", "Belanja Online"),
    ("NSiberP", "Fotokopi & ATK"),
    ("NCENTRA", "Otomotif"),
    ("NPAGI S", "Makanan Berat"),
    ("NSARJANA", "Fotokopi & ATK"),
    ("NKS 24", "Fotokopi & ATK"),
    ("NGoro", "Makanan Berat"),
    ("NALFAGIFT", "Grocery"),
    ("NSUPER CHICKEN", "Makanan Berat"),
    ("NSTUDIO", "Cafe & Minuman"),
    ("NROPI", "Cafe & Minuman"),
    ("NSoto", "Makanan Berat"),
    ("NBURGER", "Makanan Berat"),
    ("NTELOR", "Snacks & Jajan"),
    ("NDapur", "Makanan Berat"),
    ("NWarung", "Makanan Berat"),
    ("NTahu", "Makanan Berat"),
    ("NMie", "Makanan Berat"),
    ("NSushi", "Makanan Berat"),
    ("NUNIQLO", "Belanja Online"),
    ("NSens", "Toko Retail"),
    ("NCelyna", "Belanja Online"),
    ("NPokang", "Makanan Berat"),
    ("NKFC", "Makanan Berat"),
    ("NMCD", "Makanan Berat"),
    ("NAstronaut", "Cafe & Minuman"),
]
applied = 0
for kw, cat in QRIS_MAP:
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes,'') || ' | [V3-STEP-105: qris3]',
            updated_at = ?
        WHERE description LIKE ?
          AND category = 'Other / Miscellaneous'
          AND is_deleted = 0
    """, (cat, now, f"%{kw}%"))
    applied += cur.rowcount
print(f"  QRIS round 3: {applied}")

# === 6. SRI PUDJIASTUTI -> Transfer ke Pihak Lain ===
cur.execute("""
    UPDATE transactions
    SET category = 'Transfer ke Pihak Lain',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: transfer pihak lain]',
        updated_at = ?
    WHERE description LIKE '%SRI PUDJIASTUTI%' 
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Sri Pudjiastuti: {cur.rowcount}")

con.commit()
print()

# Verify
print("=== Top 20 kategori aktif ===")
for cat, ttype, n, tot in cur.execute("""
    SELECT category, transaction_type, COUNT(*), SUM(amount)
    FROM transactions
    WHERE is_deleted = 0 AND exclude_from_budget = 0
    GROUP BY category, transaction_type
    ORDER BY SUM(amount) DESC LIMIT 20
"""):
    print(f"  {str(cat)[:35]:<37} {ttype:<10} {n:>4}  Rp{tot:>14,.2f}")

print()
n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc sisa: Expense {n_exp} tx, Income {n_inc} tx")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
