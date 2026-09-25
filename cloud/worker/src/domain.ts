// domain.ts: Modul domain kalkulasi finansial AturUang untuk D1 dan Parser Ingestion
import type { Env } from "./auth.ts";

export function round2(num: number): number {
  return Math.round((num + Number.EPSILON) * 100) / 100;
}

export function getWibDate(dateObj = new Date()): { dateStr: string; monthStr: string; timeStr: string } {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Jakarta",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  const parts = formatter.formatToParts(dateObj);
  const partMap: Record<string, string> = {};
  for (const p of parts) {
    partMap[p.type] = p.value;
  }

  const year = partMap.year;
  const month = partMap.month;
  const day = partMap.day;
  const hour = partMap.hour || "00";
  const minute = partMap.minute || "00";
  const second = partMap.second || "00";

  const dateStr = `${year}-${month}-${day}`;
  const monthStr = `${year}-${month}`;
  const timeStr = `${hour}:${minute}:${second}`;

  return { dateStr, monthStr, timeStr };
}

export async function getUpcomingActiveCoverage(
  db: D1Database,
  upcomingId?: number
): Promise<Map<number, number> | number> {
  const covMap = new Map<number, number>();

  // 1. Coverage dari protected_allocations (legacy engine)
  try {
    const paRows = await db
      .prepare(
        `SELECT covers_upcoming_id, COALESCE(SUM(amount), 0.0) as cov
         FROM protected_allocations
         WHERE status = 'Active' AND covers_upcoming_id IS NOT NULL
         GROUP BY covers_upcoming_id`
      )
      .all<{ covers_upcoming_id: number; cov: number }>();

    for (const r of paRows.results || []) {
      const cur = covMap.get(r.covers_upcoming_id) || 0.0;
      covMap.set(r.covers_upcoming_id, round2(cur + Number(r.cov || 0)));
    }
  } catch {
    // Tabel protected_allocations opsional pada skema murni
  }

  // 2. Coverage dari allocation_goals (engine baru melalui upcoming.linked_goal_id)
  try {
    const goalRows = await db
      .prepare(
        `SELECT u.id, COALESCE(ag.allocated_amount, 0.0) as cov
         FROM upcoming u
         JOIN allocation_goals ag ON u.linked_goal_id = ag.id
         WHERE ag.status = 'Active' AND u.linked_goal_id IS NOT NULL`
      )
      .all<{ id: number; cov: number }>();

    for (const r of goalRows.results || []) {
      const cur = covMap.get(r.id) || 0.0;
      covMap.set(r.id, round2(cur + Number(r.cov || 0)));
    }
  } catch {
    // Abaikan jika kolom linked_goal_id belum termigrasi
  }

  if (upcomingId !== undefined) {
    return covMap.get(upcomingId) || 0.0;
  }
  return covMap;
}

export async function reconstructBalance(
  db: D1Database,
  accountName: string,
  asOfDate?: string
): Promise<{
  status: string;
  calculated_balance: number;
  expected_balance: number | null;
  cached_balance: number;
  difference: number | null;
  anchor_balance: number;
  anchor_date: string | null;
  reason: string;
}> {
  const targetDate = asOfDate || getWibDate().dateStr;

  // 1. Get cached current balance from accounts
  const accRow = await db
    .prepare("SELECT current_balance, balance_date FROM accounts WHERE name = ?")
    .bind(accountName)
    .first<{ current_balance: number | null; balance_date: string | null }>();

  if (!accRow) {
    return {
      status: "unverifiable",
      calculated_balance: 0.0,
      expected_balance: null,
      cached_balance: 0.0,
      difference: null,
      anchor_balance: 0.0,
      anchor_date: null,
      reason: `Rekening '${accountName}' tidak ditemukan.`,
    };
  }

  const cachedBal = round2(Number(accRow.current_balance || 0));
  const balDate = accRow.balance_date || null;

  // 2. Search for latest explicit trusted anchor (manual_anchor, initial_anchor)
  let snapRes = await db
    .prepare(
      `SELECT id, balance, snapshot_date, created_at, snapshot_kind
       FROM balance_snapshots
       WHERE account_name = ? AND snapshot_date <= ? AND snapshot_kind IN ('manual_anchor', 'initial_anchor')
       ORDER BY id DESC LIMIT 1`
    )
    .bind(accountName, targetDate)
    .first<{ id: number; balance: number; snapshot_date: string; created_at: string; snapshot_kind: string }>();

  // 3. Fallback to legacy snapshot if no explicit anchor found
  if (!snapRes) {
    snapRes = await db
      .prepare(
        `SELECT id, balance, snapshot_date, created_at, snapshot_kind
         FROM balance_snapshots
         WHERE account_name = ? AND snapshot_date <= ? AND (snapshot_kind = 'legacy' OR snapshot_kind IS NULL)
         ORDER BY id ASC LIMIT 1`
      )
      .bind(accountName, targetDate)
      .first<{ id: number; balance: number; snapshot_date: string; created_at: string; snapshot_kind: string }>();
  }

  if (!snapRes) {
    return {
      status: "unverifiable",
      calculated_balance: 0.0,
      expected_balance: null,
      cached_balance: cachedBal,
      difference: null,
      anchor_balance: 0.0,
      anchor_date: balDate,
      reason: "Tidak ada data snapshot saldo (anchor) tepercaya untuk akun ini.",
    };
  }

  const anchorBal = round2(Number(snapRes.balance || 0));
  const anchorDate = snapRes.snapshot_date;
  const anchorCreated = snapRes.created_at || "";

  // 4. Sum subsequent transaction mutations strictly after the chosen anchor
  const mutRes = await db
    .prepare(
      `SELECT
         COALESCE(SUM(CASE WHEN account_to = ? THEN amount ELSE 0 END), 0) as in_mutations,
         COALESCE(SUM(CASE WHEN account_from = ? THEN amount ELSE 0 END), 0) as out_mutations
       FROM transactions
       WHERE is_deleted = 0
         AND (account_from = ? OR account_to = ?)
         AND (date > ? OR (date = ? AND created_at > ?))
         AND date <= ?`
    )
    .bind(
      accountName,
      accountName,
      accountName,
      accountName,
      anchorDate,
      anchorDate,
      anchorCreated,
      targetDate
    )
    .first<{ in_mutations: number; out_mutations: number }>();

  const inMut = round2(Number(mutRes?.in_mutations || 0));
  const outMut = round2(Number(mutRes?.out_mutations || 0));
  const expectedBal = round2(anchorBal + inMut - outMut);
  const diff = round2(expectedBal - cachedBal);

  const status = Math.abs(diff) < 0.005 ? "ok" : "discrepancy";
  const reason =
    Math.abs(diff) < 0.005
      ? "Saldo terverifikasi sesuai snapshot & mutasi"
      : `Selisih saldo terdeteksi: tercatat ${cachedBal} vs hitungan mutasi ${expectedBal}`;

  return {
    status,
    calculated_balance: expectedBal,
    expected_balance: expectedBal,
    cached_balance: cachedBal,
    difference: diff,
    anchor_balance: anchorBal,
    anchor_date: anchorDate,
    reason,
  };
}

