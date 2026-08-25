"""
AturUang — Konfigurasi Path Kanonis
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent / "web"
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
BACKUP_ROOT = RUNTIME_ROOT / "backups"

# Ensure runtime dirs exist
RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

_env_db = os.environ.get("MONEY_TRACKS_DB_PATH")
if _env_db:
    DB_FILE = Path(_env_db).resolve()
else:
    DB_FILE = RUNTIME_ROOT / "money_tracks.db"
