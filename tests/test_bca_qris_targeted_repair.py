"""
Structural safety contract for targeted BCA QRIS repair.

This test is structural only.
D1 behavioral proof comes from the canary and repair audits.
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

required_worker = [
    "/api/repair/bca-qris",
    "verifyGmailHmac",
    "BCA_QRIS_REPAIR_NOT_SHADOW",
    "bca_qris_erensi",
    'action === "next"',
    'action !== "repair"',
    "parseGmailIntelligence",
    "BCA_QRIS_REPAIR_PAYLOAD_HASH_MISMATCH",
    "BCA_QRIS_REPAIR_AMOUNT_MISMATCH",
    "BCA_QRIS_REPAIR_CANDIDATE_ALREADY_APPLIED",
    "BCA_QRIS_REPAIR_REFERENCE_MULTIPLICITY",
    "referenceRows.length > 1",
    "BCA_QRIS_REPAIR_POSTCHECK_FAILED",
    "UPDATE ingestion_candidates",
    "applied_transaction_id IS NULL",
    "UPDATE canonical_event_evidence",
    "evidence_role = ?",
    "INSERT INTO canonical_financial_events",
    "WHERE EXISTS (",
    "FAILED_ATTEMPT",
    "EXTERNAL_TRANSFER",
    "isFailedQrisRepair",
    "isTransferQrisRepair",
    "isMerchantPaymentRepair",
    "candidate_status",
    "current_evidence_role",
    "already_repaired",
]

for marker in required_worker:
    assert marker in repair_route, (
        f"Missing Worker repair marker: {marker}"
    )

for forbidden in [
    "DELETE FROM raw_events",
    "DELETE FROM ingestion_candidates",
    "DELETE FROM canonical_financial_events",
    "DELETE FROM canonical_event_evidence",
    "UPDATE raw_events",
]:
    assert forbidden not in repair_route, (
        f"Forbidden repair behavior: {forbidden}"
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

repair_apps = APPS[
    apps_start:
]

required_apps = [
    "repairBcaQrisCanary",
    "repairBcaQrisCollapsedCanonical",
    "GmailApp.getMessageById",
    "/api/repair/bca-qris",
    "assertBcaQrisRepairWorkerReady_",
    "postBcaQrisRepair_",
    "BCA_QRIS_REPAIR_CANARY_PASS",
    "BCA_QRIS_REPAIR_COMPLETED",
    "resumeBcaQrisAutoRepairAfterFix",
]

for marker in required_apps:
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

print(
    "BCA_QRIS_TARGETED_REPAIR_STRUCTURE: PASS"
)
print(
    "BCA_QRIS_TARGETED_REPAIR_NONDESTRUCTIVE: PASS"
)
print(
    "BCA_QRIS_TARGETED_REPAIR_CANDIDATE_GUARD: PASS"
)
print(
    "BCA_QRIS_TARGETED_REPAIR_EVIDENCE_ROLE: PASS"
)
print(
    "BCA_QRIS_TARGETED_REPAIR_BACKFILL_ISOLATION: PASS"
)
