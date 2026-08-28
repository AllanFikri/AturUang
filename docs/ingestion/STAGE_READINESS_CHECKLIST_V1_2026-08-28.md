# AturUang - Stage Readiness Checklist V1

Use this as a stop/go checklist. A phase is not started just because the previous code compiles; its **Definition of Ready (DoR)** must be met first.

## Phase -1 - Source Corpus & Template Freeze

**DoR:** private source archives available.
**Deliverables:** sanitized manifest, template contracts, edge-case catalog, fixture policy.
**Gate:** all known source families represented; unknown future formats fail closed.
**Current:** **PASS at audit/design level (28 Aug 2026)**.

## Phase 0 - Baseline / Inventory Freeze

**DoR**
- main clean and synchronized
- production SQLite hash verified
- D1 shadow baseline verified
- source corpus/template V1 available

**Must build/record**
- isolated `feature/universal-ingestion-v1` worktree
- source coverage matrix
- open/partial versus missing month state
- known source gaps
- private corpus location excluded from Git
- PDF extraction benchmark/decision record

**Gate**
- baseline tests pass
- worktree clean
- SQLite unchanged
- no D1 authority change

## Phase 1 - Registries + Contracts

**DoR:** Phase 0 PASS; source families known.
**Build:** Institution Registry, Account Registry, Account Lifecycle, Source Registry, Source Capability Contract, Source Document Registry, Import Batch.

Additional requirements from corpus:
- provider/account identity independent of display name
- parent/subaccount hierarchy
- natural document key + SHA
- template id/fingerprint/parser version
- source role/capabilities
- period CLOSED/OPEN/PARTIAL
- account lifecycle observations

**Tests:**
- BCA Poket lifecycle variants
- Jago pocket creation/removal/rename
- provider-name/display-name collision
- bluSaving hierarchy
- duplicate SHA
- same natural key with different SHA
- source capability query

**Gate:** all BCA/Jago/SeaBank/blu/wallet/RDN/account structures can be represented without provider-specific schema hacks.

## Phase 2 - Universal Import Foundation

**DoR:** registries/contracts PASS.
**Build:**
- ZIP/file discovery
- safe recursive archive extraction
- SHA-256
- source/template detector
- PDF/image preflight
- adapter interface
- normalized event envelope
- dry-run result
- diagnostics/provenance

**Archive safety:** zip-slip, recursion depth, file count, uncompressed size, compression ratio.
**No ledger mutation.**

**Gate:**
- repeated nested and standalone copies deduplicate
- semantic same-statement duplicate can be detected
- unknown template fails closed
- private file contents never enter logs/Git

## Phase 3 - Provider Adapters

Order: BCA -> Jago -> SeaBank -> blu -> GoPay -> ShopeePay -> Stockbit -> Shopee Orders.

Every adapter requires:
- template fingerprint
- sanitized golden normal fixture
- empty/no-activity fixture when applicable
- multiline/continuation fixture
- malformed/truncated fixture
- idempotency fixture
- source-specific edge-case fixture
- privacy-safe diagnostics
- **full private corpus replay**

Provider-specific gates:
- **BCA:** posted/effective dates preserved, Poket lifecycle, no reference-only identity, encrypted PDFs supported.
- **Jago:** hierarchy parsed, internal pocket transfers safe, semantic duplicate caught.
- **SeaBank:** section-scoped zero activity; interest/deposit sections not double-counted.
- **blu:** mutation and portfolio kept distinct; portfolio is snapshot evidence.
- **GoPay:** empty months and wrapped IDs supported; payment components preserved.
- **ShopeePay:** image quality/confidence; failed/refund status; low confidence -> review.
- **Stockbit:** cash/trade/portfolio roles separated; trade date != due date.
- **Shopee:** order/line-item enrichment only; payer not inferred from order ownership.

## Phase 4 - Account Discovery

**DoR:** provider outputs are normalized only.
**Build:** observed account resolver, parent/subaccount resolution, NEW/RENAMED/CLOSED/REUSED, ownership confidence.
**Gate:** new subaccounts can appear without schema change; display-name collisions do not merge identities.

## Phase 5 - Cross-Source Matcher

**DoR:** account ownership resolvable.
**Build:** same-event, owned-transfer, order-payment, investment settlement, duplicate evidence.

Matching evidence may include:
- provider/account identity
- occurred/posted/settlement date windows
- amount + direction
- raw/normalized reference
- provider transaction id
- counterparty
- payment method
- balance context
- source provenance

**Rule:** reference alone never decides identity.
**Gate:** no double counting in synthetic tests and private real dry runs.

## Phase 6 - Financial Semantics

**DoR:** matching/ownership evidence available.
**Build:** Expense, Income, Internal Transfer, Refund, Receivable Settlement, Investment, Review.

Shopee/third-party checkout semantics:
- SELF
- THIRD_PARTY_DIRECT -> no personal expense
- SELF_REIMBURSABLE -> pass-through/receivable
- MIXED -> line-item ownership allocation
- UNKNOWN -> review

**Gate:** Credit != automatically Income; Debit != automatically Expense; commerce order != automatically personal spending.

## Phase 7 - Reconciliation

**DoR:** closed-period normalized events available.
**Build:** statement equation, subaccount reconciliation, transfer pairs, RDN reconciliation, monthly close.
**Statuses:** PASS / WARNING / FAIL / UNVERIFIABLE.
**Gate:** closed periods reconcile or carry explicit identified exceptions.

## Phase 8 - Review / Staging

**DoR:** ambiguity can be represented explicitly.
**Review reasons:** unknown template/account/ownership, unmatched incoming, reference conflict, balance mismatch, semantic ambiguity, duplicate ambiguity, third-party checkout.
**Build:** audit trail + idempotent controlled apply.
**Gate:** no ambiguous event silently becomes canonical truth.

## Phase 9 - UI

Accounts hierarchy, Import Center, Source Coverage, Reconciliation, Review Queue, Evidence Drilldown.
Display parser/template version and import/reconciliation health where useful.
**Gate:** user can see what is complete, partial, missing, ambiguous, and why.

## Phase 10 - Historical File Backfill

Process **one closed month at a time**:

parse -> match -> reconcile -> review -> canonical integrity -> freeze month -> next month

Never one-shot multi-year import.
Current/open months are not closed merely because files exist.

## Phase 11 - Drive Automation

Drive is transport/discovery only. Parser works on ordinary local files.
Detect/register new documents and source freshness; no financial authority.

## Phase 12 - Apps Script / Gmail

Near-real-time evidence only. Trusted sender + sanitized payload + HMAC + retry/idempotency.
Do not enable a new sender/source until parser + fixture + tests exist.
Do not replace statement reconciliation with Gmail assumptions.

## Phase 13 - Telegram

Review/notification interface, never direct arbitrary balance authority.
All mutation decisions audit logged and idempotent.

## Phase 14 - Pattern Recalibration

Only after historical truth is complete. Rerun eligibility, sessionization, habit, recurring, drift, anomaly, human false-positive review, candidate thresholds, freeze, prospective validation.
Historical data already inspected is not a blind final holdout.

## Phase 15 - Final Integration / Release Readiness

Full Python regression, TypeScript check, Gmail, every provider adapter, private corpus replay, account discovery, matcher, semantics, reconciliation, Telegram security, Pattern tests, SQLite hash, D1 integrity, health check.

D1 authority remains a separate explicit decision; no silent cutover.
