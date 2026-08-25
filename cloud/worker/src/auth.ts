// auth.ts: Otentikasi, Proteksi Akses Staging, HMAC Gmail Relay, dan Telegram Webhook
export interface Env {
  DB: D1Database;
  MODE?: string;
  STAGING_ADMIN_TOKEN?: string;
  GMAIL_RELAY_SECRET?: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_SECRET_TOKEN?: string;
  TELEGRAM_ALLOWED_USER_ID?: string;
}

export function getSecurityHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Signature, X-Timestamp, X-Nonce, X-Telegram-Bot-Api-Secret-Token, X-Requested-With",
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

// In-memory nonce cache for anti-replay (cleaned periodically)
const seenNonces = new Map<string, number>();

export async function verifyGmailHmac(
  request: Request,
  rawBody: string,
  secret: string
): Promise<{ valid: boolean; error?: string }> {
  const signature = request.headers.get("X-Signature") || "";
  const timestampStr = request.headers.get("X-Timestamp") || "";
  const nonce = request.headers.get("X-Nonce") || "";

  if (!signature || !timestampStr || !nonce) {
    return { valid: false, error: "MISSING_HMAC_HEADERS" };
  }

  const timestamp = parseInt(timestampStr, 10);
  if (isNaN(timestamp)) {
    return { valid: false, error: "INVALID_TIMESTAMP" };
  }

  const now = Date.now();
  // 5 minutes (300,000 ms) window tolerance
  if (Math.abs(now - timestamp) > 300000) {
    return { valid: false, error: "EXPIRED_TIMESTAMP" };
  }

  // Check nonce replay
  if (seenNonces.has(nonce)) {
    return { valid: false, error: "NONCE_REPLAY" };
  }

  // Purge old nonces (>10 min)
  for (const [n, ts] of seenNonces.entries()) {
    if (now - ts > 600000) {
      seenNonces.delete(n);
    }
  }
  seenNonces.set(nonce, now);

  // Compute HMAC-SHA256
  const payloadToSign = `${timestampStr}.${nonce}.${rawBody}`;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );

  const sigBuffer = await crypto.subtle.sign("HMAC", key, enc.encode(payloadToSign));
  const hashArray = Array.from(new Uint8Array(sigBuffer));
  const expectedSig = hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");

  if (!constantTimeEqual(signature.toLowerCase(), expectedSig.toLowerCase())) {
    return { valid: false, error: "INVALID_SIGNATURE" };
  }

  return { valid: true };
}

export function verifyTelegramWebhook(
  request: Request,
  expectedSecretToken?: string
): { valid: boolean; error?: string } {
  if (!expectedSecretToken) {
    // If not configured in test, pass
    return { valid: true };
  }
  const headerToken = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
  if (!headerToken || !constantTimeEqual(headerToken, expectedSecretToken)) {
    return { valid: false, error: "INVALID_TELEGRAM_SECRET" };
  }
  return { valid: true };
}

export function isTelegramUserAllowed(
  userId: number | string | undefined,
  allowedUserId?: string
): boolean {
  if (!allowedUserId) {
    return true; // No restriction set
  }
  if (userId === undefined || userId === null) {
    return false;
  }
  return String(userId).trim() === String(allowedUserId).trim();
}
