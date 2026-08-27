# AturUang Current Handoff

Last updated: 2026-08-27

Read this file first when continuing the project in another ChatGPT/GPT
conversation.

Then read:
1. ../../AGENTS.md
2. WORKING_RULES.md
3. DECISIONS.md
4. the latest entries in WORKLOG.md
5. only the prompt/spec files relevant to the next task

Repository/database/deployment evidence overrides this document if there is
a conflict. Never guess when state can be verified.

## Project identity

Project:
Money Tracks / AturUang

Canonical private repository:
AllanFikri/AturUang

Canonical Windows workspace:
C:\A User Main Storage\Documents\GitHub\AturUang

Do not use an old Downloads copy as the active workspace.

## Stack

Local application:
- Python backend
- Vanilla JavaScript
- HTML/CSS
- SQLite

Cloud subtree:
- TypeScript
- npm
- Cloudflare Worker
- Cloudflare D1

Do not rebuild the product into React/Vue or another stack without an
explicit project-level decision.

## Current Git state

Implementation HEAD before final-history docs update:
88d6015

Remote main at final historical audit:
88d6015

Working tree at final historical audit:
clean

Relevant latest implementation commits:
- 88d6015 chore(gmail): expose canonical failure stage
- 8489d75 fix(gmail): add canonical correlation fallbacks
- 1c0e9c2 docs(project): record backfill observability
- ced919f feat(gmail): add lightweight backfill progress logs
- ecc570a fix(gmail): make canonical ingestion retry-safe

Pattern Analysis is intentionally separate:
- worktree: C:\A User Main Storage\Documents\GitHub\AturUang-pattern-v1
- branch: feature/pattern-analysis-v1
- reported HEAD: 11fc5043746d54f3ac31fffbdeef6eed47ebeabf
- not merged to main
- not deployed
- not production-wired

## Current cloud state

Worker:
aturuang-api

Worker URL:
https://aturuang-api.allanfikrimahardika.workers.dev

Last verified deployed Worker version:
8dd6465b-0432-4414-8626-7b335be9f101

MODE:
shadow

D1:
aturuang-db

D1 schema:
7

Final health verification:
- status: ok
- mode: shadow
- schema_version: 7

Important:
D1 remains shadow/intelligence storage.
It is not the production financial source of truth.
No financial-authority cutover has occurred.

## Current financial source of truth

SQLite remains source of truth.

Production SQLite:
runtime/money_tracks.db

Current verified SHA-256:
2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

Ledger transactions:
1150

Do not mutate production SQLite from tests or Gmail historical ingestion.

## Post-February D1 state

This section is superseded by the final historical verification below.

Final verified historical range:
2025-01-01 through 2026-07-31

Final historical totals:
- Gmail raw: 2038
- Gmail evidence: 2038
- Gmail candidates: 1720
- distinct canonical events: 964

Integrity:
- raw without evidence: 0
- evidence without raw: 0
- evidence without canonical: 0
- canonical without evidence: 0
- candidate orphan: 0
- unresolved Gmail Pending/Error raw events: 0
- duplicate raw external IDs: 0
- duplicate canonical event IDs: 0
- duplicate canonical/raw evidence pairs: 0
- non-positive ingestion candidates: 0
- source_sync_state Gmail: OK
- source_sync error_code: empty
- privacy invalid JSON: 0
- forbidden persisted Gmail body/subject/message-id/from fields: 0
- maximum minimal payload length: 138
- unexpected sender count: 0

Production SQLite remained unchanged:
- transactions: 1150
- SHA-256:
  2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

Canonical amount contract:
- ingestion candidates require amount > 0;
- canonical events may legitimately use amount >= 0 for
  review/evidence/lifecycle/non-transaction facts;
- such zero canonical rows are not automatically eligible economic
  observations.

## Gmail historical backfill state

HISTORY VERIFIED = YES

Historical Backfill V2 completed through:
2026-07

Authoritative next checkpoint:
2026-08

Verified closed historical coverage:
2025-01-01 through 2026-07-31

Every month January 2025 through July 2026 contains Gmail evidence.

Final raw/evidence coverage:
2038 / 2038

Do not reset or rerun Historical Backfill V2 merely to ingest the active
August 2026 month.

August 2026 is current-month ingestion and must be handled separately.

HISTORY VERIFIED means the historical Gmail coverage, persistence,
idempotency, privacy, source coverage, and production-isolation gates passed.

It does not mean:
- August 2026 current-month coverage is complete;
- Pending Review rows are all economically resolved;
- Pattern false-positive audit is complete;
- Pattern calibration is complete;
- Pattern release readiness passed.

## HMAC incident status

Resolved.

Observed:
Real historical payloads returned HTTP 401 INVALID_SIGNATURE while a
synthetic ASCII HMAC diagnostic passed far enough to receive sender
validation HTTP 403.

Correction:
Apps Script HMAC now uses explicit UTF-8 charset.

Temporary auth diagnostic:
removed.

Sanitized Worker error-code logging:
retained.

## Retry-safety status

Worker Gmail ingestion has been hardened.

