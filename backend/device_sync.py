import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import websocket

from backend.util import api_client
from config_manager import config
from backend.system_events import report_warning, report_info, clear_visible_events

try:
    import backend.offline_identity_cache as offline_identity_cache
    from backend.offline_identity_cache import (
        sync_offline_identity_bundle,
        sync_pending_offline_login_attempts,
    )

    OFFLINE_IDENTITY_AVAILABLE = True
    OFFLINE_IDENTITY_IMPORT_ERROR = ""
    print("[OFFLINE ID] Module import OK: backend.offline_identity_cache", flush=True)

except Exception as e:
    offline_identity_cache = None
    sync_offline_identity_bundle = None
    sync_pending_offline_login_attempts = None

    OFFLINE_IDENTITY_AVAILABLE = False
    OFFLINE_IDENTITY_IMPORT_ERROR = str(e)

    print(f"[OFFLINE ID] Import failed: {e}", flush=True)


WS_URL = os.getenv("BOOTH_WS_URL", "").strip()

PRESENCE_INTERVAL_SECONDS = 60
RECONNECT_BASE_SECONDS = 3
RECONNECT_MAX_SECONDS = 60

ws_app = None
ws_connected = False

_last_sent_inventory = None
_inventory_dirty = False
_sync_lock = threading.RLock()

_last_remote_config_version = None
_last_remote_inventory_version = None

_last_visible_ws_warning_at = 0

_initial_sync_warning_active = False
_ws_warning_active = False
_device_sync_visible_warning_active = False

_last_auth_ok_at = 0.0

_last_offline_identity_sync_at = 0.0
OFFLINE_IDENTITY_SYNC_INTERVAL_SECONDS = 300


def _short_json(value, limit: int = 800) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)

    if len(text) > limit:
        return text[:limit] + "...<truncated>"

    return text


