"""
import_orchestrator.py — orchestrator untuk import statement otomatis.

Fungsi:
1. Scan folder sumber (dari config WATCH_FOLDERS)
2. Detect format berdasarkan filename/path
3. Panggil parser yang sesuai (kalau file belum diproses)
4. Setelah parse, jalankan RuleEngine ke semua tx baru (category='Other / Miscellaneous')
5. Tx yang tidak match rules → insert ke pending_review_queue
6. Log semua ke session_log.md

Usage:
    python import_orchestrator.py                    # scan semua folder
    python import_orchestrator.py --dry-run          # scan tapi tidak panggil parser
    python import_orchestrator.py --folder PATH      # scan folder tertentu
    python import_orchestrator.py --status           # lihat status processed_files
"""
import sqlite3, hashlib, subprocess, sys, argparse, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")
ACT_ROOT = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1")
SCRIPTS_DIR = ACT_ROOT / "scripts"
LOG_FILE = Path(r"C:\A User Main Storage\Downloads\aturuang_scratch\session_log.md")

# === CONFIG: folder + mapping ke parser ===
WATCH_FOLDERS = [
    {
        "path": Path(r"H:\My Drive\Money Tracks\Mutasi ShopeePay"),
        "patterns": ["Screenshot_*.jpg"],
        "source_type": "shopeepay_ocr",
        "parser": "import_shopeepay_ocr_v4.py",
        "parser_args": ["--apply"],
        "auto_apply": True,
    },
    {
        "path": Path(r"H:\My Drive\Money Tracks\Mutasi Rekening BCA"),
        "patterns": ["3680432880_*.pdf"],
        "source_type": "bca_statement",
        "parser": "bca_reparse_v2.py",
        "parser_args": ["--apply"],
        "manual_command": "python scripts\\bca_reparse_v2.py --apply",
        "destructive": False,
    },
    {
        "path": Path(r"H:\My Drive\Money Tracks\Mutasi Rekening Jago"),
        "patterns": ["Jago_monthly_statement_*.pdf"],
        "source_type": "jago_statement",
        "parser": "jago_reparse_v2.py",
        "parser_args": ["--apply"],
        "auto_apply": True,
    },
    {
        "path": Path(r"H:\My Drive\Money Tracks\Mutasi Seabank"),
        "patterns": ["Seabank_Statement_*.pdf"],
        "source_type": "seabank_statement",
        "parser": "seabank_reparse_v2.py",
        "parser_args": ["--apply"],
        "auto_apply": True,
    },
]


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_processed(con, fpath, fhash):
    return con.execute("""
        SELECT id FROM processed_files WHERE file_path = ? AND file_hash = ?
    """, (str(fpath), fhash)).fetchone() is not None


