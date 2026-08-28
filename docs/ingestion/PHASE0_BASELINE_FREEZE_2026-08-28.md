# AturUang — Phase 0 Baseline / Inventory Freeze

Date: 2026-08-28
Status: **PASS**

## Verified Git baseline
- Main workspace: `C:\A User Main Storage\Documents\GitHub\AturUang`
- Branch: `main`
- HEAD: `e69deffd9193024ad8d862ab9b639f6b53e5553f`
- `main == origin/main`
- Main working tree: CLEAN

## Universal ingestion worktree
- Worktree: `C:\A User Main Storage\Documents\GitHub\AturUang-ingestion-v1`
- Branch: `feature/universal-ingestion-v1`
- Initial HEAD: `e69deffd9193024ad8d862ab9b639f6b53e5553f`
- Initial working tree: CLEAN

## Production SQLite safety baseline
SHA-256:

`2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f`

The quick regression passed and the SQLite file remained byte-identical before and after validation.

## D1 shadow baseline
Worker configuration:
- `MODE = "shadow"`
- binding: `DB`
- database: `aturuang-db`
- database_id: `9320692c-3bfa-4782-9aec-23bde74873ac`

Read-only verified row counts:
- `raw_events = 2078`
- `ingestion_candidates = 1753`
- `canonical_event_evidence = 2075`
- `canonical_financial_events = 2066`

The verification queries reported `changed_db=false` and `rows_written=0`.

### Validation correction note
The first Phase 0 script attempted a multiline UNION query through Wrangler `--command`.
Only the first SELECT was actually executed in that invocation, producing a misleading count of `1`.
The baseline was therefore rechecked with four separate read-only `COUNT(*)` statements.
Those results matched the previously verified D1 baseline above.

## Phase 0 gate
- [x] main branch verified
- [x] main working tree clean
- [x] origin synchronization verified
- [x] production SQLite hash verified
- [x] quick regression passed
- [x] D1 shadow baseline verified read-only
- [x] isolated universal-ingestion worktree created
- [x] Phase -1 source corpus/template artifacts available
- [x] no deploy
- [x] no D1 write
- [x] no production SQLite mutation
- [x] no Pattern merge

## Next gate before provider parsers
1. Freeze these Phase -1/Phase 0 documents into the ingestion worktree.
2. Freeze source coverage matrix.
3. Benchmark and explicitly choose the minimal PDF extraction dependency against the private corpus.
4. Create an ignored private-corpus replay location/harness.
5. Begin Phase 1 — Registries + Contracts.
6. Do not begin provider adapters until Phase 1 and Phase 2 gates pass.
