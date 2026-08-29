# Phase 3 Jago Blocker — Account Period Summary Evidence Extension Design

Date: 2026-08-29
Status: FROZEN

## 1. Problem Statement

The Phase 3 Jago private-corpus audit proved that Bank Jago monthly statements provide explicit pocket-scoped period summaries across multiple subaccounts (pockets) per monthly statement. Specifically, the audited corpus contains 35 pocket-scoped period summaries across 11 documents (10 distinct periods), each containing:
- a stable provider-native pocket/subaccount identifier (`observed_provider_account_key`),
- opening balance (`opening_balance`),
- incoming total (`incoming_total`),
- outgoing total (`outgoing_total`), and
- closing balance (`closing_balance`).

The current universal ingestion model provides `SourceSummaryEvidence` (used for document-level/unscoped summaries in BCA monthly statements), which does not contain a typed subaccount or pocket attribution field.

## 2. Incompatibility of Naive Additive Changes to `SourceSummaryEvidence`

In the `semantic-document-v1` canonicalization engine (`aturuang/ingestion_orchestration.py`), dataclass instances are canonicalized by enumerating all fields returned by `dataclasses.fields(value)`. Adding an optional field (such as `observed_provider_account_key: str | None = None`) to `SourceSummaryEvidence` results in `null` being serialized for existing unscoped summaries.

This would mutate the semantic canonical JSON and change the `semantic_sha256` of already-completed BCA documents and existing test baselines.

Therefore, `SourceSummaryEvidence` MUST remain unchanged and continue to represent unscoped/document-level source summary evidence.

## 3. Frozen Extension Design

To represent account/pocket-scoped period summaries with full type safety, attribution, and backward compatibility, universal ingestion introduces exactly one new event role and exactly one new evidence dataclass.

### 3.1 New Event Role

```python
EventRole.ACCOUNT_PERIOD_SUMMARY = "ACCOUNT_PERIOD_SUMMARY"
```

### 3.2 New Evidence Dataclass: `AccountPeriodSummaryEvidence`

```python
@dataclass(frozen=True)
class AccountPeriodSummaryEvidence:
    observed_provider_account_key: str = field(repr=False)
    currency: str
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: Decimal | None = None
    incoming_total: Decimal | None = None
    outgoing_total: Decimal | None = None
    closing_balance: Decimal | None = None
```

### 3.3 Field Specifications and Validations

1. `observed_provider_account_key: str` (REQUIRED):
   - Provider-native account/subaccount/pocket identifier from source evidence.
   - Marked `repr=False` to prevent private account keys from leaking into string representations.
   - Validated using existing non-empty text validation helper (`_require_text`).
2. `currency: str` (REQUIRED):
   - Validated using existing currency normalization helper (`_currency`).
3. `period_start: str | None = None` and `period_end: str | None = None`:
   - Validated using existing optional text validation helper (`_optional_text`).
4. `opening_balance`, `incoming_total`, `outgoing_total`, `closing_balance`: `Decimal | None = None`:
   - Validated using existing optional decimal validation helper (`_optional_decimal`).
   - Float values are strictly forbidden; all numeric evidence remains `Decimal`.

## 4. Semantic Compatibility Principle

1. `SourceSummaryEvidence` dataclass fields remain exactly:
   - `currency`
   - `period_start`
   - `period_end`
   - `opening_balance`
   - `incoming_total`
   - `outgoing_total`
   - `closing_balance`
   in that exact order.
2. Existing BCA semantic canonical JSON and `semantic_sha256` hashes remain byte-identical.
3. `semantic-document-v1` in `aturuang/ingestion_orchestration.py` requires NO modifications, as it generically canonicalizes any registered `EventRole` and dataclass payload.
4. Two `ACCOUNT_PERIOD_SUMMARY` events with different `observed_provider_account_key` values produce distinct semantic canonical representations, preserving machine-readable pocket attribution.

## 5. Scope and Invariants

- **Evidence-Only Authority**: This payload records explicit source-stated summary totals for a provider-native account scope. It does not perform ledger mutation, account resolution, or canonical financial classification.
- **No Orchestration Changes**: `aturuang/ingestion_orchestration.py` remains untouched.
- **No BCA / Preflight / Registry / DB Changes**: Production SQLite, migration schema, preflight, discovery, BCA adapter, and UI remain completely untouched.
- **Implementation Scope (Checkpoint 2)**:
  1. `aturuang/ingestion_adapter.py`
  2. `tests/test_universal_ingestion_phase2_adapter.py`
  3. `tests/test_universal_ingestion_phase2_orchestration.py`
- **Targeted Test Gate**: Exactly 4 new test methods are added (2 adapter contract tests, 2 orchestration semantic tests), bringing the universal targeted test suite count from 241 to 245.
