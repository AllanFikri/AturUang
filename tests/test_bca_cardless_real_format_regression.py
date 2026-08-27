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

const event =
  domain.parseGmailIntelligence(
    'Tarik Tunai Berhasil',
    [
      'Status',
      ':',
      'Berhasil',
      '',
      'Tanggal Transaksi',
      ':',
      '25 Agu 2026 11:20:23',
      '',
      'Jenis Transfer',
      ':',
      'Cardless - Tarik Tunai',
      '',
      'Sumber Dana',
      ':',
      '3680xxxx80',
      '',
      'Nominal',
      ':',
      'IDR 50,000.00',
      '',
      'No. Handphone',
      ':',
      '0817xxxxxx05',
      '',
      'Nomor Referensi',
      ':',
      '11111111-2222-3333-4444-555555555555',
      '',
      'Kode ATM',
      ':',
      'WSID TEST1'
    ].join('\n'),
    'bca@bca.co.id',
    '2026-08-25T04:20:24Z'
  );

check(
  event.event_kind ===
    'CASH_WITHDRAWAL',
  'KIND',
  event
);

check(
  event.financial_class ===
    'Cash Withdrawal',
  'CLASS',
  event
);

check(
  event.financial_direction ===
    'Neutral',
  'DIRECTION',
  event
);

check(
  event.amount === 50000,
  'AMOUNT',
  event
);

check(
  event.transaction_reference ===
    '11111111-2222-3333-4444-555555555555',
  'REFERENCE',
  event
);

check(
  event.event_id ===
    'bca_cash_11111111-2222-3333-4444-555555555555',
  'EVENT_ID',
  event
);

check(
  event.source_account_alias ===
    'BCA Main',
  'SOURCE',
  event
);

check(
  event.destination_account_alias ===
    'Cash',
  'DESTINATION',
  event
);

check(
  event.destination_owner_type ===
    'SELF',
  'OWNER',
  event
);

check(
  event.candidate &&
  event.candidate.tx_type ===
    'Transfer' &&
  event.candidate.status ===
    'AutoApproved',
  'CANDIDATE',
  event
);

console.log(
  'BCA_CARDLESS_REAL_FORMAT: PASS'
);
console.log(
  'BCA_CARDLESS_FULL_REFERENCE: PASS'
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
        "\nBCA Cardless regression failed.\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}\n"
    )

print(result.stdout.strip())
