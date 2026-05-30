# Confidex v19 runtime-clean label-zone ROI detector
# Runtime-clean detector: no automatic CSV coordinates, no saved notes, no memorized static per-image shifts.
# v9's aggressive final ROI refinement was intentionally removed because it
# over-trimmed/narrowed several crops and reduced PASS count in validation.


import os
import csv
import glob
import math
import argparse
from typing import Dict, List, Tuple, Optional, Any

import cv2
import numpy as np


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


# ============================================================
# Optional import from your existing kit detector
# ============================================================

try:
    from confidex_detector import detect_kit as _detect_kit
except Exception:
    _detect_kit = None


# ============================================================
# Basic helpers
# ============================================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def list_images(path: str) -> List[str]:
    if os.path.isfile(path):
        return [path]

    files = []
    for ext in IMAGE_EXTS:
        files.extend(glob.glob(os.path.join(path, f"*{ext}")))
        files.extend(glob.glob(os.path.join(path, f"*{ext.upper()}")))

    return sorted(set(files))


def safe_save_image(path: str, image: np.ndarray) -> bool:
    folder = os.path.dirname(path)
    if folder:
        ensure_dir(folder)
    return bool(cv2.imwrite(path, image))


def normalize_rect(rect):
    (cx, cy), (rw, rh), angle = rect

    rw = max(1.0, float(rw))
    rh = max(1.0, float(rh))

    if rh > rw:
        rw, rh = rh, rw
        angle += 90.0

    while angle >= 180.0:
        angle -= 180.0

    while angle < -180.0:
        angle += 180.0

    return ((float(cx), float(cy)), (float(rw), float(rh)), float(angle))


def draw_rotated_box(
    image: np.ndarray,
    rect,
    label: str = "",
    color: Tuple[int, int, int] = (0, 255, 0),
    thickness: int = 3,
) -> np.ndarray:
    if rect is None:
        return image

    rect = normalize_rect(rect)
    box = cv2.boxPoints(rect).astype(np.int32)
    cv2.drawContours(image, [box], 0, color, thickness)

    if label:
        cx, cy = map(int, rect[0])
        top_y = int(np.min(box[:, 1]))

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.75
        font_thickness = 2
        (tw, th), baseline = cv2.getTextSize(label, font, font_scale, font_thickness)

        tx = max(10, cx - tw // 2)
        ty = max(th + 10, top_y - 10)

        cv2.rectangle(
            image,
            (tx - 5, ty - th - 5),
            (tx + tw + 5, ty + baseline + 5),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            image,
            label,
            (tx, ty),
            font,
            font_scale,
            color,
            font_thickness,
            cv2.LINE_AA,
        )

    return image


def order_box_points(pts: np.ndarray) -> np.ndarray:
    pts = np.array(pts, dtype=np.float32)

    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)

    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]
    ordered[2] = pts[np.argmax(s)]
    ordered[1] = pts[np.argmin(diff)]
    ordered[3] = pts[np.argmax(diff)]

    return ordered


def clipped_bbox(x1, y1, x2, y2, w, h):
    x1 = max(0, min(w - 1, int(round(x1))))
    y1 = max(0, min(h - 1, int(round(y1))))
    x2 = max(0, min(w - 1, int(round(x2))))
    y2 = max(0, min(h - 1, int(round(y2))))

    if x2 <= x1 or y2 <= y1:
        return None

    return x1, y1, x2, y2


def longest_true_segment(mask_1d):
    best_start = None
    best_end = None
    best_len = 0
    start = None

    for i, val in enumerate(mask_1d):
        if val and start is None:
            start = i

        if (not val or i == len(mask_1d) - 1) and start is not None:
            end = i if not val else i + 1
            length = end - start

            if length > best_len:
                best_len = length
                best_start = start
                best_end = end

            start = None

    if best_start is None:
        return None

    return best_start, best_end


def rect_area(rect) -> float:
    (_, _), (rw, rh), _ = normalize_rect(rect)
    return float(rw * rh)


def rect_area_ratio(rect, image_shape) -> float:
    h, w = image_shape[:2]
    return rect_area(rect) / float(w * h)


# ============================================================
# Kit CSV support
# ============================================================

def load_kit_csv(csv_path: str) -> Dict[str, Any]:
    table = {}

    if not csv_path or not os.path.exists(csv_path):
        return table

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            filename = row.get("filename") or os.path.basename(row.get("image_path", ""))
            if not filename:
                continue

            try:
                detected = str(row.get("detected", "")).strip()
                if detected in {"0", "False", "false", ""}:
                    continue

                cx = float(row["kit_cx"])
                cy = float(row["kit_cy"])
                rw = float(row["kit_w"])
                rh = float(row["kit_h"])
                angle = float(row["kit_angle"])

                table[filename] = normalize_rect(((cx, cy), (rw, rh), angle))
            except Exception:
                continue

    return table


def detect_kit_or_csv(image_bgr, image_path, kit_csv_map):
    filename = os.path.basename(image_path)

    if filename in kit_csv_map:
        return kit_csv_map[filename], {
            "kit_source": "csv",
            "kit_confidence": "",
        }

    if _detect_kit is None:
        return None, {
            "kit_source": "none",
            "kit_reason": "confidex_detector.detect_kit_not_available",
        }

    rect, debug = _detect_kit(image_bgr)
    debug = debug or {}
    debug["kit_source"] = "detector"
    return rect, debug


# ============================================================
# Warp kit and map local annotations back to original image
# ============================================================

def warp_kit_from_rect(image_bgr, kit_rect):
    if kit_rect is None:
        return None, None

    kit_rect = normalize_rect(kit_rect)
    (_, _), (rw, rh), _ = kit_rect

    if rw <= 0 or rh <= 0:
        return None, None

    box = cv2.boxPoints(kit_rect)
    src = order_box_points(box)

    dst_w = int(round(rw))
    dst_h = int(round(rh))

    if dst_w < 80 or dst_h < 20:
        return None, None

    dst = np.array(
        [
            [0, 0],
            [dst_w - 1, 0],
            [dst_w - 1, dst_h - 1],
            [0, dst_h - 1],
        ],
        dtype=np.float32,
    )

    M = cv2.getPerspectiveTransform(src, dst)
    Minv = cv2.getPerspectiveTransform(dst, src)

    crop = cv2.warpPerspective(
        image_bgr,
        M,
        (dst_w, dst_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )

    meta = {
        "M": M,
        "Minv": Minv,
        "kit_crop_size": (dst_w, dst_h),
        "kit_box": box,
    }

    return crop, meta


def unflip_points_if_needed(points: np.ndarray, kit_w: int, flipped: bool) -> np.ndarray:
    pts = np.array(points, dtype=np.float32).copy()

    if flipped:
        pts[:, 0] = (kit_w - 1) - pts[:, 0]

    return pts


def map_oriented_points_to_original(points: np.ndarray, meta, kit_w: int, flipped: bool) -> np.ndarray:
    local = unflip_points_if_needed(points, kit_w, flipped)
    global_pts = cv2.perspectiveTransform(local.reshape(1, -1, 2), meta["Minv"])[0]
    return global_pts


def map_local_bbox_to_original_rect(bbox, meta, kit_w, flipped):
    x1, y1, x2, y2 = bbox
    pts = np.array(
        [
            [x1, y1],
            [x2, y1],
            [x2, y2],
            [x1, y2],
        ],
        dtype=np.float32,
    )

    global_pts = map_oriented_points_to_original(pts, meta, kit_w, flipped)
    return normalize_rect(cv2.minAreaRect(global_pts.astype(np.float32)))


# ============================================================
# Assay and orientation
# ============================================================

def infer_assay_type(filename: str, forced: str = "auto") -> str:
    forced = (forced or "auto").lower().strip()

    if forced in {"dengue", "hiv", "ct"}:
        return forced

    name = os.path.basename(filename).lower()

    if name.startswith("den") or "dengue" in name:
        return "dengue"

    if name.startswith("hiv") or "hiv" in name:
        return "hiv"

    if "ct" in name or "covid" in name or "ag" in name:
        return "ct"

    return "hiv"



def _parse_explicit_label_order(value: str, allowed: set) -> Optional[List[str]]:
    """Accept readable GUI values like "C 1 2", "1 2 C", "G M C", "T C"."""
    raw = str(value or "").strip().upper()
    if not raw:
        return None

    for sep in [",", "-", "_", "/"]:
        raw = raw.replace(sep, " ")

    parts = [x.strip() for x in raw.split() if x.strip()]
    if len(parts) >= 2 and all(x in allowed for x in parts):
        return parts

    return None


def get_expected_labels(
    assay_type: str,
    hiv_order: str = "c21",
    dengue_order: str = "gmc",
    ct_order: str = "ct",
) -> List[str]:
    assay_type = (assay_type or "").lower().strip()

    if assay_type == "dengue":
        explicit = _parse_explicit_label_order(dengue_order, {"C", "M", "G"})
        if explicit and len(explicit) == 3:
            return explicit

        order = (dengue_order or "gmc").lower().strip().replace(" ", "").replace("-", "").replace("_", "")
        if order == "cmg":
            return ["C", "M", "G"]
        return ["G", "M", "C"]

    if assay_type == "ct":
        explicit = _parse_explicit_label_order(ct_order, {"C", "T"})
        if explicit and len(explicit) == 2:
            return explicit

        order = (ct_order or "ct").lower().strip().replace(" ", "").replace("-", "").replace("_", "")
        if order == "tc":
            return ["T", "C"]
        return ["C", "T"]

    explicit = _parse_explicit_label_order(hiv_order, {"C", "1", "2"})
    if explicit and len(explicit) == 3:
        return explicit

    # Legacy aliases kept for compatibility:
    # c12 = C 1 2, c21/21c = 2 1 C, 12c = 1 2 C,
    # c21left/leftc21 = C 2 1
    order = (hiv_order or "c21").lower().strip().replace(" ", "").replace("-", "").replace("_", "")

    if order == "c12":
        return ["C", "1", "2"]
    if order == "12c":
        return ["1", "2", "C"]
    if order in {"c21left", "c21explicit", "leftc21"}:
        return ["C", "2", "1"]
    if order in {"21c", "c21"}:
        return ["2", "1", "C"]

    return ["2", "1", "C"]


def sample_well_side_score(region_bgr: np.ndarray) -> float:
    if region_bgr is None or region_bgr.size == 0:
        return 0.0

    # Use mostly red/brown evidence, not generic darkness.
    # The old dark/edge-heavy scoring can mistake the Abbott logo or printed text
    # for the sample well. The actual sample well usually contains a red/brown
    # droplet/ring, so this stays focused on color evidence.
    hsv = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2HSV)
    b, g, r = cv2.split(region_bgr)

    r32 = r.astype(np.float32)
    g32 = g.astype(np.float32)
    b32 = b.astype(np.float32)

    red_emphasis = r32 - ((g32 + b32) * 0.5)
    red_emphasis = np.clip(red_emphasis, 0, 255)

    sat = hsv[:, :, 1].astype(np.float32)
    val = hsv[:, :, 2].astype(np.float32)

    # Brown/red blob mask. This accepts dark red blood-like blobs but rejects
    # most black text/logo markings.
    red_mask = (
        (red_emphasis >= 18)
        & (sat >= 35)
        & (r32 >= g32 + 6)
        & (r32 >= b32 + 6)
        & (val >= 35)
    ).astype(np.uint8) * 255

    red_mask = cv2.morphologyEx(
        red_mask,
        cv2.MORPH_OPEN,
        np.ones((3, 3), np.uint8),
        iterations=1,
    )
    red_mask = cv2.morphologyEx(
        red_mask,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), np.uint8),
        iterations=1,
    )

    red_score = float(np.percentile(red_emphasis, 98)) / 255.0
    sat_score = float(np.percentile(sat, 92)) / 255.0
    mask_density = float(np.mean(red_mask > 0))

    contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    blob_score = 0.0
    area_total = float(region_bgr.shape[0] * region_bgr.shape[1])

    for c in contours:
        area = cv2.contourArea(c)
        if area < area_total * 0.001:
            continue

        x, y, bw, bh = cv2.boundingRect(c)
        aspect = bw / max(1.0, bh)
        fill = area / max(1.0, bw * bh)

        # Sample droplet/ring is a compact blob. Text strokes are usually thin.
        compact = max(0.0, 1.0 - abs(aspect - 1.0))
        blob_score = max(blob_score, min(1.0, area / (area_total * 0.045)) * 1.4 + compact * 0.6 + fill * 0.4)

    return red_score * 2.6 + sat_score * 0.25 + mask_density * 4.0 + blob_score


def orient_kit_sample_well_right(kit_crop: np.ndarray, mode: str = "sample_right"):
    h, w = kit_crop.shape[:2]

    info = {
        "orientation_mode": mode,
        "orientation_flipped": False,
        "sample_left_score": 0.0,
        "sample_right_score": 0.0,
        "orientation_reason": "kept",
    }

    if mode == "keep":
        return kit_crop, info

    y1 = int(h * 0.12)
    y2 = int(h * 0.90)

    left = kit_crop[y1:y2, 0:int(w * 0.33)]
    right = kit_crop[y1:y2, int(w * 0.67):w]

    left_score = sample_well_side_score(left)
    right_score = sample_well_side_score(right)

    info["sample_left_score"] = float(left_score)
    info["sample_right_score"] = float(right_score)

    # Make sample well appear on the right side.
    if left_score > right_score * 1.55 and left_score > 0.75:
        info["orientation_flipped"] = True
        info["orientation_reason"] = "sample_well_detected_on_left_flip_to_right"
        return cv2.flip(kit_crop, 1), info

    info["orientation_reason"] = "sample_well_right_or_uncertain_keep"
    return kit_crop, info


# ============================================================
# Strip candidate detection
# ============================================================

def build_strip_debug_images(kit_crop):
    gray = cv2.cvtColor(kit_crop, cv2.COLOR_BGR2GRAY)
    gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)

    clahe = cv2.createCLAHE(clipLimit=2.8, tileGridSize=(8, 8))
    eq = clahe.apply(gray_blur)

    bg = cv2.GaussianBlur(eq, (0, 0), sigmaX=19, sigmaY=19)
    diff = cv2.absdiff(eq, bg)

    edges = cv2.Canny(eq, 14, 78)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    _, diff_mask = cv2.threshold(
        diff,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )

    dark = cv2.adaptiveThreshold(
        eq,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        7,
    )

    mask = cv2.bitwise_or(diff_mask, edges)
    mask = cv2.bitwise_or(mask, dark)

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (13, 5)),
        iterations=1,
    )

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )

    b, g, r = cv2.split(kit_crop)
    red_emphasis = r.astype(np.float32) - ((g.astype(np.float32) + b.astype(np.float32)) * 0.5)
    red_emphasis = np.clip(red_emphasis, 0, 255).astype(np.uint8)
    red_emphasis = cv2.GaussianBlur(red_emphasis, (5, 5), 0)

    return {
        "gray": gray,
        "eq": eq,
        "diff": diff,
        "edges": edges,
        "strip_mask": mask,
        "red_emphasis": red_emphasis,
    }



# ============================================================
# Speed controls
# ============================================================

