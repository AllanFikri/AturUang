/**
 * Money Tracks V12 — Frontend Client Script
 */

const state = {
  data: null,
  tx: [],
  budget: null,
  accounts: null,
  upcoming: [],
  reports: null,
  updates: null,
  page: 'home',
  hideBalances: localStorage.getItem('hideBalances') === 'true',
};

const $ = (id) => document.getElementById(id);

function getLocalDateString(d = new Date()) {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function getLocalTimeString(d = new Date()) {
  const hours = String(d.getHours()).padStart(2, '0');
  const minutes = String(d.getMinutes()).padStart(2, '0');
  return `${hours}:${minutes}`;
}

function toggleBalancePrivacy() {
  state.hideBalances = !state.hideBalances;
  localStorage.setItem('hideBalances', state.hideBalances ? 'true' : 'false');
  updatePrivacyEyeUI();
  renderHome();
}

function updatePrivacyEyeUI() {
  const btn = $('privacyEyeBtn');
  if (!btn) return;
  const isHidden = state.hideBalances;
  btn.title = isHidden ? 'Tampilkan Saldo (Sensitif)' : 'Sembunyikan Saldo (Mode Privasi)';
  btn.innerHTML = isHidden
    ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.733 5.076a10.744 10.744 0 0 1 1.267-.076c7 0 10 7 10 7a13.16 13.16 0 0 1-1.670 2.678"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/></svg>`
    : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`;
}

function relativeTimeLabel(dueDateStr, dueTimeStr) {
  if (!dueDateStr) return 'Belum ada jadwal';
  const now = new Date();
  const todayStr = getLocalDateString(now);
  if (dueDateStr === todayStr) {
    return dueTimeStr ? `Hari ini, jam ${dueTimeStr} WIB` : 'Hari ini';
  }
  const target = new Date(dueDateStr + 'T00:00:00');
  const today = new Date(todayStr + 'T00:00:00');
  const diffDays = Math.round((target - today) / (1000 * 60 * 60 * 24));
  if (diffDays < 0) {
    return `Terlambat ${Math.abs(diffDays)} hari`;
  }
  if (diffDays === 1) {
    return dueTimeStr ? `Besok, jam ${dueTimeStr} WIB` : 'Besok';
  }
  return `${diffDays} hari lagi${dueTimeStr ? `, jam ${dueTimeStr} WIB` : ''}`;
}

const money = (v) => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  if (state.hideBalances) return 'Rp ••••••••';
  const num = Number(v);
  const hasDecimals = num % 1 !== 0;
  const formatted = new Intl.NumberFormat('id-ID', {
    style: 'currency',
    currency: 'IDR',
    maximumFractionDigits: hasDecimals ? 2 : 0,
    minimumFractionDigits: hasDecimals ? 2 : 0,
  }).format(num);
  return formatted.replace(/\s+/g, '');
};

const esc = (s) =>
  String(s ?? '').replace(
    /[&<>"']/g,
    (m) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      }[m])
  );

const slug = (s) =>
  String(s || '')
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^a-z0-9-]/g, '');

const TYPE_ID = { Income: 'Pemasukan', Expense: 'Pengeluaran', Transfer: 'Transfer', Adjustment: 'Penyesuaian' };

const STATUS_ID = {
  Confirmed: 'Sudah Dicek',
  'Auto-classified': 'Dikenali Otomatis',
  'Provisional Neutral': 'Belum Jelas - Tidak Dihitung',
  Upcoming: 'Belum Dibayar',
  Tentative: 'Tentatif',
  Cancelled: 'Dibatalkan',
  Active: 'Aktif',
  Achieved: 'Tercapai',
  Released: 'Dilepaskan',
  Paid: 'Sudah Dibayar',
  Skipped: 'Dilewati',
  Safe: 'Aman',
  Watch: 'Perlu Dipantau',
  Over: 'Melebihi Anggaran',
  'Unplanned Spending': 'Di Luar Anggaran',
};

const CAT_ID = {
  'Main Meals': 'Makan Utama',
  Snacks: 'Camilan',
  'Cafe & Drinks': 'Kafe & Minuman',
  'Groceries & Daily Needs': 'Belanja Harian',
  Fuel: 'Bensin',
  'Parking/Toll': 'Parkir / Tol',
  'Transport / Other Transport': 'Transportasi Lain',
  'Vehicle Service': 'Servis Kendaraan',
  Fashion: 'Fashion',
  Electronics: 'Elektronik',
  'Personal Care': 'Perawatan Diri',
  Health: 'Kesehatan',
  Education: 'Pendidikan',
  'Campus & Organization': 'Kampus & Organisasi',
  'Phone & Internet': 'Pulsa & Internet',
  Subscriptions: 'Langganan',
  'Gifts & Giving': 'Hadiah & Pemberian',
  Travel: 'Perjalanan',
  'Other / Miscellaneous': 'Lain-lain',
  'Research — Historical Only': 'Riwayat Dana Riset',
};

const WHOM_ID = {
  'Personal / Self': 'Pribadi / Diri Sendiri',
  'Pacar / Partner': 'Pacar / Pasangan',
  'Keluarga / Family': 'Keluarga',
  'Teman / Friends': 'Teman',
  'Shared / Group': 'Bersama / Grup',
  'Sedekah / Charity': 'Sedekah / Donasi',
  Other: 'Lainnya',
};

const CONTEXT_ID = {
  Personal: 'Uang Pribadi',
  'Pass-through': 'Uang Titipan / Uang Lewat',
  'Historical Research': 'Riwayat Dana Riset',
};

const KIND_ID = {
  Owned: 'Milik Sendiri',
  Receivable: 'Uang yang Dipinjamkan',
  Suspense: 'Belum Jelas',
  External: 'Pihak Luar',
};

const trType = (x) => TYPE_ID[x] || x || '—';
const trStatus = (x) => STATUS_ID[x] || x || '—';
const trCat = (x) => CAT_ID[x] || x || '—';
const trWhom = (x) => WHOM_ID[x] || x || '—';
const trContext = (x) => CONTEXT_ID[x] || x || '—';
const trKind = (x) => KIND_ID[x] || x || '—';

function applyTheme(v) {
  document.documentElement.dataset.theme = v === 'midnight' ? '' : v;
  localStorage.setItem('moneyTracksTheme', v);
  if ($('themeSelect')) $('themeSelect').value = v;
}

window._modalFocusStack = [];

function openModal(id, triggerEl = null) {
  const modal = $(id);
  if (!modal) return;
  const trigger = triggerEl || document.activeElement;
  window._modalFocusStack.push({ id, trigger });
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');

  setTimeout(() => {
    const focusables = Array.from(modal.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter(el => el.offsetParent !== null);
    if (focusables.length > 0) {
      focusables[0].focus();
    } else {
      const box = modal.querySelector('.modal-box') || modal;
      box.setAttribute('tabindex', '-1');
      box.focus();
    }
  }, 40);
}

function closeModal(id) {
  if (id === 'txModal' && typeof dismissTxSuggestions === 'function') {
    dismissTxSuggestions();
  }
  const modal = $(id);
  if (!modal) return;
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');

  let entry = null;
  for (let i = window._modalFocusStack.length - 1; i >= 0; i--) {
    if (window._modalFocusStack[i].id === id) {
      entry = window._modalFocusStack.splice(i, 1)[0];
      break;
    }
  }
  if (!entry && window._modalFocusStack.length > 0) {
    entry = window._modalFocusStack.pop();
  }

  if (entry && entry.trigger && typeof entry.trigger.focus === 'function') {
    entry.trigger.focus();
  }
}

function goPage(page) {
  state.page = page;
  document.querySelectorAll('.page').forEach((x) => x.classList.toggle('active', x.id === page));
  const sub = ['upcoming', 'thirdparty', 'provisional', 'updates'];
  const navPage = sub.includes(page) ? 'more' : page;
  document
    .querySelectorAll('#nav button')
    .forEach((x) => {
      const isActive = x.dataset.page === navPage;
      x.classList.toggle('active', isActive);
      if (isActive) {
        x.setAttribute('aria-current', 'page');
      } else {
        x.removeAttribute('aria-current');
      }
    });

  // Mark active sub-item in Lainnya page if applicable
  document.querySelectorAll('.menu-list-item').forEach((item) => {
    const onclickStr = item.getAttribute('onclick') || '';
    const matchesChild = onclickStr.includes(`'${page}'`) || onclickStr.includes(`"${page}"`);
    item.classList.toggle('active-subitem', matchesChild);
  });

  if (page === 'transactions') loadTransactions();
  if (page === 'budget') loadBudget();
  if (page === 'upcoming') loadUpcoming();
  if (page === 'thirdparty') loadThirdparty();
  if (page === 'accounts') loadAccounts();
  if (page === 'provisional') loadProvisional();
  if (page === 'reports') loadReports();
  if (page === 'updates') loadUpdates();
  if (page === 'review') loadReviewQueue();
  if (page === 'import') loadWatchedFolderStatus();
}

document.querySelectorAll('#nav button').forEach((b) => (b.onclick = () => goPage(b.dataset.page)));

function typePill(t) {
  return `<span class="pill ${slug(t)}">${esc(trType(t))}</span>`;
}

function statusPill(s) {
  return `<span class="pill ${slug(s)}">${esc(trStatus(s))}</span>`;
}

function arrow(t) {
  const a = t.account_from || '—';
  const b = t.account_to || '—';
  return `${esc(a)}${b && b !== '—' ? ` <span class="muted">→</span> ${esc(b)}` : ''}`;
}

const _pendingIdempotencyKeys = new Map();

async function submitIdempotent(actionKey, buttonEl, requestFn) {
  let key = _pendingIdempotencyKeys.get(actionKey);
  if (!key) {
    key = (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : ('idemp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8));
    _pendingIdempotencyKeys.set(actionKey, key);
  }
  let origText = '';
  let origAriaLabel = null;
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.setAttribute('aria-busy', 'true');
    origText = buttonEl.textContent || '';
    buttonEl.setAttribute('data-original-text', origText);
    if (buttonEl.hasAttribute('aria-label')) {
      origAriaLabel = buttonEl.getAttribute('aria-label');
      buttonEl.setAttribute('data-orig-aria-label', origAriaLabel);
    }
    buttonEl.textContent = 'Memproses...';
    buttonEl.setAttribute('aria-label', 'Memproses permintaan...');
  }
  try {
    const res = await requestFn(key);
    _pendingIdempotencyKeys.delete(actionKey);
    return res;
  } finally {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.removeAttribute('aria-busy');
      if (buttonEl.hasAttribute('data-original-text')) {
        buttonEl.textContent = buttonEl.getAttribute('data-original-text');
        buttonEl.removeAttribute('data-original-text');
      }
      if (buttonEl.hasAttribute('data-orig-aria-label')) {
        buttonEl.setAttribute('aria-label', buttonEl.getAttribute('data-orig-aria-label'));
        buttonEl.removeAttribute('data-orig-aria-label');
      } else {
        buttonEl.removeAttribute('aria-label');
      }
    }
  }
}


function setMoneyInput(fieldOrId, val, allowDecimals = true) {
  const el = typeof fieldOrId === 'string' ? $(fieldOrId) : fieldOrId;
  if (!el) return;
  if (val === '' || val === null || val === undefined) {
    el.value = '';
    return;
  }
  el.value = formatMoneyInput(val, allowDecimals);
}

function attachLiveMoneyFormatting(inputEl, allowNegative = false, allowDecimals = true) {
  if (!inputEl) return;
  inputEl.addEventListener('input', () => {
    let raw = inputEl.value;
    if (!raw) return;

    const cursorPos = inputEl.selectionStart || raw.length;
    const digitsBefore = raw.slice(0, cursorPos).replace(/[^\d]/g, '').length;
    const isNeg = allowNegative && raw.trim().startsWith('-');

    let clean = raw.replace(/[^\d,\.]/g, '');
    if (!clean) {
      inputEl.value = isNeg ? '-' : '';
      return;
    }

    if (allowDecimals && clean.endsWith(',')) {
      const parts = clean.split(',');
      const intNum = Number(parts[0].replace(/\./g, '')) || 0;
      inputEl.value = (isNeg ? '-' : '') + intNum.toLocaleString('id-ID') + ',';
      return;
    }

    const n = parseMoneyInput((isNeg ? '-' : '') + clean);
    const formatted = formatMoneyInput(n, allowDecimals);
    inputEl.value = formatted;

    let newCursor = 0;
    let countedDigits = 0;
    for (let i = 0; i < formatted.length; i++) {
      if (/\d/.test(formatted[i])) {
        countedDigits++;
      }
      if (countedDigits >= digitsBefore) {
        newCursor = i + 1;
        break;
      }
    }
    if (newCursor === 0) newCursor = formatted.length;
    try {
      inputEl.setSelectionRange(newCursor, newCursor);
    } catch (_) {}
  });

  inputEl.addEventListener('paste', (e) => {
    const pasteText = (e.clipboardData || window.clipboardData)?.getData('text');
    if (pasteText) {
      e.preventDefault();
      const n = parseMoneyInput(pasteText);
      inputEl.value = formatMoneyInput(n, allowDecimals);
      inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
}

function setFieldError(inputEl, msg) {
  if (!inputEl) return;
  inputEl.setAttribute('aria-invalid', 'true');
  let errEl = $(inputEl.id + 'Error');
  if (!errEl) {
    errEl = document.createElement('div');
    errEl.id = inputEl.id + 'Error';
    errEl.className = 'field-error';
    errEl.setAttribute('role', 'alert');
    errEl.setAttribute('aria-live', 'assertive');
    errEl.style.cssText = 'color:var(--rose);font-size:11px;font-weight:600;margin-top:4px;';
    if (inputEl.parentElement) {
      inputEl.parentElement.appendChild(errEl);
    }
  }
  errEl.textContent = msg;
  inputEl.setAttribute('aria-describedby', errEl.id);
  inputEl.focus();

  const onClean = () => {
    inputEl.removeAttribute('aria-invalid');
    inputEl.removeAttribute('aria-describedby');
    if (errEl && errEl.parentElement) errEl.parentElement.removeChild(errEl);
    inputEl.removeEventListener('input', onClean);
    inputEl.removeEventListener('change', onClean);
  };
  inputEl.addEventListener('input', onClean);
  inputEl.addEventListener('change', onClean);
}

function getLocalCsrfToken() {
  if (typeof document !== 'undefined') {
    const meta = document.querySelector('meta[name="aturuang-csrf-token"]');
    if (meta && meta.content) return meta.content;
  }
  if (typeof window !== 'undefined' && window.__ATURUANG_CSRF__) {
    return window.__ATURUANG_CSRF__;
  }
  return '';
}

async function api(url, opt = {}) {
  const headers = { ...(opt.headers || {}) };
  const token = getLocalCsrfToken();
  if (token) {
    if (!headers['X-CSRF-Token']) headers['X-CSRF-Token'] = token;
    if (!headers['X-AturUang-Auth']) headers['X-AturUang-Auth'] = token;
  }
  if (opt.method && opt.method.toUpperCase() === 'POST') {
    if (!headers['Idempotency-Key'] && !headers['idempotency-key']) {
      const key = (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : ('idemp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8));
      headers['Idempotency-Key'] = key;
    }
  }
  const r = await fetch(url, { ...opt, headers });
  const j = await r.json().catch(() => ({ error: 'Jawaban dari aplikasi tidak dapat dibaca' }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

async function load() {
  const month = $('monthSelect')?.value || '';
  const [dashData, accData, forecastData, recData, freshData, insightsData] = await Promise.all([
    api('/api/dashboard?month=' + encodeURIComponent(month)),
    api('/api/accounts'),
    api('/api/forecast?days=' + (state.forecastHorizon || 30)).catch(() => ({ status: 'error' })),
    api('/api/insights/recurring').catch(() => ({ status: 'error' })),
    api('/api/accounts/freshness').catch(() => ({ status: 'error' })),
    api('/api/insights').catch(() => ({ status: 'error' }))
  ]);
  state.data = dashData;
  state.accounts = accData;
  if (forecastData?.status === 'ok') state.forecast = forecastData.forecast;
  if (recData?.status === 'ok') state.recurringPatterns = recData.patterns || [];
  if (freshData?.status === 'ok') state.accountsFreshness = freshData.accounts || [];
  if (insightsData?.status === 'ok') state.insights = insightsData.insights;

  renderMonthOptions();
  renderHome();
  renderForecast();
  renderRecurringPatterns();

  // Dynamic Auto-Update Trigger for Active Views
  if (state.page === 'transactions') loadTransactions();
  if (state.page === 'budget') loadBudget();
  if (state.page === 'upcoming') loadUpcoming();
  if (state.page === 'accounts') loadAccounts();
  if (state.page === 'provisional') loadProvisional();
  if (state.page === 'reports') loadReports();
  if (state.page === 'updates') loadUpdates();
}

function renderMonthOptions() {
  const current = $('monthSelect')?.value;
  const ms = state.data.months || [];
  if ($('monthSelect')) {
    $('monthSelect').innerHTML = ms
      .map(
        (m) =>
          `<option value="${m}">${new Date(m + '-01T00:00:00').toLocaleDateString('id-ID', {
            month: 'long',
            year: 'numeric',
          })}</option>`
      )
      .join('');
    $('monthSelect').value = ms.includes(current) ? current : state.data.month;
  }
}

function startLiveClock() {
  function updateClock() {
    const badge = $('liveTimeBadge');
    if (!badge) return;
    const now = new Date();
    const days = ['Minggu', 'Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu'];
    const months = ['Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'];

    const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
    const wib = new Date(utc + (3600000 * 7));

    const dayName = days[wib.getDay()];
    const dateNum = wib.getDate();
    const monthName = months[wib.getMonth()];
    const year = wib.getFullYear();
    const hh = String(wib.getHours()).padStart(2, '0');
    const mm = String(wib.getMinutes()).padStart(2, '0');
    const ss = String(wib.getSeconds()).padStart(2, '0');

    badge.textContent = `${dayName}, ${dateNum} ${monthName} ${year} · ${hh}.${mm}.${ss} WIB`;
  }
  updateClock();
  setInterval(updateClock, 1000);
}

function renderHome() {
  const d = state.data;
  const k = d.kpis;
  const hasPlan = !!k.hasBudgetPlan;

  // Saldo Watchdog Banner (B1 bagian 2)
  if ($('balanceDiscrepancyBanner')) {
    const disc = d.balance_discrepancies || [];
    if (disc.length > 0) {
      $('balanceDiscrepancyBanner').style.display = 'block';
      $('balanceDiscrepancyBanner').innerHTML = `
        <div class="card" style="margin-bottom:14px;border:1px solid var(--amber);background:rgba(245,158,11,0.08);padding:14px">
          <div class="between" style="gap:12px;align-items:flex-start">
            <div>
              <div style="font-weight:700;color:var(--amber);font-size:14px">⚠️ Perhatian: Verifikasi Saldo Memerlukan Audit</div>
              <div class="tiny muted" style="margin-top:4px;line-height:1.6">
                ${disc.map(x => x.status === 'unverifiable'
                  ? `• <b>${esc(x.account_name)}</b>: Saldo tidak dapat diverifikasi (${esc(x.reason)})`
                  : `• <b>${esc(x.account_name)}</b>: Saldo tercatat <b>${money(x.cached_balance)}</b> vs hitungan mutasi <b>${money(x.expected_balance)}</b> (selisih <b>${money(x.difference)}</b>)`
                ).join('<br>')}
              </div>
              <div class="tiny muted" style="margin-top:6px">Silakan lakukan pencocokan/audit saldo di menu Rekening untuk merekonsiliasi.</div>
            </div>
            <button class="btn small warn" onclick="goPage('accounts')" style="white-space:nowrap">⚖️ Rekonsiliasi Saldo</button>
          </div>
        </div>
      `;
    } else {
      $('balanceDiscrepancyBanner').style.display = 'none';
      $('balanceDiscrepancyBanner').innerHTML = '';
    }
  }

  updatePrivacyEyeUI();

  if ($('homeMonthLabel')) $('homeMonthLabel').textContent = new Date(d.month + '-01T00:00:00').toLocaleDateString('id-ID', {
    month: 'long',
    year: 'numeric',
  });
  if ($('kTotalBalance')) $('kTotalBalance').textContent = `+ ${money(k.totalBalance)}`;
  if ($('kProtected')) $('kProtected').textContent = `- ${money(k.protectedSavings)}`;
  if ($('kPass')) $('kPass').textContent = `- ${money(k.passThroughOutstanding)}`;
  const curMonth = d.time_machine?.current_month || new Date().toISOString().slice(0, 7);
  const selectedMonth = d.month;
  const monthNameStr = new Date(selectedMonth + '-01T00:00:00').toLocaleDateString('id-ID', { month: 'long', year: 'numeric' });

  if (selectedMonth > curMonth) {
    // Future Month Mode (e.g. September 2026)
    if ($('kHeroTitle')) $('kHeroTitle').textContent = `Proyeksi Sisa Ruang Belanja (${monthNameStr})`;
    if ($('kSafe')) $('kSafe').textContent = money(hasPlan ? k.remainingBudget : k.safeToSpend);
    if ($('kStatusBadge')) {
      $('kStatusBadge').textContent = 'Bulan Mendatang';
      $('kStatusBadge').className = 'pill info';
    }
    if ($('kFormulaDetails')) $('kFormulaDetails').style.display = 'block';
    if ($('kFormulaSummaryTitle')) $('kFormulaSummaryTitle').textContent = `🔍 Ringkasan Proyeksi (${monthNameStr})`;
    if ($('kFormulaCard1')) $('kFormulaCard1').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--green)">Ekspektasi Pemasukan</div><div style="font-size:14px;font-weight:800;color:var(--green);margin-top:4px">${money(k.expectedIncome || 0)}</div>`;
    if ($('kFormulaCard2')) $('kFormulaCard2').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--amber)">Target Tabungan</div><div style="font-size:14px;font-weight:800;color:var(--amber);margin-top:4px">${money(k.savingsTarget || 0)}</div>`;
    if ($('kFormulaCard3')) $('kFormulaCard3').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--blue)">Titipan Aktif</div><div style="font-size:14px;font-weight:800;color:var(--blue);margin-top:4px">${money(k.passThroughOutstanding || 0)}</div>`;
    if ($('kFormulaCard4')) $('kFormulaCard4').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--red)">Tagihan Terjadwal</div><div style="font-size:14px;font-weight:800;color:var(--red);margin-top:4px">${money(k.currentCommitments || 0)}</div>`;
  } else if (selectedMonth < curMonth) {
    // Past Month Mode (e.g. Juli 2026)
    const surplus = (k.income || 0) - (k.spent || 0);
    if ($('kHeroTitle')) $('kHeroTitle').textContent = `Surplus Kas (${monthNameStr})`;
    if ($('kSafe')) $('kSafe').textContent = money(surplus);
    if ($('kStatusBadge')) {
      $('kStatusBadge').textContent = 'Riwayat Lampau';
      $('kStatusBadge').className = surplus >= 0 ? 'pill good' : 'pill bad';
    }
    if ($('kFormulaDetails')) $('kFormulaDetails').style.display = 'block';
    if ($('kFormulaSummaryTitle')) $('kFormulaSummaryTitle').textContent = `🔍 Ringkasan Arus Kas (${monthNameStr})`;
    if ($('kFormulaCard1')) $('kFormulaCard1').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--green)">Pemasukan Murni</div><div style="font-size:14px;font-weight:800;color:var(--green);margin-top:4px">${money(k.income || 0)}</div>`;
    if ($('kFormulaCard2')) $('kFormulaCard2').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--red)">Pengeluaran Pribadi</div><div style="font-size:14px;font-weight:800;color:var(--red);margin-top:4px">${money(k.spent || 0)}</div>`;
    if ($('kFormulaCard3')) $('kFormulaCard3').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--blue)">Titipan Aktif</div><div style="font-size:14px;font-weight:800;color:var(--blue);margin-top:4px">${money(k.passThroughOutstanding || 0)}</div>`;
    if ($('kFormulaCard4')) $('kFormulaCard4').innerHTML = `<div class="eyebrow" style="font-size:10px;color:var(--amber)">Surplus Tabungan</div><div style="font-size:14px;font-weight:800;color:var(--amber);margin-top:4px">${money(surplus)}</div>`;
  } else {
    // Current Month Mode (Real-Time Spot Cash)
    if ($('kHeroTitle')) $('kHeroTitle').textContent = 'DANA TERSEDIA SAAT INI';
    if ($('kSafe')) {
      $('kSafe').textContent = money(k.safeToSpend);
      $('kSafe').className = 'big ' + (k.safeToSpend < 0 ? 'bad' : 'good');
    }
    if ($('kStatusBadge')) {
      const status = k.statusKondisi || (k.safeToSpend < 0 ? 'Defisit' : k.safeToSpend < (k.remainingBudget || 0) ? 'Perlu Perhatian' : 'Terkendali');
      $('kStatusBadge').textContent = status;
      $('kStatusBadge').className = `pill ${status === 'Defisit' ? 'bad' : status === 'Perlu Perhatian' ? 'warn' : 'good'}`;
    }

    const heroVal = k.safeToSpend;
    const comps = {
      totalBalance: k.totalBalance,
      emergencyAllocated: k.emergencyAllocated !== undefined ? k.emergencyAllocated : k.protectedSavings,
      goalsAllocated: k.goalsAllocated || 0,
      generalAllocated: k.generalAllocated || 0,
      protectedSavings: k.protectedSavings,
      passThroughOutstanding: k.passThroughOutstanding || 0,
      currentCommitments: k.effectiveConfirmedCommitments !== undefined ? k.effectiveConfirmedCommitments : (k.currentCommitments || 0),
      effectiveConfirmedCommitments: k.effectiveConfirmedCommitments !== undefined ? k.effectiveConfirmedCommitments : (k.currentCommitments || 0),
      tentativeReserved: k.tentativeReserved || 0,
      pendingExpenses: k.pendingExpenses || 0,
    };
    const isMatch = typeof componentsMatchHero === 'function' && componentsMatchHero(heroVal, comps);

    if (isMatch) {
      if ($('kFormulaDetails')) $('kFormulaDetails').style.display = 'block';
      if ($('kFormulaMismatchNotice')) $('kFormulaMismatchNotice').style.display = 'none';
      if ($('kFormulaCardsContainer')) $('kFormulaCardsContainer').style.display = 'grid';
      if ($('kFormulaSummaryTitle')) $('kFormulaSummaryTitle').textContent = '🔍 Rincian Dana';

      if ($('kFormulaCard1')) $('kFormulaCard1').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--green)">Total Aset Likuid</div><div style="font-size:13px;font-weight:800;color:var(--green);margin-top:2px">+ ${money(k.totalBalance || 0)}</div>`;
      if ($('kFormulaCard2')) $('kFormulaCard2').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--amber)">Dana Darurat</div><div style="font-size:13px;font-weight:800;color:var(--amber);margin-top:2px">- ${money(comps.emergencyAllocated || 0)}</div>`;
      if ($('kFormulaCard3')) $('kFormulaCard3').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--red)">Komitmen Pasti</div><div style="font-size:13px;font-weight:800;color:var(--red);margin-top:2px">- ${money(comps.effectiveConfirmedCommitments || 0)}</div>`;
      if ($('kFormulaCard4')) $('kFormulaCard4').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--blue)">Dana Tujuan Teralokasi</div><div style="font-size:13px;font-weight:800;color:var(--blue);margin-top:2px">- ${money((comps.goalsAllocated || 0) + (comps.generalAllocated || 0))}</div>`;

      // Conditional cards
      if ($('kFormulaCardCustody')) {
        if ((k.passThroughOutstanding || 0) > 0) {
          $('kFormulaCardCustody').style.display = 'block';
          $('kFormulaCardCustody').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--purple)">Titipan Aktif</div><div style="font-size:13px;font-weight:800;color:var(--purple);margin-top:2px">- ${money(k.passThroughOutstanding)}</div>`;
        } else {
          $('kFormulaCardCustody').style.display = 'none';
        }
      }

      if ($('kFormulaCardReceivable')) {
        if ((k.receivablesOutstanding || 0) > 0) {
          $('kFormulaCardReceivable').style.display = 'block';
          $('kFormulaCardReceivable').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--muted)">Aset Non-Likuid (Piutang)</div><div style="font-size:13px;font-weight:800;color:var(--muted);margin-top:2px">${money(k.receivablesOutstanding)}</div>`;
        } else {
          $('kFormulaCardReceivable').style.display = 'none';
        }
      }

      if ($('kFormulaCardPending')) {
        if ((k.pendingExpenses || 0) > 0) {
          $('kFormulaCardPending').style.display = 'block';
          $('kFormulaCardPending').innerHTML = `<div class="eyebrow" style="font-size:9px;color:var(--amber)">Estimasi Pending</div><div style="font-size:13px;font-weight:800;color:var(--amber);margin-top:2px">- ${money(k.pendingExpenses)}</div>`;
        } else {
          $('kFormulaCardPending').style.display = 'none';
        }
      }
    } else {
      if ($('kFormulaDetails')) $('kFormulaDetails').style.display = 'block';
      if ($('kFormulaMismatchNotice')) $('kFormulaMismatchNotice').style.display = 'block';
      if ($('kFormulaCardsContainer')) $('kFormulaCardsContainer').style.display = 'none';
    }
  }

  // Render Adaptive 2-Column Pocket Cards (with zero balance filter toggle)
  renderHomeAccountsScroll(state.accounts?.accounts || []);

  // Update 3 Micro-Info Cards
  if ($('microUpcomingVal')) {
    const monthCommitments = k.currentCommitments || k.upcoming || 0;
    $('microUpcomingVal').textContent = money(monthCommitments);
    $('microUpcomingNote').textContent = monthCommitments > 0 ? 'Kewajiban aktif bulan ini' : 'Tidak ada tagihan';
  }
  if ($('microReviewVal')) {
    const count = k.provisionalCount || 0;
    $('microReviewVal').textContent = `${count} Items`;
    $('microReviewNote').textContent = count > 0 ? `Total Nilai: ${money(k.provisionalGross || 0)}` : 'Terverifikasi';
    if ($('reviewBadgeCount')) $('reviewBadgeCount').textContent = count;
  }
  const ctx = {
    month: selectedMonth,
    currentMonth: curMonth,
    today: d.time_machine?.today || getLocalDateString(),
  };

  if ($('microSafeDailyVal')) {
    const availableForMonth = hasPlan ? (k.remainingBudget || 0) : (k.safeToSpend || 0);
    const { daysLeft, safeDaily } = computeSafeDaily(availableForMonth, ctx);
    $('microSafeDailyVal').textContent = `${money(safeDaily)} / hr`;
    $('microSafeDailyVal').className = 'value ' + (safeDaily < 0 ? 'bad' : 'good');
    if (selectedMonth < curMonth) {
      $('microSafeDailyNote').textContent = 'Bulan telah berakhir';
    } else if (selectedMonth > curMonth) {
      $('microSafeDailyNote').textContent = `Alokasi untuk ${daysLeft} hari bulan depan`;
    } else {
      $('microSafeDailyNote').textContent = safeDaily < 0
        ? `Dana kurang untuk sisa ${daysLeft} hari`
        : `Sisa ${daysLeft} hari bulan ini`;
    }
  }

  // Kondisi & Kesehatan Finansial Bulan Ini KPIs
  if ($('homeSavingsRateVal')) {
    const inc = k.income || 0;
    const spent = k.spent || 0;
    const surplus = inc - spent;
    const rate = inc > 0 ? ((surplus / inc) * 100).toFixed(1) : 0;
    $('homeSavingsRateVal').textContent = inc > 0 ? `${rate}%` : '—';
    $('homeSavingsRateVal').className = `value ${rate >= 20 ? 'good' : rate > 0 ? 'info' : 'bad'}`;
    $('homeSavingsRateNote').textContent = inc > 0 ? `${money(surplus)} tersimpan` : 'Belum ada pemasukan';
  }

  if ($('homeDailyAvgVal')) {
    const spent = k.spent || 0;
    const { dayOfMonth, dailyAvg } = computeDailyAverage(spent, ctx);
    $('homeDailyAvgVal').textContent = `${money(dailyAvg)} / hr`;
    $('homeDailyAvgNote').textContent = selectedMonth < curMonth
      ? `Rata-rata ${dayOfMonth} hari penuh`
      : selectedMonth > curMonth
      ? 'Belum ada hari berjalan'
      : `Rata-rata s.d. hari ke-${dayOfMonth}`;
  }

  if ($('homeBudgetSpaceVal')) {
    $('homeBudgetSpaceVal').textContent = hasPlan ? money(k.remainingBudget) : money(k.safeToSpend);
    $('homeBudgetSpaceNote').textContent = hasPlan ? 'Sisa alokasi anggaran' : 'Uang bebas yang tersedia';
  }

  if ($('planHeadline')) $('planHeadline').textContent = hasPlan ? `${money(k.spent)} dari ${money(k.budgetTotal)} terpakai` : `${money(k.spent)} total pengeluaran riwayat`;
  if ($('planBar')) {
    let pct = k.budgetTotal > 0 ? Math.max(0, Math.min(100, (k.spent / k.budgetTotal) * 100)) : 0;
    $('planBar').style.width = pct + '%';
    if ($('planBar').parentElement) $('planBar').parentElement.classList.toggle('red', pct > 100);
  }
  if ($('homeBudgetRemaining')) $('homeBudgetRemaining').textContent = hasPlan ? money(k.remainingBudget) : '—';
  if ($('homeBudget')) $('homeBudget').textContent = hasPlan ? money(k.budgetTotal) : '—';
  if ($('homeActual')) $('homeActual').textContent = money(k.spent);
  if ($('homeUpcoming')) $('homeUpcoming').textContent = money(k.upcoming);
  if ($('homeCommitments')) $('homeCommitments').textContent = `- ${money(k.currentCommitments)}`;

  $('homeInsights').innerHTML = (d.insights || [])
    .slice(0, 2)
    .map(
      (x) =>
        `<div class="insight"><strong class="${x.level}">${esc(x.title)}</strong><div class="small muted">${esc(
          x.text
        )}</div></div>`
    )
    .join('');

  // Category Spending List displaying ALL active categories with spacious padding
  if ($('homeTopCatsList')) {
    const allCats = (d.categorySpend || []).filter((c) => c.amount > 0);
    const totalSpent = allCats.reduce((sum, c) => sum + c.amount, 0);
    if (allCats.length === 0) {
      $('homeTopCatsList').innerHTML = '<div class="tiny muted" style="padding:16px 0;text-align:center">Belum ada pengeluaran bulan ini</div>';
    } else {
      let rowsHtml = allCats.map((c) => {
        const pct = totalSpent > 0 ? ((c.amount / totalSpent) * 100).toFixed(1) : 0;
        return `<tr>
          <td style="padding:10px 8px;font-weight:700"><b>${esc(trCat(c.name))}</b></td>
          <td style="padding:10px 12px;width:38%"><div class="bar" style="height:6px;margin-bottom:3px"><span style="width:${pct}%"></span></div><span class="tiny muted" style="font-size:11px">${pct}%</span></td>
          <td class="amount bad" style="padding:10px 8px;text-align:right"><b>${money(c.amount)}</b></td>
        </tr>`;
      }).join('');
      $('homeTopCatsList').innerHTML = `<div class="table-wrap" style="border:0"><table style="width:100%"><thead><tr><th style="padding:8px">Kategori</th><th style="padding:8px 12px">Porsi</th><th style="text-align:right;padding:8px">Jumlah</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>`;
    }
  }

  // 5 Dynamic Smart Action Recommendations Generator
  if ($('homeActionsList')) {
    const inc = k.income || 0;
    const spent = k.spent || 0;
    const remaining = hasPlan ? (k.remainingBudget || 0) : (k.safeToSpend || 0);
    const now = new Date();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const daysLeft = Math.max(1, daysInMonth - now.getDate() + 1);
    const safeDaily = Math.max(0, remaining / daysLeft);
    const topCat = (d.categorySpend || [])[0];

    const actions = [
      { icon: '⚡', title: 'Alokasi Pengeluaran Harian', text: `Batas pengeluaran harian sebesar ${money(safeDaily)}/hari untuk ${daysLeft} hari sisa bulan ini.` },
      { icon: '🛡️', title: 'Alokasi Tabungan Ideal', text: inc > 0 ? `Rekomendasi alokasi tabungan sebesar ${money(inc * 0.2)} (20% dari pemasukan).` : 'Alokasikan pemasukan baru secara konsisten ke tabungan.' },
      { icon: '⚠️', title: 'Pengeluaran Kategori Utama', text: topCat ? `Kategori ${trCat(topCat.name)} mencatat pengeluaran terbesar (${money(topCat.amount)}).` : 'Pengeluaran bulanan terpantau terkendali.' },
      { icon: '🔄', title: 'Rollover Anggaran Bulanan', text: 'Sisa alokasi anggaran bulan sebelumnya disarankan dialokasikan ke saldo tabungan.' },
      { icon: '💳', title: 'Status Kewajiban Tagihan', text: k.currentCommitments ? `Total kewajiban aktif bulan ini sebesar ${money(k.currentCommitments)}.` : 'Tidak ada kewajiban tagihan aktif.' }
    ];

    $('homeActionsList').innerHTML = actions.map(a => `<div class="insight" style="padding:8px 10px;margin-top:0"><strong style="font-size:11px;color:var(--text)">${esc(a.icon)} ${esc(a.title)}</strong><div class="small muted" style="font-size:11px;margin-top:2px">${esc(a.text)}</div></div>`).join('');
  }

  // Render recent 10 transactions on Beranda
  if ($('homeTx')) {
    const recentTx = (d.transactions || []).slice(0, 10);
    $('homeTx').innerHTML = recentTx.length > 0
      ? recentTx.map((t) => `<tr>
          <td>${esc(t.date)}<div class="tiny muted">${esc(t.time || '')}</div></td>
          <td><b>${esc(t.description)}</b><div class="tiny muted">${esc(t.account_from || '')}${t.account_to ? ' → ' + esc(t.account_to) : ''}</div></td>
          <td>${esc(trCat(t.category))}</td>
          <td class="amount ${t.transaction_type === 'Income' ? 'good' : t.transaction_type === 'Expense' ? 'bad' : 'info'}">${money(t.amount)}</td>
        </tr>`).join('')
      : '<tr><td colspan="4" class="empty">Belum ada transaksi bulan ini</td></tr>';
  }
}

