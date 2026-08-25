"""
Test Suite Prompt 8b2 — Responsive Layout, Tables, Modals, and Touch Target Structure
"""
import re
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class TestPrompt8Responsive(unittest.TestCase):
    def setUp(self):
        self.html_path = BASE_DIR / "index.html"
        self.css_path = BASE_DIR / "styles.css"
        self.html = self.html_path.read_text(encoding="utf-8")
        self.css = self.css_path.read_text(encoding="utf-8")

    def test_01_meta_viewport_present(self):
        """Test 1: Meta viewport is present and configured for responsive scaling."""
        match = re.search(r'<meta\s+name=["\']viewport["\']\s+content=["\']([^"\']+)["\']', self.html, re.IGNORECASE)
        self.assertIsNotNone(match, "Meta viewport tag not found in index.html")
        content = match.group(1)
        self.assertIn("width=device-width", content)
        self.assertIn("initial-scale=1", content)

    def test_02_breakpoints_defined(self):
        """Test 2: Standard responsive breakpoints (768px, 650px/480px) are defined in styles.css."""
        self.assertIn("@media (max-width: 768px)", self.css)
        self.assertIn("@media (max-width: 480px)", self.css)

    def _get_media_block(self, query):
        idx = self.css.find(query)
        if idx == -1:
            return ""
        open_brace = self.css.find("{", idx)
        if open_brace == -1:
            return ""
        depth = 0
        end_idx = open_brace
        for i in range(open_brace, len(self.css)):
            if self.css[i] == "{":
                depth += 1
            elif self.css[i] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        return self.css[idx:end_idx]

    def test_03_form_grid_single_column_on_mobile(self):
        """Test 3: Form grid, KPIs, and card grids collapse to 1fr on mobile viewports."""
        block_480 = self._get_media_block("@media (max-width: 480px)")
        block_650 = self._get_media_block("@media (max-width: 650px)")
        combined = block_480 + "\n" + block_650
        self.assertIn("grid-template-columns: 1fr", combined)

    def test_04_modal_max_height_and_scrollable(self):
        """Test 4: Modal containers use viewport units (dvh/vh), safe-area insets, and overflow scroll."""
        self.assertIn("max-height: 90dvh", self.css)
        self.assertIn("overflow-y: auto", self.css)
        self.assertIn("-webkit-overflow-scrolling: touch", self.css)
        self.assertIn("safe-area-inset", self.css)

    def test_05_table_wrapper_scrollable(self):
        """Test 5: Table wrapper has overflow-x auto and touch momentum scrolling."""
        match = re.search(r'\.table-wrap\s*{([^}]+)}', self.css)
        self.assertIsNotNone(match, ".table-wrap class not found in styles.css")
        wrap_css = match.group(1)
        self.assertTrue("overflow: auto" in wrap_css or "overflow-x: auto" in wrap_css)

    def test_06_no_body_overflow_hidden_hack(self):
        """Test 6: Body does not use overflow-x: hidden hack to disguise layout overflow."""
        match = re.search(r'body\s*{([^}]+)}', self.css)
        if match:
            body_css = match.group(1)
            self.assertNotIn("overflow-x: hidden", body_css, "body should not use overflow-x: hidden hack")

    def test_07_touch_target_min_height(self):
        """Test 7: Mobile media queries specify min-height: 44px for touch targets (nav, buttons, close)."""
        block_768 = self._get_media_block("@media (max-width: 768px)")
        self.assertIn("min-height: 44px", block_768)


if __name__ == "__main__":
    unittest.main()