SPEED_PRESETS = {
    "fast": {
        "max_process_width": 900,
        "top_k": 30,
        "x_positions": [0.28, 0.32, 0.36, 0.40, 0.44, 0.48, 0.52, 0.56, 0.60, 0.64],
        "y_positions": [0.34, 0.38, 0.42, 0.46, 0.50, 0.54, 0.58, 0.62],
        "sizes": [
            ("compact_tall", 0.18, 0.34),
            ("normal", 0.22, 0.30),
            ("normal_tall", 0.24, 0.36),
            ("balanced_tall", 0.26, 0.40),
            ("wide_tall", 0.30, 0.38),
        ],
    },
    "fast_gui": {
        # Faster GUI default. It still scans enough anchors for label-zone detection,
        # but keeps the warped kit smaller for responsive validation.
        "max_process_width": 950,
        "top_k": 70,
        "x_positions": [0.27, 0.31, 0.35, 0.39, 0.43, 0.47, 0.51, 0.55, 0.59, 0.63],
        "y_positions": [0.35, 0.39, 0.43, 0.47, 0.51, 0.55, 0.59, 0.63],
        "sizes": [
            ("compact_tall", 0.18, 0.34),
            ("normal", 0.22, 0.30),
            ("normal_tall", 0.24, 0.34),
            ("normal_taller", 0.24, 0.40),
            ("balanced_tall", 0.26, 0.38),
            ("wide_tall", 0.30, 0.38),
        ],
    },
    "balanced": {
        # Runtime-clean default for batch checks. 1100px is enough for the
        # visible C/M/G, C/1/2, and C/T label zones in the current capture style.
        "max_process_width": 1100,
        "top_k": 70,
        "x_positions": [0.24, 0.27, 0.30, 0.33, 0.36, 0.39, 0.42, 0.45, 0.48, 0.51, 0.54, 0.57, 0.60, 0.63, 0.66],
        "y_positions": [0.32, 0.35, 0.38, 0.41, 0.44, 0.47, 0.50, 0.53, 0.56, 0.59, 0.62, 0.65],
        "sizes": [
            ("compact", 0.18, 0.26),
            ("compact_tall", 0.18, 0.34),
            ("normal", 0.22, 0.30),
            ("normal_tall", 0.24, 0.34),
            ("normal_taller", 0.24, 0.40),
            ("balanced_tall", 0.26, 0.38),
            ("wide", 0.28, 0.34),
            ("wide_tall", 0.30, 0.38),
        ],
    },
    "full": {
        "max_process_width": 0,
        "top_k": 0,
        "x_positions": [0.24, 0.26, 0.28, 0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.64, 0.66, 0.68],
        "y_positions": [0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.64],
        "sizes": [
            ("compact", 0.18, 0.26),
            ("compact_tall", 0.18, 0.34),
            ("normal", 0.22, 0.30),
            ("normal_tall", 0.24, 0.34),
            ("normal_taller", 0.24, 0.40),
            ("balanced_tall", 0.26, 0.38),
            ("wide", 0.28, 0.34),
            ("wide_tall", 0.30, 0.38),
        ],
    },
}


def get_speed_preset(speed_mode: str) -> Dict[str, Any]:
    # Force unknown/empty speed choices to balanced. The GUI always uses balanced
    # because it is the best compromise for the current real-image validation set.
    mode = (speed_mode or "full").lower().strip()
    return SPEED_PRESETS.get(mode, SPEED_PRESETS["full"])


