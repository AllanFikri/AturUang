# AturUang Universal Ingestion — Engineering Master Log

**Document purpose:** permanent chronological engineering record for the Universal Ingestion initiative.
**Branch:** `feature/universal-ingestion-v1`
**Worktree:** `C:\A User Main Storage\Documents\GitHub\AturUang-ingestion-v1`
**Production SQLite authority:** `C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db`
**Protected production SQLite SHA-256 baseline:** `2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f`

This document records goals, architecture decisions, implementation checkpoints, failures, repairs, tests, and remaining limitations. It is intentionally sanitized: no raw private financial document contents are stored here.

---

## Operating rules

1. Universal ingestion is developed in its own worktree/branch until integration is proven safe.
2. Production SQLite must remain byte-identical during foundation work unless a later phase explicitly approves a migration.
3. Provider adapters must not mutate the canonical ledger directly.
4. Unknown or changed templates fail closed.
5. Raw private corpus data is never committed.
6. Tests must pass before every implementation commit.
7. Small coherent commits are preferred.
8. Push occurs only after a validated local checkpoint.
9. A debit is not automatically an expense; a credit is not automatically income.
10. Reconciliation and evidence quality outrank convenience.

---

# Chronology

## Phase -1 — Source Corpus & Template Audit

**Goal:** understand the real source corpus before designing parsers.

### Source families identified
- BCA statement
- Jago statement
- SeaBank statement
- blu account mutation
- blu portfolio
- GoPay statement
- ShopeePay image/history evidence
- Stockbit Statement of Account
- Shopee order receipt

### Important architecture findings
- Provider/account identity must not depend on display name.
- Parent/subaccount hierarchy is required.
- Account lifecycle must support `NEW`, `RENAMED`, `CLOSED`, and `REUSED`.
- Source documents need both exact byte identity and semantic/natural identity.
- Multiple financial dates must be preserved separately:
  - occurred date/time
  - posted date/time
  - settlement/due date
- Raw reference/description/counterparty evidence must remain separable from normalized values.
- Source event roles must distinguish cash movement, snapshot, lifecycle evidence, investment trade, commerce evidence, etc.
- Shopee order owner and economic payer are distinct concepts.
- Nested archive lineage must be preserved.
- Unknown future templates must fail closed.

### Private corpus rules
- Raw PDFs/images/ZIPs are excluded from Git.
- Committed fixtures must be sanitized/synthetic.
- Provider release requires private corpus replay, not one successful sample.

---

## Phase 0 — Baseline / Inventory Freeze

**Goal:** freeze a safe implementation baseline.

### Verified baseline
- main repository synchronized
- production SQLite SHA-256 frozen
- D1 shadow database treated as non-authoritative
- isolated worktree created:
  - branch: `feature/universal-ingestion-v1`
  - path: `C:\A User Main Storage\Documents\GitHub\AturUang-ingestion-v1`

### Documentation freeze commit
`b187027 docs(ingestion): freeze corpus and PDF dependency`

### Frozen documentation
- corpus audit
- baseline freeze
- sanitized source manifest
- source coverage matrix
- source template contracts
- stage readiness checklist
- PDF extraction dependency decision
- `requirements-ingestion.txt`

### PDF corpus benchmark
Final complete PDF corpus:
- 228 unique PDFs
- BCA: 19
- blu mutation: 25
- blu portfolio: 25
- GoPay: 7
- Jago: 11
- SeaBank: 19
- Shopee order: 112
- Stockbit: 10

Benchmark result:
- `pypdf 6.16.2`: all 228 documents readable
- `pdfplumber 0.11.10`: all 228 documents readable
- BCA encrypted documents: 19/19 readable by both engines
- raw extracted text was not written to benchmark reports

### Dependency decision
Frozen ingestion dependency:
`pypdf[crypto]==6.16.2`

