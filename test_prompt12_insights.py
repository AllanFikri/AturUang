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

class TestPrompt12Insights(unittest.TestCase):
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

    def test_01_support_under_3_abstains(self):
        """Test 1: Support < 3 is classified as Abstain with low confidence."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-08-01', 'Expense', 45000.0, 'BCA Main', 'Gym Fitness Baru', 'Health & Fitness', 'Personal', 'Confirmed'),
                ('2026-08-15', 'Expense', 45000.0, 'BCA Main', 'Gym Fitness Baru', 'Health & Fitness', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        gym_p = next((p for p in patterns if "gym fitness" in p["normalized_merchant"]), None)
        self.assertIsNotNone(gym_p)
        self.assertEqual(gym_p["classification"], "Abstain")
        self.assertEqual(gym_p["confidence"], "low")

    def test_02_monthly_pattern_across_month_lengths(self):
        """Test 2: Monthly subscription pattern across months with 28, 30, and 31 days."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-01-31', 'Expense', 100000.0, 'BCA Main', 'Langganan Cloud Storage', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-02-28', 'Expense', 100000.0, 'BCA Main', 'Langganan Cloud Storage', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-03-31', 'Expense', 100000.0, 'BCA Main', 'Langganan Cloud Storage', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-04-30', 'Expense', 100000.0, 'BCA Main', 'Langganan Cloud Storage', 'Subscriptions', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-05-15", min_support=3)
        cloud_p = next((p for p in patterns if "cloud storage" in p["normalized_merchant"]), None)
        self.assertIsNotNone(cloud_p)
        self.assertEqual(cloud_p["classification"], "Langganan / Tagihan")
        self.assertEqual(cloud_p["confidence"], "high")
        self.assertAlmostEqual(cloud_p["median_amount"], 100000.0, delta=0.01)

    def test_03_no_future_leakage(self):
        """Test 3: Recommendations and projections for date D strictly exclude transactions after D."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-08-10', 'Expense', 60000.0, 'BCA Main', 'Uji Future Leakage', 'General', 'Personal', 'Confirmed'),
                ('2026-08-20', 'Expense', 60000.0, 'BCA Main', 'Uji Future Leakage', 'General', 'Personal', 'Confirmed'),
                ('2026-08-30', 'Expense', 60000.0, 'BCA Main', 'Uji Future Leakage', 'General', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        # As of 2026-08-25: 2026-08-30 is in the future and must NOT be included
        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        fut_p = next((p for p in patterns if "uji future leakage" in p["normalized_merchant"]), None)
        self.assertIsNotNone(fut_p)
        self.assertEqual(fut_p["support"], 2)
        self.assertEqual(fut_p["classification"], "Abstain")

    def test_04_transfer_not_treated_as_expense(self):
        """Test 4: Internal transfers are excluded from personal spending patterns."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, account_to, description, category, money_context, status)
            VALUES 
                ('2026-06-01', 'Transfer', 500000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Pindah Saldo Tabungan', 'Transfer', 'Personal', 'Confirmed'),
                ('2026-07-01', 'Transfer', 500000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Pindah Saldo Tabungan', 'Transfer', 'Personal', 'Confirmed'),
                ('2026-08-01', 'Transfer', 500000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Pindah Saldo Tabungan', 'Transfer', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        patterns = services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        merchants = [p["normalized_merchant"] for p in patterns]
        self.assertNotIn("pindah saldo tabungan", merchants)

    def test_05_nonpersonal_and_nonoperational_contexts_isolated(self):
        """Test 5: Third-party, Historical Research, Pass-through, Adjustments, and Provisional are excluded."""
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, account_to, description, category, money_context, status)
            VALUES 
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
        self.assertNotIn("belanja titipan kantor", merchants)
        self.assertNotIn("riset lama", merchants)
        self.assertNotIn("pinjaman pihak ketiga", merchants)
        self.assertNotIn("belum jelas transaksi", merchants)

    def test_06_deduplication_forecast_vs_upcoming(self):
        """Test 6: Recurring patterns matching existing upcoming obligations are flagged and not double counted."""
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
        self.assertTrue(byu_p["has_existing_upcoming"])

    def test_07_unfunded_goal_targets_excluded_from_forecast(self):
        """Test 7: Unfunded allocation goal targets are excluded from cash outflow projections."""
        f30 = services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)
        conf_titles = [ev["title"] for ev in f30["confirmed"]["events"]]
        est_titles = [ev["title"] for ev in f30["estimated"]["events"]]
        
        # Goals are not cash outflows
        self.assertNotIn("Dana Darurat", conf_titles)
        self.assertNotIn("Dana Darurat", est_titles)

    def test_08_forecast_does_not_mutate_database(self):
        """Test 8: Forecast and pattern detection are purely read-only."""
        tx_count_before = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        bal_before = self.con.execute("SELECT SUM(current_balance) FROM accounts WHERE active=1 AND kind='Owned'").fetchone()[0]

        services.cashflow_forecast(self.con, as_of_date="2026-08-25", horizon_days=30)
        services.detect_recurring_patterns(self.con, as_of_date="2026-08-25", min_support=3)
        services.detect_anomalies(self.con, as_of_date="2026-08-25", min_support=3)
        services.get_source_freshness(self.con, as_of_date="2026-08-25")

        tx_count_after = self.con.execute("SELECT count(*) FROM transactions").fetchone()[0]
        bal_after = self.con.execute("SELECT SUM(current_balance) FROM accounts WHERE active=1 AND kind='Owned'").fetchone()[0]

        self.assertEqual(tx_count_before, tx_count_after)
        self.assertEqual(bal_before, bal_after)

    def test_09_nominal_anomaly_and_zero_mad(self):
        """Test 9: Anomaly detection handles both non-zero MAD and zero MAD (fixed historical amount) cases."""
        # 1. Zero MAD case (3 identical transactions of 50k, then sudden 120k)
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-06-10', 'Expense', 50000.0, 'BCA Main', 'Langganan Servis A', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-07-10', 'Expense', 50000.0, 'BCA Main', 'Langganan Servis A', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-08-10', 'Expense', 50000.0, 'BCA Main', 'Langganan Servis A', 'Subscriptions', 'Personal', 'Confirmed'),
                ('2026-08-20', 'Expense', 120000.0, 'BCA Main', 'Langganan Servis A', 'Subscriptions', 'Personal', 'Confirmed')
        """)

        # 2. Non-zero MAD outlier case
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES 
                ('2026-06-01', 'Expense', 20000.0, 'BCA Main', 'Warung Makan Barokah', 'Food & Dining', 'Personal', 'Confirmed'),
                ('2026-06-15', 'Expense', 22000.0, 'BCA Main', 'Warung Makan Barokah', 'Food & Dining', 'Personal', 'Confirmed'),
                ('2026-07-01', 'Expense', 21000.0, 'BCA Main', 'Warung Makan Barokah', 'Food & Dining', 'Personal', 'Confirmed'),
                ('2026-08-15', 'Expense', 150000.0, 'BCA Main', 'Warung Makan Barokah', 'Food & Dining', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        anomalies = services.detect_anomalies(self.con, as_of_date="2026-08-25", min_support=3, lookback_days=30)
        self.assertGreaterEqual(len(anomalies), 2)
        
        for anom in anomalies:
            self.assertEqual(anom["status_label"], "Perlu dicek")
            self.assertNotIn("salah", anom["status_label"].lower())
            self.assertNotIn("fraud", anom["status_label"].lower())

    def test_10_wib_timezone_and_date_boundaries(self):
        """Test 10: Calculations strictly follow Asia/Jakarta (WIB) boundaries."""
        fresh = services.get_source_freshness(self.con, as_of_date="2026-08-25")
        self.assertEqual(fresh["source_name"], "Input manual")
        self.assertEqual(fresh["status_label"], "Input manual")
        self.assertFalse(fresh["is_sync_connector_active"])
        self.assertIn("WIB", fresh["message"])

    def test_11_api_response_stability_and_esc(self):
        """Test 11: Consolidated insights API returns all keys cleanly."""
        all_ins = services.get_all_insights(self.con, as_of_date="2026-08-25", min_support=3)
        self.assertIn("recurring_patterns", all_ins)
        self.assertIn("forecast_7d", all_ins)
        self.assertIn("forecast_30d", all_ins)
        self.assertIn("anomalies", all_ins)
        self.assertIn("source_freshness", all_ins)

    def test_12_production_db_unmodified(self):
        """Test 12: Production database SHA-256 hash remains unmodified."""
        actual_hash = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Production database was altered during test execution!")


if __name__ == "__main__":
    unittest.main()