def resize_kit_for_processing(kit_crop: np.ndarray, meta: Dict[str, Any], max_process_width: Optional[int]):
    """
    Downscale the warped kit before strip scanning. This is the biggest GUI speed win.

    The returned meta has a combined Minv transform, so all detected local points
    still map correctly back to the original full-resolution image.
    """
    if kit_crop is None or meta is None:
        return kit_crop, meta, {"processed_scale_x": 1.0, "processed_scale_y": 1.0, "processed_resized": False}

    h, w = kit_crop.shape[:2]
    max_w = int(max_process_width or 0)

    if max_w <= 0 or w <= max_w:
        out_meta = dict(meta)
        out_meta["processed_scale_x"] = 1.0
        out_meta["processed_scale_y"] = 1.0
        out_meta["processed_resized"] = False
        out_meta["processed_size"] = (w, h)
        return kit_crop, out_meta, {
            "processed_scale_x": 1.0,
            "processed_scale_y": 1.0,
            "processed_resized": False,
            "processed_size": (w, h),
        }

    scale = max_w / float(w)
    new_w = max(80, int(round(w * scale)))
    new_h = max(20, int(round(h * scale)))

    resized = cv2.resize(kit_crop, (new_w, new_h), interpolation=cv2.INTER_AREA)

    sx = w / float(new_w)
    sy = h / float(new_h)

    scale_matrix = np.array(
        [
            [sx, 0.0, 0.0],
            [0.0, sy, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )

    out_meta = dict(meta)
    out_meta["Minv"] = meta["Minv"].astype(np.float32) @ scale_matrix
    out_meta["processed_scale_x"] = float(sx)
    out_meta["processed_scale_y"] = float(sy)
    out_meta["processed_resized"] = True
    out_meta["processed_size"] = (new_w, new_h)
    out_meta["original_kit_crop_size"] = (w, h)

    return resized, out_meta, {
        "processed_scale_x": float(sx),
        "processed_scale_y": float(sy),
        "processed_resized": True,
        "processed_size": (new_w, new_h),
        "original_kit_crop_size": (w, h),
    }


def generate_anchor_candidates(kit_crop, speed_mode: str = "fast"):
    h, w = kit_crop.shape[:2]
    candidates = []
    preset = get_speed_preset(speed_mode)

    seen = set()
    for xr in preset["x_positions"]:
        for yr in preset["y_positions"]:
            for size_name, wr, hr in preset["sizes"]:
                bw = w * wr
                bh = h * hr
                cx = w * xr
                cy = h * yr

                bbox = clipped_bbox(
                    cx - bw / 2,
                    cy - bh / 2,
                    cx + bw / 2,
                    cy + bh / 2,
                    w,
                    h,
                )

                if bbox is not None and bbox not in seen:
                    seen.add(bbox)
                    candidates.append((f"{speed_mode}_{size_name}_{xr:.2f}_{yr:.2f}", bbox))

    return candidates


def score_anchor_bbox(kit_crop, masks, bbox, name):
    h, w = kit_crop.shape[:2]
    x1, y1, x2, y2 = bbox

    roi_eq = masks["eq"][y1:y2, x1:x2]
    roi_edges = masks["edges"][y1:y2, x1:x2]
    roi_mask = masks["strip_mask"][y1:y2, x1:x2]
    roi_red = masks["red_emphasis"][y1:y2, x1:x2]

    if roi_eq.size == 0:
        return None

    bw = x2 - x1
    bh = y2 - y1

    width_ratio = bw / max(1.0, w)
    height_ratio = bh / max(1.0, h)
    aspect = bw / max(1.0, bh)
    area_ratio = (bw * bh) / float(w * h)

    if width_ratio < 0.135 or width_ratio > 0.405:
        return None

    if height_ratio < 0.20 or height_ratio > 0.49:
        return None

    if aspect < 0.78 or aspect > 4.20:
        return None

    edge_density = float(np.mean(roi_edges > 0))
    mask_density = float(np.mean(roi_mask > 0))
    contrast = float(np.std(roi_eq))

    col_profile = roi_eq.mean(axis=0)
    row_profile = roi_eq.mean(axis=1)

    col_std = float(np.std(col_profile))
    row_std = float(np.std(row_profile))
    red_mean = float(np.mean(roi_red)) / 255.0
    red_std = float(np.std(roi_red)) / 255.0

    if contrast < 3.2 and edge_density < 0.0035 and mask_density < 0.016:
        return None

    mid_x1 = int(bw * 0.18)
    mid_x2 = int(bw * 0.82)
    mid_y1 = int(bh * 0.15)
    mid_y2 = int(bh * 0.85)

    center_edges = roi_edges[mid_y1:mid_y2, mid_x1:mid_x2]
    center_mask = roi_mask[mid_y1:mid_y2, mid_x1:mid_x2]

    center_edge_density = float(np.mean(center_edges > 0)) if center_edges.size else 0.0
    center_mask_density = float(np.mean(center_mask > 0)) if center_mask.size else 0.0

    vertical_detail = min(1.0, col_std / 18.0)
    horizontal_detail = min(1.0, row_std / 16.0)

    edge_border_penalty = max(0.0, edge_density - center_edge_density) * 5.2

    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    center_x = cx / max(1.0, w)
    center_y = cy / max(1.0, h)

    # Slightly more tolerant to the right than the previous 0.40 target because
    # many of your latest Dengue notes say the true strip is a bit to the right.
    center_x_bias = max(0.0, 1.0 - abs(center_x - 0.45) / 0.31)
    center_y_bias = max(0.0, 1.0 - abs(center_y - 0.50) / 0.36)

    horizontal_location_penalty = 0.0
    if center_x > 0.64:
        horizontal_location_penalty += (center_x - 0.64) * 5.0
    if center_x < 0.22:
        horizontal_location_penalty += (0.22 - center_x) * 4.0

    shape_score = 0.0
    shape_score += max(0.0, 1.0 - abs(width_ratio - 0.245) / 0.145) * 1.35
    shape_score += max(0.0, 1.0 - abs(height_ratio - 0.36) / 0.17) * 1.75
    shape_score += max(0.0, 1.0 - abs(aspect - 1.95) / 1.55) * 0.95

    detail_score = 0.0
    detail_score += min(1.0, edge_density / 0.045) * 1.05
    detail_score += min(1.0, mask_density / 0.145) * 0.85
    detail_score += min(1.0, contrast / 30.0) * 0.85
    detail_score += vertical_detail * 1.05
    detail_score += horizontal_detail * 0.62
    detail_score += min(1.0, red_mean / 0.13) * 0.24
    detail_score += min(1.0, red_std / 0.08) * 0.52

    center_detail_score = 0.0
    center_detail_score += min(1.0, center_edge_density / 0.040) * 1.55
    center_detail_score += min(1.0, center_mask_density / 0.115) * 0.78

    oversize_penalty = 0.0

    # The latest result set has several "box too large / wrong placement" cases
    # that survived because strong texture dominated the score. Make wide + very
    # tall candidates pay a real penalty before label scoring.
    if height_ratio > 0.455:
        oversize_penalty += (height_ratio - 0.455) * 8.0

    if width_ratio > 0.335:
        oversize_penalty += (width_ratio - 0.335) * 6.0

    if width_ratio < 0.165:
        oversize_penalty += (0.165 - width_ratio) * 4.0

    if height_ratio < 0.235:
        oversize_penalty += (0.235 - height_ratio) * 5.0

    # Most true strip boxes in your CSV are not near-square after final padding.
    # Penalize near-square wide/tall boxes unless the line evaluator later proves
    # them strongly.
    if aspect < 1.22 and width_ratio > 0.25:
        oversize_penalty += (1.22 - aspect) * 1.1

    score = (
        shape_score
        + detail_score
        + center_detail_score
        + center_x_bias * 0.42
        + center_y_bias * 0.32
        - edge_border_penalty
        - oversize_penalty
        - horizontal_location_penalty
    )

    return {
        "name": name,
        "bbox": bbox,
        "score": float(score),
        "width_ratio": float(width_ratio),
        "height_ratio": float(height_ratio),
        "aspect": float(aspect),
        "area_ratio": float(area_ratio),
        "edge_density": float(edge_density),
        "mask_density": float(mask_density),
        "contrast": float(contrast),
        "col_std": float(col_std),
        "row_std": float(row_std),
        "red_mean": float(red_mean),
        "red_std": float(red_std),
        "center_edge_density": float(center_edge_density),
        "center_mask_density": float(center_mask_density),
        "center_x": float(center_x),
        "center_y": float(center_y),
    }


    roi_eq = masks["eq"][y1:y2, x1:x2]
    roi_edges = masks["edges"][y1:y2, x1:x2]
    roi_mask = masks["strip_mask"][y1:y2, x1:x2]
    roi_red = masks["red_emphasis"][y1:y2, x1:x2]

    if roi_eq.size == 0:
        return None

    bw = x2 - x1
    bh = y2 - y1

    width_ratio = bw / max(1.0, w)
    height_ratio = bh / max(1.0, h)
    aspect = bw / max(1.0, bh)
    area_ratio = (bw * bh) / float(w * h)

    if width_ratio < 0.15 or width_ratio > 0.42:
        return None

    if height_ratio < 0.20 or height_ratio > 0.46:
        return None

    if aspect < 1.10 or aspect > 4.40:
        return None

    edge_density = float(np.mean(roi_edges > 0))
    mask_density = float(np.mean(roi_mask > 0))
    contrast = float(np.std(roi_eq))

    col_profile = roi_eq.mean(axis=0)
    row_profile = roi_eq.mean(axis=1)

    col_std = float(np.std(col_profile))
    row_std = float(np.std(row_profile))
    red_mean = float(np.mean(roi_red)) / 255.0
    red_std = float(np.std(roi_red)) / 255.0

    if contrast < 3.4 and edge_density < 0.0038 and mask_density < 0.018:
        return None

    mid_x1 = int(bw * 0.18)
    mid_x2 = int(bw * 0.82)
    mid_y1 = int(bh * 0.16)
    mid_y2 = int(bh * 0.84)

    center_edges = roi_edges[mid_y1:mid_y2, mid_x1:mid_x2]
    center_mask = roi_mask[mid_y1:mid_y2, mid_x1:mid_x2]

    center_edge_density = float(np.mean(center_edges > 0)) if center_edges.size else 0.0
    center_mask_density = float(np.mean(center_mask > 0)) if center_mask.size else 0.0

    vertical_detail = min(1.0, col_std / 18.0)
    horizontal_detail = min(1.0, row_std / 16.0)

    edge_border_penalty = max(0.0, edge_density - center_edge_density) * 6.0

    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    center_x = cx / max(1.0, w)
    center_y = cy / max(1.0, h)

    center_x_bias = max(0.0, 1.0 - abs(center_x - 0.40) / 0.28)
    center_y_bias = max(0.0, 1.0 - abs(center_y - 0.50) / 0.35)

    # After the kit crop is oriented with the sample well on the right, the
    # result-strip window is normally left-of-center. This penalty prevents
    # the detector from locking onto the middle arrow/square/10µl markings.
    horizontal_location_penalty = 0.0
    if center_x > 0.54:
        horizontal_location_penalty += (center_x - 0.54) * 6.0
    if center_x < 0.24:
        horizontal_location_penalty += (0.24 - center_x) * 4.0

    shape_score = 0.0
    shape_score += max(0.0, 1.0 - abs(width_ratio - 0.25) / 0.16) * 1.5
    shape_score += max(0.0, 1.0 - abs(height_ratio - 0.32) / 0.16) * 1.9
    shape_score += max(0.0, 1.0 - abs(aspect - 2.20) / 1.70) * 1.0

    detail_score = 0.0
    detail_score += min(1.0, edge_density / 0.045) * 1.1
    detail_score += min(1.0, mask_density / 0.145) * 0.9
    detail_score += min(1.0, contrast / 30.0) * 0.9
    detail_score += vertical_detail * 1.1
    detail_score += horizontal_detail * 0.65
    detail_score += min(1.0, red_mean / 0.13) * 0.25
    detail_score += min(1.0, red_std / 0.08) * 0.55

    center_detail_score = 0.0
    center_detail_score += min(1.0, center_edge_density / 0.040) * 1.6
    center_detail_score += min(1.0, center_mask_density / 0.115) * 0.8

    oversize_penalty = 0.0

    if height_ratio > 0.43:
        oversize_penalty += (height_ratio - 0.43) * 5.0

    if width_ratio > 0.38:
        oversize_penalty += (width_ratio - 0.38) * 4.0

    if width_ratio < 0.17:
        oversize_penalty += (0.17 - width_ratio) * 4.0

    if height_ratio < 0.22:
        oversize_penalty += (0.22 - height_ratio) * 5.0

    score = (
        shape_score
        + detail_score
        + center_detail_score
        + center_x_bias * 0.45
        + center_y_bias * 0.35
        - edge_border_penalty
        - oversize_penalty
        - horizontal_location_penalty
    )

    return {
        "name": name,
        "bbox": bbox,
        "score": float(score),
        "width_ratio": float(width_ratio),
        "height_ratio": float(height_ratio),
        "aspect": float(aspect),
        "area_ratio": float(area_ratio),
        "edge_density": float(edge_density),
        "mask_density": float(mask_density),
        "contrast": float(contrast),
        "col_std": float(col_std),
        "row_std": float(row_std),
        "red_mean": float(red_mean),
        "red_std": float(red_std),
        "center_edge_density": float(center_edge_density),
        "center_mask_density": float(center_mask_density),
        "center_x": float(center_x),
        "center_y": float(center_y),
    }
def refine_anchor_bbox_by_edges(kit_crop, masks, bbox):
    h, w = kit_crop.shape[:2]
    x1, y1, x2, y2 = bbox

    pad_x = int((x2 - x1) * 0.12)
    pad_y = int((y2 - y1) * 0.12)

    rx1 = max(0, x1 - pad_x)
    ry1 = max(0, y1 - pad_y)
    rx2 = min(w - 1, x2 + pad_x)
    ry2 = min(h - 1, y2 + pad_y)

    roi = masks["strip_mask"][ry1:ry2, rx1:rx2]

    if roi.size == 0:
        return bbox

    col_profile = roi.mean(axis=0)
    row_profile = roi.mean(axis=1)

    col_thr = max(4.0, np.percentile(col_profile, 72) * 0.45)
    row_thr = max(4.0, np.percentile(row_profile, 72) * 0.45)

    col_seg = longest_true_segment(col_profile > col_thr)
    row_seg = longest_true_segment(row_profile > row_thr)

    nx1, ny1, nx2, ny2 = x1, y1, x2, y2

    if col_seg is not None:
        a, b = col_seg
        if (b - a) >= (x2 - x1) * 0.42:
            nx1 = rx1 + a
            nx2 = rx1 + b

    if row_seg is not None:
        a, b = row_seg
        if (b - a) >= (y2 - y1) * 0.35:
            ny1 = ry1 + a
            ny2 = ry1 + b

    bw = nx2 - nx1
    bh = ny2 - ny1

    min_w = int(w * 0.16)
    min_h = int(h * 0.22)
    max_w = int(w * 0.42)
    max_h = int(h * 0.46)

    if bw < min_w:
        cx = (nx1 + nx2) / 2
        nx1 = int(cx - min_w / 2)
        nx2 = int(cx + min_w / 2)

    if bh < min_h:
        cy = (ny1 + ny2) / 2
        ny1 = int(cy - min_h / 2)
        ny2 = int(cy + min_h / 2)

    if bw > max_w:
        cx = (nx1 + nx2) / 2
        nx1 = int(cx - max_w / 2)
        nx2 = int(cx + max_w / 2)

    if bh > max_h:
        cy = (ny1 + ny2) / 2
        ny1 = int(cy - max_h / 2)
        ny2 = int(cy + max_h / 2)

    refined = clipped_bbox(nx1, ny1, nx2, ny2, w, h)
    return refined if refined is not None else bbox


# ============================================================
# Label/line analysis
# ============================================================

def smooth_1d(profile: np.ndarray, k: int = 9) -> np.ndarray:
    k = max(3, int(k))
    if k % 2 == 0:
        k += 1

    return cv2.GaussianBlur(profile.astype(np.float32).reshape(1, -1), (k, 1), 0).reshape(-1)


def normalize_profile(profile: np.ndarray) -> np.ndarray:
    p = np.asarray(profile, dtype=np.float32)
    mn = float(np.min(p))
    mx = float(np.max(p))
    return (p - mn) / (mx - mn + 1e-6)


def find_peaks_1d(profile, min_height=0.25, min_distance=18):
    p = np.asarray(profile, dtype=np.float32)
    peaks = []

    for i in range(1, len(p) - 1):
        if p[i] >= min_height and p[i] >= p[i - 1] and p[i] >= p[i + 1]:
            if not peaks:
                peaks.append(i)
            elif i - peaks[-1] >= min_distance:
                peaks.append(i)
            else:
                if p[i] > p[peaks[-1]]:
                    peaks[-1] = i

    return peaks


def split_label_and_line_bands(strip_roi):
    h, w = strip_roi.shape[:2]

    # The printed C/M/G or C/2/1 labels are in the upper band. The actual
    # colored result lines sit lower inside the recessed result window.
    # Starting the line band too high makes the triangle markers get mistaken
    # as test lines, so keep it in the lower half of the strip box.
    label_band = strip_roi[0:int(h * 0.48), :]
    line_band = strip_roi[int(h * 0.45):int(h * 0.96), :]

    return label_band, line_band


def detect_label_text_peaks(label_band):
    if label_band is None or label_band.size == 0:
        return [], np.zeros((1,), dtype=np.float32)

    gray = cv2.cvtColor(label_band, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    dark = 255.0 - gray.astype(np.float32)
    profile = np.mean(dark, axis=0)
    profile = smooth_1d(normalize_profile(profile), k=17)

    peaks = find_peaks_1d(profile, min_height=0.22, min_distance=max(12, label_band.shape[1] // 12))

    return peaks, profile


def score_layout(xs: List[int], width: int, n: int) -> float:
    if len(xs) != n:
        return 1e18

    xs = [int(x) for x in xs]

    if n == 1:
        return 0.0

    diffs = np.diff(xs)
    mean_d = float(np.mean(diffs))

    score = float(np.std(diffs)) * 2.0

    if n == 3:
        min_d = width * 0.11
        max_d = width * 0.30
    else:
        min_d = width * 0.16
        max_d = width * 0.46

    if mean_d < min_d:
        score += (min_d - mean_d) * 4.0

    if mean_d > max_d:
        score += (mean_d - max_d) * 2.0

    if xs[0] < width * 0.04:
        score += (width * 0.04 - xs[0]) * 1.0

    if xs[-1] > width * 0.96:
        score += (xs[-1] - width * 0.96) * 1.0

    # Prefer printed labels around the center span of the strip box.
    center = float(np.mean(xs)) / max(1.0, width)
    target = 0.50
    score += abs(center - target) * width * 0.15

    return float(score)


def choose_label_positions_from_peaks_with_meta(peaks: List[int], width: int, expected_labels: List[str]):
    """
    Pick printed-label x positions, but explicitly report whether the positions
    came from real text peaks or from a geometric fallback.

    This matters because fallback positions are useful for drawing/debugging,
    but they must NOT be treated as proof that the detected strip is correct.
    """
    n = len(expected_labels)
    meta = {
        "label_positions_source": "fallback_prior",
        "label_peak_count": int(len(peaks or [])),
        "label_layout_score": "",
        "label_layout_reliable": False,
    }

    # Use text peaks only if the cluster is stable.
    if len(peaks) >= n:
        sorted_peaks = sorted(int(p) for p in peaks)
        best = None
        best_score = 1e18

        # choose any consecutive cluster; printed labels are adjacent
        for i in range(0, len(sorted_peaks) - n + 1):
            cand = sorted_peaks[i:i + n]
            s = score_layout(cand, width, n)

            if s < best_score:
                best_score = s
                best = cand

        meta["label_layout_score"] = float(best_score)

        # v21: stricter than the old width*0.60.  The old threshold allowed
        # noisy background/line streaks to masquerade as C/1/2 or G/M/C labels.
        if best is not None and best_score < width * 0.42:
            meta["label_positions_source"] = "detected_text_peaks"
            meta["label_layout_reliable"] = True
            return {label: int(x) for label, x in zip(expected_labels, best)}, meta

    # Fallback priors are still useful when labels are faint, but downstream
    # gates now know that this was only a fallback, not real label evidence.
    if n == 3:
        xs = [
            int(width * 0.30),
            int(width * 0.50),
            int(width * 0.70),
        ]
    elif n == 2:
        xs = [
            int(width * 0.38),
            int(width * 0.62),
        ]
    else:
        xs = [int(width * 0.50)]

    return {label: int(x) for label, x in zip(expected_labels, xs)}, meta


def choose_label_positions_from_peaks(peaks: List[int], width: int, expected_labels: List[str]) -> Dict[str, int]:
    positions, _ = choose_label_positions_from_peaks_with_meta(peaks, width, expected_labels)
    return positions


def local_dark_profile(line_band):
    gray = cv2.cvtColor(line_band, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    dark = 255.0 - gray.astype(np.float32)
    profile = np.mean(dark, axis=0)
    return smooth_1d(normalize_profile(profile), k=9)


def local_red_profile(line_band):
    b, g, r = cv2.split(line_band)
    red = r.astype(np.float32) - ((g.astype(np.float32) + b.astype(np.float32)) * 0.5)
    red = np.clip(red, 0, 255)
    profile = np.mean(red, axis=0)
    return smooth_1d(normalize_profile(profile), k=9)


def best_column_near(profile, center_x, half_width):
    left = max(0, int(center_x - half_width))
    right = min(len(profile), int(center_x + half_width + 1))

    if right <= left:
        return int(center_x)

    zone = profile[left:right]
    return int(left + np.argmax(zone))


def band_mean(profile, x1, x2):
    x1 = max(0, int(x1))
    x2 = min(len(profile), int(x2))

    if x2 <= x1:
        return 0.0

    return float(np.mean(profile[x1:x2]))


def score_line_near_label(dark_profile, red_profile, label_x, width):
    half_width = max(10, int(width * 0.075))
    best_x_dark = best_column_near(dark_profile, label_x, half_width)
    best_x_red = best_column_near(red_profile, label_x, half_width)

    # Use the stronger of dark and red local peaks. Red helps faint pink lines.
    dark_peak = float(dark_profile[best_x_dark])
    red_peak = float(red_profile[best_x_red])

    if red_peak > dark_peak * 1.08:
        best_x = best_x_red
        profile = red_profile
        source = "red"
    else:
        best_x = best_x_dark
        profile = dark_profile
        source = "dark"

    offset = abs(best_x - int(label_x))

    center = band_mean(profile, best_x - 2, best_x + 3)
    left_side = band_mean(profile, best_x - 14, best_x - 5)
    right_side = band_mean(profile, best_x + 5, best_x + 14)
    side = (left_side + right_side) * 0.5

    contrast_score = center - side

    valid = bool(
        offset <= max(14, width * 0.09)
        and (
            contrast_score >= 0.035
            or center >= 0.55
            or red_peak >= 0.62
        )
    )

    return {
        "label_x": int(label_x),
        "best_x": int(best_x),
        "offset": int(offset),
        "source": source,
        "center_score": float(center),
        "contrast_score": float(contrast_score),
        "dark_peak": float(dark_peak),
        "red_peak": float(red_peak),
        "valid": valid,
    }


def suppress_duplicate_line_assignments(line_debug: Dict[str, Dict[str, Any]], expected_labels: List[str], width: int):
    used = []

    for label in expected_labels:
        if label not in line_debug:
            continue

        info = line_debug[label]
        if not info.get("valid", False):
            continue

        best_x = info["best_x"]
        strength = max(info.get("center_score", 0.0), info.get("red_peak", 0.0))
        duplicate = False

        for item in used:
            if abs(best_x - item["x"]) <= max(8, width * 0.055):
                duplicate = True

                if strength > item["strength"]:
                    old_label = item["label"]
                    line_debug[old_label]["valid"] = False
                    line_debug[old_label]["suppressed_reason"] = f"duplicate_with_{label}"
                    item["label"] = label
                    item["x"] = best_x
                    item["strength"] = strength
                else:
                    line_debug[label]["valid"] = False
                    line_debug[label]["suppressed_reason"] = f"duplicate_with_{item['label']}"
                break

        if not duplicate:
            used.append({"label": label, "x": best_x, "strength": strength})

    return line_debug


def analyze_strip_labels(strip_roi, expected_labels: List[str]):
    label_band, line_band = split_label_and_line_bands(strip_roi)
    label_peaks, label_profile = detect_label_text_peaks(label_band)

    width = strip_roi.shape[1]
    label_positions, label_meta = choose_label_positions_from_peaks_with_meta(
        label_peaks,
        width,
        expected_labels,
    )

    dark_profile = local_dark_profile(line_band)
    red_profile = local_red_profile(line_band)

    line_debug = {}

    for label, label_x in label_positions.items():
        line_debug[label] = score_line_near_label(
            dark_profile,
            red_profile,
            label_x,
            width,
        )

    line_debug = suppress_duplicate_line_assignments(line_debug, expected_labels, width)

    return {
        "label_band": label_band,
        "line_band": line_band,
        "label_peaks": label_peaks,
        "label_profile": label_profile,
        "label_positions": label_positions,
        "label_positions_source": label_meta.get("label_positions_source", "fallback_prior"),
        "label_peak_count": label_meta.get("label_peak_count", 0),
        "label_layout_score": label_meta.get("label_layout_score", ""),
        "label_layout_reliable": bool(label_meta.get("label_layout_reliable", False)),
        "line_debug": line_debug,
        "dark_profile": dark_profile,
        "red_profile": red_profile,
    }


# ============================================================
# Candidate evaluation / geometry finalization
# ============================================================

def bbox_iou(a, b) -> float:
    if a is None or b is None:
        return 0.0

    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)

    inter = iw * ih
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))

    return float(inter) / float(area_a + area_b - inter + 1e-6)


def expand_strip_bbox_for_labels(bbox, kit_w, kit_h, assay_type="auto"):
    """
    Final box should include BOTH:
      1. printed characters: G/M/C, C/M/G, C/1/2, C/T
      2. the actual vertical result lines

    This version is less side-heavy and more height-aware. Your latest notes
    mostly ask for either small upward/downward expansion or side trimming, so
    width is capped more aggressively while height gets a minimum target.
    """
    if bbox is None:
        return None

    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)

    assay = (assay_type or "auto").lower().strip()

    if assay == "dengue":
        pad_x = int(bw * 0.045)
        pad_top = int(bh * 0.360)
        pad_bottom = int(bh * 0.145)
        min_h_ratio = 0.355
        max_h_ratio = 0.520
        max_w_ratio = 0.390
    elif assay == "hiv":
        pad_x = int(bw * 0.050)
        pad_top = int(bh * 0.315)
        pad_bottom = int(bh * 0.165)
        min_h_ratio = 0.340
        max_h_ratio = 0.505
        max_w_ratio = 0.390
    else:
        pad_x = int(bw * 0.055)
        pad_top = int(bh * 0.285)
        pad_bottom = int(bh * 0.135)
        min_h_ratio = 0.300
        max_h_ratio = 0.480
        max_w_ratio = 0.380

    nx1 = max(0, x1 - pad_x)
    nx2 = min(kit_w - 1, x2 + pad_x)
    ny1 = max(0, y1 - pad_top)
    ny2 = min(kit_h - 1, y2 + pad_bottom)

    # Enforce a useful minimum height for close-up crops. Previous outputs at
    # ~0.29-0.30 height were repeatedly marked "increase height".
    target_min_h = int(round(kit_h * min_h_ratio))
    if (ny2 - ny1) < target_min_h:
        missing = target_min_h - (ny2 - ny1)
        # Bias upward because labels are frequently clipped, but still add some
        # bottom room for the actual colored line band.
        up = int(round(missing * (0.62 if assay != "ct" else 0.50)))
        down = missing - up
        ny1 = max(0, ny1 - up)
        ny2 = min(kit_h - 1, ny2 + down)

        # If clipping at one border prevented the expansion, compensate on the
        # opposite side.
        if (ny2 - ny1) < target_min_h:
            missing = target_min_h - (ny2 - ny1)
            if ny1 == 0:
                ny2 = min(kit_h - 1, ny2 + missing)
            elif ny2 >= kit_h - 1:
                ny1 = max(0, ny1 - missing)

    # Guard against giant boxes.
    nbw = nx2 - nx1
    nbh = ny2 - ny1

    if nbw / max(1.0, kit_w) > max_w_ratio:
        cx = (nx1 + nx2) / 2.0
        target_w = int(kit_w * max_w_ratio)
        nx1 = int(cx - target_w / 2)
        nx2 = int(cx + target_w / 2)

    if nbh / max(1.0, kit_h) > max_h_ratio:
        # Keep the top padding priority: trim more from bottom than top.
        target_h = int(kit_h * max_h_ratio)
        overflow = (ny2 - ny1) - target_h
        if overflow > 0:
            ny2 -= int(round(overflow * 0.62))
            ny1 += int(round(overflow * 0.38))

    return clipped_bbox(nx1, ny1, nx2, ny2, kit_w, kit_h)

def adjust_bbox_from_line_layout(bbox, label_debug, expected_labels, kit_w, kit_h, assay_type="auto"):
    """
    Gently trim excessive left/right padding after the label/line profile pass.
    This is intentionally conservative: it only uses the detected/fallback label
    layout to reduce boxes that are wider than needed, while preserving enough
    margin for text and colored lines.
    """
    if bbox is None or not label_debug:
        return bbox

    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    assay = (assay_type or "auto").lower().strip()

    positions = []
    line_debug = label_debug.get("line_debug", {}) or {}
    label_positions = label_debug.get("label_positions", {}) or {}

    for label in expected_labels:
        info = line_debug.get(label, {}) or {}
        if info.get("valid", False):
            positions.append(int(info.get("best_x", info.get("label_x", 0))))
        elif label in label_positions:
            positions.append(int(label_positions[label]))

    if len(positions) >= 2:
        min_x = max(0, min(positions))
        max_x = min(bw - 1, max(positions))
        spread = max(1, max_x - min_x)

        if assay == "dengue":
            margin = int(max(bw * 0.145, spread * 0.32, kit_w * 0.020))
            min_w_ratio = 0.225
            max_w_ratio = 0.365
        elif assay == "hiv":
            margin = int(max(bw * 0.150, spread * 0.32, kit_w * 0.020))
            min_w_ratio = 0.220
            max_w_ratio = 0.370
        else:
            margin = int(max(bw * 0.155, spread * 0.34, kit_w * 0.018))
            min_w_ratio = 0.180
            max_w_ratio = 0.340

        tx1 = x1 + min_x - margin
        tx2 = x1 + max_x + margin

        target_w = tx2 - tx1
        min_w = int(round(kit_w * min_w_ratio))
        max_w = int(round(kit_w * max_w_ratio))

        if target_w < min_w:
            c = (tx1 + tx2) / 2.0
            tx1 = int(round(c - min_w / 2))
            tx2 = int(round(c + min_w / 2))

        if target_w > max_w:
            c = (tx1 + tx2) / 2.0
            tx1 = int(round(c - max_w / 2))
            tx2 = int(round(c + max_w / 2))

        # Only trim if it is meaningful and not an aggressive crop.
        if (tx2 - tx1) < bw * 0.96 and (tx2 - tx1) >= min_w:
            x1 = max(0, tx1)
            x2 = min(kit_w - 1, tx2)

    # Final height sanity. Do not re-center vertically because most user notes
    # call for small top/bottom expansion, not radical vertical relocation.
    min_h_ratio = 0.350 if assay == "dengue" else (0.335 if assay == "hiv" else 0.280)
    max_h_ratio = 0.525 if assay == "dengue" else (0.510 if assay == "hiv" else 0.470)

    min_h = int(round(kit_h * min_h_ratio))
    max_h = int(round(kit_h * max_h_ratio))

    if (y2 - y1) < min_h:
        missing = min_h - (y2 - y1)
        up = int(round(missing * 0.58))
        down = missing - up
        y1 = max(0, y1 - up)
        y2 = min(kit_h - 1, y2 + down)

    if (y2 - y1) > max_h:
        c = (y1 + y2) / 2.0
        y1 = int(round(c - max_h / 2))
        y2 = int(round(c + max_h / 2))

    return clipped_bbox(x1, y1, x2, y2, kit_w, kit_h) or bbox
def strip_label_score(label_debug, expected_labels, strip_width):
    """
    Scores a strip candidate by the label/line evidence inside it.
    This prevents choosing a visually detailed but wrong box.
    """
    line_debug = label_debug.get("line_debug", {}) or {}

    valid_count = 0
    strength_sum = 0.0
    xs = []

    for label in expected_labels:
        item = line_debug.get(label)
        if not item:
            continue

        center_score = float(item.get("center_score", 0.0))
        contrast_score = float(item.get("contrast_score", 0.0))
        red_peak = float(item.get("red_peak", 0.0))
        strength = max(center_score, red_peak, contrast_score * 8.0)

        if item.get("valid", False):
            valid_count += 1
            strength_sum += strength
            xs.append(int(item.get("best_x", item.get("label_x", 0))))
        else:
            strength_sum += min(0.15, strength * 0.20)

    n = max(1, len(expected_labels))

    score = valid_count * 1.25 + min(1.20, strength_sum * 0.55)

    label_source = str(label_debug.get("label_positions_source", "fallback_prior") or "fallback_prior")
    label_peak_count = int(label_debug.get("label_peak_count", 0) or 0)

    # v21: fallback label priors are not proof.  They are allowed as a rescue
    # when the actual result lines are strong, but candidates with no real label
    # text evidence should no longer dominate the ranking.
    if label_source != "detected_text_peaks":
        if valid_count >= n:
            score -= 0.45
        else:
            score -= 1.05

    # If the ROI is supposed to contain 3 printed labels but the upper band has
    # fewer than 2 text peaks, treat it as suspicious unless line evidence is
    # exceptionally strong.
    if n >= 3 and label_peak_count < 2 and valid_count < n:
        score -= 0.85

    # If 3 labels collapse into the same narrow area, it is usually a wrong box
    # or duplicate assignment. Reward a realistic spread.
    if len(xs) >= 2:
        spread = (max(xs) - min(xs)) / max(1.0, strip_width)

        if n == 3:
            if 0.24 <= spread <= 0.72:
                score += 0.85
            elif spread < 0.18:
                score -= 1.65
            elif spread < 0.22:
                score -= 0.65
        elif n == 2:
            if 0.16 <= spread <= 0.62:
                score += 0.65
            elif spread < 0.10:
                score -= 0.90

    # Require at least one valid control-like line. Since the expected order may
    # be 2/1/C, we check by label name, not by position.
    c_info = line_debug.get("C")
    if c_info and c_info.get("valid", False):
        score += 0.95
    elif "C" in expected_labels:
        score -= 0.85

    return float(score), int(valid_count)


def evaluate_strip_candidate(oriented, masks, candidate_info, expected_labels, assay_type):
    h, w = oriented.shape[:2]
    base_bbox = candidate_info["bbox"]

    # Consider both the raw anchor and an edge-refined variant.
    bboxes = [base_bbox]
    refined_bbox = refine_anchor_bbox_by_edges(oriented, masks, base_bbox)
    if refined_bbox is not None and refined_bbox != base_bbox:
        bboxes.append(refined_bbox)

    best_eval = None

    for bbox in bboxes:
        final_bbox = expand_strip_bbox_for_labels(bbox, w, h, assay_type)
        if final_bbox is None:
            continue

        x1, y1, x2, y2 = final_bbox
        bw = x2 - x1
        bh = y2 - y1

        if bw <= 0 or bh <= 0:
            continue

        width_ratio = bw / max(1.0, w)
        height_ratio = bh / max(1.0, h)
        aspect = bw / max(1.0, bh)

        # Geometry guard. Tighter than the old one for width, but with enough
        # height tolerance for the new "increase height" feedback.
        if width_ratio < 0.125 or width_ratio > 0.430:
            continue
        if height_ratio < 0.245 or height_ratio > 0.535:
            continue
        if aspect < 0.78 or aspect > 4.20:
            continue

        strip_roi = oriented[y1:y2, x1:x2].copy()
        if strip_roi.size == 0:
            continue

        label_debug = analyze_strip_labels(strip_roi, expected_labels)

        adjusted_bbox = adjust_bbox_from_line_layout(
            final_bbox,
            label_debug,
            expected_labels,
            kit_w=w,
            kit_h=h,
            assay_type=assay_type,
        )

        if adjusted_bbox is not None and adjusted_bbox != final_bbox:
            ax1, ay1, ax2, ay2 = adjusted_bbox
            adjusted_roi = oriented[ay1:ay2, ax1:ax2].copy()
            if adjusted_roi.size:
                adjusted_debug = analyze_strip_labels(adjusted_roi, expected_labels)
                adjusted_score, adjusted_valid = strip_label_score(adjusted_debug, expected_labels, adjusted_roi.shape[1])
                old_score, old_valid = strip_label_score(label_debug, expected_labels, strip_roi.shape[1])

                # Accept the trim if it does not destroy line evidence, or if it
                # improves the label/line score. This prevents over-cropping.
                if adjusted_valid >= max(1, old_valid - 1) and adjusted_score >= old_score - 0.45:
                    final_bbox = adjusted_bbox
                    strip_roi = adjusted_roi
                    label_debug = adjusted_debug
                    label_score = adjusted_score
                    valid_lines = adjusted_valid
                else:
                    label_score = old_score
                    valid_lines = old_valid
            else:
                label_score, valid_lines = strip_label_score(label_debug, expected_labels, strip_roi.shape[1])
        else:
            label_score, valid_lines = strip_label_score(label_debug, expected_labels, strip_roi.shape[1])

        x1, y1, x2, y2 = final_bbox
        bw = x2 - x1
        bh = y2 - y1
        width_ratio = bw / max(1.0, w)
        height_ratio = bh / max(1.0, h)
        aspect = bw / max(1.0, bh)
        cx_ratio = ((x1 + x2) / 2.0) / max(1.0, w)
        cy_ratio = ((y1 + y2) / 2.0) / max(1.0, h)

        # Start with visual anchor score, then let line/label evidence dominate.
        score = float(candidate_info.get("score", 0.0)) + label_score * 1.45

        if assay_type == "dengue":
            # Most accepted Dengue boxes after trimming should be moderately wide
            # and tall enough to include labels + line band.
            if 0.215 <= width_ratio <= 0.365:
                score += 0.45
            if 0.345 <= height_ratio <= 0.515:
                score += 0.55
            if height_ratio < 0.335:
                score -= (0.335 - height_ratio) * 5.0
            if width_ratio > 0.385:
                score -= (width_ratio - 0.385) * 6.0
        elif assay_type == "hiv":
            if 0.215 <= width_ratio <= 0.370:
                score += 0.40
            if 0.330 <= height_ratio <= 0.505:
                score += 0.50
            if height_ratio < 0.320:
                score -= (0.320 - height_ratio) * 5.0
        else:
            if 0.18 <= width_ratio <= 0.34:
                score += 0.32
            if 0.25 <= height_ratio <= 0.47:
                score += 0.32

        if aspect < 1.05 and valid_lines < len(expected_labels):
            score -= 0.85
        if height_ratio > 0.515 and valid_lines < len(expected_labels):
            score -= 1.20
        if width_ratio < 0.17 and valid_lines < len(expected_labels):
            score -= 0.90

        # Keep a soft location prior only. Several of your notes explicitly ask
        # for a right shift, so do not over-penalize right-side candidates when
        # their labels/lines are valid.
        if assay_type in {"dengue", "hiv"}:
            if cx_ratio > 0.64 and valid_lines < len(expected_labels):
                score -= (cx_ratio - 0.64) * 5.0
            if cx_ratio < 0.22 and valid_lines < len(expected_labels):
                score -= (0.22 - cx_ratio) * 4.0

        # Wrong large boxes often pass because all three labels become "valid"
        # in a noisy region. Penalize high mask density when geometry is already
        # wide/tall; true strip boxes can still win via label_score.
        mask_density = float(candidate_info.get("mask_density", 0.0) or 0.0)
        if mask_density > 0.68 and (width_ratio > 0.31 or height_ratio > 0.49):
            score -= (mask_density - 0.68) * 1.6

        item = {
            "score": float(score),
            "bbox": final_bbox,
            "raw_bbox": bbox,
            "candidate": candidate_info,
            "label_debug": label_debug,
            "valid_lines": valid_lines,
            "width_ratio": width_ratio,
            "height_ratio": height_ratio,
            "aspect": aspect,
            "center_x": cx_ratio,
            "center_y": cy_ratio,
        }

        if best_eval is None or item["score"] > best_eval["score"]:
            best_eval = item

    return best_eval


    # Consider both the raw anchor and an edge-refined variant.
    bboxes = [base_bbox]
    refined_bbox = refine_anchor_bbox_by_edges(oriented, masks, base_bbox)
    if refined_bbox is not None and refined_bbox != base_bbox:
        bboxes.append(refined_bbox)

    best_eval = None

    for bbox in bboxes:
        final_bbox = expand_strip_bbox_for_labels(bbox, w, h, assay_type)
        if final_bbox is None:
            continue

        x1, y1, x2, y2 = final_bbox
        bw = x2 - x1
        bh = y2 - y1

        if bw <= 0 or bh <= 0:
            continue

        width_ratio = bw / max(1.0, w)
        height_ratio = bh / max(1.0, h)
        aspect = bw / max(1.0, bh)

        # Geometry guard. This still allows the relatively tall closeup boxes
        # that were marked PASS, but penalizes extreme wrong boxes.
        if width_ratio < 0.135 or width_ratio > 0.48:
            continue
        if height_ratio < 0.18 or height_ratio > 0.56:
            continue
        if aspect < 0.90 or aspect > 4.60:
            continue

        strip_roi = oriented[y1:y2, x1:x2].copy()
        if strip_roi.size == 0:
            continue

        label_debug = analyze_strip_labels(strip_roi, expected_labels)
        label_score, valid_lines = strip_label_score(label_debug, expected_labels, strip_roi.shape[1])

        # Start with visual anchor score, then let line/label evidence dominate.
        score = float(candidate_info.get("score", 0.0)) + label_score * 1.35

        # Prefer boxes that include label text area, but avoid very tall/narrow
        # false boxes unless line evidence is excellent.
        if 0.22 <= width_ratio <= 0.36:
            score += 0.35
        if 0.28 <= height_ratio <= 0.48:
            score += 0.35

        if aspect < 1.15:
            score -= 0.65
        if height_ratio > 0.50 and valid_lines < len(expected_labels):
            score -= 1.00
        if width_ratio < 0.17 and valid_lines < len(expected_labels):
            score -= 0.90

        # Location prior after sample_right orientation: primary strip is usually
        # not far right of the kit body.
        cx_ratio = ((x1 + x2) / 2.0) / max(1.0, w)
        if assay_type in {"dengue", "hiv"}:
            if cx_ratio > 0.58 and valid_lines < len(expected_labels):
                score -= (cx_ratio - 0.58) * 5.0

        item = {
            "score": float(score),
            "bbox": final_bbox,
            "raw_bbox": bbox,
            "candidate": candidate_info,
            "label_debug": label_debug,
            "valid_lines": valid_lines,
            "width_ratio": width_ratio,
            "height_ratio": height_ratio,
            "aspect": aspect,
        }

        if best_eval is None or item["score"] > best_eval["score"]:
            best_eval = item

    return best_eval

def find_best_strip_candidate(
    oriented,
    masks,
    expected_labels,
    assay_type,
    exclude_bbox=None,
    speed_mode: str = "fast",
    top_k: Optional[int] = None,
):
    """
    Fast two-stage search:
      1. Score every anchor using cheap image statistics.
      2. Run expensive label/line analysis only on the best anchors.

    This keeps GUI navigation responsive while preserving the same final scoring
    logic for the shortlisted candidates.
    """
    preset = get_speed_preset(speed_mode)
    if top_k is None:
        top_k = int(preset.get("top_k", 30) or 0)

    anchor_infos = []
    raw_anchor_count = 0

    for name, bbox in generate_anchor_candidates(oriented, speed_mode=speed_mode):
        raw_anchor_count += 1

        if exclude_bbox is not None and bbox_iou(bbox, exclude_bbox) > 0.30:
            continue

        info = score_anchor_bbox(oriented, masks, bbox, name)
        if info is None:
            continue

        anchor_infos.append(info)

    anchor_infos.sort(key=lambda item: item.get("score", 0.0), reverse=True)

    if top_k and top_k > 0:
        anchor_infos = anchor_infos[:top_k]

    evaluated = []

    for info in anchor_infos:
        ev = evaluate_strip_candidate(oriented, masks, info, expected_labels, assay_type)
        if ev is not None:
            ev["raw_anchor_count"] = raw_anchor_count
            ev["shortlisted_anchor_count"] = len(anchor_infos)
            evaluated.append(ev)

    evaluated.sort(key=lambda item: item["score"], reverse=True)
    return evaluated




def expand_bbox_to_model_label_zone(bbox, kit_w, kit_h, assay_type="auto"):
    """
    Convert the detector's internal anchor/strip box into the ROI used by the ML model.

    The model was trained on crops that include the printed guide characters
    (C/1/2, 1/2/C, C/M/G, G/M/C, C/T, T/C) AND the actual result-line window.
    So the final exported/drawn box must be a label+line zone, not only the thin
    colored result-strip window.
    """
    if bbox is None:
        return None

    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    assay = (assay_type or "auto").lower().strip()

    if assay == "ct":
        min_w_ratio = 0.205
        max_w_ratio = 0.340
        min_h_ratio = 0.390
        max_h_ratio = 0.540
        side_pad_ratio = 0.085
        top_bias = 0.66
    elif assay == "dengue":
        min_w_ratio = 0.245
        max_w_ratio = 0.385
        min_h_ratio = 0.430
        max_h_ratio = 0.570
        side_pad_ratio = 0.075
        top_bias = 0.68
    elif assay == "hiv":
        min_w_ratio = 0.240
        max_w_ratio = 0.380
        min_h_ratio = 0.420
        max_h_ratio = 0.560
        side_pad_ratio = 0.080
        top_bias = 0.68
    else:
        min_w_ratio = 0.210
        max_w_ratio = 0.360
        min_h_ratio = 0.360
        max_h_ratio = 0.530
        side_pad_ratio = 0.080
        top_bias = 0.64

    # First, add light natural padding. Then enforce minimum model-crop size.
    pad_x = int(round(bw * side_pad_ratio))
    pad_top = int(round(bh * 0.16))
    pad_bottom = int(round(bh * 0.10))

    nx1 = max(0, x1 - pad_x)
    nx2 = min(kit_w - 1, x2 + pad_x)
    ny1 = max(0, y1 - pad_top)
    ny2 = min(kit_h - 1, y2 + pad_bottom)

    min_w = int(round(kit_w * min_w_ratio))
    max_w = int(round(kit_w * max_w_ratio))
    min_h = int(round(kit_h * min_h_ratio))
    max_h = int(round(kit_h * max_h_ratio))

    # The printed characters are above the colored line window, so missing
    # height is biased upward.
    if (nx2 - nx1) < min_w:
        missing = min_w - (nx2 - nx1)
        left = missing // 2
        right = missing - left
        nx1 = max(0, nx1 - left)
        nx2 = min(kit_w - 1, nx2 + right)
        if (nx2 - nx1) < min_w:
            if nx1 == 0:
                nx2 = min(kit_w - 1, nx1 + min_w)
            elif nx2 >= kit_w - 1:
                nx1 = max(0, nx2 - min_w)

    if (ny2 - ny1) < min_h:
        missing = min_h - (ny2 - ny1)
        up = int(round(missing * top_bias))
        down = missing - up
        ny1 = max(0, ny1 - up)
        ny2 = min(kit_h - 1, ny2 + down)
        if (ny2 - ny1) < min_h:
            if ny1 == 0:
                ny2 = min(kit_h - 1, ny1 + min_h)
            elif ny2 >= kit_h - 1:
                ny1 = max(0, ny2 - min_h)

    # Clamp extreme boxes. This is still the model crop, so do not over-trim;
    # just prevent huge unrelated areas from entering the training crop.
    if (nx2 - nx1) > max_w:
        cx = (nx1 + nx2) / 2.0
        nx1 = int(round(cx - max_w / 2))
        nx2 = int(round(cx + max_w / 2))

    if (ny2 - ny1) > max_h:
        cy = (ny1 + ny2) / 2.0
        ny1 = int(round(cy - max_h / 2))
        ny2 = int(round(cy + max_h / 2))

    return clipped_bbox(nx1, ny1, nx2, ny2, kit_w, kit_h)


def _active_bounds_1d(profile, min_span=3, percentile=68, floor=0.035):
    """Return active low/high indices from a 1D activity profile."""
    if profile is None or len(profile) == 0:
        return None

    p = np.asarray(profile, dtype=np.float32)
    if float(np.max(p) - np.min(p)) < 1e-6:
        return None

    p = normalize_profile(p)
    thr = max(float(np.percentile(p, percentile)) * 0.55, floor)
    active = p >= thr

    if not np.any(active):
        return None

    # Use all meaningful active pixels rather than only the longest segment,
    # because label text and result lines can be separated.
    idxs = np.where(active)[0]
    if len(idxs) < min_span:
        return None

    return int(idxs[0]), int(idxs[-1] + 1)


def _content_mask_for_roi(roi_bgr):
    """Build a dynamic content mask from the current image only."""
    if roi_bgr is None or roi_bgr.size == 0:
        return None

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(gray)

    # Printed labels and faint result lines are usually darker than the plastic.
    dark_thr = np.percentile(clahe, 43)
    dark = (clahe <= dark_thr).astype(np.uint8)

    edges = cv2.Canny(clahe, 42, 118)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    edge_mask = (edges > 0).astype(np.uint8)

    b, g, r = cv2.split(roi_bgr)
    r32 = r.astype(np.int16)
    g32 = g.astype(np.int16)
    b32 = b.astype(np.int16)
    red_emphasis = r32 - ((g32 + b32) // 2)
    red_mask = (red_emphasis > 16).astype(np.uint8)

    mask = ((dark + edge_mask + red_mask) > 0).astype(np.uint8)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        np.ones((2, 2), np.uint8),
        iterations=1,
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)),
        iterations=1,
    )
    return mask


