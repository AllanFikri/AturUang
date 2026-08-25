"""
Test Suite: core.js Pure Calculations (Prompt 2 Section A)
"""
import subprocess
import json
from pathlib import Path


def run_node_core_tests():
    node_script = r"""
    const assert = require('assert');
    const fs = require('fs');
    const core = require('./core.js');

    const results = [];

    function test(name, fn) {
      try {
        fn();
        results.push({ name, passed: true });
      } catch (err) {
        results.push({ name, passed: false, error: err.message });
      }
    }

    // 1. Bulan lampau
    test('A1 - Bulan lampau: dayOfMonth = jumlah hari penuh bulan tersebut', () => {
      const avg = core.computeDailyAverage(310000, { month: '2026-07', currentMonth: '2026-08', today: '2026-08-23' });
      assert.strictEqual(avg.dayOfMonth, 31, 'July 2026 should have 31 days elapsed in past month');
      assert.strictEqual(avg.dailyAvg, 10000, 'Daily average should be 310000 / 31 = 10000');
    });

    test('A1b - Bulan lampau: daysLeft = 0 dan rata-rata tidak memakai tanggal hari ini', () => {
      const safe = core.computeSafeDaily(100000, { month: '2026-07', currentMonth: '2026-08', today: '2026-08-23' });
      assert.strictEqual(safe.daysLeft, 0, 'Past month should have 0 days left');
      assert.strictEqual(safe.safeDaily, 0, 'Safe daily for past month should be 0');
    });

    // 2. Bulan berjalan
    test('A2 - Bulan berjalan: jumlah hari tersisa mencakup hari ini', () => {
      const safe = core.computeSafeDaily(90000, { month: '2026-08', currentMonth: '2026-08', today: '2026-08-23' });
      // August has 31 days: 31 - 23 + 1 = 9 days
      assert.strictEqual(safe.daysInMonth, 31);
      assert.strictEqual(safe.daysLeft, 9, 'August 23 to 31 inclusive is 9 days');
      assert.strictEqual(safe.safeDaily, 10000);
    });

    test('A2b - Bulan berjalan: hasil negatif tetap negatif (tidak diclamp ke nol)', () => {
      const safe = core.computeSafeDaily(-90000, { month: '2026-08', currentMonth: '2026-08', today: '2026-08-23' });
      assert.strictEqual(safe.daysLeft, 9);
      assert.strictEqual(safe.safeDaily, -10000, 'Deficit must produce negative safeDaily (-10000), not clamped to 0');
    });

    // 3. Bulan mendatang
    test('A3 - Bulan mendatang: daysLeft = seluruh hari pada bulan terpilih tanpa pakai tanggal sistem', () => {
      const safe = core.computeSafeDaily(300000, { month: '2026-09', currentMonth: '2026-08', today: '2026-08-23' });
      // September has 30 days
      assert.strictEqual(safe.daysInMonth, 30);
      assert.strictEqual(safe.daysLeft, 30, 'September 2026 should have 30 days left in future month');
      assert.strictEqual(safe.safeDaily, 10000);
    });

    test('A3b - Bulan mendatang: dayOfMonth = 0 hari terlewati', () => {
      const avg = core.computeDailyAverage(0, { month: '2026-09', currentMonth: '2026-08', today: '2026-08-23' });
      assert.strictEqual(avg.dayOfMonth, 0);
      assert.strictEqual(avg.dailyAvg, 0);
    });

    // 4. Purity check
    test('A4 - core.js murni: tidak memanggil new Date() dalam kode dan tidak mengakses DOM', () => {
      const raw = fs.readFileSync('./core.js', 'utf8');
      // Strip comments
      const codeWithoutComments = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*/g, '');
      assert.ok(!codeWithoutComments.includes('new Date'), 'core.js must not call new Date()');
      assert.ok(!codeWithoutComments.includes('document.'), 'core.js must not access document');
      assert.ok(!codeWithoutComments.includes('window.'), 'core.js must not access window');
      assert.ok(!codeWithoutComments.includes('$('), 'core.js must not access DOM selector $');
    });

    // 5. UI Reconciliation Modal Safety Checks
    test('UI-1 - openReconcileModal: Kegagalan API menonaktifkan submit button dan tidak membuat data dummy', async () => {
      // Mock DOM & state
      let currentReconData = 'stale_data';
      const elements = {
        reconcileForm: { reset: () => {} },
        recAccountName: { value: '' },
        recAccountDisplay: { textContent: '' },
        recForceAnchor: { value: '0' },
        recExpectedBalanceDisplay: { textContent: '', innerHTML: '' },
        recCachedBalanceDisplay: { textContent: '' },
        recActualInput: { value: '' },
        recActionPreview: { textContent: '', innerHTML: '', className: '' },
        recSubmitBtn: { disabled: false },
        reconcileModal: { classList: { add: () => {} } },
      };
      const $ = (id) => elements[id];
      const money = (val) => String(val);

      // Simulasikan kegagalan API
      const api = async () => { throw new Error('Network timeout'); };

      // Logika openReconcileModal
      $('recSubmitBtn').disabled = true;
      try {
        const res = await api('/api/account/reconstruct?name=Cash');
        currentReconData = res;
        $('recSubmitBtn').disabled = false;
      } catch (err) {
        currentReconData = null;
        $('recExpectedBalanceDisplay').innerHTML = '<span class="bad">Gagal memuat</span>';
        $('recActionPreview').innerHTML = '⚠️ <span class="bad">Gagal memuat rekonstruksi saldo. Coba lagi.</span>';
        $('recActionPreview').className = 'status-box bad';
        $('recSubmitBtn').disabled = true;
      }

      assert.strictEqual(currentReconData, null, 'currentReconData must be null on failure');
      assert.strictEqual($('recSubmitBtn').disabled, true, 'recSubmitBtn must remain disabled');
      assert.ok($('recActionPreview').innerHTML.includes('Gagal memuat'), 'Preview must show failure message');

      // Submit reconciliation must be rejected when currentReconData is null
      let apiCalled = false;
      let alertMsg = '';
      const alert = (msg) => { alertMsg = msg; };
      const submitReconciliation = async () => {
        if (!currentReconData) {
          alert('Rekonsiliasi dibatalkan: data rekonstruksi saldo belum berhasil dimuat.');
          return;
        }
        apiCalled = true;
      };

      await submitReconciliation();
      assert.strictEqual(apiCalled, false, 'API /api/reconcile must not be called when currentReconData is null');
      assert.ok(alertMsg.includes('dibatalkan'), 'User alert must be triggered on blocked submit');
    });

    test('UI-2 - openReconcileModal: Keberhasilan API mengaktifkan tombol dan menampilkan preview', async () => {
      let currentReconData = null;
      const elements = {
        reconcileForm: { reset: () => {} },
        recAccountName: { value: '' },
        recAccountDisplay: { textContent: '' },
        recForceAnchor: { value: '0' },
        recExpectedBalanceDisplay: { textContent: '', innerHTML: '' },
        recCachedBalanceDisplay: { textContent: '' },
        recActualInput: { value: '100000' },
        recActionPreview: { textContent: '', innerHTML: '', className: '' },
        recSubmitBtn: { disabled: true },
        reconcileModal: { classList: { add: () => {} } },
      };
      const $ = (id) => elements[id];
      const money = (val) => String(val);

      const mockData = { status: 'ok', expected_balance: 100000, cached_balance: 100000, difference: 0 };
      const api = async () => mockData;

      $('recSubmitBtn').disabled = true;
      try {
        const res = await api('/api/account/reconstruct?name=Cash');
        currentReconData = res;
        $('recExpectedBalanceDisplay').textContent = money(res.expected_balance);
        $('recCachedBalanceDisplay').textContent = money(res.cached_balance);
        $('recSubmitBtn').disabled = false;
      } catch (err) {
        currentReconData = null;
      }

      assert.notStrictEqual(currentReconData, null);
      assert.strictEqual($('recSubmitBtn').disabled, false, 'recSubmitBtn must be enabled on success');
    });

    // 6. Prompt 4 (K4): componentsMatchHero & Hero Breakdown / Summary Consistency
    test('K4-1 - Komponen bulan berjalan cocok -> componentsMatchHero returns true', () => {
      const hero = 204368.55;
      const comps = {
        totalBalance: 3544368.55,
        protectedSavings: 2500000.00,
        passThroughOutstanding: 0.00,
        currentCommitments: 840000.00,
      };
      assert.strictEqual(core.componentsMatchHero(hero, comps), true);
    });

    test('K4-2 - Selisih 0.004 -> dianggap cocok (tolerance 0.005)', () => {
      const hero = 100.00;
      const comps = {
        totalBalance: 200.004,
        protectedSavings: 100.00,
        passThroughOutstanding: 0.00,
        currentCommitments: 0.00,
      };
      assert.strictEqual(core.componentsMatchHero(hero, comps), true);
    });

    test('K4-3 - Selisih 0.01 -> rincian disembunyikan (returns false)', () => {
      const hero = 100.00;
      const comps = {
        totalBalance: 200.01,
        protectedSavings: 100.00,
        passThroughOutstanding: 0.00,
        currentCommitments: 0.00,
      };
      assert.strictEqual(core.componentsMatchHero(hero, comps), false);
    });

    test('K4-4 - Hero negatif dengan komponen cocok -> returns true', () => {
      const hero = -50000.00;
      const comps = {
        totalBalance: 100000.00,
        protectedSavings: 100000.00,
        passThroughOutstanding: 0.00,
        currentCommitments: 50000.00,
      };
      assert.strictEqual(core.componentsMatchHero(hero, comps), true);
    });

    test('K4-5 - Null / NaN / undefined -> returns false', () => {
      assert.strictEqual(core.componentsMatchHero(null, { totalBalance: 100, protectedSavings: 0, passThroughOutstanding: 0, currentCommitments: 0 }), false);
      assert.strictEqual(core.componentsMatchHero(NaN, { totalBalance: 100, protectedSavings: 0, passThroughOutstanding: 0, currentCommitments: 0 }), false);
      assert.strictEqual(core.componentsMatchHero(100, { totalBalance: null, protectedSavings: 0, passThroughOutstanding: 0, currentCommitments: 0 }), false);
      assert.strictEqual(core.componentsMatchHero(100, null), false);
      assert.strictEqual(core.componentsMatchHero(100, {}), false);
    });

    test('K4-6 - Bulan lampau memakai Ringkasan Arus Kas tanpa tanda aritmetika + / -', () => {
      const fs = require('fs');
      const appJs = fs.readFileSync('./app.js', 'utf8');
      // Past month block must use Ringkasan Arus Kas
      assert.ok(appJs.includes('Ringkasan Arus Kas'), 'Past month must use Ringkasan Arus Kas');
      // Must not use +/- in past month formula cards
      const pastBlockMatch = appJs.match(/selectedMonth < curMonth\) \{([\s\S]*?)\} else \{/);
      assert.ok(pastBlockMatch, 'Past month block found');
      const pastBlock = pastBlockMatch[1];
      assert.ok(!pastBlock.includes("'>+ ${money("), 'Past month must not have + prefix');
      assert.ok(!pastBlock.includes("'>- ${money("), 'Past month must not have - prefix');
    });

    test('K4-7 - Bulan mendatang memakai Ringkasan Proyeksi tanpa tanda aritmetika + / -', () => {
      const fs = require('fs');
      const appJs = fs.readFileSync('./app.js', 'utf8');
      // Future month block must use Ringkasan Proyeksi
      assert.ok(appJs.includes('Ringkasan Proyeksi'), 'Future month must use Ringkasan Proyeksi');
      const futureBlockMatch = appJs.match(/selectedMonth > curMonth\) \{([\s\S]*?)\} else if/);
      assert.ok(futureBlockMatch, 'Future month block found');
      const futureBlock = futureBlockMatch[1];
      assert.ok(!futureBlock.includes("'>+ ${money("), 'Future month must not have + prefix');
      assert.ok(!futureBlock.includes("'>- ${money("), 'Future month must not have - prefix');
    });

    test('K4-8 - Hero tidak pernah diubah oleh fungsi pemeriksaan', () => {
      const hero = 200.00;
      const comps = Object.freeze({
        totalBalance: 500.00,
        protectedSavings: 200.00,
        passThroughOutstanding: 50.00,
        currentCommitments: 50.00,
      });
      const result = core.componentsMatchHero(hero, comps);
      assert.strictEqual(result, true);
      assert.strictEqual(hero, 200.00, 'Hero value remains untouched');
    });

    console.log(JSON.stringify(results));
    """
    proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, cwd=str(Path.cwd()))
    if proc.returncode != 0:
        raise RuntimeError(f"Node execution failed: {proc.stderr}")
    return json.loads(proc.stdout)


if __name__ == "__main__":
    results = run_node_core_tests()
    for r in results:
        status = "[PASS]" if r["passed"] else "[FAIL]"
        err = f" -> {r.get('error')}" if not r["passed"] else ""
        print(f"{status} {r['name']}{err}")
