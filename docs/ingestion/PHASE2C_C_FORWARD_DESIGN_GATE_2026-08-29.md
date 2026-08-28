# Phase 2C-C Forward Design Gate — Dry-Run Orchestration and Semantic Document Identity

Date: 2026-08-29
Branch: feature/universal-ingestion-v1
Validated baseline HEAD: 580c9617c5c1b81d20a9a6f54657fb3fdbeb8a16
Production SQLite protected SHA-256: 2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f
Status: DESIGN FROZEN BEFORE 2C-C IMPLEMENTATION. PHASE 3 REMAINS LOCKED.

## 1. Purpose

Phase 2C-C connects the Phase 2 foundation into one deterministic, read-only dry-run pipeline.

The target flow is:

discovery
→ safe artifact payload access
→ content-only preflight evidence
→ source-claim reconciliation
→ registry template authority
→ exact adapter selection
→ normalized evidence envelopes
→ natural document identity candidate
→ semantic document fingerprint
→ read-only document identity classification
→ dry-run document result
→ dry-run batch result

This phase does not create canonical financial events and does not mutate the ledger.

## 2. Internal implementation split

Phase 2C-C is intentionally split into three scoped implementation checkpoints.

### 2C-C1 — Safe Artifact Payload Access

Purpose:
- safely rehydrate bytes for a discovered artifact
- support standalone files and nested ZIP members
- verify payload integrity against discovery evidence
- close the remaining local-directory symlink visibility gap

Expected scope:
- aturuang/ingestion_discovery.py
- tests/test_universal_ingestion_phase2_discovery.py

No orchestration code yet.

### 2C-C2 — Read-Only Dry-Run Orchestration

Purpose:
- separate content detection from caller/source claims
- confirm registry authority without write calls
- select one exact adapter
- execute synthetic or later provider adapters through the 2C-B interface
- collect safe dry-run results

Expected scope:
- new aturuang/ingestion_orchestration.py
- minimal read-only helper additions in aturuang/ingestion_registry_service.py
- new tests/test_universal_ingestion_phase2_orchestration.py

No semantic document hash yet.

### 2C-C3 — Semantic Document Identity and Final Dry-Run Result

Purpose:
- compute semantic-document-v1 fingerprints
- classify semantic duplicates, revisions/conflicts, and ambiguity
- aggregate deterministic batch diagnostics
- close the full Phase 2 readiness gate

Expected scope:
- aturuang/ingestion_orchestration.py
- tests/test_universal_ingestion_phase2_orchestration.py
- only minimal contract additions if an immutable result type cannot live cleanly in orchestration

No provider parser files.

## 3. Non-negotiable side-effect boundary

The normal Phase 2C-C dry-run path is read-only.

Forbidden from the dry-run call graph:
- create_or_get_import_batch
- register_source_document
- record_source_document_occurrence
- account observation writes
- registry INSERT, UPDATE, DELETE, or schema initialization
- legacy transaction writes
- account balance mutation
- reconciliation writes
- canonical event writes
- D1/cloud writes
- network calls
- OCR
- provider-specific parser implementation

Existing registry write helpers remain valid for later explicit staging/apply work, but 2C-C dry-run must never call them.

Dry-run means no persistent state mutation.

## 4. Current foundation facts that drive the design

Discovery currently returns metadata, SHA-256 identity, occurrence locators, observed extensions, and archive lineage, but it intentionally does not retain source bytes.

Therefore orchestration cannot safely parse nested ZIP evidence until a dedicated payload-access bridge exists.

Discovery already:
- deduplicates exact content by SHA-256
- preserves every occurrence
- rejects duplicate normalized ZIP member paths
- rejects ZIP symlink members
- applies archive depth, file-count, size, and compression-ratio limits
- does not extract archive contents to disk

One remaining visibility gap must be closed before orchestration:
a symlinked local directory encountered during recursive discovery must be explicitly reported as skipped and must never be traversed silently.

## 5. Safe artifact payload access contract

2C-C1 adds one public payload access operation conceptually equivalent to:

read_discovered_artifact(root, artifact, policy) -> bytes

The exact final name may differ, but the contract is frozen.

