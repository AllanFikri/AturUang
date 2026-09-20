"""
Money Tracks V12 — Root Compatibility Server Entry Point
Delegates execution directly to aturuang.server.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from aturuang.server import main, run_server, RequestHandler, get_composer, set_composer
from aturuang.web_composer import QuickCaptureComposer

__all__ = [
    "main",
    "run_server",
    "RequestHandler",
    "QuickCaptureComposer",
    "get_composer",
    "set_composer",
]

if __name__ == "__main__":
    main()
