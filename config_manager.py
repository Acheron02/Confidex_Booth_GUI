import copy
import json
import os
import threading
import time
from typing import Any


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")

CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
INVENTORY_PATH = os.path.join(CONFIG_DIR, "inventory.json")

os.makedirs(CONFIG_DIR, exist_ok=True)


DEFAULT_CONFIG = {
    "branding": {
        "app_name": "CONFIDEX",
        "tagline": "Anonymous Health Screening",
        "website_text": "confidex.local"
    },
    "welcome_page": {
        "title": "WELCOME",
        "subtitle": "Anonymous Health Screening",
        "tap_text": "TAP TO PROCEED",
        "hint_text": "Tap anywhere to start your QR login"
    },
    "qr_login_page": {
        "title": "SCAN QR TO LOGIN",
        "instruction_text": "Sign in or sign up on our website to generate a QR code",
        "waiting_text": "Waiting for QR scan..."
    },
    "products": [],
    "payment": {
        "accepted_bills": [50, 100, 200, 500, 1000],
        "change_denominations": [20, 5, 1]
    }
}

DEFAULT_INVENTORY = {
    "products": {},
    "coins": {
        "20": {"stock": 0, "enabled": True},
        "5": {"stock": 0, "enabled": True},
        "1": {"stock": 0, "enabled": True}
    }
}


def _ensure_json_file(path: str, default_data: dict):
    if os.path.exists(path):
        return

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[CONFIG] Created missing file: {path}", flush=True)
    except Exception as e:
        print(f"[CONFIG] Failed to create {path}: {e}", flush=True)


_ensure_json_file(CONFIG_PATH, DEFAULT_CONFIG)
_ensure_json_file(INVENTORY_PATH, DEFAULT_INVENTORY)


class JsonStore:
    def __init__(self, path: str, label: str):
        self.path = path
        self.label = label
        self._data = {}
        self._mtime = 0.0
        self._lock = threading.RLock()

        self.load()
        watcher = threading.Thread(target=self._watch_file, daemon=True)
        watcher.start()

    def load(self) -> bool:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            with self._lock:
                self._data = data
                self._mtime = os.path.getmtime(self.path)

            print(f"[{self.label}] Loaded {os.path.basename(self.path)}", flush=True)
            return True

        except Exception as e:
            print(f"[{self.label}] Failed to load {self.path}: {e}", flush=True)
            return False

    def save(self) -> bool:
        try:
            with self._lock:
                tmp_path = self.path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                    f.write("\n")

                os.replace(tmp_path, self.path)
                self._mtime = os.path.getmtime(self.path)

            print(f"[{self.label}] Saved {os.path.basename(self.path)}", flush=True)
            return True

        except Exception as e:
            print(f"[{self.label}] Failed to save {self.path}: {e}", flush=True)
            return False

    def _watch_file(self):
        while True:
            try:
                if os.path.exists(self.path):
                    current_mtime = os.path.getmtime(self.path)
                    with self._lock:
                        known_mtime = self._mtime

                    if current_mtime != known_mtime:
                        print(f"[{self.label}] External change detected, reloading...", flush=True)
                        self.load()
            except Exception as e:
                print(f"[{self.label}] Watch error: {e}", flush=True)

            time.sleep(1)

    def get(self, *keys: Any, default=None):
        with self._lock:
            node = self._data
            for key in keys:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    return default
            return copy.deepcopy(node)

    def set(self, *keys: Any, value) -> bool:
        if not keys:
            return False

        with self._lock:
            next_data = copy.deepcopy(self._data)
            node = next_data
            for key in keys[:-1]:
                if key not in node or not isinstance(node[key], dict):
                    node[key] = {}
                node = node[key]

            if node.get(keys[-1]) == value:
                return True

            node[keys[-1]] = value
            self._data = next_data

        return self.save()

    def replace_all(self, data: dict) -> bool:
        with self._lock:
            if self._data == data:
                return True
            self._data = copy.deepcopy(data)
        return self.save()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)


