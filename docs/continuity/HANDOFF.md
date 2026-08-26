# AturUang Current Handoff

Last updated: 2026-08-26

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

Current implementation HEAD before this handoff update:
a066a40

Recent commits:
- 841fc63 chore(gmail): prepare controlled february backfill
- 1c8aacb fix(gmail): sign relay HMAC with explicit utf-8
- c2d1055 chore(gmail): add controlled january backfill runner
- ecc570a fix(gmail): make canonical ingestion retry-safe
- 6ceb120 migration 0007 metadata/source repair

Working tree at the post-February audit:
clean

## Current cloud state

Worker:
aturuang-api

Worker URL:
https://aturuang-api.allanfikrimahardika.workers.dev

Known deployed Worker version:
9ccb47fd-cd48-4c7d-bb43-143f35e18255

MODE:
shadow

D1:
aturuang-db

D1 schema:
7

Important:
D1 is still shadow/intelligence storage.
It is not yet the production financial source of truth.

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

Exact read-only audit after February 2025:

- gmail_raw_total: 215
- gmail_candidates_total: 179
- gmail_evidence_total: 215
- canonical_events_total: 119
- gmail_raw_orphans: 0
- candidate_orphans: 0
- canonical_orphans: 0
- nonpositive_candidates: 0
- gmail_error_state: 0
- raw_privacy_leaks: 0
- ledger_transactions: 1150
- schema_version: 7
- audit rows written: 0
- D1 size: 1,052,672 bytes

## Gmail historical backfill state

Completed and validated operationally:
- January 2025
- February 2025

Current checkpoint:
2025-03

January successful retry:
59 successful relay operations.

February execution:
112 successful relay operations.

The February runner has completed and the checkpoint moved to March.

Do not rerun February.

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

The March-only runner from commit a066a40 was superseded before execution.

Do NOT execute backfillMarch2025Trial().

Current authoritative Gmail checkpoint:
2025-03

Next engineering action:
Implement and validate Historical Backfill v2 with a fail-fast circuit breaker
for closed months through 2026-07.


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

At the end of the February audit, no active ingestion integrity blocker was
known.

Next controlled month:
March 2025.
