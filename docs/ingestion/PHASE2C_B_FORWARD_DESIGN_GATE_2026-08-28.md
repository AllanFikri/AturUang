# Phase 2C-B Forward Design Gate — Adapter Interface + Normalized Evidence Envelope

**Date:** 2026-08-28
**Branch:** `feature/universal-ingestion-v1`
**Validated baseline HEAD:** `4288473f3ed096c0964175df30e39f28b4c8f226`
**Production SQLite protected SHA-256:** `2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f`
**Status:** DESIGN FROZEN FOR 2C-B IMPLEMENTATION; NO PROVIDER PARSER IMPLEMENTED HERE.

## 1. Purpose

Phase 2C-B defines the universal boundary between source-specific parsing and all later financial reasoning.

The adapter layer must preserve evidence without deciding canonical financial truth.

The contract must remain usable by:
- Phase 3 Provider Adapters
- Phase 4 Account Discovery
- Phase 5 Cross-Source Matcher
- Phase 6 Financial Semantics
- Phase 7 Reconciliation
- Phase 8 Review / Staging
- Phase 9 UI / evidence drilldown
- Phase 10 Historical Backfill
- Phase 11 Drive discovery
- Phase 12 Gmail / Apps Script near-real-time evidence
- Phase 13 Telegram review/notification
- Phase 14 Pattern recalibration
- Phase 15 final integration/release gate

No design in 2C-B may require rebuilding the envelope when these consumers arrive.

---

## 2. Non-negotiable boundary

Adapter output is **normalized evidence**, not canonical ledger data.

Forbidden in 2C-B:
- transaction/ledger writes
- balance mutation
- category/budget mutation
- final Expense/Income/Internal Transfer classification
- final ownership resolution
- cross-source matching
- reconciliation decision
- semantic document dedup implementation
- provider parser implementation
- OCR implementation
- registry migration
- D1/cloud write/deploy
- unofficial API access

`DEBIT`/`CREDIT`, sign, or `INFLOW`/`OUTFLOW` evidence must never automatically mean Expense/Income.

---

## 3. Source-channel independence

The adapter interface must support all existing `SourceChannel` values:

- `PDF`
- `IMAGE`
- `EMAIL`
- `CSV`
- `API`
- `MANUAL`

The contract must **not** require a filesystem path.

`AdapterInput` must carry:
- source/document identity metadata
- source channel
- template/parser identity
- period/preflight metadata when already known
- one private ephemeral payload representation

Payload representations allowed by contract:
- binary payload for PDF/image and other byte sources
- text payload for text/email/CSV cases
- structured mapping payload for API/manual cases

Payload fields are private, ephemeral, and excluded from normal object representation/logging.

A non-file source must not be forced to invent filename or extension metadata.

---

## 4. Adapter identity and authority

Create an immutable `AdapterDescriptor` with:
- `adapter_id`
- `source_registry_id`
- `template_id`
- `parser_version`
- `source_channel`

Create a `UniversalSourceAdapter` protocol/interface with a pure operation conceptually equivalent to:

`parse(AdapterInput) -> AdapterResult`

The protocol must not expose:
- DB connection
- commit/apply method
- ledger method
- account-balance mutation
- network side effects

Adapter selection is performed by orchestration only **after** preflight + registry/template authority validation.

An adapter must never self-select from "closest" content.

Descriptor/input mismatch fails closed.

---

## 5. One envelope, typed evidence payloads

The universal boundary is a `NormalizedEventEnvelope` containing common source/evidence metadata and exactly one typed evidence payload.

The envelope is **not** named or treated as a canonical transaction.

### Event roles

Freeze the generic roles:

- `CASH_MOVEMENT`
- `BALANCE_SNAPSHOT`
- `SOURCE_SUMMARY`
- `ACCOUNT_OBSERVATION`
- `INVESTMENT_TRADE`
- `COMMERCE_ORDER`

This prevents snapshots, account lifecycle evidence, investment trades, and commerce receipts from being forced into ordinary transaction rows.

### Common envelope metadata