def dynamic_refine_label_zone_bbox_from_content(bbox, oriented, expected_labels, assay_type="auto"):
    """
    Refine the model ROI using only image evidence inside/around the detected region.

    This is the replacement for static/manual correction behavior:
      - no filename lookup
      - no CSV notes
      - no learned calibration table
      - no hardcoded "GMC moves right" style rule

    The crop is derived from:
      1. detected printed-label peaks in the upper band
      2. detected result-line evidence in the lower band
      3. content row/column activity around those features
      4. structural min/max guards so the model crop remains label+line zone
    """
    debug = {
        "dynamic_refine_used": False,
        "dynamic_refine_reason": "not_run",
    }

    if bbox is None or oriented is None or oriented.size == 0:
        debug["dynamic_refine_reason"] = "missing_input"
        return bbox, debug

    kit_h, kit_w = oriented.shape[:2]
    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)

    # Search only near the already detected label-zone. This keeps the detector
    # dynamic without allowing it to jump to unrelated kit markings.
    pad_x = max(8, int(round(bw * 0.18)))
    pad_top = max(8, int(round(bh * 0.16)))
    pad_bottom = max(8, int(round(bh * 0.14)))

    sx1 = max(0, x1 - pad_x)
    sx2 = min(kit_w - 1, x2 + pad_x)
    sy1 = max(0, y1 - pad_top)
    sy2 = min(kit_h - 1, y2 + pad_bottom)

    search = oriented[sy1:sy2, sx1:sx2].copy()
    if search.size == 0:
        debug["dynamic_refine_reason"] = "empty_search_roi"
        return bbox, debug

    sh, sw = search.shape[:2]
    label_debug = analyze_strip_labels(search, expected_labels)
    label_positions = label_debug.get("label_positions", {}) or {}
    label_source = str(label_debug.get("label_positions_source", "fallback_prior") or "fallback_prior")
    line_debug = label_debug.get("line_debug", {}) or {}

    # X bounds from actual label/line evidence.
    xs = []
    for label in expected_labels:
        # v21: use printed-label positions for geometry only when they came from
        # real detected text peaks.  Fallback priors can make a wrong/small box
        # look correct, so they are no longer allowed to move/tighten the ROI.
        if label_source == "detected_text_peaks" and label in label_positions:
            xs.append(int(label_positions[label]))

        info = line_debug.get(label, {}) or {}
        if info.get("valid", False):
            xs.append(int(info.get("best_x", info.get("label_x", 0))))

    content_mask = _content_mask_for_roi(search)
    if content_mask is None:
        debug["dynamic_refine_reason"] = "no_content_mask"
        return bbox, debug

    col_bounds = _active_bounds_1d(content_mask.mean(axis=0), min_span=max(3, sw // 30), percentile=66, floor=0.030)

    if len(xs) >= 2 and (max(xs) - min(xs)) >= sw * 0.12:
        min_x = max(0, min(xs))
        max_x = min(sw - 1, max(xs))
        span = max(1, max_x - min_x)
        margin_x = int(round(max(sw * 0.055, span * 0.24, kit_w * 0.010)))
        nx1 = sx1 + min_x - margin_x
        nx2 = sx1 + max_x + margin_x
        x_source = "label_line_positions"
    elif col_bounds is not None:
        a, b = col_bounds
        span = max(1, b - a)
        margin_x = int(round(max(sw * 0.045, span * 0.16)))
        nx1 = sx1 + a - margin_x
        nx2 = sx1 + b + margin_x
        x_source = "content_columns"
    else:
        nx1, nx2 = x1, x2
        x_source = "fallback_original_x"

    # Y bounds from content activity, separately considering the label band and
    # result-line band. This avoids simply locking onto the thin center line.
    label_band_end = max(1, int(sh * 0.50))
    line_band_start = min(sh - 1, int(sh * 0.42))

    label_mask = content_mask[:label_band_end, :]
    line_mask = content_mask[line_band_start:, :]

    label_bounds = _active_bounds_1d(label_mask.mean(axis=1), min_span=2, percentile=63, floor=0.025)
    line_bounds = _active_bounds_1d(line_mask.mean(axis=1), min_span=2, percentile=63, floor=0.025)

    ys = []
    if label_bounds is not None:
        ys.extend([label_bounds[0], label_bounds[1]])
    if line_bounds is not None:
        ys.extend([line_band_start + line_bounds[0], line_band_start + line_bounds[1]])

    if len(ys) >= 2:
        min_y = max(0, min(ys))
        max_y = min(sh - 1, max(ys))
        span_y = max(1, max_y - min_y)

        # Give more margin above because printed labels sit above the result
        # lines; this is structural to the cassette, not dataset memorization.
        margin_top = int(round(max(sh * 0.055, span_y * 0.18, kit_h * 0.012)))
        margin_bottom = int(round(max(sh * 0.040, span_y * 0.12, kit_h * 0.008)))
        ny1 = sy1 + min_y - margin_top
        ny2 = sy1 + max_y + margin_bottom
        y_source = "content_rows"
    else:
        ny1, ny2 = y1, y2
        y_source = "fallback_original_y"

    # Structural model-crop guards. These do not encode sample names or note
    # patterns; they just keep the crop large enough to include labels + lines.
    n_labels = max(1, len(expected_labels or []))
    if n_labels >= 3:
        min_w = int(round(kit_w * 0.205))
        max_w = int(round(kit_w * 0.390))
    else:
        min_w = int(round(kit_w * 0.165))
        max_w = int(round(kit_w * 0.340))

    min_h = int(round(kit_h * (0.340 if n_labels >= 3 else 0.290)))
    max_h = int(round(kit_h * (0.585 if n_labels >= 3 else 0.535)))

    nx1 = max(0, int(round(nx1)))
    nx2 = min(kit_w - 1, int(round(nx2)))
    ny1 = max(0, int(round(ny1)))
    ny2 = min(kit_h - 1, int(round(ny2)))

    if (nx2 - nx1) < min_w:
        cx = (nx1 + nx2) / 2.0
        nx1 = int(round(cx - min_w / 2.0))
        nx2 = int(round(cx + min_w / 2.0))
    if (nx2 - nx1) > max_w:
        cx = (nx1 + nx2) / 2.0
        nx1 = int(round(cx - max_w / 2.0))
        nx2 = int(round(cx + max_w / 2.0))

    if (ny2 - ny1) < min_h:
        cy = (ny1 + ny2) / 2.0
        # Bias min-height restoration slightly upward to protect label text.
        ny1 = int(round(cy - min_h * 0.56))
        ny2 = int(round(ny1 + min_h))
    if (ny2 - ny1) > max_h:
        cy = (ny1 + ny2) / 2.0
        ny1 = int(round(cy - max_h / 2.0))
        ny2 = int(round(cy + max_h / 2.0))

    refined = clipped_bbox(nx1, ny1, nx2, ny2, kit_w, kit_h)
    if refined is None:
        debug["dynamic_refine_reason"] = "refined_invalid"
        return bbox, debug

    # Safety: reject extreme jumps from the original model ROI.
    oxc = (x1 + x2) / 2.0
    oyc = (y1 + y2) / 2.0
    rxc = (refined[0] + refined[2]) / 2.0
    ryc = (refined[1] + refined[3]) / 2.0

    if abs(rxc - oxc) > bw * 0.55 or abs(ryc - oyc) > bh * 0.55:
        debug["dynamic_refine_reason"] = "rejected_large_jump"
        debug["dynamic_x_source"] = x_source
        debug["dynamic_y_source"] = y_source
        return bbox, debug

    debug.update({
        "dynamic_refine_used": refined != bbox,
        "dynamic_refine_reason": "accepted" if refined != bbox else "same_as_input",
        "dynamic_x_source": x_source,
        "dynamic_y_source": y_source,
        "dynamic_search_bbox": (sx1, sy1, sx2, sy2),
        "dynamic_label_positions": label_positions,
        "dynamic_label_positions_source": label_source,
    })

    return refined, debug


def normalize_roi_adjust_tokens(value) -> List[str]:
    """
    Normalize note tokens from the GUI into ROI adjustment commands.

    Unlike v11, this intentionally preserves repeated tokens. Example:
      move_right, move_right
    means two right-step corrections. This is important because a single small
    step was too subtle in the GUI.
    """
    raw = str(value or "").lower().strip()
    if not raw:
        return []

    normalized = raw.replace("-", "_").replace("/", " ")
    normalized = normalized.replace("shift", "move")
    normalized = normalized.replace("upward", "up")
    normalized = normalized.replace("downward", "down")
    normalized = normalized.replace("to the right", "right")
    normalized = normalized.replace("to the left", "left")

    # Split primarily by comma so quick-button tokens can repeat.
    chunks = [c.strip() for c in normalized.split(",") if c.strip()]
    if not chunks:
        chunks = [normalized]

    aliases = {
        "move_left": ["move_left", "move left"],
        "move_right": ["move_right", "move right"],
        "move_up": ["move_up", "move up"],
        "move_down": ["move_down", "move down"],
        "expand_up": ["expand_up", "expand up", "increase height up", "increase height_up"],
        "expand_down": ["expand_down", "expand down", "increase height down", "increase height_down"],
        "trim_left": ["trim_left", "trim left"],
        "trim_right": ["trim_right", "trim right"],
        "trim_top": ["trim_top", "trim top", "trim up"],
        "trim_bottom": ["trim_bottom", "trim bottom", "trim down"],
        "keep_ratio": ["keep_ratio", "keep ratio", "preserve ratio"],
        "keep_width": ["keep_width", "keep width"],
    }

    found = []
    for chunk in chunks:
        matched = False
        for token, patterns in aliases.items():
            if any(p in chunk for p in patterns):
                found.append(token)
                matched = True
                break

        # Also catch free text with multiple words in one chunk.
        if not matched:
            for token, patterns in aliases.items():
                if any(p in normalized for p in patterns):
                    if token not in {"keep_ratio", "keep_width"}:
                        found.append(token)
                    elif token not in found:
                        found.append(token)

    return found


def apply_roi_adjust_tokens_to_bbox(bbox, kit_w, kit_h, tokens, flipped=False, assay_type="auto"):
    """
    Apply user note corrections to the exported model ROI.

    Directions are VISUAL directions from the displayed annotated image.
    Repeated tokens stack:
      move_right, move_right = two right steps
    """
    if bbox is None:
        return bbox, []

    tokens = normalize_roi_adjust_tokens(tokens)
    if not tokens:
        return bbox, []

    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    assay = (assay_type or "auto").lower().strip()

    # v11 steps were too subtle. These are still conservative, but visible.
    if assay == "dengue":
        move_x = max(4, int(round(bw * 0.085)))
        move_y = max(4, int(round(bh * 0.070)))
        expand_y = max(4, int(round(bh * 0.105)))
        trim_x = max(3, int(round(bw * 0.065)))
        trim_y = max(3, int(round(bh * 0.075)))
    elif assay == "hiv":
        move_x = max(4, int(round(bw * 0.090)))
        move_y = max(4, int(round(bh * 0.070)))
        expand_y = max(4, int(round(bh * 0.105)))
        trim_x = max(3, int(round(bw * 0.065)))
        trim_y = max(3, int(round(bh * 0.075)))
    else:
        move_x = max(4, int(round(bw * 0.080)))
        move_y = max(4, int(round(bh * 0.065)))
        expand_y = max(4, int(round(bh * 0.095)))
        trim_x = max(3, int(round(bw * 0.060)))
        trim_y = max(3, int(round(bh * 0.070)))

    count = {t: tokens.count(t) for t in set(tokens)}

    # Horizontal visual direction correction.
    # If the crop was flipped to put sample well right, oriented-x is reversed
    # relative to the displayed original image.
    visual_sign = -1 if flipped else 1

    dx = 0
    dx += count.get("move_right", 0) * move_x * visual_sign
    dx -= count.get("move_left", 0) * move_x * visual_sign
    x1 += dx
    x2 += dx

    dy = 0
    dy -= count.get("move_up", 0) * move_y
    dy += count.get("move_down", 0) * move_y
    y1 += dy
    y2 += dy

    keep_width = count.get("keep_width", 0) > 0

    if not keep_width:
        # visual left/right trim; reverse the side when the oriented crop is flipped
        trim_left_total = count.get("trim_left", 0) * trim_x
        trim_right_total = count.get("trim_right", 0) * trim_x

        if not flipped:
            x1 += trim_left_total
            x2 -= trim_right_total
        else:
            x2 -= trim_left_total
            x1 += trim_right_total

    y1 -= count.get("expand_up", 0) * expand_y
    y2 += count.get("expand_down", 0) * expand_y
    y1 += count.get("trim_top", 0) * trim_y
    y2 -= count.get("trim_bottom", 0) * trim_y

    # Keep valid size after manual token application.
    min_w = max(8, int(round(kit_w * 0.18)))
    min_h = max(8, int(round(kit_h * 0.25)))
    if (x2 - x1) < min_w:
        c = (x1 + x2) / 2.0
        x1 = int(round(c - min_w / 2.0))
        x2 = int(round(c + min_w / 2.0))
    if (y2 - y1) < min_h:
        c = (y1 + y2) / 2.0
        y1 = int(round(c - min_h / 2.0))
        y2 = int(round(c + min_h / 2.0))

    # Clamp while preserving width/height when possible.
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if x2 >= kit_w:
        shift = x2 - (kit_w - 1)
        x1 -= shift
        x2 -= shift
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if y2 >= kit_h:
        shift = y2 - (kit_h - 1)
        y1 -= shift
        y2 -= shift

    adjusted = clipped_bbox(x1, y1, x2, y2, kit_w, kit_h) or bbox
    return adjusted, tokens



def apply_roi_adjust_tokens_to_display_rect(rect, tokens, assay_type="auto"):
    """
    Apply note corrections AFTER mapping the ROI into the original displayed image.

    This fixes the confusing v11/v12 behavior where move_up/move_down could look
    inverted because the movement was applied inside the warped kit crop before
    perspective/unflip mapping.

    Here the meaning is simple:
      move_up    -> y decreases on the displayed image
      move_down  -> y increases on the displayed image
      move_left  -> x decreases on the displayed image
      move_right -> x increases on the displayed image

    Expand/trim also use visual screen sides, not warped-crop sides.
    """
    if rect is None:
        return rect, []

    tokens = normalize_roi_adjust_tokens(tokens)
    if not tokens:
        return rect, []

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect
    box = cv2.boxPoints(rect).astype(np.float32)

    # Step sizes are based on the visible rectangle size, so one click is clearly
    # visible but still small enough for tuning.
    assay = (assay_type or "auto").lower().strip()
    if assay == "hiv":
        move_x = max(4.0, rw * 0.045)
        move_y = max(4.0, rh * 0.090)
        expand_y = max(4.0, rh * 0.115)
        expand_x = max(4.0, rw * 0.045)
    elif assay == "dengue":
        move_x = max(4.0, rw * 0.042)
        move_y = max(4.0, rh * 0.085)
        expand_y = max(4.0, rh * 0.110)
        expand_x = max(4.0, rw * 0.042)
    else:
        move_x = max(4.0, rw * 0.040)
        move_y = max(4.0, rh * 0.080)
        expand_y = max(4.0, rh * 0.100)
        expand_x = max(4.0, rw * 0.040)

    count = {t: tokens.count(t) for t in set(tokens)}

    # Translation in displayed image pixels.
    dx = (count.get("move_right", 0) - count.get("move_left", 0)) * move_x
    dy = (count.get("move_down", 0) - count.get("move_up", 0)) * move_y
    box[:, 0] += dx
    box[:, 1] += dy

    # Visual side operations. Use medians so this works even for rotated boxes.
    med_x = float(np.median(box[:, 0]))
    med_y = float(np.median(box[:, 1]))

    left_mask = box[:, 0] <= med_x
    right_mask = box[:, 0] >= med_x
    top_mask = box[:, 1] <= med_y
    bottom_mask = box[:, 1] >= med_y

    # Width-side operations.
    if "keep_width" not in count:
        box[left_mask, 0] += count.get("trim_left", 0) * expand_x
        box[right_mask, 0] -= count.get("trim_right", 0) * expand_x
        # Accept optional future tokens too.
        box[left_mask, 0] -= count.get("expand_left", 0) * expand_x
        box[right_mask, 0] += count.get("expand_right", 0) * expand_x

    # Height-side operations in displayed image coordinates.
    box[top_mask, 1] -= count.get("expand_up", 0) * expand_y
    box[bottom_mask, 1] += count.get("expand_down", 0) * expand_y
    box[top_mask, 1] += count.get("trim_top", 0) * expand_y
    box[bottom_mask, 1] -= count.get("trim_bottom", 0) * expand_y

    # If the side edits crossed over, fall back to simple translation-only rect.
    new_rect = normalize_rect(cv2.minAreaRect(box.astype(np.float32)))
    (_, _), (nrw, nrh), _ = new_rect
    if nrw < 8 or nrh < 8:
        return rect, tokens

    return new_rect, tokens


def build_strip_debug_from_eval(best_eval, meta, kit_w, kit_h, flipped, assay, expected_labels, orient_debug, masks, oriented, reason, roi_adjust_tokens=None):
    detector_bbox = best_eval["bbox"]

    # IMPORTANT: best_eval["bbox"] is the internal detector box. It may be
    # centered on the thin colored result window. The final returned box is the
    # ML/model ROI: printed label characters + result-line window.
    final_bbox = expand_bbox_to_model_label_zone(
        detector_bbox,
        kit_w=kit_w,
        kit_h=kit_h,
        assay_type=assay,
    ) or detector_bbox

    final_bbox, dynamic_refine_debug = dynamic_refine_label_zone_bbox_from_content(
        final_bbox,
        oriented,
        expected_labels=expected_labels,
        assay_type=assay,
    )

    # v19 runtime-clean rule:
    # Notes are never part of normal detection. They are human validation metadata,
    # not training memory and not box-correction instructions.
    #
    # Emergency/manual export only:
    #   set CONFIDEX_ALLOW_NOTE_ROI_ADJUST=1 before running if you intentionally
    #   want old note-token behavior for a temporary experiment.
    allow_note_adjust = os.environ.get("CONFIDEX_ALLOW_NOTE_ROI_ADJUST", "0").strip() == "1"
    applied_adjust_tokens = normalize_roi_adjust_tokens(roi_adjust_tokens) if allow_note_adjust else []

    x1, y1, x2, y2 = final_bbox
    bw = x2 - x1
    bh = y2 - y1

    strip_roi = oriented[y1:y2, x1:x2].copy()

    # Re-analyze on the exported model ROI so the yellow label guides and
    # green/red line guides match the exact crop that will be used by the model.
    label_debug = analyze_strip_labels(strip_roi, expected_labels)

    label_positions_oriented = {}
    line_debug_oriented = {}

    for label, local_x in label_debug["label_positions"].items():
        label_positions_oriented[label] = int(x1 + local_x)

    for label, info in label_debug["line_debug"].items():
        item = dict(info)
        item["label_x_oriented"] = int(x1 + item["label_x"])
        item["best_x_oriented"] = int(x1 + item["best_x"])
        item["line_y1_oriented"] = int(y1 + strip_roi.shape[0] * 0.45)
        item["line_y2_oriented"] = int(y1 + strip_roi.shape[0] * 0.96)
        line_debug_oriented[label] = item

    local_rect_oriented = normalize_rect(
        (
            (x1 + bw / 2.0, y1 + bh / 2.0),
            (float(bw), float(bh)),
            0.0,
        )
    )

    base_global_rect = map_local_bbox_to_original_rect(
        final_bbox,
        meta,
        kit_w=kit_w,
        flipped=flipped,
    )

    if allow_note_adjust and applied_adjust_tokens:
        global_rect, applied_adjust_tokens = apply_roi_adjust_tokens_to_display_rect(
            base_global_rect,
            tokens=roi_adjust_tokens,
            assay_type=assay,
        )
    else:
        global_rect = base_global_rect
        applied_adjust_tokens = []

    valid_lines = int(best_eval.get("valid_lines", 0))
    confidence = min(0.94, max(0.25, best_eval["score"] / 13.0 + min(0.18, valid_lines * 0.055)))

    debug = {
        "strip_detected": True,
        "strip_confidence": float(confidence),
        "strip_reason": reason,
        "strip_layout": best_eval["candidate"].get("name", ""),
        "strip_local_rect": local_rect_oriented,
        "strip_bbox_local": final_bbox,
        "strip_bbox_oriented": final_bbox,
        "strip_detector_bbox_oriented": detector_bbox,
        "strip_roi_mode": "label_zone_for_model",
        "strip_roi_adjust_tokens": applied_adjust_tokens,
        "strip_roi_adjusted_from_notes": bool(applied_adjust_tokens),
        "strip_note_adjustment_space": "display_original_image",
        "strip_base_global_rect": base_global_rect,
        "strip_dynamic_refine_used": bool(dynamic_refine_debug.get("dynamic_refine_used", False)),
        "strip_dynamic_refine_reason": dynamic_refine_debug.get("dynamic_refine_reason", ""),
        "strip_dynamic_x_source": dynamic_refine_debug.get("dynamic_x_source", ""),
        "strip_dynamic_y_source": dynamic_refine_debug.get("dynamic_y_source", ""),
        "strip_width_ratio": float(bw / max(1.0, kit_w)),
        "strip_height_ratio": float(bh / max(1.0, kit_h)),
        "strip_aspect": float(bw / max(1.0, bh)),
        "strip_area_ratio_in_kit": float((bw * bh) / float(kit_w * kit_h)),
        "strip_final_center_x_ratio": float(((x1 + x2) / 2.0) / max(1.0, kit_w)),
        "strip_final_center_y_ratio": float(((y1 + y2) / 2.0) / max(1.0, kit_h)),
        "strip_edge_density": best_eval["candidate"].get("edge_density", ""),
        "strip_mask_density": best_eval["candidate"].get("mask_density", ""),
        "strip_contrast": best_eval["candidate"].get("contrast", ""),
        "strip_center_edge_density": best_eval["candidate"].get("center_edge_density", ""),
        "strip_center_mask_density": best_eval["candidate"].get("center_mask_density", ""),
        "strip_center_x": best_eval["candidate"].get("center_x", ""),
        "strip_center_y": best_eval["candidate"].get("center_y", ""),
        "strip_roi": strip_roi,
        "label_band": label_debug["label_band"],
        "line_band": label_debug["line_band"],
        "label_peaks": label_debug["label_peaks"],
        "label_profile": label_debug["label_profile"],
        "label_positions": label_debug["label_positions"],
        "label_positions_source": label_debug.get("label_positions_source", "fallback_prior"),
        "label_peak_count": int(label_debug.get("label_peak_count", 0) or 0),
        "label_layout_score": label_debug.get("label_layout_score", ""),
        "label_layout_reliable": bool(label_debug.get("label_layout_reliable", False)),
        "label_positions_oriented": label_positions_oriented,
        "line_debug": line_debug_oriented,
        "dark_profile": label_debug["dark_profile"],
        "red_profile": label_debug["red_profile"],
        "assay_type": assay,
        "expected_labels": expected_labels,
        "orientation_flipped": bool(flipped),
    }

    debug.update(orient_debug)

    return global_rect, debug


def ct_secondary_candidate_is_real(best, primary_bbox, kit_w, kit_h):
    """
    Decide if a dengue secondary C/T strip is real enough to draw.

    Earlier v2 drew a C/T strip on almost every dengue kit because the candidate
    search can always find some two-line-looking texture. This gate keeps the
    secondary strip conditional: it must be spatially separate from the primary
    C/M/G strip and must have clear C + T line evidence.
    """
    if best is None:
        return False, "no_candidate"

    bbox = best.get("bbox")
    if bbox is None:
        return False, "no_bbox"

    if primary_bbox is not None:
        overlap = bbox_iou(bbox, primary_bbox)
        if overlap > 0.08:
            return False, f"too_close_to_primary_iou_{overlap:.3f}"

    x1, y1, x2, y2 = bbox
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)

    width_ratio = bw / max(1.0, kit_w)
    height_ratio = bh / max(1.0, kit_h)
    aspect = bw / max(1.0, bh)

    # Secondary C/T strip should be a compact strip-like region, not a large
    # full-card box. These bounds are intentionally stricter than the primary
    # detector.
    if not (0.135 <= width_ratio <= 0.340):
        return False, f"bad_width_ratio_{width_ratio:.3f}"
    if not (0.155 <= height_ratio <= 0.430):
        return False, f"bad_height_ratio_{height_ratio:.3f}"
    if not (1.05 <= aspect <= 3.80):
        return False, f"bad_aspect_{aspect:.3f}"

    if best.get("valid_lines", 0) < 2:
        return False, f"not_both_ct_lines_valid_{best.get('valid_lines', 0)}"

    label_debug = best.get("label_debug", {}) or {}
    line_debug = label_debug.get("line_debug", {}) or {}

    c_info = line_debug.get("C", {}) or {}
    t_info = line_debug.get("T", {}) or {}

    if not c_info.get("valid", False):
        return False, "ct_control_not_valid"
    if not t_info.get("valid", False):
        return False, "ct_test_not_valid"

    cx = int(c_info.get("best_x", c_info.get("label_x", 0)))
    tx = int(t_info.get("best_x", t_info.get("label_x", 0)))
    line_gap = abs(tx - cx) / max(1.0, bw)

    if not (0.13 <= line_gap <= 0.72):
        return False, f"bad_ct_line_gap_{line_gap:.3f}"

    # Much stricter than v2's score >= 4.0. This prevents random texture from
    # becoming a second strip. If a true second strip is faint, use
    # dengue_secondary_mode='always' from the caller to inspect/debug it manually.
    if float(best.get("score", 0.0)) < 8.0:
        return False, f"score_too_low_{float(best.get('score', 0.0)):.3f}"

    return True, "accepted_real_secondary_ct"


def maybe_detect_secondary_dengue_ct(
    oriented,
    masks,
    meta,
    kit_w,
    kit_h,
    flipped,
    orient_debug,
    primary_bbox,
    mode="auto",
):
    """
    Dengue combo cards sometimes contain a second C/T strip, but not always.

    mode values:
      - "none"/"off"/"never": never return a secondary strip.
      - "auto": return it only when strict C/T evidence is present.
      - "always": return the best C/T candidate for manual GUI inspection.

    The primary returned strip remains the C/M/G strip. The optional secondary
    result is only added to debug["secondary_strips"] for drawing and review.
    """
    mode = (mode or "auto").lower().strip()

    if mode in {"none", "off", "never", "false", "0", "no"}:
        return None

    evaluated = find_best_strip_candidate(
        oriented,
        masks,
        expected_labels=["C", "T"],
        assay_type="ct",
        exclude_bbox=primary_bbox,
    )

    if not evaluated:
        return None

    best = evaluated[0]

    ok, gate_reason = ct_secondary_candidate_is_real(
        best,
        primary_bbox=primary_bbox,
        kit_w=kit_w,
        kit_h=kit_h,
    )

    if mode != "always" and not ok:
        return None

    rect, dbg = build_strip_debug_from_eval(
        best,
        meta,
        kit_w,
        kit_h,
        flipped,
        "ct",
        ["C", "T"],
        orient_debug,
        masks,
        oriented,
        reason=("accepted_secondary_dengue_ct" if ok else f"debug_secondary_dengue_ct_{gate_reason}"),
    )

    dbg["secondary_gate_reason"] = gate_reason
    dbg["secondary_mode"] = mode

    return {
        "strip_rect": rect,
        "debug": dbg,
        "bbox": best["bbox"],
        "gate_reason": gate_reason,
        "accepted": bool(ok),
    }


# ============================================================
# Strip detection main
# ============================================================


def detect_result_strip(
    image_bgr,
    kit_rect,
    assay_type="auto",
    filename="",
    hiv_order="c21",
    dengue_order="gmc",
    ct_order="ct",
    orientation_mode="sample_right",
    dengue_secondary_mode="never",
    speed_mode="balanced",
    max_process_width: Optional[int] = None,
    include_debug_images: bool = True,
    roi_adjust_tokens=None,
):
    debug = {
        "strip_detected": False,
        "strip_confidence": 0.0,
        "strip_reason": "",
        "kit_crop": None,
        "oriented_kit_crop": None,
        "strip_mask": None,
        "strip_eq": None,
        "strip_edges": None,
        "strip_red_emphasis": None,
        "strip_candidate_count": 0,
        "strip_anchor_count": 0,
        "strip_shortlisted_count": 0,
        "strip_local_rect": None,
        "strip_bbox_local": None,
        "strip_bbox_oriented": None,
        "assay_type": "",
        "expected_labels": [],
        "orientation_flipped": False,
        "label_positions": {},
        "line_debug": {},
        "secondary_strips": [],
        "dengue_secondary_mode": dengue_secondary_mode,
        "dengue_order": dengue_order,
        "ct_order": ct_order,
        "speed_mode": speed_mode,
        "roi_adjust_tokens": [],  # v19: notes are saved for review only; they do not move detector boxes.
    }

    if image_bgr is None or kit_rect is None:
        debug["strip_reason"] = "no_kit_rect"
        return None, debug

    kit_crop_full, meta_full = warp_kit_from_rect(image_bgr, kit_rect)

    if kit_crop_full is None or meta_full is None:
        debug["strip_reason"] = "kit_warp_failed"
        return None, debug

    h0, w0 = kit_crop_full.shape[:2]

    if w0 < 80 or h0 < 20:
        debug["strip_reason"] = "kit_crop_too_small"
        return None, debug

    preset = get_speed_preset(speed_mode)
    if max_process_width is None:
        max_process_width = preset.get("max_process_width", 1100)

    kit_crop, meta, process_info = resize_kit_for_processing(
        kit_crop_full,
        meta_full,
        max_process_width=max_process_width,
    )

    debug.update(process_info)
    debug["kit_crop"] = kit_crop
    debug["_meta_for_drawing"] = meta

    oriented, orient_info = orient_kit_sample_well_right(kit_crop, mode=orientation_mode)
    debug.update(orient_info)
    debug["oriented_kit_crop"] = oriented

    assay = infer_assay_type(filename, forced=assay_type)
    expected_labels = get_expected_labels(assay, hiv_order=hiv_order, dengue_order=dengue_order, ct_order=ct_order)

    debug["assay_type"] = assay
    debug["expected_labels"] = expected_labels

    h, w = oriented.shape[:2]

    masks = build_strip_debug_images(oriented)

    if include_debug_images:
        debug["strip_mask"] = masks["strip_mask"]
        debug["strip_eq"] = masks["eq"]
        debug["strip_edges"] = masks["edges"]
        debug["strip_red_emphasis"] = masks["red_emphasis"]

    evaluated = find_best_strip_candidate(
        oriented,
        masks,
        expected_labels=expected_labels,
        assay_type=assay,
        exclude_bbox=None,
        speed_mode=speed_mode,
    )

    debug["strip_candidate_count"] = len(evaluated)
    if evaluated:
        debug["strip_anchor_count"] = evaluated[0].get("raw_anchor_count", "")
        debug["strip_shortlisted_count"] = evaluated[0].get("shortlisted_anchor_count", "")

    if not evaluated:
        debug["strip_reason"] = "no_label_aware_candidate"
        return None, debug

    best = evaluated[0]

    if best["valid_lines"] <= 0:
        reason = "accepted_anchor_but_no_valid_lines"
    else:
        reason = "accepted_label_aware_v2"

    global_rect, final_debug = build_strip_debug_from_eval(
        best,
        meta,
        kit_w=w,
        kit_h=h,
        flipped=debug["orientation_flipped"],
        assay=assay,
        expected_labels=expected_labels,
        orient_debug=orient_info,
        masks=masks,
        oriented=oriented,
        reason=reason,
        roi_adjust_tokens=roi_adjust_tokens,
    )

    # Keep only what the GUI/drawing needs by default. CLI debug output can keep
    # the heavy arrays by passing include_debug_images=True.
    final_debug["kit_crop"] = kit_crop
    final_debug["oriented_kit_crop"] = oriented
    final_debug["_meta_for_drawing"] = meta
    final_debug.update(process_info)
    final_debug["speed_mode"] = speed_mode
    final_debug["strip_anchor_count"] = best.get("raw_anchor_count", "")
    final_debug["strip_shortlisted_count"] = best.get("shortlisted_anchor_count", "")
    final_debug["strip_candidate_count"] = len(evaluated)

    if include_debug_images:
        final_debug["strip_mask"] = masks["strip_mask"]
        final_debug["strip_eq"] = masks["eq"]
        final_debug["strip_edges"] = masks["edges"]
        final_debug["strip_red_emphasis"] = masks["red_emphasis"]
    else:
        final_debug["strip_mask"] = None
        final_debug["strip_eq"] = None
        final_debug["strip_edges"] = None
        final_debug["strip_red_emphasis"] = None

    secondary = []
    if assay == "dengue" and (dengue_secondary_mode or "never").lower().strip() not in {"none", "off", "never", "false", "0", "no"}:
        sec = maybe_detect_secondary_dengue_ct(
            oriented,
            masks,
            meta,
            kit_w=w,
            kit_h=h,
            flipped=final_debug["orientation_flipped"],
            orient_debug=orient_info,
            primary_bbox=final_debug.get("strip_bbox_oriented"),
            mode=dengue_secondary_mode,
        )
        if sec is not None:
            secondary.append(sec)

    final_debug["secondary_strips"] = secondary
    final_debug["dengue_secondary_mode"] = dengue_secondary_mode
    final_debug["dengue_order"] = dengue_order
    final_debug["ct_order"] = ct_order
    final_debug["secondary_strip_count"] = len(secondary)

    return global_rect, final_debug


# ============================================================
# Annotation drawing

# ============================================================

COLOR_KIT = (0, 255, 0)
COLOR_STRIP = (255, 0, 0)
COLOR_LABEL = (0, 255, 255)
COLOR_LINE_VALID = (0, 255, 0)
COLOR_LINE_WEAK = (0, 0, 255)
COLOR_LINE_TEXT = (255, 255, 255)


def draw_text_with_bg(img, text, org, color=(255, 255, 255), bg=(0, 0, 0), scale=0.58, thickness=1):
    x, y = int(org[0]), int(org[1])
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, scale, thickness)
    cv2.rectangle(img, (x - 3, y - th - 4), (x + tw + 4, y + base + 4), bg, -1)
    cv2.putText(img, text, (x, y), font, scale, color, thickness, cv2.LINE_AA)


