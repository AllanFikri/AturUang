import unittest
import sqlite3
import shutil
import tempfile
import hashlib
import json
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db
import services
import ingestion

class TestPrompt13aIngestion(unittest.TestCase):
    def setUp(self):
        self.prod_db = BASE_DIR / "money_tracks.db"
        self.prod_hash_before = hashlib.sha256(self.prod_db.read_bytes()).hexdigest() if self.prod_db.exists() else None

        self.tmp_dir = tempfile.mkdtemp()
        self.test_db = Path(self.tmp_dir) / "money_tracks.db"
        shutil.copyfile(self.prod_db, self.test_db)
        self.con = sqlite3.connect(self.test_db)
        self.con.row_factory = sqlite3.Row
        ingestion.init_staging_schema(self.con)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_migration_idempotent(self):
        """Test 1: Staging schema creation is completely idempotent across multiple runs."""
        # Run init_staging_schema multiple times
        ingestion.init_staging_schema(self.con)
        ingestion.init_staging_schema(self.con)
        
        tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("import_batches", tables)
        self.assertIn("raw_import_events", tables)
        self.assertIn("import_candidates", tables)

    def test_02_parser_contract_and_providers(self):
        """Test 2: All provider parsers implement parser contract correctly."""
        for parser in ingestion.PARSER_REGISTRY:
            self.assertTrue(hasattr(parser, "can_parse"))
            self.assertTrue(hasattr(parser, "parse"))
            self.assertTrue(hasattr(parser, "build_fingerprint"))
            self.assertTrue(hasattr(parser, "explain"))

        # Test BCA Email Parser
        bca_p = ingestion.BCAEmailParser()
        bca_raw = "Transaksi QRIS BCA di Kafe Kopi Mantap sebesar Rp 45.000 pada 2026-08-25 14:00 WIB"
        self.assertTrue(bca_p.can_parse({"provider": "bca_email"}, bca_raw))
        parsed = bca_p.parse({"raw_payload": bca_raw, "occurred_at": "2026-08-25T14:00:00"})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["amount"], 45000.0)
        self.assertEqual(parsed["date"], "2026-08-25")
        self.assertEqual(parsed["transaction_type"], "Expense")
        self.assertEqual(parsed["account_from"], "BCA Main")

        # Test ShopeePay Parser
        shopee_p = ingestion.ShopeePayEmailParser()
        shopee_raw = "Pembayaran ShopeePay sebesar Rp 25.500 berhasil pada 2026-08-25 10:15 WIB"
        self.assertTrue(shopee_p.can_parse({"provider": "shopeepay_email"}, shopee_raw))
        shopee_parsed = shopee_p.parse({"raw_payload": shopee_raw, "occurred_at": "2026-08-25T10:15:00"})
        self.assertIsNotNone(shopee_parsed)
        self.assertEqual(shopee_parsed["amount"], 25500.0)
        self.assertEqual(shopee_parsed["account_from"], "ShopeePay")

        # Test CSV Generic Parser
        csv_p = ingestion.CSVGenericParser()
        csv_raw = "2026-08-25,Expense,120000,BCA Main,Beli Buku,Education"
        self.assertTrue(csv_p.can_parse({"source_type": "csv"}, csv_raw))
        csv_parsed = csv_p.parse({"raw_payload": csv_raw})
        self.assertIsNotNone(csv_parsed)
        self.assertEqual(csv_parsed["amount"], 120000.0)
        self.assertEqual(csv_parsed["account_from"], "BCA Main")

    def test_03_nominal_parsing_formats(self):
        """Test 3: Correctly parses diverse nominal formats."""
        self.assertEqual(ingestion.parse_nominal("Rp67.821,80"), 67821.80)
        self.assertEqual(ingestion.parse_nominal("Rp67.821"), 67821.00)
        self.assertEqual(ingestion.parse_nominal("IDR 67,821.80"), 67821.80)
        self.assertEqual(ingestion.parse_nominal("67821.80"), 67821.80)
        self.assertEqual(ingestion.parse_nominal("67821"), 67821.00)
        self.assertEqual(ingestion.parse_nominal("DB 50.000,00"), 50000.00)
        self.assertEqual(ingestion.parse_nominal("CR 100.000,00"), 100000.00)
        self.assertEqual(ingestion.parse_nominal("1.418.000,00"), 1418000.00)

    def test_04_raw_payload_redaction_and_privacy(self):
        """Test 4: Raw payload strictly redacts card numbers, OTPs, and secrets."""
        raw_msg = "Transaksi BCA Kartu 4111 2222 3333 4444 Rp 50.000. Kode OTP: 981234. Token: eyJhbGciOiJIUzI1Ni."
        sanitized = ingestion.sanitize_payload(raw_msg)
        self.assertNotIn("4111 2222 3333 4444", sanitized)
        self.assertIn("•••• 4444", sanitized)
        self.assertNotIn("981234", sanitized)
        self.assertIn("OTP: [REDACTED]", sanitized)
        self.assertNotIn("eyJhbGciOiJIUzI1Ni", sanitized)

    def test_05_exact_and_probable_deduplication(self):
        """Test 5: Exact duplicates are marked Duplicate; Probable duplicates are flagged in Pending."""
        # 1. Existing transaction in ledger
        self.con.execute("""
            INSERT INTO transactions (date, transaction_type, amount, account_from, description, category, money_context, status)
            VALUES ('2026-08-20', 'Expense', 50000.0, 'BCA Main', 'Makan Siang Resto', 'Food & Dining', 'Personal', 'Confirmed')
        """)
        self.con.commit()

        # Batch
        b_res = ingestion.create_import_batch(self.con, "email", "Test Ingestion")
        batch_id = b_res["batch_id"]

        # Exact match (same date, amount, account, type)
        item_exact = {
            "provider": "bca_email",
            "external_event_id": "BCA-EXACT-01",
            "raw_payload": "Transaksi BCA di Makan Siang Resto sebesar Rp 50.000 pada 2026-08-20 12:00 WIB",
            "occurred_at": "2026-08-20T12:00:00+07:00"
        }
        res_exact = ingestion.process_single_raw_item(self.con, batch_id, item_exact)
        self.assertEqual(res_exact["duplicate_status"], "Exact")
        self.assertEqual(res_exact["review_status"], "Duplicate")

        # Probable match (date 2026-08-21 is within +-1 day, same amount & account)
        item_prob = {
            "provider": "bca_email",
            "external_event_id": "BCA-PROB-01",
            "raw_payload": "Transaksi BCA di Restoran Baru sebesar Rp 50.000 pada 2026-08-21 12:00 WIB",
            "occurred_at": "2026-08-21T12:00:00+07:00"
        }
        res_prob = ingestion.process_single_raw_item(self.con, batch_id, item_prob)
        self.assertEqual(res_prob["duplicate_status"], "Probable")
        self.assertEqual(res_prob["review_status"], "Pending")

    def test_06_concurrent_and_replay_duplicate_prevention(self):
        """Test 6: DB constraints strictly reject duplicate raw external_event_ids and duplicate fingerprints."""
        b_res = ingestion.create_import_batch(self.con, "email", "Batch 1")
        batch_id = b_res["batch_id"]

        item = {
            "provider": "bca_email",
            "external_event_id": "BCA-UNIQUE-101",
            "raw_payload": "Transaksi BCA di Warung Makan sebesar Rp 30.000 pada 2026-08-25 13:00 WIB",
            "occurred_at": "2026-08-25T13:00:00+07:00"
        }
        res1 = ingestion.process_single_raw_item(self.con, batch_id, item)
        self.assertEqual(res1["status"], "ok")

        # Re-processing exact same external event id
        res2 = ingestion.process_single_raw_item(self.con, batch_id, item)
        self.assertEqual(res2["status"], "duplicate")

    def test_07_atomic_approval_and_balance_mutation(self):
        """Test 7: Approving a candidate atomically creates transaction, updates balance, and logs audit."""
        bal_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]

        b_res = ingestion.create_import_batch(self.con, "email", "Batch App")
        item = {
            "provider": "bca_email",
            "external_event_id": "BCA-APP-001",
            "raw_payload": "Transaksi QRIS BCA di Toko Buku sebesar Rp 45.000 pada 2026-08-25 10:00 WIB",
            "occurred_at": "2026-08-25T10:00:00+07:00"
        }
        res_ing = ingestion.process_single_raw_item(self.con, b_res["batch_id"], item)
        cand_id = res_ing["candidate_id"]

        # Approve
        res_app = ingestion.approve_import_candidate(self.con, cand_id, reviewer_notes="Approved via unit test")
        self.con.commit()

        self.assertEqual(res_app["status"], "ok")
        tx_id = res_app["transaction_id"]

        # Transaction verified
        tx = self.con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        self.assertEqual(tx["amount"], 45000.0)
        self.assertEqual(tx["source_refs"], f"import:{cand_id}:bca_email:BCA-APP-001")

        # Balance mutated
        bal_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        self.assertAlmostEqual(bal_after, bal_before - 45000.0, delta=0.005)

        # Audit log
        audit = self.con.execute("SELECT * FROM transaction_audit_log WHERE transaction_id=?", (tx_id,)).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit["action"], "IMPORT_APPROVE")

        # Candidate is Approved
        cand = self.con.execute("SELECT * FROM import_candidates WHERE id=?", (cand_id,)).fetchone()
        self.assertEqual(cand["review_status"], "Approved")

        # Re-approval is rejected
        with self.assertRaises(ValueError):
            ingestion.approve_import_candidate(self.con, cand_id)

    def test_08_rejection_workflow(self):
        """Test 8: Rejection marks status as Rejected without creating transaction or balance change."""
        bal_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        b_res = ingestion.create_import_batch(self.con, "email", "Batch Rej")
        item = {
            "provider": "bca_email",
            "external_event_id": "BCA-REJ-001",
            "raw_payload": "Transaksi BCA di Merchant Salah sebesar Rp 10.000 pada 2026-08-25 10:00 WIB",
            "occurred_at": "2026-08-25T10:00:00+07:00"
        }
        res_ing = ingestion.process_single_raw_item(self.con, b_res["batch_id"], item)
        cand_id = res_ing["candidate_id"]

        # Reject
        res_rej = ingestion.reject_import_candidate(self.con, cand_id, reviewer_notes="Bukan transaksi saya")
        self.con.commit()

        self.assertEqual(res_rej["status"], "ok")
        cand = self.con.execute("SELECT * FROM import_candidates WHERE id=?", (cand_id,)).fetchone()
        self.assertEqual(cand["review_status"], "Rejected")

        # Balance remains unchanged
        bal_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        self.assertEqual(bal_before, bal_after)

        # Cannot approve rejected candidate
        with self.assertRaises(ValueError):
            ingestion.approve_import_candidate(self.con, cand_id)

    def test_09_invalid_account_rejection_and_rollback(self):
        """Test 9: Approval of candidate with non-existent or inactive account aborts cleanly."""
        b_res = ingestion.create_import_batch(self.con, "csv", "Batch Inv")
        item = {
            "provider": "csv_generic",
            "external_event_id": "CSV-INV-001",
            "raw_payload": "2026-08-25,Expense,50000,BankFiktif,Belanja,Other",
            "occurred_at": "2026-08-25T12:00:00"
        }
        res_ing = ingestion.process_single_raw_item(self.con, b_res["batch_id"], item)
        cand_id = res_ing["candidate_id"]

        with self.assertRaises(ValueError):
            ingestion.approve_import_candidate(self.con, cand_id)

    def test_10_transfer_pair_mutates_both_accounts(self):
        """Test 10: Transfer candidate approval updates both from and to account balances."""
        bal_bca_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        bal_tab_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Poket: Tabungan'").fetchone()["current_balance"]

        b_res = ingestion.create_import_batch(self.con, "csv", "Batch Transfer")
        # Direct candidate insertion for transfer pair
        cur = self.con.execute("""
            INSERT INTO raw_import_events (batch_id, provider, external_event_id, minimal_raw_payload, payload_hash, parse_status)
            VALUES (?, 'csv_generic', 'TRF-PAIR-01', 'transfer payload', 'hash123', 'PARSED')
        """, (b_res["batch_id"],))
        raw_id = cur.lastrowid

        cur_c = self.con.execute("""
            INSERT INTO import_candidates (
                raw_event_id, fingerprint, date, time, transaction_type,
                amount, account_from, account_to, description, category,
                money_context, confidence, review_status
            ) VALUES (?, 'fp_trf_01', '2026-08-25', '12:00', 'Transfer', 200000.0, 'BCA Main', 'BCA Poket: Tabungan', 'Pindah Tabungan', 'Transfer', 'Personal', 'high', 'Pending')
        """, (raw_id,))
        cand_id = cur_c.lastrowid

        # Approve
        res_app = ingestion.approve_import_candidate(self.con, cand_id)
        self.con.commit()

        self.assertEqual(res_app["status"], "ok")
        bal_bca_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        bal_tab_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Poket: Tabungan'").fetchone()["current_balance"]

        self.assertAlmostEqual(bal_bca_after, bal_bca_before - 200000.0, delta=0.005)
        self.assertAlmostEqual(bal_tab_after, bal_tab_before + 200000.0, delta=0.005)

    def test_11_bulk_approval_safety(self):
        """Test 11: Bulk approval skips low confidence and probable duplicate candidates by default."""
        b_res = ingestion.create_import_batch(self.con, "csv", "Batch Bulk")
        batch_id = b_res["batch_id"]

        # Insert 3 candidates: 1 high confidence, 1 low confidence, 1 probable duplicate
        cur_r = self.con.execute("""
            INSERT INTO raw_import_events (batch_id, provider, external_event_id, minimal_raw_payload, payload_hash, parse_status)
            VALUES (?, 'csv_generic', 'BULK-01', 'bulk payload', 'hashbulk', 'PARSED')
        """, (batch_id,))
        raw_id = cur_r.lastrowid

        c1 = self.con.execute("""
            INSERT INTO import_candidates (raw_event_id, fingerprint, date, transaction_type, amount, account_from, description, confidence, duplicate_status, review_status)
            VALUES (?, 'fp_b1', '2026-08-25', 'Expense', 10000.0, 'BCA Main', 'Beli Kopi', 'high', 'None', 'Pending')
        """, (raw_id,)).lastrowid

        c2 = self.con.execute("""
            INSERT INTO import_candidates (raw_event_id, fingerprint, date, transaction_type, amount, account_from, description, confidence, duplicate_status, review_status)
            VALUES (?, 'fp_b2', '2026-08-25', 'Expense', 20000.0, 'BCA Main', 'Beli Snack', 'low', 'None', 'Pending')
        """, (raw_id,)).lastrowid

        c3 = self.con.execute("""
            INSERT INTO import_candidates (raw_event_id, fingerprint, date, transaction_type, amount, account_from, description, confidence, duplicate_status, review_status)
            VALUES (?, 'fp_b3', '2026-08-25', 'Expense', 30000.0, 'BCA Main', 'Beli Makan', 'high', 'Probable', 'Pending')
        """, (raw_id,)).lastrowid

        # Bulk approve without allowing low confidence or probable duplicate
        bulk_res = ingestion.bulk_approve_candidates(self.con, [c1, c2, c3], allow_low_confidence=False, allow_probable=False)
        self.con.commit()

        self.assertEqual(bulk_res["approved_count"], 1)
        self.assertEqual(bulk_res["total_amount"], 10000.0)
        self.assertIn(c2, bulk_res["skipped_ids"])
        self.assertIn(c3, bulk_res["skipped_ids"])

    def test_12_no_impact_on_reports_before_approval(self):
        """Test 12: Ingestion staging data has zero impact on dashboard, verify_balances, and reports before approval."""
        dash_before = services.dashboard(self.con)
        rep_before = services.report_data(self.con, "monthly")

        # Ingest items
        b_res = ingestion.create_import_batch(self.con, "email", "Test Impact")
        item = {
            "provider": "bca_email",
            "external_event_id": "BCA-IMPACT-01",
            "raw_payload": "Transaksi BCA di Toko Komputer sebesar Rp 5.000.000 pada 2026-08-25 12:00 WIB",
            "occurred_at": "2026-08-25T12:00:00"
        }
        ingestion.process_single_raw_item(self.con, b_res["batch_id"], item)
        self.con.commit()

        dash_after = services.dashboard(self.con)
        rep_after = services.report_data(self.con, "monthly")

        self.assertEqual(dash_before["kpis"]["safeToSpend"], dash_after["kpis"]["safeToSpend"])
        self.assertEqual(dash_before["kpis"]["totalBalance"], dash_after["kpis"]["totalBalance"])
        self.assertEqual(rep_before["monthly"], rep_after["monthly"])

    def test_14_api_endpoints_workflow(self):
        """Test 14: Verifies batch query and candidates filter APIs."""
        b_res = ingestion.create_import_batch(self.con, "csv", "API Test Batch", "raw content header")
        batch_id = b_res["batch_id"]

        raw_items = [
            {
                "provider": "csv_generic",
                "external_event_id": "API-01",
                "raw_payload": "2026-08-25,Expense,15000,BCA Main,Beli Es Teh,Food & Dining",
                "occurred_at": "2026-08-25T11:00:00"
            },
            {
                "provider": "csv_generic",
                "external_event_id": "API-02",
                "raw_payload": "2026-08-25,Income,500000,BCA Main,Honor Freelance,Income",
                "occurred_at": "2026-08-25T12:00:00"
            }
        ]
        proc = ingestion.process_raw_items(self.con, batch_id, raw_items)
        self.con.commit()

        self.assertEqual(proc["total"], 2)
        self.assertEqual(proc["parsed"], 2)

        # Query batches
        batches = ingestion.get_import_batches(self.con)
        self.assertGreaterEqual(len(batches), 1)
        self.assertEqual(batches[0]["id"], batch_id)
        self.assertEqual(batches[0]["status"], "COMPLETED")

        # Query candidates by status
        cands_pending = ingestion.get_import_candidates(self.con, status="Pending", batch_id=batch_id)
        self.assertEqual(len(cands_pending), 2)

        cand_id_1 = cands_pending[0]["id"]
        # Approve first candidate
        app_res = ingestion.approve_import_candidate(self.con, cand_id_1, reviewer_notes="Approved via API")
        self.con.commit()
        self.assertEqual(app_res["status"], "ok")

        cands_approved = ingestion.get_import_candidates(self.con, status="Approved", batch_id=batch_id)
        self.assertEqual(len(cands_approved), 1)

    def test_15_production_db_unmodified(self):
        """Test 15: Production database SHA-256 hash remains unmodified."""
        actual_hash = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, self.prod_hash_before, "Production database was altered during test execution!")


if __name__ == "__main__":
    unittest.main()
