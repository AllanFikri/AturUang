# Phase 3 ShopeePay Transaction History Image Ingestion - Forward Design Gate

Date: 2026-08-29
Authoritative Baseline: 681a7e09c539ff72502a294cd683701969138ba2
Target Review Branch: review/shopeepay-v1
Production DB SHA256: 2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

---

## 1. Executive Summary & Architecture Scope

This document defines the frozen architecture, contract invariants, OCR transport boundary, resolution gates, and validation rules for ingesting ShopeePay transaction-history screenshot captures in AturUang Universal Ingestion Phase 3.

ShopeePay transaction-history captures are pure raster image artifacts (IMAGE media type: JPEG/PNG) without embedded text layers. Ingestion is performed via deterministic local optical character recognition and visual layout parsing to recover trusted financial events without cloud services, generative AI, or non-deterministic heuristics.

---

## 2. Frozen Corpus Truth & Dual-Track Partitioning

The private corpus consists of 13 SHA-unique ShopeePay transaction-history screenshots spanning August 2025 through August 2026 (13 consecutive monthly periods):

- Whole Corpus Ground Truth:
  - Total Unique Image Artifacts: 13
  - Closed Calendar Periods: 12 (2025-08 through 2026-07)
  - Open / Partial Calendar Periods: 1 (2026-08, activity through 2026-08-21)
  - Total Visible Transaction Cards: 274
  - Successful Monetary Movement Candidates: 267
  - Explicit Failed Evidence Rows: 7

- Partition 1: High-Resolution 2025 Captures (5 Artifacts):
  - Months: August 2025 through December 2025
  - Dimensions: 1220x3689 to 1220x13017 (native full-resolution mobile scrolling captures, width >= 720 px)
  - Total Visible Cards: 62
  - Successful Monetary Rows: 60 (eligible for automated raw-image CASH_MOVEMENT extraction)
  - Explicit Failed Rows: 2 (internal evidence only, 0 CASH_MOVEMENT)
  - Optical Extraction: Viable via deterministic vertical slicing using local Windows WinRT OCR transport.

- Partition 2: Low-Resolution 2026 Captures (8 Artifacts):
  - Months: January 2026 through August 2026
  - Dimensions: 118x1280 to 305x1036 (downscaled mobile captures, width < 720 px)
  - Total Visible Cards: 212
  - Successful Monetary Rows: 207
  - Explicit Failed Rows: 5 (2026-01: 1, 2026-03: 1, 2026-04: 1, 2026-07: 1, 2026-08: 1)
  - Raw Ingestion Disposition: All 8 downscaled artifacts fail the SHOPEEPAY_MIN_OCR_WIDTH gate and route to REVIEW_REQUIRED, emitting 0 raw CASH_MOVEMENT events.
  - Historical Backfill Reservation: The 207 successful 2026 transactions are validated 100% (212/212 core rows matched, 0 mismatches) by the private historical sidecar oracle (ShopeePay_Ledger_2026-01-01_to_2026-08-21.csv/.json) and are reserved for a separate Verified Historical Backfill phase, never conflated with raw-image production adapter parsing.

---

## 3. Core Architecture, Preflight Seam & Trust Model

### 3.1. Primary Production Pipeline
- Raw Screenshot Payload (IMAGE media type)
- Orchestration Seam -> forwards claimed_source_registry_id as source_hint to preflight_func
- Ingestion Preflight -> validates image headers, dimensions, applies source-hint routing (NO OCR in preflight)
- Provider Resolution Gate -> checks SHOPEEPAY_MIN_OCR_WIDTH (720 px)
- Local OCR Transport Boundary (aturuang/ingestion_image_ocr.py)
- ShopeePay Card & Layout Parser (aturuang/ingestion_shopeepay_adapter.py)
- Envelope Construction (CASH_MOVEMENT)

