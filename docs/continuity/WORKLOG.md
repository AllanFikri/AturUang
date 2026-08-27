# AturUang Project Worklog

Status: append-only project history
Last reconstructed: 2026-08-26
Project: Money Tracks / AturUang

This file records important work, failures, validation, and milestones.
It is not a substitute for Git history or database evidence.

Truth precedence:
1. Actual repository / database / deployed system.
2. Test, audit, deployment, and Git evidence.
3. HANDOFF.md.
4. This reconstructed historical worklog.
5. Old chat or human recollection.

Never place secrets, raw email bodies, full account numbers, production
database contents, tokens, private chat IDs, or credentials in this log.

## Phase 0-4 - Financial foundation and correctness

The early project focused on making Money Tracks more than a simple
expense tracker.

Important outcomes:
- Financial events gained stronger auditability.
- verify_balances used balance anchors / snapshots to detect differences
  without silently mutating the database.
- Reconciliation created canonical Adjustment events rather than hidden
  balance patches.
- Transaction account selection was corrected.
- Current, historical, and future month summaries were mathematically
  separated.
- Historical periods used cash-flow style reporting.
- Future periods used projections rather than pretending forecasts were
  actual balances.

## Prompt 5 / K1 - Anti-double-deduction

The allocation/upcoming model was hardened.

Rules established:
- Active allocations may cover upcoming commitments.
- Effective commitment avoids deducting covered amounts twice.
- Paying a covered upcoming item consumes its allocation atomically.
- State transitions must be idempotent.

## Prompt 6 / K2 - Custody, receivables, and payables

Debt/event modeling was expanded.

Rules established:
- Custody/Titipan, Receivable/Piutang, and Payable/Utang are distinct.
- debt_events form an auditable event ledger.
- Outstanding balances are derived from signed events.
- Receivables are not liquid assets.
- Upcoming and payable relationships must not cause double deduction.

## Prompt 7 / S3-S4 - Budget derivation and idempotency

Important outcomes:
- Manual budget_effect was no longer the authority for new transactions.
- Eligible Personal Expense transactions determine effective budget spend.
- Legacy history remained readable.
- Critical mutation endpoints gained idempotency key + request hash.
- Same key + same payload = replay without mutation.
- Same key + different payload = conflict.

## Prompt 8 - UI/UX and accessibility

The interface was hardened without rebuilding the stack.

Work included:
- Responsive layout and mobile behavior.
- Modal viewport handling and table horizontal scrolling.
- Minimum practical touch targets.
- Form labels and accessible dialog semantics.
- ARIA live regions.
- Focus trap / restore and focus-visible behavior.
- Reduced-motion support.
- Contrast fixes.
- Centralized money formatting.
- Canonical Indonesian terminology.

Later accessibility work further verified status/alert semantics,
aria-invalid, aria-describedby, invalid-field focus behavior, and theme
contrast.

## Prompt 9 - Release readiness and ledger correction

Important outcomes:
- Verified BCA ledger corrections were entered without synthetic balance
  manipulation.
- Release E2E tests used temporary databases.
- Physical mobile-device behavior was checked.

## Prompt 10 - Navigation, local suggestions, and runtime

Important outcomes:
- Navigation state was corrected.
- Account card layouts were improved.
- Each page was audited for purpose and behavior.
- Complex ML was rejected as unnecessary.
- Deterministic/local rule-based recommendations were preferred.
- Suggestions only populate drafts and do not auto-submit.

Major incident discovered:
Two separate runtime trees existed in Downloads and the GitHub workspace.
A stale process could serve an old source/database on port 5050.

Resolution:
- Stale process stopped.
- Canonical workspace became:
  C:\A User Main Storage\Documents\GitHub\AturUang
- Downloads copy is not an active workspace.

## Prompt 11 - Allocation model correction

A major financial modeling flaw was corrected.

Old problem:
Protection tied to an account's current balance could change "Dana
Tersedia" when money moved between owned accounts.