Reason:
- sufficient corpus coverage
- faster/minimal dependency footprint
- encrypted-readable BCA support
- avoid unnecessary overlapping PDF stacks

---

# Phase 1 — Registries + Contracts

## Phase 1B1 — Pure Contracts

### Commit
`f834de0 feat(ingestion): add universal registry contracts`

### Purpose
Define the vocabulary and invariants before touching a database.

### Concepts represented
- Institution Registry
- Source Registry
- Account identity
- ownership/confidence
- source capabilities
- source templates
- archive lineage
- document identity
- provenance
- import batch
- account lifecycle
- commerce ownership/payer distinctions

### Final review corrections
- exact content SHA dedup is global
- raw provenance locator explicit
- provider/display-name collision covered
- BCA near-real-time email capability representable
- blu/wallet/RDN/SeaBank account shapes representable
- Shopee economic owner separated from payer responsibility

### Tests
22 targeted contract tests PASS.

### Safety
- no `db.py` change
- no legacy ingestion change
- no parser
- no production DB migration
- production SQLite unchanged

---

## Phase 1B2 — SQLite Registry Schema

### Commit
`1e278dc feat(ingestion): add universal registry schema`

### Purpose
Create storage structures for Phase 1 concepts without wiring them into production.

### Important schema protections
- exact SHA and semantic duplicate model
- account lifecycle
- reused provider slot integrity
- source/template/capability registries
- Shopee owner/payer storage
- unresolved account observation support
- temporal legacy-account bridge
- account hierarchy cycle protection
- source-document/import-batch source consistency
- reversible schema drop helper

### Tests
45 Phase 1 targeted tests PASS.

### Brownfield regression
Quick suite PASS.

### Safety
- in-memory/temp SQLite only
- no production DB migration
- no parser
- production SQLite unchanged

---

## Phase 1C — Registry Compatibility Service

### Commit
`6f2df68 feat(ingestion): add registry compatibility service`

### Purpose
Provide safe service-level operations over registries while preserving the legacy ledger.

### Capabilities
- provider-key account resolution
- temporal legacy account bridge
- exact/semantic document classification
- template fail-closed behavior
- source capability query
- hierarchy resolution
- registry-only operations

### Final review corrections
- document template claims cross-validated against registry
- exact SHA short-circuits before template validation
- occurrence replay metadata conflicts fail closed
- observed parent cannot contradict registered hierarchy

### Tests
72 Phase 1 targeted tests PASS.

### Brownfield regression
Quick suite PASS.

### Safety
- no ledger mutation
- no implicit registry migration
- no parser
- production SQLite unchanged

---

## Phase 1 Final Gate

### Result
PASS.

### Verified
- exactly the expected Phase 1 files changed
- `db.py` untouched
- legacy ingestion untouched
- no cloud/D1 change
- 72/72 targeted tests PASS
- brownfield quick regression PASS
- no canonical ledger mutation path
- no parser/OCR implementation
- registry service refuses implicit schema migration
- production SQLite byte-identical

### Audit false positive encountered
Initial final-gate parser scan incorrectly matched:

`re.compile(...)`

as use of `PIL`, because the scan searched `PIL` case-insensitively and found the substring `pil` inside `compile`.

### Repair
Changed audit logic to detect actual parser imports/calls instead of arbitrary substrings.

**Lesson:** safety auditors must themselves be tested for false positives; audit failure is not automatically product-code failure.

---

# Phase 2 — Universal Import Foundation

## Phase 2A — Safe File Discovery Foundation

### Commit
`7628aa2 feat(ingestion): add safe file discovery foundation`

### Purpose
Safely discover source files and archives before provider parsing.

### Capabilities
- folder/file discovery
- ZIP traversal
- nested ZIP traversal
- SHA-256 hashing
- exact byte dedup
- preserve every occurrence of a duplicate artifact
- archive lineage
- extension filtering
- privacy-safe diagnostics

