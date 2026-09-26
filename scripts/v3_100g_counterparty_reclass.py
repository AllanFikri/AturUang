import sqlite3, json, shutil, re, datetime as dt
from pathlib import Path
from collections import defaultdict

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")

# Kategori berdasarkan relation
CAT_MAP = {
    "self":     "Allocation Movement",
    "partner":  "Transfer ke Partner",
    "family":   "Transfer ke Keluarga",
    "friend":   "Transfer ke Teman",
    "merchant": "Pembayaran Merchant",
    "unknown":  "Transfer ke Pihak Lain",
}

# Backup
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-reclass-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()

# Build lookup dari registry
lookup = {}
for bank, acct, aliases_json, owner, rel, internal in cur.execute("""
    SELECT bank, account_number, aliases, owner_name, relation, is_internal
    FROM counterparty_accounts
"""):
    lookup[acct.replace("*", "")] = (bank, owner, rel)
    if aliases_json:
        for a in json.loads(aliases_json):
            lookup[a.replace("*", "")] = (bank, owner, rel)

# Cari tx yang match
rows = cur.execute("""
    SELECT id, date, amount, transaction_type, description, category
    FROM transactions WHERE is_deleted = 0
""").fetchall()

RE_ACCT = re.compile(r"(?:Bank Jago|BCA|BNI|BRI|Mandiri|SeaBank|Blu|Jago)\s+(\*?\d{3,20})", re.I)

updates = []
for tid, date, amount, ttype, desc, cat in rows:
    d = desc or ""
    for m in RE_ACCT.finditer(d):
        acct = m.group(1).replace("*", "")
        if acct in lookup:
            bank, owner, rel = lookup[acct]
            target_cat = CAT_MAP.get(rel, "Transfer ke Pihak Lain")
            if cat != target_cat:
                updates.append((tid, date, float(amount), ttype, cat, target_cat, owner, rel, d))
            break

print(f"Tx yang perlu direklasifikasi: {len(updates)}")
print()

# Preview
by_cat_change = defaultdict(lambda: {"n": 0, "total": 0.0})
for tid, date, amount, ttype, cat_old, cat_new, owner, rel, d in updates:
    by_cat_change[(cat_old, cat_new)]["n"] += 1
    by_cat_change[(cat_old, cat_new)]["total"] += amount

print(f"{'Old Category':<25} → {'New Category':<25} {'Tx':>4} {'Total':>14}")
print("-" * 85)
for (old, new), v in sorted(by_cat_change.items(), key=lambda x: -x[1]["total"]):
    print(f"{old[:23]:<25} → {new[:23]:<25} {v['n']:>4} Rp{v['total']:>14,.2f}")
print()

print("=== Preview 10 tx ===")
for tid, date, amount, ttype, cat_old, cat_new, owner, rel, d in updates[:10]:
    print(f"  [{tid}] {date}  Rp{amount:>10,.0f}")
    print(f"    {cat_old} → {cat_new}  ({rel}: {owner[:40]})")
print()

# APPLY
now = dt.datetime.now().isoformat(timespec="seconds")
updated = 0
for tid, date, amount, ttype, cat_old, cat_new, owner, rel, d in updates:
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes, '') || ?,
            updated_at = ?
        WHERE id = ?
    """, (cat_new, f" | [V3-STEP-100G: counterparty={owner} ({rel})]", now, tid))
    updated += 1

con.commit()
print(f"Updated: {updated} rows")
print()

# Verify
res = cur.execute("""
    SELECT category, COUNT(*), SUM(amount)
    FROM transactions
    WHERE source_refs LIKE 'shopeepay_ocr%' AND description LIKE '%Send to Bank%' AND is_deleted = 0
    GROUP BY category
""").fetchall()
print("=== Kategori Send to Bank (ShopeePay) setelah reklasifikasi ===")
for cat, n, tot in res:
    print(f"  {cat:<30} {n:>3} tx  Rp{tot:>14,.2f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
