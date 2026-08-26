"""
test_prompt13b_intelligence.py — Consolidated Unit & Integration Tests for Transaction Intelligence v1
"""
import unittest
import hashlib
import json
import os
import sys
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "aturuang"))
sys.path.insert(0, str(BASE_DIR / "tools"))

from tools.audit_gmail_vs_ledger import run_audit, is_money_equal, CanonicalLedgerEvent

class TestGmailIntelligenceV1(unittest.TestCase):

    def setUp(self):
        self.worker_src = BASE_DIR / "cloud" / "worker" / "src"
        self.node_lts = r"C:\Users\allan\.local\node-lts"
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.node_lts};{self.env.get('PATH', '')}"

    def test_01_strict_gmail_sender_security(self):
        """A. Test strict evidence-backed sender allowlist & attacker rejection."""
        js_code = """
        const auth = require('./auth.ts');
        
        // 1. Valid senders
        const valid1 = auth.isGmailSenderTrusted('PT Bank Central Asia <bca@bca.co.id>');
        const valid2 = auth.isGmailSenderTrusted('noreply@jago.com');
        const valid3 = auth.isGmailSenderTrusted('no-reply@flip.id');
        const valid4 = auth.isGmailSenderTrusted('googleplay-noreply@google.com');
        const valid5 = auth.isGmailSenderTrusted('noreply@byu.id');
        const valid6 = auth.isGmailSenderTrusted('no-reply@mailer-esb.com');
        const valid7 = auth.isGmailSenderTrusted('info@shopee.co.id');
        
        if (!valid1.trusted || !valid2.trusted || !valid3.trusted || !valid4.trusted || !valid5.trusted || !valid6.trusted || !valid7.trusted) {
            console.error('FAIL_VALID_SENDER');
            process.exit(1);
        }
        
        // 2. Attacker / Invalid senders
        const evil1 = auth.isGmailSenderTrusted('attacker@evil-bca.com');
        const evil2 = auth.isGmailSenderTrusted('bca@bca.co.id.attacker.com');
        const evil3 = auth.isGmailSenderTrusted('attacker@example.com');
        const evil4 = auth.isGmailSenderTrusted('noreply@jago.com.attacker.com');
        const evil5 = auth.isGmailSenderTrusted('no-reply@marketing.go-jek.com');
        const evil6 = auth.isGmailSenderTrusted('bankjago.com');
        
        if (evil1.trusted || evil2.trusted || evil3.trusted || evil4.trusted || evil5.trusted || evil6.trusted) {
            console.error('FAIL_ATTACKER_NOT_REJECTED');
            process.exit(1);
        }
        
        console.log('SECURITY_PASS');
        """
        res = subprocess.run(
            ["node", "-e", js_code],
            cwd=str(self.worker_src),
            env=self.env,
            capture_output=True,
            text=True
        )
        self.assertIn("SECURITY_PASS", res.stdout, f"Sender security failed: {res.stderr}")

    def test_02_transaction_intelligence_parsers(self):
        """G & H. Test comprehensive parser classifications for all transaction kinds."""
        js_code = """
        const domain = require('./domain.ts');
        
        // 1. BCA QRIS Success
        const qrisSuccess = domain.parseGmailIntelligence(
            'Notifikasi Transaksi QRIS',
            'Status Transaksi: Berhasil\\nJenis Transaksi: Pembayaran QRIS\\nMerchant: Warung Mbak Yani\\nNominal: Rp 25.000,00\\nNo. Referensi: QRIS12345',
            'bca@bca.co.id',
            '2026-08-26T10:00:00Z'
        );
        if (qrisSuccess.event_kind !== 'MERCHANT_PAYMENT' || qrisSuccess.financial_class !== 'Expense' || qrisSuccess.amount !== 25000) {
            console.error('FAIL_BCA_QRIS_SUCCESS', qrisSuccess);
            process.exit(1);
        }
        
        // 2. BCA QRIS Failed
        const qrisFail = domain.parseGmailIntelligence(
            'Transaksi Ditolak',
            'Status Transaksi: Gagal\\nJenis Transaksi: Pembayaran QRIS\\nNominal: Rp 25.000,00',
            'bca@bca.co.id'
        );
        if (qrisFail.event_kind !== 'FAILED_ATTEMPT' || qrisFail.financial_class !== 'Ignore' || qrisFail.status !== 'Ignored') {
            console.error('FAIL_BCA_QRIS_FAIL', qrisFail);
            process.exit(1);
        }
        
        // 3. BCA Cardless Tarik Tunai
        const cardless = domain.parseGmailIntelligence(
            'Notifikasi Tarik Tunai Tanpa Kartu',
            'Tarik Tunai Tanpa Kartu Berhasil\\nNominal: Rp 100.000,00\\nNo Referensi: CDL9988',
            'bca@bca.co.id'
        );
        if (cardless.event_kind !== 'CASH_WITHDRAWAL' || cardless.financial_class !== 'Cash Withdrawal' || cardless.destination_account_alias !== 'Cash') {
            console.error('FAIL_BCA_CARDLESS', cardless);
            process.exit(1);
        }
        
        // 4. BCA Poket Alokasi
        const pocket = domain.parseGmailIntelligence(
            'Penambahan Dana Poket',
            'Tambah Dana Poket Berhasil\\nNama Poket: Tabungan\\nNominal: Rp 500.000,00',
            'bca@bca.co.id'
        );
        if (pocket.event_kind !== 'ALLOCATION_MOVEMENT' || pocket.financial_class !== 'Internal Transfer') {
            console.error('FAIL_BCA_POCKET', pocket);
            process.exit(1);
        }
        
        // 5. BCA -> ShopeePay (VA 122...)
        const shopeeTopup = domain.parseGmailIntelligence(
            'm-BCA: Transfer Berhasil',
            'Transfer ke Rekening 1220812345678 (ShopeePay) Berhasil\\nNominal: Rp 50.000,00',
            'bca@bca.co.id'
        );
        if (shopeeTopup.event_kind !== 'TOPUP' || shopeeTopup.financial_class !== 'Top-up' || shopeeTopup.destination_account_alias !== 'ShopeePay') {
            console.error('FAIL_BCA_SHOPEE_TOPUP', shopeeTopup);
            process.exit(1);
        }
        
        // 6. ShopeePay -> Jago (SELF)
        const shopeeToJago = domain.parseGmailIntelligence(
            'Transfer Dana Masuk',
            'Transfer dari ShopeePay Berhasil\\nNominal: Rp 75.000,00',
            'noreply@jago.com'
        );
        if (shopeeToJago.event_kind !== 'OWN_TRANSFER' || shopeeToJago.financial_class !== 'Internal Transfer') {
            console.error('FAIL_SHOPEE_TO_JAGO', shopeeToJago);
            process.exit(1);
        }
        
        // 7. Jago RDN Stockbit
        const rdn = domain.parseGmailIntelligence(
            'Mutasi RDN Stockbit',
            'Transfer ke RDN Stockbit Berhasil\\nNominal: Rp 1.000.000,00',
            'noreply@jago.com'
        );
        if (rdn.event_kind !== 'INVESTMENT_MOVEMENT' || rdn.financial_class !== 'Investment Movement') {
            console.error('FAIL_JAGO_RDN', rdn);
            process.exit(1);
        }
        
        // 8. Google Play
        const gplay = domain.parseGmailIntelligence(
            'Tanda terima pesanan Google Play',
            'Nomor Pesanan: GPA.1234-5678-9012-34567\\nTotal: Rp 29.000,00\\nItem: App Subscription',
            'googleplay-noreply@google.com'
        );
        if (gplay.event_kind !== 'DIGITAL_PURCHASE' || gplay.financial_class !== 'Expense' || gplay.amount !== 29000) {
            console.error('FAIL_GOOGLE_PLAY', gplay);
            process.exit(1);
        }
        
        // 9. by.U
        const byu = domain.parseGmailIntelligence(
            'Yay! Pembayaran berhasil',
            'Order ID: BYU123456\\nTotal Pembayaran: Rp 50.000,00\\nPaket Data 10GB',
            'noreply@byu.id'
        );
        if (byu.event_kind !== 'DIGITAL_PURCHASE' || byu.financial_class !== 'Expense' || byu.amount !== 50000) {
            console.error('FAIL_BYU', byu);
            process.exit(1);
        }
        
        // 10. ESB Receipt (Secondary evidence)
        const esb = domain.parseGmailIntelligence(
            'Struk Digital',
            'Order ID: ESB-7890\\nMerchant: UTA Ngopi\\nTotal: Rp 35.000,00',
            'no-reply@mailer-esb.com'
        );
        if (esb.evidence_role !== 'SECONDARY_RECEIPT' || esb.event_kind !== 'INVOICE_EVIDENCE') {
            console.error('FAIL_ESB', esb);
            process.exit(1);
        }
        
        console.log('PARSER_ALL_PASS');
        """
        res = subprocess.run(
            ["node", "-e", js_code],
            cwd=str(self.worker_src),
            env=self.env,
            capture_output=True,
            text=True
        )
        self.assertIn("PARSER_ALL_PASS", res.stdout, f"Parser test failed: {res.stderr}")

    def test_03_historical_matcher_synthetic(self):
        """O, P, Q, R, S. Test local read-only historical matcher logic and zero mutation."""
        report = run_audit(email_events=[])
        self.assertTrue(report["zero_mutation_verified"])
        self.assertGreaterEqual(report["total_ledger_events"], 0)
        self.assertEqual(report["db_hash_before"], report["db_hash_after"])

    def test_04_financial_core_unchanged_contract(self):
        """D. Verify financial core is unchanged from the restored canonical financial-core baseline."""
        # 1. Compare the financial-core section with restored baseline 796d9cf
        baseline = subprocess.check_output(
            ["git", "show", "796d9cf:cloud/worker/src/domain.ts"],
            cwd=str(BASE_DIR),
            text=True,
            encoding="utf-8"
        )
        with open(BASE_DIR / "cloud" / "worker" / "src" / "domain.ts", "r", encoding="utf-8") as f:
            current = f.read()

        # Compare only the actual financial-core section.
        # Parser/ingestion contracts below this marker may evolve independently
        # without weakening the financial-core regression guard.
        marker = "// INGESTION & PARSER DOMAIN"

        self.assertIn(marker, baseline)
        self.assertIn(marker, current)

        b_core = baseline.split(marker, 1)[0]
        c_core = current.split(marker, 1)[0]

        self.assertEqual(
            b_core,
            c_core,
            "Financial core modified from restored 796d9cf baseline!"
        )

        # 2. Verify exact column and query assumptions in financial core
        fc_text = c_core
        self.assertIn("account_name", fc_text)
        self.assertIn("snapshot_date", fc_text)
        self.assertIn("snapshot_kind", fc_text)
        self.assertIn("transaction_type", fc_text)
        self.assertIn("account_from", fc_text)
        self.assertIn("account_to", fc_text)
        self.assertIn("is_deleted", fc_text)
        self.assertIn("allocated_amount", fc_text)
        self.assertIn("covers_upcoming_id", fc_text)
        self.assertIn("linked_goal_id", fc_text)

    def test_05_gmail_partial_ingestion_is_retryable(self):
        """Gmail partial canonical failures must fail closed and be safely retryable."""
        index_text = (
            BASE_DIR / "cloud" / "worker" / "src" / "index.ts"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "has_canonical_evidence",
            index_text,
            "Gmail idempotency must distinguish completed from incomplete raw events.",
        )

        self.assertIn(
            "INCOMPLETE_GMAIL_EVENT_REPAIR_FAILED",
            index_text,
            "Incomplete Gmail raw events need an explicit fail-closed recovery path.",
        )

        self.assertIn(
            "remainingPartial",
            index_text,
            "Incomplete Gmail cleanup must verify post-delete state when D1 mutation metadata is inconclusive.",
        )

        repair_verification = index_text.split(
            "const remainingPartial",
            1,
        )[1].split(
            "if (remainingPartial)",
            1,
        )[0]

        self.assertIn(
            "SELECT id",
            repair_verification,
            "Incomplete Gmail repair must query the exact raw row after inconclusive mutation metadata.",
        )

        self.assertIn("FROM raw_events", repair_verification)
        self.assertIn("WHERE id = ?", repair_verification)

        self.assertIn(
            "CANONICAL_EVENT_PERSISTENCE_FAILED",
            index_text,
            "Canonical persistence failures must propagate to HTTP 500.",
        )

        self.assertIn(
            "const canonicalBatch = await env.DB.batch",
            index_text,
            "New canonical event + evidence persistence must be atomic.",
        )

        canonical_section = index_text.split(
            "// Insert / correlate Canonical Financial Event (Migration 0007)",
            1,
        )[1].split(
            "// Update source freshness",
            1,
        )[0]

        self.assertNotIn(
            "} catch {}",
            canonical_section,
            "Canonical persistence errors must never be silently swallowed.",
        )


if __name__ == "__main__":
    unittest.main()
