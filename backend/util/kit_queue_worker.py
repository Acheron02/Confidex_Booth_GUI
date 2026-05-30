import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from config_manager import config
from backend.system_events import report_error, report_warning
from backend.util import api_client
from backend.util.capture_manager import save_capture_set
from backend.util.dispenser_serial import (
    send_dispose_kit_command,
    get_dispose_status,
    stop_all,
)
from backend.booth_activity import get_booth_activity, is_booth_safe_for_background_disposal

# =====================================================
# YOLO / ULTRALYTICS MODEL
# =====================================================

try:
    from ultralytics import YOLO
except Exception as e:
    YOLO = None
    print(f"[KIT QUEUE] Ultralytics import failed: {e}", flush=True)


YOLO_MODEL = None
YOLO_MODEL_LOCK = threading.RLock()


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "kit_queue.sqlite3"

_worker_instance = None
_db_lock = threading.RLock()

_latest_frame_lock = threading.RLock()
_latest_frame = {
    "frame": None,
    "state": "starting",
    "updated_at": 0.0,
    "message": "Queue worker has not published a frame yet.",
}


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt: datetime):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def publish_queue_frame(frame, state="live", message=""):
    if frame is None:
        return

    with _latest_frame_lock:
        _latest_frame["frame"] = frame.copy()
        _latest_frame["state"] = str(state or "live")
        _latest_frame["updated_at"] = time.time()
        _latest_frame["message"] = str(message or "")


def publish_queue_state(state="idle", message=""):
    with _latest_frame_lock:
        _latest_frame["state"] = str(state or "idle")
        _latest_frame["updated_at"] = time.time()
        _latest_frame["message"] = str(message or "")


def get_latest_queue_frame():
    with _latest_frame_lock:
        frame = _latest_frame.get("frame")

        if frame is None:
            return None

        return {
            "frame": frame.copy(),
            "state": _latest_frame.get("state", "live"),
            "updated_at": _latest_frame.get("updated_at", 0.0),
            "message": _latest_frame.get("message", ""),
        }