Completion is determined by canonical evidence, not merely raw_event
existence.

An incomplete raw Gmail ingestion can be guardedly repaired/reprocessed.
Completed rows return idempotent duplicate success.

Canonical event/evidence persistence failures are no longer silently ignored.

## Gmail rules

Gmail access is read-only.

Do not:
- archive;
- mark read;
- delete;
- label;
- send;
- forward;
- mutate mailbox state.

Use sender/source allowlists.
Store only minimum evidence needed by the parser.
Do not persist full email bodies.

Gmail message ID is the ingestion external_id.

## Financial invariants

Owned-to-Owned transfer:
- does not change total liquid assets;
- does not change total allocations.

Financial goal target alone:
- does not reduce Dana Tersedia.

Only actual allocation:
- reduces Dana Tersedia.

Covered upcoming:
- must not be deducted twice.

Receivables:
- are non-liquid.

Financial calculations:
- round monetary writes/SUMs to 2 decimals;
- equality tolerance abs(diff) < 0.005.

User-facing financial dates:
Asia/Jakarta / WIB.

## Immediate next action

Historical closed-month backfill is complete.

Do not continue historical backfill.

For Pattern Analysis:
- HISTORY VERIFIED may now be marked YES;
- historical canonical data must still pass Pattern eligibility filtering;
- do not treat Pending Review, INVOICE_EVIDENCE, NON_TRANSACTION, or other
  non-authoritative zero records as economic calibration observations;
- perform real-data false-positive audit before calibration;
- keep calibration and holdout separate;
- do not merge Pattern from the backfill workflow.

For current ingestion:
August 2026 is outside the closed-history verification scope and should be
handled separately.

Do not start Prompt 14 cutover solely because historical verification passed.

## Do not do

- Do not start Prompt 14 cutover yet.
- Do not make D1 source of truth yet.
- Do not dual-write SQLite and D1.
- Do not mutate Gmail.
- Do not ask for or print secrets.
- Do not commit database/backup/export/raw financial data.
- Do not perform broad destructive D1 cleanup.
- Do not upgrade Wrangler major version merely because a warning appears.
- Do not weaken financial tests to make them pass.
- Do not rerun expensive suites when a docs-only change does not warrant it.

## No known blocker

No historical Gmail ingestion integrity blocker remains for the verified
range through 31 July 2026.

Formal state:
HISTORY VERIFIED = YES

Separate unresolved work is intentionally outside this gate:
- August 2026 current-month ingestion coverage;
- Pattern real-data false-positive audit;
- Pattern calibration and holdout validation;
- Pattern release readiness;
- future production cutover.

## Superseding verified state - 27 Aug 2026 canonical closure

This section supersedes older Git/deployment/canonical-integrity state above
where they conflict.

Current main implementation HEAD before this continuity-only commit:

7b0ad0687fae6742e394d430801d006ca88b70f3

Latest relevant implementation:

fix(gmail): support collapsed BCA cardless repair

BCA collapsed-canonical recovery is complete.

Final D1 state after guarded cleanup:

- raw_events: 2078
- ingestion_candidates: 1753
- canonical_event_evidence: 2075
- canonical_financial_events: 2066
- empty canonical events: 0
- canonical/evidence orphans: 0
- bca_qris_erensi: removed after exact evidence=0 proof

Final repaired-target verification:

- original target rows: 1081
- exact target rows found: 1081
- raw IDs preserved
- candidate IDs preserved
- evidence IDs directly matched original manifest
- amount mismatches: 0
- semantic mismatches: 0
- canonical multi-amount collisions: 0
- mixed-family collisions: 0
- reference collisions across repaired canonical targets: 0
- bad "erensi" references among repaired targets: 0
- applied candidate mutations: 0

Original affected parser-family totals:

- MERCHANT_PAYMENT: 1054
- CASH_WITHDRAWAL: 27

Final repaired semantic totals:

- MERCHANT_PAYMENT: 1045
- FAILED_ATTEMPT / Ignore: 9
- CASH_WITHDRAWAL: 27
- EXTERNAL_TRANSFER / Pending Review: 0

Production SQLite remains authoritative and unchanged.

Verified SHA-256:

2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

Worker remains:

- aturuang-api
- MODE=shadow
- schema=7
- health=ok

Formal gates:

HISTORY VERIFIED = YES

CANONICAL INTEGRITY = PASS

BCA COLLAPSED-CANONICAL INCIDENT = CLOSED

Immediate next phase:

Resume Pattern Analysis v1 from its separate local worktree.

Pattern remains:

- not merged into main;
- not production-wired;
- not deployed;
- not yet calibrated against the repaired canonical history.

Before any Pattern merge/deployment:

1. verify Pattern worktree/branch/HEAD;
2. compare it against current main;
3. audit eligibility against repaired real canonical data;
4. perform false-positive review;
5. keep calibration and holdout separate;
6. run release-readiness tests;
7. only then decide whether to merge.

Do not start Prompt 14 cutover merely because canonical integrity passed.
D1 remains shadow and SQLite remains the financial source of truth.
