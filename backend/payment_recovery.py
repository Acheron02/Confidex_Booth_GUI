"""Local online-payment recovery store for the Confidex booth.

Purpose:
- As soon as an online-payment QR checkout is created, save it locally.
- If the booth loses internet before confirming payment, log the user out safely.
- When the same user logs in again, main.py can check the saved checkout session
  and continue the transaction if the payment provider already marked it paid.
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
DB_PATH = DATA_DIR / "payment_recovery.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_payment_recovery_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_recovery (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    product_json TEXT NOT NULL,
                    discount REAL DEFAULT 0,
                    amount REAL DEFAULT 0,
                    checkout_url TEXT DEFAULT '',
                    reference TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    website_transaction_id TEXT DEFAULT '',
                    flow_stage TEXT DEFAULT 'checkout_created',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump_product(product: dict[str, Any] | None) -> str:
    try:
        return json.dumps(product or {}, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _load_product(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_pending_payment(
    session_id: str,
    user_id: str,
    username: str = "",
    product: dict[str, Any] | None = None,
    discount: float = 0,
    amount: float = 0,
    checkout_url: str = "",
    reference: str = "",
    status: str = "pending",
    flow_stage: str = "checkout_created",
    website_transaction_id: str = "",
) -> None:
    if not session_id or not user_id:
        return

    init_payment_recovery_db()
    now = _now()

    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO payment_recovery (
                    session_id, user_id, username, product_json, discount, amount,
                    checkout_url, reference, status, website_transaction_id,
                    flow_stage, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    username = excluded.username,
                    product_json = excluded.product_json,
                    discount = excluded.discount,
                    amount = excluded.amount,
                    checkout_url = excluded.checkout_url,
                    reference = excluded.reference,
                    status = excluded.status,
                    website_transaction_id = excluded.website_transaction_id,
                    flow_stage = excluded.flow_stage,
                    updated_at = excluded.updated_at
                """,
                (
                    str(session_id),
                    str(user_id),
                    str(username or ""),
                    _dump_product(product),
                    float(discount or 0),
                    float(amount or 0),
                    str(checkout_url or ""),
                    str(reference or ""),
                    str(status or "pending"),
                    str(website_transaction_id or ""),
                    str(flow_stage or "checkout_created"),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def update_payment_status(
    session_id: str,
    status: str | None = None,
    website_transaction_id: str | None = None,
    flow_stage: str | None = None,
) -> None:
    if not session_id:
        return

    init_payment_recovery_db()
    updates: dict[str, Any] = {"updated_at": _now()}

    if status is not None:
        updates["status"] = str(status)
    if website_transaction_id is not None:
        updates["website_transaction_id"] = str(website_transaction_id)
    if flow_stage is not None:
        updates["flow_stage"] = str(flow_stage)

    set_sql = ", ".join(f"{key} = ?" for key in updates.keys())
    values = list(updates.values()) + [str(session_id)]

    with _lock:
        conn = _connect()
        try:
            conn.execute(f"UPDATE payment_recovery SET {set_sql} WHERE session_id = ?", values)
            conn.commit()
        finally:
            conn.close()


def get_pending_payments_for_user(user_id: str) -> list[dict[str, Any]]:
    if not user_id:
        return []

    init_payment_recovery_db()

    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM payment_recovery
                WHERE user_id = ?
                  AND status IN ('pending', 'paid')
                  AND flow_stage != 'completed'
                ORDER BY updated_at DESC
                """,
                (str(user_id),),
            ).fetchall()

            results: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["product"] = _load_product(item.pop("product_json", "{}"))
                results.append(item)
            return results
        finally:
            conn.close()


def mark_payment_completed(session_id: str) -> None:
    update_payment_status(session_id, status="completed", flow_stage="completed")


def build_transaction_payload_from_record(user_data: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    product = record.get("product") or {}

    product_id = (
        product.get("productID")
        or product.get("product_id")
        or product.get("id")
        or ""
    )
    product_name = product.get("name") or product.get("type") or "Confidex Kit"
    product_type = product.get("type") or ""
    price = float(product.get("price", 0) or 0)
    discount = float(record.get("discount", 0) or 0)
    amount = float(record.get("amount", 0) or 0)
    user_id = user_data.get("_id") or user_data.get("userID") or record.get("user_id")

    return {
        "user_id": user_id,
        "status": "completed",
        "purchasedDate": None,
        "payment_method": "paymongo",
        "payment_session_id": record.get("session_id") or "",
        "payment_reference": record.get("reference") or "",
        "items": [
            {
                "name": product_name,
                "productID": product_id,
                "type": product_type,
                "price": price,
                "discount": discount,
                "finalPrice": amount,
                "result": "Pending",
            }
        ],
    }
