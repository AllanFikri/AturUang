import pdfplumber, sqlite3, re, shutil, datetime as dt
from pathlib import Path
from collections import defaultdict

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
PDF_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi Rekening Jago")

ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = DB.with_name(f"{DB.name}.bak-jago-reparse-{ts}")
shutil.copy2(DB, bak)
print(f"Backup: {bak.name}")
print()

MONTH_ID = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
            "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12",
            "Mei":"05","Agu":"08","Okt":"10","Des":"12"}

def col_of(x):
    if x < 60: return "date"
    if x < 200: return "sumber"
    if x < 298: return "rinci"
    if x < 438: return "catat"
    if x < 532: return "jumlah"
    return "saldo"

def parse_amount(s):
    if not s: return None
    s = s.strip().replace(" ", "").replace("Rp", "").replace("rp", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    m = re.match(r"^([+\-])?(\d+)(?:\.(\d+))?$", s)
    if not m: return None
    sign = -1 if m.group(1) == "-" else 1
    try:
        return sign * float(f"{m.group(2)}.{m.group(3) or '0'}")
    except:
        return None

# Cutoff: potong kalau ketemu header section
CUTOFF = re.compile(r"\b(Mata Uang Dalam|Saldo Sebelumnya|Total Pemasukan|Total Pengeluaran|Halaman|PT Bank Jago|www\.jago\.com|Akun Aktif)\b", re.I)

def clean_field(s, max_len=120):
    if not s: return ""
    s = re.sub(r"\s+", " ", s).strip()
    m = CUTOFF.search(s)
    if m:
        s = s[:m.start()].strip()
    return s[:max_len]

def parse_pdf(path):
    records = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=2)
            if not words: continue
            
            rows = defaultdict(list)
            for w in words:
                key = round(w["top"] / 3) * 3
                rows[key].append(w)
            
            y_sorted = sorted(rows.keys())
            cur_rec = None
            
            for y in y_sorted:
                ws = sorted(rows[y], key=lambda x: x["x0"])
                cols = defaultdict(list)
                for w in ws:
                    cols[col_of(w["x0"])].append(w["text"])
                
                date_txt = " ".join(cols["date"])
                m_date = re.match(r"(\d{1,2})\s+(\w{3,})\s+(\d{4})", date_txt)
                
                if m_date:
                    if cur_rec: records.append(cur_rec)
                    day, mon, year = m_date.groups()
                    mon_id = MONTH_ID.get(mon[:3])
                    if not mon_id:
                        cur_rec = None; continue
                    iso = f"{year}-{mon_id}-{day.zfill(2)}"
                    cur_rec = {
                        "date": iso, "time": "",
                        "sumber": " ".join(cols["sumber"]),
                        "rinci": " ".join(cols["rinci"]),
                        "catat": " ".join(cols["catat"]),
                        "jumlah": " ".join(cols["jumlah"]),
                    }
                elif cur_rec:
                    m_time = re.match(r"^(\d{2})\.(\d{2})", date_txt)
                    if m_time and not cur_rec["time"]:
                        cur_rec["time"] = f"{m_time.group(1)}:{m_time.group(2)}"
                    if cols["sumber"]: cur_rec["sumber"] += " " + " ".join(cols["sumber"])
                    if cols["rinci"]: cur_rec["rinci"] += " " + " ".join(cols["rinci"])
                    if cols["catat"]: cur_rec["catat"] += " " + " ".join(cols["catat"])
                    if cols["jumlah"] and not cur_rec["jumlah"]:
                        cur_rec["jumlah"] = " ".join(cols["jumlah"])
            
            if cur_rec:
                records.append(cur_rec); cur_rec = None
    
    for r in records:
        r["amount"] = parse_amount(r["jumlah"]) if r["jumlah"] else None
        r["sumber"] = clean_field(r["sumber"], 80)
        r["rinci"] = clean_field(r["rinci"], 60)
        r["catat"] = clean_field(r["catat"], 80)
    
    return [r for r in records if r["amount"] is not None]

# DEDUP PDF
by_month = {}
for p in PDF_DIR.glob("Jago_monthly_statement_*.pdf"):
    m = re.search(r"statement_(\w+)_(\d{4})_", p.name)
    if not m: continue
    mon_id = MONTH_ID.get(m.group(1)[:3])
    if not mon_id: continue
    key = f"{m.group(2)}-{mon_id}"
    if key not in by_month or p.stat().st_size > by_month[key].stat().st_size:
        by_month[key] = p

all_recs = []
for month, path in sorted(by_month.items()):
    recs = parse_pdf(str(path))
    for r in recs: r["source_month"] = month
    all_recs.extend(recs)

print(f"Records: {len(all_recs)}")
print()

# MATCH & APPLY
con = sqlite3.connect(str(DB))
cur = con.cursor()
now = dt.datetime.now().isoformat(timespec="seconds")

db_rows = cur.execute("""
    SELECT id, date, amount FROM transactions
    WHERE source_refs LIKE 'jago_statement%' AND is_deleted = 0
""").fetchall()

db_index = defaultdict(list)
for tid, date, amount in db_rows:
    a = round(float(amount), 2)
    db_index[(date, a)].append(tid)
    db_index[(date, -a)].append(tid)

matched = 0
used_ids = set()
for r in all_recs:
    key = (r["date"], round(r["amount"], 2))
    cands = [c for c in db_index.get(key, []) if c not in used_ids]
    if not cands: continue
    tid = cands[0]
    used_ids.add(tid)
    
    new_desc = f"[Jago Statement] {r['rinci']}"
    if r["sumber"]: new_desc += f" | {r['sumber']}"
    if r["catat"]: new_desc += f" | {r['catat']}"
    
    cur.execute("""
        UPDATE transactions
        SET description = ?,
            notes = COALESCE(notes, '') || ' | [V3-STEP-104: jago reparse]',
            updated_at = ?
        WHERE id = ?
    """, (new_desc[:200], now, tid))
    matched += 1

con.commit()
print(f"Updated: {matched}")
print()

# Verify
print("=== Sample 8 hasil update ===")
for tid, d, desc in cur.execute("""
    SELECT id, date, description FROM transactions
    WHERE source_refs LIKE 'jago_statement%' 
      AND notes LIKE '%V3-STEP-104%' AND is_deleted = 0
    ORDER BY date LIMIT 8
"""):
    print(f"  {d}  {desc[:110]}")

print()
print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
con.close()