export async function computeDashboardKpis(
  db: D1Database,
  monthStr?: string
): Promise<{
  safeToSpend: number;
  totalBalance: number;
  protectedSavings: number;
  emergencyAllocated: number;
  goalsAllocated: number;
  generalAllocated: number;
  passThroughOutstanding: number;
  currentCommitments: number;
  receivablesOutstanding: number;
  pendingExpenses: number;
  monthIncome: number;
  monthExpense: number;
  netCashflow: number;
  selectedMonth: string;
  asOfDate: string;
}> {
  const { dateStr: todayStr, monthStr: currentMonth } = getWibDate();
  const selectedMonth = monthStr && /^\d{4}-\d{2}$/.test(monthStr) ? monthStr : currentMonth;

  // 1. Total Liquid Balance from active Owned accounts
  const liquidRes = await db
    .prepare("SELECT COALESCE(SUM(current_balance), 0) as total_liquid FROM accounts WHERE active = 1 AND kind = 'Owned'")
    .first<{ total_liquid: number }>();
  const totalBalance = round2(Number(liquidRes?.total_liquid || 0));

  // 2. Allocation Goals (Emergency, Goals, General)
  const allocRes = await db
    .prepare(
      `SELECT
         COALESCE(SUM(CASE WHEN kind = 'Emergency' THEN allocated_amount ELSE 0 END), 0) as emergency,
         COALESCE(SUM(CASE WHEN kind = 'Goal' THEN allocated_amount ELSE 0 END), 0) as goals,
         COALESCE(SUM(CASE WHEN kind = 'General' THEN allocated_amount ELSE 0 END), 0) as general,
         COALESCE(SUM(allocated_amount), 0) as total_alloc
       FROM allocation_goals
       WHERE status = 'Active'`
    )
    .first<{ emergency: number; goals: number; general: number; total_alloc: number }>();

  const emergencyAllocated = round2(Number(allocRes?.emergency || 0));
  const goalsAllocated = round2(Number(allocRes?.goals || 0));
  const generalAllocated = round2(Number(allocRes?.general || 0));
  const protectedSavings = round2(Number(allocRes?.total_alloc || 0));

  // 3. Custody (Titipan) Outstanding from Event Ledger
  const custodyDebts = await db
    .prepare("SELECT id FROM debts WHERE kind = 'Custody' AND status = 'Active'")
    .all<{ id: number }>();

  let passThroughOutstanding = 0.0;
  for (const cd of custodyDebts.results || []) {
    const evRes = await db
      .prepare("SELECT COALESCE(SUM(effect * amount), 0.0) as out FROM debt_events WHERE debt_id = ?")
      .bind(cd.id)
      .first<{ out: number }>();
    passThroughOutstanding += Math.max(0.0, Number(evRes?.out || 0));
  }
  passThroughOutstanding = round2(passThroughOutstanding);

  // 4. Receivables Outstanding from Event Ledger (Informational only, does NOT increase liquidity)
  const recDebts = await db
    .prepare("SELECT id FROM debts WHERE kind = 'Receivable' AND status = 'Active'")
    .all<{ id: number }>();

  let receivablesOutstanding = 0.0;
  for (const rd of recDebts.results || []) {
    const evRes = await db
      .prepare("SELECT COALESCE(SUM(effect * amount), 0.0) as out FROM debt_events WHERE debt_id = ?")
      .bind(rd.id)
      .first<{ out: number }>();
    receivablesOutstanding += Math.max(0.0, Number(evRes?.out || 0));
  }
  receivablesOutstanding = round2(receivablesOutstanding);

  // 5. Effective Commitments (U_eff for Payables + X_eff for Regular Upcomings)
  // 5a. U_eff for Active Payables
  const payDebts = await db
    .prepare("SELECT id FROM debts WHERE kind = 'Payable' AND status = 'Active'")
    .all<{ id: number }>();

  let totalUeff = 0.0;
  for (const pd of payDebts.results || []) {
    const evRes = await db
      .prepare("SELECT COALESCE(SUM(effect * amount), 0.0) as out FROM debt_events WHERE debt_id = ?")
      .bind(pd.id)
      .first<{ out: number }>();
    const debtOut = Math.max(0.0, Number(evRes?.out || 0));

    // Active protected allocation coverage for upcomings linked to this Payable
    const covRes = await db
      .prepare(
        `SELECT COALESCE(SUM(pa.amount), 0.0) as cov
         FROM protected_allocations pa
         JOIN upcoming u ON pa.covers_upcoming_id = u.id
         WHERE u.debt_id = ? AND pa.status = 'Active' AND u.status = 'Upcoming'`
      )
      .bind(pd.id)
      .first<{ cov: number }>();
    const cov = Number(covRes?.cov || 0);
    totalUeff += Math.max(0.0, debtOut - cov);
  }

  // 5b. X_eff for Regular Upcomings (debt_id IS NULL OR debt_id = 0)
  const upcomings = await db
    .prepare(
      `SELECT id, amount, status, COALESCE(reserve_now, 0) as reserve_now
       FROM upcoming
       WHERE (debt_id IS NULL OR debt_id = 0)
         AND status IN ('Upcoming', 'Confirmed', 'Tentative')`
    )
    .all<{ id: number; amount: number; status: string; reserve_now: number }>();

  const covMap = (await getUpcomingActiveCoverage(db)) as Map<number, number>;
  let totalXeff = 0.0;
  for (const u of upcomings.results || []) {
    if (u.status === "Tentative" && Number(u.reserve_now || 0) === 0) {
      continue; // Tentative without reserve_now does not deduct
    }
    const cov = covMap.get(u.id) || 0.0;
    const amt = Number(u.amount || 0);
    totalXeff += Math.max(0.0, amt - cov);
  }

  const currentCommitments = round2(totalUeff + totalXeff);

  // 6. Pending Expenses from Provisional Neutral transactions
  const pendRes = await db
    .prepare(
      `SELECT COALESCE(SUM(amount), 0.0) as pend
       FROM transactions
       WHERE is_deleted = 0
         AND status = 'Provisional Neutral'
         AND (transaction_type = 'Expense' OR (transaction_type = '' AND amount > 0))`
    )
    .first<{ pend: number }>();
  const pendingExpenses = round2(Number(pendRes?.pend || 0));

  // 7. Safe to Spend Formula Prompt 11:
  // Liquid Assets - Active Allocations - Custody Outstanding - Effective Commitments - Pending Expenses
  const safeToSpend = round2(
    totalBalance - protectedSavings - passThroughOutstanding - currentCommitments - pendingExpenses
  );

  // 8. Monthly Income and Expense
  const monthlyRes = await db
    .prepare(
      `SELECT
         COALESCE(SUM(CASE WHEN transaction_type = 'Income' AND money_context = 'Personal' THEN amount ELSE 0 END), 0) as income,
         COALESCE(SUM(CASE WHEN transaction_type = 'Expense' AND money_context = 'Personal' THEN amount ELSE 0 END), 0) as expense
       FROM transactions
       WHERE is_deleted = 0 AND date LIKE ?`
    )
    .bind(`${selectedMonth}%`)
    .first<{ income: number; expense: number }>();

  const monthIncome = round2(Number(monthlyRes?.income || 0));
  const monthExpense = round2(Number(monthlyRes?.expense || 0));
  const netCashflow = round2(monthIncome - monthExpense);

  return {
    safeToSpend,
    totalBalance,
    protectedSavings,
    emergencyAllocated,
    goalsAllocated,
    generalAllocated,
    passThroughOutstanding,
    currentCommitments,
    receivablesOutstanding,
    pendingExpenses,
    monthIncome,
    monthExpense,
    netCashflow,
    selectedMonth,
    asOfDate: todayStr,
  };
}

export async function getDashboard(db: D1Database, monthParam?: string): Promise<Record<string, any>> {
  const kpis = await computeDashboardKpis(db, monthParam);

  const monthsRows = await db
    .prepare("SELECT DISTINCT substr(date, 1, 7) as m FROM transactions WHERE is_deleted = 0 ORDER BY m DESC")
    .all<{ m: string }>();
  const months = (monthsRows.results || []).map((r) => r.m);

  return {
    kpis,
    months,
  };
}

export async function getAccountsList(db: D1Database): Promise<Record<string, any>> {
  const accountsRes = await db
    .prepare(
      `SELECT name, kind, current_balance, balance_date, last_reconciled_at, active, protected, protected_amount, note
       FROM accounts ORDER BY kind ASC, name ASC`
    )
    .all();

  return {
    status: "ok",
    accounts: accountsRes.results || [],
  };
}

export async function getTransactionsList(
  db: D1Database,
  limit = 50,
  offset = 0,
  monthParam?: string
): Promise<Record<string, any>> {
  let query = `SELECT * FROM transactions WHERE is_deleted = 0`;
  const params: any[] = [];

  if (monthParam && /^\d{4}-\d{2}$/.test(monthParam)) {
    query += ` AND date LIKE ?`;
    params.push(`${monthParam}%`);
  }

  query += ` ORDER BY date DESC, time DESC, id DESC LIMIT ? OFFSET ?`;
  params.push(limit, offset);

  const stmt = db.prepare(query);
  const res = await stmt.bind(...params).all();

  return {
    status: "ok",
    transactions: res.results || [],
  };
}

export async function getGoalsSummary(db: D1Database): Promise<Record<string, any>> {
  const res = await db.prepare("SELECT * FROM allocation_goals ORDER BY priority ASC, id ASC").all();
  return {
    status: "success",
    goals: res.results || [],
  };
}

export async function getUpcomingList(db: D1Database): Promise<Record<string, any>> {
  const res = await db.prepare("SELECT * FROM upcoming ORDER BY due_date ASC, id ASC").all();
  return {
    status: "ok",
    upcoming: res.results || [],
  };
}

export async function getDebtsList(db: D1Database): Promise<Record<string, any>> {
  const debts = await db.prepare("SELECT * FROM debts ORDER BY id ASC").all();
  const events = await db.prepare("SELECT * FROM debt_events ORDER BY event_date ASC, id ASC").all();
  return {
    status: "success",
    debts: debts.results || [],
    events: events.results || [],
  };
}

export async function getInsightsSummary(db: D1Database, asOfDate?: string): Promise<Record<string, any>> {
  const targetDate = asOfDate || getWibDate().dateStr;

  const lastTx = await db
    .prepare(
      `SELECT date, time FROM transactions
       WHERE is_deleted = 0 AND date <= ?
       ORDER BY date DESC, time DESC, id DESC LIMIT 1`
    )
    .bind(targetDate)
    .first<{ date: string; time: string }>();

  const latestDate = lastTx?.date || targetDate;
  const latestTime = lastTx?.time || "00:00";

  return {
    status: "ok",
    insights: {
      as_of_date: targetDate,
      source_freshness: {
        latest_transaction_date: latestDate,
        latest_transaction_time: latestTime,
        source_name: "Input manual",
        status_label: "Input manual",
        is_sync_connector_active: false,
        message: `Catatan transaksi terbaru: ${latestDate} ${latestTime} WIB via input manual.`,
      },
    },
  };
}

// =====================================================================
// INGESTION & PARSER DOMAIN
// =====================================================================

export interface ParsedCandidate {
  tx_type: "Income" | "Expense" | "Transfer";
  amount: number;
  account: string;
  to_account?: string | null;
  category: string;
  money_context: "Personal" | "Pass-through" | "Historical Research" | "Third-party";
  person_name?: string | null;
  date: string;
  time?: string | null;
  confidence_score: number;
  reasons: string;
  status: "Pending" | "AutoApproved" | "Ignored";
}




export type CanonicalEventKind =
  | "MERCHANT_PAYMENT"
  | "EXTERNAL_TRANSFER"
  | "INCOMING_TRANSFER"
  | "OWN_TRANSFER"
  | "TOPUP"
  | "CASH_WITHDRAWAL"
  | "ALLOCATION_MOVEMENT"
  | "INVESTMENT_MOVEMENT"
  | "REFUND"
  | "SUBSCRIPTION_CHARGE"
  | "DIGITAL_PURCHASE"
  | "INVOICE_EVIDENCE"
  | "FAILED_ATTEMPT"
  | "NON_TRANSACTION";

export type FinancialClass =
  | "Expense"
  | "Income"
  | "Internal Transfer"
  | "Top-up"
  | "Cash Withdrawal"
  | "Investment Movement"
  | "Receivable"
  | "Payable"
  | "Refund"
  | "Pending Review"
  | "Ignore";

