"""Offline identity cache for CONFIDEX booth QR login.

Purpose:
- Download offline-login bundle from the website while online.
- Store allowed users and public verification settings locally.
- Verify signed offline QR login tokens while the booth is offline.
- Save offline-login attempts locally.
- Sync saved attempts back to the website when internet returns.

Expected website bundle shape:
{
  "algorithm": "Ed25519",
  "tokenPrefix": "LOGIN-OFFLINE-v1.",
  "offlineLoginPublicKeyPemB64": "...",
  "users": [...]
}

Expected offline QR format:
LOGIN-OFFLINE-v1.<payload_b64url>.<signature_b64url>

The Raspberry Pi stores only the public key, not the private signing key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from backend.util import api_client


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "offline_identity.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()

DEFAULT_TOKEN_PREFIX = "LOGIN-OFFLINE-v1."
DEFAULT_ALGORITHM = "Ed25519"


# ============================================================
# Time / JSON helpers
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat()


def clean(value: Any) -> str:
    return str(value or "").strip()


def json_dump(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def json_load(value: Any, default: Any = None) -> Any:
    if default is None:
        default = {}

    try:
        data = json.loads(value or "{}")
        return data
    except Exception:
        return default


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = clean(value)

        if not text:
            return None

        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"

            # Numeric Unix timestamp support.
            if text.isdigit():
                dt = datetime.fromtimestamp(int(text), tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(text)
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def is_expired(value: Any) -> bool:
    dt = parse_dt(value)

    if dt is None:
        return False

    return utc_now() >= dt


def is_not_yet_valid(value: Any) -> bool:
    dt = parse_dt(value)

    if dt is None:
        return False

    return utc_now() < dt


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def b64url_decode(value: str) -> bytes:
    text = clean(value)

    missing_padding = len(text) % 4

    if missing_padding:
        text += "=" * (4 - missing_padding)

    return base64.urlsafe_b64decode(text.encode("utf-8"))


def b64_decode_pem(value: str) -> bytes:
    return base64.b64decode(clean(value).encode("utf-8"))


# ============================================================
# DB
# ============================================================

def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = connect_db()

        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS offline_identity_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS offline_users (
                    user_id TEXT PRIMARY KEY,
                    user_json TEXT NOT NULL DEFAULT '{}',
                    token_version TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    expires_at TEXT DEFAULT '',
                    bundle_version TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS offline_login_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    user_id TEXT DEFAULT '',
                    token_id TEXT DEFAULT '',
                    qr_hash TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    used_at TEXT NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT DEFAULT '',
                    sync_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_offline_login_attempts_synced
                ON offline_login_attempts(synced, used_at)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_offline_users_status
                ON offline_users(status, expires_at)
                """
            )

            conn.commit()

        finally:
            conn.close()


def set_setting(key: str, value: Any) -> None:
    init_db()

    with _lock:
        conn = connect_db()

        try:
            conn.execute(
                """
                INSERT INTO offline_identity_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (
                    clean(key),
                    json_dump(value) if isinstance(value, (dict, list)) else clean(value),
                    now_iso(),
                ),
            )

            conn.commit()

        finally:
            conn.close()


def get_setting(key: str, default: str = "") -> str:
    init_db()

    with _lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT value
                FROM offline_identity_settings
                WHERE key = ?
                """,
                (clean(key),),
            ).fetchone()

            if row is None:
                return default

            return clean(row["value"]) or default

        finally:
            conn.close()


