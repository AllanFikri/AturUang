"""
Regression guard for BCA QRIS transaction-reference parsing.

Reproduces the historical collision where the Indonesian label
"Referensi" was partially interpreted as:
    ref + erensi
causing canonical event_id bca_qris_erensi.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER_SRC = ROOT / "cloud" / "worker" / "src"

node_lts = r"C:\Users\allan\.local\node-lts"
env = os.environ.copy()
env["PATH"] = f"{node_lts};{env.get('PATH', '')}"

js = r"""
const domain = require('./domain.ts');

const first = domain.parseGmailIntelligence(
    'Notifikasi Transaksi QRIS',
    [
        'Status Transaksi: Berhasil',
        'Jenis Transaksi: Pembayaran QRIS',
        'Merchant: Merchant A',
        'Nominal: Rp 25.000,00',
        'Referensi: QRISABC123'
    ].join('\n'),
    'bca@bca.co.id',
    '2026-01-10T10:00:00Z'
);

const second = domain.parseGmailIntelligence(
    'Notifikasi Transaksi QRIS',
    [
        'Status Transaksi: Berhasil',
        'Jenis Transaksi: Pembayaran QRIS',
        'Merchant: Merchant B',
        'Nominal: Rp 100.000,00',
        'Referensi: QRISXYZ789'
    ].join('\n'),
    'bca@bca.co.id',
    '2026-01-11T10:00:00Z'
);

if (first.transaction_reference !== 'QRISABC123') {
    console.error(
        'FAIL_FIRST_REFERENCE',
        JSON.stringify({
            reference: first.transaction_reference,
            event_id: first.event_id
        })
    );
    process.exit(1);
}

if (second.transaction_reference !== 'QRISXYZ789') {
    console.error(
        'FAIL_SECOND_REFERENCE',
        JSON.stringify({
            reference: second.transaction_reference,
            event_id: second.event_id
        })
    );
    process.exit(1);
}

if (first.event_id === second.event_id) {
    console.error(
        'FAIL_CANONICAL_ID_COLLISION',
        first.event_id,
        second.event_id
    );
    process.exit(1);
}

if (
    first.event_id === 'bca_qris_erensi' ||
    second.event_id === 'bca_qris_erensi'
) {
    console.error('FAIL_ERENSI_COLLISION');
    process.exit(1);
}

const matchingExisting = {
    event_kind: first.event_kind,
    financial_class: first.financial_class,
    financial_direction: first.financial_direction,
    amount: first.amount,
    merchant_normalized: first.merchant_normalized
};

if (!domain.isCanonicalCorrelationCompatible(first, matchingExisting)) {
    console.error('FAIL_MATCHING_PRIMARY_REJECTED');
    process.exit(1);
}

const conflictingAmount = {
    ...matchingExisting,
    amount: 100000
};

if (domain.isCanonicalCorrelationCompatible(first, conflictingAmount)) {
    console.error('FAIL_AMOUNT_COLLISION_ACCEPTED');
    process.exit(1);
}

const conflictingMerchant = {
    ...matchingExisting,
    merchant_normalized: 'Completely Different Merchant'
};

if (domain.isCanonicalCorrelationCompatible(first, conflictingMerchant)) {
    console.error('FAIL_MERCHANT_COLLISION_ACCEPTED');
    process.exit(1);
}

const conflictingKind = {
    ...matchingExisting,
    event_kind: 'OWN_TRANSFER'
};

if (domain.isCanonicalCorrelationCompatible(first, conflictingKind)) {
    console.error('FAIL_EVENT_KIND_COLLISION_ACCEPTED');
    process.exit(1);
}

console.log('BCA_QRIS_REFERENCE_REGRESSION: PASS');
console.log('CANONICAL_CORRELATION_BEHAVIOR: PASS');
"""

result = subprocess.run(
    ["node", "-e", js],
    cwd=str(WORKER_SRC),
    env=env,
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    raise AssertionError(
        "\nBCA QRIS regression reproduced.\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}\n"
    )

print(result.stdout.strip())
