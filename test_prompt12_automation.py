import unittest
import sqlite3
import shutil
import tempfile
import hashlib
import time
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import services
import db

class TestPrompt12Automation(unittest.TestCase):
    def setUp(self):
        self.prod_db = BASE_DIR / "money_tracks.db"
        self.prod_hash_before = hashlib.sha256(self.prod_db.read_bytes()).hexdigest() if self.prod_db.exists() else None
        
        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "money_tracks.db"
        shutil.copyfile(self.prod_db, self.test_db)
        self.con = sqlite3.connect(self.test_db)
        self.con.row_factory = sqlite3.Row

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_fixed_monthly_subscription_detected(self):
        """Test 1: Stable monthly subscription with support >= 3 and CV <= 10% is classified as Langganan / Tagihan."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-05-10', 'Expense', 100000.0, 'BCA Main', 'Langganan Hosting Web', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-06-10', 'Expense', 100000.0, 'BCA Main', 'Langganan Hosting Web', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-07-10', 'Expense', 100000.0, 'BCA Main', 'Langganan Hosting Web', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-08-10', 'Expense', 100000.0, 'BCA Main', 'Langganan Hosting Web', 'Subscriptions', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        hosting_p = next((p for p in patterns if "hosting web" in p["normalized_merchant"]), None)
        self.assertIsNotNone(hosting_p, "Hosting pattern not detected!")
        self.assertEqual(hosting_p["classification"], "Langganan / Tagihan")
        self.assertEqual(hosting_p["confidence"], "high")
        self.assertAlmostEqual(hosting_p["median_amount"], 100000.0, delta=0.01)
        self.assertLessEqual(hosting_p["amount_cv"], 0.10)
        self.assertEqual(hosting_p["support"], 4)

    def test_02_high_variation_pattern_categorized_as_pola_belanja(self):
        """Test 2: Variable recurring pattern with nominal CV > 10% is classified as Pola Belanja with medium confidence."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-08-01', 'Expense', 20000.0, 'BCA Main', 'Kopi Senja Kafe', 'Food & Dining', 'Personal', 'Confirmed'),
                ('2026-08-05', 'Expense', 50000.0, 'BCA Main', 'Kopi Senja Kafe', 'Food & Dining', 'Personal', 'Confirmed'),
                ('2026-08-10', 'Expense', 95000.0, 'BCA Main', 'Kopi Senja Kafe', 'Food & Dining', 'Personal', 'Confirmed'),
                ('2026-08-15', 'Expense', 35000.0, 'BCA Main', 'Kopi Senja Kafe', 'Food & Dining', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        kopi_p = next((p for p in patterns if "kopi senja" in p["normalized_merchant"]), None)
        self.assertIsNotNone(kopi_p, "Kopi Senja pattern not detected!")
        self.assertEqual(kopi_p["classification"], "Pola Belanja")
        self.assertEqual(kopi_p["confidence"], "medium")
        self.assertGreater(kopi_p["amount_cv"], 0.10)

    def test_03_support_under_3_abstains(self):
        """Test 3: Recurring pattern with support < 3 is classified as Abstain with low confidence."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-08-01', 'Expense', 45000.0, 'BCA Main', 'Gym Fitness Baru', 'Health & Fitness', 'Personal', 'Confirmed'),
                ('2026-08-15', 'Expense', 45000.0, 'BCA Main', 'Gym Fitness Baru', 'Health & Fitness', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        gym_p = next((p for p in patterns if "gym fitness" in p["normalized_merchant"]), None)
        self.assertIsNotNone(gym_p, "Gym pattern should be in patterns list as Abstain!")
        self.assertEqual(gym_p["classification"], "Abstain")
        self.assertEqual(gym_p["confidence"], "low")

    def test_04_no_temporal_leakage(self):
        """Test 4: Transactions dated after as_of_date are strictly excluded from pattern detection."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-08-10', 'Expense', 60000.0, 'BCA Main', 'Uji Future Date', 'General', 'Personal', 'Confirmed'),
                ('2026-08-20', 'Expense', 60000.0, 'BCA Main', 'Uji Future Date', 'General', 'Personal', 'Confirmed'),
                ('2026-08-30', 'Expense', 60000.0, 'BCA Main', 'Uji Future Date', 'General', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        future_p = next((p for p in patterns if "uji future" in p["normalized_merchant"]), None)
        self.assertIsNotNone(future_p)
        self.assertEqual(future_p["support"], 2)
        self.assertEqual(future_p["classification"], "Abstain")

    def test_05_exclusion_of_nonpersonal_and_nonoperational_contexts(self):
        """Test 5: Transfer, Adjustment, Third-party, Pass-through, Historical Research, and Provisional are excluded."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, account_to, description, category, money_context, status)
            VALUES 
                ('2026-06-01', 'Transfer', 500000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Auto Save Transfer', 'Transfer', 'Personal', 'Confirmed'),
                ('2026-07-01', 'Transfer', 500000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Auto Save Transfer', 'Transfer', 'Personal', 'Confirmed'),
                ('2026-08-01', 'Transfer', 500000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Auto Save Transfer', 'Transfer', 'Personal', 'Confirmed'),
                ('2026-06-05', 'Expense', 300000.0, 'BCA Main', '', 'Belanja Titipan Kantor', 'General', 'Pass-through', 'Confirmed'),
                ('2026-07-05', 'Expense', 300000.0, 'BCA Main', '', 'Belanja Titipan Kantor', 'General', 'Pass-through', 'Confirmed'),
                ('2026-08-05', 'Expense', 300000.0, 'BCA Main', '', 'Belanja Titipan Kantor', 'General', 'Pass-through', 'Confirmed'),
                ('2026-06-12', 'Expense', 150000.0, 'BCA Main', '', 'Riset Lama 2025', 'General', 'Historical Research', 'Confirmed'),
                ('2026-07-12', 'Expense', 150000.0, 'BCA Main', '', 'Riset Lama 2025', 'General', 'Historical Research', 'Confirmed'),
                ('2026-08-12', 'Expense', 150000.0, 'BCA Main', '', 'Riset Lama 2025', 'General', 'Historical Research', 'Confirmed'),
                ('2026-06-15', 'Expense', 200000.0, 'BCA Main', '', 'Pinjaman Pihak Ketiga', 'General', 'Third-party', 'Confirmed'),
                ('2026-07-15', 'Expense', 200000.0, 'BCA Main', '', 'Pinjaman Pihak Ketiga', 'General', 'Third-party', 'Confirmed'),
                ('2026-08-15', 'Expense', 200000.0, 'BCA Main', '', 'Pinjaman Pihak Ketiga', 'General', 'Third-party', 'Confirmed'),
                ('2026-06-20', 'Expense', 80000.0, 'BCA Main', '', 'Belum Jelas Transaksi', 'General', 'Personal', 'Provisional Neutral'),
                ('2026-07-20', 'Expense', 80000.0, 'BCA Main', '', 'Belum Jelas Transaksi', 'General', 'Personal', 'Provisional Neutral'),
                ('2026-08-20', 'Expense', 80000.0, 'BCA Main', '', 'Belum Jelas Transaksi', 'General', 'Personal', 'Provisional Neutral')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        merchants = [p["normalized_merchant"] for p in patterns]
        self.assertNotIn("auto save transfer", merchants)
        self.assertNotIn("belanja titipan kantor", merchants)
        self.assertNotIn("riset lama", merchants)
        self.assertNotIn("pinjaman pihak ketiga", merchants)
        self.assertNotIn("belum jelas transaksi", merchants)

    def test_06_anti_duplication_with_existing_upcoming(self):
        """Test 6: Recurring patterns matching existing upcoming obligations are flagged with has_existing_upcoming=True."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-06-02', 'Expense', 70000.0, 'BCA Main', 'Renew by.U data package', 'Phone & Internet', 'Personal', 'Confirmed'),
                ('2026-07-02', 'Expense', 70000.0, 'BCA Main', 'Renew by.U data package', 'Phone & Internet', 'Personal', 'Confirmed'),
                ('2026-08-02', 'Expense', 70000.0, 'BCA Main', 'Renew by.U data package', 'Phone & Internet', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        byu_p = next((p for p in patterns if "by" in p["normalized_merchant"]), None)
        self.assertIsNotNone(byu_p)
        self.assertTrue(byu_p["has_existing_upcoming"], "by.U pattern should be matched against upcoming ID 2!")
        self.assertEqual(byu_p["matched_upcoming_id"], 2)

    def test_07_cashflow_forecast_7_and_30_days(self):
        """Test 7: Forecast outputs opening balance, confirmed & estimated outflows, projected balance, and lowest balance."""
        f7 = services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=7)
        f30 = services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)

        self.assertEqual(f7["horizon_days"], 7)
        self.assertEqual(f30["horizon_days"], 30)
        self.assertAlmostEqual(f7["opening_balance"], 2379368.80, delta=0.01)
        self.assertAlmostEqual(f30["opening_balance"], 2379368.80, delta=0.01)

        # In 7 days (2026-08-25 to 2026-09-01): IOM (120k on 08-25), by.U (70k on 08-30), AI (60k on 08-31) = 250k
        self.assertAlmostEqual(f7["confirmed"]["outflow"], 250000.0, delta=0.01)
        self.assertAlmostEqual(f7["confirmed"]["projected_balance"], 2129368.80, delta=0.01)

    def test_08_bus_tentative_excluded_from_confirmed_forecast(self):
        """Test 8: Bus PKL tentative (status=Tentative, reserve_now=0) is excluded from confirmed forecast."""
        f30 = services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)
        conf_titles = [ev["title"] for ev in f30["confirmed"]["events"]]
        self.assertNotIn("Tiket Bus / Perjalanan", conf_titles)
        self.assertNotIn("Bus PKL", conf_titles)

    def test_09_piutang_and_internal_transfer_invariants(self):
        """Test 9: Piutang is not counted as cash inflow before Settlement, and internal transfers are cash-neutral."""
        f30 = services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)
        self.assertEqual(f30["confirmed"]["income"], 0.0)

        # Internal transfer simulation
        services.mutate_account_balance(self.con, "BCA Poket: Tabungan", -300000.0, "2026-08-25")
        services.mutate_account_balance(self.con, "BCA Main", +300000.0, "2026-08-25")
        f30_after = services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)
        self.assertAlmostEqual(f30_after["opening_balance"], f30["opening_balance"], delta=0.01)

    def test_10_accounts_freshness_indicators(self):
        """Test 10: Freshness indicators classify accounts without using 'sudah sinkron' or 'saldo pasti benar'."""
        freshness = services.get_accounts_freshness(self.con, as_of_date="2026-08-25")
        self.assertIsInstance(freshness, list)
        self.assertGreater(len(freshness), 0)

        allowed_labels = {"Baru diperbarui", "Perlu diperiksa", "Belum pernah diverifikasi"}
        for acc_f in freshness:
            self.assertIn(acc_f["status_label"], allowed_labels)
            self.assertNotIn("sudah sinkron", acc_f["status_label"].lower())
            self.assertNotIn("saldo pasti benar", acc_f["status_label"].lower())
            self.assertIn("days_since_last_record", acc_f)

    def test_11_create_upcoming_from_recurring_atomic_and_idempotent(self):
        """Test 11: create_upcoming_from_recurring creates obligation atomically and resists duplication."""
        payload = {
            "title": "Tagihan Listrik Rumah",
            "amount": 150000.0,
            "due_date": "2026-09-05",
            "category": "Bills & Utilities",
            "account": "BCA Main",
            "reserve_now": 1,
            "notes": "Tagihan rutin bulanan PLN",
        }

        res1 = services.create_upcoming_from_recurring(self.con, payload)
        self.assertEqual(res1["status"], "ok")
        self.assertFalse(res1["is_duplicate"])
        up_id = res1["upcoming_id"]

        res2 = services.create_upcoming_from_recurring(self.con, payload)
        self.assertEqual(res2["status"], "ok")
        self.assertTrue(res2["is_duplicate"])
        self.assertEqual(res2["upcoming_id"], up_id)

    def test_12_performance_benchmark_under_100ms(self):
        """Test 12: Median response time for recurring detection and forecast is under 100ms."""
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
            services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)
            services.get_accounts_freshness(self.con, as_of_date="2026-08-25")
            times.append((time.perf_counter() - t0) * 1000)

        times.sort()
        median_time = times[len(times) // 2]
        self.assertLess(median_time, 100.0, f"Benchmark failed: median execution took {median_time:.2f}ms >= 100ms")

    def test_13_production_db_unmodified(self):
        """Test 13: Production database SHA-256 hash remains unaltered."""
        actual_hash = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Production database was altered during test execution!")


if __name__ == "__main__":
    unittest.main()
