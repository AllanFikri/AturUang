"""
review_rules.py — CLI untuk lihat & audit semua rules di registry.

Usage:
    python review_rules.py                    # summary
    python review_rules.py --merchant         # list merchant_rules
    python review_rules.py --va               # list VA prefix
    python review_rules.py --counterparty     # list counterparty
    python review_rules.py --queue            # pending review queue
    python review_rules.py --search KEYWORD   # cari rule berisi keyword
    python review_rules.py --category Nama    # lihat rule dengan kategori tertentu
"""
import sqlite3, argparse, sys
from pathlib import Path

DB = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")


def get_con():
    return sqlite3.connect(str(DB))


def cmd_summary(cur):
    print("=" * 70)
    print("ATURUANG RULES REGISTRY — SUMMARY")
    print("=" * 70)
    print()
    
    n_mr = cur.execute("SELECT COUNT(*) FROM merchant_rules WHERE enabled=1").fetchone()[0]
    n_va = cur.execute("SELECT COUNT(*) FROM va_prefix_rules WHERE enabled=1").fetchone()[0]
    n_cp = cur.execute("SELECT COUNT(*) FROM counterparty_accounts").fetchone()[0]
    n_queue = cur.execute("SELECT COUNT(*) FROM pending_review_queue WHERE status='pending'").fetchone()[0]
    n_log = cur.execute("SELECT COUNT(*) FROM rule_apply_log").fetchone()[0]
    
    print(f"  merchant_rules        : {n_mr:>5} rules")
    print(f"  va_prefix_rules       : {n_va:>5} rules")
    print(f"  counterparty_accounts : {n_cp:>5} rules")
    print(f"  " + "-" * 40)
    print(f"  TOTAL                 : {n_mr + n_va + n_cp:>5} rules")
    print()
    print(f"  pending_review_queue  : {n_queue:>5} pending")
    print(f"  rule_apply_log        : {n_log:>5} log entries")
    print()
    
    print("=== Top 15 kategori dari merchant_rules ===")
    for cat, n in cur.execute("""
        SELECT category, COUNT(*) FROM merchant_rules WHERE enabled=1
        GROUP BY category ORDER BY COUNT(*) DESC LIMIT 15
    """):
        print(f"  {cat:<35} {n:>4} rules")


def cmd_merchant(cur):
    print(f"{'ID':>5} {'Prio':>5} {'Pattern':<38} {'Category':<28} {'Type':<10} {'Excl'}")
    print("-" * 100)
    for rid, prio, pat, cat, ttype, excl in cur.execute("""
        SELECT id, priority, pattern, category, transaction_type, exclude_from_budget
        FROM merchant_rules WHERE enabled=1 ORDER BY priority, id
    """):
        print(f"{rid:>5} {prio:>5} {pat[:36]:<38} {cat[:26]:<28} {str(ttype):<10} {excl}")


def cmd_va(cur):
    print(f"{'ID':>5} {'Bank':<6} {'VA':<10} {'Merchant':<20} {'Category':<28} {'Type':<10} {'Excl'}")
    print("-" * 110)
    for rid, bank, va, name, cat, ttype, excl in cur.execute("""
        SELECT id, bank, va_prefix, merchant_name, category, transaction_type, exclude_from_budget
        FROM va_prefix_rules WHERE enabled=1 ORDER BY va
    """):
        print(f"{rid:>5} {bank:<6} {va:<10} {str(name)[:18]:<20} {cat[:26]:<28} {str(ttype):<10} {excl}")


def cmd_counterparty(cur):
    print(f"{'ID':>5} {'Bank':<8} {'Account':<20} {'Owner':<38} {'Rel':<12} {'Internal'}")
    print("-" * 110)
    for rid, bank, acct, owner, rel, internal in cur.execute("""
        SELECT id, bank, account_number, owner_name, relation, is_internal
        FROM counterparty_accounts ORDER BY relation, id
    """):
        print(f"{rid:>5} {bank:<8} {acct[:18]:<20} {owner[:36]:<38} {rel:<12} {internal}")


