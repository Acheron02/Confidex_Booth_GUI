"""Offline receipt/image retry sync for the CONFIDEX booth.

This worker lets the kiosk continue when the website connection is down.
The booth saves receipts/images locally in captures/<user>/<timestamp>/.
This module periodically retries those local uploads.

Fixes included:
- No repeated CLEAR event spam.
- CLEAR is sent only once after an actual previous pending/error state.
- Already synced sessions are skipped.
- Missing images at receipt stage do not create false errors.
- Historical/legacy capture folders are marked once and no longer spam logs.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from backend.system_events import report_warning, clear_visible_events
from backend.util.api_client import upload_receipt, upload_session_images
from backend.sync_guard import (
    get_sync_epoch_iso,
    is_path_before_sync_epoch,
    is_record_before_sync_epoch,
)

try:
    from backend.offline_cash_sync import sync_offline_cash_transactions_once
except Exception:
    sync_offline_cash_transactions_once = None


ROOT = Path(__file__).resolve().parents[1]
CAPTURES_DIR = ROOT / "captures"
STATUS_FILENAME = "offline_sync_status.json"

_started = False
_lock = threading.RLock()

# Process-wide warning state. This prevents repeated CLEAR spam.
_warning_active = False
_last_clear_at = 0.0
_last_warning_at = 0.0

# Legacy skip logging should be summarized, not printed per folder forever.
_legacy_marked_since_notice = 0
_legacy_already_skipped_since_notice = 0
_last_legacy_notice_at = 0.0
LEGACY_NOTICE_INTERVAL_SECONDS = 300


def _now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(path)


def _mark_warning_active() -> None:
    global _warning_active
    _warning_active = True


def _report_pending_warning(title: str, message: str, details: Any = None) -> None:
    global _last_warning_at

    _mark_warning_active()

    now = time.time()
    if now - _last_warning_at < 30:
        visible = False
    else:
        visible = True
        _last_warning_at = now

    report_warning(
        "offline_flow_sync",
        title,
        message,
        details=details,
        visible=visible,
    )


def _clear_offline_notice_once(reason: str = "offline_flow_synced") -> None:
    """Clear the visible warning only once after a real pending/error episode."""
    global _warning_active, _last_clear_at

    if not _warning_active:
        return

    now = time.time()
    if now - _last_clear_at < 60:
        _warning_active = False
        return

    _warning_active = False
    _last_clear_at = now

    try:
        clear_visible_events(
            source="offline_flow_sync",
            title="Offline Upload Resolved",
            message="Pending local receipt/image data has been uploaded successfully.",
            details={"reason": reason},
        )
    except Exception as exc:
        print(f"[OFFLINE FLOW] Failed to clear notice: {exc}", flush=True)


def _image_results_all_ok(image_results: Any) -> bool:
    if not isinstance(image_results, dict):
        return False

    if image_results.get("ok") is False or image_results.get("error"):
        return False

    if image_results.get("skipped") is True:
        return True

    checked = False

    for item in image_results.values():
        if isinstance(item, dict):
            checked = True
            if item.get("ok") is False or item.get("error"):
                return False

    return checked


def _has_uploadable_images(session_dir: Path) -> bool:
    allowed = {".png", ".jpg", ".jpeg", ".webp"}

    try:
        return any(
            p.is_file()
            and p.suffix.lower() in allowed
            and p.name != STATUS_FILENAME
            for p in session_dir.iterdir()
        )
    except Exception:
        return False


def _extract_session_fields(session_dir: Path, receipt: dict) -> dict[str, str]:
    user = receipt.get("user") if isinstance(receipt.get("user"), dict) else {}
    purchase = receipt.get("purchase") if isinstance(receipt.get("purchase"), dict) else {}
    product = receipt.get("product") if isinstance(receipt.get("product"), dict) else {}

    return {
        "user_id": str(user.get("user_id") or session_dir.parent.name or "").strip(),
        "timestamp": str(purchase.get("timestamp_folder") or session_dir.name or "").strip(),
        "product_id": str(product.get("product_id") or product.get("productID") or "").strip(),
        "transaction_id": str(receipt.get("transaction_id") or "").strip(),
    }


def _record_legacy_skip_notice(marked_now: bool = False, already_skipped: bool = False) -> None:
    """Print one compact legacy-skip summary occasionally."""
    global _legacy_marked_since_notice
    global _legacy_already_skipped_since_notice
    global _last_legacy_notice_at

    if marked_now:
        _legacy_marked_since_notice += 1

    if already_skipped:
        _legacy_already_skipped_since_notice += 1

    now = time.time()

    if now - _last_legacy_notice_at < LEGACY_NOTICE_INTERVAL_SECONDS:
        return

    if _legacy_marked_since_notice <= 0 and _legacy_already_skipped_since_notice <= 0:
        return

    print(
        "[OFFLINE FLOW] Legacy capture cleanup summary: "
        f"newly_marked={_legacy_marked_since_notice}, "
        f"already_skipped={_legacy_already_skipped_since_notice}. "
        "Historical Raspberry Pi backlog will not be uploaded.",
        flush=True,
    )

    _legacy_marked_since_notice = 0
    _legacy_already_skipped_since_notice = 0
    _last_legacy_notice_at = now


def _mark_legacy_session_skipped(session_dir: Path, reason: str = "legacy_capture_session") -> bool:
    """Mark an old capture session as already handled so it is not uploaded.

    Returns True only when the status file was newly marked in this call.
    Returns False when it was already marked before.
    """
    status_path = session_dir / STATUS_FILENAME
    status = _read_json(status_path, default={}) or {}

    if status.get("legacy_skipped") is True:
        _record_legacy_skip_notice(already_skipped=True)
        return False

    status.update(
        {
            "receipt_done": True,
            "images_done": True,
            "legacy_skipped": True,
            "legacy_skipped_at_epoch": get_sync_epoch_iso(),
            "legacy_skip_reason": (
                "Historical local capture session intentionally skipped to prevent "
                "bulk upload into the current website database."
            ),
            "legacy_skip_source": reason,
            "updated_at": _now_text(),
        }
    )

    try:
        _write_json(status_path, status)
        _record_legacy_skip_notice(marked_now=True)
        return True
    except Exception as exc:
        print(f"[OFFLINE FLOW] Failed marking legacy session skipped {session_dir}: {exc}", flush=True)
        return False


def _is_legacy_session(session_dir: Path, receipt: dict | None = None) -> bool:
    """Return True when an unhandled capture session existed before the sync epoch."""
    status = _read_json(session_dir / STATUS_FILENAME, default={}) or {}

    # Already-marked legacy sessions are considered fully handled, not "legacy
    # needing action". This avoids repeated per-folder skip logs every sync pass.
    if status.get("legacy_skipped") is True:
        return False

    if receipt is None:
        receipt = _read_json(session_dir / "receipt.json", default={}) or {}

    if is_record_before_sync_epoch(
        receipt or {},
        (
            "created_at",
            "createdAt",
            "purchasedDate",
            "purchaseDate",
            "testedDate",
            "timestamp",
        ),
        fallback_path=session_dir / "receipt.json",
    ):
        return True

    return is_path_before_sync_epoch(session_dir / "receipt.json")


def _is_session_fully_synced(session_dir: Path) -> bool:
    status = _read_json(session_dir / STATUS_FILENAME, default={}) or {}

    if status.get("legacy_skipped") is True:
        _record_legacy_skip_notice(already_skipped=True)
        return True

    receipt_done = status.get("receipt_done") is True

    if _has_uploadable_images(session_dir):
        images_done = status.get("images_done") is True
    else:
        images_done = True

    return receipt_done and images_done


def _iter_receipt_sessions(max_sessions: int = 30) -> list[Path]:
    if not CAPTURES_DIR.exists():
        return []

    sessions: list[Path] = []

    try:
        for receipt_path in CAPTURES_DIR.glob("*/*/receipt.json"):
            session_dir = receipt_path.parent

            # Already handled / legacy-skipped folders should disappear silently.
            if _is_session_fully_synced(session_dir):
                continue

            receipt = _read_json(receipt_path, default={}) or {}

            if _is_legacy_session(session_dir, receipt=receipt):
                _mark_legacy_session_skipped(
                    session_dir,
                    reason="before_current_sync_epoch",
                )
                continue

            sessions.append(session_dir)

    except Exception as exc:
        print(f"[OFFLINE FLOW] Failed scanning captures: {exc}", flush=True)

    sessions.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return sessions[: max(1, int(max_sessions or 1))]


def sync_offline_flow_once(max_sessions: int = 20) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "checked": 0,
        "receipt_synced": 0,
        "images_synced": 0,
        "pending": 0,
        "errors": [],
    }

    had_problem_this_pass = False

    with _lock:
        for session_dir in _iter_receipt_sessions(max_sessions=max_sessions):
            summary["checked"] += 1

            receipt_path = session_dir / "receipt.json"
            status_path = session_dir / STATUS_FILENAME

            receipt = _read_json(receipt_path, default={}) or {}
            status = _read_json(status_path, default={}) or {}

            if not isinstance(receipt, dict) or not receipt:
                continue

            fields = _extract_session_fields(session_dir, receipt)
            user_id = fields["user_id"]
            timestamp = fields["timestamp"]
            product_id = fields["product_id"]
            transaction_id = fields["transaction_id"]

            status.setdefault("session_dir", str(session_dir))
            status.setdefault("user_id", user_id)
            status.setdefault("timestamp", timestamp)
            status.setdefault("product_id", product_id)
            status.setdefault("transaction_id", transaction_id)

            try:
                if (
                    transaction_id.startswith("LOCAL-CASH-")
                    and sync_offline_cash_transactions_once is not None
                ):
                    sync_offline_cash_transactions_once(
                        max_records=3,
                        target_transaction_id=transaction_id,
                    )

                if status.get("receipt_done") is not True:
                    res = upload_receipt(user_id, timestamp, receipt)

                    status["last_receipt_status_code"] = getattr(res, "status_code", None)
                    status["last_receipt_text"] = getattr(res, "text", "")[:1500]

                    if getattr(res, "ok", False):
                        status["receipt_done"] = True
                        status["receipt_synced_at"] = _now_text()
                        status["last_error"] = ""
                        summary["receipt_synced"] += 1
                    else:
                        status["receipt_done"] = False
                        status["last_error"] = (
                            f"Receipt upload failed: "
                            f"{getattr(res, 'status_code', 'unknown')}"
                        )
                        summary["pending"] += 1
                        had_problem_this_pass = True
                        _write_json(status_path, status)

                        _report_pending_warning(
                            "Receipt Upload Pending",
                            "Website connection is unavailable. The receipt remains saved locally and will be retried.",
                            details=status.get("last_error"),
                        )
                        continue

                if _has_uploadable_images(session_dir):
                    if status.get("images_done") is not True:
                        image_results = upload_session_images(
                            user_id=user_id,
                            timestamp=timestamp,
                            session_dir=session_dir,
                            product_id=product_id,
                            transaction_id=transaction_id,
                        )

                        status["last_image_results"] = image_results

                        if _image_results_all_ok(image_results):
                            status["images_done"] = True
                            status["images_synced_at"] = _now_text()
                            status["last_error"] = ""
                            summary["images_synced"] += 1
                        else:
                            status["images_done"] = False
                            status["last_error"] = f"Image upload failed: {image_results}"
                            summary["pending"] += 1
                            had_problem_this_pass = True
                            _write_json(status_path, status)

                            _report_pending_warning(
                                "Image Upload Pending",
                                "One or more local images could not be uploaded. They will be retried.",
                                details=image_results,
                            )
                            continue
                else:
                    status["images_done"] = False
                    status["images_skipped_reason"] = "No images available yet."

                status["updated_at"] = _now_text()
                _write_json(status_path, status)

            except Exception as exc:
                summary["errors"].append(str(exc))
                summary["pending"] += 1
                had_problem_this_pass = True

                status["last_error"] = str(exc)
                status["updated_at"] = _now_text()
                _write_json(status_path, status)

                _report_pending_warning(
                    "Offline Upload Pending",
                    "A local receipt/image sync attempt failed. The booth will retry automatically.",
                    details=str(exc),
                )

    if summary["pending"] == 0 and not summary["errors"] and not had_problem_this_pass:
        _clear_offline_notice_once("sync_pass_clean")

    return summary


def _worker_loop(interval_seconds: int, max_sessions: int) -> None:
    while True:
        try:
            summary = sync_offline_flow_once(max_sessions=max_sessions)

            # Only print when the worker actually did something or found a problem.
            # This removes constant "checked: 0" noise.
            if (
                summary.get("checked", 0) > 0
                or summary.get("pending", 0) > 0
                or summary.get("errors")
                or summary.get("receipt_synced", 0) > 0
                or summary.get("images_synced", 0) > 0
            ):
                print(f"[OFFLINE FLOW] Sync pass: {summary}", flush=True)

        except Exception as exc:
            _mark_warning_active()
            print(f"[OFFLINE FLOW] Worker error: {exc}", flush=True)
            _report_pending_warning(
                "Offline Upload Worker Error",
                "The local receipt/image sync worker encountered an error. It will retry automatically.",
                details=str(exc),
            )

        time.sleep(max(10, int(interval_seconds or 45)))


def start_offline_flow_sync(
    interval_seconds: int = 45,
    initial_delay_seconds: int = 9,
    max_sessions: int = 25,
) -> bool:
    global _started

    if _started:
        return True

    _started = True

    def runner() -> None:
        if initial_delay_seconds > 0:
            time.sleep(float(initial_delay_seconds))

        _worker_loop(
            interval_seconds=interval_seconds,
            max_sessions=max_sessions,
        )

    threading.Thread(
        target=runner,
        name="OfflineFlowSync",
        daemon=True,
    ).start()

    print("[OFFLINE FLOW] Background receipt/image sync started", flush=True)
    return True