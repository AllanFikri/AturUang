import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-almaas-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. FIX 2 DUPLIKAT ===
print("=== 1. Fix duplikat ===")

# id 2134 = duplikat bca_statement 2025-04-20 Rp100.000
cur.execute("""
    UPDATE transactions
    SET is_deleted = 1, exclude_from_budget = 1,
        notes = COALESCE(notes, '') || ' | [V3-STEP-100G-7: duplicate of id 2133]',
        updated_at = ?
    WHERE id = 2134
""", (now,))
print(f"  id 2134 (bca Rp100.000) → deleted")

# id 3965 = duplikat blu_statement 2025-07-23 Rp7.604.832
cur.execute("""
    UPDATE transactions
    SET is_deleted = 1, exclude_from_budget = 1,
        notes = COALESCE(notes, '') || ' | [V3-STEP-100G-7: duplicate of id 3963]',
        updated_at = ?
    WHERE id = 3965
""", (now,))
print(f"  id 3965 (blu Rp7.604.832) → deleted")

# === 2. FIX PINJAMAN Rp86.5jt ===
print()
print("=== 2. Update pinjaman Rp86.5jt ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Pinjaman dari Keluarga (Liability)',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Pinjaman dari Almaas/Mbak Erin — bukan income',
        notes = COALESCE(notes, '') || ' | [V3-STEP-100G-7: liability]',
        updated_at = ?
    WHERE date = '2025-06-26' AND amount = 86500000 AND is_deleted = 0
""", (now,))
print(f"  Updated: {cur.rowcount} rows")

# === 3. CICILAN KE ALMAAS (outflow besar Jun-Agu 2025) ===
print()
print("=== 3. Reclass cicilan pinjaman (outflow besar ke Almaas) ===")

# Criteria: ke Almaas, Transfer/Expense, amount >= Rp1jt, periode Jun-Agu 2025
rows = cur.execute("""
    SELECT id, date, amount FROM transactions
    WHERE description LIKE '%ALMAAS%' AND is_deleted = 0
      AND transaction_type IN ('Transfer', 'Expense')
      AND amount >= 1000000
      AND date BETWEEN '2025-06-26' AND '2025-08-31'
    ORDER BY date
""").fetchall()

print(f"  Kandidat cicilan: {len(rows)} tx")
for tid, d, a in rows:
    print(f"    id={tid}  {d}  Rp{float(a):>12,.0f}")

cicilan_total = 0
for tid, d, a in rows:
    cur.execute("""
        UPDATE transactions
        SET category = 'Pembayaran Pinjaman ke Keluarga',
            exclude_from_budget = 1,
            budget_exclusion_reason = 'Cicilan pinjaman Rp86.5jt',
            notes = COALESCE(notes, '') || ' | [V3-STEP-100G-7: loan repayment]',
            updated_at = ?
        WHERE id = ?
    """, (now, tid))
    cicilan_total += float(a)

print(f"  Total cicilan: Rp{cicilan_total:,.2f}")

# === 4. ASET USAHA: OVEN ===
print()
print("=== 4. Reclass oven sebagai Aset Usaha ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Aset Usaha (Oven & Mixer)',
        transaction_type = 'Expense',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Aset usaha — bukan expense pribadi',
        notes = COALESCE(notes, '') || ' | [V3-STEP-100G-7: business asset]',
        updated_at = ?
    WHERE description LIKE '%Oven%mixer%INGE NIRMALASARI%' AND is_deleted = 0
""", (now,))
print(f"  Updated: {cur.rowcount} rows")

con.commit()

# === 5. HITUNG OUTSTANDING ===
print()
print("=" * 70)
print("RINGKASAN PINJAMAN")
print("=" * 70)
pinjaman = 86500000
print(f"  Pinjaman masuk     : Rp{pinjaman:>14,.0f}")
print(f"  Cicilan dibayar    : Rp{cicilan_total:>14,.0f}")
print(f"  Outstanding        : Rp{pinjaman - cicilan_total:>14,.0f}")

print()
print("=== Kategori terkait setelah fix ===")
for cat, n, tot in cur.execute("""
    SELECT category, COUNT(*), SUM(amount) FROM transactions
    WHERE is_deleted = 0
      AND category IN ('Pinjaman dari Keluarga (Liability)', 'Pembayaran Pinjaman ke Keluarga',
                       'Aset Usaha (Oven & Mixer)', 'Uang Titipan (Liability)')
    GROUP BY category
"""):
    print(f"  {cat:<42} {n:>3} tx  Rp{tot:>14,.2f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
