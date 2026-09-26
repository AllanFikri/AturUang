import pdfplumber, sqlite3, re, shutil, datetime as dt
from pathlib import Path
from collections import defaultdict

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
PDF_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi Seabank")

ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-seabank-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

MONTH_ID = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
            "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

STOPWORDS = {"Transfer","Pembayaran","Flip","Shopee","ShopeePay","Bunga","Pajak",
             "SA","TABUNGAN","RINCIAN","TRANSAKSI","KELUAR","MASUK","(IDR)",
             "REKENING","KORAN","TANGGAL","HALAMAN","dr","Bunga","Tabungan"}

def is_stopword(txt):
    t = txt.strip()
    if not t: return True
    if t in STOPWORDS: return True
    if re.match(r"^\d+$", t): return True
    if re.match(r"^\d+\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)$", t): return True
    if "REKENING" in t or "HALAMAN" in t or "halaman" in t: return True
    return False

def parse_amount(s):
    if not s: return None
    s = s.strip().replace(".", "")
    try: return float(s)
    except: return None

def parse_pdf(path):
    records = []
    with pdfplumber.open(path) as pdf:
        p1 = pdf.pages[0].extract_text() or ""
        ym = re.search(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", p1)
        year = int(ym.group(3)) if ym else 2025
        
        all_rows = []
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not words: continue
            rows = defaultdict(list)
            for w in words:
                key = round(w["top"] / 3) * 3
                rows[key].append(w)
            for y in sorted(rows.keys()):
                ws = sorted(rows[y], key=lambda x: x["x0"])
                all_rows.append((y, ws))
        
        current = None
        prev_rows = []  # buffer 3 baris terakhir
        
        for idx, (y, ws) in enumerate(all_rows):
            date_words = [w["text"] for w in ws if 40 <= w["x0"] < 75]
            date_text = " ".join(date_words).strip()
            m = re.match(r"^(\d{1,2})\s+([A-Z]{3})$", date_text)
            
            if m:
                if current: records.append(current)
                day, mon = m.groups()
                mon_id = MONTH_ID.get(mon, "01")
                iso = f"{year}-{mon_id}-{day.zfill(2)}"
                
                amount = None
                for w in ws:
                    if w["x0"] >= 360 and re.match(r"^\d[\d\.]*$", w["text"]):
                        amount = parse_amount(w["text"])
                        break
                
                # Cari name di buffer 3 baris ke belakang
                name = None
                for py, pws in reversed(prev_rows[-3:]):
                    nw = [w["text"] for w in pws if 170 <= w["x0"] < 360]
                    if nw:
                        txt = " ".join(nw).strip()
                        if not is_stopword(txt) and len(txt) > 4:
                            name = txt
                            break
                
                current = {"date": iso, "amount": amount, "name": name,
                           "type": None, "source": None}
            elif current:
                ctx = [w["text"] for w in ws if 170 <= w["x0"] < 360]
                if ctx:
                    txt = " ".join(ctx).strip()
                    if not is_stopword(txt):
                        if current["type"] is None:
                            current["type"] = txt
                        elif current["source"] is None:
                            current["source"] = txt
            
            prev_rows.append((y, ws))
        
        if current: records.append(current)
    
    return [r for r in records if r["amount"] is not None]

pdfs = sorted(PDF_DIR.glob("*.pdf"))
all_recs = []
for p in pdfs:
    recs = parse_pdf(str(p))
    for r in recs: r["pdf"] = p.name
    all_recs.extend(recs)

print(f"Records: {len(all_recs)}")
print()

con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

db_rows = cur.execute("""
    SELECT id, date, amount FROM transactions
    WHERE source_refs LIKE 'seabank_statement%' AND is_deleted = 0
""").fetchall()

db_index = defaultdict(list)
for tid, date, amount in db_rows:
    a = round(float(amount), 2)
    db_index[(date, a)].append(tid)

matched = 0
used = set()
for r in all_recs:
    key = (r["date"], round(r["amount"], 2))
    cands = [c for c in db_index.get(key, []) if c not in used]
    if not cands: continue
    tid = cands[0]
    used.add(tid)
    
    parts = []
    if r["type"]: parts.append(r["type"])
    if r["source"]: parts.append(r["source"])
    if r["name"]: parts.append(r["name"])
    new_desc = "[SeaBank Statement] " + " | ".join(parts) if parts else "[SeaBank Statement]"
    
    cur.execute("""
        UPDATE transactions
        SET description = ?,
            notes = COALESCE(notes, '') || ' | [V3-STEP-107: seabank reparse]',
            updated_at = ?
        WHERE id = ?
    """, (new_desc[:200], now, tid))
    matched += 1

con.commit()
print(f"Updated: {matched}")
print()

print("=== Sample 10 hasil update ===")
for tid, d, desc in cur.execute("""
    SELECT id, date, description FROM transactions
    WHERE source_refs LIKE 'seabank_statement%'
      AND notes LIKE '%V3-STEP-107%' AND is_deleted = 0
    ORDER BY date LIMIT 10
"""):
    print(f"  {d}  {desc[:110]}")

print()
n_exp = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Expense' AND is_deleted=0").fetchone()[0]
n_inc = cur.execute("SELECT COUNT(*) FROM transactions WHERE category='Other / Miscellaneous' AND transaction_type='Income' AND is_deleted=0").fetchone()[0]
print(f"Other/Misc sisa: Expense {n_exp} tx, Income {n_inc} tx")

print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