Required or explicitly nullable:
- `source_document_id`
- `source_registry_id`
- `template_id`
- `parser_version`
- `source_channel`
- `event_role`
- `source_event_id`
- `row_fingerprint`
- `evidence_quality`
- `parse_confidence`
- private provenance
- safe diagnostic codes

`row_fingerprint` is row/evidence identity inside the adapter layer. It is **not** cross-source economic-event identity and is **not** semantic-document identity.

The envelope and privacy-bearing nested objects must have safe/redacted representation (`repr=False` or equivalent).

---

## 6. Cash movement evidence

`CashMovementEvidence` must preserve, when available:

- `account_id` nullable
- `subaccount_id` nullable
- observed source-account key/display hints
- observed destination-account key/display hints
- `occurred_at`
- `posted_at`
- `settlement_date`
- `amount`
- `currency`
- normalized direction
- raw direction/sign evidence
- normalized source status
- `status_raw`
- `counterparty_raw`
- optional `counterparty_normalized`
- `description_raw`
- `provider_transaction_id_raw`
- `reference_raw`
- `reference_normalized`
- `balance_after`
- `payment_method_raw`
- provider category raw
- non-authoritative `event_hint`
- payment components where one provider event uses multiple funding components

Amounts in the parsing/normalization layer use `Decimal` (or an equivalent exact decimal representation), never binary float as parser truth.

SQLite REAL + two-decimal write/SUM rules remain a later canonical-storage concern.

---

## 7. Direction and status

### Normalized direction

Use a source-relative direction vocabulary:
- `INFLOW`
- `OUTFLOW`
- `NEUTRAL`
- `UNKNOWN`

Always preserve raw DB/CR/sign/direction evidence separately when the provider prints it.

Direction is not final financial semantics.

### Normalized source status

Represent at minimum:
- `POSTED`
- `PENDING`
- `FAILED`
- `REVERSED`
- `REFUNDED`
- `CANCELLED`
- `UNKNOWN`

A failed payment cannot become a successful cash movement merely because an amount was detected.

A refund/reversal must remain distinguishable evidence for later semantics/matching.

---

## 8. Time contract

Never collapse:
- `occurred_at`
- `posted_at`
- `settlement_date`

Preserve provider precision:
- date-only stays date-only
- timestamp stays timestamp
- do not invent a timezone that the source did not provide

User-facing interpretation remains Asia/Jakarta/WIB where applicable later.

Trade date and due/settlement date remain distinct.

Current/open documents remain `OPEN`, `PARTIAL`, or `UNKNOWN` until sufficient provider evidence exists.

---

## 9. Balance snapshot and source summary

`BalanceSnapshotEvidence` represents observed balances without pretending they are transactions.

Must support:
- observed account/subaccount hint
- balance value
- currency
- observed/effective date or period
- snapshot kind such as opening/closing/current when source supports it

`SourceSummaryEvidence` must support:
- period start/end
- opening balance
- incoming total
- outgoing total
- closing balance
- currency

This is required for statement reconciliation.

blu portfolio rows are snapshot/summary evidence, not cash transactions.

SeaBank interest/tax detail and statement summary must not be double-counted as new cash rows.

---

## 10. Account observation evidence

Phase 3 adapters may observe account/subaccount facts, but Phase 4 owns identity/lifecycle resolution.

Create raw `ObservedAccountEvidence` with:
- institution/source identity
- observed provider account key
- display name raw
- parent observed key
- provider state/status raw
- date/period evidence
- source document provenance

Do **not** decide in the adapter:
- stable `account_id` from display name
- NEW/RENAMED/CLOSED/REUSED final lifecycle state
- historical provider-slot reuse identity
- final ownership confidence

The existing resolved account/lifecycle contracts remain downstream authority.

---

## 11. Investment trade evidence

`InvestmentTradeEvidence` must preserve:
- instrument/security raw identity
- trade date
- settlement/due date
- buy/sell/side raw
- quantity
- unit price
- gross/net amount when available
- fees/tax/interest components where printed
- reference/provider IDs
- account/RDN hints
- provenance

A Stockbit trade detail is not automatically the same event as its RDN cash settlement.

