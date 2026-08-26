from pathlib import Path

code = Path("cloud/worker/src/index.ts").read_text(encoding="utf-8")

markers = [
    '"SELECT id FROM canonical_financial_events WHERE transaction_reference = ? LIMIT 1"',
    '"SELECT id FROM canonical_financial_events WHERE external_order_id = ? LIMIT 1"',
    '"SELECT id FROM canonical_financial_events WHERE event_id = ? LIMIT 1"',
]

for marker in markers:
    assert marker in code, f"missing lookup: {marker}"

positions = [code.index(marker) for marker in markers]

assert positions == sorted(positions), (
    "Canonical lookup order must remain "
    "transaction_reference -> external_order_id -> event_id"
)

assert "if (!existingCanon && canonEv.external_order_id)" in code
assert "if (!existingCanon)" in code

assert "} else if (canonEv.external_order_id)" not in code

stages = [
    'canonicalStage = "lookup_transaction_reference"',
    'canonicalStage = "lookup_external_order_id"',
    'canonicalStage = "lookup_event_id"',
    'canonicalStage = "insert_existing_evidence"',
    'canonicalStage = "insert_new_canonical_batch"',
]

for stage in stages:
    assert stage in code, f"missing observability stage: {stage}"

print("CANONICAL_CORRELATION_STRUCTURE: PASS")
