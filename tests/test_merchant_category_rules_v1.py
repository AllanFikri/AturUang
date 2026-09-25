"""
Tests for STEP-14A: MERCHANT-CATEGORY-RULES-V1
Two-level categories with database-driven rules in SQLite.

Required tests:
  1. Tables created idempotently;
  2. Categories seeded (27 rows);
  3. Default rules seeded once;
  4. classify_merchant returns correct (parent, child);
  5. Priority respected;
  6. Amount override works (INDOMARET < 25000 -> Snacks, >= -> Grocery);
  7. Unmatched -> ("Lain-lain", "Other / Miscellaneous");
  8. Case-insensitive;
  9. API GET categories;
  10. API GET rules;
  11. API POST creates rule;
  12. API PUT updates rule;
  13. API DELETE soft-deletes;
  14. API classify endpoint;
  15. API apply-to-existing dry-run;
  16. API apply-to-existing real run;
  17. Re-apply idempotent;
  18. Production DB unchanged.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aturuang.merchant_classifier import (
    init_merchant_rules_schema,
    seed_categories,
    seed_default_rules,
    classify_merchant,
    reclassify_all_transactions,
    get_all_merchant_categories,
    get_all_merchant_rules,
    create_merchant_rule,
    update_merchant_rule,
    delete_merchant_rule,
    extract_qris_merchant,
)
from aturuang.server import Handler, get_local_auth_token

EXPECTED_PRODUCTION_DB_SHA256 = (
    "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
)


def _find_production_db() -> Path:
    candidates = [
        Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db"),
        Path(__file__).resolve().parent.parent.parent / "AturUang" / "runtime" / "money_tracks.db",
        Path(__file__).resolve().parent.parent / "runtime" / "money_tracks.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Production database not found")


def _get_prod_db_hash() -> str:
    p = _find_production_db()
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestMerchantCategoryRulesV1(unittest.TestCase):
    def setUp(self) -> None:
        self.prod_hash_before = _get_prod_db_hash()
        self.assertEqual(
            self.prod_hash_before,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash invariant violated at test setUp!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_merchant.db"
        self._open_connections: list[sqlite3.Connection] = []

        # Initialize test database
        con = sqlite3.connect(str(self.db_path))
        # Create minimal transactions table
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL DEFAULT '2026-09-01',
                time TEXT NOT NULL DEFAULT '12:00',
                transaction_type TEXT NOT NULL DEFAULT 'Expense',
                amount REAL NOT NULL,
                account_from TEXT NOT NULL DEFAULT 'BCA Main',
                account_to TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Other / Miscellaneous',
                is_deleted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.commit()
        init_merchant_rules_schema(con)
        con.close()

    def tearDown(self) -> None:
        for c in self._open_connections:
            try:
                c.close()
            except Exception:
                pass
        self._open_connections.clear()
        import gc
        gc.collect()
        self.temp_dir.cleanup()
        prod_hash_after = _get_prod_db_hash()
        self.assertEqual(
            prod_hash_after,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production DB hash modified during test execution!",
        )

    def _get_con(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        self._open_connections.append(con)
        return con

    def _make_handler(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Handler:
        handler = Handler.__new__(Handler)
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body or b"")
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 54321)
        handler.send_error = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.send_json = MagicMock()
        return handler

    # -------------------------------------------------------------------------
    # 1. Tables created idempotently
    # -------------------------------------------------------------------------
    def test_01_tables_created_idempotently(self) -> None:
        """1. Tables merchant_categories and merchant_category_rules created idempotently."""
        con = self._get_con()
        try:
            # Call multiple times
            init_merchant_rules_schema(con)
            init_merchant_rules_schema(con)

            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            self.assertIn("merchant_categories", tables)
            self.assertIn("merchant_category_rules", tables)

            cur_idx = con.execute("SELECT name FROM sqlite_master WHERE type='index'")
            indices = {r[0] for r in cur_idx.fetchall()}
            self.assertIn("idx_rules_active_priority", indices)
            self.assertIn("idx_rules_pattern", indices)
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 2. Categories seeded (27 rows)
    # -------------------------------------------------------------------------
    def test_02_categories_seeded_27_rows(self) -> None:
        """2. Categories seeded with exactly 27 rows (8 parents, 24 unique children)."""
        con = self._get_con()
        try:
            cur = con.execute("SELECT COUNT(*) FROM merchant_categories")
            count = cur.fetchone()[0]
            self.assertEqual(count, 27)

            cur_parents = con.execute("SELECT DISTINCT parent_name FROM merchant_categories")
            parents = {r[0] for r in cur_parents.fetchall()}
            self.assertEqual(len(parents), 8)

            cur_children = con.execute("SELECT DISTINCT child_name FROM merchant_categories")
            children = {r[0] for r in cur_children.fetchall()}
            self.assertEqual(len(children), 27)
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 3. Default rules seeded once
    # -------------------------------------------------------------------------
    def test_03_default_rules_seeded_once(self) -> None:
        """3. Default rules seeded once without duplicates upon multiple init calls."""
        con = self._get_con()
        try:
            cur = con.execute("SELECT COUNT(*) FROM merchant_category_rules")
            initial_count = cur.fetchone()[0]
            self.assertEqual(initial_count, 130)

            # Re-run init_merchant_rules_schema
            init_merchant_rules_schema(con)

            cur_after = con.execute("SELECT COUNT(*) FROM merchant_category_rules")
            after_count = cur_after.fetchone()[0]
            self.assertEqual(after_count, 130)
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 4. classify_merchant returns correct (parent, child)
    # -------------------------------------------------------------------------
    def test_04_classify_merchant_returns_correct_pair(self) -> None:
        """4. classify_merchant returns correct (parent, child) pair for seeded rules."""
        con = self._get_con()
        try:
            parent, child = classify_merchant("SPBU PERTAMINA KOTA", 50000.0, db_path=con)
            self.assertEqual(parent, "Transportasi")
            self.assertEqual(child, "Bensin")

            parent, child = classify_merchant("MIE GACOAN MALANG", 30000.0, db_path=con)
            self.assertEqual(parent, "Makanan & Minuman")
            self.assertEqual(child, "Makanan Berat")

            parent, child = classify_merchant("APOTEK KIMIA FARMA", 45000.0, db_path=con)
            self.assertEqual(parent, "Kesehatan")
            self.assertEqual(child, "Apotek")

            parent, child = classify_merchant("TOKOPEDIA OFFICIAL STORE", 125000.0, db_path=con)
            self.assertEqual(parent, "Belanja")
            self.assertEqual(child, "Online")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 5. Priority respected
    # -------------------------------------------------------------------------
    def test_05_priority_respected(self) -> None:
        """5. Priority respected: higher-priority specific rule takes precedence over generic rule."""
        con = self._get_con()
        try:
            # "ESB RESTAURANT" matches priority 5 ("ESB RESTAURANT" -> Makanan Berat)
            # and priority 70 ("RESTAURANT" -> Makanan Berat).
            # "KJPRI UB" is priority 5 (Belanja/Grocery)
            p, c = classify_merchant("ESB RESTAURANT", 50000.0, db_path=con)
            self.assertEqual((p, c), ("Makanan & Minuman", "Makanan Berat"))

            p, c = classify_merchant("KJPRI UB", 50000.0, db_path=con)
            self.assertEqual((p, c), ("Belanja", "Grocery"))
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 6. Amount override works (INDOMARET < 25000 -> Snacks, >= -> Grocery)
    # -------------------------------------------------------------------------
    def test_06_amount_override_works(self) -> None:
        """6. Amount override works: INDOMARET < 25000 -> Snacks & Jajan, >= 25000 -> Grocery."""
        con = self._get_con()
        try:
            # Below 25000: Snacks & Jajan (Makanan & Minuman)
            p_below, c_below = classify_merchant("INDOMARET POINT", 12000.0, db_path=con)
            self.assertEqual(p_below, "Makanan & Minuman")
            self.assertEqual(c_below, "Snacks & Jajan")

            # At threshold 25000: Grocery (Belanja)
            p_exact, c_exact = classify_merchant("INDOMARET POINT", 25000.0, db_path=con)
            self.assertEqual(p_exact, "Belanja")
            self.assertEqual(c_exact, "Grocery")

            # Above 25000: Grocery (Belanja)
            p_above, c_above = classify_merchant("INDOMARET POINT", 85000.0, db_path=con)
            self.assertEqual(p_above, "Belanja")
            self.assertEqual(c_above, "Grocery")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 7. Unmatched -> ("Lain-lain", "Other / Miscellaneous")
    # -------------------------------------------------------------------------
    def test_07_unmatched_returns_fallback(self) -> None:
        """7. Unmatched merchant returns ('Lain-lain', 'Other / Miscellaneous')."""
        con = self._get_con()
        try:
            p, c = classify_merchant("RANDOM_UNKNOWN_VENDOR_9999", 100000.0, db_path=con)
            self.assertEqual(p, "Lain-lain")
            self.assertEqual(c, "Other / Miscellaneous")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 8. Case-insensitive
    # -------------------------------------------------------------------------
    def test_08_case_insensitive(self) -> None:
        """8. Matching is case-insensitive."""
        con = self._get_con()
        try:
            p1, c1 = classify_merchant("indomaret", 10000.0, db_path=con)
            p2, c2 = classify_merchant("INDOMARET", 10000.0, db_path=con)
            p3, c3 = classify_merchant("IndoMaret", 10000.0, db_path=con)
            self.assertEqual((p1, c1), ("Makanan & Minuman", "Snacks & Jajan"))
            self.assertEqual((p2, c2), ("Makanan & Minuman", "Snacks & Jajan"))
            self.assertEqual((p3, c3), ("Makanan & Minuman", "Snacks & Jajan"))
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 9. API GET categories
    # -------------------------------------------------------------------------
    def test_09_api_get_categories(self) -> None:
        """9. GET /api/merchant-categories returns all 27 categories."""
        token = get_local_auth_token()
        h = self._make_handler(
            "GET",
            "/api/merchant-categories",
            {"Host": "127.0.0.1:5050", "X-CSRF-Token": token},
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_GET()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            self.assertEqual(data.get("status"), "success")
            self.assertEqual(len(data.get("categories", [])), 27)

    # -------------------------------------------------------------------------
    # 10. API GET rules
    # -------------------------------------------------------------------------
    def test_10_api_get_rules(self) -> None:
        """10. GET /api/merchant-rules returns all rules."""
        token = get_local_auth_token()
        h = self._make_handler(
            "GET",
            "/api/merchant-rules",
            {"Host": "127.0.0.1:5050", "X-CSRF-Token": token},
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_GET()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            self.assertEqual(data.get("status"), "success")
            self.assertEqual(len(data.get("rules", [])), 130)

    # -------------------------------------------------------------------------
    # 11. API POST creates rule
    # -------------------------------------------------------------------------
    def test_11_api_post_creates_rule(self) -> None:
        """11. POST /api/merchant-rules creates a new rule."""
        token = get_local_auth_token()
        payload = {
            "merchant_pattern": "MY_SPECIAL_CAFE",
            "match_type": "substring",
            "parent_category": "Makanan & Minuman",
            "child_category": "Cafe & Minuman",
            "priority": 15,
            "notes": "Custom test rule",
        }
        body = json.dumps(payload).encode("utf-8")
        h = self._make_handler(
            "POST",
            "/api/merchant-rules",
            {
                "Host": "127.0.0.1:5050",
                "X-CSRF-Token": token,
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_POST()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            status = call_args[1].get("status", 200)
            self.assertIn(status, (200, 201))
            self.assertEqual(data.get("status"), "success")
            self.assertIn("rule_id", data)

        # Verify created in DB
        con = self._get_con()
        try:
            row = con.execute(
                "SELECT parent_category, child_category FROM merchant_category_rules WHERE merchant_pattern = 'MY_SPECIAL_CAFE'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Makanan & Minuman")
            self.assertEqual(row[1], "Cafe & Minuman")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 12. API PUT updates rule
    # -------------------------------------------------------------------------
    def test_12_api_put_updates_rule(self) -> None:
        """12. PUT /api/merchant-rules/<id> updates rule fields."""
        token = get_local_auth_token()
        # Create a rule first in DB
        con = self._get_con()
        cur = con.execute(
            """
            INSERT INTO merchant_category_rules
            (merchant_pattern, match_type, parent_category, child_category, priority, is_active)
            VALUES ('RULE_TO_UPDATE', 'substring', 'Transportasi', 'Bensin', 50, 1)
            """
        )
        rule_id = cur.lastrowid
        con.commit()
        con.close()

        update_payload = {
            "merchant_pattern": "RULE_UPDATED_NAME",
            "match_type": "exact",
            "parent_category": "Transportasi",
            "child_category": "Otomotif",
            "priority": 25,
        }
        body = json.dumps(update_payload).encode("utf-8")
        h = self._make_handler(
            "PUT",
            f"/api/merchant-rules/{rule_id}",
            {
                "Host": "127.0.0.1:5050",
                "X-CSRF-Token": token,
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_PUT()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            status = call_args[1].get("status", 200)
            self.assertEqual(status, 200)
            self.assertEqual(data.get("status"), "success")

        # Verify DB updated
        con = self._get_con()
        try:
            row = con.execute("SELECT merchant_pattern, match_type, child_category, priority FROM merchant_category_rules WHERE id = ?", (rule_id,)).fetchone()
            self.assertEqual(row[0], "RULE_UPDATED_NAME")
            self.assertEqual(row[1], "exact")
            self.assertEqual(row[2], "Otomotif")
            self.assertEqual(row[3], 25)
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 13. API DELETE soft-deletes
    # -------------------------------------------------------------------------
    def test_13_api_delete_soft_deletes(self) -> None:
        """13. DELETE /api/merchant-rules/<id> soft-deletes rule by setting is_active = 0."""
        token = get_local_auth_token()
        con = self._get_con()
        cur = con.execute(
            """
            INSERT INTO merchant_category_rules
            (merchant_pattern, match_type, parent_category, child_category, priority, is_active)
            VALUES ('RULE_TO_DELETE', 'substring', 'Belanja', 'Grocery', 10, 1)
            """
        )
        rule_id = cur.lastrowid
        con.commit()
        con.close()

        h = self._make_handler(
            "DELETE",
            f"/api/merchant-rules/{rule_id}",
            {"Host": "127.0.0.1:5050", "X-CSRF-Token": token},
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_DELETE()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            self.assertEqual(data.get("status"), "success")

        # Verify is_active == 0 in DB
        con = self._get_con()
        try:
            row = con.execute("SELECT is_active FROM merchant_category_rules WHERE id = ?", (rule_id,)).fetchone()
            self.assertEqual(row[0], 0)

            # Classify should now ignore this rule
            p, c = classify_merchant("RULE_TO_DELETE", 10000.0, db_path=con)
            self.assertNotEqual((p, c), ("Belanja", "Grocery"))
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 14. API classify endpoint
    # -------------------------------------------------------------------------
    def test_14_api_classify_endpoint(self) -> None:
        """14. POST /api/merchant-rules/classify returns {parent_category, child_category}."""
        token = get_local_auth_token()
        payload = {"merchant_name": "SPBU SHELL", "amount": 100000.0}
        body = json.dumps(payload).encode("utf-8")
        h = self._make_handler(
            "POST",
            "/api/merchant-rules/classify",
            {
                "Host": "127.0.0.1:5050",
                "X-CSRF-Token": token,
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_POST()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            self.assertEqual(data.get("status"), "success")
            self.assertEqual(data.get("parent_category"), "Transportasi")
            self.assertEqual(data.get("child_category"), "Bensin")

    # -------------------------------------------------------------------------
    # 15. API apply-to-existing dry-run
    # -------------------------------------------------------------------------
    def test_15_api_apply_to_existing_dry_run(self) -> None:
        """15. POST /api/merchant-rules/apply-to-existing with dry_run=True does not mutate DB."""
        token = get_local_auth_token()
        con = self._get_con()
        con.execute(
            """
            INSERT INTO transactions (description, amount, category)
            VALUES ('Pembayaran QRIS di SPBU PERTAMINA Rp 50.000', 50000.0, 'Other / Miscellaneous'),
                   ('Beli makan siang manual di warteg', 25000.0, 'Main Meals')
            """
        )
        con.commit()
        con.close()

        payload = {"dry_run": True}
        body = json.dumps(payload).encode("utf-8")
        h = self._make_handler(
            "POST",
            "/api/merchant-rules/apply-to-existing",
            {
                "Host": "127.0.0.1:5050",
                "X-CSRF-Token": token,
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_POST()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            self.assertEqual(data.get("status"), "success")
            self.assertEqual(data.get("updated"), 1)
            self.assertEqual(data.get("skipped"), 1)

        # Check DB unchanged: parent_category still None
        con = self._get_con()
        try:
            row = con.execute("SELECT parent_category, category FROM transactions WHERE description LIKE '%SPBU%'").fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], "Other / Miscellaneous")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 16. API apply-to-existing real run
    # -------------------------------------------------------------------------
    def test_16_api_apply_to_existing_real_run(self) -> None:
        """16. POST /api/merchant-rules/apply-to-existing with dry_run=False updates QRIS transactions."""
        token = get_local_auth_token()
        con = self._get_con()
        con.execute(
            """
            INSERT INTO transactions (description, amount, category)
            VALUES ('Pembayaran QRIS di SPBU PERTAMINA Rp 50.000', 50000.0, 'Other / Miscellaneous'),
                   ('Beli makan siang manual di warteg', 25000.0, 'Main Meals')
            """
        )
        con.commit()
        con.close()

        payload = {"dry_run": False}
        body = json.dumps(payload).encode("utf-8")
        h = self._make_handler(
            "POST",
            "/api/merchant-rules/apply-to-existing",
            {
                "Host": "127.0.0.1:5050",
                "X-CSRF-Token": token,
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        with patch("aturuang.server.db_connect", side_effect=self._get_con):
            h.do_POST()
            h.send_json.assert_called()
            call_args = h.send_json.call_args
            data = call_args[0][0]
            self.assertEqual(data.get("status"), "success")
            self.assertEqual(data.get("updated"), 1)
            self.assertEqual(data.get("skipped"), 1)

        # Check DB updated
        con = self._get_con()
        try:
            row_qris = con.execute("SELECT parent_category, child_category, category FROM transactions WHERE description LIKE '%SPBU%'").fetchone()
            self.assertEqual(row_qris[0], "Transportasi")
            self.assertEqual(row_qris[1], "Bensin")
            self.assertEqual(row_qris[2], "Bensin")

            # Manual transaction unchanged
            row_manual = con.execute("SELECT parent_category, child_category, category FROM transactions WHERE description LIKE '%manual%'").fetchone()
            self.assertIsNone(row_manual[0])
            self.assertIsNone(row_manual[1])
            self.assertEqual(row_manual[2], "Main Meals")
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 17. Re-apply idempotent
    # -------------------------------------------------------------------------
    def test_17_reapply_idempotent(self) -> None:
        """17. Re-running reclassify_all_transactions is idempotent and leaves data consistent."""
        con = self._get_con()
        try:
            con.execute(
                """
                INSERT INTO transactions (description, amount, category)
                VALUES ('Pembayaran QRIS di INDOMARET Rp 15.000', 15000.0, 'Other / Miscellaneous'),
                       ('Pembayaran QRIS di ALFAMART Rp 60.000', 60000.0, 'Other / Miscellaneous')
                """
            )
            con.commit()

            # First run
            res1 = reclassify_all_transactions(db_path=con, dry_run=False)
            con.commit()
            self.assertEqual(res1["updated"], 2)
            self.assertEqual(res1["skipped"], 0)

            rows1 = con.execute("SELECT id, parent_category, child_category, category FROM transactions ORDER BY id").fetchall()

            # Second run
            res2 = reclassify_all_transactions(db_path=con, dry_run=False)
            con.commit()
            self.assertEqual(res2["updated"], 2)
            self.assertEqual(res2["skipped"], 0)

            rows2 = con.execute("SELECT id, parent_category, child_category, category FROM transactions ORDER BY id").fetchall()

            # State is completely identical
            self.assertEqual([dict(r) for r in rows1], [dict(r) for r in rows2])
        finally:
            con.close()

    # -------------------------------------------------------------------------
    # 18. Production DB unchanged
    # -------------------------------------------------------------------------
    def test_18_production_db_unchanged(self) -> None:
        """18. Production SQLite database remains completely untouched and hash is strictly invariant."""
        hash_now = _get_prod_db_hash()
        self.assertEqual(
            hash_now,
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database hash changed during execution!",
        )


if __name__ == "__main__":
    unittest.main()
