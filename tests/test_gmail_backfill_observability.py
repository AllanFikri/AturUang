from pathlib import Path

code = Path(
    "integrations/gmail-apps-script/Code.gs"
).read_text(encoding="utf-8")

required = [
    "let backfillAttemptCount = 0;",
    "const BACKFILL_PROGRESS_EVERY = 5;",
    '"BACKFILL_PROGRESS"',
    "backfillAttemptCount === 1",
    "backfillAttemptCount % BACKFILL_PROGRESS_EVERY === 0",
    "extractCleanEmail(fromHeader)",
    ".slice(0, 72)",
]

missing = [
    marker
    for marker in required
    if marker not in code
]

assert not missing, (
    "Missing observability markers: "
    + ", ".join(missing)
)

start_marker = """      if (failFast) {
        backfillAttemptCount++;
"""

end_marker = """      const messageId = msg.getId();
"""

assert start_marker in code, (
    "Progress logging is not gated by historical failFast mode"
)

start = code.index(start_marker)
end = code.index(end_marker, start)

progress_block = code[start:end]

assert "BACKFILL_PROGRESS" in progress_block

assert "getPlainBody" not in progress_block, (
    "Routine progress logs must never include email body"
)

assert "messageId" not in progress_block, (
    "Routine progress logs must not include Gmail message ID"
)

assert "safeSender" in progress_block
assert "safeSubject" in progress_block

assert '.slice(0, 72)' in progress_block, (
    "Subject must remain truncated to 72 characters"
)

# Existing operational/error diagnostics may still use messageId.
# This guard applies only to routine progress logging.

print("GMAIL BACKFILL OBSERVABILITY: PASS")