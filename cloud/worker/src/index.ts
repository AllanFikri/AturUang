// index.ts: Cloudflare Worker Entrypoint untuk AturUang (Shadow Mode)
import { Env, authenticateRequest, getSecurityHeaders } from "./auth";
import {
  getWibDate,
  getDashboard,
  getAccountsList,
  getTransactionsList,
  getGoalsSummary,
  getUpcomingList,
  getDebtsList,
  reconstructBalance,
  getInsightsSummary,
} from "./domain";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    // CORS Preflight
    if (method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: getSecurityHeaders(),
      });
    }

    // 1. Public Health Check
    if (path === "/health") {
      const { dateStr, timeStr } = getWibDate();
      let schemaVer = 0;
      try {
        const verRow = await env.DB.prepare(
          "SELECT MAX(version) as ver FROM schema_migrations"
        ).first<{ ver: number }>();
        schemaVer = verRow?.ver || 0;
      } catch {
        schemaVer = 1;
      }

      return new Response(
        JSON.stringify({
          status: "ok",
          mode: env.MODE || "shadow",
          schema_version: schemaVer,
          time_wib: `${dateStr} ${timeStr} WIB`,
          system: "Cloudflare Worker aturuang-api",
        }),
        {
          status: 200,
          headers: getSecurityHeaders(),
        }
      );
    }

    // 2. Authentication Enforcement for Staging
    const authError = authenticateRequest(request, env);
    if (authError) {
      return authError;
    }

    // 3. Shadow Mode Write Route Protection
    const isShadow = (env.MODE || "shadow").toLowerCase() === "shadow";
    if (isShadow && method !== "GET" && method !== "HEAD") {
      return new Response(
        JSON.stringify({
          status: "error",
          code: "SHADOW_READ_ONLY",
          message:
            "Backend Cloudflare D1 beroperasi dalam mode shadow read-only. Seluruh mutasi tulis ditolak untuk menjaga integritas.",
        }),
        {
          status: 503,
          headers: getSecurityHeaders(),
        }
      );
    }

    // 4. API Routing
    try {
      if (path === "/api/dashboard") {
        const month = url.searchParams.get("month") || undefined;
        const data = await getDashboard(env.DB, month);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/accounts") {
        const data = await getAccountsList(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/transactions") {
        const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 500);
        const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10), 0);
        const month = url.searchParams.get("month") || undefined;
        const data = await getTransactionsList(env.DB, limit, offset, month);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/goals") {
        const data = await getGoalsSummary(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/upcoming") {
        const data = await getUpcomingList(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/debts") {
        const data = await getDebtsList(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/reconstruct-balance") {
        const account = url.searchParams.get("account") || "";
        const asOf = url.searchParams.get("as_of") || undefined;
        if (!account) {
          return new Response(
            JSON.stringify({ status: "error", message: "Parameter account diperlukan." }),
            { status: 400, headers: getSecurityHeaders() }
          );
        }
        const data = await reconstructBalance(env.DB, account, asOf);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/insights" || path === "/api/insights/recurring" || path === "/api/accounts/freshness") {
        const asOf = url.searchParams.get("as_of") || undefined;
        const data = await getInsightsSummary(env.DB, asOf);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      // 404 Route Not Found
      return new Response(
        JSON.stringify({
          status: "error",
          code: "NOT_FOUND",
          message: `Endpoint '${path}' tidak ditemukan.`,
        }),
        {
          status: 404,
          headers: getSecurityHeaders(),
        }
      );
    } catch (err: any) {
      return new Response(
        JSON.stringify({
          status: "error",
          code: "INTERNAL_SERVER_ERROR",
          message: "Terjadi kesalahan internal pada layanan backend cloud.",
        }),
        {
          status: 500,
          headers: getSecurityHeaders(),
        }
      );
    }
  },
};