def map_oriented_line_to_original(x, y1, y2, meta, kit_w, flipped):
    pts = np.array([[x, y1], [x, y2]], dtype=np.float32)
    return map_oriented_points_to_original(pts, meta, kit_w, flipped)


def draw_original_annotation(image_bgr, kit_rect, strip_rect, strip_debug):
    annotated = image_bgr.copy()

    if kit_rect is not None:
        draw_rotated_box(annotated, kit_rect, label="KIT", color=COLOR_KIT, thickness=4)

    if strip_rect is not None:
        draw_rotated_box(annotated, strip_rect, label="STRIP", color=COLOR_STRIP, thickness=3)
    else:
        draw_text_with_bg(annotated, "STRIP NOT DETECTED", (40, 120), color=(0, 0, 255), scale=0.9, thickness=2)
        return annotated

    # Draw optional secondary strips, for example dengue combo C/T strip.
    for idx, sec in enumerate(strip_debug.get("secondary_strips", []) or [], start=2):
        sec_rect = sec.get("strip_rect")
        if sec_rect is not None:
            draw_rotated_box(
                annotated,
                sec_rect,
                label=f"STRIP {idx}",
                color=(255, 128, 0),
                thickness=3,
            )

    kit_crop = strip_debug.get("kit_crop")
    if kit_crop is None:
        return annotated

    h, w = kit_crop.shape[:2]
    meta = strip_debug.get("_meta_for_drawing")
    flipped = bool(strip_debug.get("orientation_flipped", False))

    if meta is None:
        return annotated

    def draw_line_items(debug_block, prefix=""):
        for label, item in debug_block.get("line_debug", {}).items():
            lx = item.get("label_x_oriented")
            bx = item.get("best_x_oriented")
            y1 = item.get("line_y1_oriented")
            y2 = item.get("line_y2_oriented")

            if lx is None or bx is None or y1 is None or y2 is None:
                continue

            label_pts = map_oriented_line_to_original(lx, y1, y2, meta, w, flipped).astype(np.int32)
            cv2.line(
                annotated,
                tuple(label_pts[0]),
                tuple(label_pts[1]),
                COLOR_LABEL,
                2,
                cv2.LINE_AA,
            )

            line_pts = map_oriented_line_to_original(bx, y1, y2, meta, w, flipped).astype(np.int32)
            line_color = COLOR_LINE_VALID if item.get("valid", False) else COLOR_LINE_WEAK
            cv2.line(
                annotated,
                tuple(line_pts[0]),
                tuple(line_pts[1]),
                line_color,
                3,
                cv2.LINE_AA,
            )

            tx, ty = tuple(line_pts[0])
            draw_text_with_bg(
                annotated,
                f"{prefix}{label}",
                (tx + 5, ty - 5),
                color=line_color,
                scale=0.7,
                thickness=2,
            )

    # Draw label guide and actual line guide for every expected label.
    draw_line_items(strip_debug, prefix="")

    # Draw secondary strip labels too, for example dengue C/T.
    for sec in strip_debug.get("secondary_strips", []) or []:
        sec_debug = sec.get("debug", {})
        draw_line_items(sec_debug, prefix="")

    return annotated


