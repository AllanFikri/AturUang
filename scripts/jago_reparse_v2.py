"""
jago_reparse_v2.py — selective per-file Jago PDF parser, idempotent.

Perbedaan dari v3_104:
- v3: scan semua PDF, UPDATE semua tx (full scan)
- v2: process per-file, INSERT baru + UPDATE description yang sudah ada

Usage:
    python jago_reparse_v2.py                          # dry-run folder
    python jago_reparse_v2.py --apply
    python jago_reparse_v2.py --file PATH --apply
    python jago_reparse_v2.py --dir CUSTOM --apply
"""
import pdfplumber, sqlite3, re, sys, argparse, shutil, datetime as dt
from pathlib import Path
from collections import defaultdict

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
DEFAULT_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi Rekening Jago")

MONTH_ID = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
            "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12",
            "Mei":"05","Agu":"08","Okt":"10","Des":"12"}

CUTOFF = re.compile(r"\b(Mata Uang Dalam|Saldo Sebelumnya|Total Pemasukan|Total Pengeluaran|Halaman|PT Bank Jago|www\.jago\.com|Akun Aktif)\b", re.I)


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
                    cur_rec = {"date": iso, "time": "",
                               "sumber": " ".join(cols["sumber"]),
                               "rinci": " ".join(cols["rinci"]),
                               "catat": " ".join(cols["catat"]),
                               "jumlah": " ".join(cols["jumlah"])}
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


def process_apply(cur, records, source_file):
    """Return (updated, inserted, skipped)."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    updated = 0; inserted = 0; skipped = 0
    used_ids = set()
    
    for r in records:
        # Cari existing by (date, amount)
        cands = cur.execute("""
            SELECT id, description FROM transactions
            WHERE source_refs LIKE 'jago_statement%' AND is_deleted = 0
              AND date = ? AND (amount = ? OR amount = ?)
            ORDER BY id
        """, (r["date"], r["amount"], abs(r["amount"]))).fetchall()
        
        # Pilih yang belum dipakai
        existing = None
        for tid, desc in cands:
            if tid not in used_ids:
                existing = (tid, desc); break
        
        new_desc = f"[Jago Statement] {r['rinci']}"
        if r["sumber"]: new_desc += f" | {r['sumber']}"
        if r["catat"]: new_desc += f" | {r['catat']}"
        new_desc = new_desc[:200]
        
        if existing:
            tid, old_desc = existing
            used_ids.add(tid)
            if old_desc != new_desc:
                cur.execute("""
                    UPDATE transactions
                    SET description = ?,
                        notes = COALESCE(notes,'') || ' | [Jago v2: ' || ? || ']',
                        updated_at = ?
                    WHERE id = ?
                """, (new_desc, source_file, now, tid))
                updated += 1
            else:
                skipped += 1
        else:
            # INSERT baru
            import_id = f"jago_statement:{r['date'][:7]}"
            ttype = "Income" if r["amount"] > 0 else "Expense"
            abs_amt = abs(r["amount"])
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
                r["date"], r["time"] or "00:00:00", ttype, abs_amt,
                "Jago" if ttype == "Expense" else "",
                "Jago" if ttype == "Income" else "",
                new_desc, "Other / Miscellaneous", "Personal / Self",
                "Personal", "", "Confirmed", "medium", 0.0,
                1, "Jago statement (pending rules)", "legacy", "", import_id,
                f"[Jago v2: {source_file}]",
                0, 0, now, now
            ))
            inserted += 1
    return updated, inserted, skipped


import hashlib

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def dedup_files(files):
    seen = {}
    unique = []
    dups = []
    for f in files:
        h = file_hash(f)
        if h in seen:
            dups.append((f, seen[h]))
        else:
            seen[h] = f
            unique.append(f)
    return unique, dups



def dedup_records(records):
    """Dedup by (date, amount, rinci, catat)."""
    seen = set()
    out = []
    dups = 0
    for r in records:
        key = (r.get("date"), r.get("amount"), r.get("rinci"), r.get("catat"))
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dups


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
        files = sorted([f for f in folder.glob("Jago_monthly_statement_*.pdf")
                        if "_archive" not in str(f)])
    
    # Dedup by hash
    files, dups = dedup_files(files)
    if dups:
        print(f"Duplikat (skip): {len(dups)}")
        for dup, orig in dups:
            print(f"  {dup.name}")
            print(f"    = duplikat dari: {orig.name}")
        print()
    
    print(f"Files unique: {len(files)}")
    print()
    
    all_recs = []
    global_seen = set()
    dup_count = 0
    for f in files:
        recs = parse_pdf(str(f))
        recs_unique = []
        for r in recs:
            key = (r.get("date"), r.get("amount"), r.get("rinci"), r.get("catat"))
            if key in global_seen:
                dup_count += 1
                continue
            global_seen.add(key)
            recs_unique.append(r)
        all_recs.extend([(r, f.name) for r in recs_unique])
        marker = "" if len(recs_unique) == len(recs) else f"  [dedup: -{len(recs)-len(recs_unique)}]"
        print(f"  {f.name[:55]:<57} {len(recs_unique)}{marker}")
    
    if dup_count:
        print()
        print(f"Total duplikat record di-skip: {dup_count}")
    
    print()
    print(f"Total parsed: {len(all_recs)}")
    print()
    
    if not args.apply:
        print("DRY-RUN. Pakai --apply untuk apply.")
        return
    
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"{DB.name}.bak-jago-v2-{ts}")
    shutil.copy2(DB, bak)
    print(f"Backup: {bak.name}")
    print()
    
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    
    tu = ti = tsk = 0
    for rec, src in all_recs:
        u, i, s = process_apply(cur, [rec], src)
        tu += u; ti += i; tsk += s
    con.commit()
    
    n_db = cur.execute("""
        SELECT COUNT(*) FROM transactions 
        WHERE source_refs LIKE 'jago_statement%' AND is_deleted = 0
    """).fetchone()[0]
    
    print(f"Updated : {tu}")
    print(f"Inserted: {ti}")
    print(f"Skipped : {tsk}")
    print(f"Total jago_statement di DB: {n_db}")
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()
