import json
from datetime import datetime
from pathlib import Path
import cv2

ROOT = Path(__file__).resolve().parents[2]
CAPTURES_DIR = ROOT / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)

# In-memory session registry for the running app process
_ACTIVE_SESSIONS: dict[str, Path] = {}


def _safe_user(user_id: str) -> str:
    return str(user_id or "unknown").strip() or "unknown"


def create_capture_session(user_id: str, timestamp: str | None = None) -> Path:
    safe_user = _safe_user(user_id)
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = CAPTURES_DIR / safe_user / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def remember_capture_session(user_id: str, session_dir: Path) -> Path:
    safe_user = _safe_user(user_id)
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    _ACTIVE_SESSIONS[safe_user] = session_dir
    return session_dir


def get_remembered_capture_session(user_id: str) -> Path | None:
    return _ACTIVE_SESSIONS.get(_safe_user(user_id))


def get_or_create_capture_session(user_id: str, timestamp: str | None = None) -> Path:
    safe_user = _safe_user(user_id)
    existing = _ACTIVE_SESSIONS.get(safe_user)
    if existing and existing.exists():
        return existing

    session_dir = create_capture_session(safe_user, timestamp=timestamp)
    _ACTIVE_SESSIONS[safe_user] = session_dir
    return session_dir


def clear_capture_session(user_id: str):
    _ACTIVE_SESSIONS.pop(_safe_user(user_id), None)


def get_session_timestamp(session_dir: Path) -> str:
    return Path(session_dir).name


def save_receipt_json(session_dir: Path, receipt_data: dict) -> str:
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    receipt_path = session_dir / "receipt.json"
    with receipt_path.open("w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2, ensure_ascii=False)

    return str(receipt_path)


def save_capture_set(session_dir: Path, raw_frame, annotated_frame, metadata: dict):
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    raw_path = session_dir / "raw.png"
    cv2.imwrite(str(raw_path), raw_frame)

    annotated_path = session_dir / "annotated.png"
    if annotated_frame is not None:
        cv2.imwrite(str(annotated_path), annotated_frame)

    meta_path = session_dir / "result_metadata.json"
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    return str(raw_path), str(annotated_path), str(meta_path)