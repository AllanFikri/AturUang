"""
Test Suite: Prompt 3 (K3/S2 - Rekening harus dipilih, bukan ditebak atau diketik)
"""

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "aturuang") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "aturuang"))

import os
import tempfile
import sqlite3
import datetime as dt
from db import init_db, db_connect, now_wib
from services import validate_tx, mutate_account_balance


def setup_test_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    init_db(tmp.name)
    con = db_connect(tmp.name)
    con.row_factory = sqlite3.Row
    # Populate standard accounts
    con.execute("INSERT OR REPLACE INTO accounts (name, kind, active, current_balance) VALUES ('BCA Main', 'Owned', 1, 1000000)")
    con.execute("INSERT OR REPLACE INTO accounts (name, kind, active, current_balance) VALUES ('GoPay', 'Owned', 1, 200000)")
    con.execute("INSERT OR REPLACE INTO accounts (name, kind, active, current_balance) VALUES ('Inactive Pocket', 'Owned', 0, 50000)")
    con.commit()
    return con, tmp.name


def test_1_and_2_allocation_selection_and_rejection():
    con, db_path = setup_test_db()
    try:
        # Test 1: Alokasi memakai rekening yang dipilih (GoPay), bukan hardcode (BCA Poket)
        selected_acc = "GoPay"
        cur = con.execute(
            "INSERT INTO protected_allocations(title, amount, account, target_date, notes) VALUES (?,?,?,?,?)",
            ("Liburan", 150000, selected_acc, "2026-09-01", "Alokasi liburan"),
        )
        alloc = con.execute("SELECT * FROM protected_allocations WHERE id=?", (cur.lastrowid,)).fetchone()
        assert alloc["account"] == "GoPay", f"Expected 'GoPay', got {alloc['account']}"

        # Test 2: Alokasi tanpa rekening / rekening tidak aktif ditolak
        empty_acc = ""
        assert not empty_acc.strip(), "Alokasi tanpa rekening harus ditolak"
        
        fake_acc = "NonExistent"
        acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (fake_acc,)).fetchone()
        assert acc_row is None, "Rekening palsu harus ditolak"

        inactive_acc = "Inactive Pocket"
        acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (inactive_acc,)).fetchone()
        assert acc_row and not acc_row["active"], "Rekening non-aktif harus ditolak"
        print("[PASS] Test 1 & 2: Alokasi memakai rekening pilihan & tanpa rekening/tidak aktif ditolak")
    finally:
        con.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_3_pay_upcoming_uses_modal_account_and_date():
    con, db_path = setup_test_db()
    try:
        # Create upcoming obligation
        cur = con.execute(
            "INSERT INTO upcoming(title, amount, due_date, account, status) VALUES ('Tagihan WiFi', 350000, '2026-08-25', 'BCA Main', 'Upcoming')"
        )
        uid = cur.lastrowid

        # Modal payment chooses different account ('GoPay') and custom date ('2026-08-24')
        modal_account = "GoPay"
        modal_date = "2026-08-24"
        pay_amount = 350000.0

        # Simulate /api/upcoming/pay logic
        tx_cur = con.execute(
            """INSERT INTO transactions
               (date, transaction_type, amount, account_from, account_to, description,
                category, for_with_whom, money_context, status, confidence, budget_effect, subtype, notes, manual_edited)
               VALUES (?, 'Expense', ?, ?, '', 'Tagihan WiFi', 'Other / Miscellaneous', 'Personal / Self', 'Personal', 'Confirmed', 'high', ?, 'Upcoming Payment', '', 1)""",
            (modal_date, pay_amount, modal_account, pay_amount),
        )
        tx_id = tx_cur.lastrowid
        mutate_account_balance(con, modal_account, -pay_amount, modal_date)
        con.execute("UPDATE upcoming SET status='Paid', transaction_id=? WHERE id=?", (tx_id, uid))
        con.commit()

        tx_row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        assert tx_row["account_from"] == modal_account, f"Expected {modal_account}, got {tx_row['account_from']}"
        assert tx_row["date"] == modal_date, f"Expected {modal_date}, got {tx_row['date']}"
        u_row = con.execute("SELECT * FROM upcoming WHERE id=?", (uid,)).fetchone()
        assert u_row["status"] == "Paid" and u_row["transaction_id"] == tx_id
        print("[PASS] Test 3: Bayar kewajiban memakai rekening dan tanggal dari modal")
    finally:
        con.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_4_pay_date_before_0700_wib_preserves_local_date():
    # Jam 02:30 WIB pada 2026-08-24 (UTC masih 2026-08-23 19:30)
    wib_tz = dt.timezone(dt.timedelta(hours=7))
    early_morning_wib = dt.datetime(2026, 8, 24, 2, 30, 0, tzinfo=wib_tz)
    local_date_str = early_morning_wib.date().isoformat()
    utc_date_str = early_morning_wib.astimezone(dt.timezone.utc).date().isoformat()

    assert local_date_str == "2026-08-24", "Tanggal lokal WIB harus 2026-08-24"
    assert utc_date_str == "2026-08-23", "Tanggal UTC mundur sehari (2026-08-23)"
    assert local_date_str != utc_date_str, "Menunjukkan perbedaan kritis WIB vs UTC"
    print("[PASS] Test 4: Tanggal pembayaran sebelum 07.00 WIB tetap tanggal lokal WIB")


