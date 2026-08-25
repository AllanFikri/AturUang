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
): Promise<{ status: string; calculated_balance: number; anchor_balance: number; anchor_date: string }> {
  const targetDate = asOfDate || getWibDate().dateStr;

  // 1. Get latest anchor snapshot
  const snapRes = await db
    .prepare(
      `SELECT snapshot_date, snapshot_time, balance, snapshot_kind
       FROM balance_snapshots
       WHERE account_name = ? AND snapshot_date <= ? AND is_anchor = 1
       ORDER BY snapshot_date DESC, snapshot_time DESC LIMIT 1`
    )
    .bind(accountName, targetDate)
    .first<{ snapshot_date: string; snapshot_time: string; balance: number; snapshot_kind: string }>();

  if (!snapRes) {
    return {
      status: "unverifiable",
      calculated_balance: 0.0,
      anchor_balance: 0.0,
      anchor_date: "",
    };
  }

  const anchorDate = snapRes.snapshot_date;
  const anchorTime = snapRes.snapshot_time || "00:00:00";
  const anchorBal = Number(snapRes.balance || 0);

  // 2. Sum delta since anchor
  const sumRes = await db
    .prepare(
      `SELECT
         COALESCE(SUM(CASE
           WHEN to_account = ? THEN amount
           WHEN account = ? AND transaction_type = 'Income' THEN amount
           WHEN account = ? AND transaction_type = 'Expense' THEN -amount
           WHEN account = ? AND transaction_type = 'Transfer' THEN -amount
           ELSE 0
         END), 0) as delta
       FROM transactions
       WHERE (account = ? OR to_account = ?)
         AND is_deleted = 0
         AND (date > ? OR (date = ? AND time > ?))
         AND date <= ?`
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
      targetDate
    )
    .first<{ delta: number }>();

  const delta = Number(sumRes?.delta || 0);
  const calculated = round2(anchorBal + delta);

  return {
    status: "verified",
    calculated_balance: calculated,
    anchor_balance: anchorBal,
    anchor_date: anchorDate,
  };
}

export async function computeDashboardKpis(
  db: D1Database,
  monthStr?: string
): Promise<{
  safeToSpend: number;
  totalBalance: number;
  protectedSavings: number;
  passThroughOutstanding: number;
  currentCommitments: number;
  goalsAllocated: number;
  periodStart: string;
  periodEnd: string;
}> {
  const curMonth = monthStr || getWibDate().monthStr;
  const periodStart = `${curMonth}-01`;
  const periodEnd = `${curMonth}-31`;

  // 1. Total Liquid Balance
  const balRes = await db
    .prepare("SELECT COALESCE(SUM(current_balance), 0) as total FROM accounts WHERE active = 1 AND kind = 'Owned'")
    .first<{ total: number }>();
  const totalBalance = round2(Number(balRes?.total || 0));

  // 2. Dana Darurat & Goals Allocated
  const goalsRes = await db
    .prepare("SELECT COALESCE(SUM(allocated_amount), 0) as total_goals FROM allocation_goals")
    .first<{ total_goals: number }>();
  const goalsAllocated = round2(Number(goalsRes?.total_goals || 0));

  // 3. Protected Savings
  const protRes = await db
    .prepare("SELECT COALESCE(SUM(amount), 0) as total_prot FROM protected_allocations WHERE status = 'Active'")
    .first<{ total_prot: number }>();
  const protectedSavings = round2(Number(protRes?.total_prot || 0) + goalsAllocated);

  // 4. Pass-through outstanding
  const ptRes = await db
    .prepare(
      `SELECT
         COALESCE(SUM(CASE WHEN transaction_type = 'Income' THEN amount ELSE -amount END), 0) as pt_net
       FROM transactions
       WHERE money_context = 'Pass-through' AND is_deleted = 0`
    )
    .first<{ pt_net: number }>();
  const passThroughOutstanding = round2(Math.max(0, Number(ptRes?.pt_net || 0)));

  // 5. Confirmed Commitments (Upcoming active)
  const commRes = await db
    .prepare(
      `SELECT COALESCE(SUM(amount), 0) as total_comm
       FROM upcoming
       WHERE status = 'Upcoming' AND due_date <= ?`
    )
    .bind(periodEnd)
    .first<{ total_comm: number }>();
  const currentCommitments = round2(Number(commRes?.total_comm || 0));

  // 6. Safe to Spend
  const safeToSpend = round2(totalBalance - protectedSavings - passThroughOutstanding - currentCommitments);

  return {
    safeToSpend,
    totalBalance,
    protectedSavings,
    passThroughOutstanding,
    currentCommitments,
    goalsAllocated,
    periodStart,
    periodEnd,
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
  // Matches Rp 50.000,00 or Rp. 120.000 or IDR 75,000 or 50.000
  const match = text.match(/(?:Rp\.?|IDR)?\s*([\d\.,]+)/i);
  if (!match) return null;

  let raw = match[1].trim();
  // If ends with ,00 or ,50 (Indonesian cents)
  if (/,\d{2}$/.test(raw)) {
    raw = raw.replace(/\./g, "").replace(",", ".");
  } else if (/\.\d{2}$/.test(raw)) {
    raw = raw.replace(/,/g, "");
  } else {
    // Treat dots and commas as thousand separators
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
    reasons: `Unknown sender '${fromAddress}'. Requires manual review.`,
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