Requirements:
- accepts a discovery root plus one DiscoveredArtifact
- chooses one deterministic occurrence only after occurrence metadata is proven non-conflicting
- never trusts an absolute locator supplied from outside discovery
- never extracts ZIP content to disk
- rejects symlinked root, symlinked standalone file, and symlinked local directory traversal
- validates normalized member paths again during replay
- validates archive-depth and safety policy again during replay
- verifies each archive hop against recorded lineage when applicable
- verifies final byte length equals DiscoveredArtifact.size_bytes
- verifies final SHA-256 equals DiscoveredArtifact.content_sha256
- returns bytes only after all checks pass

Payload access must fail closed on:
- missing source occurrence
- locator traversal attempt
- archive member path mismatch
- archive-hop SHA mismatch
- final size mismatch
- final SHA mismatch
- changed file after discovery
- archive safety violation
- unreadable source

No raw absolute path may appear in ordinary diagnostics.

## 6. Local symlink directory rule

During directory discovery:

- symlinked files remain skipped
- symlinked directories must also remain skipped
- symlinked directories must generate an explicit safe diagnostic such as SYMLINK_DIRECTORY_SKIPPED
- recursive traversal must not follow the directory target
- diagnostic output uses an opaque locator token, not an absolute path

This closes the known discovery visibility debt before orchestration.

## 7. Conflicting occurrence extension rule

If one exact content SHA is observed under more than one extension, artifact.extension is empty by design.

2C-C must not choose an arbitrary extension.

A mixed-extension exact-content artifact becomes REVIEW_REQUIRED with a safe code equivalent to CONFLICTING_OCCURRENCE_EXTENSIONS.

No preflight and no adapter execution occurs automatically for that artifact.

This preserves the Phase 2C-A fail-closed boundary.

## 8. Content detection is not source authority

The preflight detector currently supports a source_hint input, but 2C-C orchestration must not use that parameter as authority.

Normal orchestration rule:
- call content preflight without source_hint
- treat TemplateDetection as content evidence only
- reconcile any caller/source claim separately
- confirm the final source/template/parser tuple through registry authority

This prevents the content detector from becoming a second registry.

A claim conflict produces REVIEW_REQUIRED and no adapter execution.

A claimed known source with no exact content template is also REVIEW_REQUIRED.

Unknown template and template drift never reach an adapter.

The registry remains the final template authority.

## 9. Read-only registry authority interface

2C-C2 may add minimal read-only registry service helpers.

Allowed read-only capabilities:
- resolve active template authority
- look up an existing document by exact content SHA
- retrieve existing document identity candidates for the same source plus natural key
- return immutable/read-only identity data needed for classification

Forbidden:
- registration
- occurrence writes
- import batch creation
- transaction control hidden inside a read helper

Read-only helpers must use SELECT only.

The orchestration module must not require registry schema initialization.

If registry authority is unavailable or ambiguous, the document becomes REVIEW_REQUIRED or the batch fails closed according to the failure class.

## 10. Exact duplicate short-circuit

Exact byte duplicate identity is checked before payload rehydration when a read-only registry authority is available.

If content SHA already maps to one existing source document:
- dry-run disposition is SKIPPED_DUPLICATE
- identity status is EXACT_DUPLICATE
- the existing source_document_id is referenced
- no preflight is required
- no adapter is executed
- all newly discovered occurrences remain visible in the dry-run result
- no occurrence row is written

If the same exact content SHA maps ambiguously to conflicting existing document identities, fail closed.

Exact SHA identity remains global before source classification.

## 11. Source claim reconciliation

A source claim is optional transport/user metadata, not parser truth.

Claim reconciliation inputs:
- optional claimed source_registry_id
- content-only TemplateDetection
- registry template authority

Rules:
- no claim + one exact content match: continue to registry confirmation
- matching claim + exact content match: continue
- conflicting claim + exact content match: REVIEW_REQUIRED
- claim + no exact content template: REVIEW_REQUIRED
- no claim + no exact content template: REVIEW_REQUIRED
- ambiguous content signatures: REVIEW_REQUIRED

No fuzzy or nearest-template adapter selection is allowed.

## 12. Exact adapter catalog

2C-C2 introduces an in-memory adapter catalog.

The exact selection key is:

source_registry_id
+ template_id
+ parser_version
+ SourceChannel

Requirements:
- zero matches: REVIEW_REQUIRED with ADAPTER_NOT_AVAILABLE
- exactly one match: execute
- more than one match: internal invariant failure
- no closest-match selection
- no provider name fallback
- no filesystem-extension fallback
- descriptor/input identity must still pass validate_adapter_input

