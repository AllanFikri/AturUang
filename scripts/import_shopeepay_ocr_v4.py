"""
import_shopeepay_ocr_v4.py — selective per-file parser, idempotent.

Perbedaan dari v3:
- v3: DELETE all shopeepay_ocr% → INSERT ulang dari semua file (destructive)
- v4: process per-file, INSERT hanya yang belum ada (non-destructive)

Usage:
    python import_shopeepay_ocr_v4.py                              # dry-run semua file di folder
    python import_shopeepay_ocr_v4.py --apply                      # scan folder + apply
    python import_shopeepay_ocr_v4.py --file PATH --apply          # process 1 file
    python import_shopeepay_ocr_v4.py --files PATH1 PATH2 --apply  # process banyak file
    python import_shopeepay_ocr_v4.py --dir CUSTOM --apply         # custom folder scan

Idempotency: cek (source_refs, date, amount, merchant, source_file) sebelum INSERT.
"""
import os, re, sys, json, sqlite3, argparse, shutil
import datetime as dt
from pathlib import Path
from collections import Counter

import pytesseract
from PIL import Image

tess_cmd = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
pytesseract.pytesseract.tesseract_cmd = tess_cmd

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
DEFAULT_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi ShopeePay")

MONTH_MAP = {
    "jan": "01", "january": "01", "januari": "01",
    "feb": "02", "february": "02", "februari": "02",
    "mar": "03", "march": "03", "maret": "03",
    "apr": "04", "april": "04",
    "may": "05", "mei": "05",
    "jun": "06", "june": "06", "juni": "06",
    "jul": "07", "july": "07", "juli": "07",
    "aug": "08", "august": "08", "agustus": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10", "oktober": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12", "desember": "12",
}

RE_LINE_AMOUNT = re.compile(
    r"\b(Payment|Refund|Top\s*Up|Transfer\s+Sent|Transfer\s+Received|Send\s+to\s+Bank|Withdrawal|Cashback)\s+"
    r"([+\-\u2212\u2013])\s*Rp\.?\s*([\d\.]+)",
    re.IGNORECASE
)
RE_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)
RE_FAILED = re.compile(r"\b(Failed|Gagal)\b", re.IGNORECASE)
RE_PERIOD_RANGE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)
RE_PERIOD_MONTH = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b", re.IGNORECASE)


def extract_period(text):
    m = RE_PERIOD_RANGE.search(text)
    if m:
        mn = m.group(2).lower(); yr = m.group(3)
        if mn in MONTH_MAP: return f"{yr}-{MONTH_MAP[mn]}"
    m = RE_PERIOD_MONTH.search(text)
    if m:
        mn = m.group(1).lower(); yr = m.group(2)
        if mn in MONTH_MAP: return f"{yr}-{MONTH_MAP[mn]}"
    return None


def classify(lt, sign):
    lt = lt.lower().strip()
    if lt == "payment": return "Expense", "Other / Miscellaneous"
    if lt == "refund": return "Income", "Refund"
    if lt == "top up": return "Transfer", "Allocation Movement"
    if lt == "transfer sent": return "Expense", "Transfer ke Teman"
    if lt == "transfer received": return "Income", "Transfer dari Teman"
    if lt == "send to bank": return "Transfer", "Allocation Movement"
    if lt == "withdrawal": return "Transfer", "Allocation Movement"
    if lt == "cashback": return "Income", "Cashback"
    return "Transfer", "Other / Miscellaneous"


def parse_text(text, period, source_file):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    rows = []; i = 0
    while i < len(lines):
        m = RE_LINE_AMOUNT.search(lines[i])
        if not m:
            i += 1; continue
        lt = m.group(1).strip()
        sign = m.group(2)
        if sign in ("\u2212", "\u2013"): sign = "-"
        amt_str = m.group(3)
        try:
            amt = float(amt_str.replace(".", ""))
        except ValueError:
            i += 1; continue
        merchant = lines[i+1] if i+1 < len(lines) else ""
        date_str = None
        for j in range(i+1, min(i+4, len(lines))):
            dm = RE_DATE.search(lines[j])
            if dm:
                day = dm.group(1).zfill(2); mo = dm.group(2).lower(); yr = dm.group(3)
                if mo in MONTH_MAP:
                    date_str = f"{yr}-{MONTH_MAP[mo]}-{day}"; break
        is_failed = False
        for j in range(i+1, min(i+5, len(lines))):
            if RE_FAILED.search(lines[j]): is_failed = True; break
        if is_failed: i += 1; continue
        if not date_str and period: date_str = f"{period}-01"
        if not date_str: i += 1; continue
        tx_type, cat = classify(lt, sign)
        rows.append({
            "date": date_str, "type": tx_type, "amount": amt,
            "merchant": merchant[:80], "category": cat, "line_type": lt,
            "source_file": source_file,
        })
        i += 1
    return rows