def get_cache_counts() -> dict[str, int]:
    init_db()

    counts: dict[str, int] = {}

    with _lock:
        conn = connect_db()

        try:
            for table in [
                "offline_identity_settings",
                "offline_users",
                "offline_login_attempts",
            ]:
                try:
                    counts[table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                except Exception:
                    counts[table] = -1

        finally:
            conn.close()

    return counts


# ============================================================
# Bundle parsing
# ============================================================

def extract_public_key_b64(bundle: dict[str, Any]) -> str:
    return clean(
        bundle.get("offlineLoginPublicKeyPemB64")
        or bundle.get("offline_login_public_key_pem_b64")
        or bundle.get("publicKeyPemB64")
        or bundle.get("public_key_pem_b64")
        or bundle.get("publicKey")
        or bundle.get("public_key")
    )


def extract_token_prefix(bundle: dict[str, Any]) -> str:
    return clean(
        bundle.get("tokenPrefix")
        or bundle.get("token_prefix")
        or bundle.get("offlineLoginTokenPrefix")
        or bundle.get("offline_login_token_prefix")
        or DEFAULT_TOKEN_PREFIX
    )


def extract_algorithm(bundle: dict[str, Any]) -> str:
    return clean(
        bundle.get("algorithm")
        or bundle.get("alg")
        or DEFAULT_ALGORITHM
    )


def extract_bundle_version(bundle: dict[str, Any]) -> str:
    return clean(
        bundle.get("bundleVersion")
        or bundle.get("bundle_version")
        or bundle.get("version")
        or bundle.get("updatedAt")
        or bundle.get("updated_at")
        or now_iso()
    )


def extract_users(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    users = bundle.get("users")

    if isinstance(users, list):
        return [u for u in users if isinstance(u, dict)]

    identities = bundle.get("identities")

    if isinstance(identities, list):
        return [u for u in identities if isinstance(u, dict)]

    offline_users = bundle.get("offlineUsers") or bundle.get("offline_users")

    if isinstance(offline_users, list):
        return [u for u in offline_users if isinstance(u, dict)]

    return []


def extract_user_id(user: dict[str, Any]) -> str:
    if not isinstance(user, dict):
        return ""

    return clean(
        user.get("_id")
        or user.get("id")
        or user.get("user_id")
        or user.get("userId")
        or user.get("userID")
        or user.get("uid")
        or user.get("sub")
    )


def extract_token_version(user: dict[str, Any]) -> str:
    return clean(
        user.get("offlineLoginTokenVersion")
        or user.get("offline_login_token_version")
        or user.get("tokenVersion")
        or user.get("token_version")
        or user.get("version")
    )


def extract_user_status(user: dict[str, Any]) -> str:
    raw = clean(
        user.get("status")
        or user.get("accountStatus")
        or user.get("account_status")
        or "active"
    ).lower()

    if raw in {"inactive", "disabled", "blocked", "banned", "deleted"}:
        return raw

    return "active"


def extract_user_expires_at(user: dict[str, Any]) -> str:
    return clean(
        user.get("offlineLoginExpiresAt")
        or user.get("offline_login_expires_at")
        or user.get("expiresAt")
        or user.get("expires_at")
        or ""
    )


# ============================================================
# Public functions called by device_sync.py
# ============================================================

def sync_offline_identity_bundle() -> dict[str, Any]:
    """Download and cache the offline-login bundle from the website."""
    init_db()

    print("[OFFLINE ID] Requesting offline-login bundle from website...", flush=True)

    res = api_client.get_offline_login_bundle()

    if not getattr(res, "ok", False):
        result = {
            "ok": False,
            "status_code": getattr(res, "status_code", None),
            "error": getattr(res, "text", "")[:1000],
        }

        print(f"[OFFLINE ID] Bundle download failed: {result}", flush=True)
        return result

    try:
        bundle = res.json()
    except Exception as e:
        result = {
            "ok": False,
            "status_code": getattr(res, "status_code", None),
            "error": f"Invalid bundle JSON: {e}",
        }

        print(f"[OFFLINE ID] Bundle parse failed: {result}", flush=True)
        return result

    if not isinstance(bundle, dict):
        result = {
            "ok": False,
            "error": "Bundle response is not a JSON object.",
        }

        print(f"[OFFLINE ID] Bundle invalid: {result}", flush=True)
        return result

    algorithm = extract_algorithm(bundle)
    token_prefix = extract_token_prefix(bundle)
    public_key_b64 = extract_public_key_b64(bundle)
    bundle_version = extract_bundle_version(bundle)
    users = extract_users(bundle)

    if algorithm.lower() != "ed25519":
        result = {
            "ok": False,
            "error": f"Unsupported offline-login algorithm: {algorithm}",
        }

        print(f"[OFFLINE ID] Bundle rejected: {result}", flush=True)
        return result

    if not public_key_b64:
        result = {
            "ok": False,
            "error": "Bundle missing offlineLoginPublicKeyPemB64.",
        }

        print(f"[OFFLINE ID] Bundle rejected: {result}", flush=True)
        return result

    # Validate public key before saving.
    try:
        public_key_pem = b64_decode_pem(public_key_b64)
        serialization.load_pem_public_key(public_key_pem)
    except Exception as e:
        result = {
            "ok": False,
            "error": f"Invalid public key in bundle: {e}",
        }

        print(f"[OFFLINE ID] Bundle rejected: {result}", flush=True)
        return result

    saved_users = 0
    skipped_users = 0

    with _lock:
        conn = connect_db()

        try:
            now = now_iso()

            settings = {
                "algorithm": algorithm,
                "token_prefix": token_prefix,
                "offline_login_public_key_pem_b64": public_key_b64,
                "bundle_version": bundle_version,
                "bundle_synced_at": now,
            }

            for key, value in settings.items():
                conn.execute(
                    """
                    INSERT INTO offline_identity_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, clean(value), now),
                )

            for user in users:
                user_id = extract_user_id(user)

                if not user_id:
                    skipped_users += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO offline_users (
                        user_id,
                        user_json,
                        token_version,
                        status,
                        expires_at,
                        bundle_version,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        user_json = excluded.user_json,
                        token_version = excluded.token_version,
                        status = excluded.status,
                        expires_at = excluded.expires_at,
                        bundle_version = excluded.bundle_version,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_id,
                        json_dump(user),
                        extract_token_version(user),
                        extract_user_status(user),
                        extract_user_expires_at(user),
                        bundle_version,
                        now,
                    ),
                )

                saved_users += 1

            conn.commit()

        finally:
            conn.close()

    result = {
        "ok": True,
        "algorithm": algorithm,
        "token_prefix": token_prefix,
        "bundle_version": bundle_version,
        "users_saved": saved_users,
        "users_skipped": skipped_users,
        "db_path": str(DB_PATH),
        "counts": get_cache_counts(),
    }

    print(
        f"[OFFLINE ID] Synced offline login bundle: "
        f"users={saved_users}, skipped={skipped_users}, version={bundle_version}",
        flush=True,
    )

    return result


def sync_pending_offline_login_attempts(max_attempts: int = 50) -> dict[str, Any]:
    """Send locally saved offline-login attempts to the website."""
    init_db()

    with _lock:
        conn = connect_db()

        try:
            rows = conn.execute(
                """
                SELECT *
                FROM offline_login_attempts
                WHERE synced = 0
                ORDER BY used_at ASC
                LIMIT ?
                """,
                (int(max_attempts),),
            ).fetchall()

            attempts = [dict(row) for row in rows]

        finally:
            conn.close()

    if not attempts:
        result = {
            "ok": True,
            "pending": 0,
            "synced": 0,
            "message": "No pending offline login attempts.",
            "counts": get_cache_counts(),
        }

        print("[OFFLINE ID] No pending offline login attempts to sync.", flush=True)
        return result

    payload_attempts = []

    for item in attempts:
        payload_attempts.append(
            {
                "attempt_id": item.get("attempt_id"),
                "user_id": item.get("user_id"),
                "token_id": item.get("token_id"),
                "qr_hash": item.get("qr_hash"),
                "payload": json_load(item.get("payload_json"), default={}),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "used_at": item.get("used_at"),
                "created_at": item.get("created_at"),
            }
        )

    payload = {
        "attempts": payload_attempts,
        "source": "raspi_offline_identity_cache",
        "sent_at": now_iso(),
    }

    print(f"[OFFLINE ID] Syncing offline login attempts: count={len(payload_attempts)}", flush=True)

    try:
        res = api_client.post_offline_login_attempts(payload)
    except Exception as e:
        _mark_attempts_sync_failed(attempts, str(e))

        result = {
            "ok": False,
            "pending": len(attempts),
            "synced": 0,
            "error": str(e),
        }

        print(f"[OFFLINE ID] Offline login attempt sync failed: {result}", flush=True)
        return result

    if not getattr(res, "ok", False):
        error_text = getattr(res, "text", "")[:1000]
        _mark_attempts_sync_failed(attempts, error_text)

        result = {
            "ok": False,
            "status_code": getattr(res, "status_code", None),
            "pending": len(attempts),
            "synced": 0,
            "error": error_text,
        }

        print(f"[OFFLINE ID] Offline login attempt sync rejected: {result}", flush=True)
        return result

    synced_at = now_iso()

    with _lock:
        conn = connect_db()

        try:
            for item in attempts:
                conn.execute(
                    """
                    UPDATE offline_login_attempts
                    SET synced = 1,
                        synced_at = ?,
                        last_error = '',
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        synced_at,
                        synced_at,
                        item["attempt_id"],
                    ),
                )

            conn.commit()

        finally:
            conn.close()

    result = {
        "ok": True,
        "pending": len(attempts),
        "synced": len(attempts),
        "status_code": getattr(res, "status_code", None),
        "counts": get_cache_counts(),
    }

    print(f"[OFFLINE ID] Synced offline login attempts: synced={len(attempts)}", flush=True)
    return result


def _mark_attempts_sync_failed(attempts: list[dict[str, Any]], error: str) -> None:
    now = now_iso()

    with _lock:
        conn = connect_db()

        try:
            for item in attempts:
                conn.execute(
                    """
                    UPDATE offline_login_attempts
                    SET sync_attempts = COALESCE(sync_attempts, 0) + 1,
                        last_error = ?,
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        clean(error)[:1500],
                        now,
                        item["attempt_id"],
                    ),
                )

            conn.commit()

        finally:
            conn.close()


# ============================================================
# Offline QR verification
# ============================================================

def load_public_key():
    public_key_b64 = get_setting("offline_login_public_key_pem_b64", "")

    if not public_key_b64:
        raise RuntimeError("Offline login public key is not cached yet.")

    public_key_pem = b64_decode_pem(public_key_b64)

    return serialization.load_pem_public_key(public_key_pem)


def parse_offline_token(qr_code: str) -> tuple[str, str, dict[str, Any]]:
    token = clean(qr_code)

    if not token:
        raise ValueError("Empty QR code.")

    token_prefix = get_setting("token_prefix", DEFAULT_TOKEN_PREFIX)

    if not token.startswith(token_prefix):
        raise ValueError("QR code is not an offline-login token.")

    body = token[len(token_prefix):]

    if "." not in body:
        raise ValueError("Offline-login token is missing signature.")

    payload_part, signature_part = body.rsplit(".", 1)

    if not payload_part or not signature_part:
        raise ValueError("Offline-login token payload/signature is incomplete.")

    try:
        payload_json = b64url_decode(payload_part).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception as e:
        raise ValueError(f"Offline-login payload is invalid: {e}") from e

    if not isinstance(payload, dict):
        raise ValueError("Offline-login payload is not a JSON object.")

    return payload_part, signature_part, payload


def extract_payload_user_id(payload: dict[str, Any]) -> str:
    return clean(
        payload.get("user_id")
        or payload.get("userId")
        or payload.get("userID")
        or payload.get("_id")
        or payload.get("id")
        or payload.get("uid")
        or payload.get("sub")
    )


def extract_payload_token_id(payload: dict[str, Any]) -> str:
    return clean(
        payload.get("token_id")
        or payload.get("tokenId")
        or payload.get("jti")
        or payload.get("nonce")
        or ""
    )


def extract_payload_token_version(payload: dict[str, Any]) -> str:
    return clean(
        payload.get("token_version")
        or payload.get("tokenVersion")
        or payload.get("offlineLoginTokenVersion")
        or ""
    )


def get_cached_user(user_id: str) -> dict[str, Any] | None:
    init_db()

    with _lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM offline_users
                WHERE user_id = ?
                """,
                (clean(user_id),),
            ).fetchone()

            if row is None:
                return None

            item = dict(row)
            user = json_load(item.get("user_json"), default={})

            if isinstance(user, dict):
                user["_offline_cache_status"] = item.get("status")
                user["_offline_cache_expires_at"] = item.get("expires_at")
                user["_offline_cache_token_version"] = item.get("token_version")
                user["_offline_cache_bundle_version"] = item.get("bundle_version")
                return user

            return None

        finally:
            conn.close()


def get_cached_user_row(user_id: str) -> dict[str, Any] | None:
    init_db()

    with _lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM offline_users
                WHERE user_id = ?
                """,
                (clean(user_id),),
            ).fetchone()

            return dict(row) if row else None

        finally:
            conn.close()


def save_login_attempt(
    *,
    user_id: str = "",
    token_id: str = "",
    qr_hash: str = "",
    payload: dict[str, Any] | None = None,
    status: str,
    reason: str = "",
) -> str:
    init_db()

    attempt_id = uuid.uuid4().hex
    now = now_iso()

    with _lock:
        conn = connect_db()

        try:
            conn.execute(
                """
                INSERT INTO offline_login_attempts (
                    attempt_id,
                    user_id,
                    token_id,
                    qr_hash,
                    payload_json,
                    status,
                    reason,
                    used_at,
                    synced,
                    sync_attempts,
                    last_error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?)
                """,
                (
                    attempt_id,
                    clean(user_id),
                    clean(token_id),
                    clean(qr_hash),
                    json_dump(payload or {}),
                    clean(status),
                    clean(reason),
                    now,
                    now,
                    now,
                ),
            )

            conn.commit()

        finally:
            conn.close()

    return attempt_id


def verify_offline_login_qr(qr_code: str) -> dict[str, Any]:
    """Verify QR code using cached Ed25519 public key and cached user bundle.

    Returns a dict consumed by api_client.LocalJsonResponse:
    {
      "status_code": 200 or 401/503,
      "data": {...}
    }
    """
    qr_hash = sha256_text(qr_code)
    payload: dict[str, Any] = {}
    user_id = ""
    token_id = ""

    try:
        algorithm = get_setting("algorithm", DEFAULT_ALGORITHM)

        if algorithm.lower() != "ed25519":
            raise RuntimeError(f"Unsupported cached offline algorithm: {algorithm}")

        payload_part, signature_part, payload = parse_offline_token(qr_code)

        user_id = extract_payload_user_id(payload)
        token_id = extract_payload_token_id(payload)

        if not user_id:
            raise ValueError("Offline-login payload has no user_id.")

        public_key = load_public_key()

        try:
            public_key.verify(
                b64url_decode(signature_part),
                payload_part.encode("utf-8"),
            )
        except InvalidSignature as e:
            raise ValueError("Offline-login QR signature is invalid.") from e

        if is_not_yet_valid(payload.get("nbf") or payload.get("notBefore")):
            raise ValueError("Offline-login QR is not valid yet.")

        if is_expired(payload.get("exp") or payload.get("expiresAt") or payload.get("expires_at")):
            raise ValueError("Offline-login QR is expired.")

        user_row = get_cached_user_row(user_id)

        if not user_row:
            raise ValueError("User is not present in local offline-login bundle.")

        status = clean(user_row.get("status")).lower()

        if status and status != "active":
            raise ValueError(f"User is not active for offline login: {status}")

        if is_expired(user_row.get("expires_at")):
            raise ValueError("Cached offline user entry is expired.")

        cached_token_version = clean(user_row.get("token_version"))
        payload_token_version = extract_payload_token_version(payload)

        if cached_token_version and payload_token_version and cached_token_version != payload_token_version:
            raise ValueError("Offline-login token version does not match cached user version.")

        user = get_cached_user(user_id) or {}
        attempt_id = save_login_attempt(
            user_id=user_id,
            token_id=token_id,
            qr_hash=qr_hash,
            payload=payload,
            status="verified",
            reason="offline_login_verified",
        )

        data = {
            "success": True,
            "valid": True,
            "offline": True,
            "offline_login": True,
            "message": "Offline login verified using local booth identity cache.",
            "attempt_id": attempt_id,
            "user": user,
            "user_data": user,
            "userData": user,
        }

        print(
            f"[OFFLINE ID] Offline QR login verified: user_id={user_id}, "
            f"token_id={token_id or 'none'}, attempt_id={attempt_id}",
            flush=True,
        )

        return {
            "status_code": 200,
            "data": data,
        }

    except Exception as e:
        reason = str(e)

        try:
            save_login_attempt(
                user_id=user_id,
                token_id=token_id,
                qr_hash=qr_hash,
                payload=payload,
                status="rejected",
                reason=reason,
            )
        except Exception:
            pass

        print(
            f"[OFFLINE ID] Offline QR login rejected: user_id={user_id or 'unknown'}, "
            f"reason={reason}",
            flush=True,
        )

        return {
            "status_code": 401,
            "data": {
                "success": False,
                "valid": False,
                "offline": True,
                "offline_login": True,
                "error": reason,
                "message": reason,
            },
        }


# ============================================================
# Manual diagnostics
# ============================================================

def print_cache_status() -> None:
    init_db()

    print("[OFFLINE ID] Cache diagnostics", flush=True)
    print(f"[OFFLINE ID] DB_PATH={DB_PATH}", flush=True)
    print(f"[OFFLINE ID] counts={get_cache_counts()}", flush=True)
    print(f"[OFFLINE ID] algorithm={get_setting('algorithm', '')}", flush=True)
    print(f"[OFFLINE ID] token_prefix={get_setting('token_prefix', '')}", flush=True)
    print(f"[OFFLINE ID] bundle_version={get_setting('bundle_version', '')}", flush=True)
    print(f"[OFFLINE ID] bundle_synced_at={get_setting('bundle_synced_at', '')}", flush=True)


if __name__ == "__main__":
    print_cache_status()