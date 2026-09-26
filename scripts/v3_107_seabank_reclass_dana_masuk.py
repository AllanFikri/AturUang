import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixI-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

def apply(label, cat, ttype, excl, where):
    sql = f"""
        UPDATE transactions
        SET category = ?, transaction_type = ?, exclude_from_budget = ?,
            notes = COALESCE(notes,'') || ' | [V3-STEP-107: {label}]',
            updated_at = ?
        WHERE {where} AND category = 'Other / Miscellaneous' AND is_deleted = 0
    """
    cur.execute(sql, (cat, ttype, excl, now))
    if cur.rowcount > 0: print(f"  [{label}] -> {cat}: {cur.rowcount}")

# === 1. SeaBank reclass by description ===
print("=== 1. SeaBank reclass ===")

# Self-transfer
apply("seabank self", "Allocation Movement", "Transfer", 1,
      "description LIKE '%SeaBank Statement%ALLAN FIKRI%'")

# Top up / Transfer
apply("seabank trf out", "Transfer ke Pihak Lain", "Expense", 0,
      "description LIKE '%SeaBank Statement%Transfer%' AND transaction_type = 'Expense'")

apply("seabank trf in", "Penerimaan dari Pihak Lain", "Income", 0,
      "description LIKE '%SeaBank Statement%Transfer%' AND transaction_type = 'Income'")

# Shopee/Flip related
apply("seabank shopeepay", "Allocation Movement", "Transfer", 1,
      "description LIKE '%SeaBank Statement%ShopeePay%'")

apply("seabank flip", "Transfer ke Pihak Lain", "Expense", 0,
      "description LIKE '%SeaBank Statement%Flip%' AND transaction_type = 'Expense'")

apply("seabank flip in", "Penerimaan dari Pihak Lain", "Income", 0,
      "description LIKE '%SeaBank Statement%Flip%' AND transaction_type = 'Income'")

# Pengembalian dana
apply("seabank refund", "Penerimaan dari Pihak Lain", "Income", 0,
      "description LIKE '%Pengembalian Dana%'")

# Bunga / Pajak
apply("seabank bunga", "Investment Movement", "Transfer", 1,
      "description LIKE '%SeaBank Statement%Bunga%'")
apply("seabank pajak", "Investment Movement", "Transfer", 1,
      "description LIKE '%SeaBank Statement%Pajak%'")

# === 2. BCA "Dana masuk dari pihak lain" — biarkan tapi exclude=1 ===
print()
print("=== 2. BCA Dana masuk pihak lain ===")
cur.execute("""
    UPDATE transactions
    SET exclude_from_budget = 1,
        budget_exclusion_reason = 'Perlu review manual (V3-STEP-99)',
        notes = COALESCE(notes,'') || ' | [V3-STEP-99: pending review]',
        updated_at = ?
    WHERE description LIKE '%Dana masuk dari pihak lain%'
      AND exclude_from_budget = 0
      AND is_deleted = 0
""", (now,))
print(f"  Set exclude=1: {cur.rowcount}")

# === 3. Auto-parsed candidate — demote (no source) ===
print()
print("=== 3. Auto-parsed candidate demote ===")
cur.execute("""
    UPDATE transactions
    SET is_deleted = 1, exclude_from_budget = 1,
        notes = COALESCE(notes,'') || ' | [V3-STEP-107: legacy autoparse]',
        updated_at = ?
    WHERE description = 'Auto-parsed candidate'
      AND (source_refs IS NULL OR source_refs = '')
      AND is_deleted = 0
""", (now,))
print(f"  Demoted: {cur.rowcount}")

# === 4. BCA FLAZZ -> Transportasi ===
print()
print("=== 4. BCA FLAZZ ===")
apply("flazz topup", "Transportasi", "Expense", 0,
      "description LIKE '%FLAZZ BCA%TOPUP%'")

# === 5. SeaBank "Pembayaran" generic -> Belanja Harian (fallback) ===
print()
print("=== 5. SeaBank Pembayaran sisa ===")
apply("seabank pembayaran", "Belanja Harian", "Expense", 0,
      "description LIKE '%SeaBank Statement%Pembayaran%'")

con.commit()
print()

# Verify
n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc sisa: Expense {n_exp} tx, Income {n_inc} tx")

print()
print("=== Top 15 kategori aktif ===")
for cat, ttype, n, tot in cur.execute("""
    SELECT category, transaction_type, COUNT(*), SUM(amount)
    FROM transactions
    WHERE is_deleted = 0 AND exclude_from_budget = 0
    GROUP BY category, transaction_type
    ORDER BY SUM(amount) DESC LIMIT 15
"""):
    print(f"  {str(cat)[:35]:<37} {ttype:<10} {n:>4}  Rp{tot:>14,.2f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
