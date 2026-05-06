from pathlib import Path
from backend.util.api_client import upload_receipt, upload_session_images


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
        "receipt": None,
        "images": None,
    }

    try:
        receipt_res = upload_receipt(user_id, timestamp, receipt_data)
        result["receipt"] = {
            "ok": receipt_res.ok,
            "status_code": receipt_res.status_code,
            "text": receipt_res.text,
        }
        print(f"[SYNC] Receipt upload status: {receipt_res.status_code}", flush=True)
    except Exception as e:
        result["receipt"] = {
            "ok": False,
            "error": str(e),
        }
        print(f"[SYNC] Receipt upload failed: {e}", flush=True)

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
            else:
                result["images"] = image_results
                print(f"[SYNC] Image upload results: {image_results}", flush=True)
        else:
            result["images"] = image_results
            print(f"[SYNC] Image upload results: {image_results}", flush=True)

    except Exception as e:
        result["images"] = {
            "ok": False,
            "error": str(e),
        }
        print(f"[SYNC] Image upload batch failed: {e}", flush=True)

    return result