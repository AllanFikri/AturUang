"""
Money Tracks V12 — Root Compatibility Server Entry Point
Delegates execution directly to aturuang.server.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from aturuang.server import main, run_server, RequestHandler

if __name__ == "__main__":
    main()
