import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WORKER_SRC = (
    ROOT
    / "cloud"
    / "worker"
    / "src"
)

env = os.environ.copy()

node_lts = (
    r"C:\Users\allan\.local\node-lts"
)

env["PATH"] = (
    f"{node_lts};"
    f"{env.get('PATH', '')}"
)

js = r"""
const domain =
  require('./domain.ts');

function check(cond, name, detail) {
  if (!cond) {
    console.error(
      'FAIL_' + name,
      JSON.stringify(detail || {})
    );
    process.exit(1);
  }
}

const payment =
  domain.parseGmailIntelligence(
    'Internet Transaction Journal',
    [
      'Status',
      ':',
      'Berhasil',
      '',
      'Jenis Transaksi',
      ':',
      'Pembayaran QRIS',
      '',
      'Pembayaran Ke',
      ':',
      'MERCHANT CONTOH',
      '',
      'Lokasi Merchant',
      ':',
      'MALANG, 65125, ID',
      '',
      'Merchant PAN',
      ':',
      '9360001430000000000',
      '',
      'Total Bayar',
      ':',
      'IDR 10,000.00',
      '',
      'Nomor Referensi',
      ':',
      'PAYMENTREF123'
    ].join('\n'),
    'bca@bca.co.id',
    '2026-08-26T14:08:49Z'
  );

check(
  payment.event_kind ===
    'MERCHANT_PAYMENT',
  'PAYMENT_KIND',
  payment
);

check(
  payment.financial_class ===
    'Expense',
  'PAYMENT_CLASS',
  payment
);

check(
  payment.status ===
    'AutoApproved',
  'PAYMENT_STATUS',
  payment
);

check(
  payment.transaction_reference ===
    'PAYMENTREF123',
  'PAYMENT_REF',
  payment
);

check(
  payment.event_id ===
    'bca_qris_PAYMENTREF123',
  'PAYMENT_EVENT',
  payment
);

check(
  payment.candidate &&
  payment.candidate.status ===
    'AutoApproved',
  'PAYMENT_CANDIDATE',
  payment
);

const failed =
  domain.parseGmailIntelligence(
    'Internet Transaction Journal',
    [
      'Status',
      ':',
      'Gagal',
      '',
      'Jenis Transaksi',
      ':',
      'Pembayaran QRIS',
      '',
      'Pembayaran Ke',
      ':',
      'MERCHANT CONTOH',
      '',
      'Total Bayar',
      ':',
      'IDR 33,000.00',
      '',
      'Nomor Referensi',
      ':',
      'FAILEDREF123'
    ].join('\n'),
    'bca@bca.co.id',
    '2026-08-25T11:39:38Z'
  );

check(
  failed.event_kind ===
    'FAILED_ATTEMPT',
  'FAILED_KIND',
  failed
);

check(
  failed.financial_class ===
    'Ignore',
  'FAILED_CLASS',
  failed
);

check(
  failed.financial_direction ===
    'Neutral',
  'FAILED_DIRECTION',
  failed
);

check(
  failed.status ===
    'Ignored',
  'FAILED_STATUS',
  failed
);

check(
  failed.amount === 33000,
  'FAILED_AMOUNT',
  failed
);

check(
  failed.transaction_reference ===
    'FAILEDREF123',
  'FAILED_REF',
  failed
);

check(
  failed.event_id ===
    'bca_fail_FAILEDREF123',
  'FAILED_EVENT',
  failed
);

check(
  failed.evidence_role ===
    'LIFECYCLE_STATUS',
  'FAILED_ROLE',
  failed
);

check(
  failed.candidate === null,
  'FAILED_CANDIDATE',
  failed
);

const transfer =
  domain.parseGmailIntelligence(
    'Internet Transaction Journal',
    [
      'Status',
      ':',
      'Berhasil',
      '',
      'Jenis Transaksi',
      ':',
      'Transfer QRIS',
      '',
      'Nama Penerima',
      ':',
      'PENERIMA CONTOH',
      '',
      'Nominal',
      ':',
      'IDR 99,000.00',
      '',
      'No. Referensi',
      ':',
      '451733928'
    ].join('\n'),
    'bca@bca.co.id',
    '2025-08-01T10:52:02Z'
  );

check(
  transfer.event_kind ===
    'EXTERNAL_TRANSFER',
  'TRANSFER_KIND',
  transfer
);

check(
  transfer.financial_class ===
    'Pending Review',
  'TRANSFER_CLASS',
  transfer
);

check(
  transfer.financial_direction ===
    'Debit',
  'TRANSFER_DIRECTION',
  transfer
);

check(
  transfer.status ===
    'Pending',
  'TRANSFER_STATUS',
  transfer
);

check(
  transfer.amount === 99000,
  'TRANSFER_AMOUNT',
  transfer
);

check(
  transfer.transaction_reference ===
    '451733928',
  'TRANSFER_REF',
  transfer
);

check(
  transfer.event_id ===
    'bca_qris_transfer_451733928',
  'TRANSFER_EVENT',
  transfer
);

check(
  transfer.counterparty_normalized ===
    'PENERIMA CONTOH',
  'TRANSFER_COUNTERPARTY',
  transfer
);

check(
  transfer.candidate &&
  transfer.candidate.status ===
    'Pending',
  'TRANSFER_CANDIDATE',
  transfer
);

console.log(
  'BCA_QRIS_PAYMENT_SEMANTIC: PASS'
);

console.log(
  'BCA_QRIS_FAILED_MULTILINE_SEMANTIC: PASS'
);

console.log(
  'BCA_QRIS_TRANSFER_SEMANTIC: PASS'
);
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
        "\nBCA QRIS semantic regression failed.\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}\n"
    )

print(
    result.stdout.strip()
)