function renderHomeAccountsScroll(accounts) {
  if (!$('homeAccountsScroll')) return;
  const hideZero = $('hideZeroAccounts')?.checked ?? true;

  const allActive = (accounts || []).filter((x) => {
    const isCash = x.name.toLowerCase().includes('cash') || x.name.toLowerCase().includes('tunai');
    return x.active && x.current_balance !== null && (!hideZero || x.current_balance > 0 || isCash);
  });

  if (allActive.length === 0) {
    $('homeAccountsScroll').innerHTML = '<div class="tiny muted">Semua saldo Rp 0 tersembunyi. Hapus centang filter di atas atau klik "Saldo & Poket" untuk mengedit.</div>';
    return;
  }

  // Filter 3 Primary Hero Accounts (MyBCA/BCA Main, SPay/ShopeePay, Cash)
  const heroNames = ['bca main', 'mybca', 'shopeepay', 'spay', 'cash', 'cash wallet'];
  const heroAccs = allActive.filter(x => heroNames.includes(x.name.toLowerCase().trim()));
  const otherAccs = allActive.filter(x => !heroNames.includes(x.name.toLowerCase().trim()));

  let html = '';

  // ROW 1: 3 Columns for 3 Main Primary Accounts
  if (heroAccs.length > 0) {
    html += `<div class="hero-acc-grid">`;
    html += heroAccs.map(x => {
      const nl = x.name.toLowerCase().trim();
      const isBca = nl.includes('bca');
      const isSpay = nl.includes('shopee') || nl.includes('spay');

      const accentBorder = isBca ? '1.5px solid var(--blue)' : isSpay ? '1.5px solid var(--amber)' : '1.5px solid var(--green)';
      const accentBg = isBca ? 'rgba(59,130,246,0.08)' : isSpay ? 'rgba(245,158,11,0.08)' : 'rgba(16,185,129,0.08)';
      const fontColor = isBca ? 'var(--blue)' : isSpay ? 'var(--amber)' : 'var(--green)';
      const icon = isBca ? '💳' : isSpay ? '👛' : '💵';

      return `<div class="card account-hero-card" onclick="openReconcileModal('${esc(x.name)}', ${x.current_balance})" style="padding:10px 12px;background:${accentBg};border:${accentBorder};border-radius:12px;cursor:pointer">
        <div class="between" style="margin-bottom:4px">
          <span style="font-size:11px;font-weight:800;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${icon} ${esc(x.name)}</span>
        </div>
        <div style="font-size:17px;font-weight:850;color:${fontColor};letter-spacing:-0.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${money(x.current_balance)}</div>
      </div>`;
    }).join('');
    html += `</div>`;
  }

  // ROW 2: Micro Compact Boxes for Remaining Secondary Pockets
  if (otherAccs.length > 0) {
    html += `<div class="other-acc-grid">`;
    html += otherAccs.map(x => {
      const icon = x.kind === 'Investment' ? '📈' : x.name.includes('BCA') ? '💳' : x.name.includes('Shopee') || x.name.includes('GoPay') ? '👛' : x.name.includes('Cash') ? '💵' : '🏦';
      return `<div class="card account-mini-card" onclick="openReconcileModal('${esc(x.name)}', ${x.current_balance})" style="padding:8px 10px;background:var(--input);border:1px solid var(--line);border-radius:10px;cursor:pointer">
        <div style="font-size:10px;font-weight:700;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${icon} ${esc(x.name)}</div>
        <div style="font-size:13px;font-weight:800;color:var(--text);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${money(x.current_balance)}</div>
      </div>`;
    }).join('');
    html += `</div>`;
  }

  $('homeAccountsScroll').innerHTML = html;
}

function renderHomePie(catSpend) {
  if (!$('homePieWrap')) return;
  const colors = ['#0ea5a8', '#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#ef4444', '#64748b'];
  const cats = (catSpend || []).filter((x) => x.amount > 0);
  const topCats = cats.slice(0, 4);
  const otherSum = cats.slice(4).reduce((acc, x) => acc + x.amount, 0);
  const pieCats = topCats.map((c, i) => ({ label: trCat(c.name), value: c.amount, color: colors[i % colors.length] }));
  if (otherSum > 0) {
    pieCats.push({ label: 'Lainnya', value: otherSum, color: colors[7] });
  }

  const catTotal = pieCats.reduce((acc, x) => acc + x.value, 0);
  $('homePieWrap').innerHTML = createSvgDonut(pieCats, 26, 42) + `<div class="donut-center"><div class="tiny muted">Pengeluaran</div><div class="big-num">${money(catTotal)}</div></div>`;
  $('homePieLegend').innerHTML = pieCats.map((x) => {
    const pct = catTotal > 0 ? ((x.value / catTotal) * 100).toFixed(1) : 0;
    return `<div class="legend-item"><div class="legend-label"><span class="dot" style="background:${x.color}"></span><b>${esc(x.label)}</b></div><div>${money(x.value)} <span class="tiny muted">(${pct}%)</span></div></div>`;
  }).join('');
}

function setForecastHorizon(days) {
  state.forecastHorizon = days;
  if ($('btnForecast7')) $('btnForecast7').className = days === 7 ? 'btn tiny primary' : 'btn tiny';
  if ($('btnForecast30')) $('btnForecast30').className = days === 30 ? 'btn tiny primary' : 'btn tiny';
  loadForecastData(days);
}

async function loadForecastData(days) {
  try {
    const res = await api('/api/forecast?days=' + days);
    if (res.status === 'ok') {
      state.forecast = res.forecast;
      renderForecast();
    }
  } catch (err) {
    console.error('Failed to load forecast', err);
  }
}

function renderForecast() {
  const fc = state.forecast;
  if (!fc) return;
  const days = fc.horizon_days || 30;
  if ($('forecastEyebrow')) $('forecastEyebrow').textContent = `Proyeksi Saldo Kas (${days} Hari)`;
  if ($('forecastOpeningBal')) $('forecastOpeningBal').textContent = money(fc.opening_balance);
  if ($('forecastConfirmedOut')) $('forecastConfirmedOut').textContent = `- ${money(fc.confirmed.outflow)}`;
  if ($('forecastEstimatedOut')) $('forecastEstimatedOut').textContent = `- ${money(fc.estimated.outflow)}`;
  if ($('forecastProjectedVal')) {
    $('forecastProjectedVal').textContent = money(fc.estimated.projected_balance);
    $('forecastProjectedVal').style.color = fc.estimated.projected_balance >= 0 ? 'var(--green)' : 'var(--red)';
  }
  if ($('forecastLowestVal')) {
    $('forecastLowestVal').textContent = money(fc.estimated.lowest_balance);
    $('forecastLowestVal').style.color = fc.estimated.lowest_balance >= 0 ? 'var(--text)' : 'var(--red)';
  }

  // Warning banner
  const warnBanner = $('forecastWarningBanner');
  if (warnBanner) {
    if (fc.warnings && fc.warnings.length > 0) {
      warnBanner.style.display = 'block';
      warnBanner.innerHTML = fc.warnings.map(w => `• ${esc(w)}`).join('<br>');
    } else {
      warnBanner.style.display = 'none';
    }
  }

  // Timeline list
  const tl = $('forecastTimeline');
  if (tl) {
    const events = fc.estimated.events || [];
    if (events.length === 0) {
      tl.innerHTML = '<div class="tiny muted" style="padding:6px 0">Tidak ada pengeluaran atau komitmen yang dijadwalkan dalam periode ini.</div>';
    } else {
      tl.innerHTML = events.map(ev => {
        const isConf = ev.source === 'upcoming_confirmed';
        const badgeClass = isConf ? 'pill good' : 'pill info';
        const badgeText = isConf ? 'Pasti' : 'Perkiraan';
        return `
          <div class="between" style="padding:6px 8px;background:rgba(255,255,255,0.02);border:1px solid var(--line);border-radius:6px;font-size:12px">
            <div style="display:flex;align-items:center;gap:8px">
              <span class="${badgeClass}" style="font-size:9px;padding:1px 5px">${badgeText}</span>
              <div>
                <b>${esc(ev.title)}</b>
                <div class="tiny muted">${esc(ev.date)} · ${esc(ev.account || '')} ${ev.category ? `(${esc(trCat(ev.category))})` : ''}</div>
              </div>
            </div>
            <div style="font-weight:700;color:${ev.type === 'outflow' ? 'var(--red)' : 'var(--green)'}">
              ${ev.type === 'outflow' ? '-' : '+'} ${money(ev.amount)}
            </div>
          </div>
        `;
      }).join('');
    }
  }
}

