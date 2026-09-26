import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-self-bifast-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. BI-FAST self-transfer (501 = Allan) → Allocation Movement ===
print("=== 1. BI-FAST self-transfer ===")

# DB = keluar (Expense)
cur.execute("""
    UPDATE transactions
    SET transaction_type = 'Transfer',
        category = 'Allocation Movement',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Self-transfer BCA <-> RDN',
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: self-transfer 501]',
        updated_at = ?
    WHERE description LIKE '%BI-FAST DB%KE 501 ALLAN%' AND is_deleted = 0
""", (now,))
print(f"  DB KE 501 → Allocation: {cur.rowcount}")

# CR = masuk (Income) — juga self
cur.execute("""
    UPDATE transactions
    SET transaction_type = 'Transfer',
        category = 'Allocation Movement',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Self-transfer BCA <-> RDN',
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: self-transfer 501]',
        updated_at = ?
    WHERE description LIKE '%BI-FAST CR%DR 501 ALLAN%' AND is_deleted = 0
""", (now,))
print(f"  CR DR 501 → Allocation: {cur.rowcount}")

# Juga handle 542 (Jago RDN) kalau ada
cur.execute("""
    UPDATE transactions
    SET transaction_type = 'Transfer',
        category = 'Allocation Movement',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Self-transfer BCA <-> RDN',
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: self-transfer 542]',
        updated_at = ?
    WHERE (description LIKE '%BI-FAST DB%KE 542 ALLAN%' OR description LIKE '%BI-FAST CR%DR 542 ALLAN%')
      AND is_deleted = 0 AND category = 'Other / Miscellaneous'
""", (now,))
print(f"  542 → Allocation: {cur.rowcount}")

# === 2. Shopee Marketplace → Belanja Online ===
print()
print("=== 2. Shopee Marketplace ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Belanja Online',
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: shopee marketplace]',
        updated_at = ?
    WHERE description LIKE '%Shopee Marketplace%'
      AND category = 'Other / Miscellaneous'
      AND is_deleted = 0
""", (now,))
print(f"  Shopee Marketplace → Belanja Online: {cur.rowcount}")

# Shopee Food
cur.execute("""
    UPDATE transactions
    SET category = 'Makanan Berat',
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: shopee food]',
        updated_at = ?
    WHERE (description LIKE '%Shopee Food%' OR description LIKE '%ShopeeFood%')
      AND category = 'Other / Miscellaneous'
      AND is_deleted = 0
""", (now,))
print(f"  Shopee Food → Makanan Berat: {cur.rowcount}")

# === 3. BCA Cardless → Tarik Tunai (Expense, tetap outflow) ===
print()
print("=== 3. BCA Cardless ===")
cur.execute("""
    UPDATE transactions
    SET transaction_type = 'Expense',
        category = 'Tarik Tunai',
        exclude_from_budget = 0,
        notes = COALESCE(notes, '') || ' | [V3-STEP-102: tarik tunai]',
        updated_at = ?
    WHERE description LIKE '%BCA Cardless%Penarikan tunai%' AND is_deleted = 0
""", (now,))
print(f"  BCA Cardless → Tarik Tunai: {cur.rowcount}")

con.commit()
print()

# === Verify ===
print("=== Top 15 kategori setelah fix ===")
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
