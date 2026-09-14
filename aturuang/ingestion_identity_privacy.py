"""
Identity privacy and deterministic keyed protection for universal ingestion.

Provides HMAC-SHA-256 deterministic account identity protection.
Protects raw account identifiers into versioned, domain-separated,
canonical fingerprints that prevent raw account leaks while maintaining
stable identity across ingestion runs.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Sequence


DEFAULT_ACCOUNT_DOMAIN = "aturuang:account:v1"
MIN_SECRET_BYTES = 32


def canonical_encoding(*parts: str | bytes) -> bytes:
    """
    Encode parts into an unambiguous length-prefixed canonical byte sequence.

    Each part is framed as f"{len(bytes)}:{bytes}".
    Parts are joined with a pipe delimiter b"|".
    This guarantees collision-resistance across arbitrary inputs.
    """
    framed_parts: list[bytes] = []
    for part in parts:
        if isinstance(part, str):
            b = part.encode("utf-8")
        elif isinstance(part, (bytes, bytearray)):
            b = bytes(part)
        else:
            b = str(part).encode("utf-8")
        prefix = f"{len(b)}:".encode("ascii")
        framed_parts.append(prefix + b)
    return b"|".join(framed_parts)


class AccountIdentityProtector:
    """
    Injectable deterministic identity protector using HMAC-SHA-256.

    Guarantees:
    - Caller must supply secret explicitly (min 32 bytes).
    - No hard-coded, default, or env-read secrets.
    - Domain separation and key versioning.
    - Canonical length-prefixed encoding.
    - Constant-time comparison helper.
    - Secret is masked in __repr__ and never leaked in exceptions.
    """

    def __init__(
        self,
        secret: str | bytes,
        key_version: int = 1,
        domain: str = DEFAULT_ACCOUNT_DOMAIN,
    ) -> None:
        if secret is None:
            raise ValueError("Secret must be provided explicitly")
        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        elif isinstance(secret, (bytes, bytearray)):
            secret_bytes = bytes(secret)
        else:
            raise TypeError("Secret must be str or bytes")

        if len(secret_bytes) < MIN_SECRET_BYTES:
            raise ValueError(
                f"Secret is too weak: must be at least {MIN_SECRET_BYTES} bytes"
            )

        if not isinstance(key_version, int) or key_version < 1:
            raise ValueError("key_version must be a positive integer >= 1")

        if not domain or not isinstance(domain, str) or not domain.strip():
            raise ValueError("domain must be a non-empty string")

        self._secret_bytes: bytes = secret_bytes
        self._key_version: int = key_version
        self._domain: str = domain.strip()

    @property
    def key_version(self) -> int:
        return self._key_version

    @property
    def domain(self) -> str:
        return self._domain

    def protect_account_key(
        self,
        institution_id: str,
        source_registry_id: str,
        account_type: str | Any,
        raw_key: str,
        parent_protected_key: str | None = None,
    ) -> str:
        """
        Compute deterministic protected account key.

        Returns string formatted as 'v{key_version}:{hex_digest}'.
        """
        if not institution_id or not str(institution_id).strip():
            raise ValueError("institution_id must not be empty")
        if not source_registry_id or not str(source_registry_id).strip():
            raise ValueError("source_registry_id must not be empty")
        if not account_type or not str(account_type).strip():
            raise ValueError("account_type must not be empty")
        if not raw_key or not str(raw_key).strip():
            raise ValueError("raw_key must not be empty")

        inst_str = str(institution_id).strip()
        src_str = str(source_registry_id).strip()
        type_str = str(getattr(account_type, "value", account_type)).strip()
        raw_str = str(raw_key).strip()
        parent_str = str(parent_protected_key).strip() if parent_protected_key else ""

        encoded_message = canonical_encoding(
            self._domain,
            f"v{self._key_version}",
            inst_str,
            src_str,
            type_str,
            raw_str,
            parent_str,
        )

        digest = hmac.new(
            self._secret_bytes,
            encoded_message,
            hashlib.sha256,
        ).hexdigest().lower()

        return f"v{self._key_version}:{digest}"

    @staticmethod
    def constant_time_equals(a: str, b: str) -> bool:
        """Constant-time direct comparison of protected strings."""
        if not isinstance(a, str) or not isinstance(b, str):
            return False
        return hmac.compare_digest(a, b)

    def __repr__(self) -> str:
        return (
            f"AccountIdentityProtector("
            f"key_version={self._key_version}, "
            f"domain={self._domain!r}, "
            f"secret='***')"
        )
