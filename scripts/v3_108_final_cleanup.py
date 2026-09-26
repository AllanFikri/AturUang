import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixJ2-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

def apply_like(label, cat, ttype, excl, pattern, extra="", params_extra=None):
    """Parameterized LIKE — aman dari apostrophe."""
    sql = f"""
        UPDATE transactions
        SET category = ?, transaction_type = ?, exclude_from_budget = ?,
            notes = COALESCE(notes,'') || ?,
            updated_at = ?
        WHERE description LIKE ?
          AND category = 'Other / Miscellaneous' AND is_deleted = 0 {extra}
    """
    p = [cat, ttype, excl, f" | [V3-STEP-108: {label}]", now, f"%{pattern}%"]
    if params_extra: p.extend(params_extra)
    cur.execute(sql, p)
    if cur.rowcount > 0: print(f"  [{label}]: {cur.rowcount}")

# === EXPENSE ===
print("=== EXPENSE ===")

QRIS_JAGO = [
    ("Ketupat Ajo", "Makanan Berat"),
    ("ESB Restaurant", "Makanan Berat"),
    ("WARKOP AGAM", "Makanan Berat"),
    ("RM AMPERA", "Makanan Berat"),
    ("ARFANIA ZAHRA", "Makanan Berat"),
    ("NINA RASA", "Makanan Berat"),
    ("Urban Market", "Belanja Harian"),
    ("MULTI CELL", "Belanja Harian"),
    ("HAMDAN COLLECTION", "Belanja Harian"),
    ("MATE OOLONG", "Cafe & Minuman"),
    ("Perdagangan", "Belanja Harian"),
    ("yUBaput", "Makanan Berat"),
    ("Mamopi", "Makanan Berat"),  # hindari apostrophe
]
for kw, cat in QRIS_JAGO:
    apply_like(f"jago qris {kw[:15]}", cat, "Expense", 0, kw, "AND transaction_type = 'Expense'")

# BCA LWSN (Lawson minimarket)
apply_like("lwson", "Makanan Berat", "Expense", 0, "LWSN")

# BCA transfer ke orang (BI-FAST DB non-self)
apply_like("bca trf orang", "Transfer ke Pihak Lain", "Expense", 0, "BI-FAST DB%BIF TRANSFER KE",
           "AND description NOT LIKE '%535 ALLAN%' AND description NOT LIKE '%501 ALLAN%' AND transaction_type = 'Expense'")

# BIAYA TXN + BIAYA KARTU
cur.execute("""
    UPDATE transactions
    SET category = 'Biaya Bank', transaction_type = 'Expense', exclude_from_budget = 0,
        notes = COALESCE(notes,'') || ' | [V3-STEP-108: biaya bank]',
        updated_at = ?
    WHERE (description LIKE '%BIAYA TXN%' OR description LIKE '%BIAYA KARTU ATM%')
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
if cur.rowcount > 0: print(f"  [biaya bank]: {cur.rowcount}")

# GoPay Jl Mondoroko (self)
apply_like("gopay jl mondoroko", "Allocation Movement", "Transfer", 1, "Jl. Mondoroko",
           "AND transaction_type = 'Expense'")

apply_like("hotel", "Akomodasi", "Expense", 0, "Hotel Borobudur")
apply_like("tiktok", "Belanja Online", "Expense", 0, "Tiktok")
apply_like("seabank pulsa", "Pulsa & Data", "Expense", 0, "SeaBank%Top Up - Pulsa")
apply_like("jago xl", "Pulsa & Data", "Expense", 0, "Produk Digital%XL")
apply_like("jago biaya produk", "Biaya Bank", "Expense", 0, "Biaya Pembayaran Produk Digital")

# === INCOME ===
print()
print("=== INCOME ===")
apply_like("seabank almaas", "Penerimaan dari Keluarga", "Income", 0, "SeaBank%ALMAAS")
apply_like("gopay topup", "Allocation Movement", "Transfer", 1, "GoPay%Top up")
apply_like("gopay topup2", "Allocation Movement", "Transfer", 1, "GoPay top up")
apply_like("gopay self", "Allocation Movement", "Transfer", 1, "Ditransfer ke Allan Fikri")

# Bunga (BCA + Jago)
cur.execute("""
    UPDATE transactions
    SET category = 'Investment Movement', transaction_type = 'Transfer', exclude_from_budget = 1,
        notes = COALESCE(notes,'') || ' | [V3-STEP-108: bunga]',
        updated_at = ?
    WHERE (description LIKE '%BUNGA%' OR description LIKE '%Bunga%Kantong%' OR description LIKE '%Bunga Tabungan%')
      AND category = 'Other / Miscellaneous' AND is_deleted = 0
""", (now,))
if cur.rowcount > 0: print(f"  [bunga]: {cur.rowcount}")

apply_like("jago cashback", "Cashback", "Income", 0, "Jago%Cashback")

# === DEMOTE ===
print()
print("=== DEMOTE ===")
cur.execute("""
    UPDATE transactions
    SET exclude_from_budget = 1,
        budget_exclusion_reason = 'SeaBank parse incomplete (V3-STEP-108)',
        notes = COALESCE(notes,'') || ' | [V3-STEP-108: seabank blank]',
        updated_at = ?
    WHERE description = '[SeaBank Statement]'
      AND category = 'Other / Miscellaneous'
      AND is_deleted = 0
""", (now,))
print(f"  SeaBank blank: {cur.rowcount}")

cur.execute("""
    UPDATE transactions
    SET exclude_from_budget = 1,
        budget_exclusion_reason = 'V3-STEP-99 pending review',
        notes = COALESCE(notes,'') || ' | [V3-STEP-108: dana masuk pending]',
        updated_at = ?
    WHERE description LIKE '%Dana masuk dari pihak lain%'
      AND category = 'Other / Miscellaneous'
      AND is_deleted = 0
""", (now,))
print(f"  BCA Dana masuk pending: {cur.rowcount}")

con.commit()
print()

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
