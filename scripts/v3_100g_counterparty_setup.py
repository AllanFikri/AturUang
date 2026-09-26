import sqlite3, json, shutil, re, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")

# Backup
ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-cpa-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()

# === 1. CREATE TABLE ===
cur.execute("""
CREATE TABLE IF NOT EXISTS counterparty_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank TEXT NOT NULL,
    account_number TEXT NOT NULL,
    aliases TEXT,
    owner_name TEXT NOT NULL,
    relation TEXT NOT NULL,
    is_internal INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
)
""")
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_cpa_bank_acct ON counterparty_accounts(bank, account_number)")
print("Table counterparty_accounts: OK")
print()

# === 2. SEED ===
now = dt.datetime.now().isoformat(timespec="seconds")

SEED = [
    dict(bank="BCA", account_number="3680432880", aliases=["*2880"],
         owner_name="Allan Fikri Mahardika Santosa", relation="self", is_internal=1,
         notes="Rekening utama Allan (BCA)"),
    dict(bank="JAGO", account_number="103558407045", aliases=[],
         owner_name="Allan Fikri Mahardika Santosa", relation="self", is_internal=1,
         notes="Akun Jago Allan (045)"),
    dict(bank="BNI", account_number="0433824732", aliases=[],
         owner_name="Faza Prilia Laksmi (Aca)", relation="partner", is_internal=0,
         notes="Pacar Allan"),
    dict(bank="JAGO", account_number="509121315258", aliases=[],
         owner_name="Almaas Hillary Camillia (Mbak Erin)", relation="family", is_internal=0,
         notes="Family — Mbak Erin = Almaas (1 orang)"),
    dict(bank="JAGO", account_number="5193", aliases=["*5193"],
         owner_name="Mohammad Misbahul Khoir", relation="friend", is_internal=0,
         notes="Teman"),
]

inserted = 0
for s in SEED:
    try:
        cur.execute("""
            INSERT INTO counterparty_accounts
            (bank, account_number, aliases, owner_name, relation, is_internal, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["bank"], s["account_number"], json.dumps(s["aliases"]), s["owner_name"],
              s["relation"], s["is_internal"], s["notes"], now, now))
        inserted += 1
    except sqlite3.IntegrityError as e:
        print(f"  skip (dup): {s['bank']} {s['account_number']}")

con.commit()
total = cur.execute("SELECT COUNT(*) FROM counterparty_accounts").fetchone()[0]
print(f"Seed inserted: {inserted}")
print(f"Total registry: {total} rows")
print()

# === 3. BUILD LOOKUP ===
lookup = {}
for bank, acct, aliases_json, owner, rel, internal in cur.execute("""
    SELECT bank, account_number, aliases, owner_name, relation, is_internal
    FROM counterparty_accounts
"""):
    lookup[acct] = (bank, owner, rel, internal)
    if aliases_json:
        for a in json.loads(aliases_json):
            lookup[a.replace("*", "")] = (bank, owner, rel, internal)

# === 4. QUERY DAMPAK ===
rows = cur.execute("""
    SELECT id, date, amount, transaction_type, description, source_refs
    FROM transactions WHERE is_deleted = 0
""").fetchall()

RE_ACCT = re.compile(r"(?:Bank Jago|BCA|BNI|BRI|Mandiri|SeaBank|Blu|Jago)\s+(\*?\d{3,20})", re.I)

matches = []
for tid, date, amount, ttype, desc, src in rows:
    d = desc or ""
    for m in RE_ACCT.finditer(d):
        acct = m.group(1).replace("*", "")
        if acct in lookup:
            bank, owner, rel, internal = lookup[acct]
            matches.append((tid, date, amount, ttype, d, src or "-", bank, acct, owner, rel, internal))
            break

print("=== DAMPAK: tx yang match registry ===")
print(f"Total: {len(matches)} tx")
print()

# Group by (bank, owner, relation)
from collections import defaultdict
by_owner = defaultdict(lambda: {"n": 0, "total": 0.0})
for _, _, amount, _, _, _, bank, acct, owner, rel, internal in matches:
    by_owner[(bank, owner, rel)]["n"] += 1
    by_owner[(bank, owner, rel)]["total"] += float(amount)

print(f"{'Bank':<8} {'Owner':<40} {'Rel':<10} {'Tx':>4} {'Total':>14}")
print("-" * 90)
for (bank, owner, rel), v in sorted(by_owner.items(), key=lambda x: -x[1]["total"]):
    print(f"{bank:<8} {owner[:38]:<40} {rel:<10} {v['n']:>4} Rp{v['total']:>14,.2f}")

print()
print("=== SAMPLE 15 tx yang akan direklasifikasi ===")
for tid, date, amount, ttype, d, src, bank, acct, owner, rel, internal in matches[:15]:
    src_tag = src.split(":")[0]
    print(f"  {date}  Rp{float(amount):>10,.0f}  [{ttype:<10}]  →  {rel:<10} {owner[:35]:<37} [{src_tag}]")
    print(f"      {d[:100]}")

con.close()