def connect_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_queue_db():
    with _db_lock:
        conn = connect_db()

        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kit_queue (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    product_id TEXT NOT NULL,
                    product_name TEXT DEFAULT '',
                    transaction_id TEXT NOT NULL,
                    session_dir TEXT NOT NULL,
                    inserted_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    upload_attempts INTEGER NOT NULL DEFAULT 0,
                    dispose_attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT DEFAULT '',
                    raw_path TEXT DEFAULT '',
                    annotated_path TEXT DEFAULT '',
                    last_error TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            existing_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(kit_queue)").fetchall()
            }

            migrations = {
                "upload_done": "ALTER TABLE kit_queue ADD COLUMN upload_done INTEGER NOT NULL DEFAULT 0",
                "dispose_done": "ALTER TABLE kit_queue ADD COLUMN dispose_done INTEGER NOT NULL DEFAULT 0",
                "upload_retry_at": "ALTER TABLE kit_queue ADD COLUMN upload_retry_at TEXT DEFAULT ''",
                "dispose_retry_at": "ALTER TABLE kit_queue ADD COLUMN dispose_retry_at TEXT DEFAULT ''",
                "disposal_deferred_at": "ALTER TABLE kit_queue ADD COLUMN disposal_deferred_at TEXT DEFAULT ''",
            }

            for column, ddl in migrations.items():
                if column not in existing_columns:
                    conn.execute(ddl)

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kit_queue_status_due
                ON kit_queue(status, due_at)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kit_queue_upload_retry
                ON kit_queue(upload_done, upload_retry_at, updated_at)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kit_queue_dispose_retry
                ON kit_queue(dispose_done, dispose_retry_at, updated_at)
                """
            )

            now_text = iso(utc_now())

            # If the app is restarted while an image job is mid-capture/analysis,
            # safely queue it for another capture attempt.
            conn.execute(
                """
                UPDATE kit_queue
                SET status = 'queued',
                    updated_at = ?
                WHERE status IN ('processing', 'capturing', 'analyzing')
                """,
                (now_text,),
            )

            # If the app is restarted while a result is being uploaded, keep the
            # result/images on disk and retry upload later without blocking RVM.
            conn.execute(
                """
                UPDATE kit_queue
                SET status = 'upload_pending',
                    upload_done = 0,
                    updated_at = ?
                WHERE status = 'uploading'
                """,
                (now_text,),
            )

            # If the app is restarted while DISPOSE_KIT was running, remember it
            # as pending disposal. It will resume only when the booth is idle.
            conn.execute(
                """
                UPDATE kit_queue
                SET status = 'dispose_pending',
                    dispose_done = 0,
                    disposal_deferred_at = ?,
                    last_error = 'Disposal interrupted by restart; will resume when booth is idle.',
                    updated_at = ?
                WHERE status = 'disposing'
                """,
                (now_text, now_text),
            )

            # Backfill flags for older rows created before upload_done/dispose_done.
            conn.execute(
                """
                UPDATE kit_queue
                SET upload_done = 1,
                    dispose_done = 1
                WHERE status = 'completed'
                """
            )

            conn.execute(
                """
                UPDATE kit_queue
                SET dispose_done = 1
                WHERE status = 'disposed'
                """
            )

            conn.commit()

        finally:
            conn.close()


def row_to_dict(row):
    if row is None:
        return None

    return dict(row)


def get_queue_job_by_transaction(transaction_id: str):
    """Return the latest queue job for a transaction, if any.

    This makes KitInsertionPage retries idempotent. If the user taps confirm
    again, or the page auto-recovers after a temporary SQLite/worker issue, the
    booth reuses the existing job instead of creating duplicate analysis jobs.
    """
    tx = str(transaction_id or "").strip()
    if not tx:
        return None

    init_queue_db()
    with _db_lock:
        conn = connect_db()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM kit_queue
                WHERE transaction_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (tx,),
            ).fetchone()
            return row_to_dict(row)
        finally:
            conn.close()


def get_delay_minutes():
    return float(config.get("kit_queue", "delay_minutes", default=30))


def enqueue_kit_job(
    user_id: str,
    username: str,
    product_id: str,
    product_name: str,
    transaction_id: str,
    session_dir: str,
):
    init_queue_db()

    existing = get_queue_job_by_transaction(transaction_id)
    if existing:
        print(
            f"[KIT QUEUE] Reusing existing job={existing.get('id')} tx={transaction_id}",
            flush=True,
        )
        return existing

    now = utc_now()
    due = now + timedelta(minutes=get_delay_minutes())
    job_id = uuid.uuid4().hex

    payload = {
        "id": job_id,
        "user_id": str(user_id),
        "username": str(username or ""),
        "product_id": str(product_id),
        "product_name": str(product_name or ""),
        "transaction_id": str(transaction_id),
        "session_dir": str(session_dir),
        "inserted_at": iso(now),
        "due_at": iso(due),
        "status": "queued",
        "created_at": iso(now),
        "updated_at": iso(now),
    }

    with _db_lock:
        conn = connect_db()

        try:
            conn.execute(
                """
                INSERT INTO kit_queue (
                    id,
                    user_id,
                    username,
                    product_id,
                    product_name,
                    transaction_id,
                    session_dir,
                    inserted_at,
                    due_at,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["user_id"],
                    payload["username"],
                    payload["product_id"],
                    payload["product_name"],
                    payload["transaction_id"],
                    payload["session_dir"],
                    payload["inserted_at"],
                    payload["due_at"],
                    payload["status"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )

            conn.commit()

        finally:
            conn.close()

    print(
        f"[KIT QUEUE] Enqueued job={job_id} tx={transaction_id} due_at={payload['due_at']}",
        flush=True,
    )

    return payload


def mark_job(job_id: str, status: str, **fields):
    allowed = {
        "attempts",
        "upload_attempts",
        "dispose_attempts",
        "result",
        "raw_path",
        "annotated_path",
        "last_error",
        "metadata_json",
        "due_at",
        "upload_done",
        "dispose_done",
        "upload_retry_at",
        "dispose_retry_at",
        "disposal_deferred_at",
    }

    updates = {
        "status": status,
        "updated_at": iso(utc_now()),
    }

    for key, value in fields.items():
        if key in allowed:
            updates[key] = value

    set_sql = ", ".join([f"{key} = ?" for key in updates.keys()])
    values = list(updates.values()) + [job_id]

    with _db_lock:
        conn = connect_db()

        try:
            conn.execute(f"UPDATE kit_queue SET {set_sql} WHERE id = ?", values)
            conn.commit()

        finally:
            conn.close()


def normalize_confidex_result_for_upload(result_text, analysis_metadata=None):
    """
    Converts detector uncertainty/failure into a reviewable Invalid result.
    """
    raw = str(result_text or "").strip()
    lowered = raw.lower()

    if lowered in {"positive", "negative"}:
        return raw.capitalize()

    invalid_reasons = {
        "",
        "uncertain",
        "invalid",
        "no object detected",
        "no_object_detected",
        "kit_not_detected",
        "strip_not_detected",
        "strip_crop_unavailable",
        "pipeline_exception",
        "pipeline_import_failed",
        "no_camera_frame",
        "classifier_uncertain",
        "yolo_pipeline_exception",
        "no_yolo_result",
        "no_relevant_classification",
        "no_cv_detected",
        "no_valid_combination_contained",
        "no_relevant_family_detections",
    }

    reason = ""

    if isinstance(analysis_metadata, dict):
        reason = str(analysis_metadata.get("reason") or "").strip().lower()

    if lowered in invalid_reasons or reason in invalid_reasons:
        return "Invalid"

    if "uncertain" in lowered:
        return "Invalid"

    if "not detected" in lowered:
        return "Invalid"

    if "error" in lowered:
        return "Invalid"

    return raw or "Invalid"


def result_color_bgr(result_text):
    clean = str(result_text or "").strip().lower()

    if clean == "positive":
        return (0, 0, 255)

    if clean == "negative":
        return (0, 140, 0)

    return (0, 165, 255)


def make_result_only_annotation(original_frame, result_text):
    """
    Creates an annotated image that contains ONLY the final result text.

    No kit boxes.
    No strip boxes.
    No debug lines.
    No background rectangle.
    No border box.
    """
    if original_frame is None:
        annotated = np.zeros((720, 1280, 3), dtype=np.uint8)
    else:
        annotated = original_frame.copy()

    if len(annotated.shape) == 2:
        annotated = cv2.cvtColor(annotated, cv2.COLOR_GRAY2BGR)

    h, w = annotated.shape[:2]
    result = normalize_confidex_result_for_upload(result_text)

    label = f"RESULT: {result.upper()}"
    font = cv2.FONT_HERSHEY_SIMPLEX

    font_scale = max(0.75, min(1.55, w / 950.0))
    thickness = max(2, int(round(font_scale * 2)))

    color = result_color_bgr(result)

    x = max(24, int(w * 0.025))
    y = max(55, int(h * 0.08))

    # Thin shadow only for readability. This is still text only, not a box.
    cv2.putText(
        annotated,
        label,
        (x + 2, y + 2),
        font,
        font_scale,
        (0, 0, 0),
        thickness + 2,
        cv2.LINE_AA,
    )

    cv2.putText(
        annotated,
        label,
        (x, y),
        font,
        font_scale,
        color,
        thickness,
        cv2.LINE_AA,
    )

    return annotated


def make_yolo_class_only_annotation(original_frame, result_text, detections=None):
    """
    Creates an annotated image with:
      - final result text
      - YOLO boxes
      - class labels only

    Confidence values are intentionally NOT drawn on the image.
    """
    if original_frame is None:
        annotated = np.zeros((720, 1280, 3), dtype=np.uint8)
    else:
        annotated = original_frame.copy()

    if len(annotated.shape) == 2:
        annotated = cv2.cvtColor(annotated, cv2.COLOR_GRAY2BGR)

    h, w = annotated.shape[:2]

    # Result text
    result = normalize_confidex_result_for_upload(result_text)
    result_label = f"RESULT: {result.upper()}"
    font = cv2.FONT_HERSHEY_SIMPLEX

    result_font_scale = max(0.75, min(1.55, w / 950.0))
    result_thickness = max(2, int(round(result_font_scale * 2)))
    result_color = result_color_bgr(result)

    result_x = max(24, int(w * 0.025))
    result_y = max(55, int(h * 0.08))

    cv2.putText(
        annotated,
        result_label,
        (result_x + 2, result_y + 2),
        font,
        result_font_scale,
        (0, 0, 0),
        result_thickness + 2,
        cv2.LINE_AA,
    )

    cv2.putText(
        annotated,
        result_label,
        (result_x, result_y),
        font,
        result_font_scale,
        result_color,
        result_thickness,
        cv2.LINE_AA,
    )

    # YOLO class labels only. No confidence values.
    for det in detections or []:
        try:
            xyxy = det.get("box_xyxy") or []
            if len(xyxy) != 4:
                continue

            x1, y1, x2, y2 = [int(round(float(v))) for v in xyxy]
            x1 = max(0, min(w - 1, x1))
            y1 = max(0, min(h - 1, y1))
            x2 = max(0, min(w - 1, x2))
            y2 = max(0, min(h - 1, y2))

            if x2 <= x1 or y2 <= y1:
                continue

            class_label = str(det.get("label") or det.get("label_normalized") or "").strip()
            if not class_label:
                continue

            box_color = result_color_bgr(result)
            thickness = max(2, int(round(min(w, h) / 480)))

            cv2.rectangle(
                annotated,
                (x1, y1),
                (x2, y2),
                box_color,
                thickness,
            )

            label_font_scale = max(0.55, min(1.0, w / 1400.0))
            label_thickness = max(1, int(round(label_font_scale * 2)))

            (text_w, text_h), baseline = cv2.getTextSize(
                class_label,
                font,
                label_font_scale,
                label_thickness,
            )

            label_y = y1 - 8
            if label_y - text_h - baseline < 0:
                label_y = y1 + text_h + baseline + 8

            bg_x1 = x1
            bg_y1 = max(0, label_y - text_h - baseline - 4)
            bg_x2 = min(w - 1, x1 + text_w + 8)
            bg_y2 = min(h - 1, label_y + baseline + 4)

            cv2.rectangle(
                annotated,
                (bg_x1, bg_y1),
                (bg_x2, bg_y2),
                box_color,
                -1,
            )

            cv2.putText(
                annotated,
                class_label,
                (x1 + 4, label_y),
                font,
                label_font_scale,
                (255, 255, 255),
                label_thickness,
                cv2.LINE_AA,
            )

        except Exception:
            continue

    return annotated


def save_original_review_image(session_dir, original_frame):
    """
    Ensures the untouched original camera image exists for website/admin review.

    raw.png is kept through save_capture_set.
    original.png is added for clearer website/admin mapping.
    """
    if original_frame is None:
        return None

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    original_path = session_dir / "original.png"

    try:
        cv2.imwrite(str(original_path), original_frame)
        print(f"[KIT QUEUE] Original review image saved: {original_path}", flush=True)
        return original_path
    except Exception as e:
        print(f"[KIT QUEUE] Failed to save original review image: {e}", flush=True)
        return None


def make_json_safe(value):
    if isinstance(value, np.ndarray):
        return {
            "__type": "ndarray",
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }

    if isinstance(value, (np.integer,)):
        return int(value)

    if isinstance(value, (np.floating,)):
        return float(value)

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return str(value)


def get_yolo_model():
    """
    Loads backend/ipModel/my_model.pt once and reuses it.

    Expected path:
      backend/ipModel/my_model.pt
    """
    global YOLO_MODEL

    if YOLO is None:
        raise RuntimeError(
            "Ultralytics is not installed. Install it with: pip install ultralytics"
        )

    with YOLO_MODEL_LOCK:
        if YOLO_MODEL is not None:
            return YOLO_MODEL

        model_path = ROOT / "backend" / "ipModel" / "my_model.pt"

        if not model_path.exists():
            raise FileNotFoundError(f"YOLO model not found: {model_path}")

        print(f"[KIT QUEUE] Loading YOLO model: {model_path}", flush=True)
        YOLO_MODEL = YOLO(str(model_path))
        return YOLO_MODEL


def normalize_yolo_label(value):
    """
    Keep the YOLO model class label exact.

    The model is expected to output these exact classes:
      CV, GN, GP, MN, MP, 1N, 1P, 2N, 2P

    No alias mapping is applied.
    No class-name conversion is applied.
    Only leading/trailing whitespace is removed.
    """
    return str(value or "").strip()


# =====================================================
# ACTIVE KIT / ROI FILTERING
# =====================================================

VALID_RESULT_CLASSES = {"CV", "GN", "GP", "MN", "MP", "1N", "1P", "2N", "2P"}

DENGUE_RESULT_CLASSES = {"CV", "GN", "GP", "MN", "MP"}
HIV_RESULT_CLASSES = {"CV", "1N", "1P", "2N", "2P"}


def get_expected_result_family(product_id="", product_name=""):
    """
    Uses the queued transaction product to prevent classes from the wrong kit
    family from being used.

    HIV kit:
      CV, 1N, 1P, 2N, 2P

    Dengue kit:
      CV, GN, GP, MN, MP
    """
    pid = str(product_id or "").strip().lower()
    pname = str(product_name or "").strip().lower()

    if "hiv" in pid or "hiv" in pname or pid in {"hiv123", "kit1", "1"}:
        return "hiv", set(HIV_RESULT_CLASSES)

    if "dengue" in pid or "dengue" in pname or pid in {"dengue123", "kit2", "2"}:
        return "dengue", set(DENGUE_RESULT_CLASSES)

    return "unknown", set(VALID_RESULT_CLASSES)


def get_family_conf_threshold(family):
    """
    Returns the confidence threshold for the queued kit family.

    Supported config.json keys under "kit_queue":

      "yolo_conf_threshold": 0.30,

      "yolo_conf_threshold_hiv": 0.35,
      "yolo_conf_threshold_dengue": 0.30

    Backward-compatible alternatives are also accepted:

      "yolo_hiv_conf_threshold": 0.35,
      "yolo_dengue_conf_threshold": 0.30,
      "hiv_yolo_conf_threshold": 0.35,
      "dengue_yolo_conf_threshold": 0.30,
      "yolo_confidence": 0.30
    """
    family = str(family or "").strip().lower()

    def _read_float(keys, default=None):
        for key in keys:
            try:
                value = config.get("kit_queue", key, default=None)
            except Exception:
                value = None

            if value in (None, "", "null", "None"):
                continue

            try:
                return float(value)
            except Exception:
                continue

        return default

    default_threshold = _read_float(
        ["yolo_conf_threshold", "yolo_confidence"],
        default=0.30,
    )

    if family == "hiv":
        return _read_float(
            [
                "yolo_conf_threshold_hiv",
                "yolo_hiv_conf_threshold",
                "hiv_yolo_conf_threshold",
            ],
            default=default_threshold,
        )

    if family == "dengue":
        return _read_float(
            [
                "yolo_conf_threshold_dengue",
                "yolo_dengue_conf_threshold",
                "dengue_yolo_conf_threshold",
            ],
            default=default_threshold,
        )

    return default_threshold


def get_yolo_predict_conf_threshold(*thresholds):
    """
    Ultralytics filters boxes before Python receives them.

    To allow family-specific thresholds to work correctly, prediction uses the
    lowest configured threshold, then the queue worker applies the stricter
    product-family threshold after filtering by expected classes.

    Example:
      global = 0.25
      HIV    = 0.40
      Dengue = 0.30

      YOLO predict conf = 0.25
      HIV post-filter   = 0.40
      Dengue post-filter= 0.30
    """
    cleaned = []

    for value in thresholds:
        try:
            cleaned.append(float(value))
        except Exception:
            pass

    if not cleaned:
        return 0.25

    return max(0.01, min(cleaned))


def _det_center_y(det):
    box = det.get("box_xyxy") or []
    if len(box) != 4:
        return None

    try:
        return (float(box[1]) + float(box[3])) / 2.0
    except Exception:
        return None


def _det_center_x(det):
    box = det.get("box_xyxy") or []
    if len(box) != 4:
        return None

    try:
        return (float(box[0]) + float(box[2])) / 2.0
    except Exception:
        return None


def _det_height(det):
    box = det.get("box_xyxy") or []
    if len(box) != 4:
        return 0.0

    try:
        return abs(float(box[3]) - float(box[1]))
    except Exception:
        return 0.0


def _cluster_detections_by_vertical_position(detections, cluster_gap_px=None):
    """
    Groups detections into horizontal rows/kit bands using the Y-center.

    This is meant for your RVM camera view where more than one kit can appear
    in the frame. Since the oldest kit is the bottom kit in the stack, the
    queue worker should analyze the bottom-most cluster only.
    """
    usable = []
    for det in detections or []:
        cy = _det_center_y(det)
        if cy is None:
            continue
        usable.append((cy, det))

    if not usable:
        return []

    heights = [_det_height(det) for _, det in usable if _det_height(det) > 0]
    median_h = float(np.median(heights)) if heights else 32.0

    if cluster_gap_px is None:
        try:
            cluster_gap_px = float(
                config.get("kit_queue", "active_kit_cluster_gap_px", default=max(55.0, median_h * 1.35))
            )
        except Exception:
            cluster_gap_px = max(55.0, median_h * 1.35)

    usable.sort(key=lambda item: item[0])

    clusters = []
    current = [usable[0]]

    for cy, det in usable[1:]:
        current_centers = [item[0] for item in current]
        current_median = float(np.median(current_centers))

        if abs(cy - current_median) <= float(cluster_gap_px):
            current.append((cy, det))
        else:
            clusters.append(current)
            current = [(cy, det)]

    if current:
        clusters.append(current)

    result = []
    for cluster in clusters:
        centers = [item[0] for item in cluster]
        dets = [item[1] for item in cluster]
        result.append(
            {
                "median_y": float(np.median(centers)),
                "min_y": float(min(centers)),
                "max_y": float(max(centers)),
                "count": len(dets),
                "detections": dets,
            }
        )

    return result


def filter_detections_for_active_kit(detections, product_id="", product_name="", family_conf_threshold=None):
    """
    Prevents detections from other visible kits from affecting the result.

    Why this is needed:
    The camera can see multiple kits in the queue stack. If all detections in
    the whole image are used, the code can combine classes from the top kit and
    bottom kit and produce a false Positive/Negative.

    Filtering steps:
      1. Keep only exact expected class names.
      2. Keep only classes that belong to the queued product family.
      3. Group detections by vertical position.
      4. Use the bottom-most group by default, because the bottom kit is the
         oldest/active kit in the queue stack.
    """
    family, allowed_family_classes = get_expected_result_family(product_id, product_name)

    if family_conf_threshold is None:
        family_conf_threshold = get_family_conf_threshold(family)

    try:
        family_conf_threshold = float(family_conf_threshold)
    except Exception:
        family_conf_threshold = 0.30

    all_detections = list(detections or [])

    relevant = []
    ignored = []

    for det in all_detections:
        label = str(det.get("label") or "").strip()

        try:
            det_conf = float(det.get("confidence", 0.0))
        except Exception:
            det_conf = 0.0

        if label not in VALID_RESULT_CLASSES:
            copied = dict(det)
            copied["ignore_reason"] = "UNKNOWN_CLASS"
            ignored.append(copied)
            continue

        if label not in allowed_family_classes:
            copied = dict(det)
            copied["ignore_reason"] = f"NOT_{family.upper()}_CLASS" if family != "unknown" else "NOT_ALLOWED_CLASS"
            ignored.append(copied)
            continue

        if det_conf < family_conf_threshold:
            copied = dict(det)
            copied["ignore_reason"] = "LOW_CONFIDENCE_FOR_EXPECTED_KIT_FAMILY"
            copied["required_confidence"] = family_conf_threshold
            ignored.append(copied)
            continue

        copied = dict(det)
        copied["label_normalized"] = label
        copied["class_key"] = label
        relevant.append(copied)

    if not relevant:
        return [], {
            "family": family,
            "allowed_family_classes": sorted(allowed_family_classes),
            "family_conf_threshold": family_conf_threshold,
            "active_kit_policy": "bottom",
            "clusters": [],
            "ignored_detections": make_json_safe(ignored),
            "filter_reason": "NO_RELEVANT_FAMILY_DETECTIONS",
        }

    clusters = _cluster_detections_by_vertical_position(relevant)

    if not clusters:
        return relevant, {
            "family": family,
            "allowed_family_classes": sorted(allowed_family_classes),
            "family_conf_threshold": family_conf_threshold,
            "active_kit_policy": "none",
            "clusters": [],
            "ignored_detections": make_json_safe(ignored),
            "filter_reason": "NO_VERTICAL_CLUSTER_AVAILABLE",
        }

    policy = str(config.get("kit_queue", "active_kit_policy", default="bottom")).strip().lower()

    if policy in {"none", "all"}:
        selected = relevant
        selected_cluster = None
    elif policy in {"largest", "most_detections"}:
        selected_cluster = sorted(
            clusters,
            key=lambda c: (int(c.get("count") or 0), float(c.get("median_y") or 0.0)),
            reverse=True,
        )[0]
        selected = selected_cluster["detections"]
    else:
        # Default for RVM queue: bottom-most visible kit is the active/oldest kit.
        selected_cluster = sorted(
            clusters,
            key=lambda c: float(c.get("median_y") or 0.0),
            reverse=True,
        )[0]
        selected = selected_cluster["detections"]

    selected_ids = {id(item) for item in selected}

    for cluster in clusters:
        for det in cluster.get("detections") or []:
            if id(det) not in selected_ids:
                copied = dict(det)
                copied["ignore_reason"] = "NOT_ACTIVE_BOTTOM_KIT_CLUSTER"
                ignored.append(copied)

    cluster_summary = []
    for idx, cluster in enumerate(clusters):
        cluster_summary.append(
            {
                "index": idx,
                "median_y": round(float(cluster.get("median_y") or 0.0), 2),
                "count": int(cluster.get("count") or 0),
                "classes": sorted(
                    set(str(d.get("label") or "") for d in cluster.get("detections") or [])
                ),
                "selected": cluster is selected_cluster,
            }
        )

    return selected, {
        "family": family,
        "allowed_family_classes": sorted(allowed_family_classes),
        "family_conf_threshold": family_conf_threshold,
        "active_kit_policy": policy or "bottom",
        "clusters": cluster_summary,
        "selected_cluster": (
            {
                "median_y": round(float(selected_cluster.get("median_y") or 0.0), 2),
                "count": int(selected_cluster.get("count") or 0),
                "classes": sorted(
                    set(str(d.get("label") or "") for d in selected_cluster.get("detections") or [])
                ),
            }
            if selected_cluster
            else None
        ),
        "ignored_detections": make_json_safe(ignored),
        "filter_reason": "ACTIVE_KIT_FILTER_APPLIED",
    }


def run_yolo_on_original_frame(original_frame, conf_threshold=0.25, imgsz=640, product_id='', product_name=''):
    """
    Runs backend/ipModel/my_model.pt on the original captured frame.

    Returns:
      result_text, detections, metadata
    """
    model_path_label = "backend/ipModel/my_model.pt"

    if original_frame is None:
        return "Invalid", [], {
            "ok": False,
            "reason": "NO_CAMERA_FRAME",
            "model": model_path_label,
        }

    family, _allowed_family_classes = get_expected_result_family(product_id, product_name)
    family_conf_threshold = get_family_conf_threshold(family)
    predict_conf_threshold = get_yolo_predict_conf_threshold(conf_threshold, family_conf_threshold)

    model = get_yolo_model()

    results = model.predict(
        source=original_frame,
        imgsz=int(imgsz),
        conf=float(predict_conf_threshold),
        device=str(config.get("kit_queue", "yolo_device", default="cpu")),
        verbose=False,
    )

    if not results:
        return "Invalid", [], {
            "ok": False,
            "reason": "NO_YOLO_RESULT",
            "model": model_path_label,
        }

    result = results[0]
    names = getattr(result, "names", {}) or {}

    detections = []

    if result.boxes is not None:
        for box in result.boxes:
            xyxy = box.xyxy[0].detach().cpu().numpy().astype(float).tolist()
            cls_id = int(box.cls[0].detach().cpu().item())
            conf = float(box.conf[0].detach().cpu().item())
            label = str(names.get(cls_id, cls_id))
            label_normalized = normalize_yolo_label(label)

            detections.append(
                {
                    "label": label,
                    "label_normalized": label_normalized,
                    "class_key": label_normalized,
                    "class_id": cls_id,
                    "confidence": conf,
                    "box_xyxy": [round(v, 2) for v in xyxy],
                }
            )

    active_detections, active_filter_metadata = filter_detections_for_active_kit(
        detections,
        product_id=product_id,
        product_name=product_name,
        family_conf_threshold=family_conf_threshold,
    )

    decision = decide_result_from_yolo_detections(active_detections, return_details=True)
    result_text = decision.get("result", "Invalid")

    metadata = {
        "ok": True,
        "reason": decision.get("reason", "YOLO_INFERENCE_COMPLETE"),
        "model": model_path_label,
        "result": result_text,

        # Only active-kit detections are used for the final decision/annotation.
        "detections_count": len(active_detections),
        "detections": active_detections,

        # All detections are kept only for admin/debug metadata.
        "all_detections_count": len(detections),
        "all_detections": make_json_safe(detections),

        "active_filter": make_json_safe(active_filter_metadata),
        "detected_classes": decision.get("detected_classes", []),
        "relevant_detected_classes": decision.get("relevant_detected_classes", []),
        "matched_rule": decision.get("matched_rule", ""),
        "invalid_reason": decision.get("invalid_reason", ""),
        "conf_threshold": float(family_conf_threshold),
        "predict_conf_threshold": float(predict_conf_threshold),
        "global_conf_threshold": float(conf_threshold),
        "expected_family": family,
        "imgsz": int(imgsz),
        "device": str(config.get("kit_queue", "yolo_device", default="cpu")),
        "product_id": str(product_id or ""),
        "product_name": str(product_name or ""),
    }

    return result_text, active_detections, metadata


def _best_detection_by_class(detections):
    """
    Keeps the highest-confidence detection for each exact model class.

    Duplicate detections of the same class are allowed and do not make the
    result invalid. The original YOLO label is preserved for annotation.
    """
    best = {}

    for item in detections or []:
        label = normalize_yolo_label(item.get("label"))
        if not label:
            continue

        try:
            conf = float(item.get("confidence", 0.0))
        except Exception:
            conf = 0.0

        existing = best.get(label)
        if existing is None or conf > float(existing.get("confidence", 0.0)):
            copied = dict(item)
            copied["class_key"] = label
            copied["label_normalized"] = label
            copied["confidence"] = conf
            best[label] = copied

    return best


def decide_result_from_yolo_detections(detections, return_details=False):
    """
    Converts YOLO detections to the final Confidex result using the exact
    classes from backend/ipModel/my_model.pt.

    IMPORTANT:
    - Order does NOT matter.
    - Exact order in the detection array does NOT matter.
    - The logic checks whether the detected class set CONTAINS a valid
      Positive or Negative combination.
    - Confidence is used only by YOLO filtering before this function.
    - Confidence is NOT part of the displayed annotation label.

    Positive if detected classes contain any of:
      CV + GP + MN
      CV + GN + MP
      CV + 1N + 2P
      CV + 1P + 2N

    Negative if detected classes contain any of:
      CV + GN + MN
      CV + 1N + 2N

    Invalid if:
      - no object detected
      - no classification
      - failed classifying
      - no CV detected
      - no Positive/Negative combination is contained in the detections
    """
    result_classes = {"CV", "GN", "GP", "MN", "MP", "1N", "1P", "2N", "2P"}

    positive_combos = [
        ("CV+GP+MN", {"CV", "GP", "MN"}),
        ("CV+GN+MP", {"CV", "GN", "MP"}),
        ("CV+1N+2P", {"CV", "1N", "2P"}),
        ("CV+1P+2N", {"CV", "1P", "2N"}),
    ]

    negative_combos = [
        ("CV+GN+MN", {"CV", "GN", "MN"}),
        ("CV+1N+2N", {"CV", "1N", "2N"}),
    ]

    def finish(result, reason, matched_rule="", invalid_reason="", detected=None, relevant=None):
        payload = {
            "result": result,
            "reason": reason,
            "matched_rule": matched_rule,
            "invalid_reason": invalid_reason,
            "detected_classes": sorted(detected or []),
            "relevant_detected_classes": sorted(relevant or []),
        }

        if return_details:
            return payload

        return result

    if not detections:
        return finish(
            "Invalid",
            "NO_OBJECT_DETECTED",
            invalid_reason="No object/class was detected by the model.",
        )

    best = _best_detection_by_class(detections)
    detected_classes = set(best.keys())
    relevant = detected_classes.intersection(result_classes)

    if not relevant:
        return finish(
            "Invalid",
            "NO_RELEVANT_CLASSIFICATION",
            invalid_reason=(
                "The model produced detections, but none matched the expected "
                "CV/GN/GP/MN/MP/1N/1P/2N/2P classes."
            ),
            detected=detected_classes,
            relevant=relevant,
        )

    if "CV" not in relevant:
        return finish(
            "Invalid",
            "NO_CV_DETECTED",
            invalid_reason="CV was not detected.",
            detected=detected_classes,
            relevant=relevant,
        )

    # Positive takes priority because the user's requested rule is:
    # if detections contain a Positive combination, return Positive.
    for rule_name, required in positive_combos:
        if required.issubset(relevant):
            return finish(
                "Positive",
                "VALID_POSITIVE_COMBINATION_CONTAINED",
                matched_rule=rule_name,
                detected=detected_classes,
                relevant=relevant,
            )

    for rule_name, required in negative_combos:
        if required.issubset(relevant):
            return finish(
                "Negative",
                "VALID_NEGATIVE_COMBINATION_CONTAINED",
                matched_rule=rule_name,
                detected=detected_classes,
                relevant=relevant,
            )

    return finish(
        "Invalid",
        "NO_VALID_COMBINATION_CONTAINED",
        invalid_reason=(
            "Detected classes do not contain any valid Positive or Negative "
            "combination."
        ),
        detected=detected_classes,
        relevant=relevant,
    )


def get_job(job_id: str):
    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                "SELECT * FROM kit_queue WHERE id = ?",
                (job_id,),
            ).fetchone()

            return row_to_dict(row)

        finally:
            conn.close()