class ConfigManager:
    def __init__(self):
        self.config_store = JsonStore(CONFIG_PATH, "CONFIG")
        self.inventory_store = JsonStore(INVENTORY_PATH, "INVENTORY")
        self.ensure_inventory_matches_products()

    def get(self, *keys, default=None):
        return self.config_store.get(*keys, default=default)

    def set(self, *keys, value) -> bool:
        ok = self.config_store.set(*keys, value=value)
        if ok and (len(keys) == 1 and keys[0] == "products"):
            self.ensure_inventory_matches_products()
        return ok

    def merge_remote_config(self, partial_config: dict) -> bool:
        try:
            if not isinstance(partial_config, dict):
                return False

            current = self.config_store.snapshot()
            merged = copy.deepcopy(current)

            for key, value in partial_config.items():
                merged[key] = value

            return self.config_store.replace_all(merged)
        except Exception as e:
            print(f"[CONFIG] merge_remote_config failed: {e}", flush=True)
            return False

    def get_inventory(self, *keys, default=None):
        return self.inventory_store.get(*keys, default=default)

    def set_inventory(self, *keys, value) -> bool:
        return self.inventory_store.set(*keys, value=value)

    def get_products(self):
        return self.get("products", default=[])

    def get_enabled_products(self):
        products = self.get_products()
        return [p for p in products if p.get("enabled", True)]

    def get_product_by_id(self, product_id: str):
        for product in self.get_products():
            if str(product.get("product_id")) == str(product_id):
                return product
        return None

    def get_product_stock(self, product_id: str) -> int:
        stock = self.get_inventory("products", str(product_id), "stock", default=0)
        try:
            return int(stock)
        except Exception:
            return 0

    def set_product_stock(self, product_id: str, stock: int) -> bool:
        return self.set_inventory("products", str(product_id), "stock", value=max(0, int(stock)))

    def decrement_product_stock(self, product_id: str, qty: int = 1) -> bool:
        qty = max(1, int(qty))
        current = self.get_product_stock(product_id)
        if current < qty:
            return False
        return self.set_product_stock(product_id, current - qty)

    def increment_product_stock(self, product_id: str, qty: int = 1) -> bool:
        qty = max(1, int(qty))
        current = self.get_product_stock(product_id)
        return self.set_product_stock(product_id, current + qty)

    def is_product_available(self, product_id: str) -> bool:
        product = self.get_product_by_id(product_id)
        if not product or not product.get("enabled", True):
            return False
        return self.get_product_stock(product_id) > 0

    def ensure_inventory_matches_products(self) -> bool:
        try:
            products = self.get_products()
            current_inventory = self.get_inventory("products", default={}) or {}

            next_inventory = {}

            for product in products:
                product_id = str(product.get("product_id", "")).strip()
                if not product_id:
                    continue

                existing = current_inventory.get(product_id, {})
                next_inventory[product_id] = {
                    "stock": max(0, int(existing.get("stock", 0)))
                }

            current_coins = self.get_inventory("coins", default={}) or {
                "20": {"stock": 0, "enabled": True},
                "5": {"stock": 0, "enabled": True},
                "1": {"stock": 0, "enabled": True},
            }

            next_data = {
                "products": next_inventory,
                "coins": current_coins,
            }

            if self.inventory_store.snapshot() == next_data:
                return True

            return self.inventory_store.replace_all(next_data)
        except Exception as e:
            print(f"[CONFIG] ensure_inventory_matches_products failed: {e}", flush=True)
            return False

    def get_coin_stock(self, denomination: int) -> int:
        stock = self.get_inventory("coins", str(denomination), "stock", default=0)
        try:
            return int(stock)
        except Exception:
            return 0

    def set_coin_stock(self, denomination: int, stock: int) -> bool:
        return self.set_inventory("coins", str(denomination), "stock", value=max(0, int(stock)))

    def is_coin_enabled(self, denomination: int) -> bool:
        return bool(self.get_inventory("coins", str(denomination), "enabled", default=True))

    def set_coin_enabled(self, denomination: int, enabled: bool) -> bool:
        return self.set_inventory("coins", str(denomination), "enabled", value=bool(enabled))

    def decrement_coin_stock(self, denomination: int, qty: int = 1) -> bool:
        qty = max(1, int(qty))
        current = self.get_coin_stock(denomination)
        if current < qty:
            return False
        return self.set_coin_stock(denomination, current - qty)

    def increment_coin_stock(self, denomination: int, qty: int = 1) -> bool:
        qty = max(1, int(qty))
        current = self.get_coin_stock(denomination)
        return self.set_coin_stock(denomination, current + qty)

    def get_coin_inventory(self):
        coins = self.get_inventory("coins", default={})
        result = []

        for key, value in coins.items():
            try:
                denom = int(key)
            except Exception:
                continue

            result.append({
                "denomination": denom,
                "stock": int(value.get("stock", 0)),
                "enabled": bool(value.get("enabled", True)),
            })

        result.sort(key=lambda x: x["denomination"], reverse=True)
        return result

    def get_change_denominations(self):
        denoms = self.get("payment", "change_denominations", default=[20, 5, 1])
        cleaned = []
        for d in denoms:
            try:
                cleaned.append(int(d))
            except Exception:
                pass
        return sorted(cleaned, reverse=True)

    def compute_change_breakdown(self, amount: int):
        try:
            remaining = int(amount)
        except Exception:
            return None

        if remaining < 0:
            return None
        if remaining == 0:
            return {}

        breakdown = {}
        denominations = self.get_change_denominations()

        for denom in denominations:
            if not self.is_coin_enabled(denom):
                continue

            stock = self.get_coin_stock(denom)
            if stock <= 0:
                continue

            use_qty = min(remaining // denom, stock)
            if use_qty > 0:
                breakdown[denom] = use_qty
                remaining -= denom * use_qty

        if remaining != 0:
            return None

        return breakdown

    def apply_change_breakdown(self, breakdown: dict) -> bool:
        if not breakdown:
            return True

        for denom, qty in breakdown.items():
            if self.get_coin_stock(int(denom)) < int(qty):
                return False

        for denom, qty in breakdown.items():
            success = self.decrement_coin_stock(int(denom), int(qty))
            if not success:
                return False

        return True


config = ConfigManager()