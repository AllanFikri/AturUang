"""
AturUang — Launcher Kanonis
Menampilkan root, web root, dan database path tanpa mencetak data finansial pribadi.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from aturuang.config import PROJECT_ROOT, WEB_ROOT, RUNTIME_ROOT, DB_FILE
from aturuang.server import run_server

def main():
    print("=" * 60)
    print("ATURUANG / MONEY TRACKS V12 — CANONICAL LAUNCHER")
    print("=" * 60)
    print(f"Source Root   : {PROJECT_ROOT}")
    print(f"Web Root      : {WEB_ROOT}")
    print(f"Runtime Root  : {RUNTIME_ROOT}")
    print(f"Database File : {DB_FILE}")
    print("=" * 60)
    print("Server starting on http://127.0.0.1:5050 ...")
    run_server(port=5050)

if __name__ == "__main__":
    main()