def get_next_due_job():
    now_text = iso(utc_now())

    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM kit_queue
                WHERE status = 'queued'
                  AND due_at <= ?
                ORDER BY due_at ASC, inserted_at ASC
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()

            return row_to_dict(row)

        finally:
            conn.close()


def get_next_upload_pending_job():
    now_text = iso(utc_now())

    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM kit_queue
                WHERE COALESCE(upload_done, 0) = 0
                  AND COALESCE(raw_path, '') != ''
                  AND status NOT IN (
                      'queued',
                      'processing',
                      'capturing',
                      'analyzing',
                      'uploading',
                      'completed'
                  )
                  AND (
                      COALESCE(upload_retry_at, '') = ''
                      OR upload_retry_at <= ?
                  )
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()

            return row_to_dict(row)

        finally:
            conn.close()


def get_next_dispose_pending_job(include_failed=False):
    now_text = iso(utc_now())

    failed_clause = "" if include_failed else "AND status != 'disposal_failed'"

    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                f"""
                SELECT *
                FROM kit_queue
                WHERE COALESCE(dispose_done, 0) = 0
                  AND COALESCE(raw_path, '') != ''
                  AND status NOT IN (
                      'queued',
                      'processing',
                      'capturing',
                      'analyzing',
                      'disposing',
                      'completed'
                  )
                  {failed_clause}
                  AND (
                      COALESCE(dispose_retry_at, '') = ''
                      OR dispose_retry_at <= ?
                  )
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()

            return row_to_dict(row)

        finally:
            conn.close()


def has_pending_disposal():
    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT id
                FROM kit_queue
                WHERE COALESCE(dispose_done, 0) = 0
                  AND COALESCE(raw_path, '') != ''
                  AND status NOT IN (
                      'queued',
                      'processing',
                      'capturing',
                      'analyzing',
                      'completed'
                  )
                  AND status != 'disposal_failed'
                LIMIT 1
                """
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def get_next_completable_job():
    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM kit_queue
                WHERE COALESCE(upload_done, 0) = 1
                  AND COALESCE(dispose_done, 0) = 1
                  AND status != 'completed'
                ORDER BY updated_at ASC
                LIMIT 1
                """
            ).fetchone()

            return row_to_dict(row)

        finally:
            conn.close()


def get_next_disposal_failed_job():
    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM kit_queue
                WHERE status = 'disposal_failed'
                ORDER BY updated_at ASC
                LIMIT 1
                """
            ).fetchone()

            return row_to_dict(row)

        finally:
            conn.close()


