"""
Structural contract for the two proven families inside
bca_qris_erensi.

Authoritative D1 inventory:
- MERCHANT_PAYMENT
- CASH_WITHDRAWAL

This remains a structural test, not behavioral D1 proof.
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

DOMAIN = (
    ROOT
    / "cloud"
    / "worker"
    / "src"
    / "domain.ts"
).read_text(encoding="utf-8")

route_start = INDEX.index(
    "// 2b. TARGETED BCA QRIS CANONICAL REPAIR"
)

route_end = INDEX.index(
    "// 3. TELEGRAM BOT WEBHOOK",
    route_start,
)

route = INDEX[
    route_start:route_end
]

required_route = [
    "replayBodyHasQris",
    "replayBodyHasCashWithdrawal",
    "isMerchantPaymentRepair",
    "isTransferQrisRepair",
    "isFailedQrisRepair",
    "isCashWithdrawalRepair",
    "original_minimal_raw_payload",
    "originalParserEventKind",
    "originalFamilyCompatible",
    "candidate_tx_type",
    '"MERCHANT_PAYMENT"',
    '"CASH_WITHDRAWAL"',
    '"Transfer"',
    '"Expense"',
    "BCA_QRIS_REPAIR_ORIGINAL_FAMILY_CONFLICT",
    "source_account_alias",
    "destination_account_alias",
    "destination_owner_type",
]

for marker in required_route:
    assert marker in route, (
        f"Missing collapsed-family repair marker: {marker}"
    )

for forbidden in [
    "DELETE FROM raw_events",
    "DELETE FROM ingestion_candidates",
    "DELETE FROM canonical_event_evidence",
    "DELETE FROM canonical_financial_events",
    "UPDATE raw_events",
]:
    assert forbidden not in route, (
        f"Forbidden repair behavior: {forbidden}"
    )

bca_start = DOMAIN.index(
    "// 1. BCA (bca@bca.co.id)"
)

bca_end = DOMAIN.index(
    "// 2. JAGO (noreply@jago.com)",
    bca_start,
)

bca = DOMAIN[
    bca_start:bca_end
]

assert "ref(?:erence)?" not in bca, (
    "Legacy vulnerable reference extractor remains in BCA parser"
)

assert "BCA Cardless Tarik Tunai" in bca
assert "nomor\\s+referensi" in bca

print(
    "BCA_COLLAPSED_FAMILY_STRUCTURE: PASS"
)

print(
    "BCA_COLLAPSED_FAMILY_ONLY_TWO_PROVEN_FAMILIES: PASS"
)

print(
    "BCA_CARDLESS_REFERENCE_GUARD: PASS"
)

print(
    "BCA_COLLAPSED_REPAIR_NONDESTRUCTIVE: PASS"
)
