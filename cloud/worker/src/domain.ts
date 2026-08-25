// domain.ts: Modul domain kalkulasi finansial AturUang untuk D1
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
): Promise<{ status: string; calculated_balance: number; anchor_balance: number; anchor_date: string }> {
  const targetDate = asOfDate || getWibDate().dateStr;

  // 1. Get latest anchor snapshot
  const snapRes = await db
    .prepare(
      `SELECT snapshot_date, snapshot_time, balance, snapshot_kind 
       FROM balance_snapshots 
       WHERE account_name = ? AND snapshot_date <= ? AND snapshot_kind = 'anchor'
       ORDER BY snapshot_date DESC, snapshot_time DESC, id DESC LIMIT 1`
    )
    .bind(accountName, targetDate)
    .first<{ snapshot_date: string; snapshot_time: string; balance: number; snapshot_kind: string }>();

  let anchorDate = "1970-01-01";
  let anchorTime = "00:00:00";
  let anchorBal = 0.0;

  if (snapRes) {
    anchorDate = snapRes.snapshot_date;
    anchorTime = snapRes.snapshot_time || "00:00:00";
    anchorBal = Number(snapRes.balance || 0);
  }

  // 2. Sum mutations after anchor
  const mutRes = await db
    .prepare(
      `SELECT 
         COALESCE(SUM(CASE WHEN transaction_type = 'Income' AND account_to = ? THEN amount ELSE 0 END), 0) -
         COALESCE(SUM(CASE WHEN transaction_type = 'Expense' AND account_from = ? THEN amount ELSE 0 END), 0) +
         COALESCE(SUM(CASE WHEN transaction_type = 'Transfer' AND account_to = ? THEN amount 
                           WHEN transaction_type = 'Transfer' AND account_from = ? THEN -amount ELSE 0 END), 0) +
         COALESCE(SUM(CASE WHEN transaction_type = 'Adjustment' AND account_to = ? THEN amount 
                           WHEN transaction_type = 'Adjustment' AND account_from = ? THEN -amount ELSE 0 END), 0) as net_mutation
       FROM transactions
       WHERE is_deleted = 0 
         AND (date > ? OR (date = ? AND time > ?))
         AND date <= ?
         AND (account_from = ? OR account_to = ?)`
    )
    .bind(
      accountName,
      accountName,
      accountName,
      accountName,
      accountName,
      accountName,
      anchorDate,
      anchorDate,
      anchorTime,
      targetDate,
      accountName,
      accountName
    )
    .first<{ net_mutation: number }>();

  const netMutation = Number(mutRes?.net_mutation || 0);
  const calculatedBalance = round2(anchorBal + netMutation);

  return {
    status: snapRes ? "verified" : "unverifiable",
    calculated_balance: calculatedBalance,
    anchor_balance: anchorBal,
    anchor_date: anchorDate,
  };
}

export async function getDashboard(db: D1Database, monthParam?: string): Promise<Record<string, any>> {
  const { dateStr: todayStr, monthStr: currentMonth } = getWibDate();
  const selectedMonth = monthParam && /^\d{4}-\d{2}$/.test(monthParam) ? monthParam : currentMonth;

  // 1. Total liquid balance from active Owned accounts
  const liquidRes = await db
    .prepare(
      `SELECT COALESCE(SUM(current_balance), 0) as total_liquid
       FROM accounts WHERE active = 1 AND kind = 'Owned'`
    )
    .first<{ total_liquid: number }>();
  const totalLiquid = round2(Number(liquidRes?.total_liquid || 0));

  // 2. Goal & Emergency Fund Allocations
  const allocRes = await db
    .prepare(
      `SELECT COALESCE(SUM(allocated_amount), 0) as total_allocated,
              COALESCE(SUM(CASE WHEN name = 'Dana Darurat' THEN allocated_amount ELSE 0 END), 0) as emergency_fund
       FROM allocation_goals WHERE status = 'Active'`
    )
    .first<{ total_allocated: number; emergency_fund: number }>();
  const totalAllocated = round2(Number(allocRes?.total_allocated || 0));
  const emergencyFund = round2(Number(allocRes?.emergency_fund || 0));

  // 3. Upcoming Obligations (Confirmed vs Tentative)
  const upRes = await db
    .prepare(
      `SELECT 
         COALESCE(SUM(CASE WHEN status = 'Confirmed' THEN amount ELSE 0 END), 0) as confirmed_outflow,
         COALESCE(SUM(CASE WHEN status = 'Tentative' AND reserve_now = 1 THEN amount ELSE 0 END), 0) as tentative_reserved,
         COALESCE(SUM(CASE WHEN status = 'Tentative' AND reserve_now = 0 THEN amount ELSE 0 END), 0) as tentative_unreserved
       FROM upcoming WHERE status IN ('Upcoming', 'Confirmed', 'Tentative')`
    )
    .first<{ confirmed_outflow: number; tentative_reserved: number; tentative_unreserved: number }>();

  const confirmedObligations = round2(Number(upRes?.confirmed_outflow || 0));
  const tentativeReserved = round2(Number(upRes?.tentative_reserved || 0));
  const tentativeUnreserved = round2(Number(upRes?.tentative_unreserved || 0));

  // Safe-to-Spend (Dana Tersedia Saat Ini) Formula Prompt 11
  const safeToSpend = round2(totalLiquid - totalAllocated - confirmedObligations - tentativeReserved);

  // 4. Monthly Inflow & Outflow for selected month
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

  // Months available
  const monthsRows = await db
    .prepare(
      `SELECT DISTINCT substr(date, 1, 7) as m FROM transactions 
       WHERE is_deleted = 0 ORDER BY m DESC`
    )
    .all<{ m: string }>();
  const months = (monthsRows.results || []).map((r) => r.m);

  return {
    kpis: {
      safeToSpend: safeToSpend,
      totalBalance: totalLiquid,
      allocatedGoals: totalAllocated,
      emergencyFund: emergencyFund,
      confirmedObligations: confirmedObligations,
      tentativeReserved: tentativeReserved,
      tentativeUnreserved: tentativeUnreserved,
      monthIncome: monthIncome,
      monthExpense: monthExpense,
      netCashflow: netCashflow,
      selectedMonth: selectedMonth,
      asOfDate: todayStr,
    },
    months: months,
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
  const res = await db.prepare(`SELECT * FROM allocation_goals ORDER BY priority ASC, id ASC`).all();
  return {
    status: "success",
    goals: res.results || [],
  };
}

export async function getUpcomingList(db: D1Database): Promise<Record<string, any>> {
  const res = await db.prepare(`SELECT * FROM upcoming ORDER BY due_date ASC, id ASC`).all();
  return {
    status: "ok",
    upcoming: res.results || [],
  };
}

export async function getDebtsList(db: D1Database): Promise<Record<string, any>> {
  const debts = await db.prepare(`SELECT * FROM debts ORDER BY id ASC`).all();
  const events = await db.prepare(`SELECT * FROM debt_events ORDER BY event_date ASC, id ASC`).all();
  return {
    status: "success",
    debts: debts.results || [],
    events: events.results || [],
  };
}

export async function getInsightsSummary(db: D1Database, asOfDate?: string): Promise<Record<string, any>> {
  const targetDate = asOfDate || getWibDate().dateStr;

  // Source Freshness
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