Phase 5 performs that correlation.

---

## 12. Commerce order evidence

Create raw `CommerceOrderEvidence` for Shopee-style enrichment.

Preserve:
- order native ID raw
- order date
- seller raw
- payment method raw
- shipping service raw
- subtotal/fees/shipping/discount/total components
- recipient/buyer label raw
- line items
- quantity
- variation raw
- line subtotal
- provenance

Do **not** assign in the adapter:
- economic owner
- payer responsibility
- personal spending effect
- funding account solely from payment-method label

Those belong after payment matching + ownership resolution.

Existing commerce ownership contracts remain downstream resolved semantics.

---

## 13. Payment and amount components

Use typed component evidence rather than flattening provider detail into description text.

`PaymentComponentEvidence`:
- method/label raw
- amount
- currency

`AmountComponentEvidence`:
- component label raw
- amount
- currency
- optional direction/sign evidence

This supports:
- GoPay balance + coins
- Shopee subtotal/shipping/service fee/discount
- Stockbit fees/tax/interest
without provider-specific schema hacks.

---

## 14. AdapterResult

`AdapterResult` must contain:
- descriptor/adapter identity
- source document identity
- parse status
- parsed period evidence
- natural-document-key candidate when provider evidence supports one
- zero or more `NormalizedEventEnvelope` values
- safe diagnostics
- explicit review-required state/reasons

Adapter parse status:
- `COMPLETED`
- `REVIEW_REQUIRED`
- `FAILED`

Fatal parse failure must not return partially trusted events as if completed.

A legitimate zero-activity statement may return `COMPLETED` with zero cash-movement events plus valid summary/period evidence.

---

## 15. Natural identity versus row identity

2C-B preserves inputs needed by 2C-C, but does not implement semantic-document dedup.

Keep these concepts separate:

1. content SHA — exact bytes
2. natural document key — provider/document/account/period/order identity
3. semantic document fingerprint — Phase 2C-C
4. provider/row event fingerprint — adapter evidence identity
5. cross-source same-economic-event match — Phase 5

Reference alone is never sufficient identity.

Filename/display name alone is never sufficient identity.

---

## 16. Diagnostics and privacy

Separate:

### Private provenance
May contain, in private runtime memory/state:
- raw locator
- page/image/row index
- raw text/reference/description fragments needed for audit

### Safe diagnostic
May contain:
- stable code
- severity
- field/category
- review flag
- sanitized generic message
- opaque locator token/fingerprint when useful

Safe diagnostics must never contain:
- absolute private filesystem path
- raw financial document text
- account number
- email
- credential/token
- full raw reference if sensitive

Privacy-bearing contracts must not expose raw values through default dataclass `repr`.

2C-B implementation should minimally harden `SourceProvenanceContract` and `ReferenceEvidenceContract` representation if they are used directly in adapter outputs.

---

## 17. Confidence separation

Do not create one global "confidence".

Keep separate:
- parsing confidence
- evidence/quality confidence
- account ownership confidence — Phase 4+
- cross-source match confidence — Phase 5
- semantic classification/review state — Phase 6/8

The adapter is authoritative only about what it parsed, not what the financial meaning ultimately is.

---

## 18. Fail-closed rules

Adapter/interface validation must reject:
- descriptor/input source mismatch
- descriptor/input template mismatch
- descriptor/input channel mismatch
- malformed row fingerprint
- missing required provenance
- invalid Decimal amount
- non-finite values
- impossible role/payload pair
- malformed payload union
- raw provider event presented as canonical financial semantics
- private diagnostic content when detectable by contract tests

Unsupported adapter/template => no parsing.

Unknown/template-drift/source-claim-conflict documents never reach an adapter through the normal orchestration path.

---

## 19. Forward-consumer map

### Phase 3 — Provider adapters
Consumes the protocol and typed envelopes. Adds provider parsing only.

### Phase 4 — Account discovery
Consumes `ObservedAccountEvidence` and account hints. Resolves stable identities/lifecycle/ownership.

### Phase 5 — Cross-source matcher
Consumes dates, exact Decimal amounts, direction, provider IDs, references, counterparty, payment method, account identities, provenance, investment/commerce evidence.