### 3.2. Preflight Trust Model & Content Gate
- Preflight MUST NOT perform OCR.
- Image routing occurs if and only if valid JPEG/PNG media is present and explicit source_hint == "shopeepay_mutation".
- Source Hint Authority: Source hint provides ROUTING authority only. It does NOT prove ShopeePay content.
- Unclaimed Generic Images: Without a ShopeePay source hint, generic IMAGE behavior remains untouched (UNKNOWN_TEMPLATE / VISUAL_TEMPLATE_REQUIRES_IMAGE_ADAPTER).
- Adapter Content Gate: The ShopeePay adapter must independently verify actual OCR textual layout markers (Transaction History / Transaksi Terakhir, month header, filter tabs) before parsing cards. Falsely claimed or spoofed images immediately fail closed to REVIEW_REQUIRED with 0 emitted events.

### 3.3. Orchestration Seam Invariant
- ORCHESTRATION_CHANGE_REQUIRED = True.
- Implementation requirement: dry_run_artifact forwards source_hint=claimed_source_registry_id when invoking preflight_func.
- PreflightFunction is already Callable[..., DocumentPreflight], so no core evidence contract extension is required.

---

## 4. Provider-Specific Resolution Gate

To prevent degraded optical noise from corrupting financial accounts without lowering generic global image thresholds, a provider-specific gate is frozen:

- SHOPEEPAY_MIN_OCR_WIDTH = 720 px
- Width < 720 px:
  - Source quality is deemed insufficient for trusted automated optical character recognition.
  - Automatically yields AdapterParseStatus.REVIEW_REQUIRED before financial card parsing.
  - Emits exactly 0 CASH_MOVEMENT envelopes.
- Width >= 720 px:
  - Media proceeds to OCR transport and card-level validation.
  - Individual cards still require unambiguous financial fields to be emitted.

---

## 5. Period Authority & Calendar Boundaries

- Preflight Period Invariant: Because preflight does not perform OCR, preflight.period_status = PeriodStatus.UNKNOWN for routed ShopeePay images.
- Adapter Period Authority: The ShopeePay adapter extracts the visible date filter (e.g. 01 Nov 2025 - 30 Nov 2025) from OCR output and determines the definitive AdapterResult period status:
  - Full calendar month (1st day to last calendar day): PeriodStatus.CLOSED (12 months: 2025-08 through 2026-07).
  - Partial / ongoing month (e.g. 01 Aug 2026 - 21 Aug 2026): PeriodStatus.OPEN (1 month: 2026-08).
- Screenshot capture completeness does not override calendar period boundaries. Period values are never inferred from filenames.

---

## 6. Visual Grammar & Source-Like Evidence Mapping

### 6.1. Explicitly Recoverable Fields
- Period Filter / Month Header: E.g., 01 Nov 2025 - 30 Nov 2025 / November 2025.
- Transaction Date: Formatted as DD Month YYYY (e.g., 25 November 2025).
- Signed IDR Amount: Explicit prefix +Rp... (inflow) or -Rp... (outflow).
- Visible Description / Title: Visible merchant, counterpart, or product line (e.g., Payment, Top Up, Cashback Bonus, Transfer Sent).
- Explicit Failed Badge: Red textual Failed indicator displayed on unsuccessful transaction attempts.

### 6.2. Absent Fields & Zero-Emission Envelopes
- Transaction Time: Absent from list view -> occurred_at preserves source date (YYYY-MM-DD) without fabricating 00:00.
- Native Transaction ID: Absent from list view -> provider_transaction_id_raw = None.
- Running Wallet Balance: Absent from list view -> balance_after = None, BALANCE_SNAPSHOT = 0.
- Monthly Summary Figures: Numerical totals are not rendered in list view -> SOURCE_SUMMARY = 0, ACCOUNT_PERIOD_SUMMARY = 0.
- Wallet / MSISDN Identity: No account number is displayed -> ACCOUNT_OBSERVATION = 0.
- Investment / Trading: INVESTMENT_TRADE = 0.

