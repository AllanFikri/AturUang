import unittest
import os
import re
import json
import sqlite3
import shutil
import hashlib
import tempfile
from pathlib import Path
import services


class TestPrompt11UI(unittest.TestCase):
    def setUp(self):
        self.repo_dir = Path(__file__).resolve().parent
        self.prod_hash_before = hashlib.sha256((self.repo_dir / "money_tracks.db").read_bytes()).hexdigest() if (self.repo_dir / "money_tracks.db").exists() else None
        self.index_html_path = self.repo_dir / "index.html"
        self.app_js_path = self.repo_dir / "app.js"
        self.core_js_path = self.repo_dir / "core.js"
        self.styles_css_path = self.repo_dir / "styles.css"
        
        with open(self.index_html_path, "r", encoding="utf-8") as f:
            self.index_html = f.read()
        with open(self.app_js_path, "r", encoding="utf-8") as f:
            self.app_js = f.read()
        with open(self.core_js_path, "r", encoding="utf-8") as f:
            self.core_js = f.read()
        with open(self.styles_css_path, "r", encoding="utf-8") as f:
            self.styles_css = f.read()

        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "money_tracks.db"
        shutil.copyfile(self.repo_dir / "money_tracks.db", self.test_db)
        self.con = sqlite3.connect(self.test_db)
        self.con.row_factory = sqlite3.Row
        services.migrate_allocation_schema(self.con)
        self.con.execute("UPDATE accounts SET protected_amount=0, protected=0")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_dashboard_hero_headline_and_cards(self):
        """1. Verify Dashboard Hero headline and breakdown card labels."""
        self.assertIn("DANA TERSEDIA SAAT INI", self.index_html)
        self.assertIn("kHeroTitle", self.index_html)
        self.assertIn("kSafe", self.index_html)
        
        # Primary breakdown cards in index.html
        self.assertIn("Total Aset Likuid", self.index_html)
        self.assertIn("Dana Darurat", self.index_html)
        self.assertIn("Komitmen Pasti", self.index_html)
        self.assertIn("Dana Tujuan Teralokasi", self.index_html)
        
        # Conditional cards in index.html
        self.assertIn("kFormulaCardCustody", self.index_html)
        self.assertIn("kFormulaCardReceivable", self.index_html)
        self.assertIn("kFormulaCardPending", self.index_html)
        self.assertIn("kFormulaMismatchNotice", self.index_html)

    def test_02_no_active_dana_dijaga_in_ui(self):
        """2. Verify absence of legacy 'Dana Dijaga' on active UI copy."""
        for name, content in [("index.html", self.index_html), ("app.js", self.app_js)]:
            for line_no, line in enumerate(content.splitlines(), 1):
                if "dana dijaga" in line.lower() and not line.strip().startswith("//") and not line.strip().startswith("/*"):
                    self.fail(f"Found active 'Dana Dijaga' in {name}:{line_no} -> {line.strip()}")

    def test_03_core_js_components_match_hero_formula(self):
        """3. Verify componentsMatchHero supports new breakdown components."""
        self.assertIn("emergencyAllocated", self.core_js)
        self.assertIn("goalsAllocated", self.core_js)
        self.assertIn("effectiveConfirmedCommitments", self.core_js)
        self.assertIn("tentativeReserved", self.core_js)

    def test_04_status_id_dictionary(self):
        """4. Verify Indonesian status dictionary contains all lifecycle statuses."""
        self.assertIn("Tentative: 'Tentatif'", self.app_js)
        self.assertIn("Cancelled: 'Dibatalkan'", self.app_js)
        self.assertIn("Active: 'Aktif'", self.app_js)
        self.assertIn("Achieved: 'Tercapai'", self.app_js)
        self.assertIn("Released: 'Dilepaskan'", self.app_js)

    def test_05_tentative_commitment_explanations_and_actions(self):
        """5. Verify tentative commitment explanations and action buttons."""
        self.assertIn("Belum mengurangi Dana Tersedia", self.app_js)
        self.assertIn("Sudah dicadangkan dari Dana Tersedia", self.app_js)
        self.assertIn("Cadangkan Sekarang", self.app_js)
        self.assertIn("Lepaskan Cadangan", self.app_js)
        self.assertIn("Tandai Pasti", self.app_js)
        self.assertIn("Batalkan", self.app_js)

    def test_06_goal_and_emergency_modals_accessibility(self):
        """6. Verify all goal and upcoming modals have proper ARIA attributes."""
        modal_ids = ["goalModal", "fundGoalModal", "releaseGoalModal", "spendGoalModal", "upcomingModal"]
        for mid in modal_ids:
            self.assertIn(f'id="{mid}"', self.index_html)
            self.assertIn(f'role="dialog"', self.index_html)
            self.assertIn(f'aria-modal="true"', self.index_html)
            self.assertIn(f'aria-labelledby="{mid}Title"', self.index_html)

    def test_07_touch_targets_and_responsive_css(self):
        """7. Verify min 44px touch targets and goal styles."""
        self.assertIn(".goal-card", self.styles_css)
        self.assertIn(".goal-progress-bar", self.styles_css)
        self.assertIn(".tab-filter", self.styles_css)
        self.assertIn("min-height: 44px", self.styles_css)

    def test_08_backend_goal_endpoints_and_flow(self):
        """8. Test full API flow for goals, funding, reservation, and release."""
        # 1. Create Goal
        g = services.create_allocation_goal(
            self.con,
            name="Liburan Bali",
            target_amount=3000000.0,
            kind="Goal",
            initial_funding=1000000.0,
            priority=2,
        )
        self.assertEqual(g["allocated_amount"], 1000000.0)
        self.assertEqual(g["target_amount"], 3000000.0)

        # 2. Fund Goal
        g_funded = services.fund_allocation_goal(self.con, g["id"], 500000.0)
        self.assertEqual(g_funded["allocated_amount"], 1500000.0)

        # 3. Create Tentative obligation with reserve_now=0
        cur = self.con.execute(
            """INSERT INTO upcoming (due_date, due_time, title, amount, category, account, for_with_whom, notes, status, reserve_now)
               VALUES ('2026-08-30', '10:00', 'Beli Koper', 400000.0, 'Travel', 'BCA Main', 'Personal / Self', 'Tentatif', 'Tentative', 0)"""
        )
        u1_id = cur.lastrowid
        self.con.commit()

        # 4. Check dashboard KPIs
        d1 = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(d1["kpis"]["goalsAllocated"], 1500000.0, places=2)
        self.assertAlmostEqual(d1["kpis"]["tentativeReserved"], 0.0, places=2)

        # 5. Reserve tentative obligation
        services.set_upcoming_status_and_reservation(self.con, u1_id, reserve_now=1)
        d2 = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(d2["kpis"]["tentativeReserved"], 400000.0, places=2)

        # 6. Release goal funding
        services.release_allocation_goal(self.con, g["id"], 500000.0)
        d3 = services.dashboard(self.con, "2026-08")
        self.assertAlmostEqual(d3["kpis"]["goalsAllocated"], 1000000.0, places=2)

    def test_09_production_db_hash_unmutated(self):
        """9. Verify production money_tracks.db hash is unmutated."""
        prod_db = self.repo_dir / "money_tracks.db"
        if prod_db.exists():
            with open(prod_db, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(h, self.prod_hash_before, f"Production DB hash modified! Expected {self.prod_hash_before}, got {h}")


if __name__ == "__main__":
    unittest.main()
