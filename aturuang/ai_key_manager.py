"""
Money Tracks V12 — Secure Gemini API Key Manager
Implements OS Credential Manager (keyring) with env var fallback.
Strictly prevents key leaks in frontend, SQLite, git, or logs.
"""
from __future__ import annotations

import os
import json
import urllib.request
import urllib.error
import sqlite3
from pathlib import Path

SERVICE_NAME = "MoneyTracks"
ACCOUNT_NAME = "gemini_api_key"
FALLBACK_SECRET_PATH = Path.home() / ".money_tracks" / "secrets.json"

def _get_keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None

def get_gemini_api_key() -> str:
    """Retrieves Gemini API Key with documented precedence and conflict detection.

    Precedence:
    1. OS Credential Manager (Windows Keyring)
    2. Environment Variable GEMINI_API_KEY
    3. Secure isolated local file fallback (~/.money_tracks/secrets.json)
    """
    file_val = ""
    if FALLBACK_SECRET_PATH.exists():
        try:
            data = json.loads(FALLBACK_SECRET_PATH.read_text(encoding="utf-8"))
            file_val = data.get("gemini_api_key", "").strip()
        except Exception:
            pass

    env_val = os.getenv("GEMINI_API_KEY", "").strip()

    keyring_val = ""
    kr = _get_keyring()
    if kr:
        try:
            kv = kr.get_password(SERVICE_NAME, ACCOUNT_NAME)
            if kv and kv.strip():
                keyring_val = kv.strip()
        except Exception:
            pass

    # Conflict detection across configured stores
    configured = [v for v in (keyring_val, env_val, file_val) if v]
    if len(set(configured)) > 1:
        import sys
        sys.stderr.write("[KeyManager] WARNING: Conflicting Gemini API keys found across secret stores. Failing closed.\n")
        return ""

    return keyring_val or env_val or file_val


BRIDGE_ACCOUNT_NAME = "android_bridge_secret"


def get_bridge_secret(db_path: Path | str | None = None) -> str:
    """Retrieves Android Bridge shared secret with documented precedence and conflict detection.

    Precedence:
    1. OS Credential Manager (Keyring)
    2. Environment Variable ATURUANG_ANDROID_BRIDGE_SECRET
    3. Secure isolated local file fallback (~/.money_tracks/secrets.json)
    """
    file_val = ""
    if FALLBACK_SECRET_PATH.exists():
        try:
            data = json.loads(FALLBACK_SECRET_PATH.read_text(encoding="utf-8"))
            file_val = data.get("android_bridge_secret", "").strip()
        except Exception:
            pass

    env_val = os.getenv("ATURUANG_ANDROID_BRIDGE_SECRET", "").strip()

    keyring_val = ""
    kr = _get_keyring()
    if kr:
        try:
            kv = kr.get_password(SERVICE_NAME, BRIDGE_ACCOUNT_NAME)
            if kv and kv.strip():
                keyring_val = kv.strip()
        except Exception:
            pass

    # Conflict detection across configured stores
    configured = [v for v in (keyring_val, env_val, file_val) if v]
    if len(set(configured)) > 1:
        import sys
        sys.stderr.write("[KeyManager] WARNING: Conflicting Android bridge secrets found across secret stores. Failing closed.\n")
        return ""

    return keyring_val or env_val or file_val

def save_gemini_api_key(key: str, con: sqlite3.Connection | None = None) -> bool:
    """Saves API Key securely into OS Credential Manager and purges any DB plain text key."""
    clean_key = key.strip()
    if not clean_key:
        raise ValueError("API Key tidak boleh kosong")

    # Purge any legacy key from SQLite settings table for security
    if con:
        try:
            con.execute("DELETE FROM settings WHERE key='gemini_api_key'")
            con.commit()
        except Exception:
            pass

    # Save to Windows Keyring
    kr = _get_keyring()
    saved = False
    if kr:
        try:
            kr.set_password(SERVICE_NAME, ACCOUNT_NAME, clean_key)
            saved = True
        except Exception as e:
            print(f"[KeyManager] Keyring set_password warning: {e}")

    # Fallback to isolated user profile file
    try:
        FALLBACK_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_SECRET_PATH.write_text(json.dumps({"gemini_api_key": clean_key}), encoding="utf-8")
        saved = True
    except Exception as e:
        print(f"[KeyManager] Fallback secret store error: {e}")

    return saved

def delete_gemini_api_key(con: sqlite3.Connection | None = None) -> bool:
    """Removes API Key from OS Keyring, fallback store, and SQLite database."""
    # 1. Remove from SQLite settings table
    if con:
        try:
            con.execute("DELETE FROM settings WHERE key='gemini_api_key'")
            con.commit()
        except Exception:
            pass

    # 2. Remove from OS Keyring
    kr = _get_keyring()
    if kr:
        try:
            kr.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception:
            pass

    # 3. Remove fallback file
    if FALLBACK_SECRET_PATH.exists():
        try:
            FALLBACK_SECRET_PATH.unlink()
        except Exception:
            pass

    return True

def get_ai_status() -> dict:
    """Returns safe AI configuration status without leaking actual API key."""
    key = get_gemini_api_key()
    configured = bool(key)
    return {
        "configured": configured,
        "provider": "gemini",
        "model": "gemini-flash-latest",
        "key_preview": "••••••••••••••••" if configured else "Belum dikonfigurasi",
    }

def test_gemini_connection(key: str | None = None) -> dict:
    """Performs an actual live API test call to Gemini and classifies errors accurately."""
    api_key = (key or get_gemini_api_key()).strip()
    if not api_key:
        return {
            "status": "error",
            "error_code": 401,
            "message": "Kunci API belum dikonfigurasi. Silakan masukkan kunci API terlebih dahulu.",
        }

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "Test connection. Reply 'OK'."}]}
        ]
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-goog-api-key": api_key,
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                return {
                    "status": "success",
                    "message": "Koneksi Google Gemini API Berhasil!",
                    "model": "gemini-flash-latest",
                }
            return {
                "status": "error",
                "error_code": 500,
                "message": "Respons dari Gemini API kosong.",
            }
    except urllib.error.HTTPError as err:
        code = err.code
        if code == 401:
            msg = "Autentikasi gagal (HTTP 401): Kunci API tidak valid atau telah dicabut."
        elif code == 403:
            msg = "Izin ditolak (HTTP 403): Kunci API tidak memiliki akses ke Gemini API."
        elif code == 404:
            msg = "Model/Resource tidak ditemukan (HTTP 404): Endpoint gemini-flash-latest tidak ada."
        elif code == 429:
            msg = "Batas kuota terlampaui (HTTP 429): Rate limit Gemini API tercapai."
        elif code >= 500:
            msg = f"Gangguan server Google (HTTP {code}): Layanan Gemini sedang bermasalah."
        else:
            msg = f"Gagal terhubung ke Gemini API (HTTP {code}: {err.reason})."
        return {"status": "error", "error_code": code, "message": msg}
    except urllib.error.URLError as err:
        return {
            "status": "error",
            "error_code": 0,
            "message": f"Koneksi jaringan gagal/offline: {err.reason}",
        }
    except Exception as e:
        return {
            "status": "error",
            "error_code": 500,
            "message": f"Terjadi kesalahan tidak terduga: {e}",
        }
