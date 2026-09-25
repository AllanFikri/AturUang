# ATR Test Rules

## Database
- Tests MUST use in-memory or OS-temp DB.
- NEVER point a test at production DB.
- Missing production DB = FATAL, not PASS.

## Fixtures
- Visibly synthetic (synthetic-expense-1, not realistic numbers).
- No real account numbers, balances, merchant names.

## Coverage
- Tests MUST invoke production code, not copies.
- Adversarial / fail-closed test required per stage.
- No vacuous tests.

## Hash
- Record prod DB hash before and after every stage.
- Both values MUST appear in final report.


## Full Suite Rule (added 2026-09-25 after incident)
- NEVER run `python -m unittest discover tests` or `scripts/run_smoke_tests.py` without first verifying every test uses isolated temp DB.
- Before running full suite: grep for sqlite3.connect in tests/*.py.
- Any test that connects to 'runtime/money_tracks.db' without mode=ro or without temp path is FORBIDDEN and must be fixed.
- Known offending test as of 2026-09-25: tests/test_edge_sync_automation_v1.py (inserts man_001 into production DB).
- Full suite command allowed only after that test is patched to use temp DB.