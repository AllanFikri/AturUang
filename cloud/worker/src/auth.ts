// auth.ts: Otentikasi dan Proteksi Keamanan Fail-Closed untuk Cloudflare Worker
export interface Env {
  DB: D1Database;
  MODE?: string;
  STAGING_ADMIN_TOKEN?: string;
  GMAIL_RELAY_SECRET?: string;
  REPAIR_SECRET?: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_SECRET_TOKEN?: string;
  TELEGRAM_ALLOWED_USER_ID?: string;
}

export const MAX_HMAC_BODY_BYTES = 2 * 1024 * 1024; // 2MB strict bound
export const MAX_CLOCK_SKEW_MS = 300000; // 5 minutes strict window

export function getSecurityHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
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
  const expectedToken = (env.STAGING_ADMIN_TOKEN || "").trim();
  if (!expectedToken) {
    // Fail closed: Token tidak dikonfigurasi pada environment
    return new Response(
      JSON.stringify({
        status: "error",
        code: "UNAUTHORIZED",
        message: "Akses ditolak: otentikasi staging belum dikonfigurasi atau tidak valid.",
      }),
      {
        status: 401,
        headers: getSecurityHeaders(),
      }
    );
  }

  const authHeader = request.headers.get("Authorization") || "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.substring(7).trim() : "";

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

// =========================================================================
// GMAIL SENDER TRUST REGISTRY (STRICT & EVIDENCE-BACKED)
// =========================================================================
export const TRUSTED_PRIMARY_GMAIL_SENDERS = new Set<string>([
  "bca@bca.co.id",
  "noreply@jago.com",
  "no-reply@flip.id",
  "googleplay-noreply@google.com",
  "noreply@byu.id",
  "no-reply@mailer-esb.com",
]);

export const TRUSTED_SECONDARY_GMAIL_SENDERS = new Set<string>([
  "info@shopee.co.id",
  "info@mail.shopee.co.id",
  "noreply@cx.byu.id",
  "noreply@stockbit.com",
  "contactus@stockbit.com",
]);

export function extractCleanEmailAddress(fromHeader: string): string {
  if (!fromHeader) return "";
  const match = fromHeader.match(/<([^>]+)>/);
  const raw = match ? match[1] : fromHeader;
  return raw.trim().toLowerCase();
}

export function isGmailSenderTrusted(fromHeader: string): { trusted: boolean; email: string; isPrimary: boolean } {
  const email = extractCleanEmailAddress(fromHeader);
  if (!email) return { trusted: false, email: "", isPrimary: false };
  const isPrimary = TRUSTED_PRIMARY_GMAIL_SENDERS.has(email);
  const isSecondary = TRUSTED_SECONDARY_GMAIL_SENDERS.has(email);
  return {
    trusted: isPrimary || isSecondary,
    email,
    isPrimary,
  };
}

export async function verifyGmailHmac(
  request: Request,
  rawBody: string,
  secret?: string,
  db?: D1Database,
  missingSecretErrorCode: string = "UNCONFIGURED_GMAIL_SECRET"
): Promise<{ valid: boolean; error?: string }> {
  // 1. Validasi body size
  if (typeof rawBody === "string" && rawBody.length > MAX_HMAC_BODY_BYTES) {
    return { valid: false, error: "OVERSIZED_PAYLOAD" };
  }

  // 2. Validasi secret
  const cleanSecret = (secret || "").trim();
  if (!cleanSecret) {
    return { valid: false, error: missingSecretErrorCode };
  }

  // 3. Validasi required headers
  const signature = request.headers.get("X-Signature") || "";
  const timestampStr = request.headers.get("X-Timestamp") || "";
  const nonce = request.headers.get("X-Nonce") || "";

  if (!signature || !timestampStr || !nonce) {
    return { valid: false, error: "MISSING_HMAC_HEADERS" };
  }

  // 4. Validasi nonce format
  if (nonce.length < 8 || nonce.length > 64 || !/^[A-Za-z0-9_-]+$/.test(nonce)) {
    return { valid: false, error: "INVALID_NONCE" };
  }

  // 5. Validasi timestamp / tolerance window (5 menit, past and future)
  const timestamp = parseInt(timestampStr, 10);
  if (isNaN(timestamp) || !Number.isInteger(timestamp)) {
    return { valid: false, error: "INVALID_TIMESTAMP" };
  }

  const now = Date.now();
  if (timestamp < now - MAX_CLOCK_SKEW_MS) {
    return { valid: false, error: "EXPIRED_TIMESTAMP" };
  }
  if (timestamp > now + MAX_CLOCK_SKEW_MS) {
    return { valid: false, error: "FUTURE_TIMESTAMP" };
  }

  // 4. Hitung expected HMAC-SHA256
  const payloadToSign = `${timestampStr}.${nonce}.${rawBody}`;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(cleanSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );

  const sigBuffer = await crypto.subtle.sign("HMAC", key, enc.encode(payloadToSign));
  const hashArray = Array.from(new Uint8Array(sigBuffer));
  const expectedSig = hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");

  // 5. Constant-time compare signature SEBELUM menyentuh atau mereserve nonce
  if (!constantTimeEqual(signature.toLowerCase(), expectedSig.toLowerCase())) {
    return { valid: false, error: "INVALID_SIGNATURE" };
  }

  // 6. HANYA setelah signature terverifikasi valid, lakukan reservasi nonce secara durable di D1
  if (!db) {
    // Database tidak tersedia -> fail closed (REPLAY_GUARD_UNAVAILABLE)
    return { valid: false, error: "REPLAY_GUARD_UNAVAILABLE" };
  }

  try {
    // Auto-prune nonce lama (>10 menit)
    const pruneCutoff = now - 600000;
    await db.prepare("DELETE FROM gmail_replay_nonces WHERE timestamp < ?").bind(pruneCutoff).run();

    // Insert new nonce (PRIMARY KEY constraint mencegah replay lintas instance)
    await db.prepare(
      "INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)"
    ).bind(nonce, timestamp).run();
  } catch (err: any) {
    const errMsg = String(err?.message || "").toUpperCase();
    if (errMsg.includes("UNIQUE") || errMsg.includes("PRIMARY KEY") || errMsg.includes("CONSTRAINT")) {
      return { valid: false, error: "NONCE_REPLAY" };
    }
    // Database failure / connection error DILARANG dilabeli NONCE_REPLAY
    return { valid: false, error: "REPLAY_GUARD_UNAVAILABLE" };
  }

  return { valid: true };
}

export function verifyTelegramWebhook(
  request: Request,
  expectedSecretToken?: string
): { valid: boolean; error?: string } {
  const cleanExpected = (expectedSecretToken || "").trim();
  if (!cleanExpected) {
    // Fail-closed: Jika secret belum diisi, tolak seluruh webhook
    return { valid: false, error: "UNCONFIGURED_TELEGRAM_SECRET" };
  }
  const headerToken = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "").trim();
  if (!headerToken || !constantTimeEqual(headerToken, cleanExpected)) {
    return { valid: false, error: "INVALID_TELEGRAM_SECRET" };
  }
  return { valid: true };
}

export function isTelegramUserAllowed(
  userId: number | string | undefined,
  allowedUserId?: string
): boolean {
  if (!allowedUserId) {
    // Fail-closed: Jika allowlist belum diisi, tolak seluruh user
    return false;
  }
  if (userId === undefined || userId === null) {
    return false;
  }
  return String(userId).trim() === String(allowedUserId).trim();
}