### 6.3. Cash Movement Field Invariants
- amount: Exact positive Decimal IDR amount.
- direction: +Rp -> EventDirection.INFLOW, -Rp -> EventDirection.OUTFLOW.
- direction_raw: Preserved safely (e.g. +Rp99.000, -Rp100.000).
- currency: IDR.
- status: SourceEventStatus.UNKNOWN for normal rows without a Failed badge. Never infer POSTED.
- description_raw: Clean visible text.
- Semantic Categories: Any derived categories (expense, transfer, refund) from sidecar metadata are completely ignored in Phase 3.

---

## 7. Failed Transaction & Quality Error Policies

- Explicit Failed Cards:
  - Parsed internally as source evidence for audit logs.
  - Strictly excluded from CASH_MOVEMENT envelopes (0 cash movement emitted).
  - Do not alter cash totals and never receive fabricated success events.
- Error Dispositions:
  - Corrupt Image / Header Error -> AdapterParseStatus.FAILED.
  - OCR Execution / Runtime Timeout Error -> AdapterParseStatus.FAILED.
  - Low-Resolution (< 720 px) -> AdapterParseStatus.REVIEW_REQUIRED (0 cash events).
  - Claimed but Non-ShopeePay Image -> AdapterParseStatus.REVIEW_REQUIRED (0 cash events).
  - Card-Level Financial Ambiguity (unreadable amount, missing sign, ambiguous date) -> AdapterParseStatus.REVIEW_REQUIRED; omit ambiguous card, preserve only independently trusted cards.

---

## 8. Natural Key & Unidentified Wallet Authority

Because ShopeePay screenshot captures do not render a provider-native wallet account identifier:

- Natural Key Pattern: shopeepay_mutation:unidentified_wallet:<period> (e.g., shopeepay_mutation:unidentified_wallet:2025-11).
- Forbidden Identity Sources: Filenames, directory names, customer display names, OCR-inferred phone numbers, or sidecar annotations.
- Invariant: ACCOUNT_OBSERVATION envelopes remain 0. If multiple distinct documents for the same period are submitted, orchestration routes them to conflict review rather than silently merging.

---

## 9. Local OCR Transport & Tall Image Slicing

### 9.1. Module Boundary (aturuang/ingestion_image_ocr.py)
- Encapsulates all OCR execution behind a clean Python interface extract_image_text(image_bytes, *, config=None) -> ImageOcrResult.
- Transport: Executes Windows built-in Windows.Media.Ocr.OcrEngine via native powershell.exe in a bounded subprocess with -NoProfile -NonInteractive flags.
- Operational Invariants:
  - Local only, no network, no cloud, no LLM.
  - Bounded timeout, fail-closed error handling.
  - Temporary files placed only in OS temp directory with guaranteed cleanup in finally blocks.
  - Machine-readable structured output (JSON / line tokens with spatial coordinates).
  - Privacy Safety: Diagnostics and stderr never leak OCR card text or sensitive strings.
  - Parser Isolation: Zero shell / PowerShell code inside the ShopeePay financial parser.
  - Test Isolation: Unit tests strictly use synthetic mock/injected OCR fixtures and never spawn real PowerShell OCR processes.

### 9.2. Deterministic Image Slicing
- WinRT OCR enforces MaxImageDimension limits. Mobile scrolling captures (up to 13,017 px tall) are sliced vertically with fixed height, fixed overlap, and monotonically increasing Y offsets.
- Spatial + Content Deduplication: Tokens and cards spanning slice boundaries are deduplicated using source-relative spatial coordinates combined with normalized text. Two legitimate transactions with identical date and amount at different Y coordinates are strictly preserved and never collapsed.

---

## 10. Implementation Deliverables & Test Design

### 10.1. File Deliverables
- Ingestion Adapter: aturuang/ingestion_shopeepay_adapter.py
- OCR Extraction Module: aturuang/ingestion_image_ocr.py
- Synthetic Test Fixtures: tests/fixtures/ingestion/shopeepay_transaction_history_image_v1.json
- Test Suite: tests/test_universal_ingestion_phase3_shopeepay.py