New model separates:
- physical account location;
- allocation;
- financial goal target;
- actual allocated amount;
- confirmed/tentative commitments.

Core invariants:
- Owned-to-Owned transfers do not change total liquid assets.
- Internal transfers do not change total allocation.
- A goal target alone does not reduce available funds.
- Only actual allocated_amount reduces available funds.
- Covered upcoming items are not deducted twice.
- Receivables do not increase liquid funds.
- Custody/payables affect the financial position according to their contract.

This design must not regress back to accounts.protected_amount as the
authority for allocations.

## Prompt 12 - Deterministic intelligence

Insights were intentionally implemented without black-box ML.

Capabilities:
- recurring transaction patterns with minimum support;
- 7-day and 30-day projections;
- light anomaly detection labeled "Perlu dicek";
- source freshness;
- forecast/upcoming deduplication.

Forecasts must never mutate financial state or masquerade as actual balance.

## Repository privacy rebuild and refactor

A production shadow seed containing real financial data was discovered in a
local Prompt 13A commit before it was pushed.

Response:
- unsafe history was discarded/rebuilt;
- production seed/export/database/backup files were hardened in ignore rules;
- remote repository was rebuilt cleanly;
- GitHub repository remained private.

Important historical clean commits:
- 75a0a3b chore(security): rebuild repository without financial data
- 61848f0 refactor(repo): rapikan struktur proyek dan test runner

The short visible history at that point was deliberate privacy remediation,
not evidence that earlier project work never happened.

Canonical repository structure established:
- aturuang/
- aturuang/config.py
- tests/
- tests/run_all.py
- scripts/
- docs/
- prompts/
- cloud/worker/
- runtime/
- launcher.py

Brownfield rule:
Do not replace Python + Vanilla JS/HTML/CSS + SQLite with a new application
stack. TypeScript/npm are isolated to the cloud subtree.

## Prompt 13A - Cloudflare shadow foundation

Cloud components established:
- Cloudflare Worker: aturuang-api
- D1: aturuang-db
- D1 binding: DB
- cloud source: cloud/worker/
- MODE=shadow

Architecture contract:
- SQLite remained source of truth.
- D1 was shadow only.
- No dual-write.
- Financial write routes were blocked in shadow.
- Google Sheets was explicitly rejected as database/staging/source of truth.
- Read API parity, migrations, exporter, threat model, and runbooks were built.

## Prompt 13B - 24/7 ingestion foundation

Goal:
Receive financial evidence without requiring the laptop to stay online.

Architecture:
Gmail -> Google Apps Script -> HTTPS/HMAC -> Worker -> D1 staging
Telegram -> webhook -> Worker -> D1 staging

Core staging tables include:
- raw_events
- ingestion_candidates
- source_sync_state
- ingestion_audit_log

Principles:
- idempotent;
- privacy-minimal;
- auditable;
- human-in-the-loop;
- shadow-only;
- no dual-write.

Gmail remains read-only.
Message ID is the authoritative external ID.
D1 uniqueness, not Gmail labels, is the dedupe authority.

Telegram security was configured around private allowlisting, webhook
authentication, replay protection, and no secret exposure.

## Gmail Transaction Intelligence

Historical and live Gmail evidence was expanded beyond a single bank pattern.

Design principles established:
- source-specific adapters rather than one universal regex;
- reference/order ID is stronger evidence than textual similarity;
- multiple emails may represent evidence for one canonical financial event;
- message ID remains ingestion idempotency key;
- thread context is context, not authoritative dedupe;
- internal owned transfers remain transfers rather than fake income/expense;
- ambiguous evidence stays reviewable rather than being forced into a ledger.

Canonical event/evidence modeling was added for intelligence while shadow mode
continued to prevent ledger mutation.

## BCA pocket closure failure

A real BCA Internet Transaction Journal for a pocket closure exposed a parser
edge case.

Failure:
The first nominal value could be zero, which attempted to create an
ingestion candidate violating amount > 0.

