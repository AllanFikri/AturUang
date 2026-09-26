import sqlite3
import datetime as dt

con = sqlite3.connect("runtime/money_tracks.db")
cur = con.cursor()

now = dt.datetime.now().isoformat(timespec='seconds')

# Bulan yang akan direkonsiliasi (BCA statement ada dari Jan 2025 - Jul 2026)
MONTHS = [
    "2025-02", "2025-03", "2025-04", "2025-05", "2025-06", "2025-07",
    "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
    "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
]

print(f"Batch reconciliation untuk {len(MONTHS)} bulan")
print()

grand_activated = 0
grand_demoted = 0

for month in MONTHS:
    # STEP A: Aktifkan BCA statement sebagai canonical
    cur.execute("""
        UPDATE transactions
        SET exclude_from_budget = 0,
            budget_effect = amount,
            budget_exclusion_reason = '',
            notes = COALESCE(notes, '') || ' [V3-STEP-103: activated as canonical for ' || ? || ']',
            updated_at = ?
        WHERE substr(date,1,7) = ?
          AND source_refs LIKE 'bca_statement%'
          AND is_deleted = 0
    """, (month, now, month))
    activated = cur.rowcount

    # STEP B: Demote Gmail existing (bukan BCA statement)
    cur.execute("""
        UPDATE transactions
        SET exclude_from_budget = 1,
            budget_effect = 0,
            budget_exclusion_reason = 'Superseded by BCA statement ' || ? || ' (V3-STEP-103)',
            notes = COALESCE(notes, '') || ' [V3-STEP-103: demoted to evidence for ' || ? || ']',
            updated_at = ?
        WHERE substr(date,1,7) = ?
          AND is_deleted = 0
          AND exclude_from_budget = 0
          AND (source_refs NOT LIKE 'bca_statement%' OR source_refs IS NULL OR source_refs = '')
          AND canonical_id IS NOT NULL
    """, (month, month, now, month))
    demoted = cur.rowcount

    grand_activated += activated
    grand_demoted += demoted

    print(f"  {month}: activated={activated:<4} demoted={demoted:<4}")

con.commit()

print()
print(f"TOTAL: activated {grand_activated} row, demoted {grand_demoted} row")

print()
print("=" * 70)
print("=== VERIFIKASI SETELAH BATCH ===")
print("=" * 70)
print()

# Per bulan income/expense
print(f"{'Bulan':<10} {'Income (canonical)':>20} {'Expense (canonical)':>20}")
print("-" * 55)

for m in MONTHS:
    inc = cur.execute("""
        SELECT COALESCE(SUM(amount),0) FROM transactions
        WHERE substr(date,1,7)=? AND is_deleted=0 AND exclude_from_budget=0
          AND transaction_type='Income' AND money_context='Personal'
    """, (m,)).fetchone()[0]
    exp = cur.execute("""
        SELECT COALESCE(SUM(budget_effect),0) FROM transactions
        WHERE substr(date,1,7)=? AND is_deleted=0 AND exclude_from_budget=0
          AND transaction_type='Expense' AND money_context='Personal'
    """, (m,)).fetchone()[0]
    print(f"{m:<10} Rp{inc:>18,.2f} Rp{exp:>18,.2f}")

# Jan 2025 tetap sama?
inc_jan = cur.execute("""
    SELECT COALESCE(SUM(amount),0) FROM transactions
    WHERE substr(date,1,7)='2025-01' AND is_deleted=0 AND exclude_from_budget=0
      AND transaction_type='Income' AND money_context='Personal'
""").fetchone()[0]
exp_jan = cur.execute("""
    SELECT COALESCE(SUM(budget_effect),0) FROM transactions
    WHERE substr(date,1,7)='2025-01' AND is_deleted=0 AND exclude_from_budget=0
      AND transaction_type='Expense' AND money_context='Personal'
""").fetchone()[0]
print()
print(f"Jan 2025 (control): income=Rp{inc_jan:,.2f}  expense=Rp{exp_jan:,.2f}")
print(f"Expected:           income=Rp4,580,747.00  expense=Rp4,584,044.00")

print()
print("INTEGRITY:", cur.execute("PRAGMA integrity_check").fetchone()[0])

con.close()
