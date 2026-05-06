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
from backend.util import api_client
from backend.util.capture_manager import save_capture_set
from backend.util.dispenser_serial import send_dispose_kit_command

try:
    from backend.ip_pipeline.confidex_pipeline import run_confidex_pipeline
except Exception as e:
    run_confidex_pipeline = None
    print(f"[KIT QUEUE] Confidex pipeline import failed: {e}", flush=True)


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

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kit_queue_status_due
                ON kit_queue(status, due_at)
                """
            )

            conn.execute(
                """
                UPDATE kit_queue
                SET status = 'queued',
                    updated_at = ?
                WHERE status IN (
                    'processing',
                    'capturing',
                    'analyzing',
                    'disposing',
                    'uploading'
                )
                """,
                (iso(utc_now()),),
            )

            conn.commit()

        finally:
            conn.close()


def row_to_dict(row):
    if row is None:
        return None

    return dict(row)


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
    with _db_lock:
        conn = connect_db()

        try:
            row = conn.execute(
                """
                SELECT *
                FROM kit_queue
                WHERE status = 'upload_pending'
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
        Runs the Confidex image-processing pipeline.

        Important:
        - Kit detection and strip detection still happen inside run_confidex_pipeline.
        - The pipeline's annotated/debug frame is intentionally ignored.
        - The final annotated_frame contains only the result text.
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
                "annotation_policy": "result_only_text",
            }

            return result_text, original_frame, annotated_frame, metadata

        original_frame = raw_frame.copy()

        if run_confidex_pipeline is None:
            result_text = "Invalid"
            annotated_frame = make_result_only_annotation(original_frame, result_text)

            metadata = {
                "ok": False,
                "reason": "PIPELINE_IMPORT_FAILED",
                "result": result_text,
                "review_required": True,
                "product_id": product_id,
                "product_name": product_name,
                "raw_image_shape": list(original_frame.shape),
                "annotated_image_shape": list(annotated_frame.shape),
                "annotation_policy": "result_only_text",
            }

            return result_text, original_frame, annotated_frame, metadata

        try:
            positive_threshold = float(
                config.get("kit_queue", "positive_threshold", default=0.70)
            )
            negative_threshold = float(
                config.get("kit_queue", "negative_threshold", default=0.40)
            )

            max_width_value = config.get("kit_queue", "max_process_width", default=None)

            try:
                max_width_value = (
                    int(max_width_value)
                    if max_width_value not in (None, "", "null", "None")
                    else None
                )
            except Exception:
                max_width_value = None

            pipeline_result_text, _pipeline_annotated_frame, analysis_metadata = run_confidex_pipeline(
                original_frame,
                product_id=product_id,
                product_name=product_name,
                positive_threshold=positive_threshold,
                negative_threshold=negative_threshold,
                model_size=str(config.get("kit_queue", "model_size", default="small")),
                float_input_scale=str(config.get("kit_queue", "float_input_scale", default="raw")),
                hiv_order=str(config.get("kit_queue", "hiv_order", default="c21")),
                dengue_order=str(config.get("kit_queue", "dengue_order", default="gmc")),
                ct_order=str(config.get("kit_queue", "ct_order", default="ct")),
                orientation=str(config.get("kit_queue", "orientation", default="sample_right")),
                dengue_secondary=str(config.get("kit_queue", "dengue_secondary", default="never")),
                speed=str(config.get("kit_queue", "speed", default="balanced")),
                max_process_width=max_width_value,
                debug_images=False,
            )

            upload_result = normalize_confidex_result_for_upload(
                pipeline_result_text,
                analysis_metadata,
            )

            if not isinstance(analysis_metadata, dict):
                analysis_metadata = {}

            annotated_frame = make_result_only_annotation(original_frame, upload_result)

            analysis_metadata.update(
                {
                    "raw_pipeline_result": pipeline_result_text,
                    "result": upload_result,
                    "review_required": upload_result == "Invalid",
                    "product_id": product_id,
                    "product_name": product_name,
                    "raw_image_shape": list(original_frame.shape),
                    "annotated_image_shape": list(annotated_frame.shape),
                    "annotation_policy": "result_only_text",
                    "pipeline_annotations_discarded": True,
                }
            )

            return upload_result, original_frame, annotated_frame, analysis_metadata

        except Exception as e:
            print(f"[KIT QUEUE] Confidex pipeline failed: {e}", flush=True)

            result_text = "Invalid"
            annotated_frame = make_result_only_annotation(original_frame, result_text)

            metadata = {
                "ok": False,
                "reason": "PIPELINE_EXCEPTION",
                "error": str(e),
                "result": result_text,
                "review_required": True,
                "product_id": product_id,
                "product_name": product_name,
                "raw_image_shape": list(original_frame.shape),
                "annotated_image_shape": list(annotated_frame.shape),
                "annotation_policy": "result_only_text",
            }

            return result_text, original_frame, annotated_frame, metadata

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def worker_loop(self):
        while not self.stop_event.is_set():
            try:
                disposal_job = get_next_disposal_failed_job()

                if disposal_job:
                    self.retry_disposal(disposal_job)
                    continue

                upload_job = get_next_upload_pending_job()

                if upload_job:
                    self.retry_upload(upload_job)
                    continue

                due_job = get_next_due_job()

                if due_job:
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
            analysis_metadata["annotation_policy"] = "result_only_text"

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
                "annotation_policy": "result_only_text",
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
            )

            dispose_ok = self.dispose_job(job_id)

            if not dispose_ok:
                return

            latest = get_job(job_id) or job
            upload_ok = self.upload_result_and_images(latest)

            if upload_ok:
                mark_job(job_id, "completed")
                publish_queue_state("completed", f"Completed queued kit {job_id}")
                print(f"[KIT QUEUE] Completed job={job_id}", flush=True)
            else:
                mark_job(job_id, "upload_pending")
                publish_queue_state(
                    "upload_pending",
                    f"Upload pending for queued kit {job_id}",
                )
                print(f"[KIT QUEUE] Upload pending job={job_id}", flush=True)

        except Exception as e:
            retry_delay = int(
                config.get("kit_queue", "capture_retry_delay_seconds", default=15)
            )
            defer_job(job_id, retry_delay, str(e))
            publish_queue_state("retrying", f"Queued kit capture retry: {e}")
            print(f"[KIT QUEUE] Job failed and will retry job={job_id}: {e}", flush=True)
            time.sleep(2)

    def dispose_job(self, job_id: str):
        latest = get_job(job_id)

        if not latest:
            return False

        dispose_attempts = int(latest.get("dispose_attempts") or 0) + 1

        mark_job(job_id, "disposing", dispose_attempts=dispose_attempts)
        publish_queue_state("disposing", f"Disposing processed kit {job_id}")

        result = send_dispose_kit_command()

        if not result.get("success"):
            max_attempts = int(config.get("kit_queue", "dispose_max_attempts", default=3))
            message = result.get("message", "Trash disposal failed")

            if dispose_attempts >= max_attempts:
                mark_job(job_id, "disposal_failed", last_error=message)
                publish_queue_state("disposal_failed", message)
                print(f"[KIT QUEUE] Disposal failed job={job_id}: {result}", flush=True)
                return False

            retry_delay = int(
                config.get("kit_queue", "dispose_retry_delay_seconds", default=10)
            )
            next_due = iso(utc_now() + timedelta(seconds=retry_delay))

            mark_job(
                job_id,
                "disposal_failed",
                due_at=next_due,
                last_error=message,
            )
            publish_queue_state("disposal_retry", message)
            print(f"[KIT QUEUE] Disposal will retry job={job_id}: {result}", flush=True)
            return False

        mark_job(job_id, "disposed")
        publish_queue_state("disposed", f"Disposed processed kit {job_id}")
        print(f"[KIT QUEUE] Disposed job={job_id}: {result}", flush=True)

        settle = float(config.get("kit_queue", "post_dispose_settle_seconds", default=2.0))

        if settle > 0:
            time.sleep(settle)

        return True

    def retry_disposal(self, job):
        job_id = job["id"]

        print(f"[KIT QUEUE] Retrying disposal job={job_id}", flush=True)

        ok = self.dispose_job(job_id)

        if not ok:
            time.sleep(5)
            return

        latest = get_job(job_id) or job
        upload_ok = self.upload_result_and_images(latest)

        if upload_ok:
            mark_job(job_id, "completed")
        else:
            mark_job(job_id, "upload_pending")

    def retry_upload(self, job):
        job_id = job["id"]
        upload_attempts = int(job.get("upload_attempts") or 0) + 1

        print(f"[KIT QUEUE] Retrying upload job={job_id}", flush=True)

        mark_job(job_id, "uploading", upload_attempts=upload_attempts)
        publish_queue_state("uploading", f"Uploading queued kit {job_id}")

        ok = self.upload_result_and_images(job)

        if ok:
            mark_job(job_id, "completed")
            publish_queue_state("completed", f"Upload completed for queued kit {job_id}")
            print(f"[KIT QUEUE] Upload completed job={job_id}", flush=True)
        else:
            retry_delay = int(
                config.get("kit_queue", "upload_retry_delay_seconds", default=30)
            )
            mark_job(job_id, "upload_pending")
            publish_queue_state("upload_pending", f"Upload retry in {retry_delay}s")
            time.sleep(retry_delay)

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
                metadata.setdefault("annotation_policy", "result_only_text")
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
                "annotation_policy": "result_only_text",
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
                    return False

                for _name, item in image_results.items():
                    if isinstance(item, dict) and item.get("ok") is False:
                        print(f"[KIT QUEUE] Image upload failed: {image_results}", flush=True)
                        return False

            return True

        except Exception as e:
            print(f"[KIT QUEUE] Upload failed: {e}", flush=True)
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