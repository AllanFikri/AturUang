import sqlite3, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-taskE-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

def upd(ids, cat, ttype, excl, label, reason=""):
    if not ids: return
    placeholders = ",".join("?" for _ in ids)
    cur.execute(f"""
        UPDATE transactions
        SET category = ?, transaction_type = ?, exclude_from_budget = ?,
            budget_exclusion_reason = ?,
            notes = COALESCE(notes,'') || ?,
            updated_at = ?
        WHERE id IN ({placeholders})
    """, [cat, ttype, excl, reason, f" | [TASK-E: {label}]", now] + list(ids))
    print(f"  [{label}]: {cur.rowcount}")

# === EXPENSE ===
print("=== EXPENSE ===")
# BCA 535 ALLAN = self
upd([1872], "Allocation Movement", "Transfer", 1, "bca 535 self", "Self-transfer ke BCA 535")

# SeaBank "Hudaya" / "Defi Khoiru" - default Transfer ke Pihak Lain
upd([4402, 4401], "Transfer ke Pihak Lain", "Expense", 0, "seabank pihak lain")

# BCA QRIS generic
upd([1864], "Belanja Harian", "Expense", 0, "bca qris generic")

# GoPay Jonathan = teman
upd([4418], "Transfer ke Teman", "Expense", 0, "gopay jonathan")

# ALFAMRT
upd([3516], "Grocery", "Expense", 0, "alfamart")

# === INCOME ===
print()
print("=== INCOME ===")

# Jago "-" -> Penerimaan dari Pihak Lain
jago_blank = [4234, 4198, 4147, 4230]
upd(jago_blank, "Penerimaan dari Pihak Lain", "Income", 0, "jago blank")

# SeaBank Telkomsel
upd([4345], "Pulsa & Data", "Income", 0, "seabank telkomsel")

# SeaBank blank kecil (Rp7, Rp5, Rp1)
upd([4339, 4342, 4341], "Investment Movement", "Transfer", 1, "seabank blank kecil", "Bunga/artefak kecil")

# BCA Dana Masuk sisa 13 -> exclude + tandai
bca_dana_ids = [491, 1854, 1769, 1258, 1811, 1430, 1048, 350, 1794, 351, 561, 1614, 1577]
upd(bca_dana_ids, "Penerimaan dari Pihak Lain (Pending)", "Income", 1,
    "bca dana masuk pending", "V3-STEP-99 pending review")

con.commit()
print()

n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc final: Expense {n_exp}, Income {n_inc}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