Phase 3 later populates this catalog with real provider adapters.

Phase 2C-C tests use synthetic adapters only.

## 13. Dry-run source_document_id

A new artifact needs a deterministic temporary identity before adapter execution.

Freeze a deterministic dry-run document ID derived from content identity, not filename.

Requirements:
- same exact content produces the same dry-run ID
- different content produces a different dry-run ID
- the value is excluded from semantic-document fingerprinting
- if read-only exact-duplicate lookup finds an existing persistent source_document_id, the existing ID takes precedence in the dry-run result

No random filename-based identity.

No display-name-based identity.

## 14. Natural document key candidate

AdapterResult.natural_document_key_candidate remains the provider-facing source of natural identity evidence.

Rules:
- natural key is optional in dry-run
- orchestration must never invent one from filename alone
- display name alone is not sufficient
- normalized reference alone is not sufficient
- a statement key should use provider-native account identity plus period when available
- an order receipt should use the provider-native order identity
- an image/screenshot with insufficient provider identity may legitimately have no natural key

If semantic duplicate classification requires a natural key but none is available, identity status remains AMBIGUOUS and disposition becomes REVIEW_REQUIRED.

## 15. Semantic document fingerprint purpose

The semantic fingerprint answers:

Do two different byte documents from the same source identity describe the same normalized source document?

It is not:
- exact byte identity
- row identity
- cross-source economic-event identity
- canonical financial-event identity

Cross-source matching remains Phase 5.

## 16. semantic-document-v1 canonicalization

2C-C3 defines one central semantic fingerprint algorithm.

Version tag:
semantic-document-v1

Hash:
SHA-256 of deterministic UTF-8 canonical JSON.

The canonical document includes:
- semantic fingerprint version
- source_registry_id
- normalized natural document key when available
- PeriodStatus
- period_start
- period_end
- canonicalized normalized evidence events as a multiset

The canonical document excludes:
- content_sha256
- source_document_id
- import_batch_id
- template_id
- template_fingerprint
- parser_version
- source locator
- archive locator
- private provenance
- diagnostics
- confidence values
- row_fingerprint
- adapter implementation identity

This allows different bytes and harmless parser-layout metadata to produce the same document semantic fingerprint when normalized evidence is equivalent.

## 17. Canonical scalar normalization

For comparison-only semantic hashing:

Strings:
- Unicode NFKC normalization
- trim leading/trailing whitespace
- collapse internal whitespace
- casefold when the field is comparison-insensitive
- preserve meaningful provider-native identifiers without destructive punctuation removal unless that field already has an explicit normalized counterpart

Decimal:
- must already be finite Decimal
- canonical numeric string removes insignificant trailing zeros
- negative zero canonicalizes to zero
- no binary float conversion

Dates/timestamps:
- preserve the parsed source precision
- date-only stays date-only
- timestamp stays timestamp
- do not invent timezone data

Enums:
- canonicalize by enum value

None:
- explicit JSON null

## 18. Canonical event representation

All six evidence roles must be supported:
- CASH_MOVEMENT
- BALANCE_SNAPSHOT
- SOURCE_SUMMARY
- ACCOUNT_OBSERVATION
- INVESTMENT_TRADE
- COMMERCE_ORDER

Each canonical event includes:
- EventRole
- all semantically relevant typed payload fields
- provider-native transaction/reference evidence when the payload exposes it
- typed payment and amount components
- commerce line items
- trade quantity/price/components
- snapshot/summary values

Each canonical event excludes:
- provenance
- diagnostics
- confidence
- row_fingerprint
- source_event_id when it is merely adapter row identity

Canonical event JSON values are sorted by key.

The document event collection is sorted by canonical event representation before hashing.

Identical duplicate events remain duplicated in the list so multiplicity changes the fingerprint.

Input row order alone must not change the fingerprint.

## 19. Document identity classification after parsing

Classification precedence:

1. exact content SHA match
2. same source + same natural key + same semantic SHA
3. same source + same natural key + different semantic SHA
4. incomplete identity evidence

Map to existing DocumentIdentityStatus:

EXACT_DUPLICATE:
same bytes; short-circuited before parse when possible.

SEMANTIC_DUPLICATE:
different bytes, same source/natural identity, same semantic fingerprint.

REVISION_OR_CONFLICT:
same source/natural identity, different semantic fingerprint, with sufficient semantic evidence.