function renderRecurringPatterns() {
  const listEl = $('recurringPatternsList');
  if (!listEl) return;
  const patterns = (state.recurringPatterns || []).slice(0, 5);
  if (patterns.length === 0) {
    listEl.innerHTML = '<div class="tiny muted" style="padding:6px 0">Belum ada pola transaksi berulang yang terdeteksi dengan bukti cukup.</div>';
    return;
  }

  listEl.innerHTML = patterns.map((p, idx) => {
    const confBadge = p.confidence === 'high' ? '<span class="pill good" style="font-size:9px">Akurasi Tinggi</span>' : '<span class="pill warn" style="font-size:9px">Akurasi Sedang</span>';
    const hasUp = p.has_existing_upcoming;
    return `
      <div class="insight" style="padding:8px 10px;border-left:3px solid ${p.confidence === 'high' ? 'var(--green)' : 'var(--amber)'}">
        <div class="between" style="align-items:flex-start">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
              <b>${esc(p.title)}</b>
              ${confBadge}
            </div>
            <div class="tiny muted" style="line-height:1.4">
              Median ${money(p.median_amount)} · setiap ~${p.median_interval_days} hari (${esc(p.category)} · ${esc(p.account)})
            </div>
            <div class="tiny muted" style="margin-top:2px;font-style:italic">
              ${esc(p.reason)}
            </div>
            <div class="tiny" style="margin-top:4px;color:var(--text-muted)">
              Perkiraan berikutnya: <b>${esc(p.predicted_next_date)}</b>
            </div>
          </div>
          <div style="margin-left:8px;text-align:right">
            ${hasUp
              ? `<span class="pill" style="font-size:10px;background:rgba(255,255,255,0.06);color:var(--text-muted)">✓ Sudah Ada di Komitmen</span>`
              : `<button class="btn tiny primary" onclick="openRecurringToUpcomingModal(${idx})">+ Jadikan Komitmen</button>`
            }
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function openRecurringToUpcomingModal(patternIndex) {
  const p = state.recurringPatterns[patternIndex];
  if (!p) return;
  if ($('recurringToUpcomingForm')) $('recurringToUpcomingForm').reset();
  $('recUpTitle').value = p.title || p.normalized_merchant;
  setMoneyInput('recUpAmount', p.median_amount, true);
  $('recUpDueDate').value = p.predicted_next_date || getLocalDateString();
  fillSelect('recUpCategory', state.data?.categories || [], trCat);
  if ($('recUpCategory')) $('recUpCategory').value = p.category || 'Other / Miscellaneous';

  if ($('recUpAccount')) {
    const accs = state.accounts?.accounts || [];
    const ownedActive = accs.filter(x => x.active && x.kind === 'Owned');
    $('recUpAccount').innerHTML = `<option value="">-- Pilih Rekening --</option>` +
      ownedActive.map(x => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join('');
    if (p.account && ownedActive.some(x => x.name === p.account)) {
      $('recUpAccount').value = p.account;
    }
  }

  $('recUpReserveNow').checked = true;
  $('recUpNotes').value = `Dibuat dari deteksi pola berulang (${p.support}x, interval ~${p.median_interval_days} hari).`;
  openModal('recurringToUpcomingModal');
}

async function submitRecurringToUpcoming(e) {
  e.preventDefault();
  const btn = $('recUpSubmitBtn');
  const payload = {
    title: $('recUpTitle').value.trim(),
    amount: parseMoneyInput('recUpAmount'),
    due_date: $('recUpDueDate').value,
    category: $('recUpCategory').value,
    account: $('recUpAccount').value,
    reserve_now: $('recUpReserveNow').checked ? 1 : 0,
    notes: $('recUpNotes').value.trim(),
  };

  await submitIdempotent('upcoming.from-recurring', btn, async () => {
    const res = await api('/api/upcoming/from-recurring', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    closeModal('recurringToUpcomingModal');
    showToast(res.message || 'Komitmen berhasil ditambahkan.');
    await load();
  });
}

async function loadTransactions() {
  const q = encodeURIComponent($('txSearch').value || '');
  const s = encodeURIComponent($('txStatusFilter').value || '');
  const m = encodeURIComponent($('monthSelect')?.value || '');
  const j = await api(`/api/transactions?month=${m}&status=${s}&q=${q}`);
  state.tx = j.transactions || [];
  renderTransactions();
}

function formatDateGroupHeader(dateStr) {
  if (!dateStr) return 'Lainnya';
  const now = new Date();
  const todayStr = getLocalDateString(now);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const yesterdayStr = getLocalDateString(yesterday);

  if (dateStr === todayStr) return 'Hari Ini';
  if (dateStr === yesterdayStr) return 'Kemarin';

  const d = new Date(dateStr + 'T00:00:00');
  if (isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString('id-ID', { day: 'numeric', month: 'long', year: 'numeric' });
}

function renderTransactions() {
  const b = $('txBody');
  if (!b) return;
  if (!state.tx || state.tx.length === 0) {
    b.innerHTML = '<tr><td colspan="7" class="empty">Belum ada transaksi</td></tr>';
    return;
  }

  const groups = {};
  state.tx.forEach((t) => {
    const d = t.date || 'Lainnya';
    if (!groups[d]) groups[d] = [];
    groups[d].push(t);
  });

  const sortedDates = Object.keys(groups).sort().reverse();
  let html = '';

  sortedDates.forEach((dateStr) => {
    const headerLabel = formatDateGroupHeader(dateStr);
    html += `<tr class="tx-group-row"><td colspan="7" class="tx-group-header">📅 ${esc(headerLabel)} <span class="tiny muted">(${dateStr})</span></td></tr>`;
    groups[dateStr].forEach((t) => {
      const isLegacyDiscrepancy = (t.budget_rule_version === 'legacy' || !t.budget_rule_version) &&
        t.transaction_type === 'Expense' && t.money_context === 'Personal' &&
        Math.abs(Number(t.budget_effect || 0) - Number(t.amount || 0)) > 0.005;
      const legacyBadge = isLegacyDiscrepancy ? ' <span class="badge warn" style="font-size:10px;padding:2px 6px;border-radius:6px;background:rgba(245,158,11,0.15);color:var(--amber);border:1px solid var(--amber)">Aturan lama—perlu ditinjau</span>' : '';
      const excludedBadge = t.exclude_from_budget ? ' <span class="badge muted" style="font-size:10px;padding:2px 6px;border-radius:6px;background:rgba(156,163,175,0.2);color:var(--muted)">Di luar anggaran</span>' : '';
      html += `<tr>
        <td><span class="tiny muted">${esc(t.time || '')}</span></td>
        <td><b>${esc(t.description)}</b>${legacyBadge}${excludedBadge}<div class="tiny muted">${esc(t.account_from || '')}${t.account_to ? ' → ' + esc(t.account_to) : ''}</div></td>
        <td>${typePill(t.transaction_type)}</td>
        <td>${esc(trCat(t.category))}</td>
        <td>${esc(trWhom(t.for_with_whom))}</td>
        <td class="amount ${t.transaction_type === 'Income' ? 'good' : t.transaction_type === 'Expense' ? 'bad' : 'info'}">${money(t.amount)}</td>
        <td><button class="btn small" onclick="editTx(${t.id})">Lihat / Ubah</button></td>
      </tr>`;
    });
  });

  b.innerHTML = html;
}

$('txSearch').addEventListener('input', () => {
  clearTimeout(window._txTimer);
  window._txTimer = setTimeout(loadTransactions, 220);
});
$('txStatusFilter').onchange = loadTransactions;

function fillSelect(id, values, label = (x) => x) {
  $(id).innerHTML = values.map((x) => `<option value="${esc(x)}">${esc(label(x))}</option>`).join('');
}

function populateAccountSelects(selectedFrom = '', selectedTo = '') {
  const accounts = state.accounts?.accounts || state.data?.accounts || [];
  const ownedAccs = accounts.filter(a => a.active && a.kind === 'Owned');
  const allAccs = accounts.filter(a => a.active);

  const freqOrder = ['BCA Main', 'GoPay', 'ShopeePay', 'Cash', 'BCA Poket: Tabungan'];
  ownedAccs.sort((a, b) => {
    let idxA = freqOrder.indexOf(a.name);
    let idxB = freqOrder.indexOf(b.name);
    if (idxA === -1) idxA = 99;
    if (idxB === -1) idxB = 99;
    return idxA - idxB;
  });

  const fromOptions = `<option value="">-- Pilih rekening asal --</option>` + ownedAccs.map(a => {
    const balStr = a.current_balance !== null ? ` (Saldo: ${money(a.current_balance)})` : '';
    const icon = a.name.includes('BCA') ? '💳' : a.name.includes('Shopee') || a.name.includes('GoPay') ? '👛' : a.name.includes('Cash') ? '💵' : '🏦';
    return `<option value="${esc(a.name)}">${icon} ${esc(a.name)}${balStr}</option>`;
  }).join('');

  const toOptions = `<option value="">-- Pilih rekening tujuan / pihak luar --</option>` + allAccs.map(a => {
    const balStr = a.current_balance !== null ? ` (Saldo: ${money(a.current_balance)})` : '';
    const icon = a.kind === 'Receivable' ? '🤝' : a.name.includes('BCA') ? '💳' : a.name.includes('Shopee') || a.name.includes('GoPay') ? '👛' : a.name.includes('Cash') ? '💵' : '🏦';
    return `<option value="${esc(a.name)}">${icon} ${esc(a.name)}${balStr}</option>`;
  }).join('');

  if ($('fFrom')) $('fFrom').innerHTML = fromOptions;
  if ($('fTo')) $('fTo').innerHTML = toOptions;

  if (selectedFrom && $('fFrom')) $('fFrom').value = selectedFrom;
  if (selectedTo && $('fTo')) $('fTo').value = selectedTo;
}

function setTxType(type) {
  if ($('fType')) $('fType').value = type;
  const types = ['Expense', 'Income', 'Transfer', 'Passthrough', 'Upcoming'];
  const btnIds = {
    Expense: 'btnTypeExpense',
    Income: 'btnTypeIncome',
    Transfer: 'btnTypeTransfer',
    Passthrough: 'btnTypePassthrough',
    Upcoming: 'btnTypeUpcoming',
  };

  types.forEach(t => {
    const btn = $(btnIds[t]);
    if (btn) {
      if (t === type) {
        btn.className = 'mode-card active';
        btn.style.background = 'rgba(16,185,129,0.15)';
        btn.style.borderColor = 'var(--green)';
      } else {
        btn.className = 'mode-card';
        btn.style.background = 'rgba(255,255,255,0.03)';
        btn.style.borderColor = 'var(--line)';
      }
    }
  });

  if (type === 'Expense') {
    if ($('lblFrom')) $('lblFrom').textContent = 'Rekening Asal / Sumber';
    if ($('lblTo')) $('lblTo').textContent = 'Penerima / Keterangan (Opsional)';
    if ($('fContext')) $('fContext').value = 'Personal';
  } else if (type === 'Income') {
    if ($('lblFrom')) $('lblFrom').textContent = 'Sumber Pemasukan / Pengirim';
    if ($('lblTo')) $('lblTo').textContent = 'Rekening Penerima';
    if ($('fContext')) $('fContext').value = 'Personal';
  } else if (type === 'Transfer') {
    if ($('lblFrom')) $('lblFrom').textContent = 'Rekening Asal';
    if ($('lblTo')) $('lblTo').textContent = 'Rekening Tujuan';
    if ($('fContext')) $('fContext').value = 'Personal';
  } else if (type === 'Passthrough') {
    if ($('lblFrom')) $('lblFrom').textContent = 'Sumber Dana / Pihak Asal';
    if ($('lblTo')) $('lblTo').textContent = 'Tujuan Dana / Pihak Tujuan';
    if ($('fContext')) $('fContext').value = 'Pass-through';
  } else if (type === 'Upcoming') {
    if ($('lblFrom')) $('lblFrom').textContent = 'Rekening Pembayar Utama';
    if ($('lblTo')) $('lblTo').textContent = 'Penerima Tagihan';
    if ($('fContext')) $('fContext').value = 'Personal';
  }
  updateBudgetExclusionVisibility();
}

function parseTxAmount(val) {
  if (typeof val === 'number') return val;
  if (!val) return 0;
  const cleaned = String(val).replace(/\D/g, '');
  return parseFloat(cleaned) || 0;
}

function formatAmountInput(input) {
  if (!input) return;
  const raw = parseTxAmount(input.value);
  if (raw === 0) {
    input.value = '';
  } else {
    input.value = raw.toLocaleString('id-ID');
  }
}

function validateTxSteps() {
  const desc = $('fDescription') ? $('fDescription').value.trim() : '';
  const amt = $('fAmount') ? parseMoneyInput($('fAmount').value) : 0;

  const btnNext1 = $('btnNextStep1');
  if (btnNext1) {
    btnNext1.disabled = desc.length === 0;
  }

  const btnNext2 = $('btnNextStep2');
  if (btnNext2) {
    btnNext2.disabled = !amt || amt <= 0;
  }
}

function addQuickAmount(delta) {
  const input = $('fAmount');
  if (!input) return;
  const current = parseTxAmount(input.value);
  const next = current + delta;
  input.value = next.toLocaleString('id-ID');
  validateTxSteps();
}

function autoDetectCategory(text) {
  if (!text) return;
  const lower = text.toLowerCase();
  const catSelect = $('fCategory');
  if (!catSelect) return;

  const kwMap = {
    'Main Meals': ['makan', 'nasi', 'ayam', 'warteg', 'restoran', 'lunch', 'dinner', 'sarapan'],
    'Snacks': ['snack', 'camilan', 'roti', 'kue', 'martabak', 'pisang'],
    'Cafe & Drinks': ['kopi', 'kafe', 'boba', 'teh', 'jus', 'drink', 'starbucks', 'kenangan'],
    'Groceries & Daily Needs': ['belanja', 'indomaret', 'alfa', 'supermarket', 'sabun', 'shampoo', 'beras', 'minyak'],
    'Fuel': ['bensin', 'pertamax', 'pertalite', 'spbu', 'bbm', 'shell'],
    'Parking/Toll': ['parkir', 'tol', 'e-toll'],
    'Transport / Other Transport': ['gojek', 'grab', 'gocar', 'goride', 'bus', 'tiket', 'kereta', 'travel', 'ojek'],
    'Phone & Internet': ['pulsa', 'kuota', 'internet', 'indihome', 'by.u', 'telkomsel', 'xl', 'tri'],
    'Subscriptions': ['netflix', 'spotify', 'youtube', 'chatgpt', 'langganan'],
    'Vehicle Service': ['servis', 'bengkel', 'oli', 'ban', 'cuci motor', 'cuci mobil'],
    'Health': ['obat', 'apotek', 'dokter', 'vitamin', 'kesehatan'],
  };

  for (const [cat, kws] of Object.entries(kwMap)) {
    if (kws.some(kw => lower.includes(kw))) {
      catSelect.value = cat;
      break;
    }
  }
}

function goToTxStep(step) {
  const stepTitles = {
    1: 'Jenis & Deskripsi',
    2: 'Nominal Transaksi',
    3: 'Rekening & Detail Opsional'
  };

  if ($('stepCounterBadge')) $('stepCounterBadge').textContent = `Langkah ${step} dari 3`;
  if ($('txModalTitle')) $('txModalTitle').textContent = stepTitles[step] || 'Transaksi Baru';

  for (let s = 1; s <= 3; s++) {
    const seg = $(`progressSeg${s}`);
    if (seg) {
      if (s <= step) {
        seg.style.background = 'var(--green)';
        seg.style.boxShadow = 'var(--shadow-glow-green)';
      } else {
        seg.style.background = 'var(--line)';
        seg.style.boxShadow = 'none';
      }
    }
  }

  for (let i = 1; i <= 3; i++) {
    const p = $(`txStep${i}`);
    if (p) p.classList.toggle('hidden', i !== step);
  }

  validateTxSteps();
}

let currentTxSuggestions = null;
let txSuggestionTimer = null;
let txSuggestionSeq = 0;

function handleTxDescInput(val) {
  clearTimeout(txSuggestionTimer);
  const isEditing = Boolean($('txId')?.value);
  if (isEditing) {
    dismissTxSuggestions();
    return;
  }
  if (!val || val.trim().length < 2) {
    dismissTxSuggestions();
    return;
  }
  txSuggestionTimer = setTimeout(() => {
    fetchTxSuggestions(val.trim());
  }, 250);
}

async function fetchTxSuggestions(desc) {
  const seq = ++txSuggestionSeq;
  const ttype = $('fType')?.value || 'Expense';
  const tdate = $('fDate')?.value || '';
  try {
    const res = await api('/api/transaction/suggest', {
      method: 'POST',
      body: JSON.stringify({
        description: desc,
        transaction_type: ttype,
        date: tdate
      })
    });
    if (seq !== txSuggestionSeq) return;
    if (res && res.suggestions && Object.keys(res.suggestions).length > 0) {
      currentTxSuggestions = res.suggestions;
      renderTxSuggestions(res.suggestions);
    } else {
      dismissTxSuggestions();
    }
  } catch (e) {
    dismissTxSuggestions();
  }
}

function renderTxSuggestions(suggs) {
  const panel = $('txSuggestionPanel');
  const body = $('suggBody');
  const confEl = $('suggConfidence');
  if (!panel || !body) return;

  const items = [];
  let maxConf = 0;

  if (suggs.category) {
    items.push(`<div><b>Kategori:</b> ${esc(suggs.category.label || suggs.category.value)} <span class="tiny muted">(${esc(suggs.category.reason)})</span></div>`);
    maxConf = Math.max(maxConf, suggs.category.confidence);
  }
  if (suggs.account_from) {
    items.push(`<div><b>Rekening Asal:</b> ${esc(suggs.account_from.value)} <span class="tiny muted">(${esc(suggs.account_from.reason)})</span></div>`);
    maxConf = Math.max(maxConf, suggs.account_from.confidence);
  }
  if (suggs.account_to) {
    items.push(`<div><b>Rekening Tujuan:</b> ${esc(suggs.account_to.value)} <span class="tiny muted">(${esc(suggs.account_to.reason)})</span></div>`);
    maxConf = Math.max(maxConf, suggs.account_to.confidence);
  }

  if (items.length === 0) {
    dismissTxSuggestions();
    return;
  }

  body.innerHTML = items.join('');
  if (confEl) confEl.textContent = `Akurasi: ${Math.round(maxConf * 100)}%`;
  panel.style.display = 'block';
}

function applyTxSuggestions() {
  if (!currentTxSuggestions) return;
  if (currentTxSuggestions.category && $('fCategory')) {
    $('fCategory').value = currentTxSuggestions.category.value;
  }
  if (currentTxSuggestions.account_from && $('fFrom')) {
    $('fFrom').value = currentTxSuggestions.account_from.value;
  }
  if (currentTxSuggestions.account_to && $('fTo')) {
    $('fTo').value = currentTxSuggestions.account_to.value;
  }
  dismissTxSuggestions();
  validateTxSteps();
}

function dismissTxSuggestions() {
  currentTxSuggestions = null;
  const panel = $('txSuggestionPanel');
  if (panel) panel.style.display = 'none';
}

function openTxModal(t = null) {
  dismissTxSuggestions();
  if (!state.data) return;
  if ($('txForm')) $('txForm').reset();
  goToTxStep(1);
  fillSelect('fCategory', state.data.categories, trCat);
  fillSelect('fWhom', state.data.forWithWhom, trWhom);
  fillSelect('fContext', state.data.contexts, trContext);

  populateAccountSelects(t?.account_from || '', t?.account_to || '');

  if ($('txId')) $('txId').value = t?.id || '';
  if ($('txModalTitle')) $('txModalTitle').textContent = t ? 'Ubah Transaksi' : 'Jenis & Deskripsi';
  if ($('deleteTxBtn')) $('deleteTxBtn').classList.toggle('hidden', !t);

  const now = new Date();
  $('fDate').value = t?.date || getLocalDateString(now);
  $('fTime').value = t?.time || getLocalTimeString(now);

  const initialType = t?.transaction_type || 'Expense';
  setTxType(initialType);

  const rawAmt = t?.amount || '';
  setMoneyInput('fAmount', rawAmt, true);
  $('fDescription').value = t?.description || '';
  $('fCategory').value = t?.category || 'Other / Miscellaneous';
  $('fWhom').value = t?.for_with_whom || 'Personal / Self';
  $('fContext').value = t?.money_context || 'Personal';
  $('fStatus').value = t?.status || 'Confirmed';
  $('fSettlement').value = t?.settlement_kind || '';
  $('fSubtype').value = t?.subtype || '';
  
  const isLegacy = t?.budget_rule_version === 'legacy';
  if ($('fBudgetRuleVersion')) $('fBudgetRuleVersion').value = isLegacy ? 'legacy' : 'derived';
  if ($('legacyBudgetNotice')) $('legacyBudgetNotice').style.display = isLegacy ? 'block' : 'none';
  if ($('fExcludeBudget')) $('fExcludeBudget').checked = isLegacy ? (Number(t?.budget_effect) === 0 && t?.transaction_type === 'Expense') : Boolean(t?.exclude_from_budget);
  if ($('fBudgetExclusionReason')) $('fBudgetExclusionReason').value = t?.budget_exclusion_reason || '';
  toggleBudgetExclusionReason();
  updateBudgetExclusionVisibility();

  $('fSources').value = t?.source_refs || '';
  $('fNotes').value = t?.notes || '';

  validateTxSteps();

  openModal('txModal');
}

function toggleBudgetExclusionReason() {
  const isExcluded = $('fExcludeBudget')?.checked;
  if ($('budgetReasonWrap')) {
    $('budgetReasonWrap').style.display = isExcluded ? 'block' : 'none';
  }
}

function updateBudgetExclusionVisibility() {
  const type = $('fType')?.value || 'Expense';
  const context = $('fContext')?.value || 'Personal';
  const show = (type === 'Expense' && context === 'Personal');
  if ($('budgetExclusionField')) {
    $('budgetExclusionField').style.display = show ? 'block' : 'none';
  }
}

function editTx(id) {
  const t = [...(state.tx || []), ...((state.data || {}).transactions || [])].find(
    (x) => Number(x.id) === Number(id)
  );
  if (!t) return;
  if (t.status === 'Confirmed' || t.status === 'Auto-classified') {
    // Canonical Ledger Immutability Rule: POSTED transactions cannot be edited directly!
    openReversalModal(t);
  } else {
    openTxModal(t);
  }
}

function openReversalModal(t) {
  $('reversalForm').reset();
  $('revTxId').value = t.id;
  openModal('reversalModal');
}

async function submitReversal(e) {
  e.preventDefault();
  const txId = $('revTxId').value;
  const reason = $('revReasonInput').value.trim();
  try {
    const res = await api('/api/reversal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: txId, reason }),
    });
    closeModal('reversalModal');
    alert('Koreksi Reversal berhasil diterbitkan.');
    await load();
  } catch (err) {
    alert('Gagal menerbitkan reversal: ' + err.message);
  }
}

let currentReconData = null;

async function openReconcileModal(accountName, bookBalance) {
  $('reconcileForm').reset();
  $('recAccountName').value = accountName;
  $('recAccountDisplay').textContent = accountName;
  $('recForceAnchor').value = '0';
  $('recExpectedBalanceDisplay').textContent = 'Memuat...';
  $('recCachedBalanceDisplay').textContent = money(bookBalance);
  setMoneyInput('recActualInput', bookBalance, true);
  $('recActionPreview').textContent = 'Memuat data rekonstruksi...';
  $('recActionPreview').className = 'status-box';
  if ($('recSubmitBtn')) $('recSubmitBtn').disabled = true;
  openModal('reconcileModal');

  try {
    const res = await api(`/api/account/reconstruct?name=${encodeURIComponent(accountName)}`);
    currentReconData = res;
    if (res.status === 'ok') {
      $('recExpectedBalanceDisplay').textContent = money(res.expected_balance);
      $('recCachedBalanceDisplay').textContent = money(res.cached_balance);
      if ($('recActualInput').value === '' || $('recActualInput').value === 'undefined') {
        setMoneyInput('recActualInput', res.cached_balance, true);
      }
    } else {
      $('recExpectedBalanceDisplay').innerHTML = '<span class="warn">Belum ada anchor</span>';
      $('recCachedBalanceDisplay').textContent = money(res.cached_balance);
      $('recForceAnchor').value = '1';
    }
    calcRecDiff();
    if ($('recSubmitBtn')) $('recSubmitBtn').disabled = false;
  } catch (err) {
    currentReconData = null;
    if ($('recExpectedBalanceDisplay')) $('recExpectedBalanceDisplay').innerHTML = '<span class="bad">Gagal memuat</span>';
    if ($('recActionPreview')) {
      $('recActionPreview').innerHTML = '⚠️ <span class="bad">Gagal memuat rekonstruksi saldo. Coba lagi.</span>';
      $('recActionPreview').className = 'status-box bad';
    }
    if ($('recSubmitBtn')) $('recSubmitBtn').disabled = true;
  }
}

function calcRecDiff() {
  if (!currentReconData) {
    if ($('recActionPreview')) {
      $('recActionPreview').innerHTML = '⚠️ <span class="bad">Gagal memuat rekonstruksi saldo. Coba lagi.</span>';
      $('recActionPreview').className = 'status-box bad';
    }
    if ($('recSubmitBtn')) $('recSubmitBtn').disabled = true;
    return;
  }
  const actualVal = $('recActualInput').value;
  if (actualVal === '') {
    $('recActionPreview').textContent = 'Masukkan saldo sebenarnya.';
    $('recActionPreview').className = 'status-box';
    return;
  }
  const A = Number(actualVal);
  const C = Number(currentReconData.cached_balance || 0);
  const previewEl = $('recActionPreview');

  if (currentReconData.status === 'unverifiable') {
    previewEl.innerHTML = `⚠️ <b>Penetapan Anchor Baru</b>: Rekening belum memiliki anchor terverifikasi. Konfirmasi ini akan menetapkan baseline saldo awal sebesar <b>${money(A)}</b> tanpa membuat mutasi pengeluaran/pemasukan palsu.`;
    previewEl.className = 'status-box warn';
    $('recForceAnchor').value = '1';
    return;
  }

  const E = Number(currentReconData.expected_balance || 0);
  const ledgerAdj = Math.round((A - E) * 100) / 100;
  const cacheDrift = Math.round((A - C) * 100) / 100;

  if (Math.abs(ledgerAdj) >= 0.005) {
    const ttype = ledgerAdj > 0 ? 'Pemasukan' : 'Pengeluaran';
    previewEl.innerHTML = `📝 <b>Penyesuaian Ledger</b>: Akan diterbitkan 1 transaksi penyesuaian <b>[Rekonsiliasi]</b> (${ttype}) sebesar <b>${money(Math.abs(ledgerAdj))}</b> agar saldo ledger dan cache menjadi tepat <b>${money(A)}</b>.`;
    previewEl.className = 'status-box info';
  } else if (Math.abs(cacheDrift) >= 0.005) {
    previewEl.innerHTML = `🔄 <b>Perbaikan Cache Saldo</b>: Saldo ledger sudah tepat (${money(E)}), tetapi saldo tersimpan berbeda (${money(C)}). Nilai cache akan diperbarui ke <b>${money(A)}</b> tanpa membuat transaksi keuangan.`;
    previewEl.className = 'status-box good';
  } else {
    previewEl.innerHTML = `✅ <b>Saldo Cocok</b>: Saldo fisik sama persis dengan mutasi transaksi (${money(E)}). Akan dicatat audit konfirmasi tanpa membuat transaksi penyesuaian.`;
    previewEl.className = 'status-box good';
  }
}

async function submitReconciliation(e) {
  e.preventDefault();
  if (!currentReconData) {
    alert('Rekonsiliasi dibatalkan: data rekonstruksi saldo belum berhasil dimuat.');
    return;
  }
  const name = $('recAccountName').value;
  const actualBal = parseMoneyInput($('recActualInput').value);
  const notes = $('recNotesInput').value.trim();
  const forceAnchor = $('recForceAnchor').value === '1';

  try {
    const res = await api('/api/reconcile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, actual_balance: actualBal, notes, force_anchor: forceAnchor }),
    });
    closeModal('reconcileModal');
    alert(`Rekonsiliasi ${name} selesai.`);
    await load();
  } catch (err) {
    alert('Gagal merekonsiliasi: ' + err.message);
  }
}

$('txForm').onsubmit = async (e) => {
  e.preventDefault();
  const id = $('txId').value;
  const rawType = $('fType').value;

  const desc = $('fDescription').value.trim();
  const amt = parseMoneyInput($('fAmount').value);

  if (!desc) {
    goToTxStep(1);
    setFieldError($('fDescription'), 'Harap isi deskripsi transaksi terlebih dahulu.');
    return;
  }

  if (!amt || amt <= 0) {
    goToTxStep(2);
    setFieldError($('fAmount'), 'Harap masukkan nominal transaksi yang valid.');
    return;
  }

  const now = new Date();
  if (!$('fDate').value) $('fDate').value = getLocalDateString(now);
  if (!$('fTime').value) $('fTime').value = getLocalTimeString(now);

  const fromVal = $('fFrom') ? $('fFrom').value.trim() : '';
  const toVal = $('fTo') ? $('fTo').value.trim() : '';

  if (rawType === 'Upcoming') {
    if (!fromVal) {
      alert('Pilih rekening pembayar kewajiban.');
      goToTxStep(3);
      if ($('fFrom')) $('fFrom').focus();
      return;
    }
    const uPayload = {
      due_date: $('fDate').value,
      due_time: $('fTime').value,
      amount: amt,
      title: desc,
      category: $('fCategory').value || 'Other / Miscellaneous',
      account: fromVal,
      notes: $('fNotes').value || '',
    };
    try {
      await api('/api/upcoming', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(uPayload),
      });
      closeModal('txModal');
      await load();
      if (state.page === 'upcoming') loadUpcoming();
      return;
    } catch (err) {
      alert('Gagal menambah tagihan: ' + err.message);
      return;
    }
  }

  let finalType = rawType;
  let finalContext = $('fContext').value || 'Personal';
  let finalSettlement = $('fSettlement').value || '';

  if (rawType === 'Passthrough') {
    alert('Catatan Titipan, Piutang, dan Utang sekarang dikelola secara terstruktur melalui menu Pihak Ketiga.');
    closeModal('txModal');
    goPage('thirdparty');
    openDebtPositionModal();
    return;
  }

  // Money-moving validation
  if (finalType === 'Expense' && !fromVal) {
    alert('Pilih rekening asal untuk pengeluaran.');
    goToTxStep(3);
    if ($('fFrom')) $('fFrom').focus();
    return;
  }
  if (finalType === 'Income' && !toVal) {
    alert('Pilih rekening tujuan untuk pemasukan.');
    goToTxStep(3);
    if ($('fTo')) $('fTo').focus();
    return;
  }
  if (finalType === 'Transfer') {
    if (!fromVal || !toVal) {
      alert('Pilih rekening asal dan rekening tujuan untuk transfer.');
      goToTxStep(3);
      if (!fromVal && $('fFrom')) $('fFrom').focus();
      else if (!toVal && $('fTo')) $('fTo').focus();
      return;
    }
    if (fromVal === toVal) {
      alert('Rekening asal dan rekening tujuan transfer tidak boleh sama.');
      goToTxStep(3);
      if ($('fTo')) $('fTo').focus();
      return;
    }
  }

  const isExcluded = $('fExcludeBudget')?.checked ? 1 : 0;
  const exclusionReason = ($('fBudgetExclusionReason')?.value || '').trim();
  let ruleVer = $('fBudgetRuleVersion')?.value || 'derived';

  if (finalType === 'Expense' && finalContext === 'Personal') {
    if (isExcluded && !exclusionReason) {
      alert('Alasan pengecualian anggaran (Wajib) harus diisi jika transaksi dikecualikan dari anggaran!');
      goToTxStep(3);
      if ($('fBudgetExclusionReason')) $('fBudgetExclusionReason').focus();
      return;
    }
  }

  const p = {
    id: id ? Number(id) : undefined,
    date: $('fDate').value,
    time: $('fTime').value,
    transaction_type: finalType,
    amount: amt,
    account_from: fromVal,
    account_to: toVal,
    description: desc,
    category: $('fCategory').value || 'Other / Miscellaneous',
    for_with_whom: $('fWhom').value || 'Personal / Self',
    money_context: finalContext,
    status: $('fStatus').value || 'Confirmed',
    settlement_kind: finalSettlement,
    subtype: $('fSubtype').value || '',
    exclude_from_budget: isExcluded,
    budget_exclusion_reason: isExcluded ? exclusionReason : '',
    budget_rule_version: ruleVer,
    source_refs: $('fSources').value || '',
    notes: $('fNotes').value || '',
  };

  try {
    await submitIdempotent('save_tx_' + (id || 'new'), $('txSubmitBtn'), async (key) => {
      return await api(id ? '/api/transaction/update' : '/api/transaction', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
        body: JSON.stringify(p),
      });
    });
    closeModal('txModal');
    await load();
    if (state.page === 'transactions') loadTransactions();
    if (state.page === 'provisional') loadProvisional();
  } catch (err) {
    alert(err.message);
  }
};

async function deleteCurrentTx() {
  const id = Number($('txId').value);
  if (!id || !confirm('Hapus transaksi ini? Cadangan database dibuat terlebih dahulu.')) return;
  try {
    await api('/api/transaction/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    closeModal('txModal');
    await load();
    if (state.page === 'transactions') loadTransactions();
  } catch (e) {
    alert(e.message);
  }
}

function exportCSV() {
  const token = getLocalCsrfToken();
  location.href = '/api/export_csv' + (token ? '?token=' + encodeURIComponent(token) : '');
}

function downloadDB() {
  const token = getLocalCsrfToken();
  location.href = '/api/download_db' + (token ? '?token=' + encodeURIComponent(token) : '');
}

async function loadBudget() {
  state.budget = await api('/api/budget?month=' + encodeURIComponent($('monthSelect').value));
  renderBudget();
  loadReallocations();
}

function renderBudget() {
  const b = state.budget;
  const p = b.plan;
  const hasPlan = !!b.has_budget_plan;

  $('bIncome').textContent = money(b.expected_income);
  $('bSavings').textContent = money(b.protected_savings_target);
  $('bTotal').textContent = hasPlan ? money(b.active_total) : '—';
  $('bGap').textContent = hasPlan ? money(b.funding_gap) : '—';
  $('bGap').className = 'value ' + (hasPlan && b.funding_gap < 0 ? 'bad' : 'good');

  setMoneyInput('planGuaranteed', p.guaranteed_income, false);
  setMoneyInput('planAdditional', p.expected_additional_income, false);
  $('planSavings').value = Number(p.savings_rate) * 100;
  $('planSpendable').textContent = money(b.spendable_after_savings);

  $('budgetBody').innerHTML = b.rows
    .map((r) => {
      const budget = Number(r.current_budget || 0);
      const actual = Number(r.actual || 0);
      const pct = budget > 0 ? (actual / budget) * 100 : 0;
      const thClass = budget > 0 ? (pct >= 100 ? 'threshold-red' : pct >= 80 ? 'threshold-yellow' : 'threshold-green') : '';
      const rollText = r.rollover && r.remaining > 0 ? `<div class="tiny good" style="margin-top:2px">Sisa ${money(r.remaining)} akan ditambahkan ke bulan depan.</div>` : '';
      const suggText = r.suggested_budget !== r.current_budget && hasPlan ? `<div class="tiny muted">Saran (median 6 bln): ${money(r.suggested_budget)}</div>` : '';

      return `<tr class="${thClass}">
        <td><b>${esc(trCat(r.category))}</b>${suggText}${rollText}</td>
        <td><input class="input budget-input" data-cat="${esc(r.category)}" type="number" min="0" value="${Math.round(r.current_budget)}"></td>
        <td class="amount">${money(r.actual)}</td>
        <td class="amount ${hasPlan && r.remaining < 0 ? 'bad' : ''}">${hasPlan ? money(r.remaining) : '—'}</td>
        <td>${statusPill(hasPlan ? r.status_label : 'Confirmed')}</td>
        <td class="amount">${hasPlan && r.safe_daily !== null ? money(r.safe_daily) : '—'}</td>
        <td><label class="toggle"><input class="b-active" data-cat="${esc(r.category)}" type="checkbox" ${r.active ? 'checked' : ''}> ${r.active ? 'Ya' : 'Tidak'}</label></td>
        <td><label class="toggle"><input class="b-roll" data-cat="${esc(r.category)}" type="checkbox" ${r.rollover ? 'checked' : ''}> ${r.rollover ? 'Ya' : 'Tidak'}</label></td>
      </tr>`;
    })
    .join('');

  const cats = b.rows.map((r) => r.category);
  $('reFrom').innerHTML = cats.map((x) => `<option value="${esc(x)}">${esc(trCat(x))}</option>`).join('');
  $('reTo').innerHTML = cats.map((x) => `<option value="${esc(x)}">${esc(trCat(x))}</option>`).join('');
  if (cats.length > 1) $('reTo').selectedIndex = 1;
}

async function savePlan() {
  try {
    await api('/api/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        month: $('monthSelect').value,
        guaranteed_income: parseMoneyInput($('planGuaranteed').value),
        expected_additional_income: parseMoneyInput($('planAdditional').value),
        savings_rate: Number($('planSavings').value || 0) / 100,
      }),
    });
    await load();
    await loadBudget();
  } catch (e) {
    alert(e.message);
  }
}

async function saveBudgets() {
  const rows = [];
  document.querySelectorAll('.budget-input').forEach((x) => {
    const cat = x.dataset.cat;
    rows.push({
      category: cat,
      current_budget: Number(x.value || 0),
      active: document.querySelector(`.b-active[data-cat="${CSS.escape(cat)}"]`).checked,
      rollover: document.querySelector(`.b-roll[data-cat="${CSS.escape(cat)}"]`).checked,
    });
  });

  try {
    await api('/api/budget/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ month: $('monthSelect').value, rows }),
    });
    await load();
    await loadBudget();
  } catch (e) {
    alert(e.message);
  }
}

async function refreshSuggestions() {
  try {
    await api('/api/budget/refresh_suggestions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ month: $('monthSelect').value }),
    });
    await loadBudget();
  } catch (e) {
    alert(e.message);
  }
}

async function applySuggestions() {
  if (
    !confirm(
      'Pakai saran sebagai Anggaran Saat Ini? Anggaran Awal tetap disimpan dan perubahan akan dicatat.'
    )
  )
    return;
  try {
    await api('/api/budget/apply_suggestions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ month: $('monthSelect').value }),
    });
    await load();
    await loadBudget();
  } catch (e) {
    alert(e.message);
  }
}

async function reallocate() {
  try {
    await api('/api/budget/reallocate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        month: $('monthSelect').value,
        from_category: $('reFrom').value,
        to_category: $('reTo').value,
        amount: Number($('reAmount').value || 0),
        note: $('reNote').value,
      }),
    });
    $('reAmount').value = '';
    $('reNote').value = '';
    await load();
    await loadBudget();
  } catch (e) {
    alert(e.message);
  }
}

async function loadReallocations() {
  const j = await api('/api/reallocations?month=' + encodeURIComponent($('monthSelect').value));
  $('reallocationLog').innerHTML =
    (j.reallocations || [])
      .slice(0, 7)
      .map(
        (r) =>
          `${new Date(r.created_at + 'Z').toLocaleString('id-ID')} — <b>${esc(
            trCat(r.from_category)
          )}</b> → <b>${esc(trCat(r.to_category))}</b>: ${money(r.amount)} ${
            r.note ? `<span class="muted">(${esc(r.note)})</span>` : ''
          }`
      )
      .join('<br>') || 'Belum ada pemindahan anggaran.';
}

let _activeUpcomingTab = 'All';

function filterUpcoming(tab) {
  _activeUpcomingTab = tab || 'All';
  ['All', 'Confirmed', 'Tentative', 'Goals', 'Emergency'].forEach((t) => {
    const btn = $('tabUpcoming' + t);
    if (btn) btn.className = `btn small tab-btn ${t === _activeUpcomingTab ? 'active' : ''}`;
  });
  renderUpcomingView();
}

async function loadUpcoming() {
  const [jUp, jGoals] = await Promise.all([
    api('/api/upcoming'),
    api('/api/goals').catch(() => ({ goals: [], emergency_allocated: 0, goals_allocated: 0 })),
  ]);
  state.upcoming = jUp.upcoming || [];
  state.allocationGoals = jGoals.goals || [];
  state.allocationSummary = jGoals;

  const bn = $('backlogNote');
  if (jUp.backlog_note) {
    bn.style.display = 'block';
    bn.innerHTML = '<strong>Kewajiban lama / belum dijadwalkan:</strong> ' + esc(jUp.backlog_note);
  } else if (bn) {
    bn.style.display = 'none';
  }

  renderUpcomingView();
}

function renderUpcomingView() {
  const tab = _activeUpcomingTab || 'All';
  const tableWrap = $('upcomingTableWrap');
  const goalsWrap = $('goalsSectionWrap');
  const emergencyWrap = $('emergencySectionWrap');

  if (tableWrap) tableWrap.style.display = (tab === 'Goals' || tab === 'Emergency') ? 'none' : 'block';
  if (goalsWrap) goalsWrap.style.display = (tab === 'All' || tab === 'Goals') ? 'block' : 'none';
  if (emergencyWrap) emergencyWrap.style.display = (tab === 'All' || tab === 'Emergency') ? 'block' : 'none';

  // 1. Render Goals Cards
  renderGoalsCards(state.allocationGoals || []);

  // 2. Render Emergency Reserve Card
  renderEmergencyCard(state.allocationGoals || [], state.allocationSummary || {});

  // 3. Render Upcoming Table
  if (tableWrap && $('upcomingBody')) {
    let rows = state.upcoming || [];
    if (tab === 'Confirmed') {
      rows = rows.filter((u) => u.status === 'Upcoming' || u.status === 'Confirmed' || u.status === 'Paid');
    } else if (tab === 'Tentative') {
      rows = rows.filter((u) => u.status === 'Tentative');
    }

    $('upcomingBody').innerHTML = rows
      .map((u) => {
        const rel = relativeTimeLabel(u.due_date, u.due_time);
        const isOverdue = (u.status === 'Upcoming' || u.status === 'Confirmed') && rel.includes('Terlambat');
        const due = u.due_date
          ? `<b>${esc(u.due_date)}</b>${u.due_time ? ' ' + esc(u.due_time) + ' WIB' : ''}<div class="tiny ${isOverdue ? 'bad' : 'muted'}">${esc(rel)}</div>`
          : '<span class="muted">Belum ada tanggal</span>';

        const isTentative = u.status === 'Tentative';
        const reserveNote = isTentative
          ? (u.reserve_now
              ? `<div class="tiny good" style="margin-top:3px">🛡️ Sudah dicadangkan dari Dana Tersedia</div>`
              : `<div class="tiny muted" style="margin-top:3px">ℹ️ Belum mengurangi Dana Tersedia</div>`)
          : (u.covered_amount > 0 && u.status === 'Upcoming'
              ? `<div class="tiny" style="color:var(--primary);margin-top:3px">🛡️ Ditutup Alokasi Tujuan: ${money(u.covered_amount)}<br><b>Sisa Beban: ${money(u.effective_amount)}</b></div>`
              : '');

        let actionHtml = '—';
        if (isTentative) {
          actionHtml = `<div class="row" style="gap:4px;flex-wrap:wrap">
            ${u.reserve_now
              ? `<button class="btn tiny" onclick="setUpcomingReserve(${u.id}, 0)" title="Lepaskan pemotongan Dana Tersedia">Lepaskan Cadangan</button>`
              : `<button class="btn tiny" onclick="setUpcomingReserve(${u.id}, 1)" title="Potong langsung dari Dana Tersedia">Cadangkan Sekarang</button>`
            }
            <button class="btn tiny primary" onclick="setUpcomingStatus(${u.id}, 'Upcoming')">Tandai Pasti</button>
            <button class="btn tiny danger" onclick="setUpcomingStatus(${u.id}, 'Cancelled')">Batalkan</button>
          </div>`;
        } else if (u.status === 'Upcoming' || u.status === 'Confirmed') {
          actionHtml = `<div class="row" style="gap:4px">
            <button class="btn small primary" onclick="markPaid(${u.id})">Tandai Dibayar</button>
            <button class="btn small" onclick="skipUpcoming(${u.id})">Lewati</button>
          </div>`;
        } else if (u.transaction_id) {
          actionHtml = `<span class="tiny muted">Transaksi #${u.transaction_id}</span>`;
        } else {
          actionHtml = `<span class="tiny muted">${esc(trStatus(u.status))}</span>`;
        }

        return `<tr>
          <td>${due}</td>
          <td><b>${esc(u.title)}</b><div class="tiny muted">${esc(u.notes || '')}</div>${reserveNote}</td>
          <td>${esc(trCat(u.category))}</td>
          <td>${esc(u.account || '—')}</td>
          <td>${esc(trWhom(u.for_with_whom))}</td>
          <td class="amount">${money(u.amount)}</td>
          <td>${statusPill(u.status)}</td>
          <td>${actionHtml}</td>
        </tr>`;
      })
      .join('') || `<tr><td colspan="8" class="empty">${tab === 'Tentative' ? 'Belum ada rencana tentatif.' : 'Belum ada kewajiban / rencana mendatang.'}</td></tr>`;
  }
}

