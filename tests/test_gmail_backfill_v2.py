from pathlib import Path

code = Path(
    "integrations/gmail-apps-script/Code.gs"
).read_text(encoding="utf-8")

required = [
    "function backfillHistoricalClosedMonthsV2()",
    "function clearHistoricalBackfillV2Block()",
    "GMAIL_BACKFILL_V2_BLOCKED_CODE",
    "BACKFILL_ALREADY_RUNNING",
    "BACKFILL_BLOCKED",
    "BACKFILL_V2_BLOCKED",
    "BACKFILL_CONFIG_MISSING",
    "BACKFILL_INVALID_CHECKPOINT",
    "BACKFILL_INVALID_OFFSET",
    "BACKFILL_OFFSET_MONTH_MISMATCH",
    "BACKFILL_TIMEZONE_MISMATCH",
    "BACKFILL_CUTOFF_NOT_CLOSED",
    "BACKFILL_HEALTH_FETCH_EXCEPTION",
    "BACKFILL_HEALTH_HTTP_FAILURE",
    "BACKFILL_INVALID_HEALTH_RESPONSE",
    "BACKFILL_WORKER_NOT_SHADOW",
    "BACKFILL_SCHEMA_TOO_OLD",
    "BACKFILL_HTTP_FAILURE",
    "BACKFILL_FETCH_EXCEPTION",
    "BACKFILL_INVALID_WORKER_RESPONSE",
    "BACKFILL_RUNTIME_PAUSE",
    "BACKFILL_COMPLETED",
    "Utilities.Charset.UTF_8",
    "LockService.getScriptLock()",
]

missing = [
    marker
    for marker in required
    if marker not in code
]

assert not missing, (
    "Missing markers: " +
    ", ".join(missing)
)

assert (
    "function backfillMarch2025Trial()"
    not in code
), "March runner still exists"

assert '''
        endDate,
        true
      );''' in code, (
    "Historical page is not fail-fast"
)

failure_guard = code.index(
    "if (page.failedCount > 0)"
)

offset_advance = code.index(
    "offset += page.threadCount"
)

assert failure_guard < offset_advance, (
    "Offset can advance before failure guard"
)

block_check = code.index(
    '"GMAIL_BACKFILL_V2_BLOCKED_CODE"'
)

backfill_call = code.index(
    "return backfillGmailTransactions("
)

assert block_check < backfill_call, (
    "Persistent block is not checked before backfill"
)

assert '"2025-03"' in code
assert '"2026-07"' in code

print(
    "GMAIL HISTORICAL BACKFILL V2 STATIC: PASS"
)
