import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixC-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. DEDUP Cardless vs ATM ===
print("=== 1. Dedup Cardless vs ATM ===")

cardless = cur.execute("""
    SELECT id, date, amount FROM transactions
    WHERE description LIKE '%BCA Cardless%' AND category = 'Tarik Tunai' AND is_deleted = 0
""").fetchall()

atm = cur.execute("""
    SELECT id, date, amount FROM transactions
    WHERE description LIKE '%TARIKAN ATM%' AND category = 'Tarik Tunai' AND is_deleted = 0
""").fetchall()

# Index ATM by (date, amount)
atm_idx = {}
for tid, d, a in atm:
    key = (d, round(float(a), 2))
    atm_idx.setdefault(key, []).append(tid)

to_demote = []
for tid, d, a in cardless:
    key = (d, round(float(a), 2))
    if key in atm_idx:
        to_demote.append(tid)

print(f"Cardless duplikat (match ATM): {len(to_demote)} tx")

for tid in to_demote:
    cur.execute("""
        UPDATE transactions
        SET is_deleted = 1, exclude_from_budget = 1,
            notes = COALESCE(notes, '') || ' | [V3-STEP-103: dup of ATM statement]',
            updated_at = ?
        WHERE id = ?
    """, (now, tid))

# === 2. BIAYA ADM -> Biaya Bank ===
cur.execute("""
    UPDATE transactions
    SET category = 'Biaya Bank',
        notes = COALESCE(notes, '') || ' | [V3-STEP-103: biaya adm]',
        updated_at = ?
    WHERE description LIKE '%BIAYA ADM%' AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"BIAYA ADM -> Biaya Bank: {cur.rowcount}")

# === 3. PLN PREPAID -> Listrik & Utilitas ===
cur.execute("""
    UPDATE transactions
    SET category = 'Listrik & Utilitas',
        notes = COALESCE(notes, '') || ' | [V3-STEP-103: pln prepaid]',
        updated_at = ?
    WHERE description LIKE '%PLN PREPAID%' AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"PLN PREPAID -> Listrik & Utilitas: {cur.rowcount}")

# === 4. QRIS tambahan ===
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
    ("NPAGI SORE", "Makanan Berat"),
]

applied = 0
for keyword, cat in QRIS_MAP:
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes, '') || ' | [V3-STEP-103: qris2]',
            updated_at = ?
        WHERE description LIKE ?
          AND category = 'Other / Miscellaneous'
          AND is_deleted = 0
    """, (cat, now, f"%{keyword}%"))
    applied += cur.rowcount

print(f"QRIS round 2: {applied} tx")

con.commit()
print()

# Verify
print("=== Top 15 Expense aktif ===")
for cat, n, tot in cur.execute("""
    SELECT category, COUNT(*), SUM(amount) FROM transactions
    WHERE transaction_type = 'Expense' AND exclude_from_budget = 0 AND is_deleted = 0
    GROUP BY category ORDER BY SUM(amount) DESC LIMIT 15
"""):
    print(f"  {str(cat)[:35]:<37} {n:>4} tx  Rp{tot:>14,.2f}")

print()
q1 = "SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0"
print(f"Other/Misc sisa: {cur.execute(q1).fetchone()[0]} tx")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