function renderGoalsCards(goals) {
  const container = $('goalsCardsGrid');
  if (!container) return;
  const activeGoals = (goals || []).filter((g) => g.kind !== 'Emergency');
  if (activeGoals.length === 0) {
    container.innerHTML = `<div class="card" style="padding:16px;grid-column:1/-1;text-align:center">
      <div class="small muted">Belum ada tujuan keuangan aktif. Buat target menabung seperti Liburan, Menikah, atau Gadget.</div>
      <button class="btn small primary" style="margin-top:8px" onclick="openGoalModal()">+ Buat Tujuan Baru</button>
    </div>`;
    return;
  }

  container.innerHTML = activeGoals
    .map((g) => {
      const target = Number(g.target_amount || 0);
      const allocated = Number(g.allocated_amount || 0);
      const pct = target > 0 ? Math.min(100, Math.round((allocated / target) * 100)) : (allocated > 0 ? 100 : 0);
      const shortfall = Math.max(0, target - allocated);

      let monthlySuggestion = '';
      if (g.target_date && shortfall > 0) {
        const today = new Date();
        const tDate = new Date(g.target_date);
        const monthsLeft = Math.max(1, (tDate.getFullYear() - today.getFullYear()) * 12 + (tDate.getMonth() - today.getMonth()));
        const perMonth = Math.round(shortfall / monthsLeft);
        monthlySuggestion = `<div class="tiny muted" style="margin-top:4px">💡 Saran setoran: <b>${money(perMonth)} / bln</b> (sisa ${monthsLeft} bulan)</div>`;
      }

      return `<div class="goal-card card">
        <div class="between">
          <div>
            <b style="font-size:15px;color:var(--text)">${esc(g.name)}</b>
            <div class="tiny muted">Prioritas: ${g.priority || 3} ${g.target_date ? `| Target: ${esc(g.target_date)}` : ''}</div>
          </div>
          <span class="pill ${g.status === 'Active' ? 'good' : 'info'}">${esc(trStatus(g.status))}</span>
        </div>

        <div class="row between" style="align-items:baseline;margin-top:6px">
          <div>
            <div class="tiny muted">Dana Teralokasi</div>
            <div style="font-size:18px;font-weight:800;color:var(--green)">${money(allocated)}</div>
          </div>
          <div style="text-align:right">
            <div class="tiny muted">Target Nominal</div>
            <div style="font-size:14px;font-weight:700;color:var(--text)">${target > 0 ? money(target) : 'Tanpa Batas'}</div>
          </div>
        </div>

        <div class="goal-progress-wrap">
          <div class="goal-progress-bar" style="width:${pct}%"></div>
        </div>
        <div class="between tiny muted">
          <span>${pct}% tercapai</span>
          <span>${shortfall > 0 ? `Kurang ${money(shortfall)}` : 'Target Terpenuhi'}</span>
        </div>
        ${monthlySuggestion}

        <div class="row" style="gap:6px;margin-top:10px;padding-top:8px;border-top:1px dashed var(--line);flex-wrap:wrap">
          <button class="btn tiny primary" onclick="openFundGoalModal(${g.id})">+ Alokasikan Dana</button>
          ${allocated > 0 ? `<button class="btn tiny" onclick="openReleaseGoalModal(${g.id})">Lepaskan Dana</button>` : ''}
          ${allocated > 0 ? `<button class="btn tiny" onclick="openSpendGoalModal(${g.id})">Pakai Dana</button>` : ''}
        </div>
      </div>`;
    })
    .join('');
}

