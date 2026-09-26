import os
import re
import sys
import json
import sqlite3
import datetime as dt
from pathlib import Path

import pytesseract
from PIL import Image

tess_cmd = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
pytesseract.pytesseract.tesseract_cmd = tess_cmd

APPLY = "--apply" in sys.argv

SP_DIR = Path(r"C:\A User Main Storage\Downloads\Mutasi Rekening All Bank\Mutasi ShopeePay-20260827T171501Z-1-001\Mutasi ShopeePay")

# ===== Bulan mapping =====
MONTH_MAP = {
    "januari": "01", "january": "01",
    "februari": "02", "february": "02",
    "maret": "03", "march": "03",
    "april": "04",
    "mei": "05", "may": "05",
    "juni": "06", "june": "06",
    "juli": "07", "july": "07",
    "agustus": "08", "august": "08",
    "september": "09",
    "oktober": "10", "october": "10",
    "november": "11",
    "desember": "12", "december": "12",
}

# ===== Regex =====
RE_LINE_AMOUNT = re.compile(r"^(Payment|Refund|Top Up|Transfer Sent|Transfer Received)\s+([+-])\s*Rp\s*([\d\.]+)", re.IGNORECASE)
RE_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)
RE_FAILED = re.compile(r"\b(Failed|Gagal)\b", re.IGNORECASE)


def extract_period_from_filename(fname: str) -> str | None:
    """Ambil YYYY-MM dari nama file seperti 'September 2025.jpg' atau 'Juni 2026 (1).jpeg'."""
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", fname, re.IGNORECASE)
    if m:
        month_name = m.group(1).lower()
        year = m.group(2)
        if month_name in MONTH_MAP:
            return f"{year}-{MONTH_MAP[month_name]}"
    return None


def classify(line_type: str, sign: str) -> tuple[str, str, str]:
    """Return (transaction_type, category, money_context)."""
    lt = line_type.lower().strip()

    if lt == "payment":
        return "Expense", "Other / Miscellaneous", "Personal"
    if lt == "refund":
        return "Income", "Refund", "Personal"
    if lt == "top up":
        # From Bank → ShopeePay = Transfer (owned → owned)
        return "Transfer", "Allocation Movement", "Personal"
    if lt == "transfer sent":
        # Keluar ke pihak ketiga
        return "Expense", "Transfer ke Teman", "Personal"
    if lt == "transfer received":
        # Masuk dari pihak ketiga
        return "Income", "Transfer dari Teman", "Personal"
    return "Transfer", "Other / Miscellaneous", "Personal"


