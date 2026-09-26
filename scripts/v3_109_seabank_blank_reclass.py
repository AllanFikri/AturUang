import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-taskD-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

cur.execute("""
    UPDATE transactions
    SET category = 'Investment Movement',
        transaction_type = 'Transfer',
        exclude_from_budget = 1,
        budget_exclusion_reason = 'Bunga tabungan SeaBank',
        notes = COALESCE(notes,'') || ' | [TASK-D: seabank bunga]',
        updated_at = ?
    WHERE description = '[SeaBank Statement]'
      AND amount < 100 AND is_deleted = 0
""", (now,))
print(f"Bunga kecil -> Investment Movement: {cur.rowcount}")

cur.execute("""
    UPDATE transactions
    SET category = 'Transfer ke Pihak Lain',
        notes = COALESCE(notes,'') || ' | [TASK-D: seabank expense blank]',
        updated_at = ?
    WHERE description = '[SeaBank Statement]'
      AND transaction_type = 'Expense'
      AND amount >= 100
      AND is_deleted = 0
""", (now,))
print(f"Expense blank -> Transfer ke Pihak Lain: {cur.rowcount}")

cur.execute("""
    UPDATE transactions
    SET category = 'Penerimaan dari Pihak Lain',
        notes = COALESCE(notes,'') || ' | [TASK-D: seabank income blank]',
        updated_at = ?
    WHERE description = '[SeaBank Statement]'
      AND transaction_type = 'Income'
      AND amount >= 100
      AND is_deleted = 0
""", (now,))
print(f"Income blank -> Penerimaan dari Pihak Lain: {cur.rowcount}")

con.commit()
print()

n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc sisa: Expense {n_exp} tx, Income {n_inc} tx")
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
