"""Central event/error reporting for the Confidex booth GUI.

This module writes all reported events to SQLite + a JSONL log file and also
pushes visible errors/warnings to an in-memory queue. main.py polls that queue
and displays important issues on the kiosk screen.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DB_PATH = DATA_DIR / "booth_events.sqlite3"
LOG_PATH = LOG_DIR / "booth_events.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

_event_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
_db_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_event_db() -> None:
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS booth_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT DEFAULT '',
                    visible INTEGER NOT NULL DEFAULT 1,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _details_to_text(details: Any) -> str:
    if details is None:
        return ""
    if isinstance(details, str):
        return details
    try:
        return json.dumps(details, ensure_ascii=False, default=str)
    except Exception:
        return str(details)


def report_event(
    severity: str = "info",
    source: str = "system",
    title: str = "System Notice",
    message: str = "",
    details: Any = None,
    visible: bool = True,
) -> dict[str, Any]:
    init_event_db()

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "severity": str(severity or "info").lower(),
        "source": str(source or "system"),
        "title": str(title or "System Notice"),
        "message": str(message or ""),
        "details": _details_to_text(details),
        "visible": bool(visible),
    }

    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO booth_events (
                    created_at, severity, source, title, message, details, visible
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["created_at"],
                    payload["severity"],
                    payload["source"],
                    payload["title"],
                    payload["message"],
                    payload["details"],
                    1 if payload["visible"] else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if payload["visible"]:
        _event_queue.put(payload)

    print(
        f"[BOOTH EVENT] {payload['severity'].upper()} "
        f"{payload['source']}: {payload['title']} - {payload['message']}",
        flush=True,
    )
    return payload


def report_error(source: str, title: str, message: str, details: Any = None, visible: bool = True):
    return report_event("error", source, title, message, details, visible)


def report_warning(source: str, title: str, message: str, details: Any = None, visible: bool = True):
    return report_event("warning", source, title, message, details, visible)


def report_info(source: str, title: str, message: str, details: Any = None, visible: bool = False):
    return report_event("info", source, title, message, details, visible)


def drain_visible_events(max_items: int = 10) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for _ in range(max(1, int(max_items or 1))):
        try:
            items.append(_event_queue.get_nowait())
        except queue.Empty:
            break
    return items
