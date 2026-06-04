"""Durable per-user active-flow state for Confidex booth recovery.

This lets the GUI preserve unfinished post-payment transaction/page context
without blocking other users.

Important behavior:
- Each user can have their own unfinished paid flow.
- A different user logging in will not delete the previous user's flow.
- The previous user can scan again later and resume their own transaction.
- The obsolete legacy active_flow id=1 row is cleared so completed flows cannot
  be resurrected.
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


POST_PAYMENT_STAGES = {
    "ChangeDispensingPage",
    "ReceiptPage",
    "DispensingPage",
    "HowToUsePage",
    "KitInsertionPage",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _json_load(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_user_id(user_data: Any) -> str:
    if not isinstance(user_data, dict):
        return ""

    return str(
        user_data.get("_id")
        or user_data.get("userID")
        or user_data.get("userId")
        or user_data.get("user_id")
        or user_data.get("id")
        or ""
    ).strip()


def _owner_key(
    user_data: Any = None,
    owner_id: str = "",
    transaction_id: str = "",
) -> str:
    owner = str(owner_id or "").strip()
    if owner:
        return owner

    owner = extract_user_id(user_data)
    if owner:
        return owner

    tx = str(transaction_id or "").strip()
    if tx:
        return f"tx:{tx}"

    return ""


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def _migrate_legacy_single_row_if_needed(conn: sqlite3.Connection) -> None:
    """Migrate the old active_flow id=1 table into active_flows once.

    The old single-row table must be cleared after migration. Otherwise, when
    active_flows becomes empty later, the stale legacy row can be migrated again
    and resurrect a completed transaction.
    """

    if not _table_exists(conn, "active_flow"):
        return

    try:
        row = conn.execute(
            """
            SELECT *
            FROM active_flow
            WHERE id = 1
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        row = None

    if row is None:
        return

    data = dict(row)

    stage = str(data.get("stage") or "").strip()
    user_data = _json_load(data.get("user_json") or "{}")
    selected_product = _json_load(data.get("product_json") or "{}")
    transaction_id = str(data.get("transaction_id") or "").strip()
    extra = _json_load(data.get("extra_json") or "{}")
    updated_at = str(data.get("updated_at") or _now_text()).strip()

    owner_id = _owner_key(
        user_data=user_data,
        transaction_id=transaction_id,
    )

    existing_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM active_flows
        """
    ).fetchone()

    should_migrate = (
        int(existing_count["count"] or 0) == 0
        and owner_id
        and stage
        and transaction_id
    )

    if should_migrate:
        conn.execute(
            """
            INSERT OR REPLACE INTO active_flows (
                owner_id,
                transaction_id,
                stage,
                user_json,
                product_json,
                extra_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                transaction_id,
                stage,
                _json_dump(user_data),
                _json_dump(selected_product),
                _json_dump(extra),
                updated_at,
                updated_at,
            ),
        )

        print(
            f"[FLOW STATE] Migrated legacy active_flow row to per-user active_flows. "
            f"owner={owner_id} stage={stage} tx={transaction_id}",
            flush=True,
        )

    try:
        conn.execute("DELETE FROM active_flow WHERE id = 1")
        print("[FLOW STATE] Cleared legacy active_flow row after migration check.", flush=True)
    except Exception as e:
        print(f"[FLOW STATE] Failed to clear legacy active_flow row: {e}", flush=True)