### 10.2. Focused Test Suite (Exactly 30 Unittest Methods)
1. test_01_adapter_metadata_and_descriptor: Verify registry ID, template ID, and parser version constants.
2. test_02_template_signature_and_fingerprint: Verify template fingerprint and preflight signature matching.
3. test_03_orchestration_forwards_source_hint: Verify dry_run_artifact forwards claimed_source_registry_id to preflight.
4. test_04_unclaimed_generic_image_unrouted: Verify images without ShopeePay source hint remain unrouted.
5. test_05_source_hint_cannot_bypass_layout_verification: Ensure claimed ShopeePay image without valid layout fails closed to REVIEW_REQUIRED.
6. test_06_corrupt_or_truncated_image_fails_closed: Fail closed to FAILED on invalid image headers or truncated payload.
7. test_07_provider_resolution_gate_low_res_rejected: Verify width < 720 px triggers REVIEW_REQUIRED and 0 cash movement.
8. test_08_provider_resolution_gate_high_res_accepted: Verify width >= 720 px proceeds to OCR extraction.
9. test_09_preflight_period_status_unknown: Verify preflight emits PeriodStatus.UNKNOWN before OCR.
10. test_10_adapter_period_authority_closed: Verify adapter assigns PeriodStatus.CLOSED for complete monthly date filters.
11. test_11_adapter_period_authority_open: Verify adapter assigns PeriodStatus.OPEN for partial date filters (e.g. Aug 1-21).
12. test_12_valid_high_res_jpeg_extraction: Verify parsing of clean synthetic high-resolution JPEG capture.
13. test_13_valid_png_media_extraction: Verify parsing of clean synthetic PNG capture.
14. test_14_tall_image_vertical_slicing_geometry: Verify slicing logic partitions tall images with correct monotonic Y offsets.
15. test_15_spatial_slice_overlap_deduplication: Verify cards spanning slice boundaries are not duplicated.
16. test_16_identical_date_amount_different_y_preserved: Verify legitimate identical transactions at distinct Y coordinates are preserved.
17. test_17_ocr_transport_error_fails_closed: Verify OCR runtime timeout / subprocess error fails closed to FAILED.
18. test_18_date_only_occurred_at_preservation: Verify occurred_at stores source date (YYYY-MM-DD) without fabricated time.
19. test_19_positive_amount_inflow_mapping: Verify +Rp correctly maps to EventDirection.INFLOW.
20. test_20_negative_amount_outflow_mapping: Verify -Rp correctly maps to EventDirection.OUTFLOW.
21. test_21_decimal_amount_precision: Verify IDR amounts parse into exact Decimal values without float drift.
22. test_22_multiline_description_normalization: Verify multiline merchant/transfer details normalize cleanly.
23. test_23_explicit_failed_badge_omitted_from_cash_movement: Verify failed cards emit 0 CASH_MOVEMENT events.
24. test_24_normal_transaction_status_unknown: Verify successful cards have status = SourceEventStatus.UNKNOWN.
25. test_25_zero_non_cash_envelopes_emitted: Assert exactly 0 ACCOUNT_OBSERVATION, SOURCE_SUMMARY, and BALANCE_SNAPSHOT envelopes.
26. test_26_zero_provider_transaction_id_emitted: Assert provider_transaction_id_raw is None on all rows.
27. test_27_no_semantic_category_derivation: Verify absence of expense/transfer/refund classification.
28. test_28_financial_card_ambiguity_triggers_review: Unreadable amount or missing sign triggers REVIEW_REQUIRED and omits card.
29. test_29_natural_document_key_unidentified_wallet: Verify deterministic natural key format.
30. test_30_diagnostics_and_repr_privacy_safety: Assert no raw OCR card text or PII leaks in diagnostics.
