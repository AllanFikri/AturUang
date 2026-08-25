# Test Suite for Prompt 6c — K2 Event Ledger Verification & Integrity
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from db import db_connect, init_db, rollback_k2_event_ledger_migration
from services import (
    balance_snapshot,
    current_commitments,
    total_custody_outstanding,
    total_receivable_outstanding,
    total_payable_outstanding,
    get_payable_effective_commitments,
    reconstruct_debt_outstanding,
    update_debt_status,
    create_debt_position,
    add_debt_event,
    reverse_debt_event,
    link_upcoming_to_debt,
    unlink_upcoming_from_debt,
    link_protected_allocation,
    unlink_protected_allocation,
    set_protected_allocation_status,
    pay_upcoming_atomic,
    mutate_account_balance,
    personal_expense_rows,
    personal_income_rows,
    list_debts,
    get_debt_detail,
)


class TestPrompt6K2(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_k2.db"
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

    def test_1_migration_idempotent_and_aborts_on_nonempty_legacy_debts(self):
        # A. Non-empty legacy debts table must abort migration
        legacy_dir = tempfile.mkdtemp()
        legacy_db = Path(legacy_dir) / "legacy.db"
        con_leg = sqlite3.connect(legacy_db)
        con_leg.execute("CREATE TABLE debts (id INTEGER PRIMARY KEY, title TEXT, amount REAL)")
        con_leg.execute("INSERT INTO debts (id, title, amount) VALUES (1, 'Utang Lama', 500000)")
        con_leg.commit()
        con_leg.close()

        with self.assertRaises(RuntimeError) as ctx:
            init_db(legacy_db)
        self.assertIn("dibatalkan", str(ctx.exception).lower())

        # Verify legacy data was untouched
        con_leg_verify = sqlite3.connect(legacy_db)
        row = con_leg_verify.execute("SELECT * FROM debts WHERE id=1").fetchone()
        self.assertIsNotNone(row)
        con_leg_verify.close()
        shutil.rmtree(legacy_dir, ignore_errors=True)

        # B. Clean migration is idempotent and rollback works
        init_db(self.db_path)
        cols_debts = [r[1] for r in self.con.execute("PRAGMA table_info(debts)").fetchall()]
        self.assertIn("kind", cols_debts)
        self.assertIn("person_name", cols_debts)

        cols_up = [r[1] for r in self.con.execute("PRAGMA table_info(upcoming)").fetchall()]
        self.assertIn("debt_id", cols_up)

        idxs = [r[1] for r in self.con.execute("PRAGMA index_list(debt_events)").fetchall()]
        self.assertIn("idx_debt_events_debt", idxs)
        self.assertIn("idx_debt_events_operation_key", idxs)

        # Rollback and re-migrate
        self.con.close()
        rollback_k2_event_ledger_migration(self.db_path)
        init_db(self.db_path)
        self.con = db_connect(self.db_path)

    def test_2_five_events_and_signed_reconstruction(self):
        # 2. Lima event types dan signed reconstruction
        res = create_debt_position(self.con, "Pak Budi", "Receivable", 1000000, "2026-08-20", "Pinjaman awal")
        did = res["debt_id"]
        self.assertEqual(reconstruct_debt_outstanding(self.con, did), 1000000.0)

        # Increase (+500k)
        add_debt_event(self.con, did, "Increase", 500000, "2026-08-21", "Pinjaman tambahan")
        self.assertEqual(reconstruct_debt_outstanding(self.con, did), 1500000.0)

        # Settlement (-400k)
        add_debt_event(self.con, did, "Settlement", 400000, "2026-08-22", "Cicilan 1")
        self.assertEqual(reconstruct_debt_outstanding(self.con, did), 1100000.0)

        # WriteOff (-1.1M)
        add_debt_event(self.con, did, "WriteOff", 1100000, "2026-08-23", "Penghapusan sisa")
        self.assertEqual(reconstruct_debt_outstanding(self.con, did), 0.0)
        self.assertEqual(update_debt_status(self.con, did), "WrittenOff")

    def test_3_seven_algebraic_events_and_sts_invariants(self):
        # 3. Tujuh kejadian aljabar
        self.con.execute("UPDATE accounts SET current_balance=5000000 WHERE name='BCA Main'")
        self.con.commit()

        # 1. Terima Titipan (B+1M, T+1M, STS=5M)
        create_debt_position(self.con, "Titipan A", "Custody", 1000000, "2026-08-23", is_cash=True, account="BCA Main")
        snap1 = balance_snapshot(self.con)
        comm1 = current_commitments(self.con)
        t1 = total_custody_outstanding(self.con)
        self.assertEqual(snap1["total_balance"], 6000000.0)
        self.assertEqual(t1, 1000000.0)
        self.assertEqual(snap1["total_balance"] - snap1["protected_savings"] - t1 - comm1, 5000000.0)

        # 2. Kembalikan Titipan (B-1M, T-1M, STS=5M)
        did_t = self.con.execute("SELECT id FROM debts WHERE person_name='Titipan A'").fetchone()["id"]
        add_debt_event(self.con, did_t, "Settlement", 1000000, "2026-08-23", is_cash=True, account="BCA Main")
        snap2 = balance_snapshot(self.con)
        t2 = total_custody_outstanding(self.con)
        self.assertEqual(snap2["total_balance"], 5000000.0)
        self.assertEqual(t2, 0.0)
        self.assertEqual(snap2["total_balance"] - snap2["protected_savings"] - t2 - current_commitments(self.con), 5000000.0)

        # 3. Beri Pinjaman Piutang (B-600k, Piutang+600k, STS turun 600k -> 4.4M)
        create_debt_position(self.con, "Teman B", "Receivable", 600000, "2026-08-23", is_cash=True, account="BCA Main")
        snap3 = balance_snapshot(self.con)
        r3 = total_receivable_outstanding(self.con)
        self.assertEqual(snap3["total_balance"], 4400000.0)
        self.assertEqual(r3, 600000.0)
        self.assertEqual(snap3["total_balance"] - snap3["protected_savings"] - 0 - current_commitments(self.con), 4400000.0)

        # 4. Terima Pelunasan Piutang (B+600k, Piutang-600k, STS naik 600k -> 5.0M)
        did_r = self.con.execute("SELECT id FROM debts WHERE person_name='Teman B'").fetchone()["id"]
        add_debt_event(self.con, did_r, "Settlement", 600000, "2026-08-23", is_cash=True, account="BCA Main")
        snap4 = balance_snapshot(self.con)
        self.assertEqual(snap4["total_balance"], 5000000.0)
        self.assertEqual(total_receivable_outstanding(self.con), 0.0)
        self.assertEqual(snap4["total_balance"] - snap4["protected_savings"] - 0 - current_commitments(self.con), 5000000.0)

        # 5. Terima Pinjaman Utang (B+2M, Utang+2M, STS=5M)
        create_debt_position(self.con, "Kreditur C", "Payable", 2000000, "2026-08-23", is_cash=True, account="BCA Main")
        snap5 = balance_snapshot(self.con)
        u5 = total_payable_outstanding(self.con)
        comm5 = current_commitments(self.con)
        self.assertEqual(snap5["total_balance"], 7000000.0)
        self.assertEqual(u5, 2000000.0)
        self.assertEqual(comm5, 2000000.0)
        self.assertEqual(snap5["total_balance"] - snap5["protected_savings"] - 0 - comm5, 5000000.0)

        # 6. Bayar Pokok Utang (B-2M, Utang-2M, STS=5M)
        did_u = self.con.execute("SELECT id FROM debts WHERE person_name='Kreditur C'").fetchone()["id"]
        add_debt_event(self.con, did_u, "Settlement", 2000000, "2026-08-23", is_cash=True, account="BCA Main")
        snap6 = balance_snapshot(self.con)
        comm6 = current_commitments(self.con)
        self.assertEqual(snap6["total_balance"], 5000000.0)
        self.assertEqual(comm6, 0.0)
        self.assertEqual(snap6["total_balance"] - snap6["protected_savings"] - 0 - comm6, 5000000.0)

        # 7. Utang Non-Kas (B tetap 5M, Utang+500k, STS turun 500k -> 4.5M)
        create_debt_position(self.con, "Toko D", "Payable", 500000, "2026-08-23", is_cash=False)
        snap7 = balance_snapshot(self.con)
        comm7 = current_commitments(self.con)
        self.assertEqual(snap7["total_balance"], 5000000.0)
        self.assertEqual(comm7, 500000.0)
        self.assertEqual(snap7["total_balance"] - snap7["protected_savings"] - 0 - comm7, 4500000.0)

    def test_4_cash_vs_non_cash_modes(self):
        # 4. Cash vs sudah-tercermin
        self.con.execute("UPDATE accounts SET current_balance=1000000 WHERE name='BCA Main'")
        self.con.commit()

        # Non-cash mode
        create_debt_position(self.con, "Pak RT", "Custody", 300000, "2026-08-23", is_cash=False)
        self.assertEqual(balance_snapshot(self.con)["total_balance"], 1000000.0)

        # Cash mode
        create_debt_position(self.con, "Bu RT", "Custody", 200000, "2026-08-23", is_cash=True, account="BCA Main")
        self.assertEqual(balance_snapshot(self.con)["total_balance"], 1200000.0)

    def test_5_partial_settlement_and_over_settlement_rejected(self):
        # 5. Partial settlement dan over-settlement
        res = create_debt_position(self.con, "Pinjaman X", "Payable", 1000000, "2026-08-23")
        did = res["debt_id"]

        add_debt_event(self.con, did, "Settlement", 300000, "2026-08-23")
        self.assertEqual(reconstruct_debt_outstanding(self.con, did), 700000.0)

        # Over-settlement 800k > 700k rejected
        with self.assertRaises(ValueError) as ctx:
            add_debt_event(self.con, did, "Settlement", 800000, "2026-08-23")
        self.assertIn("melebihi", str(ctx.exception).lower())

    def test_6_automatic_status_transition(self):
        # 6. Status otomatis
        res = create_debt_position(self.con, "Status Test", "Receivable", 500000, "2026-08-23")
        did = res["debt_id"]
        d = self.con.execute("SELECT status FROM debts WHERE id=?", (did,)).fetchone()
        self.assertEqual(d["status"], "Active")

        add_debt_event(self.con, did, "Settlement", 500000, "2026-08-23")
        d2 = self.con.execute("SELECT status FROM debts WHERE id=?", (did,)).fetchone()
        self.assertEqual(d2["status"], "Settled")

    def test_7_correction_reversal_single_use(self):
        # 7. Correction/reversal satu kali
        self.con.execute("UPDATE accounts SET current_balance=5000000 WHERE name='BCA Main'")
        self.con.commit()
        res = create_debt_position(self.con, "Rev Test", "Custody", 500000, "2026-08-23", is_cash=True, account="BCA Main")
        ev_id = res["event_id"]
        self.assertEqual(balance_snapshot(self.con)["total_balance"], 5500000.0)

        # Reversal 1
        rev_res = reverse_debt_event(self.con, ev_id)
        self.assertEqual(balance_snapshot(self.con)["total_balance"], 5000000.0)
        self.assertEqual(reconstruct_debt_outstanding(self.con, res["debt_id"]), 0.0)

        # Reversal 2 on same event rejected
        with self.assertRaises(ValueError) as ctx:
            reverse_debt_event(self.con, ev_id)
        self.assertIn("sudah pernah direversal", str(ctx.exception).lower())

    def test_8_duplicate_operation_key_idempotent_across_operations(self):
        # 8. Duplicate operation_key idempotency on create, event, and pay
        op_key1 = "op_create_123"
        res1 = create_debt_position(self.con, "Idemp Test", "Payable", 500000, "2026-08-23", operation_key=op_key1)
        res2 = create_debt_position(self.con, "Idemp Test", "Payable", 500000, "2026-08-23", operation_key=op_key1)
        self.assertTrue(res2.get("idempotent", False))
        self.assertEqual(res1["event_id"], res2["event_id"])

        did = res1["debt_id"]
        op_key2 = "op_event_456"
        ev1 = add_debt_event(self.con, did, "Increase", 100000, "2026-08-23", operation_key=op_key2)
        ev2 = add_debt_event(self.con, did, "Increase", 100000, "2026-08-23", operation_key=op_key2)
        self.assertTrue(ev2.get("idempotent", False))
        self.assertEqual(ev1["event_id"], ev2["event_id"])

        # Operation key on pay_upcoming_atomic
        self.con.execute("UPDATE accounts SET current_balance=2000000 WHERE name='BCA Main'")
        self.con.execute("INSERT INTO upcoming (id, title, amount, status, account, debt_id) VALUES (501, 'Cicilan Idemp', 200000, 'Upcoming', 'BCA Main', ?)", (did,))
        self.con.commit()

        op_key3 = "op_pay_789"
        pay1 = pay_upcoming_atomic(self.con, 501, operation_key=op_key3)
        self.assertEqual(pay1["status"], "success")
        pay2 = pay_upcoming_atomic(self.con, 501, operation_key=op_key3)
        self.assertTrue(pay2.get("idempotent", False) or pay2.get("alreadyPaid", False))

    def test_9_isolation_from_personal_reports_and_budgets(self):
        # 9. Isolasi laporan/budget: transaksi Third-party tidak masuk ke personal expenses/income
        self.con.execute("UPDATE accounts SET current_balance=5000000 WHERE name='BCA Main'")
        self.con.commit()
        exp_before = len(personal_expense_rows(self.con, "2026-08"))
        inc_before = len(personal_income_rows(self.con, "2026-08"))

        create_debt_position(self.con, "Pihak Z", "Custody", 1000000, "2026-08-23", is_cash=True, account="BCA Main")
        create_debt_position(self.con, "Pihak Y", "Receivable", 500000, "2026-08-23", is_cash=True, account="BCA Main")

        exp_after = len(personal_expense_rows(self.con, "2026-08"))
        inc_after = len(personal_income_rows(self.con, "2026-08"))
        self.assertEqual(exp_before, exp_after)
        self.assertEqual(inc_before, inc_after)

    def test_10_relation_validations_and_multi_upcoming(self):
        # 10. Relation validations via link_upcoming_to_debt & unlink
        res_p = create_debt_position(self.con, "Pinjaman Bank", "Payable", 3000000, "2026-08-23")
        did_p = res_p["debt_id"]
        res_c = create_debt_position(self.con, "Titipan Teman", "Custody", 1000000, "2026-08-23")
        did_c = res_c["debt_id"]

        self.con.execute("INSERT INTO upcoming (id, title, amount, status) VALUES (101, 'Cicilan 1', 1000000, 'Upcoming')")
        self.con.execute("INSERT INTO upcoming (id, title, amount, status) VALUES (102, 'Cicilan 2', 1500000, 'Upcoming')")
        self.con.execute("INSERT INTO upcoming (id, title, amount, status) VALUES (103, 'Cicilan Terlalu Besar', 2000000, 'Upcoming')")
        self.con.commit()

        # Reject link to Custody
        with self.assertRaises(ValueError) as ctx:
            link_upcoming_to_debt(self.con, 101, did_c)
        self.assertIn("utang", str(ctx.exception).lower())

        # Reject link with fake ID
        with self.assertRaises(ValueError) as ctx:
            link_upcoming_to_debt(self.con, 9999, did_p)
        self.assertIn("tidak ditemukan", str(ctx.exception).lower())

        # Link upcomings 101 and 102
        link_upcoming_to_debt(self.con, 101, did_p)
        link_upcoming_to_debt(self.con, 102, did_p)
        d = get_debt_detail(self.con, did_p)
        self.assertEqual(len(d["linked_upcomings"]), 2)

        # Reject link exceeding remaining debt (1M + 1.5M + 2M = 4.5M > 3M)
        with self.assertRaises(ValueError) as ctx:
            link_upcoming_to_debt(self.con, 103, did_p)
        self.assertIn("melebihi", str(ctx.exception).lower())

        # Unlink 102 and re-check
        unlink_upcoming_from_debt(self.con, 102)
        d_after = get_debt_detail(self.con, did_p)
        self.assertEqual(len(d_after["linked_upcomings"]), 1)

    def test_11_anti_double_deduction_payable_upcoming_and_protected(self):
        # 11. Anti-dobel-potong Payable–Upcoming–Dana Dijaga
        self.con.execute("UPDATE accounts SET current_balance=8000000, protected=0, protected_amount=0 WHERE name='BCA Main'")
        self.con.execute("UPDATE accounts SET current_balance=2000000, protected=0, protected_amount=1000000 WHERE name='BCA Poket: Tabungan'")

        res = create_debt_position(self.con, "Utang Usaha", "Payable", 2000000, "2026-08-23")
        did = res["debt_id"]

        self.con.execute("INSERT INTO upcoming (id, title, amount, status) VALUES (201, 'Cicilan Pokok 1', 1000000, 'Upcoming')")
        self.con.execute("INSERT INTO upcoming (id, title, amount, status) VALUES (202, 'WiFi Kantor', 500000, 'Upcoming')")
        self.con.commit()

        link_upcoming_to_debt(self.con, 201, did)
        self.con.execute("INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (301, 'Tabungan Cicilan', 1000000, 'BCA Poket: Tabungan', 'Active', 201)")
        self.con.commit()

        u_eff, x_eff = get_payable_effective_commitments(self.con)
        self.assertEqual(u_eff, 1000000.0)  # 2M debt - 1M coverage = 1M
        self.assertEqual(x_eff, 500000.0)   # 500k regular upcoming
        self.assertEqual(current_commitments(self.con), 1500000.0)

        snap = balance_snapshot(self.con)
        sts = snap["total_balance"] - snap["protected_savings"] - total_custody_outstanding(self.con) - current_commitments(self.con)
        self.assertEqual(sts, 7500000.0)

    def test_12_atomic_installment_pay_using_production_service_and_sts_constant(self):
        # 12. Pembayaran cicilan atomik via pay_upcoming_atomic dan STS konstan
        self.con.execute("UPDATE accounts SET current_balance=8000000, protected=0, protected_amount=0 WHERE name='BCA Main'")
        self.con.execute("UPDATE accounts SET current_balance=2000000, protected=0, protected_amount=1000000 WHERE name='BCA Poket: Tabungan'")

        res = create_debt_position(self.con, "Utang Usaha", "Payable", 2000000, "2026-08-23")
        did = res["debt_id"]

        self.con.execute("INSERT INTO upcoming (id, title, amount, status, account) VALUES (201, 'Cicilan Pokok 1', 1000000, 'Upcoming', 'BCA Main')")
        self.con.commit()
        link_upcoming_to_debt(self.con, 201, did)
        self.con.execute("INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (301, 'Tabungan Cicilan', 1000000, 'BCA Poket: Tabungan', 'Active', 201)")
        self.con.commit()

        sts_before = 10000000.0 - 1000000.0 - 0 - (2000000.0 - 1000000.0)  # 8,000,000.0

        # Execute payment via production service
        pay_res = pay_upcoming_atomic(self.con, 201, account="BCA Main", pay_date="2026-08-23")
        self.assertEqual(pay_res["status"], "success")
        tx_id = pay_res["transaction_id"]

        # Verify debt_event links to transaction_id
        ev = self.con.execute("SELECT * FROM debt_events WHERE debt_id=? AND event_type='Settlement'", (did,)).fetchone()
        self.assertIsNotNone(ev)
        self.assertEqual(ev["transaction_id"], tx_id)
        self.assertEqual(ev["amount"], 1000000.0)

        # Verify transaction is Third-party Expense with 0 budget effect
        tx = self.con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        self.assertEqual(tx["transaction_type"], "Expense")
        self.assertEqual(tx["money_context"], "Third-party")
        self.assertEqual(tx["budget_effect"], 0.0)

        # Verify linked protected allocation marked Spent
        pa = self.con.execute("SELECT status FROM protected_allocations WHERE id=301").fetchone()
        self.assertEqual(pa["status"], "Spent")

        # Verify upcoming is Paid
        u = self.con.execute("SELECT status, transaction_id FROM upcoming WHERE id=201").fetchone()
        self.assertEqual(u["status"], "Paid")
        self.assertEqual(u["transaction_id"], tx_id)

        # Verify Live STS constant
        snap_after = balance_snapshot(self.con)
        comm_after = current_commitments(self.con)
        sts_after = snap_after["total_balance"] - snap_after["protected_savings"] - total_custody_outstanding(self.con) - comm_after
        self.assertAlmostEqual(sts_before, sts_after, delta=0.005)

    def test_13_different_payment_and_allocation_account_atomic(self):
        # 13. Beda rekening pembayaran dan alokasi
        self.con.execute("UPDATE accounts SET current_balance=3000000 WHERE name='BCA Main'")
        self.con.execute("UPDATE accounts SET current_balance=2000000, protected=0, protected_amount=1000000 WHERE name='BCA Poket: Tabungan'")
        self.con.commit()

        res = create_debt_position(self.con, "Utang Teman", "Payable", 1000000, "2026-08-23")
        did = res["debt_id"]
        self.con.execute("INSERT INTO upcoming (id, title, amount, status, account) VALUES (301, 'Cicilan Teman', 1000000, 'Upcoming', 'BCA Main')")
        self.con.commit()
        link_upcoming_to_debt(self.con, 301, did)
        self.con.execute("INSERT INTO protected_allocations (id, title, amount, account, status, covers_upcoming_id) VALUES (401, 'Alokasi Poket', 1000000, 'BCA Poket: Tabungan', 'Active', 301)")
        self.con.commit()

        # Bayar dari BCA Main via service produksi
        pay_res = pay_upcoming_atomic(self.con, 301, account="BCA Main", pay_date="2026-08-23")
        self.assertEqual(pay_res["status"], "success")

        snap = balance_snapshot(self.con)
        comm = current_commitments(self.con)
        self.assertEqual(snap["total_balance"], 4000000.0)
        self.assertEqual(snap["protected_savings"], 0.0)
        self.assertEqual(comm, 0.0)

    def test_14_rollback_failure_injection_in_atomic_service(self):
        # 14. Rollback failure injection
        self.con.execute("UPDATE accounts SET current_balance=5000000 WHERE name='BCA Main'")
        self.con.commit()

        try:
            self.con.execute("BEGIN TRANSACTION")
            mutate_account_balance(self.con, "BCA Main", -1000000, "2026-08-23")
            create_debt_position(self.con, "Crash Test", "Payable", 1000000, "2026-08-23")
            raise RuntimeError("Simulated crash")
        except RuntimeError:
            self.con.rollback()

        snap = balance_snapshot(self.con)
        self.assertEqual(snap["total_balance"], 5000000.0)
        self.assertEqual(total_payable_outstanding(self.con), 0.0)

    def test_15_writeoff_and_forgiveness_rules_per_kind(self):
        # 15. WriteOff & Forgiveness per kind
        # Custody write-off strictly rejected
        res_c = create_debt_position(self.con, "Titipan C", "Custody", 500000, "2026-08-23")
        with self.assertRaises(ValueError) as ctx:
            add_debt_event(self.con, res_c["debt_id"], "WriteOff", 500000, "2026-08-23")
        self.assertIn("dilarang", str(ctx.exception).lower())

        # Receivable write-off allowed (non-cash)
        res_r = create_debt_position(self.con, "Piutang Macet", "Receivable", 500000, "2026-08-23")
        add_debt_event(self.con, res_r["debt_id"], "WriteOff", 500000, "2026-08-23")
        self.assertEqual(reconstruct_debt_outstanding(self.con, res_r["debt_id"]), 0.0)
        self.assertEqual(update_debt_status(self.con, res_r["debt_id"]), "WrittenOff")

        # Payable forgiveness (WriteOff) allowed (non-cash)
        res_p = create_debt_position(self.con, "Utang Dimaafkan", "Payable", 750000, "2026-08-23")
        add_debt_event(self.con, res_p["debt_id"], "WriteOff", 750000, "2026-08-23")
        self.assertEqual(reconstruct_debt_outstanding(self.con, res_p["debt_id"]), 0.0)
        self.assertEqual(update_debt_status(self.con, res_p["debt_id"]), "WrittenOff")

    def test_16_legacy_40_transactions_and_prod_db_intact(self):
        # 16. Legacy 40 transaksi dan DB produksi tidak berubah
        prod_con = db_connect("money_tracks.db")
        try:
            pt_count = prod_con.execute("SELECT COUNT(*) FROM transactions WHERE money_context='Pass-through' AND is_deleted=0").fetchone()[0]
            self.assertEqual(pt_count, 40)
        finally:
            prod_con.close()

    def test_17_no_forced_expense_and_ui_invariants(self):
        # 17. Verifikasi transaksi kas titipan = Income & UI source code invariants
        self.con.execute("UPDATE accounts SET current_balance=5000000 WHERE name='BCA Main'")
        self.con.commit()
        res = create_debt_position(self.con, "Ibu Titip", "Custody", 1000000, "2026-08-23", is_cash=True, account="BCA Main")
        tx = self.con.execute("SELECT transaction_type, money_context, budget_effect FROM transactions WHERE description LIKE '%Penerimaan Titipan%'").fetchone()
        self.assertEqual(tx["transaction_type"], "Income")
        self.assertEqual(tx["money_context"], "Third-party")
        self.assertEqual(tx["budget_effect"], 0.0)

        # UI & Codebase checks:
        with open("app.js", "r", encoding="utf-8") as f:
            app_js = f.read()
        # Verify savePassThrough is deprecated / alerts
        self.assertIn("dinonaktifkan", app_js)
        # Verify esc() is used in renderThirdpartyGrid
        self.assertIn("esc(d.person_name)", app_js)

        with open("money_tracks_server.py", "r", encoding="utf-8") as f:
            srv_py = f.read()
        # Verify /api/pass_through is deprecated / disabled
        self.assertIn("dinonaktifkan", srv_py.lower())


if __name__ == "__main__":
    unittest.main()
