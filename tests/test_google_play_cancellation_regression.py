"""
Google Play cancellation must be lifecycle evidence with zero financial effect.
"""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestGooglePlayCancellationRegression(unittest.TestCase):

    def test_english_cancellation_is_non_transaction(self):
        env = os.environ.copy()
        node_lts = r"C:\Users\allan\.local\node-lts"

        if Path(node_lts).exists():
            env["PATH"] = node_lts + ";" + env.get("PATH", "")

        js = r"""
const d = require('./domain.ts');

const ev = d.parseGmailIntelligence(
  'Your Example 3D Game subscription has been canceled',
  [
    'Google Play',
    'Your Example 3D Game subscription has been canceled.',
    'Order number: GPA.1234-5678-9012-34567',
    'Order date: Apr 12, 2025 5:10:43 PM GMT+7'
  ].join('\n'),
  'googleplay-noreply@google.com',
  '2025-05-22T03:10:48.000Z'
);

if (ev.event_kind !== 'NON_TRANSACTION') process.exit(11);
if (ev.financial_class !== 'Ignore') process.exit(12);
if (ev.financial_direction !== 'Neutral') process.exit(13);
if (ev.amount !== 0) process.exit(14);
if (ev.candidate !== null) process.exit(15);
if (ev.evidence_role !== 'LIFECYCLE_STATUS') process.exit(16);
if (ev.external_order_id !== 'GPA.1234-5678-9012-34567') process.exit(17);

console.log('GOOGLE_PLAY_CANCELLATION_PASS');
"""

        r = subprocess.run(
            ["node", "-e", js],
            cwd=str(ROOT / "cloud" / "worker" / "src"),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            r.returncode,
            0,
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}",
        )
        self.assertIn(
            "GOOGLE_PLAY_CANCELLATION_PASS",
            r.stdout,
        )


if __name__ == "__main__":
    unittest.main()
