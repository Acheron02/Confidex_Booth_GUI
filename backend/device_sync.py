import json
import os
import threading
import time

import websocket

from backend.util import api_client
from config_manager import config

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
        config.inventory_store.replace_all(remote_inventory)
        _last_remote_inventory_version = inventory_version
        print(f"[DEVICE WS] Applied remote inventory v{inventory_version}", flush=True)

    if config_changed or inventory_changed:
        config.ensure_inventory_matches_products()

    return config_changed or inventory_changed


def fetch_remote_config_once():
    try:
        res = api_client.get_device_config()
        if not res.ok:
            print(
                f"[DEVICE WS] Initial config fetch failed: {res.status_code} {res.text}",
                flush=True,
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
        return True

    except Exception as e:
        print(f"[DEVICE WS] Initial sync error: {e}", flush=True)
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
    """
    Compatibility wrapper for older pages that still call
    push_inventory_if_dirty() after mark_inventory_dirty().

    New websocket flow sends inventory through the active socket if connected.
    If websocket is not connected yet, fall back to the HTTP inventory route.
    """
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
    global _last_remote_config_version, _last_remote_inventory_version

    try:
        data = json.loads(message)
        msg_type = data.get("type")

        if msg_type == "auth_ok":
            print("[DEVICE WS] Authenticated", flush=True)
            _apply_remote_payload(data, force=True)
            flush_inventory_if_connected()
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
    print(f"[DEVICE WS] Error: {error}", flush=True)


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
        except Exception as e:
            print(f"[DEVICE WS] Presence error: {e}", flush=True)

        time.sleep(PRESENCE_INTERVAL_SECONDS)


def websocket_loop():
    global ws_app, ws_connected

    reconnect_delay = RECONNECT_BASE_SECONDS

    while True:
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
        print(f"[DEVICE WS] Reconnecting in {reconnect_delay}s", flush=True)
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_SECONDS)


def start_background_sync():
    if not WS_URL:
        print("[DEVICE WS] Missing BOOTH_WS_URL", flush=True)

    fetch_remote_config_once()

    threading.Thread(target=presence_loop, daemon=True).start()
    threading.Thread(target=websocket_loop, daemon=True).start()

    print("[DEVICE WS] Background websocket sync started", flush=True)