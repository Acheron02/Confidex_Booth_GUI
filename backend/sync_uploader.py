from pathlib import Path
import json
import time
from backend.util.api_client import upload_receipt, upload_session_images
from backend.system_events import report_warning, clear_visible_events
try:
    from backend.offline_cash_sync import sync_offline_cash_transactions_once
except Exception:
    sync_offline_cash_transactions_once = None




def _status_path(session_dir: Path) -> Path:
    return Path(session_dir) / "offline_sync_status.json"


def _read_status(session_dir: Path) -> dict:
    path = _status_path(session_dir)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_status(session_dir: Path, updates: dict) -> None:
    try:
        path = _status_path(session_dir)
        data = _read_status(session_dir)
        data.update(updates or {})
        data.setdefault("session_dir", str(Path(session_dir)))
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        tmp.replace(path)
    except Exception as exc:
        print(f"[SYNC] Failed to update offline sync status: {exc}", flush=True)

def _clear_sync_warning(reason: str = "sync_recovered") -> None:
    try:
        clear_visible_events(
            source="sync_uploader",
            title="Upload Issue Resolved",
            message="The pending receipt/image upload has completed successfully.",
            details={"reason": reason},
        )
    except Exception as exc:
        print(f"[SYNC] Failed to clear sync warning: {exc}", flush=True)



def _image_results_all_ok(image_results) -> bool:
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


def sync_receipt_and_images(
    user_id: str,
    timestamp: str,
    receipt_data: dict,
    session_dir,
    product_id: str | None = None,
    transaction_id: str | None = None,
):
    session_dir = Path(session_dir)

    result = {
        "offline_transaction_sync": None,
        "receipt": None,
        "images": None,
    }

    local_tx = str(transaction_id or "").strip()
    _write_status(session_dir, {
        "user_id": str(user_id or ""),
        "timestamp": str(timestamp or ""),
        "product_id": str(product_id or ""),
        "transaction_id": local_tx,
    })

    if local_tx.startswith("LOCAL-CASH-") and sync_offline_cash_transactions_once is not None:
        try:
            offline_summary = sync_offline_cash_transactions_once(
                max_records=3,
                target_transaction_id=local_tx,
            )
            result["offline_transaction_sync"] = offline_summary
            print(f"[SYNC] Offline transaction sync before receipt: {offline_summary}", flush=True)
        except Exception as e:
            result["offline_transaction_sync"] = {"ok": False, "error": str(e)}
            print(f"[SYNC] Offline transaction sync before receipt failed: {e}", flush=True)

    try:
        receipt_res = upload_receipt(user_id, timestamp, receipt_data)
        result["receipt"] = {
            "ok": receipt_res.ok,
            "status_code": receipt_res.status_code,
            "text": receipt_res.text,
        }
        print(f"[SYNC] Receipt upload status: {receipt_res.status_code}", flush=True)
        if getattr(receipt_res, "ok", False):
            _write_status(session_dir, {
                "receipt_done": True,
                "receipt_synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "last_receipt_status_code": getattr(receipt_res, "status_code", None),
            })
            _clear_sync_warning("receipt_upload_success")
        else:
            _write_status(session_dir, {
                "receipt_done": False,
                "last_receipt_status_code": getattr(receipt_res, "status_code", None),
                "last_receipt_text": getattr(receipt_res, "text", "")[:1500],
            })
    except Exception as e:
        result["receipt"] = {
            "ok": False,
            "error": str(e),
        }
        print(f"[SYNC] Receipt upload failed: {e}", flush=True)
        _write_status(session_dir, {
            "receipt_done": False,
            "last_receipt_error": str(e),
        })
        report_warning(
            "sync_uploader",
            "Receipt Upload Pending",
            "The receipt could not be uploaded because the website connection is unstable. It remains saved locally.",
            details=str(e),
            visible=True,
        )

    try:
        image_results = upload_session_images(
            user_id=user_id,
            timestamp=timestamp,
            session_dir=session_dir,
            product_id=product_id,
            transaction_id=transaction_id,
        )

        if isinstance(image_results, dict) and image_results.get("error"):
            if "No uploadable image files found" in str(image_results.get("error", "")):
                result["images"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "No images yet at receipt stage",
                }
                print("[SYNC] No images yet; receipt uploaded only", flush=True)
                _write_status(session_dir, {"images_done": False, "images_skipped_reason": "No images yet at receipt stage"})
                _clear_sync_warning("receipt_stage_no_images_pending")
            else:
                result["images"] = image_results
                print(f"[SYNC] Image upload results: {image_results}", flush=True)
        else:
            result["images"] = image_results
            print(f"[SYNC] Image upload results: {image_results}", flush=True)
            if _image_results_all_ok(image_results):
                _write_status(session_dir, {
                    "images_done": True,
                    "images_synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "last_image_results": image_results,
                })
                _clear_sync_warning("image_upload_success")
            else:
                _write_status(session_dir, {
                    "images_done": False,
                    "last_image_results": image_results,
                })

    except Exception as e:
        result["images"] = {
            "ok": False,
            "error": str(e),
        }
        print(f"[SYNC] Image upload batch failed: {e}", flush=True)
        _write_status(session_dir, {
            "images_done": False,
            "last_image_error": str(e),
        })
        report_warning(
            "sync_uploader",
            "Image Upload Pending",
            "Captured images could not be uploaded right now. They remain saved locally and can be retried.",
            details=str(e),
            visible=True,
        )

    return result