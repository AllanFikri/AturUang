# Phase 3 BCA Forward Design Gate — Universal Monthly Statement Adapter

Date: 2026-08-29
Branch: feature/universal-ingestion-v1
Validated Phase 2 exit HEAD: e20b73510e223564a35b8cb08ac80e557ec4139d
Production SQLite protected SHA-256: 2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f
Status: DESIGN FROZEN BEFORE BCA IMPLEMENTATION. IMPLEMENTATION REMAINS LOCKED UNTIL INDEPENDENT DESIGN REVIEW PASSES.

## 1. Purpose

Phase 3 begins provider-specific parsing on top of the completed Phase 2 Universal Import Foundation.

The first provider is BCA.

This design freezes the BCA monthly-statement adapter boundary before any provider parser code is written.

The adapter converts verified BCA PDF statement evidence into universal normalized evidence only.

It does not:
- write the ledger
- create canonical transactions
- infer Expense or Income
- resolve owned transfers
- reconcile a month
- mutate account balances
- write registries
- use OCR
- use network calls
- reuse the legacy BCA email parser

The provider adapter remains evidence extraction only.

## 2. Frozen source authority

Adapter descriptor:

- adapter_id: bca-monthly-statement-v1
- source_registry_id: bca_statement
- template_id: bca_monthly_statement_v1
- parser_version: parser-v1
- source_channel: PDF

The source/template authority is the Phase 2 preflight and registry path.

The BCA adapter never claims authority by:
- filename
- folder name
- extension alone
- provider display name
- fuzzy marker match
- legacy parser availability

The exact BCA preflight marker set remains:

- REKENING TAHAPAN XPRESI
- NO. REKENING
- PERIODE
- TANGGAL KETERANGAN CBG MUTASI SALDO

The frozen Phase 2 template fingerprint is:

75dfe6b0d68ce4a4312f258536edaa86a28535bd0d1d0f9ef6fb22e4858d574c

## 3. Audited BCA corpus facts

The sanitized source contract records:

- 19 BCA PDF statements
- coverage 2025-01 through 2026-07
- page range 4 through 23
- text layer available
- encrypted but readable PDFs
- statement rows with posting-date evidence
- separate effective or transaction date when present
- multiline descriptions and references
- optional running balance
- mixed DB/CR direction evidence
- Poket lifecycle language
- separate fee rows

The private 19-PDF corpus remains outside Git.

A provider implementation is not push-ready until all 19 audited BCA PDFs have been replayed through the adapter in a privacy-safe local gate.

## 4. Exact implementation scope

The BCA implementation is frozen to exactly three new files:

1. aturuang/ingestion_bca_adapter.py
2. tests/test_universal_ingestion_phase3_bca.py
3. tests/fixtures/ingestion/bca_statement_v1.json

No Phase 2 core module is modified during the initial BCA adapter implementation.

Specifically forbidden from the BCA implementation commit:

