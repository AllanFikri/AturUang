"""
AturUang Stage 10 — Android Notification Evidence Bridge.

Provides:
1. Strict package allowlist enforcement.
2. Per-device HMAC-SHA256 signature verification & nonce replay guard.
3. Timestamp drift bounds verification (default: 300 seconds).
4. Deterministic Indonesian banking notification parsing with Decimal precision.
5. Zero Direct Ledger Mutation: Stages notifications into the Visual Review Queue.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from aturuang.review_queue_ui import ReviewItem, ReviewQueueManager
from aturuang.safe_apply import ApplyCandidate, CandidateLifecycleState, LedgerMutation

ALLOWED_PACKAGES: frozenset[str] = frozenset({
    "com.bca",
    "mybca",
    "com.bca.mybca",
    "com.gojek.app",
    "com.gopay.app",
    "com.seabank.mobile",
    "com.jago.app",
    "com.shopee.id",
})


class AndroidBridgeError(Exception):
    """Base exception for Android Bridge operations."""


class DisallowedPackageError(AndroidBridgeError):
    """Raised when notification arrives from an unlisted package."""


class HMACVerificationError(AndroidBridgeError):
    """Raised when HMAC-SHA256 signature validation fails."""


class TimestampExpiredError(AndroidBridgeError):
    """Raised when request timestamp drift exceeds maximum tolerance."""


class NonceReplayError(AndroidBridgeError):
    """Raised when a nonce has already been processed."""


class MalformedNotificationError(AndroidBridgeError):
    """Raised when notification payload structure is incomplete or invalid."""


def compute_hmac_signature(
    device_secret: str,
    device_id: str,
    timestamp: int | str,
    nonce: str,
    package_name: str,
    title: str,
    text: str,
) -> str:
    """Computes HMAC-SHA256 signature over inbound notification fields."""
    payload_str = f"{device_id}:{timestamp}:{nonce}:{package_name}:{title}:{text}"
    return hmac.new(
        device_secret.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def parse_indonesian_amount(text: str) -> Decimal | None:
    """Extracts Indonesian currency amount (e.g. 'Rp 150.000,00' or 'Rp. 25.000') into Decimal."""
    match = re.search(r"Rp\.?\s*([0-9\.\,]+)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"(?:sebesar|nominal|jumlah)\s*:?\s*([0-9\.\,]+)", text, re.IGNORECASE)
        if not match:
            return None

    raw_val = match.group(1).strip()
    # Normalize Indonesian format (dots as thousand separators, comma as decimal separator)
    if "," in raw_val and "." in raw_val:
        if raw_val.rfind(",") > raw_val.rfind("."):
            clean = raw_val.replace(".", "").replace(",", ".")
        else:
            clean = raw_val.replace(",", "")
    elif "," in raw_val:
        parts = raw_val.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            clean = parts[0] + "." + parts[1]
        else:
            clean = raw_val.replace(",", "")
    elif "." in raw_val:
        parts = raw_val.split(".")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            clean = raw_val
        else:
            clean = raw_val.replace(".", "")
    else:
        clean = raw_val

    try:
        dec = Decimal(clean)
        return dec.quantize(Decimal("0.01"))
    except Exception:
        return None


@dataclass
class ParsedNotification:
    package_name: str
    account: str
    transaction_type: str
    amount: Decimal
    account_from: str
    account_to: str
    description: str
    category: str
    date: str
    time: str


class AndroidNotificationBridge:
    """Ingestion bridge for Android notification evidence, HMAC auth, and review staging."""

    def __init__(
        self,
        db_path: Path | str,
        review_manager: ReviewQueueManager | None = None,
        default_device_secret: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.review_manager = review_manager or ReviewQueueManager(self.db_path)
        self.device_secrets: dict[str, str] = {}

        # Priority: explicit param -> ATURUANG_ANDROID_BRIDGE_SECRET env var -> credential store
        resolved_secret = default_device_secret or os.getenv("ATURUANG_ANDROID_BRIDGE_SECRET")
        if not resolved_secret:
            try:
                from aturuang.ai_key_manager import get_bridge_secret
                resolved_secret = get_bridge_secret(self.db_path) or None
            except Exception:
                resolved_secret = None

        self.default_device_secret: str | None = resolved_secret
        self._nonce_cache: set[str] = set()
        self.max_drift_seconds: int = 300  # 5 minutes
        self._init_nonce_store()

    def _init_nonce_store(self) -> None:
        """Initializes persistent SQLite nonce replay store with TTL index."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS android_bridge_nonces (
                        nonce_key TEXT PRIMARY KEY,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_nonces_created_at ON android_bridge_nonces(created_at)"
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def _is_nonce_seen(self, nonce_key: str, current_time: int) -> bool:
        """Checks if nonce was processed within TTL window across restarts."""
        if nonce_key in self._nonce_cache:
            return True
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                cutoff = current_time - self.max_drift_seconds
                conn.execute("DELETE FROM android_bridge_nonces WHERE created_at < ?", (cutoff,))
                conn.commit()
                row = conn.execute(
                    "SELECT 1 FROM android_bridge_nonces WHERE nonce_key = ?",
                    (nonce_key,),
                ).fetchone()
                if row:
                    self._nonce_cache.add(nonce_key)
                    return True
            finally:
                conn.close()
        except Exception:
            pass
        return False

    def _record_nonce(self, nonce_key: str, current_time: int) -> None:
        """Persists nonce to SQLite table and local cache."""
        self._nonce_cache.add(nonce_key)
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO android_bridge_nonces (nonce_key, created_at) VALUES (?, ?)",
                    (nonce_key, current_time),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def register_device(self, device_id: str, secret: str) -> None:
        """Registers an authorized Android device and its shared HMAC secret."""
        self.device_secrets[device_id] = secret

    def get_device_secret(self, device_id: str) -> str:
        """Returns the shared secret for a registered device, or fallback to configured secret."""
        sec = self.device_secrets.get(device_id) or self.default_device_secret or os.getenv("ATURUANG_ANDROID_BRIDGE_SECRET")
        if not sec:
            raise HMACVerificationError(f"No shared secret configured for device '{device_id}' (fail-closed)")
        return sec

    def verify_payload(
        self,
        payload: dict[str, Any],
        current_time: int | None = None,
    ) -> tuple[bool, str, str]:
        """Validates payload structure, package allowlist, timestamp drift, nonce uniqueness, and HMAC signature."""
        required_fields = ("device_id", "timestamp", "nonce", "package_name", "title", "text", "signature")
        for f in required_fields:
            if f not in payload or payload[f] is None:
                return False, f"Missing required field: {f}", "MALFORMED_PAYLOAD"

        device_id = str(payload["device_id"]).strip()
        pkg = str(payload["package_name"]).strip()
        title = str(payload["title"]).strip()
        text = str(payload["text"]).strip()
        nonce = str(payload["nonce"]).strip()
        sig = str(payload["signature"]).strip()

        # 1. Package Allowlist check
        if pkg not in ALLOWED_PACKAGES:
            return False, f"Package '{pkg}' not permitted", "DISALLOWED_PACKAGE"

        # 2. Timestamp drift check
        try:
            ts = int(payload["timestamp"])
        except (ValueError, TypeError):
            return False, "Invalid timestamp format", "MALFORMED_PAYLOAD"

        now = current_time if current_time is not None else int(time.time())
        if abs(now - ts) > self.max_drift_seconds:
            return False, f"Timestamp drift exceeds {self.max_drift_seconds}s", "EXPIRED_TIMESTAMP"

        # 3. Nonce replay check (Persistent SQLite & Memory Cache)
        nonce_key = f"{device_id}:{nonce}"
        if self._is_nonce_seen(nonce_key, now):
            return False, f"Nonce '{nonce}' has already been processed", "NONCE_REPLAYED"

        # 4. HMAC signature check (Fail-Closed)
        try:
            secret = self.get_device_secret(device_id)
        except HMACVerificationError as e:
            return False, str(e), "UNCONFIGURED_SECRET"

        expected_sig = compute_hmac_signature(secret, device_id, ts, nonce, pkg, title, text)
        if not hmac.compare_digest(expected_sig.lower(), sig.lower()):
            return False, "Invalid HMAC-SHA256 signature", "UNAUTHORIZED"

        # Mark nonce as processed persistently
        self._record_nonce(nonce_key, now)
        return True, "Valid", "OK"

    def parse_notification(
        self,
        package_name: str,
        title: str,
        text: str,
        timestamp: int | None = None,
    ) -> ParsedNotification:
        """Parses Indonesian bank notification text into structured amounts, accounts, and types."""
        account_name = "Cash"
        if "bca" in package_name:
            account_name = "BCA"
        elif "gojek" in package_name or "gopay" in package_name:
            account_name = "GoPay"
        elif "seabank" in package_name:
            account_name = "SeaBank"
        elif "jago" in package_name:
            account_name = "Bank Jago"
        elif "shopee" in package_name:
            account_name = "ShopeePay"

        amount = parse_indonesian_amount(f"{title} {text}")
        if amount is None:
            amount = Decimal("0.00")

        full_text_lower = f"{title} {text}".lower()

        income_tokens = (
            "transfer dari",
            "transfer dr",
            "transfer masuk",
            "dana masuk",
            "menerima",
            "uang masuk",
            "cr",
            "top up",
            "cashback",
            "diterima",
            "masuk sebesar",
            "bunga tabungan",
            "bunga",
            "telah cair",
            "cair ke",
            "kredit",
            "dapat transfer",
        )
        is_income = any(token in full_text_lower for token in income_tokens)

        if is_income:
            tx_type = "Income"
            account_from = ""
            account_to = account_name
            if "bunga" in full_text_lower:
                desc = f"Bunga tabungan {account_name}"
            elif "cashback" in full_text_lower:
                desc = f"Cashback {account_name}"
            else:
                sender_match = re.search(
                    r"(?:dari|dr)\s+([A-Za-z0-9\s\.\,\-]+?)(?:\s+(?:rp|sebesar|ke|berhasil|masuk|telah)|[\.\,]|$)",
                    full_text_lower,
                )
                desc = f"Transfer masuk dari {sender_match.group(1).title().strip()}" if sender_match else f"Dana masuk {account_name}"
        else:
            tx_type = "Expense"
            account_from = account_name
            account_to = "Merchant External"
            recip_match = re.search(
                r"(?:ke|untuk|di|pada|kepada)\s+([A-Za-z0-9\s\.\,\-]+?)(?:\s+(?:rp|sebesar|dari|berhasil|menggunakan)|[\.\,]|$)",
                full_text_lower,
            )
            if not recip_match:
                recip_match = re.search(
                    r"membayar\s+([A-Za-z0-9\s\.\,\-]+?)\s+(?:sebesar|rp)",
                    full_text_lower,
                )
            if recip_match:
                party = recip_match.group(1).title().strip()
                if "transfer" in full_text_lower or party.lower().startswith("rekening"):
                    desc = f"Transfer ke {party}"
                elif " di " in f" {full_text_lower} ":
                    desc = f"Pembayaran di {party}"
                else:
                    desc = f"Pembayaran ke {party}"
            else:
                desc = f"Transaksi {account_name}"

        if timestamp:
            dt_obj = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
            date_str = dt_obj.strftime("%Y-%m-%d")
            time_str = dt_obj.strftime("%H:%M:%S")
        else:
            now = dt.datetime.now(dt.timezone.utc)
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")

        return ParsedNotification(
            package_name=package_name,
            account=account_name,
            transaction_type=tx_type,
            amount=amount,
            account_from=account_from,
            account_to=account_to,
            description=desc,
            category="Uncategorized",
            date=date_str,
            time=time_str,
        )

    def ingest_notification(
        self,
        payload: dict[str, Any],
        current_time: int | None = None,
    ) -> dict[str, Any]:
        """Validates payload, parses evidence, stages candidate into Review Queue with ZERO ledger writes."""
        valid, msg, err_code = self.verify_payload(payload, current_time=current_time)
        if not valid:
            return {
                "success": False,
                "error": msg,
                "error_code": err_code,
            }

        parsed = self.parse_notification(
            package_name=payload["package_name"],
            title=payload["title"],
            text=payload["text"],
            timestamp=payload.get("timestamp"),
        )

        item_id = f"notif_{payload['device_id'][:6]}_{payload['nonce'][:8]}"
        mutation = LedgerMutation(
            date=parsed.date,
            time=parsed.time,
            transaction_type=parsed.transaction_type,
            amount=parsed.amount,
            account_from=parsed.account_from,
            account_to=parsed.account_to,
            description=parsed.description,
            category=parsed.category,
            canonical_id=f"NOTIF_{payload['device_id']}_{payload['nonce']}",
            source_refs=f"android:{payload['package_name']}:{payload['nonce']}",
        )

        candidate = ApplyCandidate(
            candidate_id=f"CAND_{item_id}",
            idempotency_key=f"IDEM_NOTIF_{payload['device_id']}_{payload['nonce']}",
            state=CandidateLifecycleState.PARSED,
            participating_evidence_keys=(f"notif:{payload['nonce']}",),
            mutations=(mutation,),
        )

        review_item = ReviewItem(
            item_id=item_id,
            batch_id="android_notification_bridge",
            date=parsed.date,
            time=parsed.time,
            transaction_type=parsed.transaction_type,
            amount=parsed.amount,
            account_from=parsed.account_from,
            account_to=parsed.account_to,
            description=parsed.description,
            category=parsed.category,
            status="REVIEW_REQUIRED",
            reason=f"Android Notification from {payload['package_name']}",
            candidate=candidate,
        )

        self.review_manager.add_item(review_item)

        return {
            "success": True,
            "item_id": item_id,
            "candidate_id": candidate.candidate_id,
            "preview_hash": candidate.preview_hash,
            "status": "STAGED_FOR_REVIEW",
            "amount": f"{parsed.amount:.2f}",
            "transaction_type": parsed.transaction_type,
            "account_from": parsed.account_from,
            "account_to": parsed.account_to,
            "description": parsed.description,
            "package_name": payload["package_name"],
        }