def mark_completed_if_finished(job_id: str):
    latest = get_job(job_id)

    if not latest:
        return False

    if int(latest.get("upload_done") or 0) == 1 and int(latest.get("dispose_done") or 0) == 1:
        mark_job(job_id, "completed", last_error="")
        publish_queue_state("completed", f"Completed queued kit {job_id}")
        print(f"[KIT QUEUE] Completed job={job_id}", flush=True)
        return True

    return False


def defer_disposal_job(job_id: str, reason: str, seconds: int | None = None):
    retry_at = ""

    if seconds is not None:
        retry_at = iso(utc_now() + timedelta(seconds=max(1, int(seconds))))

    mark_job(
        job_id,
        "dispose_pending",
        dispose_done=0,
        dispose_retry_at=retry_at,
        disposal_deferred_at=iso(utc_now()),
        last_error=str(reason or "Disposal deferred until booth is idle."),
    )


def get_queue_counts():
    init_queue_db()

    with _db_lock:
        conn = connect_db()

        try:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM kit_queue
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()

            return {str(r["status"]): int(r["count"]) for r in rows}

        finally:
            conn.close()


def defer_job(job_id: str, seconds: int, reason: str):
    next_due = utc_now() + timedelta(seconds=max(5, int(seconds)))

    mark_job(
        job_id,
        "queued",
        due_at=iso(next_due),
        last_error=reason,
    )


