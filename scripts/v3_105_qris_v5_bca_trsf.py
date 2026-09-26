import sqlite3, shutil, datetime as dt, re
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixG-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# === 1. QRIS v5: proper keyword (tanpa prefix N) ===
print("=== 1. QRIS v5 ===")
QRIS_MAP = [
    ("KEDAI SHE", "Makanan Berat"),
    ("DJAYA BAR", "Makanan Berat"),
    ("AROMATIQU", "Cafe & Minuman"),
    ("Hisana Fr", "Makanan Berat"),
    ("kremes ba", "Makanan Berat"),
    ("SiberPOS", "Fotokopi & ATK"),
    ("CENTRAL M", "Otomotif"),
    ("SOLARIA", "Makanan Berat"),
    ("NI HAO MA", "Makanan Berat"),
    ("SING JAYA", "Toko Retail"),
    ("JAKARTA C", "Makanan Berat"),
    ("MC MALL O", "Belanja Online"),
    ("BCS ELEKT", "Toko Retail"),
    ("PAGI SORE", "Makanan Berat"),
    ("KEDAI OM", "Makanan Berat"),
    ("KEDAI SAN", "Cafe & Minuman"),
    ("PEMON BAR", "Perawatan Diri"),
    ("Barber Shop", "Perawatan Diri"),
    ("PARFUM", "Perawatan Diri"),
    ("WATSONS", "Kesehatan"),
    ("APOTEK", "Apotek"),
    ("KLINIK", "Kesehatan"),
    ("PERUMDA", "Listrik & Utilitas"),
    ("PLN", "Listrik & Utilitas"),
    ("MIE GACOAN", "Makanan Berat"),
    ("Soto Ayam", "Makanan Berat"),
    ("Soto SSB", "Makanan Berat"),
    ("Soto Dok", "Makanan Berat"),
    ("Warmindo", "Makanan Berat"),
    ("Warteg", "Makanan Berat"),
    ("Warung", "Makanan Berat"),
    ("BAKSO", "Makanan Berat"),
    ("SATE ", "Makanan Berat"),
    ("AYAM", "Makanan Berat"),
    ("BEBEK", "Makanan Berat"),
    ("SEAFOOD", "Makanan Berat"),
    ("PECEL", "Makanan Berat"),
    ("PENTOL", "Makanan Berat"),
    ("PENYETAN", "Makanan Berat"),
    ("LALAPAN", "Makanan Berat"),
    ("BURGER", "Makanan Berat"),
    ("Gyoza", "Makanan Berat"),
    ("Dimsum", "Makanan Berat"),
    ("PEMPEK", "Snacks & Jajan"),
    ("CINCAU", "Snacks & Jajan"),
    ("ES KRIM", "Snacks & Jajan"),
    ("ES DEGAN", "Snacks & Jajan"),
    ("ES TELER", "Snacks & Jajan"),
    ("MOLEN", "Snacks & Jajan"),
    ("PISKIP", "Snacks & Jajan"),
    ("ROTI", "Snacks & Jajan"),
    ("SRUPUT", "Snacks & Jajan"),
    ("KUE", "Snacks & Jajan"),
    ("BUBUR", "Snacks & Jajan"),
    ("COFFEE", "Cafe & Minuman"),
    ("KOPI", "Cafe & Minuman"),
    ("CAFE", "Cafe & Minuman"),
    ("Ngopi", "Cafe & Minuman"),
    ("SWARA", "Cafe & Minuman"),
    ("TEH RACEK", "Cafe & Minuman"),
    ("Mixue", "Cafe & Minuman"),
    ("Go Kopi", "Cafe & Minuman"),
    ("KARA C", "Cafe & Minuman"),
    ("Conpanna", "Cafe & Minuman"),
    ("SPBU", "Bensin"),
    ("Shell", "Bensin"),
    ("PLANET BAN", "Otomotif"),
    ("FOTOCOPY", "Fotokopi & ATK"),
    ("KJPRI", "Fotokopi & ATK"),
    ("SARJANA", "Fotokopi & ATK"),
    ("Toko Nurra", "Belanja Online"),
    ("SING JAYA", "Toko Retail"),
    ("Toko", "Toko Retail"),
    ("Lokomart", "Grocery"),
    ("Looyal", "Toko Retail"),
    ("INDOMA", "Grocery"),
    ("ALFAMART", "Grocery"),
    ("ALFAGIFT", "Grocery"),
    ("SUPERINDO", "Grocery"),
    ("ABC MART", "Grocery"),
    ("MR.D.I.Y", "Toko Retail"),
    ("MR DIY", "Toko Retail"),
    ("MUMU", "Toko Retail"),
    ("XIAOMI", "Toko Retail"),
    ("METEOR", "Toko Retail"),
    ("TOKOPEDIA", "Belanja Online"),
    ("Shopee", "Belanja Online"),
]
applied = 0
for kw, cat in QRIS_MAP:
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes,'') || ' | [V3-STEP-105: qris5]',
            updated_at = ?
        WHERE description LIKE ?
          AND category = 'Other / Miscellaneous'
          AND is_deleted = 0
    """, (cat, now, f"%{kw}%"))
    applied += cur.rowcount
print(f"  QRIS v5: {applied}")

# === 2. BCA "TRANSAKSI DEBIT QR" tanpa merchant dikenal -> Belanja Harian ===
print()
print("=== 2. BCA QR tanpa merchant -> Belanja Harian ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Belanja Harian',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: bca qr unknown]',
        updated_at = ?
    WHERE (description LIKE '%TRANSAKSI DEBIT TGL% QR %' 
           OR description LIKE '%TRANSAKSI DEBIT TANGGAL% QR %'
           OR description LIKE '%BCA QRIS%ke%')
      AND category = 'Other / Miscellaneous'
      AND is_deleted = 0
""", (now,))
print(f"  Belanja Harian: {cur.rowcount}")

