"""
import_shopeepay_ocr_v3.py — fix for v2 parser bug
Changes:
- Remove ^ anchor (icon noise: "r Transfer Sent", "ray Transfer Received")
- Add line types: Send to Bank, Withdrawal, Cashback
- Fix RE_FAILED: remove "Reward Claimed" (that's success)
- Normalize unicode minus
- Dedup explicitly at parser level
- Clean-slate mode: delete existing shopeepay_ocr% then insert
"""
import os, re, sys, json, sqlite3, shutil
import datetime as dt
from pathlib import Path
from collections import Counter
import pytesseract
from PIL import Image

tess_cmd = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
pytesseract.pytesseract.tesseract_cmd = tess_cmd

APPLY = "--apply" in sys.argv
SP_DIR = Path(r"H:\My Drive\Money Tracks\Mutasi ShopeePay")

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


def parse(text, period):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    rows = []; i = 0
    while i < len(lines):
        m = RE_LINE_AMOUNT.search(lines[i])
        if not m:
            i += 1; continue
        lt = m.group(1).strip()
        sign = m.group(2)
        if sign in ("\u2212", "\u2013"):
            sign = "-"
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
        })
        i += 1
    return rows


files = sorted([f for f in SP_DIR.glob("Screenshot_*.jpg")])
print(f"Files: {len(files)}")
all_rows = []
for f in files:
    text = pytesseract.image_to_string(Image.open(f), lang="eng", config="--psm 6")
    period = extract_period(text)
    rows = parse(text, period)
    for r in rows: r["source_file"] = f.name
    all_rows.extend(rows)
    print(f"  {f.name[:50]:<52} {period or 'UNK'}  {len(rows)}")

print()
print(f"TOTAL raw: {len(all_rows)}")

seen = set(); deduped = []
for r in all_rows:
    key = (r["source_file"], r["date"], r["amount"], r["line_type"], r["merchant"])
    if key in seen: continue
    seen.add(key); deduped.append(r)

print(f"After dedup: {len(deduped)} (removed {len(all_rows) - len(deduped)})")
print()
print("=== BY LINE_TYPE ===")
for lt, n in sorted(Counter(r["line_type"] for r in deduped).items()):
    tot = sum(r["amount"] for r in deduped if r["line_type"] == lt)
    print(f"  {lt:<20}: {n:>3} tx, Rp{tot:>14,.2f}")

if APPLY:
    print()
    print("=== APPLYING ===")
    db_path = Path("runtime/money_tracks.db")
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = Path(f"runtime/money_tracks.db.bak-sp-v3-{ts}")
    shutil.copy2(db_path, bak)
    print(f"Backup: {bak}")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    before = cur.execute("SELECT COUNT(*) FROM transactions WHERE source_refs LIKE 'shopeepay_ocr%'").fetchone()[0]
    print(f"Deleting existing: {before} rows")
    cur.execute("DELETE FROM transactions WHERE source_refs LIKE 'shopeepay_ocr%'")

    inserted = 0
    for r in deduped:
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
            0, 0,
            dt.datetime.now().isoformat(timespec="seconds"),
            dt.datetime.now().isoformat(timespec="seconds"),
        ))
        inserted += 1
    con.commit()

    print(f"Inserted: {inserted}")
    r = cur.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE source_refs LIKE 'shopeepay_ocr%' AND is_deleted=0").fetchone()
    print(f"Total di DB: {r[0]} row, Rp{r[1]:,.2f}")
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()
else:
    print()
    print("DRY-RUN ONLY. --apply untuk commit.")
