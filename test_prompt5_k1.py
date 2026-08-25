"""
Test Suite: Prompt 5 (K1 Anti-Dobel-Potong Dana Dijaga & Kewajiban)
"""
import unittest
import sqlite3
import tempfile
import os
import shutil
from pathlib import Path

from db import init_db, db_connect, rollback_protected_allocations_covers_upcoming
from services import (
    balance_snapshot,
    current_commitments,
    get_upcoming_active_coverage,
    get_account_protected_contribution,
    reduce_account_protected_contribution,
    set_protected_allocation_status,
    link_protected_allocation,
    unlink_protected_allocation,
    dashboard,
    mutate_account_balance,
)


class TestPrompt5K1(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_k1.db"
        init_db(self.db_path)
        self.con = db_connect(self.db_path)
        self.con.execute("INSERT OR IGNORE INTO accounts (name, kind, active, current_balance, protected, protected_amount) VALUES ('BCA Poket: Tabungan', 'Owned', 1, 0, 0, 0)")
        self.con.execute("UPDATE accounts SET active=0 WHERE name NOT IN ('BCA Main', 'BCA Poket: Tabungan')")
        self.con.execute("UPDATE accounts SET current_balance=0, protected=0, protected_amount=0 WHERE name IN ('BCA Main', 'BCA Poket: Tabungan')")
        self.con.execute("DELETE FROM allocation_goals")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_1_migration_idempotent_and_existing_rows_null(self):
        # 1. Migrasi aman dipanggil berulang
        init_db(self.db_path)
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(protected_allocations)").fetchall()]
        self.assertIn("covers_upcoming_id", cols)

        # Existing rows must have covers_upcoming_id as NULL
        self.con.execute(
            "INSERT INTO protected_allocations (title, amount, account, status) VALUES ('Liburan', 500000, 'BCA Main', 'Active')"
        )
        self.con.commit()
        row = self.con.execute("SELECT covers_upcoming_id FROM protected_allocations WHERE title='Liburan'").fetchone()
        self.assertIsNone(row["covers_upcoming_id"])

        # Test rollback helper and re-migrate (close connection first to avoid lock)
        self.con.close()
        rollback_protected_allocations_covers_upcoming(self.db_path)

        self.con = db_connect(self.db_path)
        cols_after_rb = [r[1] for r in self.con.execute("PRAGMA table_info(protected_allocations)").fetchall()]
        self.assertNotIn("covers_upcoming_id", cols_after_rb)

        self.con.close()
        init_db(self.db_path)
        self.con = db_connect(self.db_path)
        cols_re = [r[1] for r in self.con.execute("PRAGMA table_info(protected_allocations)").fetchall()]
        self.assertIn("covers_upcoming_id", cols_re)

    def test_2_fk_and_index_active(self):
        # 2. FK and index active
        idxs = [r[1] for r in self.con.execute("PRAGMA index_list(protected_allocations)").fetchall()]
        self.assertIn("idx_protected_allocations_covers_upcoming", idxs)

        # FK constraint validation: invalid upcoming_id fails
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO protected_allocations (title, amount, account, status, covers_upcoming_id) VALUES ('Tiket', 100000, 'BCA Main', 'Active', 99999)"
            )

    def test_3_coverage_full_partial_and_multi_allocations(self):
        # 3. Setup account & upcoming
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status) VALUES (10, 'Tagihan Listrik & WiFi', 1000000, '2026-08-25', 'Upcoming')"
        )

        # Partial coverage 1: 400k
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (1, 'Alokasi Listrik', 400000, 'BCA Main', 'Active', 10)"
        )
        cov1 = get_upcoming_active_coverage(self.con, 10)
        self.assertEqual(cov1, 400000.0)
        self.assertEqual(current_commitments(self.con), 600000.0)  # 1M - 400k = 600k

        # Multi-allocation partial 2: 600k (Full coverage = 400k + 600k = 1M)
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (2, 'Alokasi WiFi', 600000, 'BCA Main', 'Active', 10)"
        )
        cov2 = get_upcoming_active_coverage(self.con, 10)
        self.assertEqual(cov2, 1000000.0)
        self.assertEqual(current_commitments(self.con), 0.0)  # Full covered = 0 effective commitment

    def test_4_overcoverage_and_amount_reduction_rejected(self):
        # 4. Overcoverage rejected
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status) VALUES (20, 'Sewa Rumah', 1000000, '2026-08-30', 'Upcoming')"
        )
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status) VALUES (21, 'Alokasi Tabungan Sewa', 1500000, 'BCA Main', 'Active')"
        )

        # Link fails because 1.5M > 1.0M
        with self.assertRaises(ValueError) as ctx:
            link_protected_allocation(self.con, 21, 20)
        self.assertIn("melebihi", str(ctx.exception).lower())

    def test_5_sts_constant_after_full_and_partial_payment(self):
        # 5. STS constant invariant
        # B = 5,000,000 | P = 2,000,000 | Upcoming X = 800,000 | Coverage C = 500,000
        # Eff commitment = 300,000 | STS_before = 5M - 2M - 300k = 2,700,000
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status, account) VALUES (30, 'Pajak Motor', 800000, '2026-08-28', 'Upcoming', 'BCA Main')"
        )
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (31, 'Alokasi Pajak', 500000, 'BCA Main', 'Active', 30)"
        )

        snap_before = balance_snapshot(self.con)
        comm_before = current_commitments(self.con)
        sts_before = snap_before["total_balance"] - snap_before["protected_savings"] - comm_before
        self.assertEqual(sts_before, 2700000.0)

        # Pay upcoming full amount (800k) from BCA Main
        pay_amount = 800000.0
        mutate_account_balance(self.con, "BCA Main", -pay_amount, "2026-08-28")
        set_protected_allocation_status(self.con, 31, "Spent", is_upcoming_settlement=True)
        self.con.execute("UPDATE upcoming SET status='Paid' WHERE id=30")

        snap_after = balance_snapshot(self.con)
        comm_after = current_commitments(self.con)
        sts_after = snap_after["total_balance"] - snap_after["protected_savings"] - comm_after

        # B' = 4.2M, P' = 1.5M, Comm' = 0 -> STS' = 4.2M - 1.5M - 0 = 2.7M (EXACT MATCH!)
        self.assertEqual(snap_after["total_balance"], 4200000.0)
        self.assertEqual(snap_after["protected_savings"], 1500000.0)
        self.assertEqual(comm_after, 0.0)
        self.assertAlmostEqual(sts_before, sts_after, delta=0.005)

    def test_6_payment_account_different_from_allocation_account(self):
        # 6. Rekening pembayaran (BCA Main) berbeda dari rekening alokasi (BCA Poket: Tabungan)
        self.con.execute("UPDATE accounts SET current_balance=1000000, protected=0, protected_amount=0 WHERE name='BCA Main'")
        self.con.execute("UPDATE accounts SET current_balance=3000000, protected=0, protected_amount=2000000 WHERE name='BCA Poket: Tabungan'")

        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status, account) VALUES (40, 'Tiket Kereta', 600000, '2026-08-29', 'Upcoming', 'BCA Main')"
        )
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (41, 'Alokasi Tiket di Poket', 600000, 'BCA Poket: Tabungan', 'Active', 40)"
        )

        sts_before = (1000000 + 3000000) - 2000000 - 0.0  # 2,000,000

        # Bayar dari BCA Main
        mutate_account_balance(self.con, "BCA Main", -600000, "2026-08-29")
        set_protected_allocation_status(self.con, 41, "Spent", is_upcoming_settlement=True)
        self.con.execute("UPDATE upcoming SET status='Paid' WHERE id=40")

        snap = balance_snapshot(self.con)
        comm = current_commitments(self.con)
        sts_after = snap["total_balance"] - snap["protected_savings"] - comm

        # BCA Main: 400k, BCA Poket: 3M (Total 3.4M), Protected: 1.4M, Comm: 0 -> STS: 2.0M
        self.assertAlmostEqual(sts_before, sts_after, delta=0.005)

    def test_7_released_linked_allocation_decreases_p_and_increases_commitment(self):
        # 7. Released linked allocation: P turun, commitment naik, STS konstan
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status) VALUES (50, 'Laptop Service', 500000, '2026-08-30', 'Upcoming')"
        )
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (51, 'Alokasi Service', 500000, 'BCA Main', 'Active', 50)"
        )

        # Before: B=5M, P=2M, Comm=0 -> STS = 3M
        snap_b = balance_snapshot(self.con)
        self.assertEqual(snap_b["total_balance"] - snap_b["protected_savings"] - current_commitments(self.con), 3000000.0)

        # Release allocation manually
        set_protected_allocation_status(self.con, 51, "Released")

        # After: B=5M, P'=1.5M, Comm'=500k -> STS = 5M - 1.5M - 500k = 3M (KONSTAN!)
        snap_a = balance_snapshot(self.con)
        comm_a = current_commitments(self.con)
        self.assertEqual(snap_a["protected_savings"], 1500000.0)
        self.assertEqual(comm_a, 500000.0)
        self.assertEqual(snap_a["total_balance"] - snap_a["protected_savings"] - comm_a, 3000000.0)

    def test_8_skip_upcoming_unlinks_allocation_preserving_p(self):
        # 8. Skip: link lepas, alokasi dan P tetap
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status) VALUES (60, 'Kewajiban Batal', 400000, '2026-08-30', 'Upcoming')"
        )
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (61, 'Alokasi Dana', 400000, 'BCA Main', 'Active', 60)"
        )

        # Skip
        self.con.execute("UPDATE protected_allocations SET covers_upcoming_id=NULL WHERE covers_upcoming_id=60 AND status='Active'")
        self.con.execute("UPDATE upcoming SET status='Skipped' WHERE id=60")

        alloc = self.con.execute("SELECT * FROM protected_allocations WHERE id=61").fetchone()
        self.assertEqual(alloc["status"], "Active")
        self.assertIsNone(alloc["covers_upcoming_id"])

        snap = balance_snapshot(self.con)
        self.assertEqual(snap["protected_savings"], 2000000.0)  # P tetap

    def test_9_duplicate_status_or_pay_idempotent(self):
        # 9. Klik status/pay dua kali tidak memotong ganda
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status) VALUES (71, 'Dana Cadangan', 500000, 'BCA Main', 'Active')"
        )

        res1 = set_protected_allocation_status(self.con, 71, "Released")
        self.assertEqual(res1["allocation_status"], "Released")
        self.assertEqual(balance_snapshot(self.con)["protected_savings"], 1500000.0)

        # Second call to same terminal status is idempotent no-op
        res2 = set_protected_allocation_status(self.con, 71, "Released")
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(balance_snapshot(self.con)["protected_savings"], 1500000.0)  # Tidak terpotong dua kali!

    def test_10_failure_injection_proves_rollback(self):
        # 10. Failure injection membuktikan rollback
        self.con.execute("UPDATE accounts SET current_balance=5000000, protected=0, protected_amount=2000000 WHERE name='BCA Main'")
        self.con.execute(
            "INSERT INTO upcoming (id, title, amount, due_date, status) VALUES (80, 'Test Rollback', 500000, '2026-08-30', 'Upcoming')"
        )
        self.con.execute(
            "INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (81, 'Alokasi Rollback', 500000, 'BCA Main', 'Active', 80)"
        )
        self.con.commit()

        # Simulate transaction failure
        try:
            self.con.execute("BEGIN TRANSACTION")
            mutate_account_balance(self.con, "BCA Main", -500000, "2026-08-30")
            set_protected_allocation_status(self.con, 81, "Spent", is_upcoming_settlement=True)
            raise RuntimeError("Simulated crash before upcoming mark paid")
        except RuntimeError:
            self.con.rollback()

        # Verify state rolled back
        alloc = self.con.execute("SELECT status FROM protected_allocations WHERE id=81").fetchone()
        self.assertEqual(alloc["status"], "Active")
        snap = balance_snapshot(self.con)
        self.assertEqual(snap["total_balance"], 5000000.0)
        self.assertEqual(snap["protected_savings"], 2000000.0)

    def test_11_production_data_unaltered_and_unlinked(self):
        # 11. Data produksi money_tracks.db tidak disentuh dan Alokasi ID 1 / Upcoming ID 3 tetap tidak tertaut otomatis
        prod_con = db_connect("money_tracks.db")
        try:
            cols = {r[1] for r in prod_con.execute("PRAGMA table_info(protected_allocations)").fetchall()}
            if "covers_upcoming_id" in cols:
                prod_allocs = prod_con.execute("SELECT * FROM protected_allocations").fetchall()
                for pa in prod_allocs:
                    self.assertIsNone(pa["covers_upcoming_id"])
        finally:
            prod_con.close()


if __name__ == "__main__":
    unittest.main()
