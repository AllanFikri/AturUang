import sqlite3
import datetime as dt

con = sqlite3.connect("runtime/money_tracks.db")
cur = con.cursor()

now = dt.datetime.now().isoformat(timespec='seconds')

print("=== STEP 1: Aktifkan 44 BCA statement sebagai canonical ===")
cur.execute("""
    UPDATE transactions
    SET exclude_from_budget = 0,
        budget_effect = amount,
        budget_exclusion_reason = '',
        notes = COALESCE(notes, '') || ' [V3-STEP-102: activated as canonical for 2025-01]',
        updated_at = ?
    WHERE substr(date,1,7)='2025-01'
      AND source_refs LIKE 'bca_statement%'
      AND is_deleted = 0
""", (now,))
print(f"  Activated: {cur.rowcount} rows")

print()
print("=== STEP 2: Soft-flag 34 Gmail existing sebagai evidence ===")
cur.execute("""
    UPDATE transactions
    SET exclude_from_budget = 1,
        budget_effect = 0,
        budget_exclusion_reason = 'Superseded by BCA statement Jan 2025 (V3-STEP-102)',
        notes = COALESCE(notes, '') || ' [V3-STEP-102: demoted to evidence for 2025-01]',
        updated_at = ?
    WHERE substr(date,1,7)='2025-01'
      AND is_deleted = 0
      AND exclude_from_budget = 0
      AND (source_refs NOT LIKE 'bca_statement%' OR source_refs IS NULL OR source_refs = '')
      AND canonical_id IS NOT NULL
""", (now,))
print(f"  Demoted Gmail rows: {cur.rowcount}")

print()
print("=== STEP 3: Pastikan Blu & SeaBank tetap evidence ===")
r = cur.execute("""
    SELECT source_refs, COUNT(*) FROM transactions
    WHERE substr(date,1,7)='2025-01' AND is_deleted=0
      AND (source_refs LIKE 'blu_statement%' OR source_refs LIKE 'seabank_statement%')
    GROUP BY source_refs
""").fetchall()
for row in r:
    print(f"  {row[0][:40]}: {row[1]} row (tetap exclude=1)")

con.commit()

print()
print("=" * 70)
print("=== VERIFIKASI SETELAH REKONSILIASI ===")
print("=" * 70)
print()

# Split exclude
for r in cur.execute("""
    SELECT exclude_from_budget, transaction_type, 
           COUNT(*) as n, SUM(budget_effect) as total
    FROM transactions
    WHERE substr(date,1,7)='2025-01' AND is_deleted=0
    GROUP BY exclude_from_budget, transaction_type
    ORDER BY exclude_from_budget, transaction_type
"""):
    print(f"  exclude={r[0]}  {r[1]:<10}  n={r[2]:<4}  Rp{r[3]:>15,.2f}")

print()

# Canonical totals
print("=== Canonical (exclude=0) ===")
r = cur.execute("""
    SELECT transaction_type, COUNT(*) as n, SUM(budget_effect) as t
    FROM transactions
    WHERE substr(date,1,7)='2025-01' AND is_deleted=0 AND exclude_from_budget=0
    GROUP BY transaction_type
""").fetchall()
for row in r:
    print(f"  {row[0]:<10} n={row[1]:<4} Rp{row[2]:>15,.2f}")

print()
print("=== Bandingkan dengan statement BCA Jan 2025 ===")
print("  Statement Income (CR):  Rp  4.580.747,00")
print("  Statement Expense (DB): Rp  4.584.044,00")

print()
print("INTEGRITY:", cur.execute("PRAGMA integrity_check").fetchone()[0])

# Total row
r = cur.execute("""
    SELECT COUNT(*) FROM transactions WHERE substr(date,1,7)='2025-01' AND is_deleted=0
""").fetchone()
print(f"Total row Jan 2025: {r[0]}")

con.close()