AMBIGUOUS:
natural key absent, semantic evidence unavailable, or existing identity state is incomplete.

NEW:
no exact or natural identity collision.

No automatic canonical financial event is created for any status.

## 20. Dry-run disposition

Identity status and execution disposition remain separate.

DryRunDisposition values:

READY_FOR_STAGING
SKIPPED_DUPLICATE
REVIEW_REQUIRED
FAILED

Mapping:

NEW + successful adapter result:
READY_FOR_STAGING

EXACT_DUPLICATE:
SKIPPED_DUPLICATE

SEMANTIC_DUPLICATE:
SKIPPED_DUPLICATE

REVISION_OR_CONFLICT:
REVIEW_REQUIRED

AMBIGUOUS:
REVIEW_REQUIRED

Unknown template, source claim conflict, no adapter, unsupported media:
REVIEW_REQUIRED

Integrity violation or internal invariant failure:
FAILED

Dry-run never performs the staging step itself.

## 21. Dry-run document result

Each result must preserve:

- content_sha256
- size_bytes
- every discovery occurrence
- safe locator tokens
- observed extensions
- preflight summary when executed
- content-detected source/template evidence
- reconciled source claim status
- registry authority result
- selected adapter descriptor when executed
- adapter parse status
- normalized event envelopes
- natural_document_key_candidate
- semantic_sha256 when computable
- DocumentIdentityStatus
- related existing document ID when applicable
- DryRunDisposition
- safe diagnostics

Raw payload bytes are not stored in the result.

Private raw provenance may remain inside normalized event envelopes for in-memory review, but ordinary repr/log surfaces remain redacted.

## 22. Dry-run batch result

The batch result is deterministic and immutable.

Required summary fields:
- unique artifact count
- occurrence count
- exact duplicate count
- semantic duplicate count
- ready-for-staging count
- review-required count
- failed count
- ordered document results
- ordered safe diagnostics
- overall status

Overall status:
FAILED if any batch-fatal integrity/invariant error occurred.

REVIEW_REQUIRED if no batch-fatal error occurred but one or more documents require review or failed provider parsing.

COMPLETED only when every non-duplicate document is ready for staging and there are no review-required documents.

Duplicate-only batches may be COMPLETED.

## 23. Failure containment

Batch-fatal:
- archive safety violation during discovery or replay
- source changed after discovery
- SHA/size integrity mismatch
- ambiguous exact-SHA registry identity
- duplicate adapter catalog key
- impossible internal role/payload invariant

Document-level review:
- unknown template
- template drift
- source claim conflict
- conflicting observed extensions
- unsupported preflight media
- adapter unavailable
- natural key absent where identity cannot be resolved
- semantic identity ambiguity

Adapter parse_status FAILED:
- document disposition FAILED
- overall batch status REVIEW_REQUIRED unless the failure indicates an internal invariant/integrity error

Continue processing independent documents after ordinary document-level review failures.

## 24. Diagnostics and privacy aggregation

Every diagnostic has a stage:

DISCOVERY
PAYLOAD
PREFLIGHT
AUTHORITY
ADAPTER
IDENTITY
BATCH

Ordinary diagnostics may contain:
- stable code
- severity
- stage
- source_document_id or opaque dry-run ID
- locator token
- safe field/category
- generic message

Ordinary diagnostics must never contain:
- absolute path
- raw source text
- account number
- email
- credential/token
- raw payment reference
- full counterparty text
- full commerce product text

Raw source locator remains private evidence only.

## 25. Determinism requirements

Given the same:
- source bytes
- discovery policy
- registry read snapshot
- adapter versions
- source claim inputs

the dry-run result must be byte-for-byte equivalent after serialization of its public/safe form.

Deterministic ordering:
- artifacts by content SHA
- occurrences by source locator token or stable discovery order
- diagnostics by stage/code/document/token
- semantic events by canonical representation

No current clock time enters identity or semantic hashes.

## 26. CSV and image policy in Phase 2C-C

The adapter contract supports CSV and IMAGE as channels, but Phase 2C-C must not silently fall back to legacy generic parsing.

CSV without an explicit universal preflight/adapter path:
REVIEW_REQUIRED.

ShopeePay screenshot/image without an explicit non-OCR adapter:
REVIEW_REQUIRED.

No OCR is added in Phase 2.

No legacy parser fallback is allowed from the universal dry-run orchestrator.

