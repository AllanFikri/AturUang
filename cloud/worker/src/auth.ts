// auth.ts: Otentikasi dan Proteksi Keamanan Fail-Closed untuk Cloudflare Worker
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
  const expectedToken = env.STAGING_ADMIN_TOKEN;
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

export async function verifyGmailHmac(
  request: Request,
  rawBody: string,
  secret?: string,
  db?: D1Database
): Promise<{ valid: boolean; error?: string }> {
  // 1. Validasi secret
  if (!secret) {
    return { valid: false, error: "UNCONFIGURED_GMAIL_SECRET" };
  }

  // 2. Validasi required headers
  const signature = request.headers.get("X-Signature") || "";
  const timestampStr = request.headers.get("X-Timestamp") || "";
  const nonce = request.headers.get("X-Nonce") || "";

  if (!signature || !timestampStr || !nonce) {
    return { valid: false, error: "MISSING_HMAC_HEADERS" };
  }

  // 3. Validasi timestamp / tolerance window (5 menit)
  const timestamp = parseInt(timestampStr, 10);
  if (isNaN(timestamp)) {
    return { valid: false, error: "INVALID_TIMESTAMP" };
  }

  const now = Date.now();
  if (Math.abs(now - timestamp) > 300000) {
    return { valid: false, error: "EXPIRED_TIMESTAMP" };
  }

  // 4. Hitung expected HMAC-SHA256
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

  // 5. Constant-time compare signature SEBELUM menyentuh atau mereserve nonce
  if (!constantTimeEqual(signature.toLowerCase(), expectedSig.toLowerCase())) {
    return { valid: false, error: "INVALID_SIGNATURE" };
  }

  // 6. HANYA setelah signature terverifikasi valid, lakukan reservasi nonce secara durable di D1
  if (db) {
    try {
      // Auto-prune nonce lama (>10 menit)
      const pruneCutoff = now - 600000;
      await db.prepare("DELETE FROM gmail_replay_nonces WHERE timestamp < ?").bind(pruneCutoff).run();

      // Insert new nonce (PRIMARY KEY constraint mencegah replay lintas instance)
      await db.prepare(
        "INSERT INTO gmail_replay_nonces (nonce, timestamp) VALUES (?, ?)"
      ).bind(nonce, timestamp).run();
    } catch (err: any) {
      if (err.message && err.message.includes("UNIQUE")) {
        return { valid: false, error: "NONCE_REPLAY" };
      }
      return { valid: false, error: "NONCE_REPLAY" };
    }
  }

  return { valid: true };
}

export function verifyTelegramWebhook(
  request: Request,
  expectedSecretToken?: string
): { valid: boolean; error?: string } {
  if (!expectedSecretToken) {
    // Fail-closed: Jika secret belum diisi, tolak seluruh webhook
    return { valid: false, error: "UNCONFIGURED_TELEGRAM_SECRET" };
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
    // Fail-closed: Jika allowlist belum diisi, tolak seluruh user
    return false;
  }
  if (userId === undefined || userId === null) {
    return false;
  }
  return String(userId).trim() === String(allowedUserId).trim();
}
