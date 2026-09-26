import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fixH-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

def apply(label, cat, ttype, excl, where, params=None):
    if params is None: params = []
    sql = f"""
        UPDATE transactions
        SET category = ?, transaction_type = ?, exclude_from_budget = ?,
            notes = COALESCE(notes,'') || ' | [V3-STEP-106: {label}]',
            updated_at = ?
        WHERE {where} AND category = 'Other / Miscellaneous' AND is_deleted = 0
    """
    cur.execute(sql, [cat, ttype, excl, now] + params)
    if cur.rowcount > 0:
        print(f"  [{label}] -> {cat}: {cur.rowcount}")

# === 1. KARTU DEBIT merchants ===
print("=== 1. KARTU DEBIT merchants ===")
DEBIT_MAP = [
    ("ROYAL ATK", "Fotokopi & ATK"),
    ("IDM ", "Grocery"),
    ("INDOMARET", "Grocery"),
    ("ALFAMART", "Grocery"),
    ("A&W", "Makanan Berat"),
    ("MCD", "Makanan Berat"),
    ("KFC", "Makanan Berat"),
    ("STARBUCKS", "Cafe & Minuman"),
    ("SPBU", "Bensin"),
    ("APOTEK", "Apotek"),
]
for kw, cat in DEBIT_MAP:
    apply(f"debit {kw}", cat, "Expense", 0,
          f"description LIKE '%KARTU DEBIT%{kw}%'")

# === 2. Jago QRIS merchant ===
print()
print("=== 2. Jago QRIS merchant ===")
JAGO_QRIS = [
    ("XL XENDIT", "Pulsa & Data"),
    ("SWEET LAYERS", "Snacks & Jajan"),
    ("RAYA MOND", "Makanan Berat"),
    ("Kopi", "Cafe & Minuman"),
    ("Cafe", "Cafe & Minuman"),
    ("Mart", "Grocery"),
    ("Apotek", "Apotek"),
    ("SPBU", "Bensin"),
]
for kw, cat in JAGO_QRIS:
    apply(f"jago qris {kw}", cat, "Expense", 0,
          f"description LIKE '%Jago Statement%Pembayaran QRIS%{kw}%'")

# === 3. Jago merchant manual ===
apply("jago XL", "Pulsa & Data", "Expense", 0,
      "description LIKE '%Jago:%Pembayaran merchant XL%'")

# === 4. Transfer ke Teman dari Jago (nama known) ===
print()
print("=== 4. Transfer ke Teman (named) ===")
FRIENDS = [
    "LINTANG DWI PRAYOGA",
    "NAUVALDY BASUKI",
    "MOHAMMAD MISBAHUL KHOIR",
    "ADITYA DERMAWAN",
    "SYAFRIL NUR FAIZI",
    "INDRI ALIDHA RAHMA",
    "AULIA DELLA PUTRI",
    "NOOR KHALILA RAHMA",
    "NAUFAL ALFARIZI",
]
for name in FRIENDS:
    apply(f"friend {name[:20]}", "Transfer ke Teman", "Expense", 0,
          f"description LIKE '%{name}%' AND transaction_type = 'Expense'")

# === 5. Transfer ke Pihak Lain generik Jago ===
apply("jago trf keluar", "Transfer ke Pihak Lain", "Expense", 0,
      "description LIKE '%Jago Statement%Transfer Keluar%'")

# === 6. SeaBank self-transfer ===
apply("seabank self", "Allocation Movement", "Transfer", 1,
      "description LIKE '%SeaBank Statement%ALLAN FIKRI MAHARDIKA%Transfer%'")

# === 7. Income: named transfers ===
print()
print("=== 7. Income named ===")
for name in FRIENDS:
    apply(f"income friend {name[:20]}", "Penerimaan dari Teman", "Income", 0,
          f"description LIKE '%{name}%' AND transaction_type = 'Income'")

# === 8. BCA BI-FAST CR dengan nama generik ===
apply("bca bifast cr", "Penerimaan dari Pihak Lain", "Income", 0,
      "description LIKE '%BI-FAST CR%BIF TRANSFER DR%'")

# === 9. Jago FL... (bunga/fee) -> Investment ===
apply("jago FL", "Investment Movement", "Transfer", 1,
      "description LIKE '%Jago Statement%FL%'")

# === 10. Jago statement row (sisa) ===
apply("jago stmt row", "Penerimaan dari Pihak Lain", "Income", 0,
      "description LIKE '%Jago Statement%statement row%' AND transaction_type = 'Income'")
apply("jago stmt row exp", "Transfer ke Pihak Lain", "Expense", 0,
      "description LIKE '%Jago Statement%statement row%' AND transaction_type = 'Expense'")

# === 11. Jago "Transfer Masuk" dengan nama known ===
apply("jago trf masuk", "Penerimaan dari Pihak Lain", "Income", 0,
      "description LIKE '%Jago Statement%Transfer Masuk%'")

con.commit()
print()

# Verify
n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc sisa: Expense {n_exp} tx, Income {n_inc} tx")

print()
print("=== Sisa 20 Expense terbesar ===")
for tid, d, desc, amt in cur.execute("""
    SELECT id, date, description, amount FROM transactions
    WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0
    ORDER BY amount DESC LIMIT 20
"""):
    print(f"  id={tid} {d} Rp{float(amt):>10,.0f}")
    print(f"    {desc[:110]}")

print()
print("=== Sisa 20 Income terbesar ===")
for tid, d, desc, amt in cur.execute("""
    SELECT id, date, description, amount FROM transactions
    WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0
    ORDER BY amount DESC LIMIT 20
"""):
    print(f"  id={tid} {d} Rp{float(amt):>10,.0f}")
    print(f"    {desc[:110]}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