function renderEmergencyCard(goals, summary) {
  const emGoal = (goals || []).find((g) => g.kind === 'Emergency');
  const allocated = emGoal ? Number(emGoal.allocated_amount || 0) : Number(summary.emergency_allocated || 0);
  const target = emGoal ? Number(emGoal.target_amount || 0) : 0;
  const pct = target > 0 ? Math.min(100, Math.round((allocated / target) * 100)) : (allocated > 0 ? 100 : 0);

  if ($('emergencyAmountDisplay')) $('emergencyAmountDisplay').textContent = money(allocated);
  if ($('emergencyTargetDisplay')) $('emergencyTargetDisplay').textContent = target > 0 ? `Target: ${money(target)} (${pct}% tercapai)` : 'Target: Belum ditentukan';
  if ($('emergencyProgressBar')) $('emergencyProgressBar').style.width = `${pct}%`;
  if ($('emergencyProgressNote')) {
    $('emergencyProgressNote').textContent = target > 0
      ? (allocated >= target ? 'Target dana darurat telah terpenuhi penuh.' : `Kekurangan dana darurat: ${money(Math.max(0, target - allocated))}.`)
      : 'Dana darurat murni yang disisihkan untuk keperluan mendesak di luar belanja harian.';
  }
}

function toggleUpcomingReserveCheckbox() {
  const type = $('uStatusType')?.value;
  const wrap = $('uReserveNowWrap');
  const chk = $('uReserveNow');
  if (type === 'Tentative') {
    if (chk) chk.checked = false;
    if (wrap) wrap.style.display = 'block';
  } else {
    if (chk) chk.checked = true;
    if (wrap) wrap.style.display = 'none';
  }
}

async function setUpcomingReserve(upcomingId, reserveNow) {
  try {
    await api('/api/upcoming/reserve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upcoming_id: upcomingId, reserve_now: reserveNow }),
    });
    await load();
    await loadUpcoming();
  } catch (err) {
    alert(err.message);
  }
}

async function setUpcomingStatus(upcomingId, status) {
  try {
    await api('/api/upcoming/reserve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upcoming_id: upcomingId, status: status }),
    });
    await load();
    await loadUpcoming();
  } catch (err) {
    alert(err.message);
  }
}

function openUpcomingModal() {
  fillSelect('uCategory', (state.data?.categories || CATEGORIES).filter((x) => x !== 'Research — Historical Only'), trCat);
  fillSelect('uWhom', state.data?.forWithWhom || FOR_WITH_WHOM, trWhom);
  const accounts = state.accounts?.accounts || state.data?.accounts || [];
  const ownedAccs = accounts.filter((a) => a.active && a.kind === 'Owned');
  if ($('uAccount')) {
    $('uAccount').innerHTML = `<option value="">-- Pilih rekening (opsional) --</option>` +
      ownedAccs.map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join('');
    $('uAccount').value = '';
  }

  // Populate linked goals select
  if ($('uLinkedGoal')) {
    const activeGoals = (state.allocationGoals || []).filter((g) => g.status === 'Active');
    $('uLinkedGoal').innerHTML = `<option value="">-- Tidak ditautkan ke tujuan --</option>` +
      activeGoals.map((g) => `<option value="${g.id}">${esc(g.name)} (Alokasi: ${money(g.allocated_amount || 0)})</option>`).join('');
    $('uLinkedGoal').value = '';
  }

  if ($('upcomingForm')) $('upcomingForm').reset();
  if ($('uStatusType')) $('uStatusType').value = 'Upcoming';
  toggleUpcomingReserveCheckbox();
  if ($('uDate')) $('uDate').value = '';
  if ($('uTime')) $('uTime').value = '';
  openModal('upcomingModal');
}

if ($('upcomingForm')) {
  $('upcomingForm').onsubmit = async (e) => {
    e.preventDefault();
    try {
      const statusType = $('uStatusType')?.value || 'Upcoming';
      const reserveNow = statusType === 'Upcoming' ? 0 : ($('uReserveNow')?.checked ? 1 : 0);
      const linkedGoal = $('uLinkedGoal')?.value ? Number($('uLinkedGoal').value) : null;

      await api('/api/upcoming', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          due_date: $('uDate').value,
          due_time: $('uTime').value,
          title: $('uTitle').value,
          amount: parseMoneyInput($('uAmount').value),
          category: $('uCategory').value,
          account: $('uAccount') ? $('uAccount').value : '',
          for_with_whom: $('uWhom').value,
          status: statusType,
          reserve_now: reserveNow,
          linked_goal_id: linkedGoal,
          notes: $('uNotes').value,
        }),
      });
      closeModal('upcomingModal');
      await loadUpcoming();
      await load();
    } catch (e) {
      alert(e.message);
    }
  };
}

function openGoalModal() {
  if ($('goalForm')) $('goalForm').reset();
  if ($('goalId')) $('goalId').value = '';
  const accounts = state.accounts?.accounts || state.data?.accounts || [];
  const ownedAccs = accounts.filter((a) => a.active && a.kind === 'Owned');
  if ($('gPreferredAccount')) {
    $('gPreferredAccount').innerHTML = `<option value="">-- Rekening Utama --</option>` +
      ownedAccs.map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join('');
  }
  openModal('goalModal');
}

if ($('goalForm')) {
  $('goalForm').onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api('/api/goals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: $('gName').value,
          kind: $('gKind').value,
          target_amount: parseMoneyInput($('gTargetAmount').value),
          initial_funding: parseMoneyInput($('gInitialFunding').value),
          target_date: $('gTargetDate').value,
          priority: Number($('gPriority').value || 3),
          preferred_account: $('gPreferredAccount').value,
          notes: $('gNotes').value,
        }),
      });
      closeModal('goalModal');
      await load();
      await loadUpcoming();
    } catch (err) {
      alert(err.message);
    }
  };
}

function openFundGoalModal(goalId) {
  const g = (state.allocationGoals || []).find((x) => x.id === goalId);
  if (!g) return;
  if ($('fundGoalId')) $('fundGoalId').value = g.id;
  if ($('fundGoalNameDisplay')) $('fundGoalNameDisplay').textContent = g.name;
  if ($('fundGoalAllocatedDisplay')) $('fundGoalAllocatedDisplay').textContent = money(g.allocated_amount || 0);
  if ($('fundGoalTargetDisplay')) $('fundGoalTargetDisplay').textContent = g.target_amount > 0 ? money(g.target_amount) : 'Tanpa Batas';
  if ($('fundGoalAmount')) $('fundGoalAmount').value = '';
  if ($('fundGoalNotes')) $('fundGoalNotes').value = '';
  openModal('fundGoalModal');
}

if ($('fundGoalForm')) {
  $('fundGoalForm').onsubmit = async (e) => {
    e.preventDefault();
    const gid = Number($('fundGoalId').value);
    const amt = parseMoneyInput($('fundGoalAmount').value);
    if (!amt || amt <= 0) {
      alert('Masukkan nominal penambahan dana yang valid.');
      return;
    }
    try {
      await api('/api/goals/fund', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal_id: gid, amount: amt, notes: $('fundGoalNotes').value }),
      });
      closeModal('fundGoalModal');
      await load();
      await loadUpcoming();
    } catch (err) {
      alert(err.message);
    }
  };
}

function openReleaseGoalModal(goalId) {
  const g = (state.allocationGoals || []).find((x) => x.id === goalId);
  if (!g) return;
  if ($('releaseGoalId')) $('releaseGoalId').value = g.id;
  if ($('releaseGoalNameDisplay')) $('releaseGoalNameDisplay').textContent = g.name;
  if ($('releaseGoalAllocatedDisplay')) $('releaseGoalAllocatedDisplay').textContent = money(g.allocated_amount || 0);
  if ($('releaseGoalAmount')) $('releaseGoalAmount').value = formatMoneyInput(g.allocated_amount || 0);
  openModal('releaseGoalModal');
}

if ($('releaseGoalForm')) {
  $('releaseGoalForm').onsubmit = async (e) => {
    e.preventDefault();
    const gid = Number($('releaseGoalId').value);
    const amt = parseMoneyInput($('releaseGoalAmount').value);
    if (amt < 0) {
      alert('Nominal pelepasan tidak boleh negatif.');
      return;
    }
    if (!confirm(`Konfirmasi pelepasan dana sebesar ${money(amt)} kembali ke Dana Tersedia?`)) {
      return;
    }
    try {
      await api('/api/goals/release', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal_id: gid, amount: amt, target_status: $('releaseGoalStatus').value }),
      });
      closeModal('releaseGoalModal');
      await load();
      await loadUpcoming();
    } catch (err) {
      alert(err.message);
    }
  };
}

function openSpendGoalModal(goalId) {
  const g = (state.allocationGoals || []).find((x) => x.id === goalId);
  if (!g) return;
  if ($('spendGoalId')) $('spendGoalId').value = g.id;
  if ($('spendGoalNameDisplay')) $('spendGoalNameDisplay').textContent = g.name;
  if ($('spendGoalAllocatedDisplay')) $('spendGoalAllocatedDisplay').textContent = money(g.allocated_amount || 0);
  if ($('spendGoalAmount')) $('spendGoalAmount').value = '';
  if ($('spendGoalDescription')) $('spendGoalDescription').value = `Penggunaan Tujuan: ${g.name}`;
  if ($('spendGoalDate')) $('spendGoalDate').value = getLocalDateString();

  fillSelect('spendGoalCategory', (state.data?.categories || CATEGORIES).filter((x) => x !== 'Research — Historical Only'), trCat);
  const accounts = state.accounts?.accounts || state.data?.accounts || [];
  const ownedAccs = accounts.filter((a) => a.active && a.kind === 'Owned');
  if ($('spendGoalAccount')) {
    $('spendGoalAccount').innerHTML = ownedAccs.map((a) => `<option value="${esc(a.name)}">${esc(a.name)} (Saldo: ${money(a.current_balance || 0)})</option>`).join('');
  }
  openModal('spendGoalModal');
}

if ($('spendGoalForm')) {
  $('spendGoalForm').onsubmit = async (e) => {
    e.preventDefault();
    const gid = Number($('spendGoalId').value);
    const amt = parseMoneyInput($('spendGoalAmount').value);
    if (!amt || amt <= 0) {
      alert('Masukkan nominal pengeluaran yang valid.');
      return;
    }
    try {
      await api('/api/goals/spend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          goal_id: gid,
          amount: amt,
          account_name: $('spendGoalAccount').value,
          description: $('spendGoalDescription').value,
          category: $('spendGoalCategory').value,
          tx_date: $('spendGoalDate').value,
        }),
      });
      closeModal('spendGoalModal');
      await load();
      await loadUpcoming();
    } catch (err) {
      alert(err.message);
    }
  };
}

function openFundEmergencyModal() {
  let emGoal = (state.allocationGoals || []).find((g) => g.kind === 'Emergency');
  if (!emGoal) {
    if (confirm('Belum ada pos Dana Darurat terdaftar. Buat pos Dana Darurat baru?')) {
      openGoalModal();
      if ($('gName')) $('gName').value = 'Dana Darurat';
      if ($('gKind')) $('gKind').value = 'Emergency';
      if ($('gPriority')) $('gPriority').value = '1';
    }
    return;
  }
  openFundGoalModal(emGoal.id);
}

function openReleaseEmergencyModal() {
  let emGoal = (state.allocationGoals || []).find((g) => g.kind === 'Emergency');
  if (!emGoal) {
    alert('Belum ada pos Dana Darurat.');
    return;
  }
  openReleaseGoalModal(emGoal.id);
}

async function quickAddGoal() {
  const title = ($('allocTitle')?.value || '').trim();
  const amt = parseMoneyInput($('allocAmount')?.value);
  const initFund = parseMoneyInput($('allocInitialFunding')?.value);
  const targetDate = $('allocDate')?.value || '';

  if (!title) {
    alert('Nama tujuan wajib diisi.');
    if ($('allocTitle')) $('allocTitle').focus();
    return;
  }
  try {
    await api('/api/goals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: title,
        kind: 'Goal',
        target_amount: amt,
        initial_funding: initFund,
        target_date: targetDate,
        priority: 3,
      }),
    });
    if ($('allocTitle')) $('allocTitle').value = '';
    if ($('allocAmount')) $('allocAmount').value = '';
    if ($('allocInitialFunding')) $('allocInitialFunding').value = '';
    if ($('allocDate')) $('allocDate').value = '';
    await load();
    if (state.page === 'accounts') loadAccounts();
    alert('Tujuan keuangan berhasil disimpan.');
  } catch (err) {
    alert(err.message);
  }
}

function markPaid(id) {
  const u = state.upcoming.find((x) => x.id === id);
  if (!u) return;

  if ($('payUpcomingId')) $('payUpcomingId').value = u.id;
  if ($('payUpcomingTitleDisplay')) $('payUpcomingTitleDisplay').textContent = u.title;
  if ($('payUpcomingAmountDisplay')) $('payUpcomingAmountDisplay').textContent = money(u.amount);

  const accounts = state.accounts?.accounts || state.data?.accounts || [];
  const ownedAccs = accounts.filter((a) => a.active && a.kind === 'Owned');
  if ($('payUpcomingAccount')) {
    $('payUpcomingAccount').innerHTML = `<option value="">-- Pilih rekening pembayaran --</option>` +
      ownedAccs.map((a) => {
        const balStr = a.current_balance !== null ? ` (Saldo: ${money(a.current_balance)})` : '';
        return `<option value="${esc(a.name)}">${esc(a.name)}${balStr}</option>`;
      }).join('');

    if (u.account && ownedAccs.some((a) => a.name === u.account)) {
      $('payUpcomingAccount').value = u.account;
    } else {
      $('payUpcomingAccount').value = '';
    }
  }

  if ($('payUpcomingDate')) $('payUpcomingDate').value = getLocalDateString();
  openModal('payUpcomingModal');
}

if ($('payUpcomingForm')) {
  $('payUpcomingForm').onsubmit = async (e) => {
    e.preventDefault();
    const id = Number($('payUpcomingId').value);
    const account = $('payUpcomingAccount') ? $('payUpcomingAccount').value.trim() : '';
    const date = $('payUpcomingDate') ? $('payUpcomingDate').value : '';

    if (!account) {
      alert('Pilih rekening pembayaran.');
      if ($('payUpcomingAccount')) $('payUpcomingAccount').focus();
      return;
    }
    if (!date) {
      alert('Pilih tanggal pembayaran.');
      if ($('payUpcomingDate')) $('payUpcomingDate').focus();
      return;
    }

    try {
      await submitIdempotent('pay_upcoming_' + id, $('btnConfirmPayUpcoming'), async (key) => {
        return await api('/api/upcoming/pay', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
          body: JSON.stringify({ id, account, date }),
        });
      });
      closeModal('payUpcomingModal');
      await loadUpcoming();
      await load();
    } catch (err) {
      alert('Gagal membayar kewajiban: ' + err.message);
    }
  };
}

async function skipUpcoming(id) {
  if (!confirm('Lewati kewajiban ini?')) return;
  await api('/api/upcoming/status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, status: 'Skipped' }),
  });
  loadUpcoming();
  load();
}

async function loadAccounts() {
  const [jAccs, jGoals] = await Promise.all([
    api('/api/accounts'),
    api('/api/goals').catch(() => ({ goals: [], emergency_allocated: 0, goals_allocated: 0 })),
  ]);
  state.accounts = jAccs;
  state.allocationGoals = jGoals.goals || [];
  state.allocationSummary = jGoals;
  renderAccounts();
}

function renderAccounts() {
  const a = state.accounts.accounts || [];
  const custAmt = state.dashboard?.kpis?.passThroughOutstanding ?? state.accounts.current_pass_through_outstanding ?? 0;
  if ($('custodyTotalNow')) $('custodyTotalNow').textContent = money(custAmt);
  if ($('custodySummaryText')) $('custodySummaryText').textContent = money(custAmt);
  if ($('passOutstanding')) $('passOutstanding').value = custAmt;
  if ($('passHistoryNote')) $('passHistoryNote').textContent = `Riwayat bersih uang titipan: ${money(
    state.accounts.historical_pass_through_net
  )} (tidak dianggap sebagai titipan yang masih dipegang sekarang).`;

  const summary = state.allocationSummary || {};
  const totalAlloc = (summary.emergency_allocated || 0) + (summary.goals_allocated || 0) + (summary.general_allocated || 0) || (state.accounts.snapshot?.protected_savings || 0);
  if ($('protectedTotalNow')) $('protectedTotalNow').textContent = money(totalAlloc);

  const goals = state.allocationGoals || [];
  if ($('protectedAllocations')) {
    const emGoal = goals.find((g) => g.kind === 'Emergency');
    const activeGoals = goals.filter((g) => g.status === 'Active' && g.kind !== 'Emergency');

    let html = '';
    if (emGoal) {
      html += `<div class="insight" style="margin-top:6px;border-left:3px solid var(--amber)">
        <div class="between">
          <div>
            <b>🛡️ Dana Darurat</b>
            <div class="tiny muted">${money(emGoal.allocated_amount || 0)} ${emGoal.target_amount > 0 ? `(Target: ${money(emGoal.target_amount)})` : ''}</div>
          </div>
          <button class="btn tiny" onclick="openFundEmergencyModal()">+ Dana</button>
        </div>
      </div>`;
    }

    activeGoals.forEach((g) => {
      html += `<div class="insight" style="margin-top:6px;border-left:3px solid var(--blue)">
        <div class="between">
          <div>
            <b>🎯 ${esc(g.name)}</b>
            <div class="tiny muted">${money(g.allocated_amount || 0)} ${g.target_amount > 0 ? `(Target: ${money(g.target_amount)})` : ''}</div>
          </div>
          <button class="btn tiny primary" onclick="openFundGoalModal(${g.id})">+ Dana</button>
        </div>
      </div>`;
    });

    $('protectedAllocations').innerHTML = html || '<div class="tiny muted">Belum ada alokasi tujuan atau dana darurat aktif.</div>';
  }

  if ($('allocAccount')) {
    const prevVal = $('allocAccount').value;
    const ownedActive = a.filter((x) => x.active && x.kind === 'Owned');
    $('allocAccount').innerHTML = `<option value="">-- Pilih rekening --</option>` +
      ownedActive.map(x => `<option value="${esc(x.name)}">${esc(x.name)} (${money(x.current_balance)})</option>`).join('');
    if (prevVal && ownedActive.some(x => x.name === prevVal)) {
      $('allocAccount').value = prevVal;
    }
  }

  if ($('allocUpcoming')) {
    const prevVal = $('allocUpcoming').value;
    const upcomings = state.upcoming || [];
    const activeUpcomings = upcomings.filter(u => u.status === 'Upcoming');
    $('allocUpcoming').innerHTML = `<option value="">-- Tidak menutup kewajiban (Bebas) --</option>` +
      activeUpcomings.map(u => {
        const eff = u.effective_amount !== undefined ? u.effective_amount : u.amount;
        return `<option value="${u.id}">${esc(u.title)} (Sisa beban: ${money(eff)})</option>`;
      }).join('');
    if (prevVal && activeUpcomings.some(u => String(u.id) === String(prevVal))) {
      $('allocUpcoming').value = prevVal;
    }
  }

  // Render Pocket & Account Cards Grid (#accountsPocketGrid)
  if ($('accountsPocketGrid')) {
    const activeAccs = a.filter((x) => {
      const isCash = x.name.toLowerCase().includes('cash') || x.name.toLowerCase().includes('tunai');
      return x.active && x.current_balance !== null && (x.current_balance > 0 || isCash);
    });
    const freshnessMap = {};
    (state.accountsFreshness || []).forEach(f => { freshnessMap[f.name] = f; });

    $('accountsPocketGrid').innerHTML = activeAccs.map((x) => {
      const isProt = x.protected_amount > 0;
      const icon = x.kind === 'Investment' ? '📈' : x.name.includes('BCA') ? '💳' : x.name.includes('Shopee') || x.name.includes('GoPay') ? '👛' : x.name.includes('Cash') ? '💵' : '🏦';
      const fresh = freshnessMap[x.name];
      const freshBadgeClass = fresh?.badge === 'success' ? 'pill good' : fresh?.badge === 'warning' ? 'pill warn' : 'pill';
      const freshLabel = fresh?.status_label || 'Baru diperbarui';
      return `<div class="card account-card card-emerald" style="cursor:pointer;padding:16px" onclick="openReconcileModal('${esc(x.name)}', ${x.current_balance})">
        <div class="between">
          <div class="eyebrow">${icon} ${esc(trKind(x.kind))}</div>
          <span class="${freshBadgeClass}" style="font-size:10px">${esc(freshLabel)}</span>
        </div>
        <div class="value" style="font-size:22px;margin:8px 0">${money(x.current_balance)}</div>
        <div class="between">
          <div class="name" style="font-weight:700">${esc(x.name)}</div>
          ${isProt ? `<div class="tiny warn">🔒 Dijaga ${money(x.protected_amount)}</div>` : ''}
        </div>
        <div class="row" style="margin-top:10px">
          <button class="btn small primary" style="width:100%" onclick="event.stopPropagation();openReconcileModal('${esc(x.name)}', ${x.current_balance})">⚖️ Audit / Cocokkan Saldo</button>
        </div>
      </div>`;
    }).join('');
  }

  if ($('accountGrid')) {
    $('accountGrid').innerHTML = a
      .map((x) => {
        const key = x.name.replace(/[^a-z0-9]/gi, '_');
        return `<div class="card account-card" style="opacity:${x.active ? 1 : 0.65}"><div class="between"><div><div class="eyebrow">${esc(
          trKind(x.kind)
        )}</div><b>${esc(x.name)}</b></div><label class="toggle"><input id="active_${key}" type="checkbox" ${
          x.active ? 'checked' : ''
        }> Aktif</label></div><div class="field" style="margin-top:12px"><label>Saldo saat ini</label><input id="bal_${key}" class="input" type="number" step="0.01" value="${
          x.current_balance ?? ''
        }" placeholder="Belum diisi"></div><div class="field" style="margin-top:8px"><label>Jumlah yang tidak boleh dipakai di rekening ini</label><input id="protamt_${key}" class="input" type="number" step="0.01" min="0" value="${
          x.protected_amount ?? (x.protected && x.current_balance ? x.current_balance : 0)
        }"></div><div class="field" style="margin-top:8px"><label>Tanggal saldo</label><input id="date_${key}" class="input" type="date" value="${
          x.balance_date || getLocalDateString()
        }"></div><div class="row"><button class="btn small primary" onclick='saveAccount(${JSON.stringify(
          x.name
        )},${JSON.stringify(x.kind)})'>Simpan saldo</button><span class="tiny muted">${
          !x.active
            ? 'Tidak aktif — tidak masuk Total Saldo.'
            : x.current_balance === null
            ? 'Perlu diisi agar Dana Tersedia Digunakan bisa dihitung.'
            : ''
        }</span></div></div>`;
      })
      .join('');
  }
}

