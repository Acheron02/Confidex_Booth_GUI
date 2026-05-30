"""Durable active-flow state for Confidex booth recovery.

This is intentionally small and local-only. It lets the GUI preserve the
current transaction/page context when a recoverable error occurs during
post-payment pages such as HowToUsePage and KitInsertionPage.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "active_flow.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _json_load(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def init_flow_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_flow (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    stage TEXT NOT NULL,
                    user_json TEXT DEFAULT '{}',
                    product_json TEXT DEFAULT '{}',
                    transaction_id TEXT DEFAULT '',
                    extra_json TEXT DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def save_active_flow(
    stage: str,
    user_data: Any = None,
    selected_product: Any = None,
    transaction_id: str | None = None,
    extra: Any = None,
) -> None:
    init_flow_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO active_flow (
                    id, stage, user_json, product_json, transaction_id, extra_json, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stage = excluded.stage,
                    user_json = excluded.user_json,
                    product_json = excluded.product_json,
                    transaction_id = excluded.transaction_id,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(stage or ""),
                    _json_dump(user_data),
                    _json_dump(selected_product),
                    str(transaction_id or ""),
                    _json_dump(extra),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_active_flow() -> dict[str, Any] | None:
    init_flow_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM active_flow WHERE id = 1").fetchone()
            if row is None:
                return None
            data = dict(row)
            return {
                "stage": data.get("stage") or "",
                "user_data": _json_load(data.get("user_json") or "{}"),
                "selected_product": _json_load(data.get("product_json") or "{}"),
                "transaction_id": data.get("transaction_id") or "",
                "extra": _json_load(data.get("extra_json") or "{}"),
                "updated_at": data.get("updated_at") or "",
            }
        finally:
            conn.close()


def clear_active_flow() -> None:
    init_flow_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM active_flow WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