- aturuang/ingestion_contracts.py
- aturuang/ingestion_registry.py
- aturuang/ingestion_registry_service.py
- aturuang/ingestion_discovery.py
- aturuang/ingestion_preflight.py
- aturuang/ingestion_adapter.py
- aturuang/ingestion_orchestration.py
- aturuang/ingestion.py
- aturuang/db.py
- cloud/*
- Pattern worktree files
- UI files
- D1 schema or writes

If the BCA adapter cannot be implemented inside the existing universal contracts, implementation stops and the design must be reopened explicitly.

## 5. Input contract

The adapter accepts AdapterInput only.

Required input properties:

- source_registry_id is bca_statement
- template_id is bca_monthly_statement_v1
- parser_version is parser-v1
- source_channel is PDF
- template_match_status is KNOWN
- binary_payload is present
- text_payload is absent
- structured_payload is absent

The adapter never opens a source path.

All parsing is performed from AdapterInput.binary_payload.

This keeps archive replay, file integrity, and path safety under the Phase 2 orchestration boundary.

## 6. PDF reading rule

The implementation uses pypdf only.

No OCR.

No external command.

No network.

No alternative PDF engine fallback in the first BCA adapter.

Parsing sequence:

1. construct PdfReader from BytesIO(binary_payload)
2. if encrypted, attempt the same empty-password decrypt path accepted by Phase 2 preflight
3. reject or fail closed if the PDF cannot be read
4. extract text from all pages
5. preserve page-local ordering for provenance
6. never write extracted private text to disk
7. never place extracted private text into ordinary logs or diagnostics

A locked, corrupt, empty, or structurally unreadable PDF produces a privacy-safe FAILED adapter result when tested directly.

In normal orchestration, those media states should already have been stopped by preflight.

## 7. Parser structure

The parser is a deterministic line-state parser over extracted statement text.

The implementation separates four document concerns:

A. header and document identity
B. statement summary
C. transaction rows and continuation lines
D. optional Poket lifecycle evidence

The parser does not use one monolithic regular expression over the whole PDF.

### 7.1 Header

The header parser extracts:

- statement period
- account identity evidence

The account identity is private evidence.

The raw account number is never included in:
- public diagnostics
- safe dry-run serialization
- natural_document_key_candidate
- repr output

### 7.2 Statement summary

Opening and closing balances are emitted as SourceSummaryEvidence.

They are not emitted as fake cash movements.

The initial BCA adapter may also emit BalanceSnapshotEvidence only when the source contains an explicit independently identifiable balance snapshot.

The adapter does not perform final reconciliation.

Statement-equation reconciliation belongs to Phase 7.

### 7.3 Transaction rows

A transaction row begins only when a row-start grammar is confidently identified.

Continuation lines attach only to the current open row and stop at:
- the next confident transaction-row start
- a known section boundary
- the document footer
- end of page/document

Multiline description and reference text is preserved as private evidence.

The parser must not silently merge two rows merely because text extraction wrapped.

### 7.4 Row completion

A trusted cash-movement event requires:
- a valid posting-date interpretation
- a finite Decimal amount
- one unambiguous direction interpretation

A running balance is optional.

Reference and counterparty are optional.

Missing optional fields do not fabricate values.

An incomplete or ambiguous row produces a privacy-safe review diagnostic and is not converted into a falsely trusted cash event.

## 8. Date semantics

BCA date evidence remains separated.

### posted_at

The statement transaction row posting date maps to posted_at.

If the printed row omits a year, the parser may combine the row date with the statement-period year only when the result is unambiguous.

No current system clock is used.

### occurred_at

An effective or transaction date maps to occurred_at only when a separate date is explicitly present in the source evidence.

If no distinct effective date is printed:

occurred_at = None

The adapter must never copy posted_at into occurred_at merely to fill the field.

If multiple effective-date candidates conflict, the row requires review.

### settlement_date

The first BCA monthly-statement adapter does not invent a settlement date.

settlement_date remains None unless an explicit source field later proves it.

## 9. Amount and direction semantics

All monetary parsing uses Decimal directly.

Float is forbidden.

The adapter preserves:

- amount
- direction
- direction_raw
- raw description
- raw reference
- optional running balance

Direction precedence:

1. explicit statement DB/CR column evidence
2. an exact provider direction token demonstrated by the frozen fixture/corpus
3. otherwise ambiguous

If explicit sources conflict, the row requires review.

The adapter maps only source direction:

- debit-like evidence -> EventDirection.OUTFLOW
- credit-like evidence -> EventDirection.INFLOW

This is not financial semantics.

OUTFLOW is not automatically Expense.

INFLOW is not automatically Income.

## 10. Source status

BCA monthly-statement transaction rows are treated as posted evidence only when the statement format itself represents finalized statement rows.

The initial adapter does not infer:
- PENDING
- FAILED
- REFUNDED
- REVERSED

unless a future BCA source contract explicitly contains such status evidence.

For the frozen monthly statement format, trusted transaction rows use SourceEventStatus.POSTED.

## 11. Reference and native event identity

The BCA statement capability does not promise a provider-native event ID.

Therefore:

source_event_id = None

provider_transaction_id_raw remains None unless a field is explicitly proven to be a native transaction identifier by source evidence.

reference_raw may be populated from statement reference evidence.

reference_raw is never promoted automatically into source_event_id.

Reference alone never defines row identity.

## 12. Row fingerprint

row_fingerprint is SHA-256 over a deterministic multi-field private row representation.

The input representation must include more than reference alone and, where available, includes:

- posting date
- distinct occurred date
- direction_raw
- Decimal amount canonical string
- normalized multiline description
- normalized multiline reference
- optional running balance
- deterministic row occurrence position within the document

The fingerprint is deterministic across repeated parsing of the same document.

The raw private values are not logged.

The fingerprint is evidence-local and is not the semantic-document hash.

## 13. Natural document key

The BCA natural document key is deterministic and privacy-preserving.

Account normalization:

1. Unicode NFKC
2. trim
3. collapse whitespace
4. remove presentation-only spaces, dots, and hyphens
5. preserve all other source characters
6. encode UTF-8

Then compute full SHA-256 of the normalized private account identifier.

Frozen key shape:

bca_statement:<account_sha256>:<YYYY-MM>

The raw account number must not appear in the key.

If either:
- account identity is unavailable
- statement period is unavailable
- account identity is structurally ambiguous

natural_document_key_candidate is None and the adapter requires review.

No filename, path, display name, or current time participates in the natural key.

## 14. Period status

A valid audited BCA monthly statement represents one closed statement period.

When the period is confidently parsed from the document:

period_status = CLOSED

period_start and period_end are derived deterministically from the printed statement month.

No current clock is used.

If the printed period cannot be interpreted unambiguously:

- period_status remains UNKNOWN
- natural_document_key_candidate is None
- parse_status is REVIEW_REQUIRED unless the document is structurally unreadable

## 15. Evidence roles

The initial BCA adapter is limited to universal evidence roles already present in Phase 2.

### CASH_MOVEMENT

Used for trusted statement transaction rows.

Fields may include:

- amount
- currency IDR
- direction
- status POSTED
- posted_at
- occurred_at when separately printed
- description_raw
- counterparty_raw when confidently extracted
- reference_raw
- balance_after when printed
- direction_raw
- controlled event_hint where useful

### SOURCE_SUMMARY

Used for statement-level balance evidence:

- currency IDR
- period_start
- period_end
- opening_balance
- closing_balance

Incoming/outgoing totals remain None unless the BCA statement explicitly supplies trusted statement-level totals.

### BALANCE_SNAPSHOT

Optional.

Emitted only for an explicit source snapshot that is distinct from the SourceSummary representation.

The adapter does not duplicate the same opening/closing balance as both summary and snapshot without an explicit reason.

### ACCOUNT_OBSERVATION

Used only when a stable provider account/Poket identifier is explicit in source evidence.

Display name alone is insufficient.

No stable key means no fabricated account observation.

## 16. Poket lifecycle rule

BCA Poket language is evidence, not final account lifecycle authority.

Controlled event_hint values may identify a parser-observed phrase family such as:

- BCA_POKET_INITIAL_FUNDING
- BCA_POKET_TOP_UP
- BCA_POKET_MOVE
- BCA_POKET_SCHEDULED_TRANSFER

The original phrase remains private source evidence.

A Poket ACCOUNT_OBSERVATION is emitted only when the source explicitly supplies a stable provider key or stable slot identifier.

If a Poket lifecycle phrase is detected but the only identifier is a display name:

- do not invent observed_provider_account_key
- do not hash the display name and pretend it is provider identity
- add a privacy-safe review diagnostic
- set AdapterResult.parse_status to REVIEW_REQUIRED

Account lifecycle resolution itself remains Phase 4.

## 17. Separate fee rows

A separate fee row remains a separate CASH_MOVEMENT evidence event.

The BCA adapter does not merge:
- transfer amount
- admin fee
- service fee

into one amount merely because they are adjacent or related.

Downstream matching/semantics may associate them later.

## 18. Counterparty parsing

Counterparty extraction is conservative.

Only a source segment confidently attributable to counterparty evidence is assigned to counterparty_raw.

If the text could be:
- reference
- memo
- bank routing detail
- branch text
- free-form description

the adapter preserves it in description/reference evidence rather than guessing a counterparty.

No normalized merchant/category semantics are assigned in Phase 3 BCA.

## 19. Diagnostics and privacy

SafeDiagnostic messages contain only generic parser state.

Forbidden in diagnostic messages:

- raw account number
- raw reference
- raw transaction ID
- counterparty
- raw description
- raw statement text
- absolute path
- email
- credentials
- full private locator

Allowed diagnostic examples:

- BCA_HEADER_INCOMPLETE
- BCA_PERIOD_AMBIGUOUS
- BCA_ROW_DIRECTION_AMBIGUOUS
- BCA_ROW_AMOUNT_INVALID
- BCA_ROW_DATE_AMBIGUOUS
- BCA_POKET_IDENTITY_UNSTABLE
- BCA_STRUCTURE_TRUNCATED

locator_token, when used, is opaque and derived from safe hashing.

No raw private value is required to understand a diagnostic.

## 20. Adapter result policy

### COMPLETED

Allowed only when:
- document is structurally readable
- period is unambiguous
- account identity is available
- natural key is available
- all emitted events satisfy their typed contracts
- no unresolved row/Poket ambiguity requires human review

### REVIEW_REQUIRED

Used when structurally readable evidence contains ambiguity, including:
- missing natural document identity
- ambiguous row direction
- ambiguous row date
- truncated row
- Poket lifecycle without stable provider identity
- conflicting structural evidence

Trusted independent rows may still be emitted if their evidence is unaffected.

### FAILED

Used for direct-adapter failures such as:
- corrupt PDF
- locked unreadable PDF
- no readable statement text
- impossible adapter contract
- parser internal structure cannot be safely interpreted

FAILED results contain no trusted events.

## 21. Determinism

Repeated parse of identical AdapterInput bytes must produce byte-equivalent public result semantics.

Forbidden inputs to deterministic identity:
- current time
- random UUID
- absolute path
- file modification time
- filename
- process ID

Event ordering follows source statement order.

Semantic-document hashing later treats document events as a multiset according to the frozen Phase 2 contract.

## 22. Synthetic fixture strategy

The committed fixture file is:

tests/fixtures/ingestion/bca_statement_v1.json

It contains sanitized case definitions and expected evidence only.

It contains no real account number, real reference, real counterparty, or copied private statement text.

The Phase 3 BCA test module creates minimal text-layer PDF bytes in memory from the sanitized case text using a test-only deterministic PDF builder.

No additional PDF-generation dependency is added.

Encrypted unit fixtures are derived in memory from sanitized PDF bytes using pypdf.

This preserves the exact three-file implementation scope.

## 23. Sanitized fixture cases

The JSON fixture pack must include named synthetic/sanitized cases covering at least:

- normal_statement
- encrypted_readable_statement
- multiline_description
- multiline_reference
- effective_date_distinct
- no_effective_date
- optional_running_balance
- no_running_balance
- debit_row
- credit_row
- ambiguous_direction
- separate_fee_row
- poket_initial_funding
- poket_top_up
- poket_move
- poket_scheduled_transfer
- poket_missing_stable_key
- malformed_or_truncated
- duplicate_reference_different_row
- deterministic_reparse

Expected values use non-private fake identifiers only.

## 24. Mandatory 30 committed tests

Exactly 30 new committed BCA tests are frozen for the initial implementation:

1. exact adapter descriptor and PDF channel
2. AdapterInput binary-payload contract only
3. sanitized normal statement parses COMPLETED
4. encrypted-readable sanitized PDF parses
5. locked/corrupt PDF fails closed
6. statement period and CLOSED status
7. natural key deterministic and raw account number absent
8. opening balance evidence
9. closing balance evidence
10. debit row maps to OUTFLOW
11. credit row maps to INFLOW
12. posting date preserved
13. distinct embedded effective date preserved
14. no effective date does not invent occurred_at
15. multiline description continuation
16. multiline reference continuation
17. running balance parsed when printed
18. missing running balance allowed
19. ambiguous DB/CR direction requires review
20. reference is not sole row identity
21. separate fee row remains separate event
22. Poket initial-funding lifecycle evidence
23. Poket top-up lifecycle evidence
24. Poket move lifecycle evidence
25. Poket scheduled-transfer lifecycle evidence
26. missing stable Poket key does not fabricate identity
27. malformed/truncated statement gives privacy-safe review/failure
28. repeated parse is deterministic and idempotent
29. exact AdapterCatalog plus orchestration dry-run integration
30. repr/diagnostics/static boundary leak no private data and use no ledger/network/OCR/legacy parser

Target Universal Ingestion test count after implementation:

241

The current Phase 3 entry baseline is:

211

## 25. Integration test rule

Test 29 must prove the real BCA adapter can be inserted into AdapterCatalog and selected only by the exact tuple:

- bca_statement
- bca_monthly_statement_v1
- parser-v1
- PDF

The orchestration integration test must prove:

- verified binary payload reaches the BCA adapter
- exact catalog selection succeeds
- resulting AdapterResult returns through dry-run
- no registry write occurs
- no ledger write occurs

The test must not bypass orchestration by invoking hidden internal state.

## 26. Private corpus replay gate

Private replay occurs after the committed 30 tests pass and before the BCA checkpoint is pushed/released.

The replay gate must exercise all 19 audited BCA PDFs.

It must report only aggregate and privacy-safe metadata, for example:

- document count
- completed count
- review-required count
- failed count
- event counts by EventRole
- period coverage
- deterministic repeat comparison
- safe diagnostic codes

It must not report:

- account number
- raw transaction text
- reference
- counterparty
- raw statement description
- absolute private path

The first BCA implementation is not push-ready if any private document:
- crashes the parser
- leaks private data
- silently drops structurally recognized rows
- fabricates direction/date/account identity
- violates deterministic output

Any discovered format variation must be converted into a sanitized regression case before the provider checkpoint can be finalized.

## 27. Brownfield boundary

The BCA adapter must not import or call:

- BCAEmailParser
- BaseProviderParser from legacy ingestion
- legacy staging schema helpers
- transaction mutation services

The historical BCA email parser remains a separate EMAIL evidence path.

A PDF statement is authoritative statement evidence and must not inherit legacy email assumptions such as:
- credit equals Income
- debit equals Expense
- default categories
- default account display names

## 28. No Phase 4 authority

The BCA adapter may emit observed account evidence.

It does not:
- create registry accounts
- assign ownership
- decide NEW, RENAMED, CLOSED, or REUSED
- merge Poket identities
- bridge legacy account names

Those responsibilities remain Phase 4 Account Discovery.

## 29. No Phase 5 or Phase 6 authority

The BCA adapter does not decide:

- same-event matching
- owned transfer matching
- duplicate economic events
- Expense
- Income
- Internal Transfer
- Refund
- Receivable
- Investment semantics

It emits source evidence only.

## 30. No Phase 7 reconciliation authority

The adapter may preserve:
- opening balance
- closing balance
- running balance evidence

It does not certify a closed month as reconciled.

Final statement equation and monthly close remain Phase 7.

## 31. Implementation acceptance criteria

The BCA implementation checkpoint may be created locally only if all are true:

- exact three-file implementation scope
- no Phase 2 core changes
- 30 BCA tests PASS
- 241 targeted Universal Ingestion tests PASS
- brownfield quick regression PASS
- production SQLite byte-identical
- no OCR/network/ledger/legacy-parser authority
- deterministic fixture replay
- privacy-safe diagnostics and repr
- worktree clean after commit
- no push yet

## 32. Post-implementation review sequence

After local BCA implementation commit:

1. independent implementation review
2. adversarial synthetic probe
3. full private 19-PDF replay
4. repair only if evidence demands it
5. rerun 241 targeted tests
6. rerun brownfield quick regression
7. verify production SQLite hash
8. push BCA checkpoint only after all gates pass

Jago implementation remains locked until the BCA provider checkpoint is fully reviewed.

## 33. Design decision summary

The BCA adapter is deliberately conservative.

It extracts trusted statement evidence, preserves raw/private evidence boundaries, and refuses to guess when BCA text is structurally ambiguous.

The adapter is the first provider-specific implementation and therefore establishes the pattern for later provider adapters:

- exact template authority
- private payload only
- deterministic parsing
- typed normalized evidence
- no financial semantics
- no ledger mutation
- privacy-safe review
- synthetic regression fixtures
- mandatory full private corpus replay

Implementation remains locked until this design document passes independent review.