def init_flow_db() -> None:
    with _lock:
        conn = _connect()

        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_flows (
                    owner_id TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    user_json TEXT DEFAULT '{}',
                    product_json TEXT DEFAULT '{}',
                    extra_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_active_flows_transaction
                ON active_flows(transaction_id)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_active_flows_updated
                ON active_flows(updated_at)
                """
            )

            _migrate_legacy_single_row_if_needed(conn)

            conn.commit()

        finally:
            conn.close()


def _row_to_flow(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None

    data = dict(row)
    selected_product = _json_load(data.get("product_json") or "{}")

    return {
        "owner_id": data.get("owner_id") or "",
        "stage": data.get("stage") or "",
        "user_data": _json_load(data.get("user_json") or "{}"),
        "selected_product": selected_product,
        "product": selected_product,
        "transaction_id": data.get("transaction_id") or "",
        "extra": _json_load(data.get("extra_json") or "{}"),
        "created_at": data.get("created_at") or "",
        "updated_at": data.get("updated_at") or "",
    }


def save_active_flow(
    stage: str,
    user_data: Any = None,
    selected_product: Any = None,
    transaction_id: str | None = None,
    extra: Any = None,
) -> None:
    init_flow_db()

    stage = str(stage or "").strip()
    transaction_id = str(transaction_id or "").strip()
    user_data = user_data or {}
    selected_product = selected_product or {}
    extra = extra or {}

    owner_id = _owner_key(
        user_data=user_data,
        transaction_id=transaction_id,
    )

    if not owner_id:
        print("[FLOW STATE] Not saving active flow because owner_id is missing.", flush=True)
        return

    if not stage or not transaction_id:
        print(
            f"[FLOW STATE] Not saving active flow because stage/transaction_id is missing. "
            f"stage={stage} tx={transaction_id}",
            flush=True,
        )
        return

    now = _now_text()

    with _lock:
        conn = _connect()

        try:
            existing = conn.execute(
                """
                SELECT created_at
                FROM active_flows
                WHERE owner_id = ?
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()

            created_at = str(existing["created_at"]) if existing else now

            conn.execute(
                """
                INSERT INTO active_flows (
                    owner_id,
                    transaction_id,
                    stage,
                    user_json,
                    product_json,
                    extra_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    transaction_id = excluded.transaction_id,
                    stage = excluded.stage,
                    user_json = excluded.user_json,
                    product_json = excluded.product_json,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    transaction_id,
                    stage,
                    _json_dump(user_data),
                    _json_dump(selected_product),
                    _json_dump(extra),
                    created_at,
                    now,
                ),
            )

            conn.commit()

            print(
                f"[FLOW STATE] Saved active flow owner={owner_id} stage={stage} tx={transaction_id}",
                flush=True,
            )

        finally:
            conn.close()


def get_active_flow(
    user_data: Any = None,
    owner_id: str = "",
    transaction_id: str = "",
) -> dict[str, Any] | None:
    """Return an active flow.

    Preferred:
      get_active_flow(user_data=user_data)

    Backward-compatible:
      get_active_flow() returns the latest active flow only for old callers.
    """

    init_flow_db()

    owner = _owner_key(
        user_data=user_data,
        owner_id=owner_id,
    )

    tx = str(transaction_id or "").strip()

    with _lock:
        conn = _connect()

        try:
            if owner:
                row = conn.execute(
                    """
                    SELECT *
                    FROM active_flows
                    WHERE owner_id = ?
                    LIMIT 1
                    """,
                    (owner,),
                ).fetchone()

                return _row_to_flow(row)

            if tx:
                row = conn.execute(
                    """
                    SELECT *
                    FROM active_flows
                    WHERE transaction_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (tx,),
                ).fetchone()

                return _row_to_flow(row)

            row = conn.execute(
                """
                SELECT *
                FROM active_flows
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()

            return _row_to_flow(row)

        finally:
            conn.close()


def get_active_flow_for_user(user_data: Any) -> dict[str, Any] | None:
    return get_active_flow(user_data=user_data)


def list_active_flows() -> list[dict[str, Any]]:
    init_flow_db()

    with _lock:
        conn = _connect()

        try:
            rows = conn.execute(
                """
                SELECT *
                FROM active_flows
                ORDER BY updated_at DESC
                """
            ).fetchall()

            return [flow for flow in (_row_to_flow(row) for row in rows) if flow]

        finally:
            conn.close()


def clear_active_flow(
    user_data: Any = None,
    owner_id: str = "",
    transaction_id: str = "",
    clear_all: bool = False,
) -> None:
    """Clear a finished active flow.

    Safety rule:
    - Do not call this without user_data, owner_id, transaction_id, or clear_all.
    - A plain clear_active_flow() no longer deletes another user's unfinished flow.
    - Matching legacy active_flow rows are also cleared so completed flows cannot
      be migrated back later.
    """

    init_flow_db()

    owner = _owner_key(
        user_data=user_data,
        owner_id=owner_id,
    )

    tx = str(transaction_id or "").strip()

    with _lock:
        conn = _connect()

        try:
            if clear_all:
                conn.execute("DELETE FROM active_flows")

                if _table_exists(conn, "active_flow"):
                    conn.execute("DELETE FROM active_flow")

                conn.commit()
                print("[FLOW STATE] Cleared ALL active flows.", flush=True)
                return

            if owner:
                conn.execute(
                    """
                    DELETE FROM active_flows
                    WHERE owner_id = ?
                    """,
                    (owner,),
                )

                if _table_exists(conn, "active_flow"):
                    if tx:
                        conn.execute(
                            """
                            DELETE FROM active_flow
                            WHERE transaction_id = ?
                               OR user_json LIKE ?
                            """,
                            (
                                tx,
                                f"%{owner}%",
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            DELETE FROM active_flow
                            WHERE user_json LIKE ?
                            """,
                            (f"%{owner}%",),
                        )

                conn.commit()
                print(f"[FLOW STATE] Cleared active flow owner={owner}", flush=True)
                return

            if tx:
                conn.execute(
                    """
                    DELETE FROM active_flows
                    WHERE transaction_id = ?
                    """,
                    (tx,),
                )

                if _table_exists(conn, "active_flow"):
                    conn.execute(
                        """
                        DELETE FROM active_flow
                        WHERE transaction_id = ?
                        """,
                        (tx,),
                    )

                conn.commit()
                print(f"[FLOW STATE] Cleared active flow tx={tx}", flush=True)
                return

            print(
                "[FLOW STATE] Refused unsafe clear_active_flow() without user/transaction. "
                "Pass user_data=..., transaction_id=..., or clear_all=True.",
                flush=True,
            )

        finally:
            conn.close()