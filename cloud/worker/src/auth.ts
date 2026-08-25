// auth.ts: Otentikasi dan Proteksi Akses Staging
export interface Env {
  DB: D1Database;
  MODE?: string;
  STAGING_ADMIN_TOKEN?: string;
}

export function getSecurityHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
  };
}

export function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    return false;
  }
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

export function authenticateRequest(request: Request, env: Env): Response | null {
  const authHeader = request.headers.get("Authorization") || "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.substring(7).trim() : "";

  const expectedToken = env.STAGING_ADMIN_TOKEN || "aturuang-staging-secret-key-default";

  if (!token || !constantTimeEqual(token, expectedToken)) {
    return new Response(
      JSON.stringify({
        status: "error",
        code: "UNAUTHORIZED",
        message: "Akses ditolak: token otentikasi staging salah atau belum disertakan.",
      }),
      {
        status: 401,
        headers: getSecurityHeaders(),
      }
    );
  }

  return null;
}
