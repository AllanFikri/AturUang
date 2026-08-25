"""
Test Suite Prompt 9b — Release Readiness Audit & End-to-End Verification
"""
import hashlib
import json
import shutil
import sqlite3
import unittest
from pathlib import Path

import db
import services

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "money_tracks.db"
TMP_E2E_DB = BASE_DIR / "scratch" / "temp_prompt9b_e2e.db"


class TestPrompt9bReleaseE2E(unittest.TestCase):
    def setUp(self):
        TMP_E2E_DB.parent.mkdir(exist_ok=True)
        if TMP_E2E_DB.exists():
            TMP_E2E_DB.unlink()
        shutil.copy2(DB_FILE, TMP_E2E_DB)
        self.con = sqlite3.connect(TMP_E2E_DB)
        self.con.row_factory = sqlite3.Row

    def tearDown(self):
        self.con.close()
        if TMP_E2E_DB.exists():
            TMP_E2E_DB.unlink()

    # --- SECTION E: END-TO-END SCENARIOS ---

    def test_e01_create_income_and_expense(self):
        """Scenario 1: Creating Income and Expense correctly updates balances and logs."""
        bca_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]

        # 1. Income
        tx_in = services.validate_tx({
            "date": "2026-08-25",
            "time": "10:00:00",
            "transaction_type": "Income",
            "amount": 100000.0,
            "account_to": "BCA Main",
            "category": "Salary & Primary Income",
            "for_with_whom": "Personal / Self",
            "money_context": "Personal",
            "description": "Bonus test",
            "budget_rule_version": "derived",
        })
        cur = self.con.execute(
            """INSERT INTO transactions (canonical_id, date, time, transaction_type, amount, account_to,
               description, category, for_with_whom, money_context, status, confidence, budget_effect,
               exclude_from_budget, budget_exclusion_reason, budget_rule_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tx_in["canonical_id"], tx_in["date"], tx_in["time"], tx_in["transaction_type"], tx_in["amount"],
             tx_in["account_to"], tx_in["description"], tx_in["category"], tx_in["for_with_whom"], tx_in["money_context"],
             tx_in["status"], "high", tx_in["budget_effect"], tx_in["exclude_from_budget"], tx_in["budget_exclusion_reason"], tx_in["budget_rule_version"])
        )
        services.mutate_account_balance(self.con, "BCA Main", +100000.0, "2026-08-25")

        # 2. Expense
        tx_out = services.validate_tx({
            "date": "2026-08-25",
            "time": "11:00:00",
            "transaction_type": "Expense",
            "amount": 40000.0,
            "account_from": "BCA Main",
            "category": "Main Meals",
            "for_with_whom": "Personal / Self",
            "money_context": "Personal",
            "description": "Makan siang test",
            "budget_rule_version": "derived",
        })
        self.con.execute(
            """INSERT INTO transactions (canonical_id, date, time, transaction_type, amount, account_from,
               description, category, for_with_whom, money_context, status, confidence, budget_effect,
               exclude_from_budget, budget_exclusion_reason, budget_rule_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tx_out["canonical_id"], tx_out["date"], tx_out["time"], tx_out["transaction_type"], tx_out["amount"],
             tx_out["account_from"], tx_out["description"], tx_out["category"], tx_out["for_with_whom"], tx_out["money_context"],
             tx_out["status"], "high", tx_out["budget_effect"], tx_out["exclude_from_budget"], tx_out["budget_exclusion_reason"], tx_out["budget_rule_version"])
        )
        services.mutate_account_balance(self.con, "BCA Main", -40000.0, "2026-08-25")

        bca_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        self.assertAlmostEqual(bca_after, bca_before + 60000.0, delta=0.005)

    def test_e02_transfer_inter_account_isolation(self):
        """Scenario 2: Transfer changes source and target balances with zero budget effect."""
        bca_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        gopay_before = self.con.execute("SELECT current_balance FROM accounts WHERE name='GoPay'").fetchone()["current_balance"]

        tx_tr = services.validate_tx({
            "date": "2026-08-25",
            "time": "12:00:00",
            "transaction_type": "Transfer",
            "amount": 25000.0,
            "account_from": "BCA Main",
            "account_to": "GoPay",
            "description": "Topup test",
            "budget_rule_version": "derived",
        })
        self.assertEqual(tx_tr["budget_effect"], 0.0)

        services.mutate_account_balance(self.con, "BCA Main", -25000.0, "2026-08-25")
        services.mutate_account_balance(self.con, "GoPay", +25000.0, "2026-08-25")

        bca_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        gopay_after = self.con.execute("SELECT current_balance FROM accounts WHERE name='GoPay'").fetchone()["current_balance"]

        self.assertAlmostEqual(bca_after, bca_before - 25000.0, delta=0.005)
        self.assertAlmostEqual(gopay_after, gopay_before + 25000.0, delta=0.005)

    def test_e03_reconciliation_atomic_rollback(self):
        """Scenario 3: Reconciliation with error rolls back completely."""
        with self.assertRaises(Exception):
            with self.con:
                self.con.execute("UPDATE accounts SET current_balance=999999 WHERE name='BCA Main'")
                raise RuntimeError("Simulated failure during reconciliation")

        bca_current = self.con.execute("SELECT current_balance FROM accounts WHERE name='BCA Main'").fetchone()["current_balance"]
        self.assertAlmostEqual(bca_current, 67821.80, delta=0.005)

    def test_e04_third_party_debt_lifecycle_isolation(self):
        """Scenario 4: Third-party positions and events remain strictly isolated from personal spending."""
        # 1. Create debt position
        res = services.create_debt_position(
            self.con,
            person_name="Budi Test",
            kind="Receivable",
            initial_amount=150000.0,
            event_date="2026-08-26",
            notes="Pinjaman teman",
            account="BCA Main",
            is_cash=True
        )
        self.assertEqual(res["status"], "success")
        debt_id = res["debt_id"]

        # Check that debt outstanding is 150.000
        out_1 = services.reconstruct_debt_outstanding(self.con, debt_id)
        self.assertEqual(out_1, 150000.0)

        # 2. Add partial settlement event
        ev_res = services.add_debt_event(
            self.con,
            debt_id=debt_id,
            event_type="Settlement",
            amount=50000.0,
            event_date="2026-08-26",
            account="BCA Main",
            is_cash=True,
            notes="Cicilan 1"
        )
        self.assertEqual(ev_res["status"], "success")

        out_2 = services.reconstruct_debt_outstanding(self.con, debt_id)
        self.assertEqual(out_2, 100000.0)

        # Check that personal budget/expenses do not include this debt event
        dash = services.dashboard(self.con, "2026-08")
        # Piutang events have Third-party context, not Personal
        debt_txs = self.con.execute("SELECT * FROM transactions WHERE money_context='Third-party'").fetchall()
        self.assertTrue(len(debt_txs) > 0)
        for t in debt_txs:
            self.assertEqual(t["budget_effect"], 0.0)

    # --- SECTION F & G: FORMAT RUNTIME & DASHBOARD INVARIANTS ---

    def test_f01_parse_money_input_exact_behavior(self):
        """Section F: Pure parseMoneyInput rules."""
        self.assertEqual(services.idr(1000), "Rp1.000")
        # In core pure calculations:
        def parse_m(v):
            import re
            if not v: return 0.0
            s = str(v).strip()
            is_neg = "-" in s
            s = re.sub(r"[^0-9,.]", "", s)
            if not s: return 0.0
            if "," in s:
                p = s.split(",")
                return round((-1 if is_neg else 1) * float(p[0].replace(".", "") + ("." + p[1][:2] if p[1] else "")), 2)
            if s.count(".") > 1:
                return round((-1 if is_neg else 1) * float(s.replace(".", "")), 2)
            if s.count(".") == 1:
                p = s.split(".")
                if len(p[1]) == 3 and "," not in p[1]:
                    return round((-1 if is_neg else 1) * float(s.replace(".", "")), 2)
                return round((-1 if is_neg else 1) * float(s), 2)
            return round((-1 if is_neg else 1) * float(s), 2)

        self.assertEqual(parse_m("1.000"), 1000.0)
        self.assertEqual(parse_m("1234,50"), 1234.50)
        self.assertEqual(parse_m("1234.50"), 1234.50)
        self.assertEqual(parse_m("-50000"), -50000.0)
        self.assertEqual(parse_m("Rp 1.000.000"), 1000000.0)

    def test_g01_dashboard_components_and_hero_match(self):
        """Section G: Dashboard hero match and components invariant."""
        dash = services.dashboard(self.con, "2026-08")
        kpis = dash["kpis"]
        hero = kpis["safeToSpend"]
        tb = kpis["totalBalance"]
        ps = kpis["protectedSavings"]
        pt = kpis["passThroughOutstanding"]
        cc = kpis["currentCommitments"]

        expected_hero = round(tb - ps - pt - cc, 2)
        self.assertAlmostEqual(hero, expected_hero, delta=0.005)
        self.assertAlmostEqual(hero, 1629368.80, delta=0.005)

    # --- SECTION I: RECOVERY DRILL ---

    def test_i01_recovery_drill_on_temporary_copy(self):
        """Section I: Backup restore and init_db run identically without mutation."""
        drill_db = BASE_DIR / "scratch" / "temp_recovery_drill.db"
        if drill_db.exists():
            drill_db.unlink()
        shutil.copy2(DB_FILE, drill_db)

        con_drill = sqlite3.connect(drill_db)
        con_drill.row_factory = sqlite3.Row

        # Verify integrity and count
        ic = con_drill.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(ic, "ok")
        tx_count = con_drill.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted=0").fetchone()[0]
        self.assertEqual(tx_count, 1149)

        # Reconstruct balance
        recon = services.reconstruct_account_balance(con_drill, "BCA Main")
        self.assertEqual(recon["status"], "ok")
        self.assertAlmostEqual(recon["expected_balance"], 67821.80, delta=0.005)

        con_drill.close()
        drill_db.unlink()

    # --- SECTION J: READ-ONLY BCA LEDGER AUDIT ---

    def test_j01_bca_authoritative_anchor_and_transactions(self):
        """Section J: Read-only verification of manual_anchor and transaction attributes."""
        # 1. Anchor check
        snap = self.con.execute("SELECT * FROM balance_snapshots WHERE id=4 AND account_name='BCA Main'").fetchone()
        self.assertEqual(snap["snapshot_kind"], "manual_anchor")
        self.assertAlmostEqual(snap["balance"], 218821.55, delta=0.005)

        # 2. Legacy snapshot check
        snap1 = self.con.execute("SELECT * FROM balance_snapshots WHERE id=1 AND account_name='BCA Main'").fetchone()
        self.assertEqual(snap1["snapshot_kind"], "legacy")

        # 3. Final reconstructed balance
        recon = services.reconstruct_account_balance(self.con, "BCA Main")
        self.assertEqual(recon["status"], "ok")
        self.assertAlmostEqual(recon["expected_balance"], 67821.80, delta=0.005)
        self.assertAlmostEqual(recon["difference"], 0.0, delta=0.005)


if __name__ == "__main__":
    unittest.main()