Correction:
- candidates are only inserted for positive financial amounts;
- BCA pocket closure gained explicit lifecycle handling;
- closing balance and pocket-to-main movement are treated as internal
  lifecycle/transfer evidence rather than expense;
- zero-amount lifecycle evidence may be retained without an invalid candidate.

Key referenced commit:
- 4cfcead2 fix(gmail): harden pocket closure ingestion

This did not establish a universal pocket model for every provider.
A broader stable sub-account identity model remains future work.

## Historical backfill resumability

Historical Gmail processing was made month-scoped and resumable.

Important rules:
- inclusive month start / exclusive next-month boundary;
- message-level date filtering even when Gmail returns threads;
- pages use a persisted thread offset;
- checkpoint advances only after a page is fully relayed;
- any non-200 response stops progress and preserves the offset;
- retries are expected and must be idempotent.

Referenced fixes include:
- 32a4f0ae pagination/backfill behavior
- 484a2e checkpoint safety on relay failure

## Financial-core regression guard repair

A regression test initially compared an obsolete fixed source line range and
reported a false failure after legitimate import/status changes.

The guard was changed to a semantic marker boundary and restored baseline so
future parser work cannot silently alter the financial core.

Referenced commit:
- 796d9cf fix(cloud): restore financial core after gmail intelligence

Prompt 13B Intelligence subsequently passed 5/5.

## Migration 0007 metadata repair

D1 canonical tables existed but the schema_migrations marker for version 7 was
missing.

Repair:
- migration source was corrected;
- only D1 schema metadata was repaired;
- local SQLite was not changed.

Referenced commit:
- 6ceb120

Post-repair:
- schema version 7;
- canonical_financial_events present;
- canonical_event_evidence present.

## Pre-backfill orphan repair

A known incomplete Gmail ingestion from the earlier pocket-closure failure
remained as one raw orphan.

A guarded surgical deletion removed only that exact incomplete raw row.

After repair:
- Gmail raw orphans = 0
- candidate orphans = 0
- canonical orphans = 0
- remote ledger unchanged
- local SQLite unchanged

No broad cleanup/delete was used.

## Retry-safety hardening

A critical pre-backfill review found that simple raw_event existence was not
enough to prove a Gmail message had fully completed canonical persistence.

New completion rule:
A Gmail raw event is complete only when canonical evidence exists.

Behavior:
- existing raw + canonical evidence -> duplicate success;
- existing raw without evidence -> guarded incomplete-ingestion recovery;
- canonical event and evidence persistence are checked explicitly;
- new canonical event + evidence persistence uses D1 batch semantics;
- persistence failures propagate rather than being swallowed.

Referenced commit:
- ecc570a fix(gmail): make canonical ingestion retry-safe

Validation included:
- TypeScript checks;
- Prompt 13B Intelligence 5/5;
- Prompt 13B ingestion tests;
- Prompt 13A cloud parity;
- local master quick suite;
- unchanged SQLite hash.

Worker deployment after hardening:
- version 9ccb47fd-cd48-4c7d-bb43-143f35e18255

## Controlled January 2025 backfill

A repository-backed helper restricted execution to January 2025 and refused
to rerun once the checkpoint advanced.

Referenced commit:
- c2d1055 chore(gmail): add controlled january backfill runner

First attempt:
- began at January offset 0;
- 8 messages returned HTTP 401;
- offset was correctly preserved;
- no blind checkpoint advance occurred.

A temporary HMAC diagnostic used a synthetic untrusted sender.
Result:
- HTTP 403 UNTRUSTED_GMAIL_SENDER.

Meaning:
Generic secret/header/timestamp/nonce/HMAC flow worked for the simple
diagnostic and reached sender validation.

Sanitized Worker error-code logging was temporarily added.
Real failing messages returned:
- INVALID_SIGNATURE

Evidence pointed to payload charset/signing behavior: the synthetic ASCII
payload passed while real historical payloads could contain non-ASCII text.

Correction:
Apps Script HMAC signing was changed to explicit UTF-8:
Utilities.Charset.UTF_8