export type DestinationOwnerType = "SELF" | "OTHER_PERSON" | "MERCHANT" | "INSTITUTION" | "UNKNOWN";
export type EvidenceRole = "PRIMARY_PAYMENT" | "SECONDARY_RECEIPT" | "INVOICE" | "LIFECYCLE_STATUS" | "INTERMEDIARY";

export interface ParsedCandidate {
  tx_type: "Income" | "Expense" | "Transfer";
  amount: number;
  account: string;
  to_account?: string | null;
  category: string;
  money_context: "Personal" | "Pass-through" | "Historical Research" | "Third-party";
  person_name?: string | null;
  date: string;
  time?: string | null;
  confidence_score: number;
  reasons: string;
  status: "Pending" | "AutoApproved" | "Ignored";
}

export interface CanonicalFinancialEvent {
  event_id: string;
  occurred_at_wib: string;
  status: "Pending" | "Approved" | "Rejected" | "AutoApproved" | "Ignored" | "Applied";
  event_kind: CanonicalEventKind;
  financial_class: FinancialClass;
  financial_direction: "Debit" | "Credit" | "Neutral";
  amount: number;
  currency: string;
  fee_amount: number;
  source_account_alias: string | null;
  destination_account_alias: string | null;
  destination_owner_type: DestinationOwnerType;
  merchant_normalized: string | null;
  merchant_pan: string | null;
  merchant_location: string | null;
  counterparty_normalized: string | null;
  description_normalized: string | null;
  transaction_reference: string | null;
  external_order_id: string | null;
  confidence: number;
  recommended_action: string;
  review_reason: string | null;
  evidence_role: EvidenceRole;
  candidate?: ParsedCandidate | null;
}