async function saveAccount(name, kind) {
  const key = name.replace(/[^a-z0-9]/gi, '_');
  try {
    await api('/api/account/balance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        kind,
        current_balance: $('bal_' + key).value,
        balance_date: $('date_' + key).value,
        protected: false,
        protected_amount: Number($('protamt_' + key).value || 0),
        active: $('active_' + key).checked,
      }),
    });
    await loadAccounts();
    await load();
  } catch (e) {
    alert(e.message);
  }
}

async function savePassThrough() {
  alert('Pengaturan manual titipan telah dinonaktifkan. Gunakan modul Pihak Ketiga (Custody / Titipan) berbasis Event Ledger.');
}

async function addProtectedAllocation() {
  const title = $('allocTitle').value.trim();
  const amount = parseMoneyInput($('allocAmount').value);
  const account = $('allocAccount') ? $('allocAccount').value.trim() : '';
  const target_date = $('allocDate').value;
  const covers_upcoming_id = $('allocUpcoming') && $('allocUpcoming').value ? Number($('allocUpcoming').value) : null;

  if (!title || amount <= 0) {
    alert('Isi tujuan dan jumlah lebih dari 0');
    if (!title && $('allocTitle')) $('allocTitle').focus();
    else if ($('allocAmount')) $('allocAmount').focus();
    return;
  }
  if (!account) {
    alert('Pilih rekening penyimpan dana');
    if ($('allocAccount')) {
      $('allocAccount').focus();
      $('allocAccount').style.borderColor = 'var(--rose)';
    }
    return;
  }
  if ($('allocAccount')) $('allocAccount').style.borderColor = '';

  try {
    await api('/api/protected_allocation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, amount, target_date, account, covers_upcoming_id }),
    });
    $('allocTitle').value = '';
    $('allocAmount').value = '';
    if ($('allocAccount')) $('allocAccount').value = '';
    if ($('allocUpcoming')) $('allocUpcoming').value = '';
    $('allocDate').value = '';
    await loadAccounts();
    await loadUpcoming();
    await load();
  } catch (e) {
    alert(e.message);
  }
}

async function setAllocationStatus(id, status) {
  if (!confirm('Ubah status alokasi dana ini?')) return;
  try {
    await api('/api/protected_allocation/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, status }),
    });
    await loadAccounts();
    await loadUpcoming();
    await load();
  } catch (e) {
    alert(e.message);
  }
}

function openLinkAllocationModal(allocId) {
  const alloc = (state.accounts?.protected_allocations || []).find((x) => x.id === allocId);
  if (!alloc) return;

  if ($('linkAllocId')) $('linkAllocId').value = alloc.id;
  if ($('linkAllocTitleDisplay')) $('linkAllocTitleDisplay').textContent = `${alloc.title} (${money(alloc.amount)})`;

  const upcomings = state.upcoming || [];
  const activeUpcomings = upcomings.filter((u) => u.status === 'Upcoming');
  if ($('linkUpcomingSelect')) {
    $('linkUpcomingSelect').innerHTML = `<option value="">-- Pilih kewajiban yang ditutup --</option>` +
      activeUpcomings.map((u) => {
        const eff = u.effective_amount !== undefined ? u.effective_amount : u.amount;
        return `<option value="${u.id}">${esc(u.title)} (Nominal: ${money(u.amount)}, Sisa beban: ${money(eff)})</option>`;
      }).join('');
    $('linkUpcomingSelect').value = '';
  }
  openModal('linkAllocModal');
}

async function submitLinkAllocation(e) {
  e.preventDefault();
  const allocId = Number($('linkAllocId').value);
  const upcomingId = Number($('linkUpcomingSelect').value);
  if (!allocId || !upcomingId) {
    alert('Pilih kewajiban yang ditutup.');
    return;
  }
  try {
    await api('/api/protected_allocation/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allocation_id: allocId, upcoming_id: upcomingId }),
    });
    closeModal('linkAllocModal');
    await loadAccounts();
    await loadUpcoming();
    await load();
  } catch (err) {
    alert('Gagal menautkan alokasi: ' + err.message);
  }
}

async function unlinkAllocation(allocId) {
  if (!confirm('Lepas tautan alokasi ini dari kewajiban? Alokasi akan kembali menjadi pos bebas.')) return;
  try {
    await api('/api/protected_allocation/unlink', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allocation_id: allocId }),
    });
    await loadAccounts();
    await loadUpcoming();
    await load();
  } catch (err) {
    alert('Gagal melepas tautan: ' + err.message);
  }
}

async function loadProvisional() {
  const j = await api('/api/transactions?status=Provisional%20Neutral');
  const t = j.transactions || [];
  const gross = t.reduce((a, x) => a + Number(x.amount), 0);
  $('provisionalSummary').textContent = `${t.length} transaksi • pergerakan kotor ${money(gross)}`;
  $('provisionalBody').innerHTML =
    t
      .map(
        (x) =>
          `<tr><td>${esc(x.date)}</td><td>${arrow(x)}</td><td><b>${esc(x.description)}</b><div class="tiny muted">${esc(
            x.subtype
          )}</div></td><td class="amount">${money(x.amount)}</td><td>${esc(x.notes)}</td><td><button class="btn small" onclick='openProvisionalById(${
            x.id
          })'>Tinjau</button></td></tr>`
      )
      .join('') || '<tr><td colspan="6" class="empty">Tidak ada transaksi Belum Jelas.</td></tr>';
  state.provisional = t;
}

function openProvisionalById(id) {
  const t = state.provisional.find((x) => x.id === id);
  if (t) openTxModal(t);
}

async function loadReports() {
  state.reports = await api('/api/reports');
  renderReports();
}

function renderReports() {
  const pKind = $('reportPeriodSelect')?.value || 'monthly';
  let dataList = state.reports.monthly || [];
  let labelKey = 'month';

  if (pKind === 'trimester') {
    dataList = state.reports.trimester || [];
    labelKey = 'period';
    $('chartPeriodTitle').textContent = 'Pemasukan vs pengeluaran per Caturwulan / Trimester';
    $('tablePeriodTitle').textContent = 'Ringkasan Caturwulan / Trimester (4-Bulanan)';
  } else if (pKind === 'yearly') {
    dataList = state.reports.yearly || [];
    labelKey = 'period';
    $('chartPeriodTitle').textContent = 'Pemasukan vs pengeluaran per Tahun';
    $('tablePeriodTitle').textContent = 'Ringkasan Tahunan';
  } else {
    $('chartPeriodTitle').textContent = 'Pemasukan vs pengeluaran per Bulan';
    $('tablePeriodTitle').textContent = 'Ringkasan Bulanan';
  }

  const max = Math.max(1, ...dataList.flatMap((x) => [x.income, x.expense]));
  $('monthlyChart').innerHTML = dataList
    .map((x) => {
      const lbl = x[labelKey] || '';
      const displayLbl = pKind === 'monthly' ? lbl.slice(5) : lbl;
      return `<div class="spark-col"><div class="tiny muted">${money(x.income)}</div><div class="spark-bars"><div class="spark-bar income" title="Pemasukan ${money(
        x.income
      )}" style="height:${Math.max(2, (x.income / max) * 135)}px"></div><div class="spark-bar expense" title="Pengeluaran ${money(
        x.expense
      )}" style="height:${Math.max(2, (x.expense / max) * 135)}px"></div></div><b class="tiny">${esc(
        displayLbl
      )}</b></div>`;
    })
    .join('');

  const cats = state.reports.categories || [];
  const cm = Math.max(1, ...cats.map((x) => x.amount));
  $('reportCats').innerHTML = cats
    .slice(0, 10)
    .map(
      (x) =>
        `<div class="chart-row"><span>${esc(trCat(x.name))}</span><div class="bar"><span style="width:${Math.max(
          3,
          (x.amount / cm) * 100
        )}%"></span></div><b>${money(x.amount)}</b></div>`
    )
    .join('');

  $('reportMonthlyBody').innerHTML = dataList
    .map(
      (x) =>
        `<tr><td><b>${esc(x[labelKey])}</b></td><td class="amount good">${money(x.income)}</td><td class="amount bad">${money(
          x.expense
        )}</td><td class="amount ${x.surplus >= 0 ? 'good' : 'bad'}">${money(x.surplus)}</td><td class="amount">${money(
          x.research
        )}</td><td class="amount warn">${money(x.provisional)}</td></tr>`
    )
    .join('');

  // Financial Health Indicators & Daily Pulse (Gaya Jago / myBCA / Jenius)
  const totalInc = dataList.reduce((acc, x) => acc + x.income, 0);
  const totalExp = dataList.reduce((acc, x) => acc + x.expense, 0);
  const totalSurplus = Math.max(0, totalInc - totalExp);
  const sRate = totalInc > 0 ? (totalSurplus / totalInc) * 100 : 0;

  if ($('statSavingsRate')) {
    $('statSavingsRate').textContent = sRate.toFixed(1) + '%';
    $('statSavingsRate').className = 'value ' + (sRate >= 20 ? 'good' : sRate >= 10 ? 'warn' : 'bad');
    $('statSavingsRateNote').textContent = sRate >= 20 ? 'Status: Sangat Sehat (Target ≥ 20%)' : sRate >= 10 ? 'Status: Cukup Baik' : 'Status: Waspada';
  }

  const dailyTrend = state.reports.daily_trend || [];
  const daysCount = dailyTrend.length || 30;
  const avgDaily = totalExp > 0 ? (totalExp / (pKind === 'monthly' ? daysCount : daysCount * 4)) : 0;
  if ($('statDailyAvg')) {
    $('statDailyAvg').textContent = money(avgDaily) + ' / hari';
  }

  if ($('statTopCat')) {
    if (cats.length > 0) {
      const top = cats[0];
      const topPct = totalExp > 0 ? ((top.amount / totalExp) * 100).toFixed(0) : 0;
      $('statTopCat').textContent = trCat(top.name);
      $('statTopCatNote').textContent = `${money(top.amount)} (${topPct}% dari pengeluaran)`;
    } else {
      $('statTopCat').textContent = '—';
      $('statTopCatNote').textContent = 'Belum ada pengeluaran';
    }
  }

  if ($('dailyPulseChart')) {
    if (pKind === 'monthly' && dailyTrend.length > 0) {
      $('dailyPulseCard').style.display = 'block';
      const dMax = Math.max(1, ...dailyTrend.map((x) => x.amount));
      $('dailyPulseChart').innerHTML = dailyTrend
        .map(
          (x) =>
            `<div class="spark-col"><div class="spark-bars"><div class="spark-bar expense" title="Tgl ${x.day}: ${money(
              x.amount
            )}" style="height:${Math.max(2, (x.amount / dMax) * 105)}px"></div></div><b class="tiny" style="font-size:9px">${x.day}</b></div>`
        )
        .join('');
    } else {
      $('dailyPulseCard').style.display = 'none';
    }
  }

  renderPieCharts(dataList, cats);
}

function createSvgDonut(items, innerRadius = 28, outerRadius = 44) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  if (total <= 0) {
    return `<div class="tiny muted" style="text-align:center;padding:50px 0">Belum ada data</div>`;
  }

  let startAngle = 0;
  const cx = 50, cy = 50;
  const paths = items.map((item) => {
    const sliceAngle = (item.value / total) * 2 * Math.PI;
    const endAngle = startAngle + sliceAngle;

    const xo1 = cx + outerRadius * Math.cos(startAngle);
    const yo1 = cy + outerRadius * Math.sin(startAngle);
    const xo2 = cx + outerRadius * Math.cos(endAngle);
    const yo2 = cy + outerRadius * Math.sin(endAngle);

    const xi2 = cx + innerRadius * Math.cos(endAngle);
    const yi2 = cy + innerRadius * Math.sin(endAngle);
    const xi1 = cx + innerRadius * Math.cos(startAngle);
    const yi1 = cy + innerRadius * Math.sin(startAngle);

    const largeArc = sliceAngle > Math.PI ? 1 : 0;

    const d = `M ${xo1.toFixed(2)} ${yo1.toFixed(2)} A ${outerRadius} ${outerRadius} 0 ${largeArc} 1 ${xo2.toFixed(2)} ${yo2.toFixed(2)} L ${xi2.toFixed(2)} ${yi2.toFixed(2)} A ${innerRadius} ${innerRadius} 0 ${largeArc} 0 ${xi1.toFixed(2)} ${yi1.toFixed(2)} Z`;

    startAngle = endAngle;
    const pct = ((item.value / total) * 100).toFixed(1);
    return `<path d="${d}" fill="${item.color}"><title>${esc(item.label)}: ${money(item.value)} (${pct}%)</title></path>`;
  }).join('');

  return `<svg viewBox="0 0 100 100">${paths}</svg>`;
}

function renderPieCharts(dataList, cats) {
  const colors = ['#0ea5a8', '#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#ef4444', '#64748b'];

  const topCats = cats.slice(0, 6);
  const otherSum = cats.slice(6).reduce((acc, x) => acc + x.amount, 0);
  const pieCats = topCats.map((c, i) => ({ label: trCat(c.name), value: c.amount, color: colors[i % colors.length] }));
  if (otherSum > 0) {
    pieCats.push({ label: 'Lainnya', value: otherSum, color: colors[7] });
  }

  const catTotal = pieCats.reduce((acc, x) => acc + x.value, 0);
  if ($('categoryPieWrap')) {
    $('categoryPieWrap').innerHTML = createSvgDonut(pieCats) + `<div class="donut-center"><div class="tiny muted">Pengeluaran</div><div class="big-num">${money(catTotal)}</div></div>`;
    $('categoryPieLegend').innerHTML = pieCats.map((x) => {
      const pct = catTotal > 0 ? ((x.value / catTotal) * 100).toFixed(1) : 0;
      return `<div class="legend-item"><div class="legend-label"><span class="dot" style="background:${x.color}"></span><b>${esc(x.label)}</b></div><div>${money(x.value)} <span class="tiny muted">(${pct}%)</span></div></div>`;
    }).join('');
  }

  const totalInc = dataList.reduce((acc, x) => acc + x.income, 0);
  const totalExp = dataList.reduce((acc, x) => acc + x.expense, 0);
  const totalSurplus = Math.max(0, totalInc - totalExp);

  const pieCashflow = [
    { label: 'Pengeluaran Pribadi', value: totalExp, color: '#ef4444' },
    { label: 'Surplus / Tabungan Bersih', value: totalSurplus, color: '#31d7a8' },
  ];
  const totalFlow = totalExp + totalSurplus;
  const surplusPct = totalFlow > 0 ? ((totalSurplus / totalFlow) * 100).toFixed(1) : 0;

  if ($('cashflowPieWrap')) {
    $('cashflowPieWrap').innerHTML = createSvgDonut(pieCashflow) + `<div class="donut-center"><div class="tiny muted">Rasio Surplus</div><div class="big-num good">${surplusPct}%</div></div>`;
    $('cashflowPieLegend').innerHTML = pieCashflow.map((x) => {
      const pct = totalFlow > 0 ? ((x.value / totalFlow) * 100).toFixed(1) : 0;
      return `<div class="legend-item"><div class="legend-label"><span class="dot" style="background:${x.color}"></span><b>${esc(x.label)}</b></div><div>${money(x.value)} <span class="tiny muted">(${pct}%)</span></div></div>`;
    }).join('');
  }
}

function openStatement() {
  const m = $('monthSelect').value || '2026-08';
  window.open('/statement?month=' + encodeURIComponent(m), '_blank');
}

async function loadUpdates() {
  const j = await api('/api/update_info');
  state.updates = j;
  $('updateVersion').textContent = 'Versi ' + (j.current_version || '—');
  $('updateMessage').textContent = j.message || '';
  $('updateManifestUrl').value = j.manifest_url || '';
  $('updateAuto').checked = !!j.auto_check;
}

async function checkUpdate() {
  await loadUpdates();
}

async function saveUpdateSettings() {
  try {
    await api('/api/update_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest_url: $('updateManifestUrl').value.trim(),
        auto_check: $('updateAuto').checked,
      }),
    });
    await loadUpdates();
    alert(
      'Pengaturan pembaruan tersimpan. Kalau cek otomatis aktif, Money Tracks akan memeriksa versi baru saat dibuka.'
    );
  } catch (e) {
    alert(e.message);
  }
}

$('monthSelect').onchange = async () => {
  await load();
  if (state.page === 'transactions') loadTransactions();
  if (state.page === 'budget') loadBudget();
  if (state.page === 'reports') loadReports();
};

$('themeSelect').onchange = (e) => applyTheme(e.target.value);
applyTheme(localStorage.getItem('moneyTracksTheme') || 'midnight');

window.addEventListener('keydown', (e) => {
  const openModals = Array.from(document.querySelectorAll('.modal.open'));
  if (openModals.length > 0) {
    const topModal = openModals[openModals.length - 1];
    if (e.key === 'Escape') {
      e.preventDefault();
      closeModal(topModal.id);
      return;
    }
    if (e.key === 'Tab') {
      const focusables = Array.from(topModal.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter(el => el.offsetParent !== null);
      if (focusables.length > 0) {
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first || !topModal.contains(document.activeElement)) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last || !topModal.contains(document.activeElement)) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    }
  }
  if (e.ctrlKey && e.key.toLowerCase() === 'n') {
    e.preventDefault();
    openTxModal();
  }
});

startLiveClock();
  // Attach live Indonesian money formatting
  attachLiveMoneyFormatting($('fAmount'), false, true);
  attachLiveMoneyFormatting($('uAmount'), false, true);
  attachLiveMoneyFormatting($('recActualInput'), true, true);
  attachLiveMoneyFormatting($('allocAmount'), false, true);
  attachLiveMoneyFormatting($('debtInitialAmount'), false, true);
  attachLiveMoneyFormatting($('evAmount'), false, true);
  attachLiveMoneyFormatting($('planGuaranteed'), false, false);
  attachLiveMoneyFormatting($('planAdditional'), false, false);

load().catch((e) => alert(e.message));

/* Floating AI Financial Advisor Live Chat Module */
function renderAIMessage(sender, text, actionProposal = null, badgeLabel = null) {
  const container = $('aiChatMessages');
  if (!container) return;

  const div = document.createElement('div');
  div.className = `ai-msg ${sender}`;
  const bubbleDiv = document.createElement('div');
  bubbleDiv.className = 'ai-msg-bubble';
  div.appendChild(bubbleDiv);
  container.appendChild(div);

  if (sender === 'user') {
    bubbleDiv.innerHTML = esc(text).replace(/\n/g, '<br>');
  } else {
    const defaultBadge = badgeLabel || '[Dihitung Money Tracks]';
    updateStreamBubble(bubbleDiv, text, defaultBadge, actionProposal);
  }
  container.scrollTop = container.scrollHeight;
}

function toggleAIChat() {
  const win = $('aiChatWindow');
  if (!win) return;
  win.classList.toggle('hidden');
  if (!win.classList.contains('hidden')) {
    $('aiChatInput')?.focus();
    checkAIStatus();
    loadAIChatHistory();
  }
}

async function loadAIChatHistory() {
  try {
    const res = await api('/api/ai/history');
    if (res.status === 'success' && Array.isArray(res.history) && res.history.length > 0) {
      const container = $('aiChatMessages');
      if (!container) return;
      container.innerHTML = '';
      for (const item of res.history) {
        const badge = item.role === 'user' ? null : (item.intent === 'GENERAL_CHAT' ? '[Dihitung Money Tracks · Dijelaskan Gemini]' : '[Dihitung Money Tracks]');
        renderAIMessage(item.role === 'user' ? 'user' : 'bot', item.content, item.action_proposal, badge);
      }
    }
  } catch (e) {}
}

