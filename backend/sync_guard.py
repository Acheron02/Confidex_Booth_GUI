"""Local sync guard for CONFIDEX booth.

Purpose:
- Prevent an old Raspberry Pi backlog from being replayed into a new website DB.
- Allow only records created after this patch/install epoch to be automatically synced.

The first time this module runs, it creates:
    data/current_sync_epoch.json

Anything older than that epoch is treated as historical/local archive data and is
skipped unless CONFIDEX_ALLOW_LEGACY_BACKFILL=1 is explicitly set.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
EPOCH_PATH = DATA_DIR / "current_sync_epoch.json"

_TRUE_VALUES = {"1", "true", "yes", "y", "on", "allow"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    """Parse common ISO/local timestamp values used by booth files."""
    if value in (None, "", "null", "None"):
        return None

    text = str(value).strip()
    if not text:
        return None

    candidates = [text]

    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")

    # Capture folders and local transaction ids sometimes use compact dates.
    for fmt in (
        "%Y%m%d%H%M%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return _to_utc(datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt))
        except Exception:
            pass

    for item in candidates:
        try:
            return _to_utc(datetime.fromisoformat(item))
        except Exception:
            pass

    return None


def legacy_backfill_allowed() -> bool:
    return str(os.getenv("CONFIDEX_ALLOW_LEGACY_BACKFILL", "")).strip().lower() in _TRUE_VALUES


def get_sync_epoch() -> datetime:
    """Return the install/runtime epoch used to block historical replay."""
    env_epoch = os.getenv("CONFIDEX_SYNC_EPOCH_ISO", "").strip()
    env_dt = parse_datetime(env_epoch)
    if env_dt is not None:
        return env_dt

    try:
        if EPOCH_PATH.exists():
            data = json.loads(EPOCH_PATH.read_text(encoding="utf-8"))
            dt = parse_datetime(data.get("epoch_iso") or data.get("created_at"))
            if dt is not None:
                return dt
    except Exception:
        pass

    dt = _utc_now()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        EPOCH_PATH.write_text(
            json.dumps(
                {
                    "epoch_iso": dt.isoformat(),
                    "created_at": dt.isoformat(),
                    "purpose": "Prevents old Raspberry Pi local backlog from being uploaded to a new website database.",
                    "override": "Set CONFIDEX_ALLOW_LEGACY_BACKFILL=1 only if you intentionally want old local records uploaded.",
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    return dt


def get_sync_epoch_iso() -> str:
    return get_sync_epoch().isoformat()


def is_datetime_before_sync_epoch(value: Any) -> bool:
    if legacy_backfill_allowed():
        return False

    dt = parse_datetime(value)
    if dt is None:
        return False

    return dt < get_sync_epoch()


def is_path_before_sync_epoch(path: Path) -> bool:
    if legacy_backfill_allowed():
        return False

    try:
        return float(path.stat().st_mtime) < get_sync_epoch().timestamp()
    except Exception:
        return False


def first_record_datetime(record: dict[str, Any], keys: tuple[str, ...]) -> datetime | None:
    if not isinstance(record, dict):
        return None

    for key in keys:
        dt = parse_datetime(record.get(key))
        if dt is not None:
            return dt

    return None


def is_record_before_sync_epoch(record: dict[str, Any], keys: tuple[str, ...], fallback_path: Path | None = None) -> bool:
    if legacy_backfill_allowed():
        return False

    dt = first_record_datetime(record or {}, keys)
    if dt is not None:
        return dt < get_sync_epoch()

    if fallback_path is not None:
        return is_path_before_sync_epoch(fallback_path)

    return False
