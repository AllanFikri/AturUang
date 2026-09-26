"""
archive_processed.py — pindahkan file yang sudah diproses ke folder _archive.

Cara kerja:
1. Baca processed_files yang status='processed'
2. Cek file masih ada di path asli
3. Rename: YYYY-MM-DD_<filename_asli>
4. Pindah ke <folder_asli>/_archive/
5. Update file_path di DB

Usage:
    python archive_processed.py --dry-run          # preview (tidak pindah)
    python archive_processed.py --source shopeepay
    python archive_processed.py --source jago
    python archive_processed.py --source seabank
    python archive_processed.py --all              # semua source
"""
import sqlite3, shutil, argparse, datetime as dt
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")

SOURCE_MAP = {
    "shopeepay": Path(r"H:\My Drive\Money Tracks\Mutasi ShopeePay"),
    "jago":      Path(r"H:\My Drive\Money Tracks\Mutasi Rekening Jago"),
    "seabank":   Path(r"H:\My Drive\Money Tracks\Mutasi Seabank"),
}


def get_con():
    return sqlite3.connect(str(DB))


def find_target(src_type):
    """Cari folder source berdasarkan tipe."""
    return SOURCE_MAP.get(src_type)


def resolve_collision(target_path):
    """Kalau nama target sudah ada, append _v2, _v3, dst."""
    if not target_path.exists():
        return target_path
    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    n = 2
    while True:
        candidate = parent / f"{stem}_v{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def archive_one(cur, file_id, original_path, source_type, date_prefix, dry_run=False):
    """Pindah 1 file ke _archive."""
    original = Path(original_path)
    
    if not original.exists():
        return "missing", f"file hilang: {original}"
    
    archive_dir = original.parent / "_archive"
    new_name = f"{date_prefix}_{original.name}"
    target = archive_dir / new_name
    
    if target.exists() or any(archive_dir.glob(f"*_{original.name}")) if archive_dir.exists() else False:
        # Sudah pernah di-archive
        return "already_archived", f"sudah ada di _archive"
    
    target = resolve_collision(target)
    
    if dry_run:
        return "would_move", f"{original.name} → _archive/{target.name}"
    
    archive_dir.mkdir(exist_ok=True)
    try:
        shutil.move(str(original), str(target))
        now = dt.datetime.now().isoformat(timespec="seconds")
        cur.execute("""
            UPDATE processed_files
            SET file_path = ?, processed_at = ?
            WHERE id = ?
        """, (str(target), now, file_id))
        return "moved", f"{original.name} → _archive/{target.name}"
    except Exception as e:
        return "error", f"{original.name}: {e}"


def cmd_archive(con, sources=None, dry_run=False):
    print("=" * 70)
    print("ARCHIVE PROCESSED FILES" + (" [DRY-RUN]" if dry_run else ""))
    print("=" * 70)
    print()
    
    cur = con.cursor()
    date_prefix = dt.datetime.now().strftime("%Y-%m-%d")
    
    total = {"moved": 0, "would_move": 0, "already_archived": 0, "missing": 0, "error": 0}
    
    for src_type in (sources or SOURCE_MAP.keys()):
        base = SOURCE_MAP.get(src_type)
        if not base:
            print(f"=== {src_type}: source tidak dikenal ===")
            continue
        
        print(f"=== {src_type}: {base} ===")
        
        rows = cur.execute("""
            SELECT id, file_path FROM processed_files
            WHERE source_type = ? AND status = 'processed'
              AND file_path NOT LIKE '%_archive%'
            ORDER BY file_path
        """, (src_type,)).fetchall()
        
        if not rows:
            print(f"  (tidak ada file untuk di-archive)")
            print()
            continue
        
        print(f"  Kandidat: {len(rows)} file")
        print()
        
        for file_id, fpath in rows:
            status, msg = archive_one(cur, file_id, fpath, src_type, date_prefix, dry_run)
            total[status] = total.get(status, 0) + 1
            prefix = "[DRY] " if dry_run else ""
            icon = {
                "moved": "✓",
                "would_move": "→",
                "already_archived": "·",
                "missing": "!",
                "error": "✗"
            }.get(status, "?")
            print(f"    {icon} {prefix}{msg}")
        
        print()
        con.commit()
    
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if dry_run:
        print(f"  Akan dipindah : {total.get('would_move', 0)}")
    else:
        print(f"  Dipindah         : {total.get('moved', 0)}")
    print(f"  Sudah di _archive: {total.get('already_archived', 0)}")
    print(f"  File hilang      : {total.get('missing', 0)}")
    print(f"  Error            : {total.get('error', 0)}")
    print()


def cmd_status(con):
    print("=" * 70)
    print("STATUS — file di root vs di _archive")
    print("=" * 70)
    print()
    
    for src_type, base in SOURCE_MAP.items():
        print(f"=== {src_type}: {base} ===")
        if not base.exists():
            print("  (folder tidak ada)")
            print()
            continue
        
        root_files = [f for f in base.iterdir() if f.is_file() and not f.name.startswith(".")]
        archive_dir = base / "_archive"
        archived = [f for f in archive_dir.iterdir() if f.is_file()] if archive_dir.exists() else []
        
        print(f"  Root    : {len(root_files)} file")
        print(f"  Archive : {len(archived)} file")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview tanpa pindah file")
    parser.add_argument("--source", type=str, choices=list(SOURCE_MAP.keys()) + ["all"], help="Source tertentu")
    parser.add_argument("--status", action="store_true", help="Lihat status root vs archive")
    args = parser.parse_args()
    
    con = get_con()
    
    if args.status:
        cmd_status(con)
    else:
        if args.source == "all" or not args.source:
            sources = list(SOURCE_MAP.keys())
        else:
            sources = [args.source]
        cmd_archive(con, sources=sources, dry_run=args.dry_run)
    
    con.close()


if __name__ == "__main__":
    main()