## 27. Full Phase 2 gate after 2C-C

Phase 2 is complete only when all of these are demonstrated:

- standalone and nested ZIP occurrences deduplicate by exact SHA
- safe payload replay works without disk extraction
- local symlink directories are explicitly skipped and reported
- mixed-extension exact duplicates fail closed
- content detection is separated from source claim authority
- registry authority is read-only in dry-run
- unknown template fails closed
- source claim conflict fails closed
- exact adapter selection is deterministic
- adapter output remains normalized evidence only
- semantic same-statement duplicate across different bytes is detected
- revision/conflict remains review-required
- missing natural identity remains review-required
- private source content never appears in safe logs or diagnostics
- no registry write helper is called by dry-run
- no ledger mutation occurs
- production SQLite remains byte-identical

Phase 3 provider adapters remain locked until this full gate passes.

## 28. Mandatory 2C-C tests

By the end of 2C-C, add at least the following tests.

### 2C-C1 payload access

1. standalone discovered artifact can be re-read and SHA verified.
2. nested ZIP artifact can be re-read without extraction to disk.
3. multi-level nested ZIP lineage can be replayed.
4. changed standalone file after discovery fails closed.
5. final size mismatch fails closed.
6. final SHA mismatch fails closed.
7. archive-hop SHA mismatch fails closed.
8. unsafe archive member replay fails closed.
9. symlinked local file is skipped.
10. symlinked local directory is explicitly diagnosed and never traversed.
11. payload replay diagnostics do not leak absolute paths.
12. mixed observed extensions are preserved for orchestration review.

### 2C-C2 orchestration and authority

13. exact SHA registry duplicate short-circuits before adapter execution.
14. exact SHA ambiguity fails closed.
15. preflight content detection runs without source_hint authority.
16. matching source claim continues.
17. conflicting source claim becomes review-required.
18. unknown template never reaches adapter.
19. template drift never reaches adapter.
20. registry template mismatch never reaches adapter.
21. exact adapter catalog key selects one adapter.
22. missing adapter becomes review-required.
23. duplicate adapter catalog key fails closed.
24. descriptor/input mismatch fails closed.
25. dry-run does not call create_or_get_import_batch.
26. dry-run does not call register_source_document.
27. dry-run does not call record_source_document_occurrence.
28. zero-activity completed adapter result remains valid.
29. adapter FAILED becomes document failure without ledger mutation.
30. safe diagnostics aggregate without raw locator leakage.

### 2C-C3 semantic identity

31. different bytes with equivalent normalized evidence produce the same semantic SHA.
32. input event order does not change semantic SHA.
33. changing duplicate-event multiplicity changes semantic SHA.
34. Decimal 1, 1.0, and 1.00 canonicalize equivalently.
35. negative zero canonicalizes to zero.
36. Unicode and whitespace normalization is deterministic.
37. provenance differences do not change semantic SHA.
38. diagnostic/confidence differences do not change semantic SHA.
39. template/parser version differences do not change semantic SHA.
40. source_registry_id differences do change semantic SHA.
41. natural document key differences do change semantic SHA.
42. all six evidence roles have deterministic canonical forms.
43. semantic duplicate maps to SKIPPED_DUPLICATE.
44. revision/conflict maps to REVIEW_REQUIRED.
45. missing natural key maps to AMBIGUOUS review.
46. exact duplicate precedence wins before semantic classification.
47. dry-run document ordering is deterministic.
48. repeated dry-run with identical inputs has identical safe output.

Existing 161 targeted Universal Ingestion tests must continue to pass throughout 2C-C.

## 29. Definition of Ready for 2C-C1 implementation

READY_FOR_2C_C1_IMPLEMENTATION = YES only when:

- Phase 2C-B commit 580c961 is pushed and synchronized
- 161 targeted tests are green
- brownfield quick regression is green
- production SQLite is unchanged
- the missing payload-access bridge is explicitly recognized
- local symlink directory behavior is explicitly frozen
- mixed-extension behavior is explicitly frozen
- content detection versus registry authority boundary is explicitly frozen
- dry-run write prohibition is explicit
- semantic-document-v1 canonicalization is explicit
- identity precedence and dry-run dispositions are explicit
- 48 minimum 2C-C tests are frozen
- Phase 3 remains locked

No registry schema migration is required by this design.

No canonical ledger schema change is required by this design.