The temporary diagnostic helper was then removed.
Privacy-safe Worker error-code logging was retained.

Validation:
- January retry completed without INVALID_SIGNATURE;
- 59 relay operations returned success during that execution;
- checkpoint advanced to 2025-02.

Referenced commit:
- 1c8aacb fix(gmail): sign relay HMAC with explicit utf-8

## Controlled February 2025 backfill

A February-only runner replaced the January controlled helper.

Referenced commit:
- 841fc63 chore(gmail): prepare controlled february backfill

Execution:
- started at February offset 0;
- page advanced to offset 25;
- page advanced to offset 50;
- month completed;
- 112 relay operations succeeded;
- checkpoint advanced to 2025-03;
- no relay errors were reported.

Post-February exact audit:
- schema_version = 7
- ledger_transactions = 1150
- gmail_raw_total = 215
- gmail_candidates_total = 179
- gmail_evidence_total = 215
- canonical_events_total = 119
- gmail_raw_orphans = 0
- candidate_orphans = 0
- canonical_orphans = 0
- nonpositive_candidates = 0
- gmail_error_state = 0
- raw_privacy_leaks = 0
- D1 audit rows written = 0
- D1 database size = 1,052,672 bytes
- local SQLite unchanged = PASS

Current historical Gmail checkpoint:
2025-03

## Current state at reconstruction

Date:
2026-08-26

Git HEAD before continuity-doc commit:
841fc63

Working tree:
clean

Worker:
aturuang-api

Worker mode:
shadow

D1:
aturuang-db
schema version 7

Current local production SQLite SHA-256:
2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

Local/remote ledger count:
1150

Next controlled operational milestone:
Prepare and validate March 2025 historical Gmail backfill.

Do not start broad multi-month backfill or Prompt 14 cutover until the
controlled historical validation sequence is intentionally completed.

## Continuity foundation and March preparation

Continuity documentation was established and pushed:

- commit 1f78c33 docs(project): add durable continuity log
- WORKLOG.md created
- HANDOFF.md created
- DECISIONS.md created
- WORKING_RULES.md created

This makes repository documentation, rather than chat memory alone,
the continuity mechanism for future GPT/ChatGPT sessions.

After the clean February 2025 audit, the controlled Apps Script helper
was advanced from February to March only.

March runner preparation:
- commit a066a40 chore(gmail): prepare controlled march backfill
- required checkpoint: 2025-03
- allowed historical window: 2025-03 through 2025-03 only
- retry remains offset-preserving
- runner refuses execution after checkpoint advances beyond March
- no Worker, D1, or SQLite mutation was performed during preparation

Next operational action:
Paste repository Code.gs into the existing Apps Script project and run
backfillMarch2025Trial() exactly once, then perform an exact post-March audit.

## Historical backfill strategy revised before March execution

January and February 2025 were used as controlled canary months.

They exposed and allowed correction of important failure classes including:
- incomplete-ingestion retry safety;
- canonical evidence completion semantics;
- UTF-8 HMAC signing;
- page/checkpoint preservation after relay failure.

After February passed its exact integrity audit, the strategy was revised.

The March-only helper prepared in commit a066a40 was superseded before
execution.

Decision:
Do not continue creating one manual runner per month.

Next architecture:
One resumable Historical Backfill v2 runner will resume from the authoritative
stored checkpoint, process closed historical months through 2026-07, stop
fail-fast on infrastructure/relay integrity errors, respect Apps Script
runtime limits, and preserve retry position.

Cloudflare capacity is not the reason for the previous month-by-month process.
The purpose was canary validation of ingestion correctness.

## Historical Backfill v2 circuit breaker implemented

Implementation commit:
- 8ccdf92 feat(gmail): add fail-fast historical backfill v2

Historical range:
- authoritative start checkpoint: 2025-03
- closed cutoff: 2026-07

Safety behavior:
- concurrent bulk execution is rejected by ScriptLock;
- any operational/integrity error creates a persistent circuit-breaker latch;
- a blocked runner refuses future runs until the block is explicitly cleared
  after diagnosis;
