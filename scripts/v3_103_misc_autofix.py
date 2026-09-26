import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-fix-misc-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

RULES = [
    ("TARIKAN ATM%", "Tarik Tunai", "Expense"),
    ("%by.U%paket data%", "Pulsa & Data", "Expense"),
    ("%by.U%Pembayaran paket%", "Pulsa & Data", "Expense"),
    ("%il PLN Mobile%", "Listrik & Utilitas", "Expense"),
    ("%Google Play%", "Langganan Digital", "Expense"),
    ("%Claude by Ant%", "Langganan Digital", "Expense"),
    ("%ceosePy Google Play%", "Langganan Digital", "Expense"),
]

for pattern, cat, ttype in RULES:
    cur.execute("""
        UPDATE transactions
        SET category = ?, transaction_type = ?, exclude_from_budget = 0,
            notes = COALESCE(notes, '') || ' | [V3-STEP-103: misc autofix]',
            updated_at = ?
        WHERE description LIKE ? AND category = 'Other / Miscellaneous' AND is_deleted = 0
    """, (cat, ttype, now, pattern))
    if cur.rowcount > 0:
        print(f"  {pattern[:35]:<37} -> {cat:<25} ({cur.rowcount} tx)")

con.commit()
print()

# Verify — pakai variabel, bukan inline
q1 = "SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0"
q2 = "SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0"

n_exp = cur.execute(q1).fetchone()[0]
n_inc = cur.execute(q2).fetchone()[0]

print("=== Other/Misc sisa ===")
print(f"  Expense: {n_exp} tx")
print(f"  Income : {n_inc} tx")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
