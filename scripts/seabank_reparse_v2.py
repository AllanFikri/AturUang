"""
seabank_reparse_v2.py — selective per-file SeaBank PDF parser, idempotent.

Perbedaan dari v3_107:
- v3: scan semua PDF, UPDATE semua tx (full scan)
- v2: dedup by file hash + record content, INSERT baru + UPDATE

Usage:
    python seabank_reparse_v2.py                              # dry-run folder
    python seabank_reparse_v2.py --apply
    python seabank_reparse_v2.py --file PATH --apply
    python seabank_reparse_v2.py --dir CUSTOM --apply
"""
import pdfplumber, sqlite3, re, sys, argparse, shutil, hashlib, datetime as dt
from pathlib import Path
from collections import defaultdict

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
DEFAULT_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi Seabank")

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
            # Cek apakah halaman ini tabel BUNGA/PAJAK/DEPOSITO
            page_text = " ".join(w["text"] for w in words)
            if "RINCIAN BUNGA" in page_text or "DEPOSITO" in page_text or "BUNGA & PAJAK" in page_text:
                # Halaman ini bukan transaksi, skip
                continue
            
            for y in sorted(rows.keys()):
                ws = sorted(rows[y], key=lambda x: x["x0"])
                all_rows.append((y, ws))
        
        current = None
        prev_rows = []
        stop_parsing = False
        
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
    
    return [r for r in records if r["amount"] is not None and r["amount"] != 0]


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_desc(r):
    parts = []
    if r["type"]: parts.append(r["type"])
    if r["source"]: parts.append(r["source"])
    if r["name"]: parts.append(r["name"])
    return "[SeaBank Statement] " + " | ".join(parts) if parts else "[SeaBank Statement]"


def apply_records(cur, records, source_file):
    now = dt.datetime.now().isoformat(timespec="seconds")
    updated = 0; inserted = 0; skipped = 0
    used_ids = set()
    
    for r in records:
        new_desc = build_desc(r)[:200]
        # Cari existing
        cands = cur.execute("""
            SELECT id, description FROM transactions
            WHERE source_refs LIKE 'seabank_statement%' AND is_deleted = 0
              AND date = ? AND amount = ?
            ORDER BY id
        """, (r["date"], r["amount"])).fetchall()
        
        existing = None
        for tid, desc in cands:
            if tid not in used_ids:
                existing = (tid, desc); break
        
        if existing:
            tid, old_desc = existing
            used_ids.add(tid)
            if old_desc != new_desc:
                cur.execute("""
                    UPDATE transactions
                    SET description = ?,
                        notes = COALESCE(notes,'') || ' | [SeaBank v2: ' || ? || ']',
                        updated_at = ?
                    WHERE id = ?
                """, (new_desc, source_file, now, tid))
                updated += 1
            else:
                skipped += 1
        else:
            import_id = f"seabank_statement:{r['date'][:7]}"
            ttype = "Income" if (r["type"] or "") == "Transfer" and r["name"] and "Masuk" in str(r["type"]) else "Expense"
            # Detect from name/source
            cur.execute("""
                INSERT INTO transactions (
                    canonical_id, date, time, transaction_type, amount,
                    account_from, account_to, description, category, for_with_whom,
                    money_context, settlement_kind, status, confidence, budget_effect,
                    exclude_from_budget, budget_exclusion_reason, budget_rule_version,
                    subtype, source_refs, notes, manual_edited, is_deleted,
                    created_at, updated_at
                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["date"], "00:00:00", "Expense", r["amount"],
                "SeaBank", "", new_desc, "Other / Miscellaneous", "Personal / Self",
                "Personal", "", "Confirmed", "medium", 0.0,
                1, "SeaBank statement (pending rules)", "legacy", "", import_id,
                f"[SeaBank v2: {source_file}]",
                0, 0, now, now
            ))
            inserted += 1
    return updated, inserted, skipped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--file", type=str)
    p.add_argument("--files", nargs="+")
    p.add_argument("--dir", type=str, default=str(DEFAULT_DIR))
    args = p.parse_args()
    
    if args.file:
        files = [Path(args.file)]
    elif args.files:
        files = [Path(f) for f in args.files]
    else:
        folder = Path(args.dir)
        if not folder.exists():
            print(f"Folder tidak ada: {folder}")
            sys.exit(1)
        files = sorted([f for f in folder.glob("*.pdf")
                        if "_archive" not in str(f)])
    
    # Dedup by file hash
    seen_hash = {}
    unique = []
    for f in files:
        h = file_hash(f)
        if h in seen_hash:
            continue
        seen_hash[h] = f
        unique.append(f)
    files = unique
    
    print(f"Files: {len(files)}")
    print()
    
    all_recs = []
    global_seen = set()
    dup_count = 0
    for f in files:
        recs = parse_pdf(str(f))
        recs_unique = []
        for r in recs:
            key = (r["date"], r["amount"], r["type"], r["source"], r["name"])
            if key in global_seen:
                dup_count += 1; continue
            global_seen.add(key)
            recs_unique.append(r)
        all_recs.extend([(r, f.name) for r in recs_unique])
        marker = "" if len(recs_unique) == len(recs) else f"  [dedup: -{len(recs)-len(recs_unique)}]"
        print(f"  {f.name[:55]:<57} {len(recs_unique)}{marker}")
    
    if dup_count:
        print()
        print(f"Total duplikat: {dup_count}")
    print()
    print(f"Total parsed: {len(all_recs)}")
    print()
    
    if not args.apply:
        print("DRY-RUN. Pakai --apply untuk apply.")
        return
    
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"{DB.name}.bak-sbv2-{ts}")
    shutil.copy2(DB, bak)
    print(f"Backup: {bak.name}")
    print()
    
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    
    tu = ti = tsk = 0
    for rec, src in all_recs:
        u, i, s = apply_records(cur, [rec], src)
        tu += u; ti += i; tsk += s
    con.commit()
    
    n_db = cur.execute("""
        SELECT COUNT(*) FROM transactions 
        WHERE source_refs LIKE 'seabank_statement%' AND is_deleted = 0
    """).fetchone()[0]
    
    print(f"Updated : {tu}")
    print(f"Inserted: {ti}")
    print(f"Skipped : {tsk}")
    print(f"Total seabank di DB: {n_db}")
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()