# === 3. BCA TRSF DB FTSCY dengan nama orang -> Transfer ke Pihak Lain ===
print()
print("=== 3. BCA TRSF DB dengan nama -> Transfer Pihak Lain ===")
cur.execute("""
    UPDATE transactions
    SET category = 'Transfer ke Pihak Lain',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: bca trsf]',
        updated_at = ?
    WHERE description LIKE '%TRSF E-BANKING DB%FTSCY%'
      AND category = 'Other / Miscellaneous'
      AND transaction_type = 'Expense'
      AND is_deleted = 0
""", (now,))
print(f"  BCA TRSF DB: {cur.rowcount}")

# === 4. BCA TRSF CR dengan nama -> Penerimaan dari Pihak Lain ===
cur.execute("""
    UPDATE transactions
    SET category = 'Penerimaan dari Pihak Lain',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: bca trsf in]',
        updated_at = ?
    WHERE description LIKE '%TRSF E-BANKING CR%FTSCY%'
      AND category = 'Other / Miscellaneous'
      AND transaction_type = 'Income'
      AND is_deleted = 0
""", (now,))
print(f"  BCA TRSF CR: {cur.rowcount}")

# === 5. BCA KR OTOMATIS LLG -> Penerimaan Pihak Lain ===
cur.execute("""
    UPDATE transactions
    SET category = 'Penerimaan dari Pihak Lain',
        notes = COALESCE(notes,'') || ' | [V3-STEP-105: kr otomatis]',
        updated_at = ?
    WHERE description LIKE '%KR OTOMATIS%'
      AND category = 'Other / Miscellaneous'
      AND is_deleted = 0
""", (now,))
print(f"  KR OTOMATIS: {cur.rowcount}")

# === 6. Investigasi: Auto-parsed candidate ===
print()
print("=== 6. Auto-parsed candidate — cek asal ===")
for tid, d, a, src, notes in cur.execute("""
    SELECT id, date, amount, source_refs, notes FROM transactions
    WHERE description LIKE '%Auto-parsed candidate%' AND is_deleted = 0 LIMIT 10
"""):
    print(f"  id={tid} {d} Rp{float(a):>10,.0f}")
    print(f"    src: {src}")
    print(f"    notes: {notes}")

# === 7. Investigasi: SeaBank Pembayaran ===
print()
print("=== 7. SeaBank Pembayaran — cek sample raw ===")
for tid, d, a, src, notes in cur.execute("""
    SELECT id, date, amount, source_refs, notes FROM transactions
    WHERE description LIKE '%SeaBank Statement%Pembayaran%' AND is_deleted = 0 LIMIT 5
"""):
    print(f"  id={tid} {d} Rp{float(a):>10,.0f}")
    print(f"    src: {src}")
    print(f"    notes: {notes}")

con.commit()
print()

# Verify
n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc sisa: Expense {n_exp} tx, Income {n_inc} tx")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
