"""
Structural safety contract for the targeted BCA QRIS canonical repair.

This test intentionally does not claim D1 behavioral proof.
It verifies the source-level safety boundaries before deployment.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INDEX = (
    ROOT
    / "cloud"
    / "worker"
    / "src"
    / "index.ts"
).read_text(encoding="utf-8")

APPS = (
    ROOT
    / "integrations"
    / "gmail-apps-script"
    / "Code.gs"
).read_text(encoding="utf-8")


route_start = INDEX.index(
    "// 2b. TARGETED BCA QRIS CANONICAL REPAIR"
)

route_end = INDEX.index(
    "// 3. TELEGRAM BOT WEBHOOK",
    route_start,
)

repair_route = INDEX[
    route_start:route_end
]


required_worker_markers = [
    '/api/repair/bca-qris',
    'verifyGmailHmac',
    'BCA_QRIS_REPAIR_NOT_SHADOW',
    'bca_qris_erensi',
    'action === "next"',
    'action !== "repair"',
    'parseGmailIntelligence',
    'BCA_QRIS_REPAIR_PAYLOAD_HASH_MISMATCH',
    'candidate_amount',
    'isCanonicalCorrelationCompatible',
    'UPDATE canonical_event_evidence',
    'INSERT INTO canonical_financial_events',
    'BCA_QRIS_REPAIR_POSTCHECK_FAILED',
    'BCA_QRIS_REPAIR_REFERENCE_MULTIPLICITY',
    'referenceRows.length > 1',
    'WHERE EXISTS (',
    'already_repaired',
]

for marker in required_worker_markers:
    assert marker in repair_route, (
        f"Missing Worker repair safety marker: {marker}"
    )


for forbidden in [
    "DELETE FROM raw_events",
    "DELETE FROM ingestion_candidates",
    "DELETE FROM canonical_financial_events",
    "UPDATE raw_events",
    "UPDATE ingestion_candidates",
]:
    assert forbidden not in repair_route, (
        f"Forbidden destructive repair behavior: {forbidden}"
    )


assert (
    "current.current_event_id !=="
    in repair_route
)

assert (
    'senderCheck.email !=='
    in repair_route
)

assert (
    '"bca@bca.co.id"'
    in repair_route
)


apps_start = APPS.index(
    "TARGETED BCA QRIS COLLAPSED-CANONICAL REPAIR"
)

repair_apps = APPS[apps_start:]


required_apps_markers = [
    "repairBcaQrisCanary",
    "repairBcaQrisCollapsedCanonical",
    "GmailApp.getMessageById",
    "/api/repair/bca-qris",
    "assertBcaQrisRepairWorkerReady_",
    "postBcaQrisRepair_",
    "BCA_QRIS_REPAIR_CANARY_PASS",
    "BCA_QRIS_REPAIR_COMPLETED",
]

for marker in required_apps_markers:
    assert marker in repair_apps, (
        f"Missing Apps Script repair marker: {marker}"
    )


for forbidden in [
    "GMAIL_BACKFILL_CHECKPOINT",
    "GMAIL_BACKFILL_OFFSET",
    "resetBackfillCheckpoint(",
    "backfillGmailTransactions(",
    "backfillHistoricalClosedMonthsV2(",
]:
    assert forbidden not in repair_apps, (
        "Targeted repair must remain independent "
        f"from historical backfill: {forbidden}"
    )


print("BCA_QRIS_TARGETED_REPAIR_STRUCTURE: PASS")
print("BCA_QRIS_TARGETED_REPAIR_NONDESTRUCTIVE: PASS")
print("BCA_QRIS_TARGETED_REPAIR_BACKFILL_ISOLATION: PASS")
