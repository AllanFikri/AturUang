"""
export_rules.py — export semua rules ke CSV untuk review di Google Sheets.

Usage:
    python export_rules.py                    # export semua
    python export_rules.py --dir CUSTOM_PATH  # custom output dir
"""
import sqlite3, csv, argparse
from pathlib import Path
from datetime import datetime

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
DEFAULT_OUT = Path(r"C:\A User Main Storage\Downloads\aturuang_scratch\rules_export")


def export_all(out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    
    # === 1. Combined all_rules.csv ===
    all_path = out_dir / f"all_rules_{ts}.csv"
    with open(all_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "rule_type", "id", "enabled", "priority",
            "match_field", "match_value",
            "category", "transaction_type", "exclude_from_budget",
            "reason_or_owner", "relation_or_source", "notes",
            "created_at", "updated_at"
        ])
        
        # merchant_rules
        for rid, mt, pat, cat, ttype, excl, reason, prio, src, enabled, notes, ca, ua in cur.execute("""
            SELECT id, match_type, pattern, category, transaction_type,
                   exclude_from_budget, exclusion_reason, priority, source,
                   enabled, notes, created_at, updated_at
            FROM merchant_rules ORDER BY priority, id
        """):
            w.writerow([
                "merchant", rid, enabled, prio,
                "contains", pat,
                cat, ttype or "", excl if excl is not None else "",
                reason or "", src or "", notes or "",
                ca or "", ua or ""
            ])
        
        # va_prefix_rules
        for rid, bank, va, name, cat, ttype, excl, reason, prio, src, enabled, notes, ca, ua in cur.execute("""
            SELECT id, bank, va_prefix, merchant_name, category, transaction_type,
                   exclude_from_budget, exclusion_reason, priority, source,
                   enabled, notes, created_at, updated_at
            FROM va_prefix_rules ORDER BY bank, va_prefix
        """):
            w.writerow([
                "va_prefix", rid, enabled, prio,
                "va", f"{bank}:{va}",
                cat, ttype or "", excl if excl is not None else "",
                reason or "", src or "", notes or "",
                ca or "", ua or ""
            ])
        
        # counterparty_accounts
        for rid, bank, acct, aliases, owner, rel, internal, notes, ca, ua in cur.execute("""
            SELECT id, bank, account_number, aliases, owner_name, relation,
                   is_internal, notes, created_at, updated_at
            FROM counterparty_accounts ORDER BY relation, id
        """):
            w.writerow([
                "counterparty", rid, 1, 1,
                "account", f"{bank}:{acct}",
                rel, "Transfer" if rel in ("self",) else "", internal,
                owner, aliases or "", notes or "",
                ca or "", ua or ""
            ])
    
    # === 2. Individual files (per rule type) ===
    mr_path = out_dir / f"merchant_rules_{ts}.csv"
    with open(mr_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "priority", "pattern", "category", "transaction_type",
                    "exclude_from_budget", "exclusion_reason", "source", "enabled", "notes"])
        for row in cur.execute("""
            SELECT id, priority, pattern, category, transaction_type,
                   exclude_from_budget, exclusion_reason, source, enabled, notes
            FROM merchant_rules ORDER BY priority, id
        """):
            w.writerow([r if r is not None else "" for r in row])
    
    va_path = out_dir / f"va_prefix_rules_{ts}.csv"
    with open(va_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "bank", "va_prefix", "merchant_name", "category",
                    "transaction_type", "exclude_from_budget", "exclusion_reason",
                    "priority", "source", "enabled", "notes"])
        for row in cur.execute("""
            SELECT id, bank, va_prefix, merchant_name, category, transaction_type,
                   exclude_from_budget, exclusion_reason, priority, source, enabled, notes
            FROM va_prefix_rules ORDER BY bank, va_prefix
        """):
            w.writerow([r if r is not None else "" for r in row])
    
    cp_path = out_dir / f"counterparty_accounts_{ts}.csv"
    with open(cp_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "bank", "account_number", "aliases", "owner_name",
                    "relation", "is_internal", "notes"])
        for row in cur.execute("""
            SELECT id, bank, account_number, aliases, owner_name,
                   relation, is_internal, notes
            FROM counterparty_accounts ORDER BY relation, id
        """):
            w.writerow([r if r is not None else "" for r in row])
    
    con.close()
    
    print("=" * 70)
    print("EXPORT COMPLETE")
    print("=" * 70)
    print()
    for p in [all_path, mr_path, va_path, cp_path]:
        size = p.stat().st_size
        print(f"  {p.name}  ({size:,} bytes)")
    print()
    print(f"Folder: {out_dir}")
    print()
    print("Cara pakai di Google Sheets:")
    print("  1. Buka Google Drive")
    print("  2. Upload file CSV (bisa pilih salah satu atau semua)")
    print("  3. Klik kanan → Open with → Google Sheets")
    print("  4. Edit di Sheets — kolom 'category' & 'pattern' yang paling penting")
    print("  5. Kalau mau balikin ke DB, save as CSV → jalankan import_rules.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    export_all(args.dir)


if __name__ == "__main__":
    main()
