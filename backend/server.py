import json
from pathlib import Path
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Confidex Local Pi API")

ROOT = Path("/home/code200/Confidex_GUI")
CAPTURES_DIR = ROOT / "captures"


def get_receipt_path(user_id: str, timestamp: str) -> Path:
    return CAPTURES_DIR / user_id / timestamp / "receipt.json"


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Confidex Local Pi API",
        "captures_dir": str(CAPTURES_DIR),
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/receipt/{user_id}/{timestamp}")
def get_receipt(user_id: str, timestamp: str):
    """
    Return receipt.json for a specific user and timestamp.

    Example:
    /receipt/69b4259c191db5be838a8d9f/20260321_000754
    """
    receipt_path = get_receipt_path(user_id, timestamp)

    if not receipt_path.exists():
        raise HTTPException(status_code=404, detail="Receipt not found")

    try:
        with receipt_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Receipt JSON is invalid")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read receipt: {e}")


@app.get("/receipt/latest/{user_id}")
def get_latest_receipt(user_id: str):
    """
    Return the latest receipt.json for a specific user.
    """
    user_dir = CAPTURES_DIR / user_id

    if not user_dir.exists() or not user_dir.is_dir():
        raise HTTPException(status_code=404, detail="User folder not found")

    timestamp_dirs = sorted(
        [p for p in user_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )

    if not timestamp_dirs:
        raise HTTPException(status_code=404, detail="No receipt sessions found")

    for ts_dir in timestamp_dirs:
        receipt_path = ts_dir / "receipt.json"
        if receipt_path.exists():
            try:
                with receipt_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                return {
                    "user_id": user_id,
                    "timestamp": ts_dir.name,
                    "receipt": data,
                }
            except json.JSONDecodeError:
                continue
            except Exception:
                continue

    raise HTTPException(status_code=404, detail="No valid receipt found for user")


@app.get("/receipt/list/{user_id}")
def list_receipts(user_id: str):
    """
    List available receipt timestamps for a user.
    """
    user_dir = CAPTURES_DIR / user_id

    if not user_dir.exists() or not user_dir.is_dir():
        raise HTTPException(status_code=404, detail="User folder not found")

    results = []

    timestamp_dirs = sorted(
        [p for p in user_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )

    for ts_dir in timestamp_dirs:
        receipt_path = ts_dir / "receipt.json"
        results.append({
            "timestamp": ts_dir.name,
            "has_receipt": receipt_path.exists(),
        })

    return {
        "user_id": user_id,
        "receipts": results,
    }