def log_processed(con, fpath, fhash, size, source_type, parser, status, error=None):
    now = dt.datetime.now().isoformat(timespec="seconds")
    con.execute("""
        INSERT OR REPLACE INTO processed_files
        (file_path, file_hash, file_size, source_type, parser_used, status, error, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(fpath), fhash, size, source_type, parser, status, error, now))
    con.commit()


def append_log(msg):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n### [{ts}] ORCHESTRATOR\n")
        f.write(msg)
        f.write("\n")


def run_parser(parser_name, extra_args=None, files=None):
    """Jalankan parser. Kalau files di-set, pakai --files selective."""
    parser_path = SCRIPTS_DIR / parser_name
    if not parser_path.exists():
        return False, f"parser tidak ada: {parser_path}"
    
    cmd = [sys.executable, str(parser_path)]
    if files:
        cmd.append("--files")
        cmd.extend(str(f) for f in files)
    if extra_args:
        cmd.extend(extra_args)
    
    print(f"    -> Menjalankan: {' '.join(str(c) for c in cmd[:4])} ... ({len(files) if files else 'scan'} files)")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    
    if result.returncode != 0:
        return False, f"exit={result.returncode}\n{result.stderr[-500:]}"
    return True, result.stdout[-1500:] if result.stdout else ""


def apply_rules_to_new(con):
    """Apply rules ke tx yang masih Other/Misc, queue yang tidak match."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from apply_rules import RuleEngine
    
    cur = con.cursor()
    engine = RuleEngine()
    now = dt.datetime.now().isoformat(timespec="seconds")
    
    rows = cur.execute("""
        SELECT id, description, transaction_type, category
        FROM transactions
        WHERE is_deleted = 0 AND category IN ('Other / Miscellaneous', '', 'Uncategorized')
    """).fetchall()
    
    matched = 0
    queued = 0
    
    for tid, desc, ttype, cat in rows:
        r = engine.apply({"description": desc, "transaction_type": ttype})
        if r:
            cur.execute("""
                UPDATE transactions
                SET category = ?, transaction_type = COALESCE(?, transaction_type),
                    exclude_from_budget = ?, budget_exclusion_reason = ?,
                    updated_at = ?
                WHERE id = ?
            """, (r["category"], r.get("transaction_type"),
                  r.get("exclude_from_budget", 0),
                  r.get("reason", "")[:200], now, tid))
            matched += 1
        else:
            # Insert ke queue
            cur.execute("""
                INSERT INTO pending_review_queue
                (transaction_id, reason, suggestion_category, suggestion_confidence,
                 status, created_at)
                VALUES (?, 'no_rule_match', NULL, NULL, 'pending', ?)
            """, (tid, now))
            queued += 1
    
    con.commit()
    engine.close()
    return matched, queued


def cmd_scan(con, folder_override=None, dry_run=False):
    print("=" * 70)
    print("IMPORT ORCHESTRATOR — SCAN")
    print("=" * 70)
    print()
    
    folders = WATCH_FOLDERS
    if folder_override:
        folders = [f for f in WATCH_FOLDERS if str(f["path"]) == folder_override]
        if not folders:
            print(f"Folder {folder_override} tidak di WATCH_FOLDERS")
            return
    
    total_new = 0
    total_skipped = 0
    total_parser_ok = 0
    total_parser_fail = 0
    
    for cfg in folders:
        fpath = cfg["path"]
        print(f"=== {fpath} ===")
        if not fpath.exists():
            print(f"  -- folder tidak ada")
            print()
            continue
        
        files_to_check = []
        for pat in cfg["patterns"]:
            files_to_check.extend(fpath.glob(pat))
        
        files_to_check = sorted([f for f in set(files_to_check) if "_archive" not in str(f)])
        print(f"  Total file: {len(files_to_check)}")
        
        new_files = []
        for f in files_to_check:
            fh = file_hash(f)
            if is_processed(con, f, fh):
                total_skipped += 1
            else:
                new_files.append((f, fh))
        
        print(f"  Sudah diproses: {len(files_to_check) - len(new_files)}")
        print(f"  File baru     : {len(new_files)}")
        print()
        
        if dry_run:
            for f, fh in new_files[:5]:
                print(f"    [DRY] {f.name} ({f.stat().st_size:,} bytes)")
            if len(new_files) > 5:
                print(f"    ... ({len(new_files) - 5} lagi)")
            total_new += len(new_files)
            print()
            continue
        
        # Process new files — batch sekali untuk semua file baru di folder ini
        if new_files:
            file_paths = [f for f, fh in new_files]
            print(f"  -> Proses {len(file_paths)} file sekaligus via {cfg['parser']}")
            ok, output = run_parser(cfg["parser"], cfg["parser_args"], files=file_paths)
            if ok:
                print(f"    OK")
                for f, fh in new_files:
                    log_processed(con, f, fh, f.stat().st_size, cfg["source_type"], cfg["parser"], "processed")
                    total_parser_ok += 1
            else:
                print(f"    FAILED: {output[:300]}")
                for f, fh in new_files:
                    log_processed(con, f, fh, f.stat().st_size, cfg["source_type"], cfg["parser"], "failed", error=output[:500])
                    total_parser_fail += 1
            total_new += len(new_files)
        print()
    
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  File baru         : {total_new}")
    print(f"  Sudah diproses    : {total_skipped}")
    print(f"  Parser OK         : {total_parser_ok}")
    print(f"  Parser FAILED     : {total_parser_fail}")
    print()
    
    if not dry_run and total_parser_ok > 0:
        print("=== Apply rules ke tx yang masih Other/Misc ===")
        matched, queued = apply_rules_to_new(con)
        print(f"  Matched otomatis  : {matched}")
        print(f"  Queue (perlu review): {queued}")
        print()
        append_log(f"Scan: {total_new} file baru, {total_parser_ok} parser OK\nApply rules: {matched} matched, {queued} queued")


def cmd_status(con):
    print("=" * 70)
    print("ORCHESTRATOR — STATUS")
    print("=" * 70)
    print()
    
    rows = con.execute("""
        SELECT source_type, status, COUNT(*), MAX(processed_at)
        FROM processed_files
        GROUP BY source_type, status
        ORDER BY source_type, status
    """).fetchall()
    
    if not rows:
        print("  (belum ada file yang diproses)")
        return
    
    print(f"{'Source Type':<22} {'Status':<12} {'Count':>6}  Last Processed")
    print("-" * 80)
    for src, status, cnt, last in rows:
        print(f"{src:<22} {status:<12} {cnt:>6}  {last}")
    print()
    
    # Queue
    n_q = con.execute("SELECT COUNT(*) FROM pending_review_queue WHERE status='pending'").fetchone()[0]
    print(f"  Pending review queue: {n_q}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Scan tanpa panggil parser")
    parser.add_argument("--folder", type=str, help="Scan folder tertentu saja")
    parser.add_argument("--status", action="store_true", help="Lihat status processed_files")
    args = parser.parse_args()
    
    con = sqlite3.connect(str(DB))
    
    if args.status:
        cmd_status(con)
    else:
        cmd_scan(con, folder_override=args.folder, dry_run=args.dry_run)
    
    con.close()


if __name__ == "__main__":
    main()