### Archive protections
- zip-slip/path traversal rejection
- archive recursion depth limit
- file-count limit
- single-file size limit
- total uncompressed-size limit
- suspicious compression-ratio guard
- corrupt archive fail-closed
- no extraction of private archive members to disk

### Duplicate semantics
If the same bytes appear as:
- standalone file
- ZIP member
- nested ZIP member

they become:
- one unique artifact
- multiple preserved occurrences

### Tests
96 targeted tests PASS:
- 72 existing Phase 1
- 24 Phase 2A

### Brownfield regression
Quick suite PASS.

### Safety
- no DB dependency
- no ledger mutation
- no provider parser
- production SQLite unchanged

### Audit false positive
The first Phase 2A safety scan repeated the `PIL`/`re.compile` substring mistake.

### Repair
The auditor was changed to inspect explicit imports and calls.

---

## Phase 2B — Source/Template Detection + PDF/Image Preflight

### Commit
`1428b79 feat(ingestion): add source template preflight`

### Purpose
Determine whether a document is structurally safe and whether a known source/template can be claimed before any provider transaction parser runs.

### PDF preflight
- PDF magic check
- extension/magic mismatch detection
- page count
- text-layer availability
- encryption state
- corrupt PDF fail-closed
- limited text extraction only for preflight/template markers
- extracted document text is not returned in preflight contracts

### Image preflight
- PNG/JPEG magic check
- dimensions
- low-resolution flagging
- no OCR claim yet

### Template detection
Status:
- `KNOWN`
- `UNKNOWN_TEMPLATE`
- `TEMPLATE_DRIFT`

Rules:
- exact required markers can identify a known template
- ambiguous multiple exact matches fail closed
- source hint with partial mismatch becomes template drift
- unknown template never falls through to the “closest parser”

### V1 text-marker signatures represented
- BCA
- Jago
- SeaBank
- blu mutation
- blu portfolio
- GoPay
- Stockbit SOA
- Shopee order receipt

ShopeePay image evidence intentionally remains without a known visual-template claim until an image/OCR adapter is validated.

### Dependency drift discovered
Before implementation, local environment contained:
`pypdf 6.6.2`

but frozen dependency required:
`pypdf[crypto]==6.16.2`

### Repair
Local Python environment upgraded and verified:
- Python 3.14.7
- pypdf 6.16.2
- cryptography 46.0.4

Repository and production SQLite remained unchanged during dependency repair.

### Tests
129 targeted tests PASS:
- 96 existing
- 33 Phase 2B

### Brownfield regression
Quick suite PASS.

### Safety
- no OCR
- no transaction parser
- no DB/ledger mutation
- period remains `UNKNOWN` until provider adapter extracts it
- production SQLite unchanged

### Push
Phase 2B commit pushed successfully.
Local/origin synchronization verified `0 0`.

---

# External implementation/reference research checkpoint

Before provider adapters are implemented, public implementations were reviewed as independent engineering references.

Potential references:
- `akhy/idbank` — BCA/Jago/GoPay statement parsing concepts
- `DIRJEN-BOT/docai` — BCA parsing and balance validation concepts
- `benedictjohannes/bca-pdfestatement-extractor` — BCA e-statement extraction edge cases
- `iniakunhuda/bank-parser-indonesia` — Indonesian bank PDF parsing approaches
- `sebastienrousseau/bankstatementparser` — provenance, dedup, review, verification architecture
- `ledgerfeed/bank-statement-pdf-parser` — geometry-aware PDF parsing concepts
- unofficial Gojek/GoPay and Shopee wrappers — research only for possible optional near-real-time/enrichment paths

Policy:
- do not blindly copy or depend on an external parser
- inspect algorithmic ideas and edge cases
- verify license and maintenance before reuse
- keep official/private statement evidence as reconciliation authority
- unofficial APIs, if ever used, are optional provisional evidence, never sole canonical truth

---