- clearing the block does not reset checkpoint or offset;
- Worker health must return HTTP 200;
- Worker health JSON must be valid;
- Worker MODE must remain shadow;
- schema must be >= 7;
- checkpoint and offset must be consistent;
- invalid offset is not silently reset;
- current/open month is refused;
- first non-200 relay stops immediately;
- first network exception stops immediately;
- malformed/non-success HTTP 200 Worker response stops immediately;
- page offset advances only after whole-page success;
- runtime pause is coded as BACKFILL_RUNTIME_PAUSE and is safe to resume;
- successful earlier messages on a failed page remain retry-safe through Worker
  idempotency and canonical evidence completion semantics.

Validation before commit:
- Code.gs JavaScript syntax PASS;
- Backfill v2 static safety test PASS;
- Prompt 13B Intelligence regression PASS when available;
- git diff --check PASS;
- local production SQLite hash unchanged.

Status:
Repository implementation prepared.
Apps Script still requires manual full-Code.gs synchronization before running.

## Historical Backfill v2 offset-regex correction

During manual review of the final Code.gs before execution, an escaping defect
was caught before any bulk historical run.

Observed:
The inner historical offset validator contained /^\\d+$/ instead of /^\d+$/.

Impact if executed:
A persisted numeric offset such as 25 could be rejected as
BACKFILL_INVALID_OFFSET on a later resume.

Correction:
- ce092cc fix(gmail): correct historical offset validation

Validation:
- JavaScript syntax PASS;
- numeric offset runtime regex test PASS;
- Backfill v2 static safety test PASS;
- git diff --check PASS.

Important:
The defect was found before Historical Backfill v2 was executed, so no Gmail
checkpoint, D1 data, or local ledger repair was required.


## Historical Backfill v2 - Google Play cancellation failure

Historical Backfill v2 operational result:
- March 2025 completed.
- April 2025 completed.
- checkpoint advanced to 2025-05.
- first May 2025 page stopped fail-fast with BACKFILL_HTTP_FAILURE.
- Worker returned HTTP 500 for a trusted Google Play lifecycle email.
- page offset did not advance.

Diagnosis:
The triggering email was a subscription cancellation notification.
The Google Play parser did not recognize English canceled/cancelled wording
before generic numeric amount parsing.

Correction:
- English cancellation is classified before amount parsing.
- event_kind = NON_TRANSACTION.
- financial_class = Ignore.
- financial_direction = Neutral.
- amount = 0.
- candidate = null.
- evidence_role = LIFECYCLE_STATUS.
- no ledger mutation.

Implementation:
- 05dadec fix(gmail): classify google play cancellations

Validation before implementation commit:
- dedicated Google Play cancellation regression passed.
- Prompt 13B Intelligence regression passed.
- Historical Backfill v2 regression passed.
- TypeScript check passed.
- production SQLite hash unchanged.

Deployment:
- Worker aturuang-api deployed successfully.
- Worker version: 6c04bb23-6572-47b1-9a2d-e91f00e3c788
- MODE: shadow
- schema >= 7.
- production SQLite remained unchanged.

Operational state:
The Backfill v2 circuit breaker remains intentionally blocked until the
deployed correction has been verified and explicitly cleared.
Checkpoint and offset must not be reset.


## Gmail incomplete cleanup metadata false-negative

During the May 2025 Historical Backfill v2 retry, Worker returned
INCOMPLETE_GMAIL_EVENT_REPAIR_FAILED while recovering an incomplete Gmail
raw event.

An exact read-only D1 query afterward returned no matching raw_event row.

Root cause:
The recovery path treated cleanup.meta.changes != 1 as definitive failure,
although the authoritative post-delete database state showed the raw row
was already absent.

Correction:
- 7a8377d fix(gmail): verify incomplete cleanup state
- mutation metadata remains the fast path.
- if metadata is inconclusive, Worker queries the exact raw_event id.
- if the row remains, recovery still fails closed with HTTP 503.
- if the row is absent, processing safely continues.
- no broad delete.
- no checkpoint reset.
- production SQLite unchanged.

