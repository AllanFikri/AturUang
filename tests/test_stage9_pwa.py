"""
AturUang Stage 9 — Installable PWA & Mobile Shell Tests.

Comprehensive testing of:
1. PWA Manifest delivery and required fields.
2. Service Worker delivery and Service-Worker-Allowed header.
3. Strict NO-CACHE policy for financial API endpoints (/api/*).
4. Mobile viewport meta tag with user-scalable=no.
5. Theme-color meta tag matching manifest.
6. Apple touch icon and mobile web app capable meta tags.
7. Honest online/offline indicator in the UI.
8. Responsive mobile tabs CSS layout.
9. Quick capture input touch-friendly sizing and styles.
10. Standalone display mode configuration in manifest.
11. Versioned cache naming in service worker script.
12. PWA beforeinstallprompt event handling and installation hook.
13. Zero hardcoded secrets, admin tokens, or credentials in scripts.
14. Full HTTP routing of PWA assets (/sw.js, /manifest.webmanifest, /icon.svg).
15. Assertion that real production database remains completely untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from aturuang.pwa_assets import (
    CACHE_NAME,
    CACHE_VERSION,
    ICON_SVG,
    PWA_MANIFEST,
    get_icon_svg,
    get_manifest_json,
    get_service_worker_js,
)
from aturuang.web_composer import QuickCaptureComposer

EXPECTED_PRODUCTION_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"


def _find_production_db() -> Path:
    candidates = [
        Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db"),
        Path(__file__).resolve().parent.parent.parent / "AturUang" / "runtime" / "money_tracks.db",
        Path(__file__).resolve().parent.parent / "runtime" / "money_tracks.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Production database not found")


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestStage9PWA(unittest.TestCase):
    """Test suite for Stage 9 Installable PWA & Mobile Shell."""

    def setUp(self) -> None:
        self.prod_db = _find_production_db()
        self.assertEqual(
            _compute_sha256(self.prod_db),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered prior to test execution!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_pwa.db"
        self.composer = QuickCaptureComposer(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        self.assertEqual(
            _compute_sha256(self.prod_db),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered by test execution!",
        )

    # 1. test_pwa_manifest_endpoint
    def test_pwa_manifest_endpoint(self) -> None:
        """Serves valid JSON manifest with required PWA properties."""
        code, headers, body = self.composer.handle_request("GET", "/manifest.webmanifest")
        self.assertEqual(code, 200)
        self.assertIn("application/manifest+json", headers.get("Content-Type", ""))

        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data.get("name"), "AturUang")
        self.assertEqual(data.get("short_name"), "AturUang")
        self.assertEqual(data.get("start_url"), "/")
        self.assertEqual(data.get("display"), "standalone")
        self.assertEqual(data.get("theme_color"), "#1e293b")
        self.assertEqual(data.get("background_color"), "#0f172a")
        self.assertIsInstance(data.get("icons"), list)
        self.assertGreaterEqual(len(data["icons"]), 1)

    # 2. test_pwa_service_worker_endpoint
    def test_pwa_service_worker_endpoint(self) -> None:
        """Serves service worker script with appropriate Content-Type and Service-Worker-Allowed headers."""
        code, headers, body = self.composer.handle_request("GET", "/sw.js")
        self.assertEqual(code, 200)
        self.assertIn("application/javascript", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Service-Worker-Allowed"), "/")
        self.assertGreater(len(body), 50)

    # 3. test_pwa_sw_bypasses_api_cache
    def test_pwa_sw_bypasses_api_cache(self) -> None:
        """Asserts service worker script contains explicit network-only rule for financial API endpoints."""
        sw_js = get_service_worker_js()
        # Must check /api/
        self.assertIn("url.pathname.startsWith('/api/')", sw_js)
        # Must explicitly bypass cache via fetch(event.request)
        self.assertIn("event.respondWith(fetch(event.request))", sw_js)
        # Verify strict privacy comment/rule
        self.assertTrue(
            "STRICT PRIVACY" in sw_js or "NO-CACHE" in sw_js,
            "Service worker should contain documentation of strict financial privacy",
        )

    # 4. test_pwa_meta_viewport_present
    def test_pwa_meta_viewport_present(self) -> None:
        """HTML shell includes mobile-friendly viewport meta tag configured for touch ergonomics."""
        html = self.composer.render_html()
        expected_viewport = (
            '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">'
        )
        self.assertIn(expected_viewport, html)

    # 5. test_pwa_meta_theme_color
    def test_pwa_meta_theme_color(self) -> None:
        """HTML shell includes theme-color meta tag matching manifest theme color."""
        html = self.composer.render_html()
        expected_theme = f'<meta name="theme-color" content="{PWA_MANIFEST["theme_color"]}">'
        self.assertIn(expected_theme, html)

    # 6. test_pwa_apple_touch_icon
    def test_pwa_apple_touch_icon(self) -> None:
        """HTML shell includes mobile web app capable and icon links for iOS / PWA shells."""
        html = self.composer.render_html()
        self.assertIn('<meta name="mobile-web-app-capable" content="yes">', html)
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', html)
        self.assertIn('<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">', html)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)
        self.assertIn('<link rel="apple-touch-icon" href="/icon.svg">', html)

    # 7. test_pwa_offline_indicator_present
    def test_pwa_offline_indicator_present(self) -> None:
        """HTML contains honest offline status indicator element and listeners."""
        html = self.composer.render_html()
        self.assertIn('id="offlineIndicator"', html)
        self.assertIn("offline", html.lower())
        self.assertIn("window.addEventListener('offline'", html)
        self.assertIn("window.addEventListener('online'", html)

    # 8. test_pwa_responsive_tabs_css
    def test_pwa_responsive_tabs_css(self) -> None:
        """HTML includes mobile layout CSS media query and responsive tab navigation classes."""
        html = self.composer.render_html()
        self.assertIn("@media (max-width: 640px)", html)
        self.assertIn(".nav-tabs", html)
        self.assertIn("overflow-x: auto", html)
        self.assertIn("-webkit-overflow-scrolling: touch", html)

    # 9. test_pwa_quick_capture_mobile_layout
    def test_pwa_quick_capture_mobile_layout(self) -> None:
        """Quick Capture input renders within touch-friendly mobile layout container."""
        html = self.composer.render_html()
        self.assertIn('class="quick-capture-box"', html)
        self.assertIn('id="captureInput"', html)
        self.assertIn("min-height: 44px", html)
        self.assertIn("box-sizing: border-box", html)

    # 10. test_pwa_standalone_display_mode
    def test_pwa_standalone_display_mode(self) -> None:
        """Manifest declares standalone display mode for native app feel."""
        self.assertEqual(PWA_MANIFEST["display"], "standalone")
        self.assertIn("standalone", get_manifest_json())

    # 11. test_pwa_sw_cache_name_versioned
    def test_pwa_sw_cache_name_versioned(self) -> None:
        """Service worker uses versioned cache key for reliable cache busting."""
        sw_js = get_service_worker_js()
        self.assertTrue(re.search(r"const CACHE_NAME = 'aturuang-shell-v\d+';", sw_js))
        self.assertEqual(CACHE_NAME, f"aturuang-shell-{CACHE_VERSION}")

    # 12. test_pwa_install_prompt_hook
    def test_pwa_install_prompt_hook(self) -> None:
        """UI script includes beforeinstallprompt event handling and install button hook."""
        html = self.composer.render_html()
        self.assertIn("beforeinstallprompt", html)
        self.assertIn('id="pwaInstallBtn"', html)
        self.assertIn("installPwa", html)

    # 13. test_pwa_no_admin_tokens_in_storage
    def test_pwa_no_admin_tokens_in_storage(self) -> None:
        """Asserts client-side templates and scripts contain zero hardcoded secrets or admin tokens."""
        html = self.composer.render_html()
        sw_js = get_service_worker_js()
        manifest_json = get_manifest_json()

        forbidden_patterns = [
            r"AIzaSy[A-Za-z0-9_-]{33}",  # Google API key
            r"sk-[A-Za-z0-9]{32,}",     # Standard bearer secret
            r"Bearer\s+[A-Za-z0-9_-]{16,}",
            r"admin_secret",
            r"admin_token",
            r"private_key",
        ]

        combined = html + sw_js + manifest_json
        for pat in forbidden_patterns:
            matches = re.findall(pat, combined, re.IGNORECASE)
            self.assertEqual(matches, [], f"Detected potential credential or token leak: {matches}")

    # 14. test_web_composer_routes_pwa_correctly
    def test_web_composer_routes_pwa_correctly(self) -> None:
        """HTTP dispatcher cleanly routes all PWA assets (/sw.js, /manifest.webmanifest, /icon.svg)."""
        # Test /sw.js
        c1, h1, b1 = self.composer.handle_request("GET", "/sw.js")
        self.assertEqual(c1, 200)
        self.assertIn("javascript", h1["Content-Type"])

        # Test /manifest.webmanifest
        c2, h2, b2 = self.composer.handle_request("GET", "/manifest.webmanifest")
        self.assertEqual(c2, 200)
        self.assertIn("json", h2["Content-Type"])

        # Test /manifest.json alias
        c3, h3, b3 = self.composer.handle_request("GET", "/manifest.json")
        self.assertEqual(c3, 200)
        self.assertIn("json", h3["Content-Type"])

        # Test /icon.svg
        c4, h4, b4 = self.composer.handle_request("GET", "/icon.svg")
        self.assertEqual(c4, 200)
        self.assertIn("image/svg+xml", h4["Content-Type"])
        self.assertIn(b"<svg", b4)

    # 15. test_production_db_untouched_during_stage9_tests
    def test_production_db_untouched_during_stage9_tests(self) -> None:
        """Asserts production database SHA-256 remains strictly untouched."""
        current_hash = _compute_sha256(self.prod_db)
        self.assertEqual(
            current_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            f"Production DB altered! Expected {EXPECTED_PRODUCTION_DB_SHA256}, got {current_hash}",
        )


if __name__ == "__main__":
    unittest.main()