async function clearAIChatHistory() {
  if (!confirm('Apakah Anda yakin ingin membersihkan riwayat obrolan AI? (Data transaksi dan keuangan Anda TIDAK akan terhapus)')) return;
  try {
    await fetch('/api/ai/history', { method: 'DELETE' });
    const container = $('aiChatMessages');
    if (container) {
      container.innerHTML = `
        <div class="ai-msg bot">
          <div class="ai-msg-bubble">
            <div class="tiny muted" style="font-size:10px;margin-bottom:4px;opacity:0.8">[Dihitung Money Tracks]</div>
            Riwayat percakapan telah dibersihkan ✨. Ada yang ingin kamu tanyakan hari ini?
          </div>
        </div>`;
    }
  } catch (err) {
    alert('Gagal membersihkan riwayat: ' + err.message);
  }
}

let activeStreamAbortController = null;

async function checkAIStatus() {
  try {
    const res = await api('/api/ai/status');
    if ($('aiStatusSub')) {
      $('aiStatusSub').textContent = res.configured ? '● Mode Lokal + Gemini' : '● Mode Lokal';
    }
    if ($('keyStatusHint')) {
      $('keyStatusHint').textContent = res.configured
        ? `Status: Kunci tersimpan aman di OS Keyring (${res.key_preview})`
        : 'Status: Belum ada kunci tersimpan di OS Keyring';
    }
  } catch (e) {}
}

function openAIConfigModal() {
  checkAIStatus();
  openModal('aiConfigModal');
}

async function saveAIConfigSecure(e) {
  if (e && e.preventDefault) e.preventDefault();
  const key = $('geminiApiKeyInput').value.trim();
  try {
    const res = await api('/api/ai/settings/key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gemini_api_key: key }),
    });
    $('geminiApiKeyInput').value = '';
    closeModal('aiConfigModal');
    await checkAIStatus();
    alert('Kunci API berhasil disimpan dengan aman di OS Credential Manager!');
  } catch (err) {
    alert('Gagal menyimpan kunci: ' + err.message);
  }
}

async function testAIKeyConnection() {
  const inputKey = $('geminiApiKeyInput').value.trim();
  const statusHint = $('keyStatusHint');
  if (statusHint) statusHint.textContent = 'Status: Menguji koneksi ke Google Gemini API...';

  try {
    const res = await api('/api/ai/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gemini_api_key: inputKey || null }),
    });
    if (res.status === 'success') {
      alert('✅ ' + res.message);
      if (statusHint) statusHint.textContent = 'Status: Koneksi Berhasil! Model Gemini Siap.';
    } else {
      alert('❌ ' + res.message);
      if (statusHint) statusHint.textContent = 'Status: ' + res.message;
    }
  } catch (err) {
    alert('Gagal menguji koneksi: ' + err.message);
    if (statusHint) statusHint.textContent = 'Status: Gagal menguji koneksi';
  }
}

async function deleteAIKeySecure() {
  if (!confirm('Apakah Anda yakin ingin menghapus Kunci API Gemini dari OS Credential Manager?')) return;
  try {
    await fetch('/api/ai/settings/key', { method: 'DELETE' });
    $('geminiApiKeyInput').value = '';
    closeModal('aiConfigModal');
    await checkAIStatus();
    alert('Kunci API berhasil dihapus dari OS Credential Manager.');
  } catch (err) {
    alert('Gagal menghapus kunci: ' + err.message);
  }
}

function sendQuickAIChat(text) {
  if ($('aiChatInput')) {
    $('aiChatInput').value = text;
    submitAIChat(new Event('submit'));
  }
}

function handleAIChatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submitAIChat(e);
  }
}

function stopAIChatStream() {
  if (activeStreamAbortController) {
    activeStreamAbortController.abort();
    activeStreamAbortController = null;
  }
}

async function submitAIChat(e) {
  if (e && e.preventDefault) e.preventDefault();
  const input = $('aiChatInput');
  const msg = input.value.trim();
  if (!msg) return;

  renderAIMessage('user', msg);
  input.value = '';

  const stopBtn = $('aiStopStreamBtn');
  if (stopBtn) stopBtn.style.display = 'inline-flex';

  activeStreamAbortController = new AbortController();

  // Create stream message bubble
  const container = $('aiChatMessages');
  const msgDiv = document.createElement('div');
  msgDiv.className = 'ai-msg bot';

  const bubbleDiv = document.createElement('div');
  bubbleDiv.className = 'ai-msg-bubble';
  bubbleDiv.innerHTML = '<span class="tiny muted">Sedang berpikir... 💬</span>';
  msgDiv.appendChild(bubbleDiv);
  container.appendChild(msgDiv);
  container.scrollTop = container.scrollHeight;

  let fullText = '';
  let badgeLabel = '[Dihitung Money Tracks]';
  let actionProposal = null;

  try {
    const selectedMonth = $('monthSelect')?.value || '';
    const response = await fetch('/api/ai/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg, month: selectedMonth }),
      signal: activeStreamAbortController.signal,
    });

    if (!response.ok) throw new Error('HTTP ' + response.status);

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep last partial line in buffer

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const data = JSON.parse(line);
          if (data.type === 'meta') {
            badgeLabel = data.badge || badgeLabel;
            actionProposal = data.action_proposal || actionProposal;
          } else if (data.type === 'chunk') {
            fullText += data.text;
            updateStreamBubble(bubbleDiv, fullText, badgeLabel, actionProposal);
            container.scrollTop = container.scrollHeight;
          }
        } catch (err) {}
      }
    }

    if (!fullText.trim()) {
      fullText = 'Tidak ada tanggapan.';
      updateStreamBubble(bubbleDiv, fullText, badgeLabel, actionProposal);
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      fullText += '\n\n*(Jawaban terputus oleh pengguna)*';
      updateStreamBubble(bubbleDiv, fullText, badgeLabel, actionProposal);
    } else {
      bubbleDiv.innerHTML = `<span class="bad">Jawaban terputus. Coba lagi. (${esc(err.message)})</span>`;
    }
  } finally {
    activeStreamAbortController = null;
    if (stopBtn) stopBtn.style.display = 'none';
  }
}

function updateStreamBubble(bubbleDiv, text, badgeLabel, actionProposal = null) {
  let actionHtml = '';
  if (actionProposal) {
    if (actionProposal.type === 'RELEASE_ALLOCATION') {
      const validId = Number.isInteger(Number(actionProposal.id)) && Number(actionProposal.id) > 0 ? Number(actionProposal.id) : null;
      const btnHtml = validId
        ? `<div class="row" style="gap:6px;margin-top:8px">
            <button class="btn small primary" onclick="executeAIReleaseAllocation(${validId})">🔓 Bebaskan Dana Sekarang</button>
          </div>`
        : '';
      actionHtml = `
        <div class="ai-action-card" style="margin-top:8px">
          <div style="font-weight:700;font-size:12px;color:var(--amber)">🛡️ Usulan Pembebasan Alokasi Dana</div>
          <div class="tiny muted" style="margin-top:2px">${esc(actionProposal.title)} — <b>${esc(actionProposal.formatted_amount)}</b></div>
          <div class="tiny good" style="margin-top:2px">📈 Dana Tersedia di Beranda akan bertambah +${esc(actionProposal.formatted_amount)}</div>
          ${btnHtml}
        </div>
      `;
    } else if (actionProposal.type === 'CREATE_TRANSACTION_DRAFT') {
      actionHtml = `
        <div class="ai-action-card" style="margin-top:8px">
          <div style="font-weight:700;font-size:12px;color:var(--amber)">📝 Draft Catatan Transaksi Baru</div>
          <div class="tiny muted" style="margin-top:2px">${esc(actionProposal.description)} (${esc(actionProposal.category)}) — <b>${esc(actionProposal.formatted_amount)}</b></div>
          <div class="row" style="gap:6px;margin-top:8px">
            <button class="btn small primary" onclick="alert('Transaksi dikonfirmasi! Catatan disimpan.')">✅ Simpan Transaksi</button>
          </div>
        </div>
      `;
    } else if (actionProposal.type === 'REALLOCATE_BUDGET') {
      actionHtml = `
        <div class="ai-action-card" style="margin-top:8px">
          <div style="font-weight:700;font-size:12px;color:var(--amber)">🔄 Usulan Realokasi Anggaran</div>
          <div class="tiny muted" style="margin-top:2px">Pindah <b>${esc(actionProposal.formatted_amount)}</b> dari ${esc(actionProposal.from_category)} ke ${esc(actionProposal.to_category)}</div>
          <div class="row" style="gap:6px;margin-top:8px">
            <button class="btn small primary" onclick="alert('Realokasi anggaran berhasil dikonfirmasi!')">🔄 Konfirmasi Realokasi</button>
          </div>
        </div>
      `;
    }
  }

  const formattedText = esc(text)
    .replace(/\*\*(.*?)\*\*/g, '<b>$1</b>')
    .replace(/\*(.*?)\*/g, '<i>$1</i>')
    .replace(/\n/g, '<br>');

  const badgeHtml = `<div class="tiny muted" style="font-size:10px;margin-bottom:4px;opacity:0.8">${esc(badgeLabel)}</div>`;

  bubbleDiv.innerHTML = `${badgeHtml}${formattedText}${actionHtml}`;
}

async function executeAIReleaseAllocation(id, title = '', amount = '') {
  if (!title || !amount) {
    const alloc = (state.accounts?.protected_allocations || []).find((x) => x.id === id);
    if (alloc) {
      title = title || alloc.title;
      amount = amount || money(alloc.amount);
    }
  }
  const desc = title && amount ? `alokasi “${title}” sebesar ${amount}` : title ? `alokasi “${title}”` : 'alokasi dana ini';
  if (!confirm(`Apakah Anda yakin ingin membebaskan ${desc}?`)) return;
  try {
    await api('/api/protected_allocation/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, status: 'Released' }),
    });
    alert('Dana berhasil dibebaskan! Dana Tersedia di Beranda telah bertambah.');
    await loadAccounts();
    await load();
    renderAIMessage('bot', '✅ Berhasil! Alokasi dana telah dibebaskan. Saldo Dana Tersedia di Beranda otomatis bertambah.');
  } catch (err) {
    alert('Gagal membebaskan dana: ' + err.message);
  }
}

/* ==========================================================================
   PIHAK KETIGA (TITIPAN, PIUTANG, & UTANG) — K2 EVENT LEDGER
   ========================================================================== */

state.debtFilter = 'All';
state.debts = [];
state.currentDebtDetail = null;

async function loadThirdparty() {
  try {
    const res = await api('/api/debts');
    state.debts = res.debts || [];
    renderThirdpartyGrid();
  } catch (err) {
    if ($('thirdpartyGrid')) {
      $('thirdpartyGrid').innerHTML = `<div class="card" style="color:var(--rose)">Gagal memuat pihak ketiga: ${esc(err.message)}</div>`;
    }
  }
}

function filterDebts(kind) {
  state.debtFilter = kind;
  ['All', 'Custody', 'Receivable', 'Payable'].forEach((k) => {
    const btn = $('tabDebt' + k);
    if (btn) btn.classList.toggle('active', k === kind);
  });
  renderThirdpartyGrid();
}

function renderThirdpartyGrid() {
  const container = $('thirdpartyGrid');
  if (!container) return;

  const filtered = (state.debts || []).filter((d) => {
    if (state.debtFilter === 'All') return true;
    return d.kind === state.debtFilter;
  });

  if (filtered.length === 0) {
    container.innerHTML = `<div class="card" style="grid-column:1/-1;text-align:center;padding:24px">
      <div class="eyebrow">Belum ada catatan posisi pihak ketiga</div>
      <div class="tiny muted" style="margin-top:6px">Klik tombol "+ Tambah Posisi Baru" di atas untuk mencatat titipan, piutang, atau utang.</div>
    </div>`;
    return;
  }

  container.innerHTML = filtered.map((d) => {
    const kindLabel = d.kind === 'Custody' ? 'Titipan (Custody)' : d.kind === 'Receivable' ? 'Piutang (Receivable)' : 'Utang (Payable)';
    const kindColor = d.kind === 'Custody' ? 'var(--blue)' : d.kind === 'Receivable' ? 'var(--green)' : 'var(--red)';
    const statusPillClass = d.status === 'Active' ? 'good' : d.status === 'Settled' ? 'info' : 'warn';
    const statusText = d.status === 'Active' ? 'Aktif' : d.status === 'Settled' ? 'Lunas' : 'Dihapusbukukan';

    let upcomingsBadge = '';
    if (d.kind === 'Payable' && d.linked_upcomings && d.linked_upcomings.length > 0) {
      const activeUps = d.linked_upcomings.filter(u => u.status === 'Upcoming');
      upcomingsBadge = `<div class="tiny" style="color:var(--primary);margin-top:4px">📅 ${activeUps.length} jadwal cicilan aktif (${money(activeUps.reduce((acc, u) => acc + u.amount, 0))})</div>`;
    }

    return `<div class="card account-card" style="padding:16px;cursor:pointer" onclick="openDebtDetailModal(${d.id})">
      <div class="between">
        <div class="eyebrow" style="color:${kindColor}">${kindLabel}</div>
        <span class="pill ${statusPillClass}">${statusText}</span>
      </div>
      <div class="value" style="font-size:22px;margin:6px 0;color:${kindColor}">${money(d.outstanding)}</div>
      <div class="between">
        <div>
          <b>${esc(d.person_name)}</b>
          ${d.notes ? `<div class="tiny muted">${esc(d.notes)}</div>` : ''}
          ${upcomingsBadge}
        </div>
      </div>
      <div class="row" style="gap:6px;margin-top:12px">
        <button class="btn small primary" onclick="event.stopPropagation();openDebtEventModal(${d.id}, 'Settlement')">💸 Pelunasan</button>
        <button class="btn small secondary" onclick="event.stopPropagation();openDebtDetailModal(${d.id})">📜 Riwayat</button>
      </div>
    </div>`;
  }).join('');
}

function openDebtPositionModal() {
  if ($('debtPersonName')) $('debtPersonName').value = '';
  if ($('debtInitialAmount')) $('debtInitialAmount').value = '';
  if ($('debtEventDate')) $('debtEventDate').value = getLocalDateString(new Date());
  if ($('debtNotes')) $('debtNotes').value = '';
  if ($('debtCashMode')) $('debtCashMode').value = 'none';
  onDebtCashModeChange();
  populateDebtAccountSelect();
  openModal('debtPositionModal');
}

function populateDebtAccountSelect() {
  const sel = $('debtAccountSelect');
  if (!sel) return;
  const owned = (state.accounts?.accounts || []).filter((x) => x.active && x.kind === 'Owned');
  sel.innerHTML = `<option value="">-- Pilih rekening --</option>` +
    owned.map(x => `<option value="${esc(x.name)}">${esc(x.name)} (${money(x.current_balance)})</option>`).join('');
}

function onDebtKindChange() {
  const kind = $('debtKind').value;
  onDebtCashModeChange();
}

function onDebtCashModeChange() {
  const mode = $('debtCashMode').value;
  const kind = $('debtKind').value;
  const grp = $('debtAccountGroup');
  const lbl = $('debtAccountLabel');
  if (!grp) return;
  if (mode === 'cash') {
    grp.style.display = 'block';
    if (kind === 'Custody') lbl.textContent = 'Rekening Penerima Uang Titipan';
    else if (kind === 'Receivable') lbl.textContent = 'Rekening Sumber Pinjaman (Uang Keluar)';
    else if (kind === 'Payable') lbl.textContent = 'Rekening Penerima Pinjaman (Uang Masuk)';
  } else {
    grp.style.display = 'none';
  }
}

async function submitDebtPosition(e) {
  e.preventDefault();
  const person_name = $('debtPersonName').value.trim();
  const kind = $('debtKind').value;
  const initial_amount = parseMoneyInput($('debtInitialAmount').value);
  const event_date = $('debtEventDate').value;
  const is_cash = $('debtCashMode').value === 'cash';
  const account = is_cash ? $('debtAccountSelect').value : null;
  const notes = $('debtNotes').value.trim();

  if (!person_name) return alert('Isi nama pihak / orang.');
  if (initial_amount <= 0) return alert('Nominal harus lebih besar dari nol.');
  if (!event_date) return alert('Pilih tanggal posisi.');
  if (is_cash && !account) return alert('Pilih rekening untuk pergerakan kas.');

  const opKey = 'pos_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);

  try {
    await submitIdempotent('debt_pos_' + person_name, $('debtPosSubmitBtn'), async (key) => {
      return await api('/api/debts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
        body: JSON.stringify({
          person_name,
          kind,
          initial_amount,
          event_date,
          is_cash,
          account,
          notes,
          operation_key: opKey,
        }),
      });
    });
    closeModal('debtPositionModal');
    alert('Posisi pihak ketiga berhasil disimpan.');
    await loadThirdparty();
    await loadAccounts();
    await load();
  } catch (err) {
    alert('Gagal menyimpan posisi: ' + err.message);
  }
}

function openDebtEventModal(debtId, defaultType = 'Settlement') {
  const d = (state.debts || []).find((x) => x.id === debtId);
  if (!d) return alert('Posisi tidak ditemukan');
  $('evDebtId').value = debtId;
  $('evDebtDisplay').textContent = `${d.person_name} (${d.kind}) — Sisa: ${money(d.outstanding)}`;
  $('evEventType').value = defaultType;
  $('evAmount').value = '';
  $('evDate').value = getLocalDateString(new Date());
  $('evNotes').value = '';
  $('evCashMode').value = 'cash';
  populateEvAccountSelect();
  onEvTypeChange();
  openModal('debtEventModal');
}

function populateEvAccountSelect() {
  const sel = $('evAccountSelect');
  if (!sel) return;
  const owned = (state.accounts?.accounts || []).filter((x) => x.active && x.kind === 'Owned');
  sel.innerHTML = `<option value="">-- Pilih rekening --</option>` +
    owned.map(x => `<option value="${esc(x.name)}">${esc(x.name)} (${money(x.current_balance)})</option>`).join('');
}

function onEvTypeChange() {
  const evType = $('evEventType').value;
  const cashGrp = $('evCashModeGroup');
  const accGrp = $('evAccountGroup');
  if (evType === 'WriteOff') {
    if (cashGrp) cashGrp.style.display = 'none';
    if (accGrp) accGrp.style.display = 'none';
  } else {
    if (cashGrp) cashGrp.style.display = 'block';
    onEvCashModeChange();
  }
}

function onEvCashModeChange() {
  const mode = $('evCashMode').value;
  const accGrp = $('evAccountGroup');
  if (accGrp) {
    accGrp.style.display = mode === 'cash' ? 'block' : 'none';
  }
}

async function submitDebtEvent(e) {
  e.preventDefault();
  const debt_id = Number($('evDebtId').value);
  const event_type = $('evEventType').value;
  const amount = parseMoneyInput($('evAmount').value);
  const event_date = $('evDate').value;
  const is_cash = event_type !== 'WriteOff' && $('evCashMode').value === 'cash';
  const account = is_cash ? $('evAccountSelect').value : null;
  const notes = $('evNotes').value.trim();

  if (amount <= 0) return alert('Nominal harus lebih besar dari nol.');
  if (!event_date) return alert('Pilih tanggal event.');
  if (is_cash && !account) return alert('Pilih rekening untuk pergerakan kas.');

  const opKey = 'ev_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);

  try {
    await api('/api/debts/event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        debt_id,
        event_type,
        amount,
        event_date,
        is_cash,
        account,
        notes,
        operation_key: opKey,
      }),
    });
    closeModal('debtEventModal');
    alert('Event mutasi berhasil disimpan.');
    await loadThirdparty();
    await loadAccounts();
    await load();
  } catch (err) {
    alert('Gagal menyimpan event: ' + err.message);
  }
}

async function openDebtDetailModal(debtId) {
  try {
    const res = await api(`/api/debts/detail?id=${debtId}`);
    const d = res.debt;
    state.currentDebtDetail = d;

    $('detailDebtKind').textContent = d.kind === 'Custody' ? 'Titipan (Custody)' : d.kind === 'Receivable' ? 'Piutang (Receivable)' : 'Utang (Payable)';
    $('detailDebtName').textContent = d.person_name;
    $('detailDebtOutstanding').textContent = money(d.outstanding);

    // Linked Upcomings
    const upWrap = $('detailDebtUpcomingsWrap');
    const upList = $('detailDebtUpcomingsList');
    if (d.kind === 'Payable' && upWrap && upList) {
      upWrap.style.display = 'block';
      if (d.linked_upcomings && d.linked_upcomings.length > 0) {
        upList.innerHTML = d.linked_upcomings.map(u => `
          <div class="between" style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
            <div><b>${esc(u.title)}</b> <span class="tiny muted">(${u.due_date || 'Tanpa tgl'})</span></div>
            <div><b>${money(u.amount)}</b> <span class="pill ${u.status === 'Paid' ? 'good' : 'warn'}">${esc(u.status)}</span></div>
          </div>
        `).join('');
      } else {
        upList.innerHTML = `<div class="tiny muted">Belum ada jadwal cicilan tertaut.</div>`;
      }
    } else if (upWrap) {
      upWrap.style.display = 'none';
    }

    // Events List
    const evList = $('detailDebtEventsList');
    if (evList) {
      evList.innerHTML = (d.events || []).map(e => {
        const sign = e.effect > 0 ? '+' : '-';
        const signColor = e.effect > 0 ? 'var(--green)' : 'var(--red)';
        const isCorrection = e.event_type === 'Correction';
        const revBtn = (!isCorrection && !e.reversal_of_id)
          ? `<button class="btn tiny" style="margin-left:8px" onclick="reverseDebtEventAction(${e.id})">Koreksi</button>`
          : '';

        return `<div class="between" style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
          <div>
            <div style="font-weight:700">${esc(e.event_type)}: <span style="color:${signColor}">${sign}${money(e.amount)}</span></div>
            <div class="tiny muted">${esc(e.event_date)}${e.notes ? ' • ' + esc(e.notes) : ''}</div>
          </div>
          <div class="row">
            ${revBtn}
          </div>
        </div>`;
      }).join('') || '<div class="tiny muted">Belum ada riwayat event.</div>';
    }

    openModal('debtDetailModal');
  } catch (err) {
    alert('Gagal memuat rincian posisi: ' + err.message);
  }
}

function openAddEventForCurrentDebt() {
  if (!state.currentDebtDetail) return;
  closeModal('debtDetailModal');
  openDebtEventModal(state.currentDebtDetail.id);
}

async function reverseDebtEventAction(eventId) {
  if (!confirm(`Apakah Anda yakin ingin membatalkan (koreksi reversal) event #${eventId}?`)) return;
  const opKey = 'rev_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
  try {
    await api('/api/debts/reverse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event_id: eventId,
        notes: `Reversal atas event #${eventId}`,
        operation_key: opKey,
      }),
    });
    alert('Event berhasil direversal.');
    closeModal('debtDetailModal');
    await loadThirdparty();
    await loadAccounts();
    await load();
  } catch (err) {
    alert('Gagal mereversal event: ' + err.message);
  }
}

/* ==========================================================================
   Universal Ingestion — Quick Capture, Import Center, Review Queue, Receipt OCR & PWA
   ========================================================================== */

