# AturUang Architecture and Product Decisions

This file records durable decisions and why they exist.
Do not reverse them casually because a different implementation looks easier.

## ADR-001 - Preserve the brownfield stack

Decision:
Keep the local Python + Vanilla JS/HTML/CSS + SQLite architecture.

Cloud TypeScript/npm remains isolated under cloud/worker.

Reason:
The existing product already contains tested financial behavior. Rebuilding
the stack creates risk without solving a current product problem.

## ADR-002 - One canonical workspace

Decision:
The canonical workspace is:
C:\A User Main Storage\Documents\GitHub\AturUang

Reason:
A historical split-brain between Downloads and GitHub caused an old process
and database to be served accidentally.

## ADR-003 - SQLite remains source of truth before controlled cutover

Decision:
Production SQLite remains the financial source of truth until a formal
cutover gate explicitly changes that.

Reason:
Cloud ingestion and historical parsing must be proven without risking the
existing ledger.

## ADR-004 - D1 remains shadow; no dual-write

Decision:
D1 is shadow/intelligence storage during the current phase.

Do not write the same financial mutation to both SQLite and D1.

Reason:
Dual-write creates divergent authorities and difficult recovery semantics.

## ADR-005 - Financial calculations have one canonical owner

Decision:
Do not independently recalculate the same financial KPI across service,
handler, and UI layers.

Reason:
One financial number should have one canonical computation path.

## ADR-006 - Allocation is independent of physical account location

Decision:
Allocations/goals are not derived from accounts.protected_amount.

Reason:
An internal transfer between owned accounts must not alter the economic
meaning of money already allocated.

## ADR-007 - Deterministic insight before ML

Decision:
Recurring patterns, forecasts, and anomaly flags use explainable,
deterministic rules unless a future requirement justifies ML.

Reason:
The data volume and use case do not require opaque models, and projections
must remain auditable.

## ADR-008 - Gmail is read-only and filtered

Decision:
Gmail connector reads only relevant allowlisted transaction notifications.

It must not mutate mailbox state.

Reason:
The connector exists to gather financial evidence, not administer the user's
mailbox.

## ADR-009 - Apps Script is a relay, not source of truth

Decision:
Apps Script may read Gmail, construct the minimum payload, sign it, and
relay it.

Authoritative dedupe/persistence lives in D1.

Reason:
Apps Script Properties/checkpoints are operational optimizations, not durable
financial truth.

## ADR-010 - Gmail relay uses timestamp + nonce + raw-body HMAC

Decision:
HMAC-SHA256 covers:
timestamp + "." + nonce + "." + exact JSON body.

Apps Script signing must specify UTF-8 explicitly.

Reason:
The Worker verifies UTF-8 bytes. Explicit charset prevents real non-ASCII
email content from producing signature disagreement.

## ADR-011 - Message ID is ingestion identity

Decision:
Gmail message ID is external_id.

Thread identity alone must never deduplicate independent messages.

Reason:
A Gmail thread may contain multiple distinct financial notifications.

## ADR-012 - Canonical evidence proves ingestion completion

Decision:
raw_event existence alone does not mean ingestion completed.

A completed Gmail raw event must have canonical evidence.

Reason:
A prior failure demonstrated that a raw row can survive while later
persistence fails. Treating such a row as an ordinary duplicate would
permanently lose canonical evidence.

## ADR-013 - Partial Gmail ingestion is retryable

Decision:
Existing raw without canonical evidence may be repaired/reprocessed only
through a guarded Gmail-specific path.

Reason:
Historical backfill must be safe to retry after transient failure.

## ADR-014 - Canonical financial fact and source evidence are separate

Decision:
Multiple source messages may support one canonical financial event.

Reason:
Bank, intermediary, receipt, and lifecycle emails may describe the same
economic event. Counting each email as a transaction would create duplicates.

## ADR-015 - Historical backfill is controlled month-by-month

Decision:
Process one closed month, then audit before continuing.

Reason:
Cloudflare capacity is not the limiting factor. Parser correctness and
financial evidence integrity are.

## ADR-016 - Backfill checkpoint advances only after full page success

Decision:
If any relay on a page fails, preserve the page offset.

Reason:
Advancing after partial failure could permanently skip historical evidence.

## ADR-017 - Minimal cloud persistence

Decision:
Do not persist full Gmail body, full sender/subject where unnecessary,
attachments, full account numbers, or unrelated PII.

Reason:
A personal-finance ingestion system should minimize breach impact and Git/log
exposure.

## ADR-018 - Production data never belongs in Git history

Decision:
Never commit production SQLite, backups, generated production seed SQL,
transaction exports, secrets, or raw email payloads.

Reason:
A real production seed was once caught in a local commit and required a
privacy-driven history rebuild.

## ADR-019 - Tests may not mutate production SQLite

Decision:
Mutation tests use temporary/in-memory databases.

Production SQLite hash is checked around high-risk stages.

Reason:
A test suite is not allowed to become a financial event source.

## ADR-020 - Cloudflare services are adopted only when they solve a problem

Decision:
Do not add Queues, R2, Durable Objects, Workers AI, or other services solely
because free quota exists.

Reason:
Avoid infrastructure complexity before there is a measured operational need.

Potential future direction:
Queues may become useful for asynchronous ingestion retries after historical
backfill and canonical behavior are stable.

## ADR-021 - Continuity documentation is part of the engineering process

Decision:
Maintain:
- WORKLOG.md
- HANDOFF.md
- DECISIONS.md
- WORKING_RULES.md

After meaningful implementation/deployment/audit:
- append the worklog;
- update handoff current state;
- add/change an ADR when architecture/product rules change.

Reason:
The project must remain continuable across new ChatGPT/GPT conversations
without relying on hidden chat memory.

Logs must never contain secrets or unnecessary personal financial data.

## ADR-022 - Archive Orphan Modules (5C, 5D, Stage 8)

Date: 2026-09-25
Status: Accepted

Context:
Three modules were detected as orphan (0 non-test caller) during V2.1 audit and confirmed during Milestone B spike.

Decision:
Archive (delete from working tree, restore via git if needed).

Rationale:
- 5C exposes only 6 semantic classes (not the 16 planned in Grand Design Appendix A). Wiring requires Phase 5A matching pipeline (4-7 day effort) for marginal gain over existing heuristics.
- 5D has no production caller; import_center.py uses a rows // 2 placeholder.
- Stage 8 is functionally covered by safe_apply.py + review_queue_ui.py.
- Total ~1,550 lines of dead code removed.

Consequences:
Restore via `git checkout <sha>:<path>` if needed. Reconciliation gap (rows // 2) remains open; tracked as backlog item.