def _offline_identity_cache_status(reason: str = "status"):
    """Print cache DB status so we can confirm offline-login data exists locally."""
    if not OFFLINE_IDENTITY_AVAILABLE or offline_identity_cache is None:
        print(
            f"[OFFLINE ID] Cache status skipped during {reason}: "
            f"module unavailable ({OFFLINE_IDENTITY_IMPORT_ERROR})",
            flush=True,
        )
        return

    db_path = getattr(offline_identity_cache, "DB_PATH", None)

    if not db_path:
        print(
            f"[OFFLINE ID] Cache status during {reason}: DB_PATH not exposed by offline_identity_cache.py",
            flush=True,
        )
        return

    db_path = Path(db_path)

    if not db_path.exists():
        print(
            f"[OFFLINE ID] Cache status during {reason}: db={db_path} exists=False",
            flush=True,
        )
        return

    counts = {}

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        for table in [
            "offline_identity_settings",
            "offline_users",
            "offline_login_attempts",
        ]:
            try:
                counts[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception as e:
                counts[table] = f"ERROR:{e}"

        conn.close()

    except Exception as e:
        print(
            f"[OFFLINE ID] Cache status during {reason}: db={db_path} read_error={e}",
            flush=True,
        )
        return

    print(
        f"[OFFLINE ID] Cache status during {reason}: db={db_path} exists=True counts={counts}",
        flush=True,
    )


def build_inventory_snapshot():
    return {
        "products": config.get_inventory("products", default={}),
        "coins": config.get_inventory("coins", default={}),
    }


def mark_inventory_dirty():
    global _inventory_dirty

    with _sync_lock:
        _inventory_dirty = True

    flush_inventory_if_connected()


def _apply_remote_payload(data: dict, force: bool = False):
    global _last_remote_config_version, _last_remote_inventory_version

    remote_config = data.get("config") or {}
    remote_inventory = data.get("inventorySnapshot") or {}

    config_version = int(data.get("configVersion", 1))
    inventory_version = int(data.get("inventoryVersion", 1))

    config_changed = force or (_last_remote_config_version != config_version)
    inventory_changed = force or (_last_remote_inventory_version != inventory_version)

    if config_changed and remote_config:
        config.merge_remote_config(remote_config)
        _last_remote_config_version = config_version
        print(f"[DEVICE WS] Applied remote config v{config_version}", flush=True)

    if inventory_changed and remote_inventory:
        with _sync_lock:
            local_inventory_dirty = bool(_inventory_dirty)

        if local_inventory_dirty:
            print(
                f"[DEVICE WS] Skipped remote inventory v{inventory_version} "
                "because local inventory has pending unsynced changes.",
                flush=True,
            )
        else:
            config.inventory_store.replace_all(remote_inventory)
            _last_remote_inventory_version = inventory_version
            print(f"[DEVICE WS] Applied remote inventory v{inventory_version}", flush=True)

    if config_changed or inventory_changed:
        config.ensure_inventory_matches_products()

    return config_changed or inventory_changed


def _mark_device_sync_warning_active(kind: str):
    global _initial_sync_warning_active
    global _ws_warning_active
    global _device_sync_visible_warning_active
    global _last_visible_ws_warning_at

    kind = str(kind or "").strip().lower()

    if kind == "initial_sync":
        _initial_sync_warning_active = True
    elif kind == "websocket":
        _ws_warning_active = True

    _device_sync_visible_warning_active = True
    _last_visible_ws_warning_at = time.time()


def _clear_device_sync_visible_warning(reason="website connection recovered"):
    global _initial_sync_warning_active
    global _ws_warning_active
    global _device_sync_visible_warning_active
    global _last_visible_ws_warning_at

    had_visible_warning = (
        _initial_sync_warning_active
        or _ws_warning_active
        or _device_sync_visible_warning_active
    )

    _initial_sync_warning_active = False
    _ws_warning_active = False
    _device_sync_visible_warning_active = False
    _last_visible_ws_warning_at = 0

    if not had_visible_warning:
        return

    message = "The booth has reconnected to the website and authenticated successfully."

    try:
        clear_visible_events(
            "device_sync",
            "Website Connection Restored",
            message,
            details={"reason": reason},
        )
    except Exception as e:
        print(f"[DEVICE WS] Failed to queue website recovery clear event: {e}", flush=True)

    try:
        report_info(
            "device_sync",
            "Website Connection Restored",
            message,
            details={"reason": reason},
            visible=False,
        )
    except Exception:
        pass


def sync_offline_identity_safely(reason: str = "device_sync", force: bool = False):
    """Sync offline QR-login cache and reconcile pending offline logins."""
    global _last_offline_identity_sync_at

    now = time.time()

    if not force and now - _last_offline_identity_sync_at < OFFLINE_IDENTITY_SYNC_INTERVAL_SECONDS:
        return

    _last_offline_identity_sync_at = now

    print(
        f"[OFFLINE ID] Sync requested: reason={reason}, force={force}, "
        f"available={OFFLINE_IDENTITY_AVAILABLE}",
        flush=True,
    )

    if not OFFLINE_IDENTITY_AVAILABLE:
        print(
            f"[OFFLINE ID] Sync skipped: offline_identity_cache unavailable. "
            f"import_error={OFFLINE_IDENTITY_IMPORT_ERROR}",
            flush=True,
        )
        return

    try:
        if sync_offline_identity_bundle is not None:
            print(f"[OFFLINE ID] Bundle download starting: reason={reason}", flush=True)
            bundle_result = sync_offline_identity_bundle()
            print(
                f"[OFFLINE ID] Bundle download finished: reason={reason}, "
                f"result={_short_json(bundle_result)}",
                flush=True,
            )
            _offline_identity_cache_status(f"bundle_sync:{reason}")
        else:
            print("[OFFLINE ID] Bundle download skipped: function missing", flush=True)

        if sync_pending_offline_login_attempts is not None:
            print(f"[OFFLINE ID] Pending login attempt sync starting: reason={reason}", flush=True)
            attempts_result = sync_pending_offline_login_attempts()
            print(
                f"[OFFLINE ID] Pending login attempt sync finished: reason={reason}, "
                f"result={_short_json(attempts_result)}",
                flush=True,
            )
            _offline_identity_cache_status(f"attempt_sync:{reason}")
        else:
            print("[OFFLINE ID] Pending login attempt sync skipped: function missing", flush=True)

    except Exception as e:
        print(f"[OFFLINE ID] Safe sync failed during {reason}: {e}", flush=True)
        _offline_identity_cache_status(f"failed:{reason}")


def fetch_remote_config_once():
    try:
        res = api_client.get_device_config()

        if not res.ok:
            print(
                f"[DEVICE WS] Initial config fetch failed: {res.status_code} {res.text}",
                flush=True,
            )

            _mark_device_sync_warning_active("initial_sync")

            report_warning(
                "device_sync",
                "Initial Website Sync Failed",
                "The booth could not load the latest website config/inventory. Local cached config will be used.",
                details=f"{res.status_code} {res.text}",
                visible=True,
            )
            return False

        data = res.json()
        _apply_remote_payload(data, force=True)

        print(
            f"[DEVICE WS] Initial sync applied "
            f"(config v{int(data.get('configVersion', 1))}, "
            f"inventory v{int(data.get('inventoryVersion', 1))})",
            flush=True,
        )

        _clear_device_sync_visible_warning("initial_http_sync_success")

        sync_offline_identity_safely("initial_http_sync_success", force=True)

        return True

    except Exception as e:
        print(f"[DEVICE WS] Initial sync error: {e}", flush=True)

        _mark_device_sync_warning_active("initial_sync")

        report_warning(
            "device_sync",
            "Initial Website Sync Failed",
            f"The booth could not load the latest website config/inventory. Local cached config will be used.",
            details=str(e),
            visible=True,
        )
        return False


def flush_inventory_if_connected():
    global _inventory_dirty, _last_sent_inventory

    if not ws_app or not ws_connected:
        return False

    try:
        with _sync_lock:
            snapshot = build_inventory_snapshot()

            if not _inventory_dirty:
                return True

            if _last_sent_inventory == snapshot:
                _inventory_dirty = False
                return True

        ws_app.send(
            json.dumps(
                {
                    "type": "inventory_update",
                    "inventorySnapshot": snapshot,
                }
            )
        )

        with _sync_lock:
            _last_sent_inventory = snapshot
            _inventory_dirty = False

        print("[DEVICE WS] Inventory update sent", flush=True)
        return True

    except Exception as e:
        print(f"[DEVICE WS] Inventory send error: {e}", flush=True)
        return False


def push_inventory_if_dirty(force: bool = False):
    global _inventory_dirty, _last_sent_inventory, _last_remote_inventory_version

    try:
        with _sync_lock:
            snapshot = build_inventory_snapshot()

            if not force and not _inventory_dirty:
                return True

            if not force and _last_sent_inventory == snapshot:
                _inventory_dirty = False
                return True

        if ws_app and ws_connected:
            return flush_inventory_if_connected()

        res = api_client.post_device_inventory(
            {
                "inventorySnapshot": snapshot,
            }
        )

        if not res.ok:
            print(
                f"[DEVICE WS] HTTP inventory fallback failed: {res.status_code} {res.text}",
                flush=True,
            )
            return False

        data = {}
        try:
            data = res.json()
        except Exception:
            data = {}

        with _sync_lock:
            _last_sent_inventory = snapshot
            _inventory_dirty = False

        if "inventoryVersion" in data:
            try:
                _last_remote_inventory_version = int(data.get("inventoryVersion", 1))
            except Exception:
                pass

        print("[DEVICE WS] Inventory synced via HTTP fallback", flush=True)
        return True

    except Exception as e:
        print(f"[DEVICE WS] push_inventory_if_dirty error: {e}", flush=True)
        return False


def _send_auth(ws):
    payload = {
        "type": "auth",
        "apiKey": os.getenv("DEVICE_API_KEY", ""),
        "deviceId": os.getenv("BOOTH_DEVICE_ID", ""),
        "deviceSecret": os.getenv("BOOTH_DEVICE_SECRET", ""),
    }
    ws.send(json.dumps(payload))


def on_open(ws):
    global ws_connected

    ws_connected = True
    print("[DEVICE WS] Connected", flush=True)
    _send_auth(ws)


def on_message(ws, message):
    global _last_remote_config_version
    global _last_remote_inventory_version
    global _last_auth_ok_at

    try:
        data = json.loads(message)
        msg_type = data.get("type")

        if msg_type == "auth_ok":
            print("[DEVICE WS] Authenticated", flush=True)

            _last_auth_ok_at = time.time()

            _apply_remote_payload(data, force=True)
            flush_inventory_if_connected()

            _clear_device_sync_visible_warning("auth_ok")

            sync_offline_identity_safely("websocket_auth_ok", force=True)

            return

        if msg_type == "config_updated":
            _apply_remote_payload(data, force=False)
            return

        if msg_type == "inventory_replace":
            _apply_remote_payload(
                {
                    "configVersion": _last_remote_config_version or 1,
                    "inventoryVersion": data.get("inventoryVersion", 1),
                    "inventorySnapshot": data.get("inventorySnapshot") or {},
                },
                force=False,
            )
            return

        if msg_type == "inventory_ack":
            version = data.get("inventoryVersion")
            if version is not None:
                try:
                    _last_remote_inventory_version = int(version)
                except Exception:
                    pass

            print(
                f"[DEVICE WS] Inventory acknowledged "
                f"(v{data.get('inventoryVersion')})",
                flush=True,
            )
            return

        if msg_type == "ping":
            ws.send(json.dumps({"type": "pong"}))
            return

        if msg_type == "error":
            print(f"[DEVICE WS] Server error: {data.get('message')}", flush=True)
            return

        print(f"[DEVICE WS] Unknown message: {data}", flush=True)

    except Exception as e:
        print(f"[DEVICE WS] Message handling error: {e}", flush=True)


def on_error(ws, error):
    global _last_visible_ws_warning_at

    print(f"[DEVICE WS] Error: {error}", flush=True)

    now = time.time()

    if not _ws_warning_active:
        _mark_device_sync_warning_active("websocket")

        report_warning(
            "device_sync",
            "Website Connection Lost",
            "The booth lost connection to the website. It will keep retrying automatically.",
            details=str(error),
            visible=True,
        )
        return

    if now - _last_visible_ws_warning_at > 30:
        _last_visible_ws_warning_at = now

        report_warning(
            "device_sync",
            "Website Connection Still Reconnecting",
            "The booth is still reconnecting to the website.",
            details=str(error),
            visible=False,
        )


def on_close(ws, close_status_code, close_msg):
    global ws_connected

    ws_connected = False
    print(f"[DEVICE WS] Closed: {close_status_code} {close_msg}", flush=True)


def presence_loop():
    while True:
        try:
            if ws_app and ws_connected:
                ws_app.send(
                    json.dumps(
                        {
                            "type": "presence",
                            "status": "online",
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        }
                    )
                )

                sync_offline_identity_safely("presence_loop", force=False)

        except Exception as e:
            print(f"[DEVICE WS] Presence error: {e}", flush=True)

        time.sleep(PRESENCE_INTERVAL_SECONDS)


def websocket_loop():
    global ws_app, ws_connected

    reconnect_delay = RECONNECT_BASE_SECONDS

    while True:
        run_started_at = time.time()

        try:
            ws_app = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            ws_app.run_forever(
                ping_interval=25,
                ping_timeout=10,
            )

        except Exception as e:
            print(f"[DEVICE WS] run_forever error: {e}", flush=True)

        ws_connected = False

        if _last_auth_ok_at >= run_started_at:
            reconnect_delay = RECONNECT_BASE_SECONDS

        print(f"[DEVICE WS] Reconnecting in {reconnect_delay}s", flush=True)
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_SECONDS)


def start_background_sync():
    if not WS_URL:
        print("[DEVICE WS] Missing BOOTH_WS_URL", flush=True)

    print(
        f"[OFFLINE ID] Startup check: available={OFFLINE_IDENTITY_AVAILABLE}, "
        f"import_error={OFFLINE_IDENTITY_IMPORT_ERROR or 'none'}",
        flush=True,
    )
    _offline_identity_cache_status("startup_before_sync")

    fetch_remote_config_once()

    threading.Thread(target=presence_loop, daemon=True).start()
    threading.Thread(target=websocket_loop, daemon=True).start()

    print("[DEVICE WS] Background websocket sync started", flush=True)