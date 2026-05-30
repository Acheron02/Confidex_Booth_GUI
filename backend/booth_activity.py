"""Current booth activity state used by background workers.

The kit queue worker and GUI run in the same kiosk process, but this state is
also stored in SQLite so background workers can answer one question safely:

    "May the low-priority RVM trash disposal use the Arduino right now?"

Policy:
- Trash disposal is low priority.
- Trash disposal may continue during pages/tasks that do not need Arduino USB
  serial communication.
- Trash disposal must be deferred before/during pages that use the Arduino for
  bill acceptor relay, coin servos, kit actuators, homing, or change dispense.
- Raspberry Pi persists the queue state; Arduino RAM only remembers an
  interrupted disposal while the board remains powered.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "booth_activity.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()

# Pages that are known to issue Arduino USB-serial commands as part of their
# normal page flow. Keep this list conservative and explicit.
ARDUINO_SERIAL_REQUIRED_PAGES = {
    "CashPaymentPage",        # BILL_ON / BILL_OFF
    "ChangeDispensingPage",   # DISPENSE_CHANGE
    "DispensingPage",         # DISPENSE:KITx / optional RETURN_KITx_HOME
}

# Pseudo page names used by startup/background code.
ARDUINO_SERIAL_REQUIRED_STATES = {
    "StartupHardwareInit",    # BILL_OFF / RESET_SERVOS / HOME_KITS
    "ArduinoHardwareCommand",
}


def page_requires_arduino_serial(page_name: str) -> bool:
    clean = str(page_name or "").strip()
    return clean in ARDUINO_SERIAL_REQUIRED_PAGES or clean in ARDUINO_SERIAL_REQUIRED_STATES


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_booth_activity_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS booth_activity (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    page_name TEXT NOT NULL DEFAULT '',
                    transaction_id TEXT DEFAULT '',
                    busy INTEGER NOT NULL DEFAULT 1,
                    reason TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def set_booth_activity(
    page_name: str,
    transaction_id: str | None = None,
    busy: bool | None = None,
    reason: str = "",
) -> None:
    """Persist whether the current flow needs Arduino serial.

    The historical column name is ``busy`` for compatibility, but in this file
    it specifically means: "background disposal is NOT allowed because this
    state/page requires the Arduino serial link."
    """
    init_booth_activity_db()

    clean_page = str(page_name or "").strip()
    clean_tx = str(transaction_id or "").strip()

    if busy is None:
        busy = page_requires_arduino_serial(clean_page)

    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO booth_activity (
                    id, page_name, transaction_id, busy, reason, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    page_name = excluded.page_name,
                    transaction_id = excluded.transaction_id,
                    busy = excluded.busy,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_page,
                    clean_tx,
                    1 if busy else 0,
                    str(reason or ""),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_booth_activity() -> dict[str, Any]:
    init_booth_activity_db()

    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM booth_activity WHERE id = 1").fetchone()
            if row is None:
                return {
                    "page_name": "",
                    "transaction_id": "",
                    "busy": True,
                    "reason": "No booth activity has been published yet.",
                    "updated_at": "",
                }

            data = dict(row)
            return {
                "page_name": data.get("page_name") or "",
                "transaction_id": data.get("transaction_id") or "",
                "busy": bool(data.get("busy")),
                "reason": data.get("reason") or "",
                "updated_at": data.get("updated_at") or "",
            }
        finally:
            conn.close()


def is_booth_safe_for_background_disposal() -> bool:
    """Return True when background trash disposal may use Arduino lightly.

    This does not mean the booth is on WelcomePage. It means the active page is
    not expected to use the Arduino USB-serial link. If a hardware command does
    arrive while disposal is active, the revised Arduino firmware defers trash
    immediately before processing that command.
    """
    state = get_booth_activity()
    return not bool(state.get("busy"))


def is_booth_idle_for_disposal() -> bool:
    """Backward-compatible alias used by older patches."""
    return is_booth_safe_for_background_disposal()
