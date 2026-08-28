# AturUang - PDF Extraction Dependency Decision V1

Date: 2026-08-28
Status: FROZEN FOR UNIVERSAL INGESTION V1

## Decision

Primary PDF extraction dependency:

pypdf[crypto]==6.16.2

pdfplumber is not a default/runtime dependency for Universal Ingestion V1.
It may only be added later as a narrowly scoped layout helper when targeted fixtures and full corpus replay prove that coordinate-aware extraction is required.

## Benchmark evidence

- Unique PDF documents: 228
- Total pages: 505
- pypdf documents OK: 228/228
- pypdf failed: 0
- non-empty page ratio: 1.0
- BCA encrypted statements: 19/19 successfully extracted
- Stockbit SOA documents included: 10
- pypdf 6.16.2 total benchmark time: 37.768 seconds
- pdfplumber 0.11.10 total benchmark time: 56.861 seconds

No raw extracted financial text is committed to the repository.

## Architectural boundary

Provider adapters must not independently choose or instantiate arbitrary PDF libraries.

Intended flow:

Source Document
-> Document Registry
-> PDF Extraction Boundary
-> Provider Adapter
-> NormalizedFinancialEvent

Successful text extraction is not equivalent to successful financial parsing.

Every provider adapter still requires:
- template detection
- golden fixtures
- full private-corpus replay
- malformed-input tests
- idempotency tests
- provenance preservation
- fail-closed unknown-template behavior
- reconciliation checks where available

## OCR policy

OCR is not part of the default PDF V1 path because all 228 audited PDFs and all 505 pages exposed usable text layers.

A future PDF with an unusable or missing text layer must fail closed to review or to a separately approved OCR path.

## ShopeePay exception

ShopeePay historical evidence is image-based and uses a separate image-document contract with confidence and human-review gates.

## Shopee commerce ownership rule

Shopee order receipts are commerce evidence, not automatic personal expenses.

The future model must separate:
- commerce account
- recipient
- payer
- economic owner

This includes titip-CO cases.

Supported ownership semantics must include at least:
- SELF
- THIRD_PARTY_DIRECT
- SELF_REIMBURSABLE
- MIXED
- UNKNOWN

## Dependency change rule

Do not add pdfplumber, OCR libraries, or another PDF stack merely because one parser implementation is inconvenient.

A new dependency requires:
1. representative failing evidence
2. targeted fixtures
3. corpus tests
4. documented architectural justification

## Safety

This decision does not:
- mutate production SQLite
- mutate D1
- deploy Worker code
- merge Pattern Analysis
- make D1 financial authority
- commit raw private source files

END
