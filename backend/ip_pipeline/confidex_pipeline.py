from pathlib import Path
from typing import Any, Dict, Tuple

import cv2
import numpy as np

from backend.ip_pipeline.confidex_detector import (
    detect_kit,
    normalize_rect,
    draw_rotated_box,
)

try:
    from backend.ip_pipeline.confidex_detector import rect_area_ratio
except Exception:
    rect_area_ratio = None

from backend.ip_pipeline.strip_detector import (
    detect_result_strip,
    draw_original_annotation,
    infer_assay_type,
    get_expected_labels,
)


ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "models"

DEFAULT_POSITIVE_THRESHOLD = 0.70
DEFAULT_NEGATIVE_THRESHOLD = 0.40
DEFAULT_FLOAT_INPUT_SCALE = "raw"

_CLASSIFIER_CACHE: Dict[str, Any] = {}


LABEL_ALIASES = {
    "negative": "negative",
    "neg": "negative",
    "nonreactive": "negative",
    "non-reactive": "negative",
    "non_reactive": "negative",
    "non reactive": "negative",
    "n": "negative",

    "positive": "positive",
    "pos": "positive",
    "reactive": "positive",
    "p": "positive",

    "invalid": "invalid",
    "uncertain": "invalid",
    "review": "invalid",
    "no_strip_crop": "invalid",
    "no_model": "invalid",
    "inference_failed": "invalid",
}


