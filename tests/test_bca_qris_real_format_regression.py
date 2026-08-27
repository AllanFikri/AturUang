import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER_SRC = ROOT / "cloud" / "worker" / "src"

env = os.environ.copy()
node_lts = r"C:\Users\allan\.local\node-lts"
env["PATH"] = f"{node_lts};{env.get('PATH', '')}"

js = r"""
const domain = require('./domain.ts');

const ev = domain.parseGmailIntelligence(
  'Internet Transaction Journal',
  [
    'Status',
    ':',
    'Berhasil',
    '',
    'Tanggal Transaksi',
    ':',
    '26 Agu 2026 20:29:14',
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
    'RRN',
    ':',
    '148962856',
    '',
    'Nomor Referensi',
    ':',
    '9527120260826202912474QRS0000000000'
  ].join('\n'),
  'bca@bca.co.id',
  '2026-08-26T14:08:49Z'
);

if (ev.amount !== 10000) {
  console.error('FAIL_AMOUNT', ev.amount);
  process.exit(1);
}

if (ev.transaction_reference !== '9527120260826202912474QRS0000000000') {
  console.error(
    'FAIL_REFERENCE',
    JSON.stringify({
      reference: ev.transaction_reference,
      event_id: ev.event_id
    })
  );
  process.exit(1);
}

if (ev.merchant_normalized !== 'MERCHANT CONTOH') {
  console.error('FAIL_MERCHANT', ev.merchant_normalized);
  process.exit(1);
}

if (ev.merchant_pan !== '9360001430000000000') {
  console.error('FAIL_PAN', ev.merchant_pan);
  process.exit(1);
}

if (ev.merchant_location !== 'MALANG, 65125, ID') {
  console.error('FAIL_LOCATION', ev.merchant_location);
  process.exit(1);
}

if (
  ev.event_id !==
  'bca_qris_9527120260826202912474QRS0000000000'
) {
  console.error('FAIL_EVENT_ID', ev.event_id);
  process.exit(1);
}

console.log('BCA_QRIS_REAL_FORMAT: PASS');
"""

constResult = subprocess.run(
    ["node", "-e", js],
    cwd=str(WORKER_SRC),
    env=env,
    capture_output=True,
    text=True,
)

if constResult.returncode != 0:
    raise AssertionError(
        "\nReal BCA QRIS format regression reproduced.\n"
        f"STDOUT:\n{constResult.stdout}\n"
        f"STDERR:\n{constResult.stderr}\n"
    )

print(constResult.stdout.strip())
