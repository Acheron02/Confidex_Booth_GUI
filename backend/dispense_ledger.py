"""Durable dispense ledger for CONFIDEX booth.

This file prevents accidental duplicate kit dispensing when the GUI resumes,
retries, or receives a second login/transaction event while a previous paid
flow is still active.

Rule:
    One transaction_id may only have one confirmed kit dispense.

The ledger is local-only and safe to keep across app restarts.
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
DB_PATH = DATA_DIR / "dispense_ledger.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()


HARDWARE_STATUSES = {"started", "confirmed", "failed", "blocked"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _dump(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _load(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def init_dispense_ledger() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dispense_ledger (
                    transaction_id TEXT PRIMARY KEY,
                    product_id TEXT DEFAULT '',
                    product_name TEXT DEFAULT '',
                    dispense_slot TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'started',
                    started_at TEXT NOT NULL,
                    confirmed_at TEXT DEFAULT '',
                    failed_at TEXT DEFAULT '',
                    last_message TEXT DEFAULT '',
                    result_json TEXT DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def get_dispense_record(transaction_id: str) -> dict[str, Any] | None:
    tx = str(transaction_id or "").strip()
    if not tx:
        return None

    init_dispense_ledger()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM dispense_ledger WHERE transaction_id = ?",
                (tx,),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["result"] = _load(item.pop("result_json", "{}"))
            return item
        finally:
            conn.close()


def begin_dispense_attempt(
    transaction_id: str,
    product_id: str = "",
    product_name: str = "",
    dispense_slot: str = "",
) -> tuple[bool, dict[str, Any] | None]:
    """Return (allowed, existing_record).

    If a transaction is already confirmed, the caller must NOT send another
    DISPENSE command. If a previous started/failed attempt exists, this keeps
    the record but allows one foreground retry only when not confirmed.
    """
    tx = str(transaction_id or "").strip()
    if not tx:
        # No transaction id means we cannot make a durable safety decision.
        # Let the caller continue but no ledger protection is possible.
        return True, None

    init_dispense_ledger()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM dispense_ledger WHERE transaction_id = ?",
                (tx,),
            ).fetchone()

            if row is not None:
                existing = dict(row)
                existing["result"] = _load(existing.pop("result_json", "{}"))
                if str(existing.get("status") or "").lower() == "confirmed":
                    return False, existing

                conn.execute(
                    """
                    UPDATE dispense_ledger
                    SET product_id = ?,
                        product_name = ?,
                        dispense_slot = ?,
                        status = 'started',
                        last_message = 'Retrying dispense attempt after non-confirmed previous attempt.',
                        updated_at = ?
                    WHERE transaction_id = ?
                    """,
                    (str(product_id or ""), str(product_name or ""), str(dispense_slot or ""), _now(), tx),
                )
                conn.commit()
                return True, existing

            now = _now()
            conn.execute(
                """
                INSERT INTO dispense_ledger (
                    transaction_id, product_id, product_name, dispense_slot,
                    status, started_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'started', ?, ?)
                """,
                (tx, str(product_id or ""), str(product_name or ""), str(dispense_slot or ""), now, now),
            )
            conn.commit()
            return True, None
        finally:
            conn.close()


def mark_dispense_confirmed(transaction_id: str, result: Any = None, message: str = "") -> None:
    tx = str(transaction_id or "").strip()
    if not tx:
        return

    init_dispense_ledger()
    with _lock:
        conn = _connect()
        try:
            now = _now()
            conn.execute(
                """
                INSERT INTO dispense_ledger (
                    transaction_id, status, started_at, confirmed_at,
                    last_message, result_json, updated_at
                )
                VALUES (?, 'confirmed', ?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    status = 'confirmed',
                    confirmed_at = excluded.confirmed_at,
                    last_message = excluded.last_message,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (tx, now, now, str(message or "Kit dispense confirmed."), _dump(result), now),
            )
            conn.commit()
        finally:
            conn.close()


def mark_dispense_failed(transaction_id: str, result: Any = None, message: str = "") -> None:
    tx = str(transaction_id or "").strip()
    if not tx:
        return

    init_dispense_ledger()
    with _lock:
        conn = _connect()
        try:
            now = _now()
            conn.execute(
                """
                INSERT INTO dispense_ledger (
                    transaction_id, status, started_at, failed_at,
                    last_message, result_json, updated_at
                )
                VALUES (?, 'failed', ?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    status = 'failed',
                    failed_at = excluded.failed_at,
                    last_message = excluded.last_message,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (tx, now, now, str(message or "Kit dispense failed before confirmation."), _dump(result), now),
            )
            conn.commit()
        finally:
            conn.close()


def clear_dispense_record(transaction_id: str) -> None:
    tx = str(transaction_id or "").strip()
    if not tx:
        return

    init_dispense_ledger()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM dispense_ledger WHERE transaction_id = ?", (tx,))
            conn.commit()
        finally:
            conn.close()