class KitQueueWorker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.camera_thread = None

        self.camera_lock = threading.RLock()
        self.cap = None
        self.current_frame = None
        self.current_frame_time = 0.0
        self.camera_error = ""

    def start(self):
        init_queue_db()

        if not self.camera_thread or not self.camera_thread.is_alive():
            self.camera_thread = threading.Thread(
                target=self.camera_loop,
                name="KitQueueCamera",
                daemon=True,
            )
            self.camera_thread.start()

        if not self.worker_thread or not self.worker_thread.is_alive():
            self.worker_thread = threading.Thread(
                target=self.worker_loop,
                name="KitQueueWorker",
                daemon=True,
            )
            self.worker_thread.start()

        print("[KIT QUEUE] Background camera + worker started", flush=True)

    def stop(self):
        self.stop_event.set()
        self.release_camera()

    # ------------------------------------------------------------------
    # Camera ownership
    # ------------------------------------------------------------------

    def get_camera_index(self):
        return int(
            config.get(
                "kit_queue",
                "camera_index",
                default=config.get("kit_insertion_page", "camera_index", default=0),
            )
        )

    def get_frame_width(self):
        return int(
            config.get(
                "kit_queue",
                "frame_width",
                default=config.get("kit_insertion_page", "frame_width", default=1280),
            )
        )

    def get_frame_height(self):
        return int(
            config.get(
                "kit_queue",
                "frame_height",
                default=config.get("kit_insertion_page", "frame_height", default=720),
            )
        )

    def get_preview_interval_ms(self):
        return int(config.get("kit_queue", "preview_interval_ms", default=80))

    def open_camera(self):
        camera_index = self.get_camera_index()
        frame_width = self.get_frame_width()
        frame_height = self.get_frame_height()

        cap = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, frame_height)

        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass

            raise RuntimeError(f"Camera is not available on index {camera_index}.")

        with self.camera_lock:
            self.cap = cap
            self.camera_error = ""

        print(f"[KIT QUEUE] Camera opened on index={camera_index}", flush=True)

    def release_camera(self):
        with self.camera_lock:
            cap = self.cap
            self.cap = None

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def camera_loop(self):
        while not self.stop_event.is_set():
            try:
                with self.camera_lock:
                    cap_missing = self.cap is None

                if cap_missing:
                    publish_queue_state("camera_starting", "Opening queue camera...")
                    self.open_camera()
                    time.sleep(0.3)

                with self.camera_lock:
                    cap = self.cap

                if cap is None:
                    time.sleep(1)
                    continue

                ret, frame = cap.read()

                if not ret or frame is None:
                    raise RuntimeError("Camera read failed.")

                with self.camera_lock:
                    self.current_frame = frame.copy()
                    self.current_frame_time = time.time()
                    self.camera_error = ""

                publish_queue_frame(frame, "live", "Queue camera live")

                time.sleep(max(0.01, self.get_preview_interval_ms() / 1000.0))

            except Exception as e:
                self.camera_error = str(e)
                publish_queue_state("camera_error", str(e))
                print(f"[KIT QUEUE] Camera loop error: {e}", flush=True)
                report_error(
                    "kit_queue",
                    "Camera Error",
                    "The booth camera is not available or failed to read frames.",
                    details=str(e),
                    visible=True,
                )
                self.release_camera()
                time.sleep(
                    float(config.get("kit_queue", "camera_retry_seconds", default=3))
                )

    def snapshot_frame(self):
        max_age = float(
            config.get("kit_queue", "max_preview_frame_age_seconds", default=2.5)
        )

        with self.camera_lock:
            if self.current_frame is None:
                raise RuntimeError(
                    self.camera_error or "No camera frame is available yet."
                )

            age = time.time() - float(self.current_frame_time or 0)

            if age > max_age:
                raise RuntimeError(f"Latest camera frame is too old: {age:.1f}s")

            return self.current_frame.copy()

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def analyze_frame(self, raw_frame, job=None):
        """
        Runs backend/ipModel/my_model.pt on the original captured image.

        Important:
        - Inference is done on original_frame, not on annotated_frame.
        - annotated_frame still contains only final result text.
        - YOLO detections are stored in metadata for admin/debug review.
        """
        product_id = ""
        product_name = ""

        if isinstance(job, dict):
            product_id = str(job.get("product_id") or "")
            product_name = str(job.get("product_name") or "")

        if raw_frame is None:
            original_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            result_text = "Invalid"
            annotated_frame = make_result_only_annotation(original_frame, result_text)

            metadata = {
                "ok": False,
                "reason": "NO_CAMERA_FRAME",
                "result": result_text,
                "review_required": True,
                "product_id": product_id,
                "product_name": product_name,
                "raw_image_shape": list(original_frame.shape),
                "annotated_image_shape": list(annotated_frame.shape),
                "annotation_policy": "yolo_class_labels_only",
                "model_source": "backend/ipModel/my_model.pt",
                "pipeline": "ultralytics_yolo_pt",
            }

            return result_text, original_frame, annotated_frame, metadata

        original_frame = raw_frame.copy()

        try:
            yolo_conf = float(
                config.get(
                    "kit_queue",
                    "yolo_conf_threshold",
                    default=config.get("kit_queue", "yolo_confidence", default=0.30),
                )
            )

            yolo_imgsz = int(
                config.get("kit_queue", "yolo_imgsz", default=640)
            )

            pipeline_result_text, detections, analysis_metadata = run_yolo_on_original_frame(
                original_frame,
                conf_threshold=yolo_conf,
                imgsz=yolo_imgsz,
                product_id=product_id,
                product_name=product_name,
            )

            upload_result = normalize_confidex_result_for_upload(
                pipeline_result_text,
                analysis_metadata,
            )

            annotated_frame = make_yolo_class_only_annotation(
                original_frame,
                upload_result,
                detections,
            )

            if not isinstance(analysis_metadata, dict):
                analysis_metadata = {}

            analysis_metadata.update(
                {
                    "raw_pipeline_result": pipeline_result_text,
                    "result": upload_result,
                    "review_required": upload_result == "Invalid",
                    "product_id": product_id,
                    "product_name": product_name,
                    "raw_image_shape": list(original_frame.shape),
                    "annotated_image_shape": list(annotated_frame.shape),
                    "annotation_policy": "yolo_class_labels_only",
                    "model_source": "backend/ipModel/my_model.pt",
                    "pipeline": "ultralytics_yolo_pt",
                    "detections": make_json_safe(detections),
                }
            )

            return upload_result, original_frame, annotated_frame, analysis_metadata

        except Exception as e:
            print(f"[KIT QUEUE] YOLO model inference failed: {e}", flush=True)

            result_text = "Invalid"
            annotated_frame = make_result_only_annotation(original_frame, result_text)

            metadata = {
                "ok": False,
                "reason": "YOLO_PIPELINE_EXCEPTION",
                "error": str(e),
                "result": result_text,
                "review_required": True,
                "product_id": product_id,
                "product_name": product_name,
                "raw_image_shape": list(original_frame.shape),
                "annotated_image_shape": list(annotated_frame.shape),
                "annotation_policy": "yolo_class_labels_only",
                "model_source": "backend/ipModel/my_model.pt",
                "pipeline": "ultralytics_yolo_pt",
            }

            return result_text, original_frame, annotated_frame, metadata

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def worker_loop(self):
        while not self.stop_event.is_set():
            try:
                completable = get_next_completable_job()

                if completable:
                    mark_completed_if_finished(completable["id"])
                    continue

                dispose_job = get_next_dispose_pending_job()

                # Disposal uses the same Arduino as bill/payment/dispense/change.
                # Keep it low priority: run it only while the active page does
                # not require Arduino USB serial. Always do it before the next
                # queued capture so the physical RVM stack remains ordered from
                # oldest-to-newest.
                if dispose_job:
                    if is_booth_safe_for_background_disposal():
                        self.dispose_job(dispose_job["id"])
                        continue

                    activity = get_booth_activity()
                    message = (
                        "Disposal deferred because the current flow requires Arduino serial "
                        f"on {activity.get('page_name') or 'unknown page'}."
                    )
                    defer_disposal_job(dispose_job["id"], message)
                    publish_queue_state("dispose_pending", message)
                    time.sleep(float(config.get("kit_queue", "idle_poll_seconds", default=2)))
                    continue

                upload_job = get_next_upload_pending_job()

                if upload_job:
                    self.retry_upload(upload_job)
                    continue

                due_job = get_next_due_job()

                if due_job:
                    # A stale/pending disposal must always be resolved first once
                    # the booth becomes idle. This prevents the next queued kit
                    # from being captured/analyzed while an older kit is still at
                    # the disposal stage.
                    if has_pending_disposal():
                        publish_queue_state(
                            "dispose_pending",
                            "Waiting to finish pending disposal before the next capture.",
                        )
                        time.sleep(float(config.get("kit_queue", "idle_poll_seconds", default=2)))
                        continue

                    self.process_due_job(due_job)
                    continue

                time.sleep(float(config.get("kit_queue", "idle_poll_seconds", default=2)))

            except Exception as e:
                publish_queue_state("worker_error", str(e))
                print(f"[KIT QUEUE] Worker loop error: {e}", flush=True)
                time.sleep(5)

    def process_due_job(self, job):
        job_id = job["id"]
        attempts = int(job.get("attempts") or 0) + 1

        print(
            f"[KIT QUEUE] Processing job={job_id} tx={job.get('transaction_id')} attempt={attempts}",
            flush=True,
        )

        mark_job(job_id, "processing", attempts=attempts, last_error="")

        try:
            publish_queue_state("capturing", f"Capturing queued kit {job_id}")

            raw_frame = self.snapshot_frame()
            publish_queue_frame(raw_frame, "capturing", "Captured queued kit frame")

            publish_queue_state("analyzing", f"Analyzing queued kit {job_id}")

            result_text, original_frame, annotated_frame, analysis_metadata = self.analyze_frame(
                raw_frame,
                job,
            )

            result_text = normalize_confidex_result_for_upload(
                result_text,
                analysis_metadata,
            )

            if not isinstance(analysis_metadata, dict):
                analysis_metadata = {}

            analysis_metadata["result"] = result_text
            analysis_metadata["review_required"] = result_text == "Invalid"
            analysis_metadata["annotation_policy"] = "yolo_class_labels_only"

            publish_queue_frame(
                annotated_frame,
                "result_ready",
                f"Result: {result_text}",
            )

            session_dir = Path(job["session_dir"])
            session_dir.mkdir(parents=True, exist_ok=True)

            captured_at = iso(utc_now())

            original_review_path = save_original_review_image(
                session_dir,
                original_frame,
            )

            metadata = {
                "queue_job_id": job_id,
                "user_id": job["user_id"],
                "username": job.get("username", ""),
                "product_id": job["product_id"],
                "product_name": job.get("product_name", ""),
                "transaction_id": job["transaction_id"],
                "inserted_at": job["inserted_at"],
                "due_at": job["due_at"],
                "captured_at": captured_at,
                "result": result_text,
                "original_result": result_text,
                "review_status": "under_review" if result_text == "Invalid" else "none",
                "review_required": result_text == "Invalid",
                "annotation_policy": "yolo_class_labels_only",
                "review_image": "original.png",
                "analysis": make_json_safe(analysis_metadata),
                "images": {
                    "original": "original.png",
                    "raw": "raw.png",
                    "annotated": "annotated.png",
                    "review_original": "original.png",
                },
            }

            raw_path, annotated_path, _meta_path = save_capture_set(
                session_dir,
                original_frame,
                annotated_frame,
                metadata,
            )

            if original_review_path is None or not Path(original_review_path).exists():
                original_review_path = save_original_review_image(
                    session_dir,
                    original_frame,
                )

            mark_job(
                job_id,
                "captured",
                result=result_text,
                raw_path=str(raw_path),
                annotated_path=str(annotated_path),
                metadata_json=json.dumps(make_json_safe(metadata), ensure_ascii=False),
                upload_done=0,
                dispose_done=0,
                upload_retry_at="",
                dispose_retry_at="",
                disposal_deferred_at="",
                last_error="",
            )

            # Upload is no longer gated by the trash/disposal motor. The result
            # and images are attempted immediately after capture/analysis. If
            # offline, upload_pending is persisted and retried later.
            latest = get_job(job_id) or job
            upload_ok = self.upload_result_and_images(latest)

            if upload_ok:
                mark_job(
                    job_id,
                    "dispose_pending",
                    upload_done=1,
                    upload_retry_at="",
                    last_error="",
                )
                publish_queue_state(
                    "dispose_pending",
                    f"Result uploaded. Disposal pending for queued kit {job_id}.",
                )
            else:
                retry_delay = int(
                    config.get("kit_queue", "upload_retry_delay_seconds", default=30)
                )
                mark_job(
                    job_id,
                    "upload_pending",
                    upload_done=0,
                    upload_retry_at=iso(utc_now() + timedelta(seconds=max(1, retry_delay))),
                    last_error="Upload failed; will retry without blocking disposal.",
                )
                publish_queue_state(
                    "upload_pending",
                    f"Upload pending for queued kit {job_id}; disposal can still run when idle.",
                )
                print(f"[KIT QUEUE] Upload pending job={job_id}", flush=True)

            # Try disposal now only if the booth is idle. If a user is already in
            # a flow, the job remains persisted as dispose_pending/upload_pending.
            latest = get_job(job_id) or job
            if int(latest.get("dispose_done") or 0) == 0:
                if is_booth_safe_for_background_disposal():
                    self.dispose_job(job_id)
                else:
                    activity = get_booth_activity()
                    defer_disposal_job(
                        job_id,
                        "Disposal deferred after capture because booth is active "
                        f"on {activity.get('page_name') or 'unknown page'}.",
                    )

            mark_completed_if_finished(job_id)

        except Exception as e:
            retry_delay = int(
                config.get("kit_queue", "capture_retry_delay_seconds", default=15)
            )
            defer_job(job_id, retry_delay, str(e))
            publish_queue_state("retrying", f"Queued kit capture retry: {e}")
            print(f"[KIT QUEUE] Job failed and will retry job={job_id}: {e}", flush=True)
            report_warning(
                "kit_queue",
                "Kit Processing Retry",
                "The booth could not complete image capture/analysis. It will retry automatically.",
                details={"job_id": job_id, "error": str(e)},
                visible=True,
            )
            time.sleep(2)

    def dispose_job(self, job_id: str):
        latest = get_job(job_id)

        if not latest:
            return False

        if int(latest.get("dispose_done") or 0) == 1:
            return True

        if not is_booth_safe_for_background_disposal():
            activity = get_booth_activity()
            message = (
                "Disposal deferred because the current flow requires Arduino serial "
                f"on {activity.get('page_name') or 'unknown page'}."
            )
            defer_disposal_job(job_id, message)
            publish_queue_state("dispose_pending", message)
            return False

        dispose_attempts = int(latest.get("dispose_attempts") or 0) + 1

        mark_job(job_id, "disposing", dispose_attempts=dispose_attempts)
        publish_queue_state("disposing", f"Starting/resuming low-priority disposal for {job_id}")

        result = send_dispose_kit_command()

        if not result.get("success"):
            message = result.get("message", "Trash disposal failed")

            if result.get("deferred"):
                defer_disposal_job(job_id, message)
                publish_queue_state("dispose_pending", message)
                print(f"[KIT QUEUE] Disposal deferred before start job={job_id}: {result}", flush=True)
                return False

            mark_job(job_id, "disposal_failed", dispose_done=0, last_error=message)
            publish_queue_state("disposal_failed", message)
            print(f"[KIT QUEUE] Disposal failed to start job={job_id}: {result}", flush=True)
            report_error(
                "kit_queue",
                "Trash Disposal Failed",
                "The processed kit could not start trash disposal. Results/uploads are not blocked by this disposal failure.",
                details={"job_id": job_id, "result": result},
                visible=True,
            )
            return False

        if result.get("completed"):
            return self._mark_disposal_done(job_id, result)

        timeout_seconds = int(config.get("kit_queue", "dispose_timeout_seconds", default=90))
        poll_seconds = float(config.get("kit_queue", "dispose_status_poll_seconds", default=0.75))
        deadline = time.time() + max(10, timeout_seconds)

        while not self.stop_event.is_set():
            if not is_booth_safe_for_background_disposal():
                activity = get_booth_activity()
                message = (
                    "Disposal deferred because the current flow now requires Arduino serial "
                    f"on {activity.get('page_name') or 'unknown page'}."
                )

                try:
                    stop_result = stop_all()
                    print(f"[KIT QUEUE] STOP sent to defer disposal job={job_id}: {stop_result}", flush=True)
                except Exception as e:
                    print(f"[KIT QUEUE] STOP failed while deferring disposal job={job_id}: {e}", flush=True)

                defer_disposal_job(job_id, message)
                publish_queue_state("dispose_pending", message)
                return False

            status = get_dispose_status(timeout=3)

            if status.get("success"):
                if status.get("completed"):
                    return self._mark_disposal_done(job_id, status)

                if status.get("deferred"):
                    message = "Trash disposal was deferred by Arduino and will resume later."
                    defer_disposal_job(job_id, message)
                    publish_queue_state("dispose_pending", message)
                    print(f"[KIT QUEUE] Disposal deferred by Arduino job={job_id}: {status}", flush=True)
                    return False

                phase = status.get("phase") or "UNKNOWN"
                remaining = status.get("remaining_steps")
                publish_queue_state(
                    "disposing",
                    f"Disposal running for {job_id}: phase={phase}, remaining={remaining}",
                )
            else:
                print(f"[KIT QUEUE] Disposal status read failed job={job_id}: {status}", flush=True)

            if time.time() >= deadline:
                retry_delay = int(config.get("kit_queue", "dispose_retry_delay_seconds", default=10))
                message = "Trash disposal status timed out; will verify/resume later."
                defer_disposal_job(job_id, message, seconds=retry_delay)
                publish_queue_state("dispose_pending", message)
                print(f"[KIT QUEUE] Disposal timed out job={job_id}: {status}", flush=True)
                return False

            time.sleep(max(0.2, poll_seconds))

        message = "Queue worker stopped while disposal was running; will resume later."
        defer_disposal_job(job_id, message)
        publish_queue_state("dispose_pending", message)
        return False

    def _mark_disposal_done(self, job_id: str, result):
        latest = get_job(job_id)
        if not latest:
            return False

        next_status = "completed" if int(latest.get("upload_done") or 0) == 1 else "upload_pending"

        mark_job(
            job_id,
            next_status,
            dispose_done=1,
            dispose_retry_at="",
            disposal_deferred_at="",
            last_error="",
        )
        publish_queue_state("disposed", f"Disposed processed kit {job_id}")
        print(f"[KIT QUEUE] Disposed job={job_id}: {result}", flush=True)

        settle = float(config.get("kit_queue", "post_dispose_settle_seconds", default=2.0))

        if settle > 0:
            time.sleep(settle)

        mark_completed_if_finished(job_id)
        return True

    def retry_upload(self, job):
        job_id = job["id"]
        upload_attempts = int(job.get("upload_attempts") or 0) + 1

        print(f"[KIT QUEUE] Retrying upload job={job_id}", flush=True)

        mark_job(job_id, "uploading", upload_attempts=upload_attempts)
        publish_queue_state("uploading", f"Uploading queued kit {job_id}")

        ok = self.upload_result_and_images(job)

        if ok:
            latest = get_job(job_id) or job
            next_status = "completed" if int(latest.get("dispose_done") or 0) == 1 else "dispose_pending"
            mark_job(
                job_id,
                next_status,
                upload_done=1,
                upload_retry_at="",
                last_error="",
            )
            if next_status == "completed":
                publish_queue_state("completed", f"Upload completed for queued kit {job_id}")
            else:
                publish_queue_state("dispose_pending", f"Upload completed; disposal pending for queued kit {job_id}")
            print(f"[KIT QUEUE] Upload completed job={job_id}", flush=True)
            mark_completed_if_finished(job_id)
        else:
            retry_delay = int(
                config.get("kit_queue", "upload_retry_delay_seconds", default=30)
            )
            latest = get_job(job_id) or job
            next_status = "dispose_pending" if int(latest.get("dispose_done") or 0) == 0 else "upload_pending"
            mark_job(
                job_id,
                next_status,
                upload_done=0,
                upload_retry_at=iso(utc_now() + timedelta(seconds=max(1, retry_delay))),
                last_error=f"Upload retry scheduled in {retry_delay}s.",
            )
            publish_queue_state("upload_pending", f"Upload retry in {retry_delay}s")

    def upload_result_and_images(self, job):
        try:
            latest = get_job(job["id"])

            if latest:
                job = latest

            metadata = {}

            try:
                metadata = json.loads(job.get("metadata_json") or "{}")
            except Exception:
                metadata = {}

            result_text = normalize_confidex_result_for_upload(
                job.get("result"),
                metadata.get("analysis") if isinstance(metadata, dict) else None,
            )

            session_dir = Path(job["session_dir"])
            timestamp = session_dir.name

            if isinstance(metadata, dict):
                metadata.setdefault("review_image", "original.png")
                metadata.setdefault("annotation_policy", "yolo_class_labels_only")
                metadata.setdefault("images", {})
                metadata["images"].setdefault("original", "original.png")
                metadata["images"].setdefault("raw", "raw.png")
                metadata["images"].setdefault("annotated", "annotated.png")
                metadata["images"].setdefault("review_original", "original.png")

            payload = {
                "user_id": job["user_id"],
                "productID": job["product_id"],
                "result": result_text,
                "original_result": result_text,
                "review_status": "under_review" if result_text == "Invalid" else "none",
                "review_required": result_text == "Invalid",
                "transaction_id": job["transaction_id"],
                "metadata": metadata,

                # Metadata only.
                # Do NOT send "original_image": "original.png" or
                # "annotated_image": "annotated.png" here.
                # Those fields must contain real website/R2 URLs,
                # and those are created by upload_session_images().
                "annotation_policy": "yolo_class_labels_only",
                "review_image": "original.png",
            }

            result_res = api_client.post_result(payload)

            if not result_res.ok:
                print(
                    f"[KIT QUEUE] Result upload failed: "
                    f"{result_res.status_code} {result_res.text}",
                    flush=True,
                )
                return False

            image_extra_data = {
                "result": result_text,
                "final_result": result_text,
                "original_result": result_text,
                "review_status": "under_review" if result_text == "Invalid" else "none",
                "review_notes": (
                    metadata.get("analysis", {}).get("reason")
                    if isinstance(metadata.get("analysis"), dict)
                    else ""
                ) or ("Automatically submitted for admin review." if result_text == "Invalid" else ""),
            }

            try:
                image_results = api_client.upload_session_images(
                    user_id=job["user_id"],
                    timestamp=timestamp,
                    session_dir=session_dir,
                    product_id=job["product_id"],
                    transaction_id=job["transaction_id"],
                    extra_data=image_extra_data,
                )
            except TypeError:
                # Backward compatibility with older api_client.upload_session_images()
                # signatures that do not yet accept extra_data.
                image_results = api_client.upload_session_images(
                    user_id=job["user_id"],
                    timestamp=timestamp,
                    session_dir=session_dir,
                    product_id=job["product_id"],
                    transaction_id=job["transaction_id"],
                )

            if isinstance(image_results, dict):
                if image_results.get("ok") is False:
                    print(f"[KIT QUEUE] Image upload failed: {image_results}", flush=True)
                    report_warning(
                        "kit_queue",
                        "Image Upload Pending",
                        "One or more captured images could not be uploaded right now. Upload will retry.",
                        details=image_results,
                        visible=True,
                    )
                    return False

                for _name, item in image_results.items():
                    if isinstance(item, dict) and item.get("ok") is False:
                        print(f"[KIT QUEUE] Image upload failed: {image_results}", flush=True)
                        report_warning(
                            "kit_queue",
                            "Image Upload Pending",
                            "One or more captured images could not be uploaded right now. Upload will retry.",
                            details=image_results,
                            visible=True,
                        )
                        return False

            return True

        except Exception as e:
            print(f"[KIT QUEUE] Upload failed: {e}", flush=True)
            report_warning(
                "kit_queue",
                "Upload Pending",
                "The result/images could not be uploaded right now. They will remain queued for retry.",
                details=str(e),
                visible=True,
            )
            return False


def start_kit_queue_worker():
    global _worker_instance

    if _worker_instance is None:
        _worker_instance = KitQueueWorker()

    _worker_instance.start()
    return _worker_instance


def stop_kit_queue_worker():
    global _worker_instance

    if _worker_instance is not None:
        _worker_instance.stop()


def get_kit_queue_worker():
    return _worker_instance