# Current checkpoint

**Remote branch:** `feature/universal-ingestion-v1`
**Current validated HEAD:** `1428b79c97d65281a81b314c9c8f30de33ed1237`
**Current phase complete:** Phase 2B
**Production SQLite SHA-256:** `2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f`

Current targeted-test baseline:
- 129 targeted Universal Ingestion tests
- brownfield quick regression PASS

---

# Next planned work

## Phase 2C — Adapter Interface + Normalized Event Envelope

Phase 2 foundation still requires:
- adapter interface
- normalized event envelope
- dry-run result
- diagnostics/provenance integration
- semantic same-statement duplicate foundation

The normalized event must preserve, where available:
- source document identity
- source/provider identity
- provider event ID
- raw/normalized reference
- occurred/posted/settlement dates separately
- amount/currency/direction
- source/destination account hints
- raw/normalized counterparty
- merchant/description
- balance-after
- source event role/event hint
- provider category
- evidence quality
- parser version
- raw-row fingerprint
- provenance locator

Important:
- event hint is not final financial semantics
- adapter output must not create canonical ledger rows
- debit must not automatically become Expense
- credit must not automatically become Income

---

# Remaining Phase 2 gate items

Before Phase 3 Provider Adapters:
- adapter interface PASS
- normalized event envelope PASS
- dry-run result PASS
- diagnostics/provenance PASS
- semantic same-statement duplicate detection PASS
- repeated nested/standalone exact duplicates remain deduplicated
- unknown template remains fail-closed
- private contents remain absent from Git/logs
- no ledger mutation

---

# Known future limitations by design

1. True realtime universal bank synchronization is not guaranteed.
2. Near-real-time evidence can be provisional; statement reconciliation remains authoritative.
3. Cash activity without evidence cannot be reconstructed automatically.
4. Unknown provider formats require a new adapter/template.
5. OCR will be confidence-based and cannot be perfect.
6. Ambiguous ownership/reimbursement/commerce cases can require user review.
7. Missing historical evidence may remain `PARTIAL` or `UNVERIFIABLE`.
8. Financial truth is established through evidence + matching + reconciliation, not by guessing.

---

## Phase 2C-A — Discovery / Preflight Boundary Hardening

### Commit
`4288473 fix(ingestion): harden discovery and preflight boundaries`

### Why this checkpoint was added
A forward integration review before adapter orchestration identified four boundary cases that Phase 2A/2B unit scopes did not yet protect.

### Hardened boundaries
- artifact occurrence now preserves its observed extension
- same exact bytes with conflicting observed extensions remain one content identity but no longer lose occurrence metadata
- duplicate normalized ZIP member paths fail closed
- Unix ZIP symlink members fail closed
- contradictory claimed source versus exact detected template returns `SOURCE_HINT_CONFLICT` and is not adapter-ready

### Independent review
- same-extension duplicate behavior preserved
- cross-extension exact duplicate behavior verified
- no ledger/DB/network/OCR/cloud scope expansion
- exact four-file implementation scope
- 135 Universal Ingestion targeted tests PASS
- brownfield quick regression PASS
- production SQLite byte-identical

### Push
Remote branch synchronized to:
`4288473f3ed096c0964175df30e39f28b4c8f226`

### Lesson
Component tests are not sufficient for a foundation layer. Before a later phase depends on an earlier layer, boundary/adversarial integration behavior must be reviewed explicitly.

---

## Phase 2C-B — Forward Design Freeze

Before implementing the adapter interface, the complete forward contract was frozen in:

`docs/ingestion/PHASE2C_B_FORWARD_DESIGN_GATE_2026-08-28.md`

Key design decision:
the universal adapter boundary carries normalized **evidence**, not canonical transactions. Cash movement, balance snapshot, source summary, account observation, investment trade, and commerce order are distinct typed evidence roles under a common envelope.

Phase 3 remains locked.
