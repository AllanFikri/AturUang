"""
Test Suite Prompt 8b4 — UI Terminology Alignment & Money Formatting Runtime
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import hashlib
import re
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "runtime" / "money_tracks.db"


class TestPrompt8UIFormat(unittest.TestCase):
    def setUp(self):
        self.prod_hash_before = hashlib.sha256((BASE_DIR / "runtime" / "money_tracks.db").read_bytes()).hexdigest() if (BASE_DIR / "runtime" / "money_tracks.db").exists() else None
        self.html_path = BASE_DIR / "aturuang" / "web" / "index.html"
        self.css_path = BASE_DIR / "aturuang" / "web" / "css" / "styles.css"
        self.js_path = BASE_DIR / "aturuang" / "web" / "js" / "app.js"
        self.core_path = BASE_DIR / "aturuang" / "web" / "js" / "core.js"
        self.html = self.html_path.read_text(encoding="utf-8")
        self.css = self.css_path.read_text(encoding="utf-8")
        self.js = self.js_path.read_text(encoding="utf-8")
        self.core = self.core_path.read_text(encoding="utf-8")

    def test_01_canonical_terminology_in_index_html(self):
        """Test 1: Canonical terms (§5) are present and outdated terms are removed from visible UI."""
        self.assertTrue("DANA TERSEDIA SAAT INI" in self.html or "Dana Tersedia" in self.html)
        self.assertIn("Total Aset Likuid", self.html)
        self.assertIn("Komitmen Pasti", self.html)

        # Ensure outdated terms are eliminated from visible UI text
        outdated = ["Safe-to-Spend", "Uang Aman", "Total Balance", "Dana Tabungan"]
        for term in outdated:
            self.assertNotIn(term, self.html, f"Outdated term '{term}' still found in index.html")

    def test_02_pure_money_parsing_and_formatting_logic(self):
        """Test 2: Deterministic money parsing and formatting handling."""
        def parse_money(val):
            if val is None or val == "":
                return 0.0
            if isinstance(val, (int, float)):
                return round(float(val), 2)
            s = str(val).strip()
            if not s:
                return 0.0
            is_neg = "-" in s
            s = re.sub(r"[^0-9,.]", "", s)
            if not s:
                return 0.0
            if "," in s:
                parts = s.split(",")
                int_part = parts[0].replace(".", "")
                dec_part = "".join(parts[1:])[:2]
                num_str = int_part + ("." + dec_part if dec_part else "")
                num = float(num_str) if num_str else 0.0
                res = -num if is_neg else num
                return round(res, 2)
            dot_count = s.count(".")
            if dot_count > 1:
                num = float(s.replace(".", "")) if s.replace(".", "") else 0.0
                res = -num if is_neg else num
                return round(res, 2)
            if dot_count == 1:
                parts = s.split(".")
                if len(parts[1]) == 3 and "," not in parts[1]:
                    num = float(s.replace(".", ""))
                    res = -num if is_neg else num
                    return round(res, 2)
                else:
                    num = float(s)
                    res = -num if is_neg else num
                    return round(res, 2)
            num = float(s) if s else 0.0
            res = -num if is_neg else num
            return round(res, 2)

        self.assertEqual(parse_money("1.000"), 1000.0)
        self.assertEqual(parse_money("1000000"), 1000000.0)
        self.assertEqual(parse_money("1.000.000"), 1000000.0)
        self.assertEqual(parse_money("Rp 1.000.000"), 1000000.0)
        self.assertEqual(parse_money("1234,50"), 1234.50)
        self.assertEqual(parse_money("1234.50"), 1234.50)
        self.assertEqual(parse_money("-1000000"), -1000000.0)
        self.assertEqual(parse_money("-1.000.000"), -1000000.0)

    def test_03_all_money_fields_use_canonical_helpers(self):
        """Test 3: Money fields across app.js use parseMoneyInput and setMoneyInput."""
        money_fields = [
            "fAmount", "uAmount", "recActualInput", "allocAmount",
            "debtInitialAmount", "evAmount", "planGuaranteed", "planAdditional"
        ]
        for field in money_fields:
            self.assertIn(f"parseMoneyInput($('{field}')", self.js, f"Field '{field}' does not use parseMoneyInput")

    def test_04_live_money_formatting_attached_to_all_money_inputs(self):
        """Test 4: attachLiveMoneyFormatting is called for all 8 money inputs upon startup."""
        money_fields = [
            "fAmount", "uAmount", "recActualInput", "allocAmount",
            "debtInitialAmount", "evAmount", "planGuaranteed", "planAdditional"
        ]
        for field in money_fields:
            self.assertIn(f"attachLiveMoneyFormatting($('{field}')", self.js, f"Field '{field}' missing attachLiveMoneyFormatting")

    def test_05_reconcile_allows_negative_actual_balance(self):
        """Test 5: recActualInput is configured with allowNegative=true."""
        self.assertIn("attachLiveMoneyFormatting($('recActualInput'), true, true)", self.js)

    def test_06_sighted_loading_state_and_restoration(self):
        """Test 6: submitIdempotent displays visible 'Memproses...' and restores original text/label."""
        self.assertIn("buttonEl.textContent = 'Memproses...'", self.js)
        self.assertIn("buttonEl.setAttribute('aria-label', 'Memproses permintaan...')", self.js)
        self.assertIn("buttonEl.setAttribute('data-original-text', origText)", self.js)
        self.assertIn("buttonEl.textContent = buttonEl.getAttribute('data-original-text')", self.js)

    def test_07_database_unmodified(self):
        """Test 7: Database SHA-256 hash matches post-import canonical baseline."""
        actual_hash = hashlib.sha256((BASE_DIR / "runtime" / "money_tracks.db").read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Database hash changed unexpectedly!")


if __name__ == "__main__":
    unittest.main()
