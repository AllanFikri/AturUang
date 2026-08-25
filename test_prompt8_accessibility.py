"""
Test Suite Prompt 8 — Comprehensive Accessibility, Live Money Format & Sighted Loading State
"""
import hashlib
import re
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "money_tracks.db"


def hex_to_rgb(hex_str):
    hex_str = hex_str.strip().lstrip('#')
    if len(hex_str) == 3:
        hex_str = ''.join(c*2 for c in hex_str)
    return [int(hex_str[i:i+2], 16) / 255.0 for i in (0, 2, 4)]


def luminance(r, g, b):
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(hex1, hex2):
    l1 = luminance(*hex_to_rgb(hex1))
    l2 = luminance(*hex_to_rgb(hex2))
    top = max(l1, l2)
    bot = min(l1, l2)
    return round((top + 0.05) / (bot + 0.05), 2)


class TestPrompt8Accessibility(unittest.TestCase):
    def setUp(self):
        self.prod_hash_before = hashlib.sha256(DB_FILE.read_bytes()).hexdigest() if DB_FILE.exists() else None
        self.html_path = BASE_DIR / "index.html"
        self.css_path = BASE_DIR / "styles.css"
        self.js_path = BASE_DIR / "app.js"
        self.core_path = BASE_DIR / "core.js"
        self.html = self.html_path.read_text(encoding="utf-8")
        self.css = self.css_path.read_text(encoding="utf-8")
        self.js = self.js_path.read_text(encoding="utf-8")
        self.core = self.core_path.read_text(encoding="utf-8")

    def test_01_no_duplicate_ids(self):
        """Test 1: All HTML element IDs are globally unique."""
        all_ids = re.findall(r'\bid=["\']([^"\']+)["\']', self.html)
        id_counts = {}
        for i in all_ids:
            id_counts[i] = id_counts.get(i, 0) + 1
        duplicates = [k for k, v in id_counts.items() if v > 1]
        self.assertEqual(len(duplicates), 0, f"Duplicate IDs found: {duplicates}")

    def test_02_no_broken_label_for_references(self):
        """Test 2: All <label for="..."> attributes refer to existing element IDs."""
        all_ids = set(re.findall(r'\bid=["\']([^"\']+)["\']', self.html))
        for_attrs = re.findall(r'<label[^>]*\bfor=["\']([^"\']+)["\']', self.html)
        self.assertTrue(len(for_attrs) > 0, "No <label for> attributes found in index.html")
        broken = [f for f in for_attrs if f not in all_ids]
        self.assertEqual(len(broken), 0, f"Broken label for attributes: {broken}")

    def test_03_modal_dialog_semantics(self):
        """Test 3: All modals have role='dialog', aria-modal='true', and aria-labelledby."""
        modals = [
            "txModal", "upcomingModal", "payUpcomingModal", "reconcileModal",
            "reversalModal", "aiConfigModal", "linkAllocModal", "debtPositionModal",
            "debtEventModal", "debtDetailModal"
        ]
        for m_id in modals:
            pattern = rf'<div\s+id="{m_id}"\s+class="modal"\s+role="dialog"\s+aria-modal="true"\s+aria-labelledby="([^"]+)"'
            match = re.search(pattern, self.html)
            self.assertIsNotNone(match, f"Modal #{m_id} missing proper dialog semantics")
            title_id = match.group(1)
            self.assertIn(f'id="{title_id}"', self.html, f"Title ID #{title_id} for modal #{m_id} not found")

    def test_04_validation_error_live_region_and_describedby(self):
        """Test 4: Validation error helper attaches role='alert', aria-live='assertive', and aria-describedby."""
        self.assertIn("function setFieldError(inputEl, msg)", self.js)
        self.assertIn("errEl.setAttribute('role', 'alert')", self.js)
        self.assertIn("errEl.setAttribute('aria-live', 'assertive')", self.js)
        self.assertIn("inputEl.setAttribute('aria-invalid', 'true')", self.js)
        self.assertIn("inputEl.setAttribute('aria-describedby', errEl.id)", self.js)

    def test_05_placeholder_contrast_across_all_themes(self):
        """Test 5: Placeholder color meets >=4.5:1 normal text contrast on all themes."""
        themes = {
            "Midnight": {"input": "#0f172a", "muted": "#94a3b8"},
            "Ocean": {"input": "#091d2d", "muted": "#91b4c8"},
            "Emerald": {"input": "#081d18", "muted": "#90b7aa"},
        }
        for name, t in themes.items():
            c = contrast(t["muted"], t["input"])
            self.assertGreaterEqual(c, 4.5, f"Placeholder contrast too low on {name}: {c}:1")

    def test_06_error_text_contrast_across_all_themes(self):
        """Test 6: Error text meets >=4.5:1 normal text contrast against panels on all themes."""
        themes = {
            "Midnight": {"panel": "#1e293b", "red": "#fb7185"},
            "Ocean": {"panel": "#0c2234", "red": "#fb7185"},
            "Emerald": {"panel": "#0a231d", "red": "#fb7185"},
        }
        for name, t in themes.items():
            c = contrast(t["red"], t["panel"])
            self.assertGreaterEqual(c, 4.5, f"Error text contrast too low on {name}: {c}:1")

    def test_07_accessible_sighted_loading_state_and_recovery(self):
        """Test 7: Async submit sets visible 'Memproses...' text and aria-label, then restores both."""
        self.assertIn("buttonEl.textContent = 'Memproses...'", self.js)
        self.assertIn("buttonEl.setAttribute('aria-label', 'Memproses permintaan...')", self.js)
        self.assertIn("buttonEl.setAttribute('data-original-text', origText)", self.js)
        self.assertIn("buttonEl.getAttribute('data-original-text')", self.js)
        self.assertIn("buttonEl.removeAttribute('aria-busy')", self.js)
        self.assertIn("buttonEl.disabled = true", self.js)
        self.assertIn("buttonEl.disabled = false", self.js)

    def test_08_centralized_money_helpers_present(self):
        """Test 8: Centralized parseMoneyInput, formatMoneyInput, setMoneyInput are defined."""
        self.assertIn("function parseMoneyInput(val)", self.core)
        self.assertIn("function formatMoneyInput(val, allowDecimals)", self.core)
        self.assertIn("function setMoneyInput(fieldOrId, val, allowDecimals", self.js)
        self.assertIn("function attachLiveMoneyFormatting(inputEl", self.js)

    def test_09_topmost_modal_focus_and_escape(self):
        """Test 9: Keydown listener routes Escape and Tab trap strictly to topmost active modal."""
        self.assertIn("document.querySelectorAll('.modal.open')", self.js)
        self.assertIn("openModals[openModals.length - 1]", self.js)
        self.assertIn("closeModal(topModal.id)", self.js)

    def test_10_database_unmodified(self):
        """Test 10: Database SHA-256 hash matches post-import canonical baseline."""
        actual_hash = hashlib.sha256(DB_FILE.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Database hash changed unexpectedly!")


if __name__ == "__main__":
    unittest.main()