def normalize_label(label):
    text = str(label or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    if text in {"non_reactive", "nonreactive"}:
        return "negative"

    return LABEL_ALIASES.get(text, text)


def display_result(label):
    normalized = normalize_label(label)

    if normalized == "positive":
        return "Positive"

    if normalized == "negative":
        return "Negative"

    return "Invalid"


def softmax(x):
    x = np.asarray(x, dtype=np.float32)
    x = x - np.max(x)
    e = np.exp(x)
    return e / np.sum(e)


def load_labels(labels_path):
    labels = []

    labels_path = Path(labels_path)

    if not labels_path.exists():
        return labels

    with labels_path.open("r", encoding="utf-8") as file:
        for line in file:
            text = line.strip()

            if not text:
                continue

            parts = text.split(maxsplit=1)

            if len(parts) == 2 and parts[0].isdigit():
                labels.append(normalize_label(parts[1].strip()))
            else:
                labels.append(normalize_label(text))

    return labels


def resolve_kit_from_job(product_id="", product_name=""):
    text = f"{product_id} {product_name}".lower()

    if "dengue" in text or "den" in text:
        return "Dengue"

    if "hiv" in text:
        return "HIV"

    return "HIV"


def resolve_assay_for_kit(kit):
    kit_lower = str(kit or "").lower().strip()

    if kit_lower == "dengue":
        return "dengue"

    if kit_lower == "hiv":
        return "hiv"

    return "hiv"


def get_model_paths(kit, size="small"):
    model_path = MODELS_DIR / kit / size / "model.tflite"
    labels_path = MODELS_DIR / kit / size / "labels.txt"

    return model_path, labels_path


def classify_from_scores(
    negative_score,
    positive_score,
    positive_threshold=DEFAULT_POSITIVE_THRESHOLD,
    negative_threshold=DEFAULT_NEGATIVE_THRESHOLD,
):
    negative_score = float(negative_score)
    positive_score = float(positive_score)

    if positive_score >= positive_threshold:
        return "Positive", "positive", positive_score, "confident_positive"

    if positive_score <= negative_threshold:
        return "Negative", "negative", negative_score, "confident_negative"

    return "Invalid", "invalid", max(negative_score, positive_score), "uncertain_invalid"


class TFLiteClassifier:
    def __init__(self, model_path, labels_path=None, float_input_scale=DEFAULT_FLOAT_INPUT_SCALE):
        self.model_path = str(model_path)
        self.labels_path = str(labels_path) if labels_path else ""
        self.float_input_scale = str(float_input_scale or DEFAULT_FLOAT_INPUT_SCALE).strip().lower()
        self.labels = load_labels(labels_path)

        if not Path(model_path).exists():
            raise FileNotFoundError(f"Model not found: {model_path}")

        try:
            from ai_edge_litert.interpreter import Interpreter
        except Exception:
            try:
                from tflite_runtime.interpreter import Interpreter
            except Exception:
                try:
                    from tensorflow.lite.python.interpreter import Interpreter
                except Exception as exc:
                    raise ImportError(
                        "No LiteRT/TFLite interpreter found. Install ai-edge-litert, "
                        "tflite-runtime, or tensorflow."
                    ) from exc

        self.interpreter = Interpreter(model_path=str(model_path))
        self.interpreter.allocate_tensors()

        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()

        input_shape = self.input_details[0]["shape"]

        if len(input_shape) != 4:
            raise ValueError(f"Unsupported model input shape: {input_shape}")

        self.input_height = int(input_shape[1])
        self.input_width = int(input_shape[2])
        self.input_channels = int(input_shape[3])
        self.input_dtype = self.input_details[0]["dtype"]

    def prepare_input(self, image_bgr):
        if image_bgr is None or image_bgr.size == 0:
            raise ValueError("Empty image passed to classifier.")

        if self.input_channels == 1:
            image = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            image = cv2.resize(
                image,
                (self.input_width, self.input_height),
                interpolation=cv2.INTER_AREA,
            )
            image = np.expand_dims(image, axis=-1)
        else:
            image = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            image = cv2.resize(
                image,
                (self.input_width, self.input_height),
                interpolation=cv2.INTER_AREA,
            )

        if self.input_dtype == np.float32:
            if self.float_input_scale == "zero_one":
                image = image.astype(np.float32) / 255.0
            else:
                image = image.astype(np.float32)
        else:
            image = image.astype(self.input_dtype)

        image = np.expand_dims(image, axis=0)

        return image

    def predict(self, image_bgr):
        input_data = self.prepare_input(image_bgr)

        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()

        output = self.interpreter.get_tensor(self.output_details[0]["index"])
        output = np.squeeze(output).astype(np.float32)

        if output.ndim == 0:
            output = np.array([float(output)], dtype=np.float32)

        output_dtype = self.output_details[0]["dtype"]
        quantization = self.output_details[0].get("quantization", None)

        if output_dtype != np.float32 and quantization:
            scale, zero_point = quantization

            if scale and scale > 0:
                output = (output - zero_point) * scale

        if len(output) == 1:
            class_1_score = float(output[0])

            if class_1_score < 0.0 or class_1_score > 1.0:
                class_1_score = float(1.0 / (1.0 + np.exp(-class_1_score)))

            class_0_score = 1.0 - class_1_score

            labels = self.labels if len(self.labels) >= 2 else ["negative", "positive"]

            if normalize_label(labels[1]) == "positive":
                positive_score = class_1_score
                negative_score = class_0_score
            elif normalize_label(labels[0]) == "positive":
                positive_score = class_0_score
                negative_score = class_1_score
            else:
                positive_score = class_1_score
                negative_score = class_0_score

            scores = {
                "negative": float(negative_score),
                "positive": float(positive_score),
            }

        else:
            scores_array = output.astype(np.float32)

            if (
                np.any(scores_array < 0.0)
                or np.any(scores_array > 1.0)
                or abs(float(np.sum(scores_array)) - 1.0) > 0.20
            ):
                scores_array = softmax(scores_array)

            labels = self.labels if self.labels else [
                f"class_{i}" for i in range(len(scores_array))
            ]

            score_map = {}

            for label, score in zip(labels, scores_array):
                score_map[normalize_label(label)] = float(score)

            if "negative" not in score_map and len(scores_array) >= 1:
                score_map["negative"] = float(scores_array[0])

            if "positive" not in score_map and len(scores_array) >= 2:
                score_map["positive"] = float(scores_array[1])

            scores = {
                "negative": float(score_map.get("negative", 0.0)),
                "positive": float(score_map.get("positive", 0.0)),
            }

        if scores["positive"] >= scores["negative"]:
            label = "Positive"
            confidence = scores["positive"]
        else:
            label = "Negative"
            confidence = scores["negative"]

        return {
            "label": label,
            "confidence": float(confidence),
            "scores": scores,
        }


def get_classifier(kit, size="small", float_input_scale=DEFAULT_FLOAT_INPUT_SCALE):
    kit = "Dengue" if str(kit).lower() == "dengue" else "HIV"
    key = f"{kit}:{size}:{float_input_scale}"

    if key in _CLASSIFIER_CACHE:
        return _CLASSIFIER_CACHE[key]

    model_path, labels_path = get_model_paths(kit, size=size)

    classifier = TFLiteClassifier(
        model_path=model_path,
        labels_path=labels_path,
        float_input_scale=float_input_scale,
    )

    _CLASSIFIER_CACHE[key] = classifier

    print(
        f"[IP PIPELINE] Loaded {kit} classifier: {model_path} labels={classifier.labels}",
        flush=True,
    )

    return classifier


def get_valid_debug_image(debug_dict, keys):
    if not isinstance(debug_dict, dict):
        return None

    for key in keys:
        value = debug_dict.get(key)

        if isinstance(value, np.ndarray) and value.size > 0:
            return value

    return None


def draw_text_with_bg(
    image,
    text,
    org,
    font_scale=0.70,
    color=(255, 255, 255),
    bg_color=(0, 0, 0),
    thickness=2,
):
    font = cv2.FONT_HERSHEY_SIMPLEX
    x, y = org

    lines = str(text).split("\n")
    line_height = int(30 * font_scale) + 12

    max_w = 0
    total_h = line_height * len(lines)

    for line in lines:
        (tw, _), _ = cv2.getTextSize(line, font, font_scale, thickness)
        max_w = max(max_w, tw)

    x1 = max(0, x - 10)
    y1 = max(0, y - 28)
    x2 = min(image.shape[1] - 1, x + max_w + 14)
    y2 = min(image.shape[0] - 1, y1 + total_h + 16)

    overlay = image.copy()
    cv2.rectangle(overlay, (x1, y1), (x2, y2), bg_color, -1)
    cv2.addWeighted(overlay, 0.72, image, 0.28, 0, image)

    for i, line in enumerate(lines):
        cv2.putText(
            image,
            line,
            (x, y + i * line_height),
            font,
            font_scale,
            color,
            thickness,
            cv2.LINE_AA,
        )


def annotate_with_inference(
    image_bgr,
    kit_rect,
    strip_rect,
    strip_debug,
    kit,
    assay_type,
    expected_labels,
    final_result,
    raw_prediction,
    confidence,
    score_negative,
    score_positive,
    inference_done,
    error="",
):
    try:
        annotated = draw_original_annotation(
            image_bgr.copy(),
            kit_rect,
            strip_rect,
            strip_debug or {},
        )
    except Exception:
        annotated = image_bgr.copy()

        if kit_rect is not None:
            draw_rotated_box(
                annotated,
                kit_rect,
                label="KIT",
                color=(0, 255, 0),
                thickness=3,
            )

        if strip_rect is not None:
            draw_rotated_box(
                annotated,
                strip_rect,
                label="STRIP",
                color=(255, 128, 0),
                thickness=3,
            )

    kit_status = "OK" if kit_rect is not None else "MISS"
    strip_status = "OK" if strip_rect is not None else "MISS"
    infer_status = "OK" if inference_done else "SKIPPED"

    if isinstance(expected_labels, (list, tuple)):
        expected_text = " ".join(expected_labels)
    else:
        expected_text = str(expected_labels)

    text = (
        f"Kit type: {kit}\n"
        f"Assay: {assay_type}\n"
        f"Expected labels: {expected_text}\n"
        f"Kit detector: {kit_status}\n"
        f"Strip detector: {strip_status}\n"
        f"Inference: {infer_status}\n"
        f"Result: {final_result}\n"
        f"Raw: {raw_prediction}\n"
        f"Confidence: {confidence:.3f}\n"
        f"Neg: {score_negative:.3f} | Pos: {score_positive:.3f}"
    )

    if error:
        text += f"\nError: {error[:80]}"

    draw_text_with_bg(
        annotated,
        text,
        (24, 42),
        font_scale=0.70,
        color=(255, 255, 255),
        bg_color=(0, 0, 0),
        thickness=2,
    )

    return annotated


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


def strip_debug_to_metadata(strip_debug):
    if not isinstance(strip_debug, dict):
        return {}

    keep = {}

    for key, value in strip_debug.items():
        if isinstance(value, np.ndarray):
            continue

        keep[key] = make_json_safe(value)

    return keep


def kit_debug_to_metadata(kit_debug):
    if not isinstance(kit_debug, dict):
        return {}

    keep = {}

    for key, value in kit_debug.items():
        if isinstance(value, np.ndarray):
            continue

        keep[key] = make_json_safe(value)

    return keep


def invalid_response(
    image_bgr,
    kit,
    assay_type,
    expected_labels,
    kit_rect=None,
    strip_rect=None,
    strip_debug=None,
    reason="INVALID",
    error="",
    kit_debug=None,
):
    annotated = annotate_with_inference(
        image_bgr=image_bgr,
        kit_rect=kit_rect,
        strip_rect=strip_rect,
        strip_debug=strip_debug or {},
        kit=kit,
        assay_type=assay_type,
        expected_labels=expected_labels,
        final_result="Invalid",
        raw_prediction=reason,
        confidence=0.0,
        score_negative=0.0,
        score_positive=0.0,
        inference_done=False,
        error=error,
    )

    metadata = {
        "ok": False,
        "result": "Invalid",
        "raw_pipeline_result": reason,
        "reason": reason,
        "review_required": True,
        "kit": kit,
        "assay_type": assay_type,
        "expected_labels": expected_labels,
        "kit_detected": kit_rect is not None,
        "strip_detected": strip_rect is not None,
        "inference_done": False,
        "kit_debug": kit_debug_to_metadata(kit_debug or {}),
        "strip_debug": strip_debug_to_metadata(strip_debug or {}),
    }

    return "Invalid", annotated, metadata


def run_confidex_pipeline(
    image_bgr,
    product_id="",
    product_name="",
    positive_threshold=DEFAULT_POSITIVE_THRESHOLD,
    negative_threshold=DEFAULT_NEGATIVE_THRESHOLD,
    model_size="small",
    float_input_scale=DEFAULT_FLOAT_INPUT_SCALE,
    hiv_order="c21",
    dengue_order="gmc",
    ct_order="ct",
    orientation="sample_right",
    dengue_secondary="never",
    speed="balanced",
    max_process_width=None,
    debug_images=False,
) -> Tuple[str, Any, Dict[str, Any]]:
    """
    Runtime version of your full pipeline.

    Returns:
        result_text, annotated_full_image, metadata
    """
    if image_bgr is None or not isinstance(image_bgr, np.ndarray) or image_bgr.size == 0:
        blank = np.zeros((720, 1280, 3), dtype=np.uint8)
        return invalid_response(
            image_bgr=blank,
            kit="Unknown",
            assay_type="unknown",
            expected_labels=[],
            reason="NO_IMAGE",
            error="No image passed to run_confidex_pipeline.",
        )

    original = image_bgr.copy()

    kit = resolve_kit_from_job(product_id=product_id, product_name=product_name)
    assay_type = resolve_assay_for_kit(kit)

    if assay_type == "auto":
        assay_type = infer_assay_type(f"{product_id}_{product_name}", "auto")

    expected_labels = get_expected_labels(
        assay_type,
        hiv_order,
        dengue_order,
        ct_order,
    )

    kit_rect = None
    strip_rect = None
    kit_debug = {}
    strip_debug = {}

    try:
        kit_rect, kit_debug = detect_kit(original)
        kit_debug = kit_debug or {}

    except Exception as exc:
        return invalid_response(
            image_bgr=original,
            kit=kit,
            assay_type=assay_type,
            expected_labels=expected_labels,
            reason="KIT_DETECTOR_EXCEPTION",
            error=f"{type(exc).__name__}: {exc}",
            kit_debug=kit_debug,
        )

    if kit_rect is None:
        strip_debug = {
            "strip_detected": False,
            "strip_confidence": 0.0,
            "strip_reason": "no_kit_detected",
            "assay_type": assay_type,
            "expected_labels": expected_labels,
        }

        return invalid_response(
            image_bgr=original,
            kit=kit,
            assay_type=assay_type,
            expected_labels=expected_labels,
            kit_rect=None,
            strip_rect=None,
            strip_debug=strip_debug,
            reason="KIT_NOT_DETECTED",
            kit_debug=kit_debug,
        )

    try:
        strip_rect, strip_debug = detect_result_strip(
            original,
            kit_rect,
            assay_type=assay_type,
            filename=f"{kit}.png",
            hiv_order=hiv_order,
            dengue_order=dengue_order,
            ct_order=ct_order,
            orientation_mode=orientation,
            dengue_secondary_mode=dengue_secondary,
            speed_mode=speed,
            max_process_width=max_process_width,
            include_debug_images=debug_images,
            roi_adjust_tokens=None,
        )

        strip_debug = strip_debug or {}

    except Exception as exc:
        strip_debug = {
            "strip_detected": False,
            "strip_confidence": 0.0,
            "strip_reason": "strip_detector_exception",
            "assay_type": assay_type,
            "expected_labels": expected_labels,
        }

        return invalid_response(
            image_bgr=original,
            kit=kit,
            assay_type=assay_type,
            expected_labels=expected_labels,
            kit_rect=kit_rect,
            strip_rect=None,
            strip_debug=strip_debug,
            reason="STRIP_DETECTOR_EXCEPTION",
            error=f"{type(exc).__name__}: {exc}",
            kit_debug=kit_debug,
        )

    assay_type = strip_debug.get("assay_type") or assay_type
    expected_labels = strip_debug.get("expected_labels") or expected_labels

    if strip_rect is None:
        return invalid_response(
            image_bgr=original,
            kit=kit,
            assay_type=assay_type,
            expected_labels=expected_labels,
            kit_rect=kit_rect,
            strip_rect=None,
            strip_debug=strip_debug,
            reason="STRIP_NOT_DETECTED",
            kit_debug=kit_debug,
        )

    strip_roi = get_valid_debug_image(strip_debug, ["strip_roi"])

    if strip_roi is None:
        return invalid_response(
            image_bgr=original,
            kit=kit,
            assay_type=assay_type,
            expected_labels=expected_labels,
            kit_rect=kit_rect,
            strip_rect=strip_rect,
            strip_debug=strip_debug,
            reason="NO_STRIP_CROP",
            kit_debug=kit_debug,
        )

    try:
        classifier = get_classifier(
            kit=kit,
            size=model_size,
            float_input_scale=float_input_scale,
        )

        pred = classifier.predict(strip_roi)
        raw_prediction = pred["label"]

        all_scores = pred["scores"] or {}
        score_negative = float(all_scores.get("negative", 0.0))
        score_positive = float(all_scores.get("positive", 0.0))

        final_result, predicted_label, confidence, decision = classify_from_scores(
            negative_score=score_negative,
            positive_score=score_positive,
            positive_threshold=positive_threshold,
            negative_threshold=negative_threshold,
        )

        inference_done = True

    except Exception as exc:
        return invalid_response(
            image_bgr=original,
            kit=kit,
            assay_type=assay_type,
            expected_labels=expected_labels,
            kit_rect=kit_rect,
            strip_rect=strip_rect,
            strip_debug=strip_debug,
            reason="INFERENCE_FAILED",
            error=f"{type(exc).__name__}: {exc}",
            kit_debug=kit_debug,
        )

    upload_result = display_result(final_result)

    annotated = annotate_with_inference(
        image_bgr=original,
        kit_rect=kit_rect,
        strip_rect=strip_rect,
        strip_debug=strip_debug or {},
        kit=kit,
        assay_type=assay_type,
        expected_labels=expected_labels,
        final_result=upload_result,
        raw_prediction=raw_prediction,
        confidence=confidence,
        score_negative=score_negative,
        score_positive=score_positive,
        inference_done=inference_done,
        error="",
    )

    metadata = {
        "ok": upload_result in {"Positive", "Negative"},
        "result": upload_result,
        "raw_pipeline_result": final_result,
        "predicted_label": predicted_label,
        "decision": decision,
        "review_required": upload_result == "Invalid",
        "reason": "OK" if upload_result in {"Positive", "Negative"} else "CLASSIFIER_UNCERTAIN",
        "kit": kit,
        "assay_type": assay_type,
        "expected_labels": expected_labels,
        "kit_detected": True,
        "strip_detected": True,
        "inference_done": True,
        "raw_prediction": raw_prediction,
        "inference_confidence": float(confidence),
        "score_negative": float(score_negative),
        "score_positive": float(score_positive),
        "positive_threshold": float(positive_threshold),
        "negative_threshold": float(negative_threshold),
        "all_scores": make_json_safe(all_scores),
        "model_size": model_size,
        "float_input_scale": float_input_scale,
        "kit_debug": kit_debug_to_metadata(kit_debug),
        "strip_debug": strip_debug_to_metadata(strip_debug),
    }

    return upload_result, annotated, metadata