### Phase 6 — Financial semantics
Consumes matched/owned evidence. Produces Expense/Income/Internal Transfer/Refund/Receivable/Investment/Review. Adapter hints are non-authoritative.

### Phase 7 — Reconciliation
Consumes source summaries, snapshots, running balances, closed-period events, transfer/settlement matches.

### Phase 8 — Review/staging
Consumes safe diagnostics, provenance references, ambiguity and confidence. No raw private text is required in ordinary queue summaries.

### Phase 9 — UI
Can show evidence health/parser/template version without exposing raw private payload by default.

### Phase 10 — Historical backfill
Uses deterministic adapter outputs and row fingerprints one closed month at a time.

### Phase 11–13 — Automation/connectors
Feed the same adapter boundary; connectors remain transport/evidence only.

### Phase 14 — Pattern
Consumes canonical historical truth only, not raw adapter outputs.

### Phase 15 — Release
Requires adapter tests + private corpus replay + matching/semantics/reconciliation + SQLite/D1 safety gates.

---

## 20. Mandatory 2C-B implementation tests

Minimum tests before commit:

1. `AdapterInput` supports binary, text, and structured source payloads without filesystem dependency.
2. raw payload does not appear in `repr`.
3. source channel mismatch fails closed.
4. source registry mismatch fails closed.
5. template mismatch fails closed.
6. parser-version mismatch fails closed where contract requires exact descriptor identity.
7. protocol exposes no DB/ledger/apply method.
8. valid cash movement envelope.
9. occurred/posted/settlement remain distinct.
10. Decimal amount rejects NaN/Infinity.
11. direction does not map to Expense/Income.
12. failed/pending/refund/reversal statuses remain representable.
13. balance snapshot is not a cash movement.
14. source summary supports opening/incoming/outgoing/closing.
15. observed account evidence does not resolve lifecycle/ownership.
16. investment trade preserves trade vs settlement date.
17. commerce evidence has no economic-owner/payer-responsibility inference.
18. payment components preserve multiple funding components.
19. row fingerprint validation.
20. private provenance representation is redacted/suppressed.
21. safe diagnostics reject or sanitize private absolute locator/raw content.
22. zero-activity completed result is valid.
23. fatal result cannot masquerade as completed trusted output.
24. all 135 existing Universal Ingestion tests still pass.
25. brownfield quick regression passes.
26. production SQLite hash remains byte-identical.

---

## 21. Exact 2C-B implementation scope

Expected implementation concern:

- new `aturuang/ingestion_adapter.py`
- minimal privacy representation hardening in `aturuang/ingestion_contracts.py`
- new `tests/test_universal_ingestion_phase2_adapter.py`

No provider parser files.

No registry schema change.

No orchestration/dry-run/semantic-document fingerprint yet.

No engineering-log update inside the implementation code commit; append documentation only after the implementation checkpoint is independently validated.

---

## 22. Deferred explicitly to 2C-C

2C-C owns:
- discovery + preflight orchestration
- registry/template authority reconciliation
- adapter selection
- adapter execution
- normalized output collection
- semantic document fingerprint
- semantic duplicate classification
- dry-run result
- deterministic diagnostics aggregation

2C-B must provide enough information for those operations without implementing them.

---

## 23. Definition of Ready for 2C-B implementation

`READY_FOR_2C_B_IMPLEMENTATION = YES` only when:

- Phase 2C-A is pushed/synchronized
- all 9 source families remain representable
- all 6 source channels are supported by the interface design
- non-transaction evidence is not forced into transaction rows
- ownership/matching/semantics/reconciliation authority boundaries are explicit
- raw payload/provenance privacy boundary is explicit
- exact/natural/semantic/row/cross-source identities remain separate
- exact 2C-B implementation scope is bounded
- mandatory tests are frozen
- production SQLite remains unchanged

At this design checkpoint, there are no unresolved design decisions that require changing the Phase 1/2 registry schema.

Phase 3 remains locked until 2C-B + 2C-C + full Phase 2 gate pass.