def dedup_rows(rows):
    seen = set(); out = []
    for r in rows:
        key = (r["source_file"], r["date"], r["amount"], r["line_type"], r["merchant"])
        if key in seen: continue
        seen.add(key); out.append(r)
    return out


def process_file(filepath):
    """OCR 1 file, return list of parsed rows (belum di-dedup)."""
    text = pytesseract.image_to_string(Image.open(filepath), lang="eng", config="--psm 6")
    period = extract_period(text)
    return parse_text(text, period, filepath.name)


def apply_rows(cur, rows):
    """INSERT rows yang belum ada. Return (inserted, skipped)."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    inserted = 0; skipped = 0
    for r in rows:
        # Idempotency: cek berdasarkan (date, amount, line_type, source_file marker)
        existing = cur.execute("""
            SELECT id FROM transactions
            WHERE source_refs LIKE 'shopeepay_ocr%'
              AND date = ? AND amount = ? AND is_deleted = 0
              AND description LIKE ?
              AND notes LIKE ?
        """, (r["date"], r["amount"], f"%{r['line_type']}%",
              f"%{r['source_file']}%")).fetchone()
        
        if existing:
            skipped += 1
            continue
        
        import_id = f"shopeepay_ocr:{r['date'][:7]}"
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
            r["date"], "00:00:00", r["type"], r["amount"],
            "ShopeePay" if r["type"] in ("Expense", "Transfer") else "",
            "ShopeePay" if r["type"] == "Income" else "",
            f"[ShopeePay OCR] {r['line_type']} - {r['merchant']}",
            r["category"], "Personal / Self",
            "Personal", "", "Confirmed", "medium", 0.0,
            1, "ShopeePay evidence (V3-STEP-100F)",
            "legacy", "", import_id,
            f"[V3-STEP-100F: ShopeePay OCR {r['source_file']}; {r['line_type']}]",
            0, 0, now, now,
        ))
        inserted += 1
    return inserted, skipped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--file", type=str, help="Path 1 file")
    p.add_argument("--files", nargs="+", help="Path banyak file")
    p.add_argument("--dir", type=str, default=str(DEFAULT_DIR), help="Folder scan")
    args = p.parse_args()
    
    # Kumpulkan files
    files = []
    if args.file:
        files = [Path(args.file)]
    elif args.files:
        files = [Path(f) for f in args.files]
    else:
        folder = Path(args.dir)
        if not folder.exists():
            print(f"Folder tidak ada: {folder}")
            sys.exit(1)
        files = sorted([f for f in folder.glob("Screenshot_*.jpg") 
                        if "_archive" not in str(f)])
    
    print(f"Files: {len(files)}")
    print()
    
    all_rows = []
    per_file = {}
    for f in files:
        rows = process_file(f)
        rows = dedup_rows(rows)
        per_file[f.name] = len(rows)
        all_rows.extend(rows)
        print(f"  {f.name[:55]:<57} {len(rows)}")
    
    print()
    print(f"Total parsed: {len(all_rows)}")
    print()
    
    if not args.apply:
        print("DRY-RUN. Pakai --apply untuk insert.")
        return
    
    # Backup
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"{DB.name}.bak-spv4-{ts}")
    shutil.copy2(DB, bak)
    print(f"Backup: {bak.name}")
    print()
    
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    ins, skip = apply_rows(cur, all_rows)
    con.commit()
    
    total_db = cur.execute("""
        SELECT COUNT(*) FROM transactions 
        WHERE source_refs LIKE 'shopeepay_ocr%' AND is_deleted = 0
    """).fetchone()[0]
    
    print(f"Inserted: {ins}")
    print(f"Skipped (existing): {skip}")
    print(f"Total shopeepay_ocr di DB: {total_db}")
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()