def draw_local_strip_debug(strip_debug):
    crop = strip_debug.get("oriented_kit_crop")
    if crop is None:
        return None

    out = crop.copy()
    bbox = strip_debug.get("strip_bbox_oriented")

    if bbox is not None:
        x1, y1, x2, y2 = bbox
        cv2.rectangle(out, (x1, y1), (x2, y2), COLOR_STRIP, 2)

    for sec in strip_debug.get("secondary_strips", []) or []:
        sec_debug = sec.get("debug", {})
        sec_bbox = sec_debug.get("strip_bbox_oriented")
        if sec_bbox is not None:
            sx1, sy1, sx2, sy2 = sec_bbox
            cv2.rectangle(out, (sx1, sy1), (sx2, sy2), (255, 128, 0), 2)

    for label, item in strip_debug.get("line_debug", {}).items():
        lx = item.get("label_x_oriented")
        bx = item.get("best_x_oriented")
        y1 = item.get("line_y1_oriented")
        y2 = item.get("line_y2_oriented")

        if lx is None or bx is None or y1 is None or y2 is None:
            continue

        cv2.line(out, (lx, y1), (lx, y2), COLOR_LABEL, 1)
        color = COLOR_LINE_VALID if item.get("valid", False) else COLOR_LINE_WEAK
        cv2.line(out, (bx, y1), (bx, y2), color, 2)
        draw_text_with_bg(out, label, (bx + 4, y1 - 5), color=color, scale=0.55, thickness=1)

    return out


