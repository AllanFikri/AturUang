# AturUang - Phase -1 Source Corpus & Template Freeze

Date: 28 August 2026
Status: **CORPUS AUDIT COMPLETE / IMPLEMENTATION NOT STARTED**

## 1. Why this phase exists

Before building registries, importers, or provider parsers, AturUang needs a frozen understanding of the actual private source corpus. The purpose is to prevent parser-specific assumptions from leaking into the financial truth engine and to avoid late architectural surprises.

Core rule:

> No parser before a source/template contract. No canonical event before provenance, ownership, matching, and reconciliation/review.

Raw private financial documents remain evidence outside Git. This report is sanitized and contains no raw account numbers, addresses, phone numbers, or full private source contents.

## 2. Corpus actually inspected

The Library archives are available and were materialized for this audit. Recursive archive inspection found **427 extracted source-document copies**. After byte-level SHA-256 deduplication there are **241 unique source files**, meaning **186 duplicate copies** already exist in the real bundle structure.

This is an important design result: nested archives and repeated imports are not hypothetical edge cases. The universal importer must deduplicate before parsing and must preserve archive lineage.

| Source family | SHA-unique docs | Coverage in corpus | Format | Key note |
|---|---:|---|---|---|
| BCA monthly statement | 19 | Jan 2025-Jul 2026 | PDF | encrypted-but-readable, text layer |
| Jago monthly statement | 11 files / 10 periods | Oct 2025-Jul 2026 | PDF | one semantic duplicate for Apr 2026 despite different bytes |
| SeaBank e-statement | 19 | Jan 2025-Jul 2026 | PDF | section-scoped zero-transaction handling required |
| bluAccount mutation | 25 | Aug 2024-Aug 2026 | PDF | transaction + running balance |
| blu portfolio | 25 | Aug 2024-Aug 2026 | PDF | subaccount snapshot, not transaction source |
| GoPay e-statement | 7 | Jan-Jul 2026 | PDF | true zero-transaction months exist |
| ShopeePay history | 13 | Aug 2025-Aug 2026 | image | long screenshots, variable resolution |
| Stockbit SOA | 10 | Oct 2025-Jul 2026 | PDF | cash ledger + trades + optional portfolio |
| Shopee order receipts | 112 | Dec 2024-Aug 2026 | PDF | commerce enrichment; not proof of payer |

All **228 PDF documents** in the SHA-unique corpus have an extractable text layer in this audit. ShopeePay is the image-only family. BCA is the special PDF case: all 19 audited PDFs report encryption, yet text extraction succeeds, so the eventual PDF engine must handle encrypted-but-readable statements instead of rejecting them just because an encryption flag exists.

## 3. Stable template families found

No major header-family drift was detected inside the current PDF corpus. The stable families to freeze as V1 are:

1. `bca_monthly_statement_v1`
2. `jago_monthly_statement_v1`
3. `seabank_estatement_v1`
4. `blu_account_mutation_v1`
5. `blu_portfolio_v1`
6. `gopay_estatement_v1`
7. `shopeepay_history_screenshot_v1`
8. `stockbit_soa_v1`
9. `shopee_order_receipt_v1`

Template detection must still be fingerprint-based and fail closed. A future provider layout change is `TEMPLATE_DRIFT`, not permission to run the old parser optimistically.

## 4. Critical source-specific findings

### BCA

- One statement family is stable across 19 audited months.
- Rows are multiline.
- Posting date and effective/transaction date can differ; both must be preserved.
- Running balance is not printed on every transaction row.
- Direction evidence is spread across DB/CR markers and description tokens.
- Poket lifecycle phrases appear in the statement and can signal creation/funding/movement/scheduled transfer.
- Transfer fees may be separate rows.
- Raw reference must be preserved independently from normalized reference.

**Design consequence:** a single `date`, `reference`, or `balance_after` field cannot be mandatory for every BCA row.

### Jago

- Statement first page contains pocket hierarchy and balances; subsequent sections contain pocket-specific transaction rows.
- Pocket identity and pocket display name must be separate.
- Pockets change over time, so lifecycle is a first-class model.
- Internal pocket-to-pocket movements are owned-to-owned transfers.
- A real audited pocket display name overlaps with another provider label. Therefore global identity by display name would be unsafe.
- The corpus contains two April 2026 PDFs with different file bytes but semantically identical extracted statements.