// --- Quick Capture ---
let _qcDebounceTimer = null;
let _lastQcPreviewData = null;

function openQuickCaptureModal() {
  openModal('quickCaptureModal');
  const input = $('captureInputModal');
  if (input) {
    input.value = '';
    input.focus();
  }
  const previewArea = $('previewAreaModal');
  if (previewArea) {
    previewArea.innerHTML = '';
    previewArea.style.display = 'none';
  }
}

function initQuickCapture() {
  const modalInput = $('captureInputModal');
  const modalArea = $('previewAreaModal');
  if (modalInput && modalArea) {
    modalInput.addEventListener('input', () => {
      clearTimeout(_qcDebounceTimer);
      _qcDebounceTimer = setTimeout(() => {
        handleQuickCapturePreview(modalInput.value.trim(), modalArea, true);
      }, 250);
    });
    modalInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && _lastQcPreviewData && (_lastQcPreviewData.status === 'SUCCESS' || _lastQcPreviewData.status === 'valid')) {
        e.preventDefault();
        applyQuickCapture(_lastQcPreviewData, true);
      }
    });
  }

  const pageInput = $('captureInputPage');
  const pageArea = $('previewAreaPage');
  if (pageInput && pageArea) {
    pageInput.addEventListener('input', () => {
      clearTimeout(_qcDebounceTimer);
      _qcDebounceTimer = setTimeout(() => {
        handleQuickCapturePreview(pageInput.value.trim(), pageArea, false);
      }, 250);
    });
    pageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && _lastQcPreviewData && (_lastQcPreviewData.status === 'SUCCESS' || _lastQcPreviewData.status === 'valid')) {
        e.preventDefault();
        applyQuickCapture(_lastQcPreviewData, false);
      }
    });
  }
}

async function handleQuickCapturePreview(text, previewAreaEl, isModal) {
  if (!text) {
    previewAreaEl.innerHTML = '';
    previewAreaEl.style.display = 'none';
    _lastQcPreviewData = null;
    return;
  }

  previewAreaEl.style.display = 'block';
  previewAreaEl.innerHTML = '<div class="small muted">Menganalisis kalimat transaksi...</div>';

  try {
    const res = await api(`/api/quick-capture/preview?q=${encodeURIComponent(text)}`);
    _lastQcPreviewData = res;

    if (res.status === 'SUCCESS' || res.status === 'valid') {
      const tType = String(res.transaction_type || '').toUpperCase();
      const typeColor = tType === 'EXPENSE' ? 'var(--red)' : tType === 'INCOME' ? 'var(--green)' : 'var(--blue)';
      const typeLabel = tType === 'EXPENSE' ? 'Pengeluaran' : tType === 'INCOME' ? 'Pemasukan' : 'Transfer Antar Rekening';
      const hashShort = res.preview_hash ? res.preview_hash.slice(0, 12) + '...' : '—';
      const targetAcc = res.account_to ? ` &rarr; ${esc(res.account_to)}` : '';

      previewAreaEl.innerHTML = `
        <div style="background:rgba(255,255,255,0.03);border:1px solid var(--line);border-radius:12px;padding:14px">
          <div class="between" style="margin-bottom:8px">
            <span class="pill" style="font-weight:700;color:${typeColor}">${esc(typeLabel)}</span>
            <span class="tiny muted" title="Preview Hash SHA-256">Hash: ${esc(hashShort)}</span>
          </div>
          <div style="font-size:20px;font-weight:800;color:${typeColor};margin-bottom:6px">
            ${money(res.amount)}
          </div>
          <div style="font-size:14px;color:var(--text);margin-bottom:4px">
            <strong>${esc(res.description || '—')}</strong>
          </div>
          <div class="small muted" style="margin-bottom:12px">
            Akun: <strong>${esc(res.account_from || 'Cash')}</strong>${targetAcc} &bull; Kategori: <strong>${esc(res.category || 'Lainnya')}</strong>
          </div>
          <div class="between">
            <span class="tiny muted">${esc(res.date)} ${esc(res.time || '')}</span>
            <button class="btn primary small" id="btnApplyQc" onclick="applyQuickCapture(_lastQcPreviewData, ${isModal})">
              Terapkan (Safe Apply)
            </button>
          </div>
        </div>
      `;
    } else {
      const reason = res.diagnostic_reason || 'Format belum dikenali. Lengkapi nominal dan keterangan.';
      previewAreaEl.innerHTML = `
        <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:12px;padding:12px;color:var(--text)">
          <div class="small" style="color:var(--red);font-weight:600;margin-bottom:4px">Belum Siap Diterapkan</div>
          <div class="tiny muted">${esc(reason)}</div>
        </div>
      `;
    }
  } catch (err) {
    previewAreaEl.innerHTML = `<div class="tiny" style="color:var(--red)">Gagal mengecek pratinjau: ${esc(err.message)}</div>`;
  }
}

async function applyQuickCapture(previewData, isModal) {
  if (!previewData || (previewData.status !== 'SUCCESS' && previewData.status !== 'valid')) return;
  const btn = $('btnApplyQc');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Menyimpan...';
  }

  try {
    const res = await api('/api/quick-capture/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(previewData),
    });

    if (res.success) {
      alert('Transaksi berhasil dicatat dan diverifikasi dengan Safe Apply!');
      if (isModal) {
        closeModal('quickCaptureModal');
      } else {
        const pInput = $('captureInputPage');
        if (pInput) pInput.value = '';
        const pArea = $('previewAreaPage');
        if (pArea) {
          pArea.innerHTML = '';
          pArea.style.display = 'none';
        }
      }
      _lastQcPreviewData = null;
      await load();
      if (state.page === 'transactions') await loadTransactions();
    } else {
      alert('Gagal menerapkan: ' + (res.error || 'Terjadi kesalahan'));
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Terapkan (Safe Apply)';
      }
    }
  } catch (err) {
    alert('Gagal menerapkan transaksi: ' + err.message);
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Terapkan (Safe Apply)';
    }
  }
}

// --- Import Center ---
async function uploadImportFile() {
  const fileInput = $('importFileInput');
  const resultArea = $('importResultArea');
  if (!fileInput || !fileInput.files.length) {
    alert('Silakan pilih berkas mutasi rekening terlebih dahulu.');
    return;
  }

  const file = fileInput.files[0];
  resultArea.style.display = 'block';
  resultArea.innerHTML = '<div class="small muted">Membaca dan memproses berkas mutasi...</div>';

  const reader = new FileReader();
  reader.onload = async function() {
    try {
      const dataUrl = reader.result;
      const base64Data = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
      const res = await api('/api/import/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          content_base64: base64Data,
        }),
      });

      renderImportResult(res, resultArea);
      await load();
    } catch (err) {
      resultArea.innerHTML = `
        <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:12px;padding:12px">
          <div class="small" style="color:var(--red);font-weight:600">Gagal mengunggah berkas</div>
          <div class="tiny muted" style="margin-top:4px">${esc(err.message)}</div>
        </div>
      `;
    }
  };
  reader.readAsDataURL(file);
}

function renderImportResult(res, containerEl) {
  const total = res.total_items ?? res.total_rows ?? 0;
  const imported = res.imported_count ?? res.accepted_count ?? 0;
  const reviewCount = res.review_count ?? res.ambiguous_count ?? 0;
  const dupCount = res.duplicate_count ?? 0;

  containerEl.innerHTML = `
    <div style="background:rgba(255,255,255,0.03);border:1px solid var(--line);border-radius:12px;padding:16px">
      <div style="font-size:16px;font-weight:700;color:var(--green);margin-bottom:8px">Impor Selesai Diproses</div>
      <div class="small" style="margin-bottom:12px">
        Total baris: <strong>${total}</strong> &bull;
        Dibukukan: <strong style="color:var(--green)">${imported}</strong> &bull;
        Perlu Tinjauan: <strong style="color:var(--amber)">${reviewCount}</strong> &bull;
        Duplikat: <strong style="color:var(--muted)">${dupCount}</strong>
      </div>
      <div class="row">
        ${reviewCount > 0 ? '<button class="btn primary small" onclick="goPage(\'review\')">Buka Antrean Tinjauan</button>' : ''}
        <button class="btn small" onclick="goPage(\'transactions\')">Lihat Transaksi</button>
      </div>
    </div>
  `;
}

// --- Watched Folder Automation ---
async function loadWatchedFolderStatus() {
  const badgeEl = $('watchedFolderStatusBadge');
  const pathEl = $('watchedFolderPathDisplay');
  const lastScanEl = $('watchedFolderLastScan');
  const countEl = $('watchedFolderProcessedCount');
  if (!badgeEl) return;

  try {
    const res = await api('/api/watched-folder/status');
    if (res.active) {
      badgeEl.className = 'pill ready';
      badgeEl.textContent = 'Aktif';
    } else {
      badgeEl.className = 'pill pending';
      badgeEl.textContent = 'Siaga';
    }
    if (pathEl && res.watched_path) {
      pathEl.innerHTML = `Lokasi: <code>${esc(res.watched_path)}</code>`;
    }
    if (lastScanEl) {
      lastScanEl.textContent = res.last_scan_timestamp ? new Date(res.last_scan_timestamp).toLocaleString('id-ID') : 'Belum pernah';
    }
    if (countEl) {
      countEl.textContent = String(res.processed_files_count ?? 0);
    }
  } catch (err) {
    if (badgeEl) {
      badgeEl.className = 'pill rejected';
      badgeEl.textContent = 'Nonaktif';
    }
  }
}

async function scanWatchedFolderNow() {
  const btn = $('scanFolderBtn');
  const resArea = $('scanFolderResultArea');
  if (!btn || !resArea) return;

  btn.disabled = true;
  btn.textContent = 'Memindai Folder...';
  resArea.style.display = 'block';
  resArea.innerHTML = '<div class="small muted">Memindai berkas mutasi di folder Google Drive...</div>';

  try {
    const res = await api('/api/watched-folder/scan-now', { method: 'POST' });
    const scanned = res.scanned_files ?? 0;
    const newFiles = res.new_files ?? 0;
    const skipped = res.skipped_files ?? 0;
    const queued = res.items_queued ?? 0;

    resArea.innerHTML = `
      <div style="background:rgba(255,255,255,0.03);border:1px solid var(--line);border-radius:12px;padding:12px">
        <div style="font-weight:600;color:var(--green);margin-bottom:4px">Pemindaian Folder Selesai</div>
        <div class="small" style="margin-bottom:8px">
          Berkas dipindai: <strong>${scanned}</strong> &bull;
          Berkas baru: <strong style="color:var(--green)">${newFiles}</strong> &bull;
          Duplikat (dilewati): <strong style="color:var(--muted)">${skipped}</strong> &bull;
          Antrean tinjauan: <strong style="color:var(--amber)">${queued}</strong>
        </div>
        ${queued > 0 ? '<button class="btn primary tiny" onclick="goPage(\'review\')">Buka Antrean Tinjauan</button>' : ''}
      </div>
    `;
    await loadWatchedFolderStatus();
  } catch (err) {
    resArea.innerHTML = `
      <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:12px;padding:12px">
        <div class="small" style="color:var(--red);font-weight:600">Gagal memindai folder</div>
        <div class="tiny muted" style="margin-top:4px">${esc(err.message)}</div>
      </div>
    `;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Pindai Folder Sekarang';
  }
}

// --- Review Queue ---
async function loadReviewQueue() {
  const container = $('reviewQueueList');
  if (!container) return;
  container.innerHTML = '<div class="small muted">Memuat antrean tinjauan...</div>';

  try {
    const items = await api('/api/review-queue/pending');
    if (!Array.isArray(items) || items.length === 0) {
      container.innerHTML = `
        <div style="padding:24px;text-align:center;color:var(--muted)">
          <div style="font-size:24px;margin-bottom:8px">✓</div>
          <div>Semua transaksi telah ditinjau. Tidak ada antrean tertunda.</div>
        </div>
      `;
      return;
    }

    container.innerHTML = items.map((it) => {
      const typeColor = it.transaction_type === 'Expense' ? 'var(--red)' : it.transaction_type === 'Income' ? 'var(--green)' : 'var(--blue)';
      return `
        <div class="card" style="margin-bottom:10px;padding:14px;border:1px solid var(--line)">
          <div class="between" style="margin-bottom:6px">
            <span class="pill small" style="color:${typeColor}">${esc(it.transaction_type || 'Transaksi')}</span>
            <span class="tiny muted">${esc(it.date)} ${esc(it.time || '')}</span>
          </div>
          <div class="between" style="margin-bottom:6px">
            <div style="font-size:16px;font-weight:700;color:var(--text)">${esc(it.description || '—')}</div>
            <div style="font-size:16px;font-weight:800;color:${typeColor}">${money(it.amount)}</div>
          </div>
          <div class="tiny muted" style="margin-bottom:10px">
            Akun: <strong>${esc(it.account_from || '—')}</strong> &bull; Kategori: <strong>${esc(it.category || '—')}</strong> &bull; Alasan: <em>${esc(it.reason || 'Perlu konfirmasi')}</em>
          </div>
          <div class="row" style="gap:8px">
            <button class="btn primary tiny" onclick="handleReviewAction('${esc(it.item_id)}', 'approve')">Setujui</button>
            <button class="btn tiny" onclick="handleReviewAction('${esc(it.item_id)}', 'reject')">Tolak</button>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    container.innerHTML = `<div class="small" style="color:var(--red)">Gagal memuat antrean tinjauan: ${esc(err.message)}</div>`;
  }
}

async function handleReviewAction(itemId, action) {
  try {
    const res = await api('/api/review-queue/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: itemId, action: action }),
    });
    if (res.success) {
      await loadReviewQueue();
      await load();
    } else {
      alert('Gagal memproses tinjauan: ' + (res.error || 'Terjadi kesalahan'));
    }
  } catch (err) {
    alert('Gagal memproses tinjauan: ' + err.message);
  }
}

// --- Receipt OCR ---
let _currentReceiptDraft = null;

async function uploadReceiptFile() {
  const fileInput = $('receiptFileInput');
  const resultArea = $('receiptResultArea');
  if (!fileInput || !fileInput.files.length) {
    alert('Silakan pilih foto struk belanja terlebih dahulu.');
    return;
  }

  const file = fileInput.files[0];
  resultArea.style.display = 'block';
  resultArea.innerHTML = '<div class="small muted">Mengekstraksi teks struk belanja (OCR)...</div>';

  const reader = new FileReader();
  reader.onload = async function() {
    try {
      const dataUrl = reader.result;
      const base64Data = dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
      const res = await api('/api/receipt/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          content_base64: base64Data,
        }),
      });

      _currentReceiptDraft = res;
      renderReceiptDraft(res, resultArea);
    } catch (err) {
      resultArea.innerHTML = `
        <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:12px;padding:12px">
          <div class="small" style="color:var(--red);font-weight:600">Gagal memproses struk</div>
          <div class="tiny muted" style="margin-top:4px">${esc(err.message)}</div>
        </div>
      `;
    }
  };
  reader.readAsDataURL(file);
}

function renderReceiptDraft(draft, containerEl) {
  containerEl.innerHTML = `
    <div style="background:rgba(255,255,255,0.03);border:1px solid var(--line);border-radius:12px;padding:16px">
      <div class="between" style="margin-bottom:12px">
        <span class="pill info small">Draf Hasil Pindai Struk</span>
        <span class="tiny muted">Hash: ${esc((draft.preview_hash || '').slice(0, 12))}...</span>
      </div>
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px">
        <div>
          <label class="tiny muted" for="ocrMerchant">Nama Toko / Merchant:</label>
          <input type="text" id="ocrMerchant" class="input small" value="${esc(draft.merchant || '')}">
        </div>
        <div>
          <label class="tiny muted" for="ocrAmount">Total Nominal (Rp):</label>
          <input type="text" id="ocrAmount" class="input small" value="${esc(draft.amount || '')}">
        </div>
        <div>
          <label class="tiny muted" for="ocrDate">Tanggal:</label>
          <input type="date" id="ocrDate" class="input small" value="${esc(draft.date || '')}">
        </div>
        <div>
          <label class="tiny muted" for="ocrAccount">Akun Pembayaran:</label>
          <input type="text" id="ocrAccount" class="input small" value="${esc(draft.account_from || 'Cash')}">
        </div>
      </div>
      <div style="margin-bottom:12px">
        <label class="tiny muted" for="ocrCategory">Kategori:</label>
        <input type="text" id="ocrCategory" class="input small" value="${esc(draft.category || 'Makanan & Minuman')}">
      </div>
      <div class="row" style="gap:8px">
        <button class="btn primary small" onclick="confirmReceiptDraft()">Terapkan Transaksi (Safe Apply)</button>
        <button class="btn small" onclick="rejectReceiptDraft()">Batalkan</button>
      </div>
    </div>
  `;
}

async function confirmReceiptDraft() {
  if (!_currentReceiptDraft) return;
  const merchant = $('ocrMerchant')?.value || _currentReceiptDraft.merchant;
  const amount = $('ocrAmount')?.value || _currentReceiptDraft.amount;
  const date = $('ocrDate')?.value || _currentReceiptDraft.date;
  const account_from = $('ocrAccount')?.value || _currentReceiptDraft.account_from;
  const category = $('ocrCategory')?.value || _currentReceiptDraft.category;

  try {
    const res = await api('/api/receipt/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        receipt_id: _currentReceiptDraft.receipt_id,
        action: 'apply',
        corrections: {
          merchant,
          amount,
          date,
          account_from,
          category,
        },
      }),
    });

    if (res.success) {
      alert('Struk berhasil dibukukan dengan Safe Apply!');
      const area = $('receiptResultArea');
      if (area) {
        area.innerHTML = '';
        area.style.display = 'none';
      }
      const fileInput = $('receiptFileInput');
      if (fileInput) fileInput.value = '';
      _currentReceiptDraft = null;
      await load();
      if (state.page === 'transactions') await loadTransactions();
    } else {
      alert('Gagal menerapkan struk: ' + (res.error || 'Terjadi kesalahan'));
    }
  } catch (err) {
    alert('Gagal menerapkan struk: ' + err.message);
  }
}

async function rejectReceiptDraft() {
  if (!_currentReceiptDraft) return;
  try {
    await api('/api/receipt/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        receipt_id: _currentReceiptDraft.receipt_id,
        action: 'reject',
      }),
    });
    const area = $('receiptResultArea');
    if (area) {
      area.innerHTML = '';
      area.style.display = 'none';
    }
    const fileInput = $('receiptFileInput');
    if (fileInput) fileInput.value = '';
    _currentReceiptDraft = null;
  } catch (err) {
    alert('Gagal membatalkan draf: ' + err.message);
  }
}

// --- PWA Service Worker Registration ---
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
      console.warn('Service Worker registration skipped or failed:', err);
    });
  });
}

// --- Cloudflare Edge Inbox Sync & Freshness Visibility ---
async function fetchAndRenderSyncStatus() {
  const badge = $('cloudSyncStatusBadge');
  if (!badge) return;
  try {
    const res = await api('/api/sync/status', { method: 'GET' });
    if (!res) return;
    const status = (res.status || 'IDLE').toUpperCase();
    const lastSynced = res.last_synced_at || res.last_sync_at;
    const lastErr = res.last_error;
    const isStale = !!res.is_stale;

    let timeStr = '';
    if (lastSynced) {
      try {
        const d = new Date(lastSynced);
        timeStr = d.toLocaleDateString('id-ID') + ' ' + d.toLocaleTimeString('id-ID', { hour12: false }) + ' WIB';
      } catch (_) {
        timeStr = lastSynced + ' WIB';
      }
    }

    if (status === 'PULLING') {
      badge.textContent = 'Cloud: Menyinkronkan...';
      badge.style.color = '#38bdf8';
      badge.title = 'Sedang menarik bukti mutasi dari Cloudflare Edge...';
    } else if (status === 'OK') {
      if (isStale) {
        badge.textContent = `Cloud: Terhubung (Usang - ${timeStr})`;
        badge.style.color = '#eab308';
        badge.title = `Sinkronisasi terakhir pada ${timeStr}. Data belum diperbarui lebih dari 30 menit.`;
      } else {
        badge.textContent = `Cloud: Sinkron (${timeStr})`;
        badge.style.color = '#10b981';
        badge.title = `Sinkronisasi terakhir pada ${timeStr}.`;
      }
    } else if (status === 'DISABLED') {
      badge.textContent = 'Cloud: Nonaktif';
      badge.style.color = '#94a3b8';
      badge.title = lastErr ? `Auto-sync nonaktif (${lastErr})` : 'Worker URL atau token belum dikonfigurasi';
    } else if (status === 'FAILED') {
      const errLabel = lastErr || 'ERROR';
      if (lastSynced) {
        badge.textContent = `Cloud: Gagal (${errLabel}) - Data Usang`;
        badge.style.color = '#ef4444';
        badge.title = `Gagal menyinkronkan: ${errLabel}. Data terakhir ${timeStr} sudah usang.`;
      } else {
        badge.textContent = `Cloud: Gagal (${errLabel})`;
        badge.style.color = '#ef4444';
        badge.title = `Gagal menyinkronkan: ${errLabel}`;
      }
    } else {
      badge.textContent = 'Cloud: Idle';
      badge.style.color = 'var(--muted)';
      badge.title = lastSynced ? `Terakhir sinkron: ${timeStr}` : 'Belum pernah sinkron';
    }
  } catch (e) {
    console.warn('Failed to fetch sync status:', e);
  }
}

async function pullFromCloud() {
  const btn = $('pullCloudBtn');
  const badge = $('cloudSyncStatusBadge');
  if (btn) btn.disabled = true;
  if (badge) {
    badge.textContent = 'Cloud: Menyinkronkan...';
    badge.style.color = '#38bdf8';
  }
  try {
    let token = '';
    if (typeof localStorage !== 'undefined') {
      token = localStorage.getItem('aturuang_staging_token') || '';
    }
    if (!token && typeof prompt !== 'undefined') {
      token = prompt('Masukkan Staging Admin Token:');
      if (token && typeof localStorage !== 'undefined') {
        localStorage.setItem('aturuang_staging_token', token.trim());
      }
    }
    const headers = { 'Content-Type': 'application/json' };
    if (token) {
      headers['Authorization'] = 'Bearer ' + token.trim();
    }
    const res = await api('/api/sync/pull-cloud', {
      method: 'POST',
      headers,
      body: JSON.stringify({ staging_admin_token: token ? token.trim() : '' }),
    });
    if (res && res.status === 'success') {
      await fetchAndRenderSyncStatus();
      await load();
      if (state.page === 'transactions') await loadTransactions();
      if (state.page === 'review') await loadReviewQueue();
    } else {
      await fetchAndRenderSyncStatus();
    }
  } catch (err) {
    await fetchAndRenderSyncStatus();
    console.warn('Pull cloud sync failed:', err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Initialize Quick Capture event bindings
initQuickCapture();

// Initialize Cloud Sync status and honest freshness monitoring
if (typeof window !== 'undefined') {
  fetchAndRenderSyncStatus();
  setInterval(fetchAndRenderSyncStatus, 60000);
}