def draw_strip_roi_labels(strip_debug):
    strip = strip_debug.get("strip_roi")

    if strip is None:
        return None

    out = strip.copy()
    h, w = out.shape[:2]

    # Visual bands
    label_y2 = int(h * 0.48)
    line_y1 = int(h * 0.45)
    line_y2 = int(h * 0.96)

    cv2.rectangle(out, (0, 0), (w - 1, label_y2), (0, 255, 255), 1)
    cv2.rectangle(out, (0, line_y1), (w - 1, line_y2), (255, 128, 0), 1)

    for label, item in strip_debug.get("line_debug", {}).items():
        label_x = int(item["label_x"])
        best_x = int(item["best_x"])
        valid = item.get("valid", False)
        color = COLOR_LINE_VALID if valid else COLOR_LINE_WEAK

        cv2.line(out, (label_x, 0), (label_x, h - 1), COLOR_LABEL, 1)
        cv2.line(out, (best_x, 0), (best_x, h - 1), color, 2)

        txt = f"{label} {'OK' if valid else 'WEAK'}"
        draw_text_with_bg(out, txt, (max(2, best_x - 20), 18 + 18 * list(strip_debug.get("line_debug", {}).keys()).index(label)), color=color, scale=0.42, thickness=1)

    return out


def draw_profile_image(profile, markers=None, height=180):
    p = np.asarray(profile, dtype=np.float32)
    width = len(p)

    if width <= 0:
        return np.full((height, 50, 3), 255, dtype=np.uint8)

    p = normalize_profile(p)
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)

    for x in range(width - 1):
        y1 = height - 1 - int(p[x] * (height - 20))
        y2 = height - 1 - int(p[x + 1] * (height - 20))
        cv2.line(canvas, (x, y1), (x + 1, y2), (0, 0, 0), 1)

    if markers:
        for m in markers:
            x = int(m["x"])
            color = m.get("color", (0, 0, 255))
            cv2.line(canvas, (x, 0), (x, height - 1), color, 1)

    return canvas


