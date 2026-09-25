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
