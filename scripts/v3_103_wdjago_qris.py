import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixB-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. WD JAGO (paired in+out = internal RDN transfer) ===
# Cek dulu
paired_dates = []
for d, n in cur.execute("""
    SELECT date, COUNT(*) FROM transactions
    WHERE description LIKE '%WD jago%' AND is_deleted = 0
    GROUP BY date HAVING COUNT(*) >= 2
"""):
    paired_dates.append(d)

print(f"WD jago paired dates: {len(paired_dates)}")

# Update WD jago yang paired -> Allocation Movement
cur.execute("""
    UPDATE transactions
    SET category = 'Allocation Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Jago WD <-> RDN Stockbit (internal)',
        notes = COALESCE(notes, '') || ' | [V3-STEP-103: WD jago internal]',
        updated_at = ?
    WHERE description LIKE '%WD jago%' AND is_deleted = 0
""", (now,))
print(f"  WD jago -> Allocation: {cur.rowcount}")

# === 2. QRIS MERCHANT MAPPING ===
# Mapping keyword -> kategori
QRIS_MAP = [
    # (keyword, kategori)
    ("INDOMA", "Grocery"),
    ("ALFAMART", "Grocery"),
    ("ALFAGIFT", "Grocery"),
    ("SUPERINDO", "Grocery"),
    ("ABC MART", "Grocery"),
    ("KANTIN", "Makanan Berat"),
    ("WARMINDO", "Makanan Berat"),
    ("WARUNG", "Makanan Berat"),
    ("WARTEG", "Makanan Berat"),
    ("BAKSO", "Makanan Berat"),
    ("MIE GACOAN", "Makanan Berat"),
    ("MIE AYAM", "Makanan Berat"),
    ("NASI", "Makanan Berat"),
    ("SATE", "Makanan Berat"),
    ("SOTO", "Makanan Berat"),
    ("RM AMPERA", "Makanan Berat"),
    ("RM RODA", "Makanan Berat"),
    ("RM SAMALAM", "Makanan Berat"),
    ("ESB RESTAU", "Makanan Berat"),
    ("GWAJA", "Makanan Berat"),
    ("AYAM", "Makanan Berat"),
    ("BEBEK", "Makanan Berat"),
    ("SEAFOOD", "Makanan Berat"),
    ("PECEL", "Makanan Berat"),
    ("PENTOL", "Makanan Berat"),
    ("PENYETAN", "Makanan Berat"),
    ("TACONESIA", "Makanan Berat"),
    ("BAKPAO", "Snacks & Jajan"),
    ("CINCAU", "Snacks & Jajan"),
    ("ES KRIM", "Snacks & Jajan"),
    ("ES DEGAN", "Snacks & Jajan"),
    ("ES TELER", "Snacks & Jajan"),
    ("MOLEN", "Snacks & Jajan"),
    ("PISKIP", "Snacks & Jajan"),
    ("ROTI", "Snacks & Jajan"),
    ("SRUPUT", "Snacks & Jajan"),
    ("KUE", "Snacks & Jajan"),
    ("LAPIS KUKUS", "Snacks & Jajan"),
    ("BUBUR", "Snacks & Jajan"),
    ("CAFE", "Cafe & Minuman"),
    ("COFFEE", "Cafe & Minuman"),
    ("KOPI", "Cafe & Minuman"),
    ("NGOPI", "Cafe & Minuman"),
    ("SWARA", "Cafe & Minuman"),
    ("KARA", "Cafe & Minuman"),
    ("TEH RACEK", "Cafe & Minuman"),
    ("MR. TEA", "Cafe & Minuman"),
    ("MIXUE", "Cafe & Minuman"),
    ("COTTA", "Cafe & Minuman"),
    ("MANGLOO", "Cafe & Minuman"),
    ("CITRA SUSU", "Cafe & Minuman"),
    ("APOTEK", "Apotek"),
    ("KLINIK", "Kesehatan"),
    ("WATSONS", "Kesehatan"),
    ("SPBU", "Bensin"),
    ("SHELL", "Bensin"),
    ("PLANET BAN", "Otomotif"),
    ("CENTRAL MOTOR", "Otomotif"),
    ("RAJAWALI MOTOR", "Otomotif"),
    ("FOTOCOPY", "Fotokopi & ATK"),
    ("KJPRI UB", "Fotokopi & ATK"),
    ("KS N", "Fotokopi & ATK"),
    ("KS 24", "Fotokopi & ATK"),
    ("SARJANA", "Fotokopi & ATK"),
    ("TOKO NURRA", "Belanja Online"),
    ("TOKO", "Toko Retail"),
    ("SHOPEE", "Belanja Online"),
    ("TOKOPEDIA", "Belanja Online"),
    ("MR.D.I.Y", "Toko Retail"),
    ("MR DIY", "Toko Retail"),
    ("MUMU FAMILY", "Toko Retail"),
    ("METEOR XIAOMI", "Toko Retail"),
    ("Lokomart", "Grocery"),
    ("Looyal", "Toko Retail"),
]

applied = 0
for keyword, cat in QRIS_MAP:
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes, '') || ' | [V3-STEP-103: qris auto]',
            updated_at = ?
        WHERE description LIKE ?
          AND category = 'Other / Miscellaneous'
          AND transaction_type = 'Expense'
          AND is_deleted = 0
    """, (cat, now, f"%{keyword}%"))
    if cur.rowcount > 0:
        applied += cur.rowcount

print(f"  QRIS merchant auto-categorize: {applied} tx")
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