def safe_name_part(value: Any, default: str = "unknown") -> str:
    """Small filename sanitizer for assay/label strings."""
    text = str(value or default).strip()
    if not text:
        text = default
    keep = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_"}:
            keep.append(ch)
        elif ch.isspace():
            keep.append("-")
    cleaned = "".join(keep).strip("-_")
    return cleaned or default


def save_detected_strip_crop(
    image_path: str,
    output_dir: Optional[str] = None,
    strip_debug: Optional[Dict[str, Any]] = None,
    crop_dir: Optional[str] = None,
) -> List[str]:
    """
    Save the final detected result-strip ROI to one dedicated folder.

    The saved crop is the detector's final label-zone ROI (`strip_debug["strip_roi"]`),
    so it includes the printed labels plus the result-line window. This is better
    for future ML training than cropping only the thin line window.

    Default folder:
        <output_dir>/result_strip_crops

    Returns the list of saved crop paths. Also stores the first path in
    `strip_debug["strip_crop_path"]` for CSV/reporting convenience.
    """
    saved_paths: List[str] = []

    if strip_debug is None:
        return saved_paths

    if not bool(strip_debug.get("strip_detected", False)):
        return saved_paths

    if crop_dir is None:
        crop_dir = os.path.join(output_dir or ".", "result_strip_crops")

    ensure_dir(crop_dir)

    stem = os.path.splitext(os.path.basename(image_path))[0]
    assay = safe_name_part(strip_debug.get("assay_type", "assay"))
    labels = safe_name_part("-".join(strip_debug.get("expected_labels", []) or []), "labels")

    strip_roi = strip_debug.get("strip_roi")
    if strip_roi is not None and getattr(strip_roi, "size", 0) > 0:
        main_path = os.path.join(crop_dir, f"{stem}_result_strip_{assay}_{labels}.jpg")
        if safe_save_image(main_path, strip_roi):
            saved_paths.append(main_path)
            strip_debug["strip_crop_path"] = main_path

    # Optional secondary strips, for example Dengue C/T if you enable it.
    for idx, sec in enumerate(strip_debug.get("secondary_strips", []) or [], start=2):
        sec_debug = sec.get("debug", {}) if isinstance(sec, dict) else {}
        sec_roi = sec_debug.get("strip_roi")
        if sec_roi is None or getattr(sec_roi, "size", 0) <= 0:
            continue

        sec_assay = safe_name_part(sec_debug.get("assay_type", "secondary"))
        sec_labels = safe_name_part("-".join(sec_debug.get("expected_labels", []) or []), "labels")
        sec_path = os.path.join(crop_dir, f"{stem}_result_strip_{idx}_{sec_assay}_{sec_labels}.jpg")
        if safe_save_image(sec_path, sec_roi):
            saved_paths.append(sec_path)

    if saved_paths:
        strip_debug["strip_crop_paths"] = saved_paths

    return saved_paths


def save_strip_detection_outputs(image_path, output_dir, image_bgr, kit_rect, strip_rect, strip_debug, crop_dir=None):
    ensure_dir(output_dir)
    stem = os.path.splitext(os.path.basename(image_path))[0]

    annotated = draw_original_annotation(image_bgr, kit_rect, strip_rect, strip_debug)
    safe_save_image(os.path.join(output_dir, f"{stem}_kit_strip_labels.jpg"), annotated)

    # Dedicated training/export folder for the detected label-zone result strip.
    # This saves <out>/result_strip_crops/<filename>_result_strip_<assay>_<labels>.jpg
    save_detected_strip_crop(image_path, output_dir, strip_debug, crop_dir=crop_dir)

    tune_dir = os.path.join(output_dir, f"{stem}_strip_debug")
    ensure_dir(tune_dir)

    debug_images = [
        ("kit_crop", "01_kit_crop_original_orientation.jpg"),
        ("oriented_kit_crop", "02_kit_crop_sample_well_right.jpg"),
        ("strip_eq", "03_strip_eq.jpg"),
        ("strip_edges", "04_strip_edges.jpg"),
        ("strip_mask", "05_strip_mask.jpg"),
        ("strip_red_emphasis", "06_red_emphasis.jpg"),
        ("strip_roi", "07_strip_roi.jpg"),
        ("label_band", "08_label_band.jpg"),
        ("line_band", "09_line_band.jpg"),
    ]

    for key, fname in debug_images:
        if strip_debug.get(key) is not None:
            safe_save_image(os.path.join(tune_dir, fname), strip_debug[key])

    local_dbg = draw_local_strip_debug(strip_debug)
    if local_dbg is not None:
        safe_save_image(os.path.join(tune_dir, "10_local_kit_strip_and_lines.jpg"), local_dbg)

    strip_labels = draw_strip_roi_labels(strip_debug)
    if strip_labels is not None:
        safe_save_image(os.path.join(tune_dir, "11_strip_roi_labels_and_lines.jpg"), strip_labels)

    markers = []
    for label, item in strip_debug.get("line_debug", {}).items():
        markers.append({"x": item["label_x"], "color": COLOR_LABEL})
        markers.append({"x": item["best_x"], "color": COLOR_LINE_VALID if item.get("valid", False) else COLOR_LINE_WEAK})

    if strip_debug.get("dark_profile") is not None:
        safe_save_image(
            os.path.join(tune_dir, "12_dark_profile.jpg"),
            draw_profile_image(strip_debug["dark_profile"], markers=markers),
        )

    if strip_debug.get("red_profile") is not None:
        safe_save_image(
            os.path.join(tune_dir, "13_red_profile.jpg"),
            draw_profile_image(strip_debug["red_profile"], markers=markers),
        )


# ============================================================
# CSV output
# ============================================================

CSV_COLUMNS = [
    "image_path",
    "filename",
    "kit_detected",
    "kit_source",
    "strip_detected",
    "strip_confidence",
    "strip_reason",
    "assay_type",
    "expected_labels",
    "orientation_flipped",
    "orientation_reason",
    "sample_left_score",
    "sample_right_score",
    "strip_candidate_count",
    "strip_width_ratio",
    "strip_height_ratio",
    "strip_aspect",
    "label_summary",
    "valid_line_count",
    "strip_crop_path",
]


def make_csv_row(image_path, kit_rect, kit_debug, strip_rect, strip_debug):
    label_parts = []
    valid_count = 0

    for label, item in strip_debug.get("line_debug", {}).items():
        if item.get("valid", False):
            valid_count += 1

        label_parts.append(
            f"{label}:label_x={item.get('label_x')},line_x={item.get('best_x')},valid={int(bool(item.get('valid', False)))},source={item.get('source')}"
        )

    return {
        "image_path": image_path,
        "filename": os.path.basename(image_path),
        "kit_detected": int(kit_rect is not None),
        "kit_source": (kit_debug or {}).get("kit_source", ""),
        "strip_detected": int(strip_rect is not None and strip_debug.get("strip_detected", False)),
        "strip_confidence": strip_debug.get("strip_confidence", 0.0),
        "strip_reason": strip_debug.get("strip_reason", ""),
        "assay_type": strip_debug.get("assay_type", ""),
        "expected_labels": " ".join(strip_debug.get("expected_labels", [])),
        "orientation_flipped": int(bool(strip_debug.get("orientation_flipped", False))),
        "orientation_reason": strip_debug.get("orientation_reason", ""),
        "sample_left_score": strip_debug.get("sample_left_score", ""),
        "sample_right_score": strip_debug.get("sample_right_score", ""),
        "strip_candidate_count": strip_debug.get("strip_candidate_count", 0),
        "strip_width_ratio": strip_debug.get("strip_width_ratio", ""),
        "strip_height_ratio": strip_debug.get("strip_height_ratio", ""),
        "strip_aspect": strip_debug.get("strip_aspect", ""),
        "label_summary": " | ".join(label_parts),
        "valid_line_count": valid_count,
        "strip_crop_path": strip_debug.get("strip_crop_path", ""),
    }


# ============================================================
# Main CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Detect and annotate Confidex rapid-test result strips, labels, and line positions."
    )
    parser.add_argument("path", help="Image file or folder")
    parser.add_argument("--out", default="debug_outputs_result_strip")
    parser.add_argument(
        "--crop-dir",
        default=None,
        help="Dedicated folder for saved result-strip crops. Defaults to <out>/result_strip_crops.",
    )
    parser.add_argument("--kit-csv", default=None, help="Optional CSV with kit_cx/kit_cy/kit_w/kit_h/kit_angle from your validation GUI")
    parser.add_argument("--assay", default="auto", choices=["auto", "dengue", "hiv", "ct"])
    parser.add_argument("--hiv-order", default="c21", choices=["c21", "c12"], help="Default c21 draws visible sample-right Abbott HIV order as 2 1 C. Use c12 only if you intentionally keep the kit in C 1 2 order.")
    parser.add_argument("--dengue-order", default="gmc", choices=["gmc", "cmg"], help="Default gmc draws the latest validated Dengue primary strip order as G M C. Use cmg only for older/kept orientations.")
    parser.add_argument("--orientation", default="sample_right", choices=["sample_right", "keep"], help="sample_right flips warped kit crop if sample well appears on the left")
    parser.add_argument("--speed", default="balanced", choices=["fast", "fast_gui", "balanced", "full"], help="fast is intended for GUI/manual tuning; balanced/full are slower but scan more anchors")
    parser.add_argument("--max-process-width", type=int, default=None, help="Optional override for strip-processing width. Use 0 to disable resizing.")
    parser.add_argument("--no-debug-images", action="store_true", help="Do not store large intermediate debug arrays in memory/output.")
    parser.add_argument(
        "--dengue-secondary",
        default="never",
        choices=["auto", "never", "always"],
        help="auto only draws a second dengue C/T strip when strong evidence exists; never disables it; always shows best C/T candidate for debugging",
    )
    args = parser.parse_args()

    files = list_images(args.path)
    ensure_dir(args.out)

    # v19 runtime-clean rule:
    # Do NOT auto-load confidex_kit_detector_labels.csv from the image folder.
    # The detector must redraw boxes from the current image only.
    # Use --kit-csv explicitly only for legacy comparison/debugging.
    kit_csv_map = load_kit_csv(args.kit_csv) if args.kit_csv else {}

    rows = []

    for image_path in files:
        image_bgr = cv2.imread(image_path)

        if image_bgr is None:
            continue

        kit_rect, kit_debug = detect_kit_or_csv(image_bgr, image_path, kit_csv_map)

        strip_rect = None
        strip_debug = {
            "strip_detected": False,
            "strip_reason": "kit_not_detected",
            "assay_type": infer_assay_type(image_path, args.assay),
            "expected_labels": get_expected_labels(infer_assay_type(image_path, args.assay), args.hiv_order, args.dengue_order),
        }

        if kit_rect is not None:
            strip_rect, strip_debug = detect_result_strip(
                image_bgr,
                kit_rect,
                assay_type=args.assay,
                filename=image_path,
                hiv_order=args.hiv_order,
                dengue_order=args.dengue_order,
                orientation_mode=args.orientation,
                dengue_secondary_mode=args.dengue_secondary,
                speed_mode=args.speed,
                max_process_width=args.max_process_width,
                include_debug_images=not args.no_debug_images,
            )

            # detect_result_strip already stores the correct drawing meta, including
            # the processing downscale transform. This fallback is only for older
            # detector versions.
            if strip_debug is not None and strip_debug.get("_meta_for_drawing") is None:
                kit_crop, meta = warp_kit_from_rect(image_bgr, kit_rect)
                if meta is not None:
                    strip_debug["_meta_for_drawing"] = meta

        save_strip_detection_outputs(
            image_path,
            args.out,
            image_bgr,
            kit_rect,
            strip_rect,
            strip_debug,
            crop_dir=args.crop_dir,
        )

        rows.append(make_csv_row(image_path, kit_rect, kit_debug, strip_rect, strip_debug))

        print(
            f"{os.path.basename(image_path)} | kit={kit_rect is not None} | "
            f"strip={strip_rect is not None} | assay={strip_debug.get('assay_type')} | "
            f"labels={' '.join(strip_debug.get('expected_labels', []))} | "
            f"reason={strip_debug.get('strip_reason')}"
        )

    csv_path = os.path.join(args.out, "result_strip_detection_summary.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows),\

    print(f"\nSaved summary CSV: {csv_path}")
    print(f"Saved annotated images/debug folders under: {args.out}")
    print(f"Saved result-strip crops under: {args.crop_dir or os.path.join(args.out, 'result_strip_crops')}")


if __name__ == "__main__":
    main()
