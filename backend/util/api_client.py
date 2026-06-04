import mimetypes
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env.local"


def reload_env():
    load_dotenv(ENV_PATH, override=True)


reload_env()


class LocalJsonResponse:
    """Small requests.Response-compatible wrapper for local/offline responses.

    QRLoginPage and other callers expect:
      - .ok
      - .status_code
      - .headers
      - .text
      - .json()
    """

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data or {}
        self.status_code = int(status_code)
        self.ok = 200 <= self.status_code < 300
        self.headers = {"content-type": "application/json"}

        try:
            import json

            self.text = json.dumps(self._data, ensure_ascii=False, default=str)
        except Exception:
            self.text = str(self._data)

    def json(self):
        return self._data


def _base_url() -> str:
    reload_env()
    base = (
        os.getenv("WEBSITE_BASE_URL")
        or os.getenv("BASE_URL")
        or os.getenv("networkIP")
        or "127.0.0.1:3000"
    )

    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"http://{base}"

    return base.rstrip("/")


def _device_api_key() -> str:
    reload_env()
    return os.getenv("DEVICE_API_KEY", "").strip()


def _booth_device_id() -> str:
    reload_env()
    return os.getenv("BOOTH_DEVICE_ID", "").strip()


def _booth_device_secret() -> str:
    reload_env()
    return os.getenv("BOOTH_DEVICE_SECRET", "").strip()


def _headers() -> dict:
    headers = {}

    api_key = _device_api_key()
    booth_device_id = _booth_device_id()
    booth_device_secret = _booth_device_secret()

    if api_key:
        headers["x-device-api-key"] = api_key

    if booth_device_id:
        headers["x-booth-device-id"] = booth_device_id

    if booth_device_secret:
        headers["x-booth-device-secret"] = booth_device_secret

    return headers


def url(path: str) -> str:
    return f"{_base_url()}{path}"


def website_base_url() -> str:
    return _base_url()


def post_json(path: str, payload: dict, timeout: int = 10):
    reload_env()
    return requests.post(
        url(path),
        json=payload,
        headers=_headers(),
        timeout=timeout,
    )


def get_json(path: str, timeout: int = 10):
    reload_env()
    return requests.get(
        url(path),
        headers=_headers(),
        timeout=timeout,
    )


def post_multipart(path: str, data: dict, files: dict, timeout: int = 30):
    reload_env()
    return requests.post(
        url(path),
        data=data,
        files=files,
        headers=_headers(),
        timeout=timeout,
    )


def verify_login_qr(qr_code: str):
    payload = {
        "qrCode": str(qr_code).strip() if qr_code else ""
    }

    try:
        res = post_json("/api/qr-tokens/verify", payload, timeout=7)

        # Do not offline-fallback invalid/expired/rejected QR codes.
        # If the website clearly says 400/401/403/404, respect that response.
        # Offline fallback is only for server/network availability failures.
        if res.ok or getattr(res, "status_code", 0) in {400, 401, 403, 404}:
            return res

        print(
            f"[API] Online QR verify returned {res.status_code}; trying offline cache.",
            flush=True,
        )

    except Exception as exc:
        print(f"[API] Online QR verify unavailable; trying offline cache: {exc}", flush=True)

    try:
        from backend.offline_identity_cache import verify_offline_login_qr

        offline = verify_offline_login_qr(qr_code)

        return LocalJsonResponse(
            offline.get("data") or {},
            status_code=int(offline.get("status_code") or 500),
        )

    except Exception as offline_exc:
        return LocalJsonResponse(
            {
                "error": f"Online and offline QR verification failed: {offline_exc}",
                "offline": True,
            },
            status_code=503,
        )


def validate_discount_token(user_id: str, token: str):
    clean_user_id = str(user_id).strip() if user_id else ""
    clean_token = str(token).strip() if token else ""

    payload = {
        "userId": clean_user_id,
        "user_id": clean_user_id,
        "token": clean_token,
        "qrCode": clean_token,
        "qr_code": clean_token,
    }

    return post_json("/api/qr-tokens/validate", payload)


