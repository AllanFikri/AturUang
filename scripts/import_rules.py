"""
import_rules.py — import rules yang sudah diedit di Google Sheets kembali ke DB.

Usage:
    python import_rules.py --file path/to/all_rules_edited.csv           # dry-run (preview)
    python import_rules.py --file path/to/all_rules_edited.csv --apply  # apply

Format CSV: sama dengan output export_rules.py (kolom: rule_type, id, enabled,
priority, match_field, match_value, category, transaction_type, exclude_from_budget,
reason_or_owner, relation_or_source, notes).

Perubahan yang dideteksi:
- Rule baru (id kosong atau id=0) → INSERT
- Rule lama dengan perubahan → UPDATE
- Rule lama dengan 'enabled=0' → disable (tidak dihapus)
"""
import sqlite3, csv, argparse, shutil, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")


def parse_int(v, default=None):
    if v is None or str(v).strip() == "": return default
    try: return int(float(v))
    except: return default


def parse_str(v, strip=True):
    if v is None: return ""
    return str(v).strip() if strip else str(v)


def import_rules(csv_path, apply=False):
    csv_path = Path(csv_path)
    if not csv_path.exists():
        print(f"ERROR: file tidak ditemukan: {csv_path}")
        return
    
    if apply:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = DB.with_name(f"{DB.name}.bak-import-{ts}")
        shutil.copy2(DB, bak)
        print(f"Backup: {bak.name}")
        print()
    
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    now = dt.datetime.now().isoformat(timespec="seconds")
    
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    print(f"Rows di CSV: {len(rows)}")
    print()
    
    stats = {"insert": 0, "update": 0, "skip": 0, "error": 0}
    errors = []
    updates = []
    inserts = []
    
    for i, row in enumerate(rows, 2):
        rtype = parse_str(row.get("rule_type")).lower()
        rid = parse_int(row.get("id"))
        enabled = parse_int(row.get("enabled"), 1)
        prio = parse_int(row.get("priority"), 100)
        match_value = parse_str(row.get("match_value"), strip=False)
        cat = parse_str(row.get("category"))
        ttype = parse_str(row.get("transaction_type")) or None
        excl = row.get("exclude_from_budget")
        excl = parse_int(excl) if str(excl).strip() != "" else None
        reason = parse_str(row.get("reason_or_owner"))
        rel_or_src = parse_str(row.get("relation_or_source"))
        notes = parse_str(row.get("notes"))
        
        if not cat:
            stats["error"] += 1
            errors.append(f"  line {i}: kategori kosong (id={rid})")
            continue
        
        # === merchant_rules ===
        if rtype == "merchant":
            pat = parse_str(match_value, strip=False)
            if not pat:
                stats["error"] += 1
                errors.append(f"  line {i}: pattern kosong")
                continue
            
            if not rid or rid == 0:
                # INSERT baru
                inserts.append(("merchant", (pat, cat, ttype, excl, prio, notes, now, now)))
            else:
                # Cek ada?
                exists = cur.execute("SELECT id, pattern, category, transaction_type, exclude_from_budget, priority, enabled FROM merchant_rules WHERE id=?", (rid,)).fetchone()
                if not exists:
                    inserts.append(("merchant", (pat, cat, ttype, excl, prio, notes, now, now)))
                    continue
                # Bandingkan
                old_pat, old_cat, old_ttype, old_excl, old_prio, old_enabled = exists[1], exists[2], exists[3], exists[4], exists[5], exists[6]
                changed = (old_pat != pat or old_cat != cat or 
                           (old_ttype or "") != (ttype or "") or
                           (old_excl if old_excl is not None else -1) != (excl if excl is not None else -1) or
                           old_prio != prio or old_enabled != enabled)
                if changed:
                    updates.append(("merchant", (pat, cat, ttype, excl, prio, enabled, notes, now, rid)))
                else:
                    stats["skip"] += 1
        
        # === va_prefix_rules ===
        elif rtype == "va_prefix":
            if ":" not in match_value:
                stats["error"] += 1
                errors.append(f"  line {i}: format match_value harus 'BANK:VA'")
                continue
            bank, va = match_value.split(":", 1)
            if not rid or rid == 0:
                inserts.append(("va", (bank, va, cat, ttype, excl, prio, notes, now, now)))
            else:
                exists = cur.execute("SELECT bank, va_prefix, category, transaction_type, exclude_from_budget, priority, enabled FROM va_prefix_rules WHERE id=?", (rid,)).fetchone()
                if not exists:
                    inserts.append(("va", (bank, va, cat, ttype, excl, prio, notes, now, now)))
                    continue
                changed = (exists[0] != bank or exists[1] != va or exists[2] != cat or
                           (exists[3] or "") != (ttype or "") or
                           (exists[4] if exists[4] is not None else -1) != (excl if excl is not None else -1) or
                           exists[5] != prio or exists[6] != enabled)
                if changed:
                    updates.append(("va", (bank, va, cat, ttype, excl, prio, enabled, notes, now, rid)))
                else:
                    stats["skip"] += 1
        
        # === counterparty_accounts ===
        elif rtype == "counterparty":
            if ":" not in match_value:
                stats["error"] += 1
                errors.append(f"  line {i}: format match_value harus 'BANK:ACCOUNT'")
                continue
            bank, acct = match_value.split(":", 1)
            if not rid or rid == 0:
                stats["error"] += 1
                errors.append(f"  line {i}: counterparty baru butuh id manual")
            else:
                exists = cur.execute("SELECT owner_name, relation FROM counterparty_accounts WHERE id=?", (rid,)).fetchone()
                if not exists:
                    stats["error"] += 1
                    errors.append(f"  line {i}: counterparty id={rid} tidak ada")
                    continue
                if exists[0] != reason or exists[1] != cat:
                    cur.execute("""
                        UPDATE counterparty_accounts
                        SET owner_name=?, relation=?, updated_at=?
                        WHERE id=?
                    """, (reason, cat, now, rid))
                    stats["update"] += 1
        
        else:
            stats["error"] += 1
            errors.append(f"  line {i}: rule_type tidak dikenal '{rtype}'")
    
    print(f"=== Preview ===")
    print(f"  INSERT baru : {len(inserts)}")
    print(f"  UPDATE      : {len(updates)}")
    print(f"  SKIP (sama) : {stats['skip']}")
    print(f"  ERROR       : {stats['error'] + len(errors)}")
    print()
    
    if errors[:10]:
        print("=== Error sample ===")
        for e in errors[:10]:
            print(e)
        print()
    
    if updates[:15]:
        print("=== Sample UPDATE ===")
        for kind, vals in updates[:15]:
            print(f"  [{kind}] {vals}")
        print()
    
    if not apply:
        print("DRY-RUN. Jalankan dengan --apply untuk apply.")
        con.close()
        return
    
    print("=== APPLY ===")
    for kind, vals in inserts:
        if kind == "merchant":
            pat, cat, ttype, excl, prio, notes, ca, ua = vals
            cur.execute("""
                INSERT INTO merchant_rules (match_type, pattern, category, transaction_type,
                    exclude_from_budget, priority, source, enabled, notes, created_at, updated_at)
                VALUES ('contains', ?, ?, ?, ?, ?, 'user_edit', 1, ?, ?, ?)
            """, (pat, cat, ttype, excl, prio, notes, ca, ua))
        elif kind == "va":
            bank, va, cat, ttype, excl, prio, notes, ca, ua = vals
            cur.execute("""
                INSERT INTO va_prefix_rules (bank, va_prefix, merchant_name, category,
                    transaction_type, exclude_from_budget, priority, source, enabled, notes, created_at, updated_at)
                VALUES (?, ?, '', ?, ?, ?, ?, 'user_edit', 1, ?, ?, ?)
            """, (bank, va, cat, ttype, excl, prio, notes, ca, ua))
    print(f"  Inserted: {len(inserts)}")
    
    for kind, vals in updates:
        if kind == "merchant":
            pat, cat, ttype, excl, prio, enabled, notes, ua, rid = vals
            cur.execute("""
                UPDATE merchant_rules
                SET pattern=?, category=?, transaction_type=?, exclude_from_budget=?,
                    priority=?, enabled=?, notes=?, updated_at=?
                WHERE id=?
            """, (pat, cat, ttype, excl, prio, enabled, notes, ua, rid))
        elif kind == "va":
            bank, va, cat, ttype, excl, prio, enabled, notes, ua, rid = vals
            cur.execute("""
                UPDATE va_prefix_rules
                SET bank=?, va_prefix=?, category=?, transaction_type=?, exclude_from_budget=?,
                    priority=?, enabled=?, notes=?, updated_at=?
                WHERE id=?
            """, (bank, va, cat, ttype, excl, prio, enabled, notes, ua, rid))
    print(f"  Updated: {len(updates)}")
    
    con.commit()
    print()
    print(f"Integrity: {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path CSV hasil edit")
    parser.add_argument("--apply", action="store_true", help="Apply ke DB (default dry-run)")
    args = parser.parse_args()
    import_rules(args.file, apply=args.apply)


if __name__ == "__main__":
    main()