def parse_ocr_text(text: str, fallback_period: str | None):
    """Parse OCR output jadi list dict transaksi."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    rows = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = RE_LINE_AMOUNT.match(line)

        if not m:
            i += 1
            continue

        line_type = m.group(1)
        sign = m.group(2)
        amt_str = m.group(3)

        try:
            amount = float(amt_str.replace(".", ""))
        except ValueError:
            i += 1
            continue

        # merchant = line berikut
        merchant = lines[i + 1] if i + 1 < len(lines) else ""

        # date = search 1-3 line berikut
        date_str = None
        for j in range(i + 1, min(i + 4, len(lines))):
            dm = RE_DATE.search(lines[j])
            if dm:
                day = dm.group(1).zfill(2)
                month_name = dm.group(2).lower()
                yr = dm.group(3)
                if month_name in MONTH_MAP:
                    date_str = f"{yr}-{MONTH_MAP[month_name]}-{day}"
                    break

        # failed = search 4 line berikut
        is_failed = False
        for j in range(i + 1, min(i + 5, len(lines))):
            if RE_FAILED.search(lines[j]):
                is_failed = True
                break

        # kalau failed, skip
        if is_failed:
            i += 1
            continue

        # kalau date belum ketemu, fallback ke period filename
        if not date_str and fallback_period:
            date_str = f"{fallback_period}-01"

        if not date_str:
            i += 1
            continue

        tx_type, category, ctx = classify(line_type, sign)

        rows.append({
            "date": date_str,
            "time": "00:00:00",
            "type": tx_type,
            "amount": amount,
            "merchant": merchant[:80],
            "line_type": line_type,
            "sign": sign,
            "category": category,
            "money_context": ctx,
        })

        i += 1

    return rows


# ===== MAIN =====
files = sorted([
    f for f in SP_DIR.iterdir()
    if f.suffix.lower() in (".jpg", ".jpeg") and "WhatsApp" not in f.name
])

print(f"=== MULAI PROSES {len(files)} FILE ===")
print()

all_rows = []
summary_per_file = []

for f in files:
    period = extract_period_from_filename(f.name)

    img = Image.open(f)
    text = pytesseract.image_to_string(img, lang="eng", config="--psm 6")

    rows = parse_ocr_text(text, period)

    for r in rows:
        r["source_file"] = f.name

    all_rows.extend(rows)
    summary_per_file.append((f.name, period, len(rows)))

    print(f"{f.name[:50]:<52} period={period or '-'}  {len(rows)} transaksi")

print()
print("=" * 70)
print(f"TOTAL: {len(all_rows)} transaksi dari {len(files)} file")
print("=" * 70)
print()

# Group by type
from collections import Counter
by_type = Counter(r["type"] for r in all_rows)
print("Berdasarkan tipe:")
for t, c in by_type.most_common():
    print(f"  {t:<12}: {c}")

print()
by_category = Counter(r["category"] for r in all_rows)
print("Berdasarkan kategori:")
for cat, c in by_category.most_common():
    print(f"  {cat:<28}: {c}")

print()
print("=== Sample 10 transaksi ===")
for r in all_rows[:10]:
    print(f"  {r['date']}  {r['type']:<10}  Rp{r['amount']:>12,.2f}  [{r['category']:<20}]  {r['merchant'][:40]}")

# Save to JSON
out_path = Path(r"C:\A User Main Storage\Downloads\aturuang_scratch\shopeepay_parsed.json")
out_path.write_text(json.dumps(all_rows, indent=2, ensure_ascii=False), encoding="utf-8")
print()
print(f"Saved: {out_path}")

# ===== APPLY =====
if APPLY:
    print()
    print("=" * 70)
    print("APPLYING ke DB")
    print("=" * 70)

    # Backup check
    db_path = Path("runtime/money_tracks.db")
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = Path(f"runtime/money_tracks.db.bak-shopeepay-{ts}")
    import shutil
    shutil.copy2(db_path, bak)
    print(f"Backup: {bak}")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    import_id_base = f"shopeepay_ocr:{ts[:8]}"
    inserted = 0

    for r in all_rows:
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
            r["date"], r["time"], r["type"], r["amount"],
            "ShopeePay" if r["type"] == "Expense" else "",
            "ShopeePay" if r["type"] == "Income" else "",
            f"[ShopeePay OCR] {r['line_type']} - {r['merchant']}",
            r["category"], "Personal / Self",
            r["money_context"], "", "Confirmed", "medium", 0.0,
            1, f"ShopeePay evidence (V3-STEP-100E, {r['source_file']})",
            "legacy", "", import_id,
            f"[V3-STEP-100E: ShopeePay OCR from {r['source_file']}; {r['line_type']}]",
            0, 0,
            dt.datetime.now().isoformat(timespec="seconds"),
            dt.datetime.now().isoformat(timespec="seconds"),
        ))
        inserted += 1

    con.commit()

    print(f"Inserted: {inserted}")

    # Verify
    r = cur.execute("""
        SELECT COUNT(*) as n, SUM(amount) as t
        FROM transactions WHERE source_refs LIKE 'shopeepay_ocr%' AND is_deleted=0
    """).fetchone()
    print(f"Total di DB: {r[0]} row, Rp{r[1]:,.2f}")
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")

    con.close()
else:
    print()
    print("DRY-RUN ONLY. Jalankan ulang dengan --apply untuk commit.")
