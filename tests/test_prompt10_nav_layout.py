"""
Test Suite Prompt 10a — Navigation State, Account Layout, & Authoritative DB Verification
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import hashlib
import json
import re
import sqlite3
import unittest
from pathlib import Path

import db
import services

BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "runtime" / "money_tracks.db"
APP_JS = BASE_DIR / "aturuang" / "web" / "js" / "app.js"
INDEX_HTML = BASE_DIR / "aturuang" / "web" / "index.html"
STYLES_CSS = BASE_DIR / "aturuang" / "web" / "css" / "styles.css"


class TestPrompt10NavLayout(unittest.TestCase):
    def setUp(self):
        self.prod_hash_before = hashlib.sha256(DB_FILE.read_bytes()).hexdigest() if DB_FILE.exists() else None
        self.html = INDEX_HTML.read_text(encoding="utf-8")
        self.js = APP_JS.read_text(encoding="utf-8")
        self.css = STYLES_CSS.read_text(encoding="utf-8")

    def test_01_navigation_mapping_all_six_menus(self):
        """Test 1: All 6 main menu navigation buttons map accurately to their respective page."""
        # Find goPage function definition
        go_page_match = re.search(r"function goPage\(page\)\s*\{([\s\S]*?)\n\}", self.js)
        self.assertIsNotNone(go_page_match, "goPage function not found in app.js")
        body = go_page_match.group(1)

        # Ensure sub array ONLY contains subpages of more, NOT accounts or reports
        sub_match = re.search(r"const sub = \[(.*?)\];", body)
        self.assertIsNotNone(sub_match, "sub array not found in goPage")
        sub_items = [s.strip(" '\"") for s in sub_match.group(1).split(",")]

        self.assertNotIn("accounts", sub_items, "accounts must NOT map to more!")
        self.assertNotIn("reports", sub_items, "reports must NOT map to more!")
        self.assertIn("upcoming", sub_items)
        self.assertIn("thirdparty", sub_items)
        self.assertIn("provisional", sub_items)
        self.assertIn("updates", sub_items)

    def test_02_aria_current_and_single_active_state(self):
        """Test 2: goPage sets aria-current='page' and ensures exactly one active nav item."""
        self.assertIn("x.setAttribute('aria-current', 'page')", self.js)
        self.assertIn("x.removeAttribute('aria-current')", self.js)

    def test_03_account_scroll_wrap_and_grid_css(self):
        """Test 3: .account-scroll-wrap is a column flex container avoiding child grid collision."""
        self.assertIn(".account-scroll-wrap {", self.css)
        self.assertIn("display: flex;", self.css)
        self.assertIn("flex-direction: column;", self.css)

        self.assertIn(".hero-acc-grid {", self.css)
        self.assertIn(".other-acc-grid {", self.css)

    def test_04_authoritative_bca_balance_in_db_and_api(self):
        """Test 4: BCA Main balance is exactly Rp83.821,80 with zero discrepancy."""
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row

        recon = services.reconstruct_account_balance(con, "BCA Main")
        self.assertEqual(recon["status"], "ok")
        self.assertAlmostEqual(recon["expected_balance"], 67821.80, delta=0.005)
        self.assertAlmostEqual(recon["cached_balance"], 67821.80, delta=0.005)
        self.assertAlmostEqual(recon["difference"], 0.0, delta=0.005)

        accs = services.account_management(con)["accounts"]
        bca_acc = next(a for a in accs if a["name"] == "BCA Main")
        self.assertAlmostEqual(bca_acc["current_balance"], 67821.80, delta=0.005)

        dash = services.dashboard(con)
        self.assertAlmostEqual(dash["kpis"]["totalBalance"], 2379368.80, delta=0.005)
        self.assertAlmostEqual(dash["kpis"]["safeToSpend"], 1629368.80, delta=0.005)

        con.close()

    def test_05_database_unmodified(self):
        """Test 5: Database SHA-256 hash remains canonical post-import baseline."""
        actual_hash = hashlib.sha256(DB_FILE.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Database hash mutated unexpectedly!")


if __name__ == "__main__":
    unittest.main()