export function isCanonicalCorrelationCompatible(
  incoming: CanonicalFinancialEvent,
  existing: {
    event_kind: string;
    financial_class: string;
    financial_direction: string;
    amount: number;
    merchant_normalized?: string | null;
  }
): boolean {
  // Secondary/lifecycle evidence may legitimately enrich an existing
  // canonical event. The strict economic-signature guard applies to
  // independent primary payment evidence.
  if (incoming.evidence_role !== "PRIMARY_PAYMENT") {
    return true;
  }

  if (
    incoming.event_kind !== existing.event_kind ||
    incoming.financial_class !== existing.financial_class ||
    incoming.financial_direction !== existing.financial_direction
  ) {
    return false;
  }

  const incomingAmount = Number(incoming.amount);
  const existingAmount = Number(existing.amount);

  if (
    !Number.isFinite(incomingAmount) ||
    !Number.isFinite(existingAmount) ||
    Math.abs(round2(incomingAmount) - round2(existingAmount)) >= 0.005
  ) {
    return false;
  }

  const normalizeMerchant = (value?: string | null): string => {
    const normalized = (value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();

    // Generic parser placeholder is not strong enough to reject correlation.
    return normalized === "qris merchant" ? "" : normalized;
  };

  const incomingMerchant = normalizeMerchant(incoming.merchant_normalized);
  const existingMerchant = normalizeMerchant(existing.merchant_normalized);

  if (
    incomingMerchant &&
    existingMerchant &&
    incomingMerchant !== existingMerchant
  ) {
    return false;
  }

  return true;
}
export function parseIndonesianAmount(text: string): number | null {
  if (!text) return null;

  // 1. Try explicit total/nominal/sebesar keyword first
  const kwMatch = text.match(/(?:total pembayaran|total|nominal|sebesar|jumlah|amount|bayar)\s*[:]?\s*(?:rp\.?|idr)?\s*([\d\.,]+)/i);
  let raw = kwMatch ? kwMatch[1].trim() : null;

  // 2. Try explicit currency prefix Rp / IDR
  if (!raw) {
    const curMatch = text.match(/(?:Rp\.?|IDR)\s*([\d\.,]+)/i);
    if (curMatch) raw = curMatch[1].trim();
  }

  // 3. Fallback to isolated numeric sequence
  if (!raw) {
    const fallbackMatch = text.match(/\b([\d\.,]+)\b/);
    if (fallbackMatch) raw = fallbackMatch[1].trim();
  }

  if (!raw) return null;

  if (/,\d{2}$/.test(raw)) {
    raw = raw.replace(/\./g, "").replace(",", ".");
  } else if (/\.\d{2}$/.test(raw)) {
    raw = raw.replace(/,/g, "");
  } else {
    raw = raw.replace(/[\.,]/g, "");
  }

  const parsed = parseFloat(raw);
  return isNaN(parsed) || parsed <= 0 ? null : round2(parsed);
}

export function inferMerchantCategory(merchantName: string, textContext = ""): string {
  const m = (merchantName || "").toLowerCase();
  const c = (textContext || "").toLowerCase();
  const full = `${m} ${c}`;

  if (full.includes("warung mbak yani") || full.includes("kantin arsitektur") || full.includes("resto") || full.includes("makan") || full.includes("padang")) {
    return "Main Meals";
  }
  if (full.includes("uta ngopi") || full.includes("kopi studio24") || full.includes("kopi") || full.includes("cafe") || full.includes("coffee") || full.includes("chatime")) {
    return "Cafe & Drinks";
  }
  if (full.includes("spbu") || full.includes("pertamina") || full.includes("shell") || full.includes("bensin") || full.includes("pertalite") || full.includes("pertamax")) {
    return "Fuel";
  }
  if (full.includes("xl") || full.includes("by.u") || full.includes("telkomsel") || full.includes("indosat") || full.includes("tri") || full.includes("pulsa") || full.includes("kuota")) {
    return "Phone & Internet";
  }
  if (full.includes("fotocopy sarjana") || full.includes("fotokopi") || full.includes("print") || full.includes("percetakan") || full.includes("buku") || full.includes("gramedia")) {
    return "Education & Career";
  }
  if (full.includes("parkir") || full.includes("parking") || full.includes("tol") || full.includes("etoll")) {
    return "Parking/Toll";
  }
  if (full.includes("google play") || full.includes("subscription") || full.includes("spotify") || full.includes("netflix") || full.includes("youtube")) {
    return "Subscriptions";
  }
  return "Other / Miscellaneous";
}

export function normalizeAccountName(name: string): string {
  const n = name.toLowerCase().trim();
  if (n.includes("shopeepay") || n.includes("shopee")) return "ShopeePay";
  if (n.includes("gopay")) return "GoPay";
  if (n.includes("jago")) return "Jago Main";
  if (n.includes("bca poket") || n.includes("poket")) {
    const sub = name.split(":")[1]?.trim() || "Tabungan";
    return `BCA Poket: ${sub}`;
  }
  if (n.includes("bca")) return "BCA Main";
  if (n.includes("cash") || n.includes("tunai")) return "Cash";
  if (n.includes("rdn") || n.includes("stockbit")) return "RDN Stockbit";
  return name;
}

// -------------------------------------------------------------------------
// Byte-safe 64-bit FNV-1a hash returning 16 lowercase hex characters.
// Pure synchronous, dependency-free, UTF-8 via TextEncoder.
// -------------------------------------------------------------------------
export function fnv1a64Hex(input: string): string {
  const FNV_OFFSET_BASIS = 0xcbf29ce484222325n;
  const FNV_PRIME = 0x00000100000001b3n;
  const MASK_64 = 0xffffffffffffffffn;

  const bytes = new TextEncoder().encode(input);
  let hash = FNV_OFFSET_BASIS;
  for (let i = 0; i < bytes.length; i++) {
    hash ^= BigInt(bytes[i]);
    hash = (hash * FNV_PRIME) & MASK_64;
  }
  return hash.toString(16).padStart(16, "0");
}

export const CANONICAL_EVENT_KIND_SET: ReadonlySet<string> = new Set([
  "MERCHANT_PAYMENT",
  "EXTERNAL_TRANSFER",
  "INCOMING_TRANSFER",
  "OWN_TRANSFER",
  "TOPUP",
  "CASH_WITHDRAWAL",
  "ALLOCATION_MOVEMENT",
  "INVESTMENT_MOVEMENT",
  "REFUND",
  "SUBSCRIPTION_CHARGE",
  "DIGITAL_PURCHASE",
  "INVOICE_EVIDENCE",
  "FAILED_ATTEMPT",
  "NON_TRANSACTION",
]);

// =========================================================================
// GMAIL TRANSACTION INTELLIGENCE V1 PARSER
// =========================================================================
export function parseGmailIntelligence(
  subject: string,
  bodyText: string,
  fromAddress: string,
  occurredAt?: string
): CanonicalFinancialEvent {
  const wib = getWibDate(occurredAt ? new Date(occurredAt) : new Date());
  const cleanFrom = (fromAddress || "").toLowerCase();
  const cleanSubj = (subject || "").toLowerCase();
  const cleanBody = (bodyText || "").toLowerCase();
  const occurredAtWib = `${wib.dateStr} ${wib.timeStr} WIB`;

  // -----------------------------------------------------------------------
  // 1. BCA (bca@bca.co.id)
  // -----------------------------------------------------------------------
  if (cleanFrom.includes("bca.co.id")) {
    // A. Failed transaction / Gagal
    const failedStatusLabel = bodyText.match(
      /(?:^|\r?\n)\s*(?:status|status\s+transaksi)\s*(?::\s*|\r?\n\s*:\s*)gagal\b/im
    );

    if (
      failedStatusLabel ||
      cleanBody.includes("status transaksi: gagal") ||
      cleanBody.includes("transaksi gagal") ||
      cleanBody.includes("transaksi ditolak") ||
      cleanSubj.includes("gagal")
    ) {
      const amount =
        parseIndonesianAmount(bodyText) ||
        parseIndonesianAmount(subject) ||
        0;

      const failedRefMatch = bodyText.match(
        /(?:^|\r?\n)\s*(?:nomor\s+referensi|no\.\s*referensi|no\s+referensi|referensi|reference|ref)\s*(?::\s*|\r?\n\s*:\s*)([^\r\n]+)/im
      );

      const failedRef =
        failedRefMatch
          ? failedRefMatch[1].trim()
          : null;

      return {
        event_id: failedRef
          ? `bca_fail_${failedRef}`
          : `bca_fail_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "Ignored",
        event_kind: "FAILED_ATTEMPT",
        financial_class: "Ignore",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "BCA Main",
        destination_account_alias: null,
        destination_owner_type: "UNKNOWN",
        merchant_normalized: null,
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: null,
        description_normalized:
          "BCA Alert: Transaksi Gagal/Ditolak",
        transaction_reference: failedRef,
        external_order_id: null,
        confidence: 0.99,
        recommended_action:
          "Ignore failed transaction",
        review_reason:
          "Failed attempt; zero ledger mutation.",
        evidence_role: "LIFECYCLE_STATUS",
        candidate: null,
      };
    }

    // B. BCA Cardless Tarik Tunai
    if (cleanBody.includes("tarik tunai tanpa kartu") || cleanBody.includes("cardless withdrawal") || cleanSubj.includes("tarik tunai")) {
      const amount = parseIndonesianAmount(bodyText) || 0;
      const refMatch = bodyText.match(
        /(?:^|\r?\n)\s*(?:nomor\s+referensi|no\.?\s*referensi|reference|ref)\s*(?::\s*|\r?\n\s*:\s*)([^\r\n]+)/im
      );

      const refId =
        refMatch
          ? refMatch[1].trim()
          : null;

      const cand: ParsedCandidate = {
        tx_type: "Transfer",
        amount,
        account: "BCA Main",
        to_account: "Cash",
        category: "Cash Withdrawal",
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons: "BCA Cardless: Penarikan tunai tanpa kartu -> BCA ke Cash.",
        status: "AutoApproved",
      };

      return {
        event_id: refId ? `bca_cash_${refId}` : `bca_cash_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind: "CASH_WITHDRAWAL",
        financial_class: "Cash Withdrawal",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "BCA Main",
        destination_account_alias: "Cash",
        destination_owner_type: "SELF",
        merchant_normalized: null,
        merchant_pan: null,
        merchant_location: "ATM BCA",
        counterparty_normalized: "Self (Cash)",
        description_normalized: "BCA Cardless Tarik Tunai",
        transaction_reference: refId,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Record Internal Transfer BCA -> Cash",
        review_reason: null,
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }
    // C. BCA Poket
    if (cleanBody.includes("poket") || cleanSubj.includes("poket")) {
      const isPocketClosure =
        cleanBody.includes("penutupan poket") ||
        cleanBody.includes("tutup poket") ||
        cleanSubj.includes("penutupan poket");

      // Ambil nama Poket secara lebih ketat agar tidak menyapu banyak baris email.
      const pocketMatch = bodyText.match(
        /nama\s+poket\s*[\r\n\s]*:\s*[\r\n\s]*([^\r\n]+)/i
      );
      const pocketName = pocketMatch
        ? pocketMatch[1].trim()
        : "Tabungan";

      const pocketAccount = `BCA Poket: ${pocketName}`;

      // Penutupan Poket memakai "Total Saldo" sebagai dana yang kembali
      // ke rekening induk. Jangan memakai currency pertama secara generik,
      // karena email dapat berisi "Saldo Poket IDR 0.00" lebih dahulu.
      let amount: number = 0;

      if (isPocketClosure) {
        const totalSaldoMatch = bodyText.match(
          /total\s+saldo\s*[\r\n\s]*:\s*[\r\n\s]*(?:rp\.?|idr)?\s*([\d.,]+)/i
        );

        amount = totalSaldoMatch
          ? (parseIndonesianAmount(`Total: IDR ${totalSaldoMatch[1]}`) || 0)
          : (parseIndonesianAmount(bodyText) || 0);
      } else {
        amount = parseIndonesianAmount(bodyText) || 0;
      }

      const refMatch = bodyText.match(
        /nomor\s+referensi\s*[\r\n\s]*:\s*[\r\n\s]*([a-zA-Z0-9]+)/i
      );
      const refId = refMatch ? refMatch[1] : null;

      // Penutupan Poket berarti dana kembali dari Poket ke BCA Main.
      if (isPocketClosure) {
        const cand: ParsedCandidate | null =
          amount > 0
            ? {
                tx_type: "Transfer",
                amount,
                account: pocketAccount,
                to_account: "BCA Main",
                category: "Allocation Movement",
                money_context: "Personal",
                person_name: null,
                date: wib.dateStr,
                time: wib.timeStr,
                confidence_score: 0.95,
                reasons:
                  `BCA Poket: Penutupan ${pocketAccount}; sisa dana kembali ke BCA Main. Bukan pengeluaran ledger.`,
                status: "AutoApproved",
              }
            : null;

        return {
          event_id: refId
            ? `bca_poket_close_${refId}`
            : `bca_poket_close_${Date.now()}`,
          occurred_at_wib: occurredAtWib,
          status: amount > 0 ? "AutoApproved" : "Ignored",
          event_kind: "ALLOCATION_MOVEMENT",
          financial_class: "Internal Transfer",
          financial_direction: "Neutral",
          amount,
          currency: "IDR",
          fee_amount: 0,
          source_account_alias: pocketAccount,
          destination_account_alias: "BCA Main",
          destination_owner_type: "SELF",
          merchant_normalized: null,
          merchant_pan: null,
          merchant_location: null,
          counterparty_normalized: "BCA Main",
          description_normalized: `Penutupan BCA Poket (${pocketName})`,
          transaction_reference: refId,
          external_order_id: null,
          confidence: 0.95,
          recommended_action:
            amount > 0
              ? "Record Allocation Movement Poket -> BCA Main"
              : "Keep as lifecycle evidence; zero ledger mutation",
          review_reason:
            amount > 0
              ? null
              : "Penutupan Poket tanpa nominal perpindahan dana yang dapat dicatat.",
          evidence_role: "LIFECYCLE_STATUS",
          candidate: cand,
        };
      }

      // Aktivitas Poket selain penutupan mempertahankan perilaku lama:
      // BCA Main -> Poket sebagai allocation/internal movement.
      const cand: ParsedCandidate | null =
        amount > 0
          ? {
              tx_type: "Transfer",
              amount,
              account: "BCA Main",
              to_account: pocketAccount,
              category: "Allocation Movement",
              money_context: "Personal",
              person_name: null,
              date: wib.dateStr,
              time: wib.timeStr,
              confidence_score: 0.95,
              reasons:
                `BCA Poket: Pergerakan alokasi dana ke ${pocketAccount}. Bukan pengeluaran ledger.`,
              status: "AutoApproved",
            }
          : null;

      return {
        event_id: refId
          ? `bca_poket_${refId}`
          : `bca_poket_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: amount > 0 ? "AutoApproved" : "Ignored",
        event_kind: "ALLOCATION_MOVEMENT",
        financial_class: "Internal Transfer",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "BCA Main",
        destination_account_alias: pocketAccount,
        destination_owner_type: "SELF",
        merchant_normalized: null,
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: pocketAccount,
        description_normalized: `BCA Poket Alokasi (${pocketName})`,
        transaction_reference: refId,
        external_order_id: null,
        confidence: 0.95,
        recommended_action:
          amount > 0
            ? "Record Allocation Movement (Internal)"
            : "Keep as evidence; zero ledger mutation",
        review_reason:
          amount > 0
            ? null
            : "Aktivitas Poket tanpa nominal perpindahan dana yang dapat dicatat.",
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // D. BCA QRIS
    if (
      cleanBody.includes("qris") ||
      cleanSubj.includes("qris")
    ) {
      const amount =
        parseIndonesianAmount(bodyText) ||
        parseIndonesianAmount(subject) ||
        0;

      const extractQrisField =
        (labels: string[]): string | null => {
          const escapedLabels =
            labels.map((label) =>
              label.replace(
                /[.*+?^${}()|[\]\\]/g,
                "\\$&"
              )
            );

          const fieldMatch =
            bodyText.match(
              new RegExp(
                `(?:^|\\r?\\n)\\s*(?:${escapedLabels.join("|")})\\s*(?::\\s*|\\r?\\n\\s*:\\s*)([^\\r\\n]+)`,
                "im"
              )
            );

          return fieldMatch
            ? fieldMatch[1].trim()
            : null;
        };

      const qrisType =
        extractQrisField([
          "Jenis Transaksi",
        ]) || "";

      const refId =
        extractQrisField([
          "Nomor Referensi",
          "No. Referensi",
          "No Referensi",
          "Referensi",
          "Reference",
          "Ref",
        ]);

      const recipientName =
        extractQrisField([
          "Nama Penerima",
          "Penerima",
          "Nama Tujuan",
        ]);

      const isTransferQris =
        /\btransfer\s+qris\b/i.test(
          qrisType
        );

      if (isTransferQris) {
        const counterparty =
          recipientName ||
          "Third Party";

        const cand: ParsedCandidate = {
          tx_type: "Expense",
          amount,
          account: "BCA Main",
          to_account: null,
          category:
            "Other / Miscellaneous",
          money_context: "Personal",
          person_name: null,
          date: wib.dateStr,
          time: wib.timeStr,
          confidence_score: 0.80,
          reasons:
            "BCA Transfer QRIS: transfer ke pihak lain; review konteks sebelum ledger.",
          status: "Pending",
        };

        return {
          event_id: refId
            ? `bca_qris_transfer_${refId}`
            : `bca_qris_transfer_${Date.now()}`,
          occurred_at_wib: occurredAtWib,
          status: "Pending",
          event_kind:
            "EXTERNAL_TRANSFER",
          financial_class:
            "Pending Review",
          financial_direction:
            "Debit",
          amount,
          currency: "IDR",
          fee_amount: 0,
          source_account_alias:
            "BCA Main",
          destination_account_alias:
            null,
          destination_owner_type:
            "OTHER_PERSON",
          merchant_normalized: null,
          merchant_pan: null,
          merchant_location: null,
          counterparty_normalized:
            counterparty,
          description_normalized:
            "Transfer QRIS BCA",
          transaction_reference:
            refId,
          external_order_id: null,
          confidence: 0.80,
          recommended_action:
            "Review recipient and context before ledger classification",
          review_reason:
            "Transfer QRIS ke pihak lain memerlukan review konteks.",
          evidence_role:
            "PRIMARY_PAYMENT",
          candidate: cand,
        };
      }

      const merchantName =
        extractQrisField([
          "Pembayaran Ke",
          "Nama Merchant",
          "Merchant",
          "Pembayaran Kepada",
          "Kepada",
        ]) ||
        "QRIS Merchant";

      const pan =
        extractQrisField([
          "Merchant PAN",
          "NMID",
          "PAN",
        ]);

      const location =
        extractQrisField([
          "Lokasi Merchant",
          "Lokasi",
          "Kota",
        ]);

      const isPaymentQris =
        /\bpembayaran\s+qris\b/i.test(
          qrisType
        ) ||
        (
          !qrisType &&
          merchantName !==
            "QRIS Merchant"
        );

      if (!isPaymentQris) {
        const cand: ParsedCandidate = {
          tx_type: "Expense",
          amount,
          account: "BCA Main",
          to_account: null,
          category:
            "Other / Miscellaneous",
          money_context: "Personal",
          person_name: null,
          date: wib.dateStr,
          time: wib.timeStr,
          confidence_score: 0.50,
          reasons:
            "BCA QRIS: tipe transaksi belum dikenali secara aman.",
          status: "Pending",
        };

        return {
          event_id: refId
            ? `bca_qris_review_${refId}`
            : `bca_qris_review_${Date.now()}`,
          occurred_at_wib: occurredAtWib,
          status: "Pending",
          event_kind:
            "EXTERNAL_TRANSFER",
          financial_class:
            "Pending Review",
          financial_direction:
            "Debit",
          amount,
          currency: "IDR",
          fee_amount: 0,
          source_account_alias:
            "BCA Main",
          destination_account_alias:
            null,
          destination_owner_type:
            "UNKNOWN",
          merchant_normalized: null,
          merchant_pan: null,
          merchant_location: null,
          counterparty_normalized:
            recipientName ||
            "Third Party",
          description_normalized:
            "QRIS BCA - review required",
          transaction_reference:
            refId,
          external_order_id: null,
          confidence: 0.50,
          recommended_action:
            "Manual review",
          review_reason:
            "Jenis QRIS belum cocok dengan Pembayaran QRIS atau Transfer QRIS.",
          evidence_role:
            "PRIMARY_PAYMENT",
          candidate: cand,
        };
      }

      const category =
        inferMerchantCategory(
          merchantName,
          bodyText
        );

      const cand: ParsedCandidate = {
        tx_type: "Expense",
        amount,
        account: "BCA Main",
        to_account: null,
        category,
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons:
          `BCA QRIS: Pembayaran berhasil ke ${merchantName}.`,
        status: "AutoApproved",
      };

      return {
        event_id: refId
          ? `bca_qris_${refId}`
          : `bca_qris_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind:
          "MERCHANT_PAYMENT",
        financial_class:
          "Expense",
        financial_direction:
          "Debit",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias:
          "BCA Main",
        destination_account_alias:
          null,
        destination_owner_type:
          "MERCHANT",
        merchant_normalized:
          merchantName,
        merchant_pan: pan,
        merchant_location: location,
        counterparty_normalized:
          merchantName,
        description_normalized:
          `Pembayaran QRIS ${merchantName}`,
        transaction_reference:
          refId,
        external_order_id: null,
        confidence: 0.95,
        recommended_action:
          "Record QRIS Expense",
        review_reason: null,
        evidence_role:
          "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // E. BCA Top-up ShopeePay (Virtual Account 122...)
    if (cleanBody.includes("shopeepay") || cleanBody.includes("airpay") || cleanBody.includes("12208") || cleanBody.includes("122")) {
      const amount = parseIndonesianAmount(bodyText) || 0;
      const cand: ParsedCandidate = {
        tx_type: "Transfer",
        amount,
        account: "BCA Main",
        to_account: "ShopeePay",
        category: "Top-up",
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons: "BCA Topup: Transfer ke ShopeePay VA -> Internal Transfer SELF.",
        status: "AutoApproved",
      };

      return {
        event_id: `bca_topup_shopee_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind: "TOPUP",
        financial_class: "Top-up",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "BCA Main",
        destination_account_alias: "ShopeePay",
        destination_owner_type: "SELF",
        merchant_normalized: "ShopeePay",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "ShopeePay (Self)",
        description_normalized: "Top-up ShopeePay via BCA Virtual Account",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Record Internal Transfer (Topup)",
        review_reason: null,
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // F. BCA Transfer to Jago (SELF)
    if (cleanBody.includes("jago") || cleanBody.includes("bank jago") || cleanBody.includes("artos")) {
      const amount = parseIndonesianAmount(bodyText) || 0;
      const cand: ParsedCandidate = {
        tx_type: "Transfer",
        amount,
        account: "BCA Main",
        to_account: "Jago Main",
        category: "Other / Miscellaneous",
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons: "BCA ke Jago: Transfer antar rekening sendiri (SELF).",
        status: "AutoApproved",
      };

      return {
        event_id: `bca_jago_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind: "OWN_TRANSFER",
        financial_class: "Internal Transfer",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "BCA Main",
        destination_account_alias: "Jago Main",
        destination_owner_type: "SELF",
        merchant_normalized: null,
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "Jago Main (Self)",
        description_normalized: "Transfer BCA ke Jago",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Record Internal Transfer",
        review_reason: null,
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // G. Generic BCA Outgoing / Incoming
    const amount = parseIndonesianAmount(bodyText) || parseIndonesianAmount(subject) || 0;
    const isIncoming = cleanBody.includes("transfer dari") || cleanBody.includes("kredit") || cleanBody.includes("masuk");

    const cand: ParsedCandidate = {
      tx_type: isIncoming ? "Income" : "Expense",
      amount,
      account: "BCA Main",
      to_account: null,
      category: "Other / Miscellaneous",
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: 0.60,
      reasons: isIncoming
        ? "BCA: Dana masuk dari pihak lain. Memerlukan review konteks."
        : "BCA: Transfer keluar ke perorangan/pihak ketiga. Memerlukan review kategori.",
      status: "Pending",
    };

    return {
      event_id: `bca_tx_${Date.now()}`,
      occurred_at_wib: occurredAtWib,
      status: "Pending",
      event_kind: isIncoming ? "INCOMING_TRANSFER" : "EXTERNAL_TRANSFER",
      financial_class: "Pending Review",
      financial_direction: isIncoming ? "Credit" : "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: isIncoming ? null : "BCA Main",
      destination_account_alias: isIncoming ? "BCA Main" : null,
      destination_owner_type: "OTHER_PERSON",
      merchant_normalized: null,
      merchant_pan: null,
      merchant_location: null,
      counterparty_normalized: "Third Party",
      description_normalized: isIncoming ? "Transfer Masuk BCA" : "Transfer Keluar BCA",
      transaction_reference: null,
      external_order_id: null,
      confidence: 0.60,
      recommended_action: "Review recipient/sender before ledger classification",
      review_reason: "Person-to-person transfer requires manual semantics confirmation.",
      evidence_role: "PRIMARY_PAYMENT",
      candidate: cand,
    };
  }

  // -----------------------------------------------------------------------
  // 2. JAGO (noreply@jago.com)
  // -----------------------------------------------------------------------
  if (cleanFrom.includes("jago.com")) {
    const amount = parseIndonesianAmount(bodyText) || parseIndonesianAmount(subject) || 0;

    // A. RDN / Stockbit Investment
    if (cleanBody.includes("rdn") || cleanBody.includes("stockbit") || cleanSubj.includes("stockbit") || cleanSubj.includes("rdn")) {
      const isFromRdn = cleanBody.includes("dari rdn") || cleanBody.includes("tarik dari stockbit");
      const cand: ParsedCandidate = {
        tx_type: "Transfer",
        amount,
        account: isFromRdn ? "RDN Stockbit" : "Jago Main",
        to_account: isFromRdn ? "Jago Main" : "RDN Stockbit",
        category: "Investment Movement",
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons: "Jago: Pergerakan dana investasi RDN / Stockbit.",
        status: "AutoApproved",
      };

      return {
        event_id: `jago_rdn_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind: "INVESTMENT_MOVEMENT",
        financial_class: "Investment Movement",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: isFromRdn ? "RDN Stockbit" : "Jago Main",
        destination_account_alias: isFromRdn ? "Jago Main" : "RDN Stockbit",
        destination_owner_type: "SELF",
        merchant_normalized: "Stockbit / RDN",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "RDN Stockbit",
        description_normalized: "Mutasi Investasi Jago <-> RDN Stockbit",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Record Investment Movement",
        review_reason: null,
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // B. ShopeePay ke Jago (SELF)
    if (cleanBody.includes("shopeepay") || cleanBody.includes("airpay")) {
      const cand: ParsedCandidate = {
        tx_type: "Transfer",
        amount,
        account: "ShopeePay",
        to_account: "Jago Main",
        category: "Other / Miscellaneous",
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons: "Jago: Tarik saldo ShopeePay ke Jago (Internal Transfer SELF).",
        status: "AutoApproved",
      };

      return {
        event_id: `jago_shopee_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind: "OWN_TRANSFER",
        financial_class: "Internal Transfer",
        financial_direction: "Neutral",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "ShopeePay",
        destination_account_alias: "Jago Main",
        destination_owner_type: "SELF",
        merchant_normalized: null,
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "ShopeePay (Self)",
        description_normalized: "Transfer ShopeePay ke Jago",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Record Internal Transfer",
        review_reason: null,
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // C. Jago Merchant Payment
    if (cleanBody.includes("pembayaran merchant") || cleanBody.includes("pembayaran berhasil") || cleanBody.includes("merchant")) {
      const mMatch = bodyText.match(/(?:merchant|di|kepada)\s*[:]?\s*([a-zA-Z0-9\s\.,\-_]+)/i);
      const merchant = mMatch ? mMatch[1].trim() : "Merchant Jago";
      const category = inferMerchantCategory(merchant, bodyText);

      const cand: ParsedCandidate = {
        tx_type: "Expense",
        amount,
        account: "Jago Main",
        to_account: null,
        category,
        money_context: "Personal",
        person_name: null,
        date: wib.dateStr,
        time: wib.timeStr,
        confidence_score: 0.95,
        reasons: `Jago: Pembayaran merchant ${merchant}.`,
        status: "AutoApproved",
      };

      return {
        event_id: `jago_merchant_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "AutoApproved",
        event_kind: "MERCHANT_PAYMENT",
        financial_class: "Expense",
        financial_direction: "Debit",
        amount,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: "Jago Main",
        destination_account_alias: null,
        destination_owner_type: "MERCHANT",
        merchant_normalized: merchant,
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: merchant,
        description_normalized: `Pembayaran Jago ke ${merchant}`,
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Record Expense",
        review_reason: null,
        evidence_role: "PRIMARY_PAYMENT",
        candidate: cand,
      };
    }

    // D. Generic Jago
    const isIncoming = cleanBody.includes("uang masuk") || cleanBody.includes("transfer dari");
    const cand: ParsedCandidate = {
      tx_type: isIncoming ? "Income" : "Expense",
      amount,
      account: "Jago Main",
      to_account: null,
      category: "Other / Miscellaneous",
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: 0.60,
      reasons: "Jago: Transfer pihak ketiga. Memerlukan review manual.",
      status: "Pending",
    };

    return {
      event_id: `jago_tx_${Date.now()}`,
      occurred_at_wib: occurredAtWib,
      status: "Pending",
      event_kind: isIncoming ? "INCOMING_TRANSFER" : "EXTERNAL_TRANSFER",
      financial_class: "Pending Review",
      financial_direction: isIncoming ? "Credit" : "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: isIncoming ? null : "Jago Main",
      destination_account_alias: isIncoming ? "Jago Main" : null,
      destination_owner_type: "OTHER_PERSON",
      merchant_normalized: null,
      merchant_pan: null,
      merchant_location: null,
      counterparty_normalized: "Third Party",
      description_normalized: isIncoming ? "Transfer Masuk Jago" : "Transfer Keluar Jago",
      transaction_reference: null,
      external_order_id: null,
      confidence: 0.60,
      recommended_action: "Review transfer",
      review_reason: "Third party transfer semantics unconfirmed.",
      evidence_role: "PRIMARY_PAYMENT",
      candidate: cand,
    };
  }

  // -----------------------------------------------------------------------
  // 3. FLIP (no-reply@flip.id)
  // -----------------------------------------------------------------------
  function extractFlipReferenceId(
    subject: string,
    body: string
  ): { id: string | null; kind: string | null } {
    const combined = (subject || "") + "\n" + (body || "");
    const patterns = [
      { regex: /#FT\d{9}/i, kind: "flip_transfer" },
      { regex: /#W\d{9}/i, kind: "flip_transfer_digital" },
      { regex: /#R\d{8}/i, kind: "flip_transfer_or_refund" },
      { regex: /#INT\d{7}/i, kind: "flip_international" },
      { regex: /#BT\d{8}/i, kind: "flip_bulk" },
      { regex: /#QT-\d{20}/i, kind: "flip_qris" },
      { regex: /\bFT\d{9}\b/i, kind: "flip_fallback" },
    ];
    for (const p of patterns) {
      const m = combined.match(p.regex);
      if (m) return { id: m[0].toUpperCase(), kind: p.kind };
    }
    return { id: null, kind: null };
  }

  if (cleanFrom.includes("flip.id")) {
    // 1. Blacklist check BEFORE any parsing
    const isCoinReward = /#TU\d{9}/i.test(subject + bodyText);
    const isMarketingSubject =
      /Koin|Coin|Cashback|Pemenang|Pemeliharaan|expired|sure you use saldo|Flip Coins will expire/i.test(
        subject || ""
      ) || /sure you use saldo|Flip Coins will expire/i.test(bodyText || "");
    const isMarketingSender =
      cleanFrom.includes("hello@flip.id") ||
      cleanFrom.includes("hello@mail.flip.id");
    const isFailedAttempt = /TRANSAKSI GAGAL DIPROSES/i.test(bodyText || "");

    if (isCoinReward || isMarketingSubject || isMarketingSender || isFailedAttempt) {
      return {
        event_id: `flip_skip_${occurredAtWib.replace(/[^0-9]/g, "")}`,
        occurred_at_wib: occurredAtWib,
        status: "Ignored",
        event_kind: (isFailedAttempt ? "FAILED_ATTEMPT" : "NON_TRANSACTION") as any,
        financial_class: "Ignore",
        financial_direction: "Neutral",
        amount: 0,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: null,
        destination_account_alias: null,
        destination_owner_type: "MERCHANT",
        merchant_normalized: "Flip",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "Flip",
        description_normalized: isFailedAttempt
          ? "Flip Failed Transfer Notice"
          : "Flip Non-Transaction Notice",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Keep as lifecycle evidence; zero ledger mutation",
        review_reason: isFailedAttempt
          ? "Flip failed transfer; no completed charge."
          : "Flip non-transaction notice.",
        evidence_role: "LIFECYCLE_STATUS",
        candidate: null,
      };
    }

    // 2. Extract reference ID multi-format
    const { id: refId, kind: refKind } = extractFlipReferenceId(
      subject || "",
      bodyText || ""
    );

    // 3. Extract amount
    const amount =
      parseIndonesianAmount(bodyText) || parseIndonesianAmount(subject) || 0;

    // 4. Extract destination / merchant (strict regex, no over-capture)
    const destMatch = bodyText.match(
      /(?:destination name|recipient name|penerima|atas nama)\s*[\r\n:]+\s*([^\r\n]{3,60})/i
    );
    const destName = destMatch ? destMatch[1].trim() : null;

    // 5. Detect refund / bulk / qris / international
    const isRefund =
      /refund|pengembalian dana/i.test(subject || "") ||
      /refund|pengembalian dana/i.test(bodyText || "");
    const isBulk =
      refKind === "flip_bulk" || /banyak tujuan/i.test(subject || "");
    const isQris =
      refKind === "flip_qris" || /QRIS Payment/i.test(subject || "");
    const isInternational =
      refKind === "flip_international" ||
      /transfer to Malaysia|Flip Globe/i.test(subject || "");

    // QRIS merchant extraction (A2_QRIS_HYBRID)
    let qrisMerchant: string | null = null;
    if (isQris) {
      const qrisMatch = bodyText.match(
        /(?:merchant(?:\s*name)?|penerima|kepada|atas nama|destination name|recipient name)\s*[\r\n:]+\s*([^\r\n]{3,60})/i
      );
      qrisMerchant = qrisMatch ? qrisMatch[1].trim() : null;
    }

    // 6. Classify
    let financialClass: string;
    let eventKind: string;
    let txType: string;
    let category: string;

    if (isRefund) {
      financialClass = "Refund";
      eventKind = "REFUND";
      txType = "Reversal";
      category = "Other / Miscellaneous";
    } else if (isQris) {
      financialClass = "Expense";
      eventKind = "MERCHANT_PAYMENT";
      txType = "Expense";
      category = "Other / Miscellaneous";
    } else if (isInternational) {
      financialClass = "Expense";
      eventKind = "EXTERNAL_TRANSFER";
      txType = "Expense";
      category = "Lain-lain / Lab Equipment";
    } else if (isBulk) {
      financialClass = "Expense";
      eventKind = "EXTERNAL_TRANSFER";
      txType = "Transfer";
      category = "Transfer & Investasi / Transfer ke Teman";
    } else {
      financialClass = "Expense";
      eventKind = "EXTERNAL_TRANSFER";
      txType = "Transfer";
      category = "Transfer & Investasi / Transfer ke Teman";
    }

    // Status & confidence determination
    let status: "Pending" | "AutoApproved" = "Pending";
    let confidence = 0.75;

    if (isRefund) {
      status = refId ? "AutoApproved" : "Pending";
      confidence = refId ? 0.95 : 0.75;
    } else if (isQris) {
      if (!qrisMerchant) {
        status = "Pending";
        confidence = 0.60;
      } else {
        status = refId ? "AutoApproved" : "Pending";
        confidence = refId ? 0.95 : 0.75;
      }
    } else {
      status = refId ? "AutoApproved" : "Pending";
      confidence = refId ? 0.95 : 0.75;
    }

    // 7. Build candidate (A1_REFUND_IS_REVERSAL: refund creates candidate with is_reversal: true)
    const cand: ParsedCandidate | null =
      amount > 0
        ? ({
            tx_type: txType as any,
            amount,
            account: "BCA Main",
            to_account: null,
            category,
            money_context: "Personal",
            person_name: isQris ? qrisMerchant : destName,
            date: wib.dateStr,
            time: wib.timeStr,
            confidence_score: confidence,
            reasons: refId
              ? `Flip: ${eventKind} (${refId})${
                  (isQris ? qrisMerchant : destName)
                    ? " ke " + (isQris ? qrisMerchant : destName)
                    : ""
                }.`
              : `Flip: ${eventKind} tanpa ID referensi.`,
            status,
            ...(isRefund ? { is_reversal: true } : {}),
          } as any)
        : null;

    // A3 & A4: Event ID formatting
    // A3: No '#' symbol in event_id for referenced events
    // A4: Stable byte-safe hash without Date.now() for unreferenced events
    const eventId = refId
      ? `flip_${refId.replace(/^#/, "")}`
      : `flip_noref_${fnv1a64Hex(
          (subject || "") + "\n" + (bodyText || "") + "\n" + occurredAtWib
        )}`;

    const merchantNormalized = isQris ? (qrisMerchant || "Flip") : "Flip";
    const counterpartyNormalized = isQris
      ? (qrisMerchant || "Flip")
      : (destName || "Flip");

    if (!CANONICAL_EVENT_KIND_SET.has(eventKind)) {
      throw new Error(`Flip parser produced invalid event_kind: ${eventKind}`);
    }

    return {
      event_id: eventId,
      occurred_at_wib: occurredAtWib,
      status,
      event_kind: eventKind as any,
      financial_class: financialClass as any,
      financial_direction: isRefund ? "Credit" : "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: "BCA Main",
      destination_account_alias: null,
      destination_owner_type: isRefund
        ? "SELF"
        : (isQris ? (qrisMerchant ? "MERCHANT" : "UNKNOWN") : (destName ? "OTHER_PERSON" : "UNKNOWN")),
      merchant_normalized: merchantNormalized,
      merchant_pan: null,
      merchant_location: null,
      counterparty_normalized: counterpartyNormalized,
      description_normalized: `Flip ${eventKind}${refId ? " " + refId : ""}`,
      transaction_reference: refId,
      external_order_id: refId,
      confidence,
      recommended_action: isRefund
        ? "Record Flip refund reversal; correlate with original debit"
        : "Record Flip transaction; dedup by reference ID",
      review_reason: !refId
        ? "Flip transaction without extractable reference ID."
        : isQris && !qrisMerchant
        ? "Flip QRIS payment without extractable merchant name."
        : null,
      evidence_role: "PRIMARY_PAYMENT",
      candidate: cand,
      ...(isRefund ? { is_reversal: true } : {}),
    } as any;
  }

  // -----------------------------------------------------------------------
  // 4. GOOGLE PLAY (googleplay-noreply@google.com)
  // -----------------------------------------------------------------------
  if (cleanFrom.includes("googleplay-noreply@google.com")) {
    const orderMatch = bodyText.match(
      /(?:nomor pesanan|order number)\s*[:]?\s*(GPA\.[\d\-]+)/i
    );
    const orderId = orderMatch ? orderMatch[1] : null;

    // Detect non-charge lifecycle semantics BEFORE generic amount parsing.
    // Otherwise digits from app names, GPA IDs, or dates can be mistaken
    // for transaction amounts.
    const isCancellation =
      cleanSubj.includes("canceled") ||
      cleanSubj.includes("cancelled") ||
      cleanSubj.includes("dibatalkan") ||
      cleanBody.includes("has been canceled") ||
      cleanBody.includes("has been cancelled") ||
      cleanBody.includes("subscription canceled") ||
      cleanBody.includes("subscription cancelled") ||
      cleanBody.includes("dibatalkan");

    const isDeclined =
      cleanBody.includes("declined") ||
      cleanBody.includes("ditolak");

    const lifecycleKey =
      (occurredAt || occurredAtWib)
        .replace(/[^0-9]/g, "")
        .slice(0, 14) || "unknown";

    if (isCancellation || isDeclined) {
      return {
        event_id: orderId
          ? `gplay_lifecycle_${orderId}_${lifecycleKey}`
          : `gplay_lifecycle_${lifecycleKey}`,
        occurred_at_wib: occurredAtWib,
        status: "Ignored",
        event_kind: "NON_TRANSACTION",
        financial_class: "Ignore",
        financial_direction: "Neutral",
        amount: 0,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: null,
        destination_account_alias: null,
        destination_owner_type: "MERCHANT",
        merchant_normalized: "Google Play",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "Google Play",
        description_normalized: isCancellation
          ? "Google Play Subscription Cancellation"
          : "Google Play Declined/Rejected Notice",
        transaction_reference: null,
        external_order_id: orderId,
        confidence: 0.99,
        recommended_action:
          "Keep as lifecycle evidence; zero ledger mutation",
        review_reason: isCancellation
          ? "Subscription cancellation notice; no financial charge."
          : "Declined/rejected Google Play event; no completed charge.",
        evidence_role: "LIFECYCLE_STATUS",
        candidate: null,
      };
    }

    const amount = parseIndonesianAmount(bodyText) || 0;

    if (amount === 0) {
      return {
        event_id: orderId
          ? `gplay_lifecycle_${orderId}_${lifecycleKey}`
          : `gplay_lifecycle_${lifecycleKey}`,
        occurred_at_wib: occurredAtWib,
        status: "Ignored",
        event_kind: "NON_TRANSACTION",
        financial_class: "Ignore",
        financial_direction: "Neutral",
        amount: 0,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: null,
        destination_account_alias: null,
        destination_owner_type: "MERCHANT",
        merchant_normalized: "Google Play",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "Google Play",
        description_normalized:
          "Google Play Non-charge Lifecycle Notice",
        transaction_reference: null,
        external_order_id: orderId,
        confidence: 0.95,
        recommended_action:
          "Keep as lifecycle evidence; zero ledger mutation",
        review_reason:
          "Google Play notice without a financial charge.",
        evidence_role: "LIFECYCLE_STATUS",
        candidate: null,
      };
    }

    const cand: ParsedCandidate = {
      tx_type: "Expense",
      amount,
      account: "BCA Main",
      to_account: null,
      category: "Subscriptions",
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: 0.95,
      reasons:
        `Google Play: Pembelian aplikasi / langganan digital (${orderId || "Order"}).`,
      status: "AutoApproved",
    };

    return {
      event_id: orderId
        ? `gplay_${orderId}`
        : `gplay_${Date.now()}`,
      occurred_at_wib: occurredAtWib,
      status: "AutoApproved",
      event_kind: "DIGITAL_PURCHASE",
      financial_class: "Expense",
      financial_direction: "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: "BCA Main",
      destination_account_alias: null,
      destination_owner_type: "MERCHANT",
      merchant_normalized: "Google Play",
      merchant_pan: null,
      merchant_location: "Digital",
      counterparty_normalized: "Google Play",
      description_normalized: "Google Play Digital Purchase",
      transaction_reference: orderId,
      external_order_id: orderId,
      confidence: 0.95,
      recommended_action: "Record Digital Expense",
      review_reason: null,
      evidence_role: "PRIMARY_PAYMENT",
      candidate: cand,
    };
  }

  // -----------------------------------------------------------------------
  // 5. by.U (noreply@byu.id / noreply@cx.byu.id)
  // -----------------------------------------------------------------------
  if (cleanFrom.includes("byu.id")) {
    if (cleanFrom.includes("cx.byu.id") || cleanBody.includes("kuota kamu habis") || cleanBody.includes("kuota aktif")) {
      return {
        event_id: `byu_cx_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "Ignored",
        event_kind: "NON_TRANSACTION",
        financial_class: "Ignore",
        financial_direction: "Neutral",
        amount: 0,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: null,
        destination_account_alias: null,
        destination_owner_type: "UNKNOWN",
        merchant_normalized: "by.U",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "by.U",
        description_normalized: "by.U Quota/Lifecycle Notification",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Ignore lifecycle notice",
        review_reason: "Lifecycle quota event; zero payment charge.",
        evidence_role: "LIFECYCLE_STATUS",
        candidate: null,
      };
    }

    const amount = parseIndonesianAmount(bodyText) || 0;
    const orderMatch = bodyText.match(/(?:no\.?\s*pesanan|order id)\s*[:]?\s*([a-zA-Z0-9]+)/i);
    const orderId = orderMatch ? orderMatch[1] : null;

    const cand: ParsedCandidate = {
      tx_type: "Expense",
      amount,
      account: "BCA Main",
      to_account: null,
      category: "Phone & Internet",
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: 0.95,
      reasons: `by.U: Pembayaran paket data / pulsa berhasil (${orderId || "Receipt"}).`,
      status: "AutoApproved",
    };

    return {
      event_id: orderId ? `byu_${orderId}` : `byu_${Date.now()}`,
      occurred_at_wib: occurredAtWib,
      status: "AutoApproved",
      event_kind: "DIGITAL_PURCHASE",
      financial_class: "Expense",
      financial_direction: "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: "BCA Main",
      destination_account_alias: null,
      destination_owner_type: "MERCHANT",
      merchant_normalized: "by.U (Telkomsel)",
      merchant_pan: null,
      merchant_location: "Digital",
      counterparty_normalized: "by.U",
      description_normalized: "Pembelian Paket by.U",
      transaction_reference: orderId,
      external_order_id: orderId,
      confidence: 0.95,
      recommended_action: "Record Telecom Expense",
      review_reason: null,
      evidence_role: "PRIMARY_PAYMENT",
      candidate: cand,
    };
  }

  // -----------------------------------------------------------------------
  // 6. ESB E-Receipt (no-reply@mailer-esb.com)
  // -----------------------------------------------------------------------
  if (cleanFrom.includes("mailer-esb.com")) {
    const amount = parseIndonesianAmount(bodyText) || 0;
    const orderMatch = bodyText.match(/(?:order id|receipt no|no struk)\s*[:]?\s*([a-zA-Z0-9\-_]+)/i);
    const orderId = orderMatch ? orderMatch[1] : null;
    const mMatch = bodyText.match(/(?:merchant|resto|outlet|store)\s*[:]?\s*([a-zA-Z0-9\s\.,\-_]+)/i);
    const merchant = mMatch ? mMatch[1].trim() : "ESB Restaurant";

    return {
      event_id: orderId ? `esb_${orderId}` : `esb_${Date.now()}`,
      occurred_at_wib: occurredAtWib,
      status: "Approved",
      event_kind: "INVOICE_EVIDENCE",
      financial_class: "Expense",
      financial_direction: "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: null,
      destination_account_alias: null,
      destination_owner_type: "MERCHANT",
      merchant_normalized: merchant,
      merchant_pan: null,
      merchant_location: null,
      counterparty_normalized: merchant,
      description_normalized: `Struk Digital ESB: ${merchant}`,
      transaction_reference: orderId,
      external_order_id: orderId,
      confidence: 0.90,
      recommended_action: "Correlate with bank debit evidence",
      review_reason: null,
      evidence_role: "SECONDARY_RECEIPT",
      candidate: null, // Secondary evidence: enriches bank debit
    };
  }

  // -----------------------------------------------------------------------
  // 7. SHOPEE (info@shopee.co.id / info@mail.shopee.co.id)
  // -----------------------------------------------------------------------
  if (cleanFrom.includes("shopee.co.id")) {
    // Shipping / delivery status -> NON_TRANSACTION
    if (cleanSubj.includes("dikirim") || cleanSubj.includes("dalam perjalanan") || cleanSubj.includes("pesanan diterima") || cleanBody.includes("sudahkah menerima")) {
      return {
        event_id: `shopee_ship_${Date.now()}`,
        occurred_at_wib: occurredAtWib,
        status: "Ignored",
        event_kind: "NON_TRANSACTION",
        financial_class: "Ignore",
        financial_direction: "Neutral",
        amount: 0,
        currency: "IDR",
        fee_amount: 0,
        source_account_alias: null,
        destination_account_alias: null,
        destination_owner_type: "MERCHANT",
        merchant_normalized: "Shopee",
        merchant_pan: null,
        merchant_location: null,
        counterparty_normalized: "Shopee",
        description_normalized: "Shopee Shipping/Delivery Status",
        transaction_reference: null,
        external_order_id: null,
        confidence: 0.95,
        recommended_action: "Ignore non-invoice update",
        review_reason: "Shipping lifecycle update; zero financial transaction.",
        evidence_role: "LIFECYCLE_STATUS",
        candidate: null,
      };
    }

    // Invoice evidence
    const amount = parseIndonesianAmount(bodyText) || 0;
    const orderMatch = bodyText.match(/(?:no\.?\s*pesanan|order id|no invoice)\s*[:]?\s*([a-zA-Z0-9]+)/i);
    const orderId = orderMatch ? orderMatch[1] : null;

    return {
      event_id: orderId ? `shopee_inv_${orderId}` : `shopee_inv_${Date.now()}`,
      occurred_at_wib: occurredAtWib,
      status: "Approved",
      event_kind: "INVOICE_EVIDENCE",
      financial_class: "Expense",
      financial_direction: "Debit",
      amount,
      currency: "IDR",
      fee_amount: 0,
      source_account_alias: null,
      destination_account_alias: null,
      destination_owner_type: "MERCHANT",
      merchant_normalized: "Shopee",
      merchant_pan: null,
      merchant_location: null,
      counterparty_normalized: "Shopee",
      description_normalized: `Faktur Shopee (${orderId || "Invoice"})`,
      transaction_reference: orderId,
      external_order_id: orderId,
      confidence: 0.90,
      recommended_action: "Correlate with bank/wallet payment",
      review_reason: null,
      evidence_role: "INVOICE",
      candidate: null, // Secondary evidence: enriches payment
    };
  }

  // -----------------------------------------------------------------------
  // Default / Unknown Fallback
  // -----------------------------------------------------------------------
  const amount = parseIndonesianAmount(bodyText) || 0;
  return {
    event_id: `unk_${Date.now()}`,
    occurred_at_wib: occurredAtWib,
    status: "Pending",
    event_kind: "NON_TRANSACTION",
    financial_class: "Pending Review",
    financial_direction: "Neutral",
    amount,
    currency: "IDR",
    fee_amount: 0,
    source_account_alias: "BCA Main",
    destination_account_alias: null,
    destination_owner_type: "UNKNOWN",
    merchant_normalized: null,
    merchant_pan: null,
    merchant_location: null,
    counterparty_normalized: "Unknown Sender",
    description_normalized: "Unknown Email Transaction",
    transaction_reference: null,
    external_order_id: null,
    confidence: 0.20,
    recommended_action: "Review manually",
    review_reason: "Untrusted or unrecognized sender format.",
    evidence_role: "PRIMARY_PAYMENT",
    candidate: {
      tx_type: "Expense",
      amount,
      account: "BCA Main",
      to_account: null,
      category: "Other / Miscellaneous",
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: 0.20,
      reasons: "Unknown sender format. Requires manual review.",
      status: "Pending",
    },
  };
}

// Backward-compatible adapter for existing caller endpoints
export function parseGmailNotification(
  subject: string,
  bodyText: string,
  fromAddress: string,
  occurredAt?: string
): ParsedCandidate {
  const ev = parseGmailIntelligence(subject, bodyText, fromAddress, occurredAt);
  if (ev.candidate) {
    return ev.candidate;
  }
  const wib = getWibDate(occurredAt ? new Date(occurredAt) : new Date());
  return {
    tx_type: "Expense",
    amount: ev.amount,
    account: ev.source_account_alias || "BCA Main",
    to_account: ev.destination_account_alias,
    category: ev.financial_class === "Expense" ? "Other / Miscellaneous" : ev.financial_class,
    money_context: "Personal",
    person_name: ev.counterparty_normalized,
    date: wib.dateStr,
    time: wib.timeStr,
    confidence_score: ev.confidence,
    reasons: ev.review_reason || ev.recommended_action,
    status: ev.status === "AutoApproved" ? "AutoApproved" : ev.status === "Ignored" ? "Ignored" : "Pending",
  };
}

export function parseTelegramText(text: string, referenceDate?: string): ParsedCandidate {
  const wib = getWibDate(referenceDate ? new Date(referenceDate) : new Date());
  const clean = text.trim();

  // Pattern 1: Transfer "transfer 100000 BCA Main ke Jago" or "tf 50k bca ke jago"
  const tfMatch = clean.match(/(?:transfer|tf)\s+([\d\.,kK]+)\s+(?:dari\s+)?([a-zA-Z0-9\s:]+?)\s+ke\s+([a-zA-Z0-9\s:]+)/i);
  if (tfMatch) {
    let amtStr = tfMatch[1].toLowerCase().replace("k", "000");
    const amount = parseIndonesianAmount(amtStr) || 0;
    const fromAcc = normalizeAccountName(tfMatch[2].trim());
    const toAcc = normalizeAccountName(tfMatch[3].trim());

    return {
      tx_type: "Transfer",
      amount,
      account: fromAcc,
      to_account: toAcc,
      category: "Other / Miscellaneous",
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: amount > 0 ? 0.95 : 0.4,
      reasons: "Telegram: Internal transfer command.",
      status: amount > 0 ? "AutoApproved" : "Pending",
    };
  }

  // Pattern 2: Expense "keluar 12000 dari ShopeePay untuk makan" / "bayar 25000 pakai BCA untuk bensin"
  const expMatch = clean.match(/(?:keluar|bayar|beli)\s+([\d\.,kK]+)\s+(?:dari|pakai|via)\s+([a-zA-Z0-9\s:]+?)\s+(?:untuk|buat)\s+(.+)/i);
  if (expMatch) {
    let amtStr = expMatch[1].toLowerCase().replace("k", "000");
    const amount = parseIndonesianAmount(amtStr) || 0;
    const account = normalizeAccountName(expMatch[2].trim());
    const purpose = expMatch[3].trim();
    const category = inferMerchantCategory("", purpose);

    return {
      tx_type: "Expense",
      amount,
      account,
      to_account: null,
      category,
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: amount > 0 ? 0.95 : 0.4,
      reasons: "Telegram: Structured expense command.",
      status: amount > 0 ? "AutoApproved" : "Pending",
    };
  }

  // Pattern 3: Income "masuk 500000 ke BCA dari Allan untuk gaji"
  const incMatch = clean.match(/(?:masuk|dapat|terima)\s+([\d\.,kK]+)\s+(?:ke|di)\s+([a-zA-Z0-9\s:]+?)(?:\s+dari\s+([a-zA-Z0-9\s]+?))?(?:\s+(?:untuk|buat)\s+(.+))?$/i);
  if (incMatch) {
    let amtStr = incMatch[1].toLowerCase().replace("k", "000");
    const amount = parseIndonesianAmount(amtStr) || 0;
    const account = normalizeAccountName(incMatch[2].trim());
    const person = incMatch[3] ? incMatch[3].trim() : null;
    const purpose = incMatch[4] ? incMatch[4].trim() : "";

    return {
      tx_type: "Income",
      amount,
      account,
      to_account: null,
      category: purpose ? inferMerchantCategory("", purpose) : "Other / Miscellaneous",
      money_context: "Personal",
      person_name: person,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: amount > 0 ? 0.90 : 0.4,
      reasons: "Telegram: Structured income command.",
      status: amount > 0 ? "AutoApproved" : "Pending",
    };
  }

  // Ambiguous / Unstructured
  const rawAmt = parseIndonesianAmount(clean) || 0;
  return {
    tx_type: "Expense",
    amount: rawAmt,
    account: "BCA Main",
    to_account: null,
    category: "Other / Miscellaneous",
    money_context: "Personal",
    person_name: null,
    date: wib.dateStr,
    time: wib.timeStr,
    confidence_score: 0.3,
    reasons: "Telegram: Ambiguous unstructured text. Requires manual review.",
    status: "Pending",
  };
}

export function evaluateAutoApproval(candidate: ParsedCandidate, senderAllowed: boolean): "AutoApproved" | "Pending" | "Ignored" {
  if (candidate.status === "Ignored") return "Ignored";
  if (!senderAllowed) return "Pending";
  if (candidate.confidence_score < 0.85) return "Pending";
  if (!candidate.amount || candidate.amount <= 0) return "Pending";
  if (!candidate.account || !candidate.date) return "Pending";
  if (candidate.money_context !== "Personal") return "Pending";
  if (candidate.tx_type === "Transfer" && !candidate.to_account) return "Pending";
  return "AutoApproved";
}