Validation:
- Prompt13B Intelligence PASS.
- Google Play cancellation regression PASS.
- Historical Backfill v2 regression PASS.
- TypeScript PASS.
- Worker health PASS.
- MODE shadow.
- schema >= 7.

Worker version:
47d015bc-39dc-48b0-9bf2-fc82490cc382


## Historical Backfill lightweight progress observability

Operational state before implementation:
- May 2025 completed successfully.
- June 2025 first 25-thread page completed.
- authoritative resume state is 2025-06 offset 25.
- runner exited normally with BACKFILL_RUNTIME_PAUSE.
- no circuit-breaker error caused this pause.

Implementation:
- ced919f feat(gmail): add lightweight backfill progress logs
- historical fail-fast processing logs the first trusted in-scope message
  and every fifth trusted message afterward.
- routine log shows cleaned sender and subject truncated to 72 characters.
- email body is never emitted in routine progress logs.
- Gmail message ID is not emitted in routine progress logs.
- existing detailed error diagnostics are unchanged.
- live relay behavior is unchanged.
- checkpoint and offset behavior are unchanged.
- Worker and D1 logic are unchanged.
- production SQLite hash remained unchanged.

Validation:
- Code.gs JavaScript syntax PASS.
- dedicated observability regression PASS.
- Historical Backfill v2 regression PASS.
- Prompt13B Intelligence regression PASS.
- git diff check PASS.

Apps Script source must be manually synchronized before the next
historical backfill execution.

## Final historical Gmail verification - 27 Aug 2026

Historical Backfill V2 completed through 2026-07 and advanced the
authoritative checkpoint to 2026-08.

Final read-only verification:
- historical range: 2025-01-01 through 2026-07-31
- Gmail raw: 2038
- Gmail canonical evidence: 2038
- Gmail candidates: 1720
- distinct historical canonical events: 964
- raw without evidence: 0
- evidence without raw: 0
- evidence without canonical: 0
- canonical without evidence: 0
- candidate orphans: 0
- unresolved Gmail Pending/Error raw events: 0
- duplicate raw external IDs: 0
- duplicate canonical event IDs: 0
- duplicate canonical/raw evidence pairs: 0
- non-positive ingestion candidates: 0
- source_sync_state gmail: OK with no error code
- privacy invalid JSON: 0
- persisted body/subject/message-id/from fields: 0
- maximum minimal payload length: 138
- unexpected sender count: 0
- every month Jan 2025 through Jul 2026 contains evidence

A preliminary audit incorrectly failed all non-Ignore zero-amount canonical
events. That gate was corrected against the actual schema contract:
ingestion candidates require amount > 0, while canonical events allow
amount >= 0 because review/evidence/lifecycle facts may legitimately carry
zero until or unless an economic amount is established.

Historical zero canonical rows were confined to:
- 38 Pending Review transfer/incoming-transfer events
- 36 INVOICE_EVIDENCE events
- 140 NON_TRANSACTION / Ignore events

All classified economic movement categories had zero_count=0.

Production SQLite remained unchanged:
- transactions: 1150
- SHA-256:
  2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

Final cloud verification:
- Worker health: ok
- MODE: shadow
- D1 schema: 7
- deployed version evidence:
  8dd6465b-0432-4414-8626-7b335be9f101

Final Git audit before this docs update:
- main: 88d6015
- origin/main: 88d6015
- working tree: clean

Formal decision:
HISTORY VERIFIED = YES for historical Gmail ingestion through 31 Jul 2026.

This does not certify August 2026 current-month coverage, Pattern
false-positive performance, or Pattern calibration.

Parallel Pattern work reported separately:
- worktree: AturUang-pattern-v1
- branch: feature/pattern-analysis-v1
- HEAD: 11fc5043746d54f3ac31fffbdeef6eed47ebeabf
- not merged
- not deployed
- not production-wired

Do not merge Pattern as part of this historical-backfill closure.