def store_qr_token(
    user_id: str,
    token: str,
    discount_percent: float = 10,
    source: str = "booth_printed_coupon",
    receipt_transaction_id: str | None = None,
    expires_at: str | None = None,
):
    clean_user_id = str(user_id).strip() if user_id else ""
    clean_token = str(token).strip() if token else ""
    clean_transaction_id = str(receipt_transaction_id or "").strip()
    clean_expires_at = str(expires_at or "").strip()
    clean_source = str(source or "booth_printed_coupon").strip()

    try:
        clean_discount_percent = float(discount_percent or 0)
    except Exception:
        clean_discount_percent = 10.0

    payload = {
        # User identifiers.
        "userId": clean_user_id,
        "user_id": clean_user_id,

        # Critical fix:
        # Website returns {"error":"qrCode is required"} when this is missing.
        "qrCode": clean_token,

        # Compatibility aliases for older/newer endpoints.
        "qr_code": clean_token,
        "token": clean_token,
        "qrValue": clean_token,
        "qr_value": clean_token,

        # Discount details.
        "discountPercent": clean_discount_percent,
        "discount_percent": clean_discount_percent,

        # Source/reason details.
        "source": clean_source,
        "reason": clean_source,

        # Transaction reference.
        "receiptTransactionId": clean_transaction_id,
        "receipt_transaction_id": clean_transaction_id,
        "transactionId": clean_transaction_id,
        "transaction_id": clean_transaction_id,

        # Expiration.
        "expiresAt": clean_expires_at,
        "expires_at": clean_expires_at,
    }

    print("[API] store_qr_token payload:", repr(payload), flush=True)

    res = post_json("/api/qr-tokens", payload, timeout=15)

    print("[API] store_qr_token status:", getattr(res, "status_code", None), flush=True)
    print("[API] store_qr_token body:", getattr(res, "text", ""), flush=True)

    return res


def post_transaction(payload: dict):
    return post_json("/api/transaction", payload)


def post_result(payload: dict):
    return post_json("/api/results", payload)


def create_paymongo_checkout(payload: dict):
    return post_json("/api/paymongo/checkout", payload, timeout=15)


def get_paymongo_checkout_status(session_id: str):
    return get_json(f"/api/paymongo/checkout-status/{session_id}", timeout=15)


def upload_receipt(user_id: str, timestamp: str, receipt_data: dict):
    payload = {
        "user_id": str(user_id),
        "timestamp": str(timestamp),
        "receipt": receipt_data,
    }
    return post_json("/api/receipts/upload", payload, timeout=20)


def get_device_config():
    return get_json("/api/device/config", timeout=15)


def post_device_presence(payload: dict):
    return post_json("/api/device/heartbeat", payload, timeout=10)


def post_device_inventory(payload: dict):
    return post_json("/api/device/inventory", payload, timeout=15)


def get_offline_login_bundle():
    return get_json("/api/device/offline-login-bundle", timeout=20)


def post_offline_login_attempts(payload: dict):
    return post_json("/api/device/offline-login-attempts", payload, timeout=20)


def _stringify_extra_value(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, (str, int, float, bool)):
        return str(value)

    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _merge_extra_form_data(data: dict, extra_data: dict | None):
    if not extra_data:
        return data

    for key, value in extra_data.items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue

        text_value = _stringify_extra_value(value).strip()
        if text_value:
            data[clean_key] = text_value

    return data


def upload_image(
    user_id: str,
    timestamp: str,
    image_type: str,
    image_path: str,
    product_id: str | None = None,
    transaction_id: str | None = None,
    extra_data: dict | None = None,
):
    image_file = Path(image_path)

    if not image_file.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    mime_type = mimetypes.guess_type(image_file.name)[0] or "application/octet-stream"

    with image_file.open("rb") as f:
        files = {
            "file": (image_file.name, f, mime_type)
        }

        data = {
            "user_id": str(user_id),
            "timestamp": str(timestamp),
            "image_type": str(image_type),
        }

        if product_id:
            data["productID"] = str(product_id)

        if transaction_id:
            data["transaction_id"] = str(transaction_id)

        data = _merge_extra_form_data(data, extra_data)

        return post_multipart(
            "/api/device/image/upload",
            data=data,
            files=files,
            timeout=60,
        )


def upload_session_images(
    user_id: str,
    timestamp: str,
    session_dir,
    product_id: str | None = None,
    transaction_id: str | None = None,
    extra_data: dict | None = None,
):
    session_dir = Path(session_dir)
    results = {}

    if not session_dir.exists() or not session_dir.is_dir():
        return {
            "ok": False,
            "error": f"Session directory not found: {session_dir}",
        }

    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    found_files = sorted(
        p for p in session_dir.iterdir()
        if p.is_file() and p.suffix.lower() in allowed_suffixes
    )

    if not found_files:
        return {
            "ok": False,
            "error": f"No uploadable image files found in {session_dir}",
        }

    for image_path in found_files:
        stem = image_path.stem.lower()

        if "annotated" in stem:
            image_type = "annotated"
        elif "result" in stem:
            image_type = "result"
        elif "original" in stem:
            image_type = "original"
        elif "raw" in stem:
            image_type = "raw"
        else:
            image_type = stem

        try:
            res = upload_image(
                user_id=user_id,
                timestamp=timestamp,
                image_type=image_type,
                image_path=str(image_path),
                product_id=product_id,
                transaction_id=transaction_id,
                extra_data=extra_data,
            )

            results[image_path.name] = {
                "image_type": image_type,
                "ok": res.ok,
                "status_code": res.status_code,
                "text": res.text,
            }

        except Exception as e:
            results[image_path.name] = {
                "image_type": image_type,
                "ok": False,
                "error": str(e),
            }

    return results