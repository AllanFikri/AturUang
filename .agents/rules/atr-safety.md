# ATR Safety Rules

## Production Database
- Path: C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db
- Expected SHA-256: 8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421
- MUST hash before AND after every stage. Values MUST match.
- NEVER read/write/query this DB. Missing file = FATAL.

## Migrations
- NEVER create migration 0008 or higher. D1 chain stops at 0007.
- New migrations require explicit owner approval.

## Git Push
- NEVER push to main or feature/universal-ingestion-v1 directly.
- Push only to review branches.
- NEVER force push.

## Scope
- Touch only files in prompt SCOPE. No silent expansion. Report BLOCKER instead.