**Design consequence:** SHA-256 alone is insufficient for document idempotency. Add a provider-natural document key.

### SeaBank

- Main account summary, transaction detail, interest/tax detail, and deposit detail can coexist in one statement.
- The literal phrase `TIDAK ADA TRANSAKSI` is not a document-wide signal. In active months it can appear only in a deposit subsection.
- True no-transaction months also exist, where the phrase occurs in the main savings transaction section.

**Design consequence:** parsers must be section-aware. Never decide zero activity by searching the entire PDF for one phrase.

### blu

There are two distinct same-period document families:

- `bluAccount mutation`: transaction events and remaining balances.
- `blu portfolio`: account/subaccount balance snapshots.

The portfolio family exposes bluSaving/bluGether hierarchy and is valuable for account discovery/reconciliation, but it must not create duplicate transaction events.

August 2026 is partial/open in the current corpus and must not be treated as a closed statement month.

### GoPay

- Monthly one-page statement family is stable.
- True empty months exist with summary totals zero and an empty transaction table.
- Transaction IDs/descriptions can wrap in extracted text.
- Rows may expose more than one payment component, such as balance plus coins/rewards.

### ShopeePay

- This source is not PDF: it is a set of long dark-mode transaction-history screenshots.
- Image widths range from roughly **118 to 1220 px** and heights from roughly **1036 to 13017 px**.
- The UI family appears consistent, but resolution/quality varies dramatically.
- Entries can include Payment, Top Up, Transfer, Send to Bank, Refund, and status annotations such as failure/reward states.

**Design consequence:** do not build a fixed-coordinate screenshot parser. Add image preflight/quality scoring, relative/layout extraction, confidence, and fail-closed review. Failed payments must never become successful expenses; refunds must remain refund evidence.

### Stockbit

The SOA is a mixed financial document:

- RDN cash-ledger rows
- trade/invoice detail
- receipt/payment settlement rows
- fee/interest rows
- portfolio snapshot (optional; absent in at least one audited month)

Trade date and due date are different financial dates. Nested buy/sell lines are evidence about investments, not separate personal consumption expenses.

**Design consequence:** the adapter must emit event roles/subtypes and cannot flatten every numeric row into one generic expense/income stream.

### Shopee Orders - why this source exists

Shopee is **commerce enrichment**, not a bank ledger. It answers: *what was the money for?* The receipt supplies seller, order, line items, quantities, payment method, shipping, fees, discounts, and total payment. After the financial event is matched, those fields allow spending to be categorized by the actual purchased goods rather than only by merchant/payment description.

The corpus contains:

- 112 unique order receipts
- 32 multi-product receipts
- one two-page receipt
- maximum reported total quantity of 150 in one receipt
- 30 distinct buyer/recipient labels
- 24 receipts containing a `CO Buku` line-item label, a useful **candidate** signal for checkout-for-others but not proof by itself
- observed raw payment methods: 59 `Saldo ShopeePay`, 33 `SeaBank Bayar Instan`, 7 `QRIS`, 7 `Bank BCA`, 4 `ShopeePay`, 2 `Bank Mandiri`

This confirms a required separation between **commerce actor/order owner** and **economic payer/expense owner**.

Required ownership semantics:

- `SELF`: personal purchase and personal payment.
- `THIRD_PARTY_DIRECT`: order made through the user's Shopee account, but another person pays directly. **No personal expense is created.**
- `SELF_REIMBURSABLE`: user pays first for another person. Use pass-through/receivable semantics until settlement; do not treat the final net as ordinary personal spending.
- `MIXED`: one order has personal and third-party line items.
- `UNKNOWN`: unresolved; review required.

The receipt alone cannot decide these states. Match the order to bank/wallet evidence and allow explicit user review/correction.

## 5. Architecture corrections now required before Phase 1

### 5.1 SourceDocument identity

Every source document needs both:

- `content_sha256`
- `natural_document_key`

and also:

- `template_id`
- `template_fingerprint`
- `parser_version`
- `source_registry_id`
- `import_batch_id`
- archive lineage

