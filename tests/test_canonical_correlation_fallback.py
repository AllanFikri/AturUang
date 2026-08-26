from pathlib import Path

code = Path(
    "cloud/worker/src/index.ts"
).read_text(encoding="utf-8")

tx_ref = '''if (canonEv.transaction_reference) {
            existingCanon = await env.DB.prepare(
              "SELECT id FROM canonical_financial_events WHERE transaction_reference = ? LIMIT 1"
'''

external = '''if (!existingCanon && canonEv.external_order_id) {
            existingCanon = await env.DB.prepare(
              "SELECT id FROM canonical_financial_events WHERE external_order_id = ? LIMIT 1"
'''

event_id = '''if (!existingCanon) {
            existingCanon = await env.DB.prepare(
              "SELECT id FROM canonical_financial_events WHERE event_id = ? LIMIT 1"
'''

assert tx_ref in code, "transaction_reference lookup missing"
assert external in code, "external_order_id fallback missing"
assert event_id in code, "event_id fallback missing"

tx_pos = code.index(tx_ref)
ext_pos = code.index(external)
event_pos = code.index(event_id)

assert tx_pos < ext_pos < event_pos, (
    "Canonical lookup order must be "
    "transaction_reference -> external_order_id -> event_id"
)

assert "} else if (canonEv.external_order_id)" not in code, (
    "Old mutually-exclusive external_order_id lookup still present"
)

print("CANONICAL_CORRELATION_FALLBACK: PASS")
