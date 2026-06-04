"""Offline cash transaction sync helpers for CONFIDEX booth.

Cash payments are physical payments. If the website transaction endpoint is
unreachable right after cash is accepted, the GUI continues using a
LOCAL-CASH-* transaction id and stores a local outbox file. This module
retries those outbox files so the website transaction collection eventually
receives the transaction record too.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.util import api_client
from backend.sync_guard import get_sync_epoch_iso, is_record_before_sync_epoch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
USER_HOME_ROOT = PROJECT_ROOT.parent

# New/correct location plus the earlier fallback location used by older
# patched builds. The log showed /home/code200/data/offline_cash_transactions,
# so we must keep scanning that path to recover existing pending payments.
OFFLINE_DIRS = [
    PROJECT_ROOT / "data" / "offline_cash_transactions",
    USER_HOME_ROOT / "data" / "offline_cash_transactions",
]

_thread_lock = threading.Lock()
_worker_started = False
_sync_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[OFFLINE TX] Failed to read {path}: {exc}", flush=True)
        return None


def _safe_json_write(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _iter_pending_files(target_transaction_id: str | None = None) -> list[Path]:
    target = str(target_transaction_id or "").strip()
    seen: set[Path] = set()
    files: list[Path] = []

    for directory in OFFLINE_DIRS:
        try:
            if not directory.exists() or not directory.is_dir():
                continue

            for path in sorted(directory.glob("*.json")):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)

                if target and path.stem != target:
                    # Some files may include a local_transaction_id inside the
                    # JSON but a slightly different file name. Keep checking
                    # those by reading later only when no target filter exists.
                    continue

                files.append(path)
        except Exception as exc:
            print(f"[OFFLINE TX] Failed to scan {directory}: {exc}", flush=True)

    if target and not files:
        # Fallback: read all json files to find a matching id inside the file.
        for directory in OFFLINE_DIRS:
            try:
                if not directory.exists() or not directory.is_dir():
                    continue
                for path in sorted(directory.glob("*.json")):
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    data = _safe_json_load(path) or {}
                    ids = {
                        str(data.get("local_transaction_id") or "").strip(),
                        str(data.get("transaction_id") or "").strip(),
                    }
                    if target in ids:
                        seen.add(resolved)
                        files.append(path)
            except Exception:
                pass

    return files


def _extract_transaction_id(response_data: dict[str, Any]) -> str:
    transaction_obj = response_data.get("transaction") or {}
    if isinstance(transaction_obj, dict):
        value = (
            transaction_obj.get("_id")
            or transaction_obj.get("transaction_id")
            or transaction_obj.get("id")
        )
        if value:
            return str(value)

    value = (
        response_data.get("_id")
        or response_data.get("transaction_id")
        or response_data.get("id")
    )
    return str(value or "").strip()


def _build_transaction_payload(record: dict[str, Any]) -> dict[str, Any]:
    local_id = str(
        record.get("local_transaction_id")
        or record.get("transaction_id")
        or ""
    ).strip()

    source_payload = record.get("transaction_data") or {}
    if not isinstance(source_payload, dict):
        source_payload = {}

    payload = dict(source_payload)
    payload.setdefault("status", "completed")
    payload.setdefault("items", [])

    # Keep both user-facing and reconciliation fields. Unknown fields are safe
    # for most JSON endpoints; if the website ignores them, the transaction
    # is still created. If it stores them, admins can link it back to the
    # receipt/result uploaded under LOCAL-CASH-*.
    if local_id:
        payload["transaction_id"] = local_id
        payload["local_transaction_id"] = local_id
        payload["offline_local_transaction_id"] = local_id
        payload["payment_reference"] = payload.get("payment_reference") or local_id

    payload["payment_method"] = payload.get("payment_method") or "cash"
    payload["payment_status"] = payload.get("payment_status") or "completed"
    payload["offline_synced_from_booth"] = True

    created_at = str(record.get("created_at") or _utc_now_iso())
    if not payload.get("purchasedDate"):
        payload["purchasedDate"] = created_at

    # Preserve cash accounting in the transaction payload.
    for key in ("cash", "total_paid", "change", "total"):
        if key in record and record.get(key) is not None:
            payload[key] = record.get(key)

    return payload


def sync_offline_cash_transactions_once(
    max_records: int = 20,
    target_transaction_id: str | None = None,
) -> dict[str, Any]:
    """Try to sync pending offline cash transactions once.

    Returns a small summary. This function is intentionally non-throwing so
    it can be called from receipt/image sync and background threads.
    """
    with _sync_lock:
        summary: dict[str, Any] = {
            "checked": 0,
            "synced": 0,
            "failed": 0,
            "skipped": 0,
            "records": [],
        }

        files = _iter_pending_files(target_transaction_id=target_transaction_id)
        if max_records and max_records > 0:
            files = files[:max_records]

        for path in files:
            summary["checked"] += 1
            record = _safe_json_load(path)
            if not record:
                summary["failed"] += 1
                continue

            local_id = str(
                record.get("local_transaction_id")
                or record.get("transaction_id")
                or path.stem
            ).strip()

            current_status = str(record.get("status") or "").lower()

            if current_status == "synced":
                summary["skipped"] += 1
                summary["records"].append({"id": local_id, "status": "already_synced"})
                continue

            if current_status == "legacy_skipped":
                summary["skipped"] += 1
                summary["records"].append({"id": local_id, "status": "legacy_skipped"})
                continue

            if is_record_before_sync_epoch(
                record,
                (
                    "created_at",
                    "createdAt",
                    "purchasedDate",
                    "purchaseDate",
                    "last_attempt_at",
                ),
                fallback_path=path,
            ):
                record["status"] = "legacy_skipped"
                record["legacy_skipped_at_epoch"] = get_sync_epoch_iso()
                record["legacy_skip_reason"] = (
                    "Historical offline cash transaction intentionally skipped to prevent "
                    "bulk upload into the current website database."
                )
                try:
                    _safe_json_write(path, record)
                except Exception:
                    pass

                summary["skipped"] += 1
                summary["records"].append({"id": local_id, "status": "legacy_skipped"})
                print(
                    f"[OFFLINE TX] Skipped legacy offline cash transaction {local_id}; "
                    "not uploading historical Raspberry Pi backlog.",
                    flush=True,
                )
                continue

            try:
                payload = _build_transaction_payload(record)
                response = api_client.post_transaction(payload)
                text = getattr(response, "text", "")

                if not getattr(response, "ok", False):
                    raise RuntimeError(f"HTTP {getattr(response, 'status_code', 'ERR')}: {text[:500]}")

                try:
                    response_data = response.json()
                except Exception:
                    response_data = {}

                website_transaction_id = _extract_transaction_id(response_data)

                record["status"] = "synced"
                record["synced_at"] = _utc_now_iso()
                record["website_transaction_id"] = website_transaction_id
                record["last_sync_error"] = ""
                record["last_response_status"] = getattr(response, "status_code", None)
                record["last_response_text"] = text[:2000]

                _safe_json_write(path, record)

                summary["synced"] += 1
                summary["records"].append(
                    {
                        "id": local_id,
                        "status": "synced",
                        "website_transaction_id": website_transaction_id,
                    }
                )
                print(
                    f"[OFFLINE TX] Synced offline cash transaction {local_id} "
                    f"-> {website_transaction_id or 'website accepted'}",
                    flush=True,
                )

            except Exception as exc:
                record["status"] = "offline_pending_sync"
                record["last_attempt_at"] = _utc_now_iso()
                record["last_sync_error"] = str(exc)
                try:
                    _safe_json_write(path, record)
                except Exception:
                    pass

                summary["failed"] += 1
                summary["records"].append(
                    {"id": local_id, "status": "failed", "error": str(exc)}
                )
                print(
                    f"[OFFLINE TX] Pending offline cash transaction {local_id} not synced yet: {exc}",
                    flush=True,
                )

        return summary


def trigger_offline_cash_sync_async(target_transaction_id: str | None = None) -> None:
    def worker():
        sync_offline_cash_transactions_once(
            max_records=5,
            target_transaction_id=target_transaction_id,
        )

    threading.Thread(
        target=worker,
        name="OfflineCashSyncOnce",
        daemon=True,
    ).start()


def start_offline_cash_sync(interval_seconds: int = 30, initial_delay_seconds: int = 5) -> None:
    global _worker_started

    with _thread_lock:
        if _worker_started:
            return
        _worker_started = True

    def worker():
        if initial_delay_seconds > 0:
            time.sleep(initial_delay_seconds)

        while True:
            try:
                sync_offline_cash_transactions_once(max_records=20)
            except Exception as exc:
                print(f"[OFFLINE TX] Background sync error: {exc}", flush=True)

            time.sleep(max(10, int(interval_seconds or 30)))

    threading.Thread(
        target=worker,
        name="OfflineCashSyncWorker",
        daemon=True,
    ).start()
    print("[OFFLINE TX] Background offline cash sync started", flush=True)