def cmd_queue(cur):
    print("=== PENDING REVIEW QUEUE ===")
    print()
    rows = cur.execute("""
        SELECT q.id, q.transaction_id, t.date, t.description, t.amount,
               q.suggestion_category, q.suggestion_confidence, q.status
        FROM pending_review_queue q
        LEFT JOIN transactions t ON t.id = q.transaction_id
        WHERE q.status = 'pending'
        ORDER BY q.created_at DESC
    """).fetchall()
    
    if not rows:
        print("  (kosong — tidak ada tx yang butuh review)")
        return
    
    for qid, tid, date, desc, amt, sug_cat, sug_conf, status in rows:
        print(f"  Queue #{qid} — tx id={tid} {date}")
        print(f"    Desc : {str(desc)[:80]}")
        print(f"    Amount: Rp{float(amt) if amt else 0:,.0f}")
        print(f"    Saran : {sug_cat} ({sug_conf})")
        print()


def cmd_search(cur, keyword):
    print(f"=== Search keyword: '{keyword}' ===")
    print()
    
    print("### merchant_rules:")
    for rid, prio, pat, cat, ttype, excl in cur.execute("""
        SELECT id, priority, pattern, category, transaction_type, exclude_from_budget
        FROM merchant_rules WHERE enabled=1 AND pattern LIKE ?
        ORDER BY priority
    """, (f"%{keyword}%",)):
        print(f"  id={rid:>4} pr={prio:>4} '{pat}' -> {cat} [{ttype}] excl={excl}")
    
    print()
    print("### va_prefix_rules:")
    for rid, va, name, cat, ttype, excl in cur.execute("""
        SELECT id, va_prefix, merchant_name, category, transaction_type, exclude_from_budget
        FROM va_prefix_rules WHERE enabled=1 AND (va_prefix LIKE ? OR merchant_name LIKE ?)
    """, (f"%{keyword}%", f"%{keyword}%")):
        print(f"  id={rid:>4} VA {va} ({name}) -> {cat} [{ttype}] excl={excl}")
    
    print()
    print("### counterparty_accounts:")
    for rid, acct, owner, rel in cur.execute("""
        SELECT id, account_number, owner_name, relation
        FROM counterparty_accounts
        WHERE account_number LIKE ? OR owner_name LIKE ?
    """, (f"%{keyword}%", f"%{keyword}%")):
        print(f"  id={rid:>4} {acct} ({owner}) [{rel}]")


def cmd_category(cur, cat_name):
    print(f"=== Rules dengan kategori mengandung '{cat_name}' ===")
    print()
    for rid, prio, pat, cat, ttype, excl in cur.execute("""
        SELECT id, priority, pattern, category, transaction_type, exclude_from_budget
        FROM merchant_rules WHERE enabled=1 AND category LIKE ?
        ORDER BY priority, id
    """, (f"%{cat_name}%",)):
        print(f"  id={rid:>4} pr={prio:>4} '{pat}' [{ttype}] excl={excl}")


def main():
    parser = argparse.ArgumentParser(description="Review AturUang rules registry")
    parser.add_argument("--merchant", action="store_true", help="List merchant_rules")
    parser.add_argument("--va", action="store_true", help="List VA prefix rules")
    parser.add_argument("--counterparty", action="store_true", help="List counterparty accounts")
    parser.add_argument("--queue", action="store_true", help="Show pending review queue")
    parser.add_argument("--search", type=str, help="Search keyword di semua rules")
    parser.add_argument("--category", type=str, help="Show rules dengan kategori tertentu")
    args = parser.parse_args()
    
    con = get_con()
    cur = con.cursor()
    
    if args.merchant:
        cmd_merchant(cur)
    elif args.va:
        cmd_va(cur)
    elif args.counterparty:
        cmd_counterparty(cur)
    elif args.queue:
        cmd_queue(cur)
    elif args.search:
        cmd_search(cur, args.search)
    elif args.category:
        cmd_category(cur, args.category)
    else:
        cmd_summary(cur)
    
    con.close()


if __name__ == "__main__":
    main()
