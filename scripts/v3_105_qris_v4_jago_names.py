import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixF-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. QRIS v4 (extract merchant setelah "QR <N> <N>.<N>" pattern) ===
print("=== 1. QRIS v4 ===")
QRIS_MAP = [
    ("NKEDAI SHE", "Makanan Berat"),
    ("NDJAYA BAR", "Makanan Berat"),
    ("NAROMATIQU", "Cafe & Minuman"),
    ("NHisana Fr", "Makanan Berat"),
    ("Nkremes ba", "Makanan Berat"),
    ("NSiberPOS", "Fotokopi & ATK"),
    ("NCENTRAL M", "Otomotif"),
    ("NSOLARIA", "Makanan Berat"),
    ("NNI HAO MA", "Makanan Berat"),
    ("NSING JAYA", "Toko Retail"),
    ("NJAKARTA C", "Makanan Berat"),
    ("NMC MALL O", "Belanja Online"),
    ("NBCS ELEKT", "Toko Retail"),
    ("NPAGI SORE", "Makanan Berat"),
    ("NAYAM", "Makanan Berat"),
    ("NLALAPAN", "Makanan Berat"),
    ("NPEMPEK", "Snacks & Jajan"),
    ("NMie Gaco", "Makanan Berat"),
    ("NSoto", "Makanan Berat"),
    ("NWarmindo", "Makanan Berat"),
    ("NBakso", "Makanan Berat"),
    ("NBurger", "Makanan Berat"),
    ("NTahu", "Makanan Berat"),
    ("NDapur", "Makanan Berat"),
    ("NPokang", "Makanan Berat"),
    ("NWarung", "Makanan Berat"),
    ("NGudeg", "Makanan Berat"),
    ("NSate", "Makanan Berat"),
    ("NRM ", "Makanan Berat"),
    ("NEs Degan", "Snacks & Jajan"),
    ("NJamu", "Snacks & Jajan"),
    ("NMojito", "Cafe & Minuman"),
    ("NToko", "Toko Retail"),
    ("NAlfa", "Grocery"),
    ("NIndomaret", "Grocery"),
]
applied = 0
for kw, cat in QRIS_MAP:
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes,'') || ' | [V3-STEP-105: qris4]',
            updated_at = ?
        WHERE description LIKE ?
          AND category = 'Other / Miscellaneous'
          AND is_deleted = 0
    """, (cat, now, f"%{kw}%"))
    applied += cur.rowcount
print(f"  QRIS v4: {applied}")

# === 2. Jago Transfer Keluar/Masuk dengan counterparty known ===
print()
print("=== 2. Jago transfer with known names ===")
# Jago Transfer Keluar/Masuk ALMAAS -> Transfer ke Keluarga / Penerimaan dari Keluarga
cur.execute("""
    UPDATE transactions
    SET category = 'Transfer ke Keluarga',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: jago almaas]',
        updated_at = ?
    WHERE description LIKE '%Jago Statement%Transfer Keluar%ALMAAS%'
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Jago Transfer Keluar ALMAAS: {cur.rowcount}")

cur.execute("""
    UPDATE transactions
    SET category = 'Penerimaan dari Keluarga',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: jago almaas]',
        updated_at = ?
    WHERE description LIKE '%Jago Statement%Transfer Masuk%ALMAAS%'
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Jago Transfer Masuk ALMAAS: {cur.rowcount}")

# Jago "Transfer Keluar ALMAAS" - salah arah, ini sebenarnya yang masuk? Fix
# Jago Transfer Keluar ALLAN (self) -> Allocation
cur.execute("""
    UPDATE transactions
    SET category = 'Allocation Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: jago self]',
        updated_at = ?
    WHERE description LIKE '%Jago Statement%Transfer Keluar%ALLAN%'
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Jago Transfer Keluar ALLAN (self): {cur.rowcount}")

cur.execute("""
    UPDATE transactions
    SET category = 'Allocation Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: jago self]',
        updated_at = ?
    WHERE description LIKE '%Jago Statement%Transfer Masuk%ALLAN%'
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
print(f"  Jago Transfer Masuk ALLAN (self): {cur.rowcount}")

# === 3. BCA QRIS failed -> demote ===
print()
print("=== 3. BCA QRIS failed demote ===")
cur.execute("""
    UPDATE transactions
    SET is_deleted = 1, exclude_from_budget = 1,
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: failed qris]',
        updated_at = ?
    WHERE description LIKE '%BCA QRIS%transaksi gagal%'
      AND is_deleted = 0
""", (now,))
print(f"  BCA QRIS failed: {cur.rowcount}")

# === 4. Auto-parsed candidate -> cek dulu ===
print()
print("=== 4. Auto-parsed candidate (sample 5) ===")
for tid, d, a, desc in cur.execute("""
    SELECT id, date, amount, description FROM transactions
    WHERE description LIKE '%Auto-parsed candidate%' AND is_deleted = 0 LIMIT 5
"""):
    print(f"  id={tid} {d} Rp{float(a):>10,.0f}")
    print(f"    {desc[:120]}")

# === 5. BCA "Dana masuk dari pihak lain" — biarkan pending, tandai ===
print()
print("=== 5. Dana masuk pihak lain (71 tx) ===")
cur.execute("""
    UPDATE transactions
    SET notes = COALESCE(notes,'') || ' | [V3-STEP-99: pending classification]',
        updated_at = ?
    WHERE description LIKE '%Dana masuk dari pihak lain%'
      AND notes NOT LIKE '%V3-STEP-99%'
      AND is_deleted = 0
""", (now,))
print(f"  Tagged pending: {cur.rowcount}")

# === 6. SeaBank Pembayaran — cek sample ===
print()
print("=== 6. SeaBank Pembayaran (sample 5) ===")
for tid, d, a, desc in cur.execute("""
    SELECT id, date, amount, description FROM transactions
    WHERE description LIKE '%SeaBank Statement%Pembayaran%' AND is_deleted = 0 LIMIT 5
"""):
    print(f"  id={tid} {d} Rp{float(a):>10,.0f}")
    print(f"    {desc[:120]}")

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

con.close()