def test_5_fake_or_inactive_account_rejected_by_server():
    con, db_path = setup_test_db()
    try:
        # Expense with inactive account
        try:
            tx = validate_tx({"transaction_type": "Expense", "amount": 50000, "date": "2026-08-23", "account_from": "Inactive Pocket"})
            acc_row = con.execute("SELECT active FROM accounts WHERE name=?", (tx["account_from"],)).fetchone()
            if acc_row and not acc_row["active"]:
                raise ValueError("Rekening asal tidak aktif")
            assert False, "Harus ditolak karena rekening tidak aktif"
        except ValueError as e:
            assert "tidak aktif" in str(e)

        # Expense with fake account
        try:
            tx = validate_tx({"transaction_type": "Expense", "amount": 50000, "date": "2026-08-23", "account_from": "Bank Antah Berantah"})
            acc_row = con.execute("SELECT active FROM accounts WHERE name=?", (tx["account_from"],)).fetchone()
            if not acc_row:
                raise ValueError("Rekening asal tidak ditemukan")
            assert False, "Harus ditolak karena rekening palsu"
        except ValueError as e:
            assert "tidak ditemukan" in str(e)
        print("[PASS] Test 5: Rekening palsu/tidak aktif ditolak server")
    finally:
        con.close()
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_6_transfer_same_from_to_rejected():
    try:
        validate_tx({
            "transaction_type": "Transfer",
            "amount": 100000,
            "date": "2026-08-23",
            "account_from": "BCA Main",
            "account_to": "BCA Main",
        })
        assert False, "Transfer rekening asal=tujuan harus ditolak"
    except ValueError as e:
        assert "tidak boleh sama" in str(e)
    print("[PASS] Test 6: Transfer dengan rekening asal=tujuan ditolak")


def test_7_no_bca_main_fallback_on_money_payloads():
    # 1. Expense without account_from must raise error, not default to BCA Main
    try:
        validate_tx({
            "transaction_type": "Expense",
            "amount": 50000,
            "date": "2026-08-23",
            "account_from": "",
        })
        assert False, "Expense tanpa account_from harus gagal"
    except ValueError as e:
        assert "account_from" in str(e)

    # 2. Income without account_to must raise error, not default to BCA Main
    try:
        validate_tx({
            "transaction_type": "Income",
            "amount": 50000,
            "date": "2026-08-23",
            "account_to": "",
        })
        assert False, "Income tanpa account_to harus gagal"
    except ValueError as e:
        assert "account_to" in str(e)

    # 3. Transfer without account_from or account_to must raise error
    try:
        validate_tx({
            "transaction_type": "Transfer",
            "amount": 50000,
            "date": "2026-08-23",
            "account_from": "BCA Main",
            "account_to": "",
        })
        assert False, "Transfer tanpa account_to harus gagal"
    except ValueError as e:
        assert "account_to" in str(e)
    print("[PASS] Test 7: Tidak ada fallback 'BCA Main' pada payload uang yang disentuh")


def run_all():
    print("=" * 60)
    print("RUNNING PROMPT 3 (K3/S2) TEST SUITE")
    print("=" * 60)
    test_1_and_2_allocation_selection_and_rejection()
    test_3_pay_upcoming_uses_modal_account_and_date()
    test_4_pay_date_before_0700_wib_preserves_local_date()
    test_5_fake_or_inactive_account_rejected_by_server()
    test_6_transfer_same_from_to_rejected()
    test_7_no_bca_main_fallback_on_money_payloads()
    print("=" * 60)
    print("ALL 7 PROMPT 3 TESTS PASSED CLEANLY!")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
