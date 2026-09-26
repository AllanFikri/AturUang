import sqlite3, json, shutil, re, datetime as dt
from pathlib import Path
from collections import defaultdict

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-reclass-cp-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

# Direction-aware category mapping
CAT_OUT = {
    "family":       "Transfer ke Keluarga",
    "partner":      "Transfer ke Partner",
    "friend":       "Transfer ke Teman",
    "vendor":       "Pembayaran Vendor",
    "intermediary": "Remitansi",
    "self":         "Allocation Movement",
    "unknown":      "Transfer ke Pihak Lain",
}
CAT_IN = {
    "family":       "Penerimaan dari Keluarga",
    "partner":      "Penerimaan dari Partner",
    "friend":       "Penerimaan dari Teman",
    "vendor":       "Penerimaan dari Vendor",
    "intermediary": "Refund dari Remitansi",
    "self":         "Allocation Movement",
    "unknown":      "Penerimaan dari Pihak Lain",
}

# Build name lookup dari registry (skip numerik)
name_lookup = {}
for acct, owner, rel in cur.execute("""
    SELECT account_number, owner_name, relation FROM counterparty_accounts
"""):
    if acct.replace("*", "").isdigit(): continue
    first = owner.split()[0].upper()
    if len(first) > 3:
        name_lookup[first] = (owner, rel)

# Numeric lookup untuk tx dengan nomor akun
num_lookup = {}
for acct, owner, rel in cur.execute("""
    SELECT account_number, owner_name, relation FROM counterparty_accounts
"""):
    if acct.replace("*", "").isdigit():
        num_lookup[acct.replace("*", "")] = (owner, rel)

# Scan tx kandidat
rows = cur.execute("""
    SELECT id, date, amount, transaction_type, description, category, source_refs
    FROM transactions
    WHERE is_deleted = 0
""").fetchall()

preview = []
for tid, date, amount, ttype, desc, cat, src in rows:
    d = (desc or "").upper()
    src_tag = (src or "").split(":")[0]
    
    # Skip tx yang sudah kita handle sebelumnya (ShopeePay OCR + FTFVA + BCA Topup)
    if src_tag == "shopeepay_ocr": continue
    if "FTFVA" in d: continue
    if "BCA TOPUP" in d: continue
    
    # Skip QRIS / merchant payment — beda domain
    if "QRIS" in d: continue
    
    matched = None
    
    # Coba numeric match dulu (paling akurat)
    m = re.search(r"(?:BANK JAGO|BCA|BNI|BRI|MANDIRI|SEABANK|JAGO|BLU)\s+(\*?\d{3,20})", d)
    if m:
        acct = m.group(1).replace("*", "")
        if acct in num_lookup:
            matched = num_lookup[acct]
    
    # Kalau tidak, match by name
    if not matched:
        for first, (owner, rel) in name_lookup.items():
            if first in d:
                matched = (owner, rel)
                break
    
    if not matched: continue
    
    owner, rel = matched
    is_out = ttype in ("Expense", "Transfer")
    is_in  = ttype == "Income"
    
    if is_out:
        target_cat = CAT_OUT.get(rel, "Transfer ke Pihak Lain")
    elif is_in:
        target_cat = CAT_IN.get(rel, "Penerimaan dari Pihak Lain")
    else:
        continue
    
    if cat != target_cat:
        preview.append((tid, date, float(amount), ttype, cat, target_cat, owner, rel))

print(f"Tx perlu reklasifikasi: {len(preview)}")
print()

# Summary by target_cat
by_cat = defaultdict(lambda: {"n": 0, "total": 0.0})
for p in preview:
    by_cat[(p[4], p[5], p[7])]["n"] += 1
    by_cat[(p[4], p[5], p[7])]["total"] += p[2]

print(f"{'Old':<25} -> {'New':<30} {'Rel':<12} {'Tx':>4} {'Total':>14}")
print("-" * 100)
for (old, new, rel), v in sorted(by_cat.items(), key=lambda x: -x[1]["total"]):
    print(f"{str(old)[:23]:<25} -> {str(new)[:28]:<30} {rel:<12} {v['n']:>4} Rp{v['total']:>14,.2f}")

print()
print("=== Sample 15 ===")
for p in preview[:15]:
    tid, date, amount, ttype, co, cn, owner, rel = p
    print(f"  {date}  {ttype:<8}  Rp{amount:>12,.0f}  [{rel:<12}] {owner[:32]}")
    print(f"      {co} -> {cn}")

# APPLY
print()
print("=== APPLY ===")
updated = 0
for p in preview:
    tid, date, amount, ttype, co, cn, owner, rel = p
    cur.execute("""
        UPDATE transactions
        SET category = ?,
            notes = COALESCE(notes, '') || ?,
            updated_at = ?
        WHERE id = ?
    """, (cn, f" | [V3-STEP-100G-4: counterparty={owner} ({rel})]", now, tid))
    updated += 1

con.commit()
print(f"Updated: {updated} rows")
print()

# Verify top categories
print("=== Top 15 kategori setelah reklasifikasi ===")
for cat, n, tot in cur.execute("""
    SELECT category, COUNT(*), SUM(amount) FROM transactions
    WHERE is_deleted = 0
    GROUP BY category ORDER BY COUNT(*) DESC LIMIT 15
"""):
    print(f"  {str(cat)[:35]:<37} {n:>4} tx  Rp{tot:>14,.2f}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
