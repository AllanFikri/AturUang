// domain.ts: Modul domain kalkulasi finansial AturUang untuk D1 dan Parser Ingestion
import { Env } from "./auth";

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

  let totalXeff = 0.0;
  for (const u of upcomings.results || []) {
    if (u.status === "Tentative" && Number(u.reserve_now || 0) === 0) {
      continue; // Tentative without reserve_now does not deduct
    }
    const covRes = await db
      .prepare("SELECT COALESCE(SUM(amount), 0.0) as cov FROM protected_allocations WHERE covers_upcoming_id = ? AND status = 'Active'")
      .bind(u.id)
      .first<{ cov: number }>();
    const cov = Number(covRes?.cov || 0);
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
      `SELECT name, kind, type, current_balance, balance_date, last_reconciled_at, active
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
  status: "Pending" | "AutoApproved";
}

export function parseIndonesianAmount(text: string): number | null {
  const match = text.match(/(?:Rp\.?|IDR)?\s*([\d\.,]+)/i);
  if (!match) return null;

  let raw = match[1].trim();
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

export function parseGmailNotification(
  subject: string,
  bodyText: string,
  fromAddress: string,
  occurredAt?: string
): ParsedCandidate {
  const wib = getWibDate(occurredAt ? new Date(occurredAt) : new Date());
  const cleanSubject = (subject || "").toLowerCase();
  const cleanBody = (bodyText || "").toLowerCase();
  const cleanFrom = (fromAddress || "").toLowerCase();

  // 1. BCA Adapter
  if (cleanFrom.includes("bca") || cleanSubject.includes("bca") || cleanBody.includes("m-bca")) {
    let tx_type: "Income" | "Expense" | "Transfer" = "Expense";
    let amount = parseIndonesianAmount(bodyText) || parseIndonesianAmount(subject) || 0;
    let account = "BCA Main";
    let to_account: string | null = null;
    let category = "Other / Miscellaneous";
    let confidence = 0.95;
    let reasons = "BCA Alert: parsed standard transaction notification.";

    if (cleanBody.includes("transfer ke") || cleanBody.includes("debit") || cleanBody.includes("qris")) {
      tx_type = "Expense";
      if (cleanBody.includes("qris") || cleanBody.includes("resto") || cleanBody.includes("makan") || cleanBody.includes("cafe")) {
        category = "Main Meals";
      } else if (cleanBody.includes("spbu") || cleanBody.includes("pertamina") || cleanBody.includes("shell")) {
        category = "Fuel";
      }
    } else if (cleanBody.includes("transfer dari") || cleanBody.includes("kredit") || cleanBody.includes("masuk")) {
      tx_type = "Income";
      category = "Other / Miscellaneous";
    }

    if (amount <= 0) {
      confidence = 0.4;
      reasons = "BCA Alert: could not extract exact amount.";
    }

    const autoStatus = confidence >= 0.85 ? "AutoApproved" : "Pending";

    return {
      tx_type,
      amount,
      account,
      to_account,
      category,
      money_context: "Personal",
      person_name: null,
      date: wib.dateStr,
      time: wib.timeStr,
      confidence_score: confidence,
      reasons,
      status: autoStatus,
    };
  }

  // 2. Generic / Unknown Adapter
  const amount = parseIndonesianAmount(bodyText) || 0;
  return {
    tx_type: cleanBody.includes("masuk") ? "Income" : "Expense",
    amount,
    account: "BCA Main",
    to_account: null,
    category: "Other / Miscellaneous",
    money_context: "Personal",
    person_name: null,
    date: wib.dateStr,
    time: wib.timeStr,
    confidence_score: 0.4,
    reasons: "Unknown sender. Requires manual review.",
    status: "Pending",
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
    const category = inferCategory(expMatch[3].trim());

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

  // Pattern 3: Income "masuk 500000 ke BCA dari bonus"
  const incMatch = clean.match(/(?:masuk|terima|dapat)\s+([\d\.,kK]+)\s+(?:ke|di)\s+([a-zA-Z0-9\s:]+?)(?:\s+(?:dari|untuk)\s+(.+))?$/i);
  if (incMatch) {
    let amtStr = incMatch[1].toLowerCase().replace("k", "000");
    const amount = parseIndonesianAmount(amtStr) || 0;
    const account = normalizeAccountName(incMatch[2].trim());

    return {
      tx_type: "Income",
      amount,
      account,
      to_account: null,
      category: "Other / Miscellaneous",
      money_context: "Personal",
      person_name: null,
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

function normalizeAccountName(name: string): string {
  const n = name.toLowerCase();
  if (n.includes("shopee")) return "ShopeePay";
  if (n.includes("gopay")) return "GoPay";
  if (n.includes("jago")) return "Jago Main";
  if (n.includes("bca poket") || n.includes("tabungan")) return "BCA Poket: Tabungan";
  if (n.includes("bca")) return "BCA Main";
  if (n.includes("cash") || n.includes("tunai")) return "Cash";
  return name;
}

function inferCategory(text: string): string {
  const t = text.toLowerCase();
  if (t.includes("makan") || t.includes("lunch") || t.includes("dinner") || t.includes("sarapan")) return "Main Meals";
  if (t.includes("snack") || t.includes("jajan") || t.includes("cemilan")) return "Snacks";
  if (t.includes("kopi") || t.includes("cafe") || t.includes("minum")) return "Cafe & Drinks";
  if (t.includes("bensin") || t.includes("pertalite") || t.includes("pertamax")) return "Fuel";
  if (t.includes("parkir") || t.includes("tol")) return "Parking/Toll";
  if (t.includes("pulsa") || t.includes("kuota") || t.includes("internet")) return "Phone & Internet";
  if (t.includes("langganan") || t.includes("subscription")) return "Subscriptions";
  if (t.includes("obat") || t.includes("dokter") || t.includes("klinik")) return "Health";
  return "Other / Miscellaneous";
}

export function evaluateAutoApproval(candidate: ParsedCandidate, senderAllowed: boolean): "AutoApproved" | "Pending" {
  if (!senderAllowed) return "Pending";
  if (candidate.confidence_score < 0.85) return "Pending";
  if (!candidate.amount || candidate.amount <= 0) return "Pending";
  if (!candidate.account || !candidate.date) return "Pending";
  if (candidate.money_context !== "Personal") return "Pending";
  if (candidate.tx_type === "Transfer" && !candidate.to_account) return "Pending";
  return "AutoApproved";
}