A byte hash catches exact duplicates. The natural key catches regenerated/saved-again statements with different bytes.

### 5.2 Multiple financial dates

The normalized event must support nullable separate fields:

- `occurred_at`
- `posted_at`
- `settlement_date`

This is required by actual BCA and Stockbit evidence.

### 5.3 Raw versus normalized identity fields

Preserve separately:

- provider transaction id raw
- raw reference
- normalized reference
- raw description
- raw counterparty

Reference normalization is not allowed to destroy source evidence.

### 5.4 Source event role

A normalized record must declare what it is evidence for, for example:

- cash movement
- balance snapshot
- account lifecycle observation
- investment trade
- portfolio snapshot
- commerce order
- item/category enrichment

This prevents blu portfolio rows and Stockbit trade details from being counted as ordinary cash transactions.

### 5.5 Provenance locator

Each normalized output must retain a private locator back to its evidence:

- document id
- page/image
- row/block/region
- parser/template version

A user reviewing an ambiguous event should be able to reach the evidence that produced it.

## 6. Archive/import requirements discovered from the real ZIPs

The master archive contains nested provider ZIPs, while several of the same provider ZIPs also exist independently. Therefore Phase 2 must support safe recursive archive discovery.

Required guards:

- reject zip-slip/path traversal
- archive-depth limit
- file-count limit
- uncompressed-size limit
- suspicious compression-ratio guard
- content SHA dedup before expensive parsing
- natural-key semantic dedup after source detection
- preserve parent archive lineage

Unknown files remain registered as evidence but become `UNKNOWN_TEMPLATE`; they do not fall through to a "closest" parser.

## 7. Fixture and regression strategy

Do not put raw private statements in Git.

Use two layers:

### A. Committed sanitized fixtures

For every source/template family commit small synthetic/sanitized fixtures preserving layout and edge cases:

- normal
- empty
- multiline
- malformed/truncated
- duplicate
- open/partial month where relevant
- source-specific special cases

### B. Private corpus replay

A local ignored corpus replays every private document before adapter release. Store only a sanitized result manifest in test output, not raw source values.

A provider adapter is not considered PASS because one PDF works.

## 8. Dependency decision gate

The current AturUang Python backend is intentionally lightweight. PDF/image ingestion adds a real dependency decision and must be explicit.

Audit result:

- all 228 PDF documents have a usable text layer
- only ShopeePay requires image extraction in this corpus
- BCA requires encrypted-readable PDF support

Before Phase 2 code, benchmark/select one minimal PDF extraction approach against **all nine V1 template families**, then freeze the dependency and parser contract. Do not install multiple overlapping PDF stacks casually.

ShopeePay image parsing is a separate capability and can remain fail-closed/manual-review until its extraction approach is validated.

## 9. Phase -1 PASS criteria

Completed in this audit:

- [x] Library ZIP sources found
- [x] provider bundles materialized for audit
- [x] nested archives inspected
- [x] exact-file SHA dedup measured
- [x] provider/document families identified
- [x] PDF versus image sources identified
- [x] PDF text-layer capability checked
- [x] BCA encryption behavior identified
- [x] template V1 families frozen at contract level
- [x] major source-specific edge cases catalogued
- [x] Shopee economic-payer requirement added
- [x] private-fixture policy defined

Still required before implementation writes:

- [ ] verify current repo/main/SQLite/D1 baseline again
- [ ] create `feature/universal-ingestion-v1` isolated worktree
- [ ] decide/freeze PDF extraction dependency through a corpus benchmark
- [ ] create source coverage matrix in the worktree from this sanitized manifest
- [ ] create sanitized committed golden fixtures
- [ ] create ignored private corpus replay location/harness

## 10. Immediate implementation order

Do **not** start provider parsers next.

Next sequence:

1. Phase 0 baseline verification.
2. Create isolated universal-ingestion worktree.
3. Add source corpus/template documentation and test contracts.
4. Benchmark/freeze PDF extraction engine against the private corpus.
5. Phase 1 Registries + Contracts.
6. Phase 2 Universal Import Foundation with archive safety + document identity.
7. Only then Phase 3 adapters, each requiring golden tests + full private corpus replay.

Pattern Analysis remains separate and provisional until historical financial truth is complete.
