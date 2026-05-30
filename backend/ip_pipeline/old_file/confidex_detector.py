import os
import cv2
import glob
import math
import argparse
import numpy as np


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def list_images(path):
    if os.path.isfile(path):
        return [path]

    files = []
    for ext in IMAGE_EXTS:
        files.extend(glob.glob(os.path.join(path, f"*{ext}")))
        files.extend(glob.glob(os.path.join(path, f"*{ext.upper()}")))

    return sorted(set(files))


def resize_keep_ratio(image, max_side=1400):
    h, w = image.shape[:2]
    scale = min(1.0, max_side / max(h, w))

    if scale == 1.0:
        return image.copy(), 1.0

    resized = cv2.resize(
        image,
        (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def scale_rect(rect, scale):
    if rect is None:
        return None

    (cx, cy), (rw, rh), angle = rect
    return ((cx / scale, cy / scale), (rw / scale, rh / scale), angle)


def rect_original_to_small(rect, scale):
    if rect is None:
        return None

    (cx, cy), (rw, rh), angle = rect
    return ((cx * scale, cy * scale), (rw * scale, rh * scale), angle)


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


def rect_area(rect):
    (_, _), (rw, rh), _ = normalize_rect(rect)
    return float(rw * rh)


def rect_area_ratio(rect, image_shape):
    h, w = image_shape[:2]
    return rect_area(rect) / float(w * h)


def angle_to_horizontal(angle_deg):
    a = angle_deg % 180.0
    if a > 90.0:
        a = 180.0 - a
    return a


def angle_to_axis(angle_deg):
    a = angle_deg % 180.0
    return min(abs(a - 0.0), abs(a - 90.0), abs(a - 180.0))


def should_apply_angle_update(old_rect, new_rect, image_shape):
    if old_rect is None or new_rect is None:
        return False

    old_rect = normalize_rect(old_rect)
    new_rect = normalize_rect(new_rect)

    old_area = rect_area(old_rect)
    new_area = rect_area(new_rect)
    area_change = new_area / max(1.0, old_area)

    old_angle = old_rect[2]
    new_angle = new_rect[2]
    delta = abs(angle_to_horizontal(new_angle - old_angle))

    # Too tiny = visual noise. Too large = risky correction.
    if delta < 0.8:
        return False
    if delta > 6.0:
        return False

    # Angle correction should not resize/reposition the box.
    if not (0.92 <= area_change <= 1.08):
        return False

    return validate_rect_soft(new_rect, image_shape)


def rect_center_distance_score(cx, cy, w, h):
    mx, my = w / 2.0, h / 2.0
    d = math.hypot(cx - mx, cy - my)
    max_d = math.hypot(mx, my)
    return 1.0 - min(1.0, d / max_d)


def safe_save_image(path, image):
    folder = os.path.dirname(path)
    if folder:
        ensure_dir(folder)
    return cv2.imwrite(path, image)


def draw_rotated_box(image, rect, label="KIT", color=(0, 255, 0), thickness=3):
    if rect is None:
        return image

    rect = normalize_rect(rect)
    box = cv2.boxPoints(rect)
    box = np.int32(box)

    cv2.drawContours(image, [box], 0, color, thickness)

    cx, cy = map(int, rect[0])
    top_y = int(np.min(box[:, 1]))

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.7
    font_thickness = 2

    (tw, th), baseline = cv2.getTextSize(label, font, font_scale, font_thickness)

    tx = max(10, cx - tw // 2)
    ty = max(th + 10, top_y - 8)

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


def contour_touches_border(contour, shape, margin=3):
    h, w = shape[:2]
    x, y, bw, bh = cv2.boundingRect(contour)

    return (
        x <= margin
        or y <= margin
        or x + bw >= w - margin
        or y + bh >= h - margin
    )


def build_search_roi(image, mode="normal"):
    h, w = image.shape[:2]

    if mode == "closeup":
        x1, x2, y1, y2 = 0, w, 0, h
    elif mode == "wide":
        x1 = int(w * 0.02)
        x2 = int(w * 0.98)
        y1 = int(h * 0.02)
        y2 = int(h * 0.98)
    else:
        x1 = int(w * 0.05)
        x2 = int(w * 0.95)
        y1 = int(h * 0.05)
        y2 = int(h * 0.95)

    roi = image[y1:y2, x1:x2].copy()
    return roi, (x1, y1, x2, y2)


def build_white_body_mask(roi_bgr):
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)

    gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)
    eq = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(gray_blur)

    v = hsv[:, :, 2]
    s = hsv[:, :, 1]

    v_thr = max(80, np.percentile(v, 48))
    s_thr = min(120, np.percentile(s, 78) + 20)

    white = np.where((v >= v_thr) & (s <= s_thr), 255, 0).astype(np.uint8)

    bg = cv2.GaussianBlur(eq, (0, 0), sigmaX=23, sigmaY=23)
    diff = cv2.absdiff(eq, bg)

    _, diff_mask = cv2.threshold(
        diff,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )

    edges = cv2.Canny(eq, 18, 85)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    mask = cv2.bitwise_or(white, diff_mask)
    mask = cv2.bitwise_or(mask, edges)

    k_h = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 11))
    k_v = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 25))
    k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

    mask_h = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_h, iterations=2)
    mask_v = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_v, iterations=2)

    mask = cv2.bitwise_or(mask_h, mask_v)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open, iterations=1)

    return mask, eq


def build_shadow_edge_mask(roi_bgr):
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    eq = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8)).apply(gray)

    blackhat_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31))
    blackhat = cv2.morphologyEx(eq, cv2.MORPH_BLACKHAT, blackhat_kernel)

    edges = cv2.Canny(eq, 18, 90)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    grad_x = cv2.Sobel(eq, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(eq, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.convertScaleAbs(cv2.magnitude(grad_x, grad_y))

    _, bh_mask = cv2.threshold(
        blackhat,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    _, grad_mask = cv2.threshold(
        grad,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )

    mask = cv2.bitwise_or(bh_mask, edges)
    mask = cv2.bitwise_or(mask, grad_mask)

    k_h = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 13))
    k_v = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 31))
    k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

    mask_h = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_h, iterations=2)
    mask_v = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_v, iterations=2)

    mask = cv2.bitwise_or(mask_h, mask_v)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open, iterations=1)

    return mask, eq


def build_combined_mask(roi_bgr, mode="white"):
    white_mask, eq = build_white_body_mask(roi_bgr)

    if mode == "white":
        return white_mask, eq

    shadow_mask, eq2 = build_shadow_edge_mask(roi_bgr)

    if mode == "shadow":
        return shadow_mask, eq2

    combined = cv2.bitwise_or(white_mask, shadow_mask)

    k = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 13))
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, k, iterations=1)

    return combined, eq


def validate_rect_soft(rect, image_shape):
    if rect is None:
        return False

    h, w = image_shape[:2]
    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)
    axis = angle_to_axis(angle)

    if not (-0.18 * w <= cx <= 1.18 * w and -0.18 * h <= cy <= 1.18 * h):
        return False

    if rw < 55 or rh < 18:
        return False

    if area_ratio < 0.006:
        return False

    if area_ratio > 0.82:
        return False

    if not (1.12 <= aspect <= 9.5):
        return False

    if axis > 46.0:
        return False

    return True


def get_candidate_rects_from_mask(mask, image_shape, mode="white"):
    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    h, w = image_shape[:2]
    img_area = float(w * h)

    candidates = []

    for contour in contours:
        area = cv2.contourArea(contour)

        if area < img_area * 0.0022:
            continue

        if area > img_area * 0.58:
            continue

        rect = normalize_rect(cv2.minAreaRect(contour))
        (cx, cy), (rw, rh), angle = rect

        if rw < 45 or rh < 14:
            continue

        rect_area_value = rw * rh
        area_ratio = rect_area_value / img_area
        aspect = rw / max(1.0, rh)
        fill = area / max(1.0, rect_area_value)
        axis = angle_to_axis(angle)

        if not (0.005 <= area_ratio <= 0.45):
            continue

        if not (1.12 <= aspect <= 8.8):
            continue

        if axis > 38.0:
            continue

        if fill < 0.045:
            continue

        center_score = rect_center_distance_score(cx, cy, w, h)
        aspect_score = max(0.0, 1.0 - abs(aspect - 3.0) / 3.9)
        area_score = max(0.0, 1.0 - abs(area_ratio - 0.115) / 0.25)
        angle_score = max(0.0, 1.0 - axis / 38.0)
        fill_score = min(1.0, fill * 2.2)

        score = (
            center_score * 1.4
            + aspect_score * 3.1
            + area_score * 2.7
            + angle_score * 1.5
            + fill_score * 1.8
        )

        if area_ratio < 0.018:
            score -= 3.5

        if rh < 65 and area_ratio < 0.025:
            score -= 2.0

        if aspect < 1.45 and area_ratio < 0.08:
            score -= 2.0

        if 0.18 <= area_ratio <= 0.50 and 1.7 <= aspect <= 3.8 and axis <= 18:
            score += 1.2

        if contour_touches_border(contour, image_shape, margin=4) and area_ratio > 0.12:
            score -= 0.6

        if mode == "shadow":
            score -= 0.12

        candidates.append({
            "rect": rect,
            "score": score,
            "area_ratio": area_ratio,
            "aspect": aspect,
            "fill": fill,
            "mode": mode,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def expand_rect(rect, image_shape, pad_x_ratio=0.030, pad_y_ratio=0.035):
    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)

    if area_ratio >= 0.22:
        px = 0.004
        py = 0.012
    elif area_ratio >= 0.14:
        px = 0.010
        py = 0.022
    else:
        px = pad_x_ratio
        py = pad_y_ratio

    if aspect < 2.10 and area_ratio >= 0.18:
        px = 0.000
        py = min(py, 0.018)

    rw2 = rw * (1.0 + px)
    rh2 = rh * (1.0 + py)

    return normalize_rect(((cx, cy), (rw2, rh2), angle))


def order_box_points(pts):
    pts = np.array(pts, dtype=np.float32)

    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)

    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]
    ordered[2] = pts[np.argmax(s)]
    ordered[1] = pts[np.argmin(diff)]
    ordered[3] = pts[np.argmax(diff)]

    return ordered


def make_warp_from_rect(image_bgr, rect):
    rect = normalize_rect(rect)
    (_, _), (rw, rh), _ = rect

    box = cv2.boxPoints(rect)
    src = order_box_points(box)

    dst_w = max(1, int(round(rw)))
    dst_h = max(1, int(round(rh)))

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

    return crop, M, Minv


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


def rect_from_crop_box(local_box, Minv, angle_override=None):
    x1, y1, x2, y2 = local_box

    local = np.array(
        [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        dtype=np.float32,
    )

    global_pts = cv2.perspectiveTransform(local.reshape(1, 4, 2), Minv)[0]
    rect = normalize_rect(cv2.minAreaRect(global_pts.astype(np.float32)))

    if angle_override is not None:
        (cx, cy), (rw, rh), _ = rect
        rect = normalize_rect(((cx, cy), (rw, rh), angle_override))

    return rect


def detect_internal_kit_features(crop_bgr):
    h, w = crop_bgr.shape[:2]

    if h < 25 or w < 80:
        return {
            "valid": False,
            "feature_score": 0.0,
            "reason": "crop_too_small",
        }

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    eq = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 18, 90)

    dark = cv2.adaptiveThreshold(
        eq,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        7,
    )

    center_y1 = int(h * 0.18)
    center_y2 = int(h * 0.82)
    center_x1 = int(w * 0.08)
    center_x2 = int(w * 0.92)

    center_edges = edges[center_y1:center_y2, center_x1:center_x2]
    center_dark = dark[center_y1:center_y2, center_x1:center_x2]

    edge_density = np.count_nonzero(center_edges) / max(1.0, center_edges.size)
    dark_density = np.count_nonzero(center_dark) / max(1.0, center_dark.size)

    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(21, w // 12), 3),
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (3, max(13, h // 7)),
    )

    h_lines = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, horizontal_kernel, iterations=1)
    v_lines = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, vertical_kernel, iterations=1)

    h_density = np.count_nonzero(h_lines) / max(1.0, h_lines.size)
    v_density = np.count_nonzero(v_lines) / max(1.0, v_lines.size)

    contours, _ = cv2.findContours(
        dark,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    inner_feature_count = 0

    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = bw * bh

        if area < w * h * 0.001:
            continue

        if bw < w * 0.025 or bh < h * 0.025:
            continue

        if y < h * 0.04 or y + bh > h * 0.96:
            continue

        aspect = bw / max(1.0, bh)

        if 0.08 <= area / max(1.0, w * h) <= 0.25:
            inner_feature_count += 1
        elif 0.15 <= aspect <= 12.0:
            inner_feature_count += 1

    feature_score = 0.0
    feature_score += min(0.35, edge_density * 7.0)
    feature_score += min(0.25, dark_density * 5.0)
    feature_score += min(0.20, h_density * 4.0)
    feature_score += min(0.12, v_density * 3.0)
    feature_score += min(0.20, inner_feature_count * 0.04)

    # Adaptive threshold for closeups
    if h * w > 200000:  # large crop = close-up
        valid = (
            feature_score >= 0.12
            and edge_density >= 0.003
        )
    else:
        valid = (
            feature_score >= 0.18
            and edge_density >= 0.006
            and dark_density >= 0.004
        )

    return {
        "valid": valid,
        "feature_score": float(feature_score),
        "edge_density": float(edge_density),
        "dark_density": float(dark_density),
        "h_density": float(h_density),
        "v_density": float(v_density),
        "inner_feature_count": int(inner_feature_count),
        "reason": "accepted_features" if valid else "weak_internal_features",
        "feature_edges": edges,
        "feature_dark": dark,
    }


def validate_candidate_by_features(image_bgr, rect):
    if rect is None:
        return False, {
            "feature_valid": False,
            "feature_reason": "no_rect",
            "feature_score": 0.0,
        }

    try:
        crop, _, _ = make_warp_from_rect(image_bgr, rect)
    except Exception:
        return False, {
            "feature_valid": False,
            "feature_reason": "warp_failed",
            "feature_score": 0.0,
        }

    info = detect_internal_kit_features(crop)

    return info.get("valid", False), {
        "feature_valid": info.get("valid", False),
        "feature_reason": info.get("reason", ""),
        "feature_score": info.get("feature_score", 0.0),
        "feature_edges": info.get("feature_edges"),
        "feature_dark": info.get("feature_dark"),
    }


def estimate_angle_from_internal_edges(image_bgr, rect):
    if rect is None:
        return rect, {
            "angle_refined": False,
            "angle_reason": "no_rect",
        }

    original_rect = normalize_rect(rect)
    crop, M, Minv = make_warp_from_rect(image_bgr, original_rect)
    ch, cw = crop.shape[:2]

    if cw < 120 or ch < 40:
        return original_rect, {
            "angle_refined": False,
            "angle_reason": "crop_too_small",
        }

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    eq = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 18, 90)

    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=max(30, cw // 18),
        minLineLength=max(45, cw // 7),
        maxLineGap=max(10, cw // 35),
    )

    if lines is None:
        return original_rect, {
            "angle_refined": False,
            "angle_reason": "no_hough_lines",
            "angle_edges": edges,
        }

    weighted_angles = []

    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = line
        dx = float(x2 - x1)
        dy = float(y2 - y1)
        length = math.hypot(dx, dy)

        if length < cw * 0.10:
            continue

        angle = math.degrees(math.atan2(dy, dx))

        while angle <= -90.0:
            angle += 180.0
        while angle > 90.0:
            angle -= 180.0

        if abs(angle) > 25.0:
            continue

        weighted_angles.append((angle, length))

    if not weighted_angles:
        return original_rect, {
            "angle_refined": False,
            "angle_reason": "no_horizontal_internal_lines",
            "angle_edges": edges,
        }

    angles = np.array([a for a, _ in weighted_angles], dtype=np.float32)
    weights = np.array([w for _, w in weighted_angles], dtype=np.float32)

    median_angle = float(np.average(angles, weights=weights))

    if abs(median_angle) < 0.4:
        return original_rect, {
            "angle_refined": False,
            "angle_reason": f"angle_correction_too_small_{median_angle:.2f}",
            "angle_edges": edges,
        }

    (cx, cy), (rw, rh), base_angle = original_rect
    corrected = normalize_rect(((cx, cy), (rw, rh), base_angle + median_angle))

    if not validate_rect_soft(corrected, image_bgr.shape):
        return original_rect, {
            "angle_refined": False,
            "angle_reason": "angle_corrected_rect_invalid",
            "angle_delta": median_angle,
            "angle_edges": edges,
        }

    return corrected, {
        "angle_refined": True,
        "angle_reason": f"accepted_internal_edge_angle_delta_{median_angle:.2f}",
        "angle_delta": median_angle,
        "angle_edges": edges,
    }


def refine_rect_by_internal_mask(image_bgr, rect):
    if rect is None:
        return rect, {"refined": False, "refine_reason": "no_rect"}

    original_rect = normalize_rect(rect)
    (_, _), (rw, rh), _ = original_rect

    if rw < 80 or rh < 25:
        return original_rect, {"refined": False, "refine_reason": "too_small_for_refine"}

    original_area_ratio = rect_area_ratio(original_rect, image_bgr.shape)

    crop, M, Minv = make_warp_from_rect(image_bgr, original_rect)
    ch, cw = crop.shape[:2]

    mask, eq = build_combined_mask(crop, mode="combined")

    k_h = cv2.getStructuringElement(cv2.MORPH_RECT, (max(21, cw // 18), max(5, ch // 22)))
    k_v = cv2.getStructuringElement(cv2.MORPH_RECT, (max(5, cw // 45), max(17, ch // 12)))

    mask_h = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_h, iterations=1)
    mask_v = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_v, iterations=1)
    mask = cv2.bitwise_or(mask_h, mask_v)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    crop_area = float(cw * ch)

    for c in contours:
        area = cv2.contourArea(c)

        if area < crop_area * 0.008:
            continue

        x, y, bw, bh = cv2.boundingRect(c)

        if bw < cw * 0.10 or bh < ch * 0.08:
            continue

        fill = area / max(1.0, bw * bh)

        if fill < 0.025:
            continue

        boxes.append((x, y, x + bw, y + bh, area, fill))

    if not boxes:
        return original_rect, {
            "refined": False,
            "refine_reason": "no_refine_boxes_keep_original",
            "refine_crop": crop,
            "refine_mask": mask,
        }

    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes)
    y2 = max(b[3] for b in boxes)

    body_w = x2 - x1
    body_h = y2 - y1

    if body_w <= 0 or body_h <= 0:
        return original_rect, {
            "refined": False,
            "refine_reason": "bad_refine_union_keep_original",
            "refine_crop": crop,
            "refine_mask": mask,
        }

    width_ratio = body_w / max(1.0, cw)
    height_ratio = body_h / max(1.0, ch)

    if original_area_ratio >= 0.16:
        # close-up case → allow tighter shrink
        min_width_ratio = 0.38
        min_height_ratio = 0.28
    else:
        min_width_ratio = 0.55
        min_height_ratio = 0.40

    if width_ratio < min_width_ratio or height_ratio < min_height_ratio:
        return original_rect, {
            "refined": False,
            "refine_reason": f"refine_too_partial_w{width_ratio:.3f}_h{height_ratio:.3f}_keep_original",
            "refine_crop": crop,
            "refine_mask": mask,
        }

    pad_x = int(body_w * 0.045)
    pad_y = int(body_h * 0.040)

    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(cw - 1, x2 + pad_x)
    y2 = min(ch - 1, y2 + pad_y)

    refined = rect_from_crop_box((x1, y1, x2, y2), Minv)

    area_change = rect_area(refined) / max(1.0, rect_area(original_rect))

    if original_area_ratio >= 0.18:
        min_allowed_change = 0.55
    elif original_area_ratio >= 0.10:
        min_allowed_change = 0.62
    else:
        min_allowed_change = 0.68

    if area_change < min_allowed_change:
        return original_rect, {
            "refined": False,
            "refine_reason": f"unsafe_shrink_{area_change:.3f}_keep_original",
            "refine_area_change": area_change,
            "refine_crop": crop,
            "refine_mask": mask,
        }

    if area_change > 1.14:
        return original_rect, {
            "refined": False,
            "refine_reason": f"unsafe_growth_{area_change:.3f}_keep_original",
            "refine_area_change": area_change,
            "refine_crop": crop,
            "refine_mask": mask,
        }

    if not validate_rect_soft(refined, image_bgr.shape):
        return original_rect, {
            "refined": False,
            "refine_reason": "refined_not_plausible_keep_original",
            "refine_area_change": area_change,
            "refine_crop": crop,
            "refine_mask": mask,
        }

    return refined, {
        "refined": True,
        "refine_reason": "accepted_safe_union_refine",
        "refine_area_change": area_change,
        "refine_crop": crop,
        "refine_mask": mask,
    }



def snap_tiny_angle_noise_if_safe(rect, debug, image_shape):
    if rect is None:
        return rect, {
            "angle_snap_applied": False,
            "angle_snap_reason": "no_rect",
        }

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)
    horiz = angle_to_horizontal(angle)
    pass_name = debug.get("pass_name", "")

    if horiz <= 0.85:
        snapped = normalize_rect(((cx, cy), (rw, rh), 0.0))
        return snapped, {
            "angle_snap_applied": True,
            "angle_snap_reason": "sub_degree_noise_snap",
        }

    if (
        pass_name == "normal_shadow"
        and 0.108 <= area_ratio <= 0.245
        and 2.65 <= aspect <= 3.90
        and horiz <= 4.85
    ):
        snapped = normalize_rect(((cx, cy), (rw, rh), 0.0))
        return snapped, {
            "angle_snap_applied": True,
            "angle_snap_reason": "normal_shadow_low_angle_shadow_snap",
        }

    if (
        pass_name == "wide_shadow"
        and 0.095 <= area_ratio <= 0.215
        and 2.90 <= aspect <= 3.90
        and horiz <= 1.60
    ):
        snapped = normalize_rect(((cx, cy), (rw, rh), 0.0))
        return snapped, {
            "angle_snap_applied": True,
            "angle_snap_reason": "wide_shadow_low_angle_shadow_snap",
        }

    if (
        pass_name == "closeup_shadow"
        and 0.150 <= area_ratio <= 0.360
        and 2.60 <= aspect <= 3.80
        and horiz <= 5.80
    ):
        snapped = normalize_rect(((cx, cy), (rw, rh), 0.0))
        return snapped, {
            "angle_snap_applied": True,
            "angle_snap_reason": "closeup_shadow_low_angle_shadow_snap",
        }

    return rect, {
        "angle_snap_applied": False,
        "angle_snap_reason": "preserved_original_angle",
    }


def adjust_projection_rescue_width(rect, debug, image_shape):
    if rect is None:
        return rect, {
            "projection_width_adjusted": False,
            "projection_width_reason": "no_rect",
        }

    if not debug.get("projection_rescue", False):
        return rect, {
            "projection_width_adjusted": False,
            "projection_width_reason": "not_projection_rescue",
        }

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect
    h, w = image_shape[:2]

    area_ratio = rect_area_ratio(rect, image_shape)
    width_ratio = rw / max(1.0, w)

    # Important: do not expand boxes that are already huge.
    if area_ratio >= 0.25:
        return rect, {
            "projection_width_adjusted": False,
            "projection_width_reason": "large_projection_no_width_expand",
        }

    # Only expand rescued projection when it is genuinely too narrow.
    if 0.10 <= area_ratio < 0.25 and width_ratio < 0.52:
        new_w = min(w * 0.70, rw * 1.16)

        expanded = normalize_rect(((cx, cy), (new_w, rh), angle))

        if validate_rect_soft(expanded, image_shape):
            return expanded, {
                "projection_width_adjusted": True,
                "projection_width_reason": "expanded_narrow_rescue_projection_width",
            }

    return rect, {
        "projection_width_adjusted": False,
        "projection_width_reason": "projection_width_already_ok",
    }


def reject_small_random_object(rect, debug, image_shape):
    if rect is None:
        return False, "no_rect"

    rect = normalize_rect(rect)
    (_, _), (rw, rh), angle = rect
    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)
    axis = angle_to_axis(angle)
    pass_name = debug.get("pass_name", "")

    # Catches den18-style random object: small, diagonal-ish, closeup shadow.
    if pass_name.startswith("closeup_") and area_ratio < 0.070 and axis > 16.0:
        return True, "small_closeup_diagonal_random_object"

    if area_ratio < 0.018:
        return True, "too_small_area"

    if area_ratio < 0.050 and (aspect < 2.40 or axis > 18.0):
        return True, "small_bad_shape_or_angle"

    return False, "accepted_shape"


def tighten_vertical_spaces_by_edge_profile(image_bgr, rect, debug):
    """
    Targeted top/bottom-space correction. It only shrinks height when the current
    box is clearly loose, and it avoids touching already-good, thin boxes.
    """
    if rect is None:
        return rect, {"space_refined": False, "space_reason": "no_rect"}

    rect = normalize_rect(rect)
    original_area = rect_area(rect)
    area_ratio = rect_area_ratio(rect, image_bgr.shape)
    (_, _), (rw, rh), angle = rect
    aspect = rw / max(1.0, rh)
    pass_name = debug.get("pass_name", "")

    # Preserve already tight/working boxes. Most good boxes have aspect >= ~3.
    should_try = (
        area_ratio >= 0.18
        or aspect < 2.75
        or pass_name in {"closeup_shadow", "wide_shadow", "closeup_projection"}
    )
    if not should_try:
        return rect, {"space_refined": False, "space_reason": "already_tight_skip"}

    try:
        crop, M, Minv = make_warp_from_rect(image_bgr, rect)
    except Exception:
        return rect, {"space_refined": False, "space_reason": "warp_failed"}

    ch, cw = crop.shape[:2]
    if cw < 120 or ch < 40:
        return rect, {"space_refined": False, "space_reason": "crop_too_small"}

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    eq = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(gray)

    # Combine long horizontal edges + dark printed/window areas.
    edges = cv2.Canny(eq, 14, 84)
    dark = cv2.adaptiveThreshold(
        eq,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        6,
    )

    kh = cv2.getStructuringElement(cv2.MORPH_RECT, (max(35, cw // 16), 3))
    h_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kh, iterations=1)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)

    combined = cv2.bitwise_or(h_edges, dark)
    combined = cv2.dilate(combined, np.ones((3, 3), np.uint8), iterations=1)

    row_score = combined.sum(axis=1).astype(np.float32) / 255.0
    if row_score.max() <= 0:
        return rect, {"space_refined": False, "space_reason": "empty_profile", "space_mask": combined}

    # Smooth and ignore extreme top/bottom 3% noise.
    k = max(5, ch // 36)
    if k % 2 == 0:
        k += 1
    smooth = cv2.GaussianBlur(row_score.reshape(-1, 1), (1, k), 0).reshape(-1)

    y_lo_guard = int(ch * 0.03)
    y_hi_guard = int(ch * 0.97)
    profile = smooth[y_lo_guard:y_hi_guard]

    thr = max(cw * 0.022, np.percentile(profile, 50))
    active = smooth >= thr
    active[:y_lo_guard] = False
    active[y_hi_guard:] = False

    ys = np.where(active)[0]
    if len(ys) < ch * 0.22:
        return rect, {
            "space_refined": False,
            "space_reason": f"not_enough_profile_support_{len(ys)/max(1,ch):.3f}",
            "space_mask": combined,
        }

    y1 = int(np.percentile(ys, 2))
    y2 = int(np.percentile(ys, 98))
    body_h = y2 - y1

    if body_h < ch * 0.42:
        return rect, {
            "space_refined": False,
            "space_reason": f"body_too_partial_{body_h/max(1,ch):.3f}",
            "space_mask": combined,
        }

    # More padding for normal boxes, less for very loose closeups.
    if aspect < 2.35 or area_ratio >= 0.24:
        pad_y = int(body_h * 0.070)
    else:
        pad_y = int(body_h * 0.095)

    y1 = max(0, y1 - pad_y)
    y2 = min(ch - 1, y2 + pad_y)

    refined = rect_from_crop_box((0, y1, cw - 1, y2), Minv)
    change = rect_area(refined) / max(1.0, original_area)

    # Preserve existing good boxes: no aggressive shrink unless clearly loose.
    min_change = 0.62 if (area_ratio >= 0.24 or aspect < 2.45) else 0.72
    if change < min_change:
        return rect, {
            "space_refined": False,
            "space_reason": f"unsafe_space_shrink_{change:.3f}",
            "space_area_change": change,
            "space_mask": combined,
        }

    if change > 1.04:
        return rect, {
            "space_refined": False,
            "space_reason": f"unsafe_space_growth_{change:.3f}",
            "space_area_change": change,
            "space_mask": combined,
        }

    if change > 0.97:
        return rect, {
            "space_refined": False,
            "space_reason": f"space_change_too_small_{change:.3f}",
            "space_area_change": change,
            "space_mask": combined,
        }

    if not validate_rect_soft(refined, image_bgr.shape):
        return rect, {
            "space_refined": False,
            "space_reason": "space_rect_invalid",
            "space_area_change": change,
            "space_mask": combined,
        }

    return refined, {
        "space_refined": True,
        "space_reason": "accepted_vertical_space_tighten",
        "space_area_change": change,
        "space_mask": combined,
    }

def tighten_height_by_horizontal_profile(image_bgr, rect):
    if rect is None:
        return rect, {
            "size_refined": False,
            "size_reason": "no_rect",
        }

    original_rect = normalize_rect(rect)
    original_area = rect_area(original_rect)

    crop, M, Minv = make_warp_from_rect(image_bgr, original_rect)
    ch, cw = crop.shape[:2]

    if cw < 120 or ch < 40:
        return original_rect, {
            "size_refined": False,
            "size_reason": "crop_too_small",
        }

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    eq = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 16, 85)

    k_h = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(31, cw // 18), 3),
    )
    hmask = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_h, iterations=1)
    hmask = cv2.dilate(hmask, np.ones((3, 3), np.uint8), iterations=1)

    row_score = hmask.sum(axis=1) / 255.0

    kernel = max(5, ch // 40)
    if kernel % 2 == 0:
        kernel += 1

    row_score_smooth = cv2.GaussianBlur(
        row_score.astype(np.float32).reshape(-1, 1),
        (1, kernel),
        0,
    ).reshape(-1)

    thr = max(cw * 0.025, np.percentile(row_score_smooth, 48))
    active = row_score_smooth >= thr

    segments = []
    start = None

    for i, hit in enumerate(active):
        if hit and start is None:
            start = i

        if (not hit or i == len(active) - 1) and start is not None:
            end = i if not hit else i + 1
            if end - start >= max(4, int(ch * 0.025)):
                segments.append((start, end))
            start = None

    if not segments:
        return original_rect, {
            "size_refined": False,
            "size_reason": "no_active_height_segment",
            "size_mask": hmask,
        }

    y1 = min(s[0] for s in segments)
    y2 = max(s[1] for s in segments)

    body_h = y2 - y1

    if body_h < ch * 0.35:
        return original_rect, {
            "size_refined": False,
            "size_reason": f"height_too_partial_{body_h / ch:.3f}",
            "size_mask": hmask,
        }

    pad_y = int(body_h * 0.085)

    y1 = max(0, y1 - pad_y)
    y2 = min(ch - 1, y2 + pad_y)

    x1 = 0
    x2 = cw - 1

    refined = rect_from_crop_box((x1, y1, x2, y2), Minv)
    area_change = rect_area(refined) / max(1.0, original_area)

    if area_change < 0.78:
        return original_rect, {
            "size_refined": False,
            "size_reason": f"unsafe_height_shrink_{area_change:.3f}",
            "size_area_change": area_change,
            "size_mask": hmask,
        }

    if area_change > 1.05:
        return original_rect, {
            "size_refined": False,
            "size_reason": f"unsafe_height_growth_{area_change:.3f}",
            "size_area_change": area_change,
            "size_mask": hmask,
        }

    if not validate_rect_soft(refined, image_bgr.shape):
        return original_rect, {
            "size_refined": False,
            "size_reason": "size_refined_rect_invalid",
            "size_area_change": area_change,
            "size_mask": hmask,
        }

    return refined, {
        "size_refined": True,
        "size_reason": "accepted_horizontal_profile_height_tighten",
        "size_area_change": area_change,
        "size_mask": hmask,
    }


def nudge_hiv_box_up_if_needed(rect, image_shape):
    if rect is None:
        return rect, {
            "nudge_applied": False,
            "nudge_reason": "no_rect",
        }

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)

    # HIV small/medium boxes often sit slightly too low.
    # This targets hiv13/hiv47-style cases without touching large dengue/normal boxes.
    if 0.075 <= area_ratio <= 0.145 and 2.40 <= aspect <= 4.25:
        cy2 = cy - rh * 0.115
        rh2 = rh * 1.055

        nudged = normalize_rect(((cx, cy2), (rw, rh2), angle))

        if validate_rect_soft(nudged, image_shape):
            return nudged, {
                "nudge_applied": True,
                "nudge_reason": "small_hiv_upward_height_nudge_v2",
            }

    return rect, {
        "nudge_applied": False,
        "nudge_reason": "not_hiv_nudge_case",
    }


def shrink_oversized_projection_box(rect, debug, image_shape):
    if rect is None:
        return rect, {
            "oversize_fixed": False,
            "oversize_reason": "no_rect",
        }

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    pass_name = debug.get("pass_name", "")
    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)

    if pass_name != "closeup_projection":
        return rect, {
            "oversize_fixed": False,
            "oversize_reason": "not_projection",
        }

    # Very loose projection rescue: usually almost the whole frame.
    if area_ratio >= 0.55:
        new_w = rw * 0.74
        new_h = rh * 0.68
        new_cy = cy - rh * 0.020

        fixed = normalize_rect(((cx, new_cy), (new_w, new_h), angle))

        if validate_rect_soft(fixed, image_shape):
            return fixed, {
                "oversize_fixed": True,
                "oversize_reason": "strong_shrink_huge_projection_box",
            }

    # Large but not full-frame.
    if area_ratio >= 0.38:
        new_w = rw * 0.86
        new_h = rh * 0.78
        new_cy = cy - rh * 0.015

        fixed = normalize_rect(((cx, new_cy), (new_w, new_h), angle))

        if validate_rect_soft(fixed, image_shape):
            return fixed, {
                "oversize_fixed": True,
                "oversize_reason": "medium_shrink_large_projection_box",
            }

    # Slightly loose projection.
    if area_ratio >= 0.24 and 1.65 <= aspect <= 4.20:
        new_w = rw * 0.94
        new_h = rh * 0.88

        fixed = normalize_rect(((cx, cy), (new_w, new_h), angle))

        if validate_rect_soft(fixed, image_shape):
            return fixed, {
                "oversize_fixed": True,
                "oversize_reason": "light_shrink_projection_box",
            }

    return rect, {
        "oversize_fixed": False,
        "oversize_reason": "projection_size_already_ok",
    }


def tighten_width_by_vertical_profile(image_bgr, rect):
    if rect is None:
        return rect, {
            "width_refined": False,
            "width_reason": "no_rect",
        }

    original_rect = normalize_rect(rect)
    original_area = rect_area(original_rect)

    crop, M, Minv = make_warp_from_rect(image_bgr, original_rect)
    ch, cw = crop.shape[:2]

    if cw < 120 or ch < 40:
        return original_rect, {
            "width_refined": False,
            "width_reason": "crop_too_small",
        }

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    eq = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 16, 85)

    k_v = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (3, max(17, ch // 8)),
    )
    vmask = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_v, iterations=1)
    vmask = cv2.dilate(vmask, np.ones((3, 3), np.uint8), iterations=1)

    col_score = vmask.sum(axis=0) / 255.0

    kernel = max(5, cw // 50)
    if kernel % 2 == 0:
        kernel += 1

    col_score_smooth = cv2.GaussianBlur(
        col_score.astype(np.float32).reshape(1, -1),
        (kernel, 1),
        0,
    ).reshape(-1)

    thr = max(ch * 0.020, np.percentile(col_score_smooth, 52))
    active = col_score_smooth >= thr

    x_seg = longest_true_segment(active)

    if x_seg is None:
        return original_rect, {
            "width_refined": False,
            "width_reason": "no_active_width_segment",
            "width_mask": vmask,
        }

    x1, x2 = x_seg
    body_w = x2 - x1

    if body_w < cw * 0.40:
        return original_rect, {
            "width_refined": False,
            "width_reason": f"width_too_partial_{body_w / cw:.3f}",
            "width_mask": vmask,
        }

    pad_x = int(body_w * 0.030)

    x1 = max(0, x1 - pad_x)
    x2 = min(cw - 1, x2 + pad_x)

    y1 = 0
    y2 = ch - 1

    refined = rect_from_crop_box((x1, y1, x2, y2), Minv)
    area_change = rect_area(refined) / max(1.0, original_area)

    if area_change < 0.65:
        return original_rect, {
            "width_refined": False,
            "width_reason": f"unsafe_width_shrink_{area_change:.3f}",
            "width_area_change": area_change,
            "width_mask": vmask,
        }

    if area_change > 1.05:
        return original_rect, {
            "width_refined": False,
            "width_reason": f"unsafe_width_growth_{area_change:.3f}",
            "width_area_change": area_change,
            "width_mask": vmask,
        }

    if not validate_rect_soft(refined, image_bgr.shape):
        return original_rect, {
            "width_refined": False,
            "width_reason": "width_refined_rect_invalid",
            "width_area_change": area_change,
            "width_mask": vmask,
        }

    return refined, {
        "width_refined": True,
        "width_reason": "accepted_vertical_profile_width_tighten",
        "width_area_change": area_change,
        "width_mask": vmask,
    }


def run_full_body_white_object_pass(small):
    h, w = small.shape[:2]
    img_area = float(w * h)

    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    v = hsv[:, :, 2]
    s = hsv[:, :, 1]

    v_thr = max(78, np.percentile(v, 42))
    s_thr = min(135, np.percentile(s, 72) + 28)

    white = np.where((v >= v_thr) & (s <= s_thr), 255, 0).astype(np.uint8)

    eq = cv2.createCLAHE(clipLimit=2.3, tileGridSize=(8, 8)).apply(gray)
    edges = cv2.Canny(eq, 16, 82)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    mask = cv2.bitwise_or(white, edges)

    k_close_big = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(31, w // 22), max(17, h // 35)),
    )
    k_close_tall = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(15, w // 55), max(35, h // 18)),
    )
    k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

    mask1 = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close_big, iterations=2)
    mask2 = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close_tall, iterations=2)
    mask = cv2.bitwise_or(mask1, mask2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []

    for c in contours:
        area = cv2.contourArea(c)

        if area < img_area * 0.006:
            continue

        if area > img_area * 0.86:
            continue

        rect = normalize_rect(cv2.minAreaRect(c))
        (cx, cy), (rw, rh), angle = rect

        rect_ar = rw * rh / img_area
        aspect = rw / max(1.0, rh)
        fill = area / max(1.0, rw * rh)
        axis = angle_to_axis(angle)

        if not (0.006 <= rect_ar <= 0.82):
            continue

        if not (1.15 <= aspect <= 9.2):
            continue

        if fill < 0.18:
            continue

        if rect_ar < 0.035 and aspect < 2.1:
            continue

        center_score = rect_center_distance_score(cx, cy, w, h)
        aspect_score = max(0.0, 1.0 - abs(aspect - 3.2) / 4.2)
        area_score = max(0.0, 1.0 - abs(rect_ar - 0.22) / 0.45)
        fill_score = min(1.0, fill * 1.6)
        angle_score = max(0.0, 1.0 - axis / 46.0)

        score = (
            center_score * 1.2
            + aspect_score * 2.8
            + area_score * 2.3
            + fill_score * 2.2
            + angle_score * 1.3
        )

        if rect_ar >= 0.08:
            score += 1.0

        if 1.8 <= aspect <= 5.2:
            score += 0.8

        candidates.append((score, rect, rect_ar, aspect, fill))

    candidates.sort(key=lambda x: x[0], reverse=True)

    debug = {
        "search_roi": small,
        "search_roi_box": (0, 0, w, h),
        "coarse_mask": mask,
        "eq": eq,
        "candidate_count": len(candidates),
        "coarse_candidate_count": len(candidates),
        "pass_name": "full_body_white",
        "selection_rule": "",
        "reject_reason": "",
    }

    if not candidates:
        debug["reject_reason"] = "no_full_body_candidate"
        return None, debug, 0.0

    score, rect, rect_ar, aspect, fill = candidates[0]

    if not validate_rect_soft(rect, small.shape):
        debug["reject_reason"] = "full_body_candidate_invalid"
        return None, debug, 0.0

    confidence = 0.66
    if rect_ar >= 0.08:
        confidence += 0.08
    if 1.8 <= aspect <= 5.2:
        confidence += 0.07
    if fill >= 0.32:
        confidence += 0.05

    confidence = min(0.84, confidence)

    debug["selection_rule"] = "largest_white_plastic_body"
    debug["reject_reason"] = "accepted"
    debug["confidence"] = confidence

    return rect, debug, confidence


def run_detector_pass(small, roi_mode="normal", mask_mode="white"):
    roi, (sx1, sy1, sx2, sy2) = build_search_roi(small, mode=roi_mode)
    mask, eq = build_combined_mask(roi, mode=mask_mode)
    candidates = get_candidate_rects_from_mask(mask, roi.shape, mode=mask_mode)

    debug = {
        "search_roi": roi,
        "search_roi_box": (sx1, sy1, sx2, sy2),
        "coarse_mask": mask,
        "eq": eq,
        "candidate_count": len(candidates),
        "coarse_candidate_count": len(candidates),
        "pass_name": f"{roi_mode}_{mask_mode}",
        "selection_rule": "",
        "reject_reason": "",
    }

    if not candidates:
        debug["reject_reason"] = "no_candidate"
        return None, debug, 0.0

    best = None

    for c in candidates[:10]:
        (cx, cy), (rw, rh), angle = c["rect"]

        rect = normalize_rect(((cx + sx1, cy + sy1), (rw, rh), angle))
        rect = expand_rect(rect, small.shape)

        if not validate_rect_soft(rect, small.shape):
            continue

        (_, _), (erw, erh), eang = normalize_rect(rect)

        area_ratio = rect_area_ratio(rect, small.shape)
        aspect = erw / max(1.0, erh)
        axis = angle_to_axis(eang)

        confidence = 0.40
        confidence += min(0.23, c["score"] / 38.0)

        if 0.020 <= area_ratio <= 0.24:
            confidence += 0.10
        elif 0.24 < area_ratio <= 0.46:
            confidence += 0.08
        elif 0.015 <= area_ratio < 0.020:
            confidence += 0.02

        if 1.8 <= aspect <= 5.8:
            confidence += 0.10
        elif 1.3 <= aspect < 1.8 or 5.8 < aspect <= 8.0:
            confidence += 0.04

        if axis <= 12:
            confidence += 0.07
        elif axis <= 28:
            confidence += 0.03

        if mask_mode == "shadow":
            confidence -= 0.035

        if mask_mode == "combined":
            confidence += 0.035

        confidence = max(0.0, min(0.88, confidence))

        score = confidence

        if area_ratio < 0.020:
            score -= 0.22

        if aspect < 1.35:
            score -= 0.18

        item = (score, confidence, rect, c)

        if best is None or item[0] > best[0]:
            best = item

    if best is None:
        debug["reject_reason"] = "all_candidates_rejected"
        return None, debug, 0.0

    score, confidence, rect, raw = best

    debug["selection_rule"] = f"best_{mask_mode}_candidate"
    debug["reject_reason"] = "accepted"

    return rect, debug, confidence


def run_closeup_projection_pass(small):
    h, w = small.shape[:2]

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    eq = cv2.createCLAHE(clipLimit=2.8, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 12, 70)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    k_h = cv2.getStructuringElement(cv2.MORPH_RECT, (max(61, w // 18), 5))
    hmask = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_h, iterations=2)
    hmask = cv2.dilate(hmask, np.ones((7, 7), np.uint8), iterations=1)

    row_score = hmask.sum(axis=1) / 255.0
    row_thr = max(w * 0.10, np.percentile(row_score, 86))
    row_hits = row_score >= row_thr

    segments = []
    start = None

    for i, hit in enumerate(row_hits):
        if hit and start is None:
            start = i

        if (not hit or i == len(row_hits) - 1) and start is not None:
            end = i if not hit else i + 1
            if end - start >= 4:
                segments.append((start, end, float(row_score[start:end].mean())))
            start = None

    if len(segments) < 2:
        return None, {
            "pass_name": "closeup_projection",
            "selection_rule": "",
            "reject_reason": "not_enough_horizontal_bands",
            "candidate_count": 0,
            "coarse_candidate_count": 0,
            "coarse_mask": hmask,
            "eq": eq,
        }, 0.0

    best_pair = None
    best_pair_score = -1.0

    for a in segments:
        for b in segments:
            if b[0] <= a[0]:
                continue

            y_gap = b[1] - a[0]
            if not (h * 0.18 <= y_gap <= h * 0.76):
                continue

            score = y_gap + (a[2] + b[2]) * 0.12
            if score > best_pair_score:
                best_pair_score = score
                best_pair = (a, b)

    if best_pair is None:
        return None, {
            "pass_name": "closeup_projection",
            "selection_rule": "",
            "reject_reason": "no_valid_top_bottom_pair",
            "candidate_count": len(segments),
            "coarse_candidate_count": len(segments),
            "coarse_mask": hmask,
            "eq": eq,
        }, 0.0

    top_seg, bot_seg = best_pair
    y1 = max(0, top_seg[0] - int(h * 0.030))
    y2 = min(h - 1, bot_seg[1] + int(h * 0.040))

    band = hmask[max(0, y1 - 10):min(h, y2 + 10), :]
    col_score = band.sum(axis=0) / 255.0

    col_thr = max((y2 - y1) * 0.030, np.percentile(col_score, 70))
    xs = np.where(col_score >= col_thr)[0]

    if len(xs) < w * 0.30:
        return None, {
            "pass_name": "closeup_projection",
            "selection_rule": "",
            "reject_reason": "not_enough_width_support",
            "candidate_count": len(segments),
            "coarse_candidate_count": len(segments),
            "coarse_mask": hmask,
            "eq": eq,
        }, 0.0

    x1 = max(0, int(np.percentile(xs, 1)) - int(w * 0.020))
    x2 = min(w - 1, int(np.percentile(xs, 99)) + int(w * 0.020))

    rw = float(x2 - x1)
    rh = float(y2 - y1)
    cx = float((x1 + x2) / 2.0)
    cy = float((y1 + y2) / 2.0)

    rect = normalize_rect(((cx, cy), (rw, rh), 0.0))

    area_ratio = rect_area_ratio(rect, small.shape)
    aspect = rw / max(1.0, rh)

    if not (0.070 <= area_ratio <= 0.78 and 1.35 <= aspect <= 6.50):
        return None, {
            "pass_name": "closeup_projection",
            "selection_rule": "",
            "reject_reason": f"projection_rect_not_plausible_area_{area_ratio:.3f}_aspect_{aspect:.3f}",
            "candidate_count": len(segments),
            "coarse_candidate_count": len(segments),
            "coarse_mask": hmask,
            "eq": eq,
        }, 0.0

    confidence = 0.70

    if 0.16 <= area_ratio <= 0.45:
        confidence += 0.05

    if 2.0 <= aspect <= 4.2:
        confidence += 0.05

    confidence = min(0.80, confidence)

    return rect, {
        "pass_name": "closeup_projection",
        "selection_rule": "outer_horizontal_projection_last_resort",
        "reject_reason": "accepted",
        "candidate_count": len(segments),
        "coarse_candidate_count": len(segments),
        "coarse_mask": hmask,
        "eq": eq,
        "confidence": confidence,
    }, confidence


def choose_best_detection(detections, image_bgr, image_shape):
    if not detections:
        return None, None, 0.0

    ranked = []
    h, w = image_shape[:2]
    img_area = float(w * h)

    for rect, debug, conf in detections:
        rect = normalize_rect(rect)
        (_, _), (rw, rh), angle = rect

        area_ratio = (rw * rh) / img_area
        aspect = rw / max(1.0, rh)
        axis = angle_to_axis(angle)

        feature_ok, feature_debug = validate_candidate_by_features(image_bgr, rect)
        feature_score = float(feature_debug.get("feature_score", 0.0))

        score = conf

        if 0.020 <= area_ratio <= 0.24:
            score += 0.10
        elif 0.24 < area_ratio <= 0.48:
            score += 0.06
        elif area_ratio < 0.015:
            score -= 0.35
        elif area_ratio > 0.52:
            score -= 0.18

        if 1.8 <= aspect <= 5.8:
            score += 0.10
        elif aspect < 1.25:
            score -= 0.20

        if axis <= 12:
            score += 0.05
        elif axis > 34:
            score -= 0.12

        if "combined" in debug.get("pass_name", ""):
            score += 0.03

        if "projection" in debug.get("pass_name", ""):
            score -= 0.06

        if debug.get("pass_name", "") == "full_body_white":
            score += 0.22
            if area_ratio >= 0.08 and 1.8 <= aspect <= 5.5:
                score += 0.18

        if feature_ok:
            score += min(0.18, feature_score * 0.45)
        else:
            if area_ratio < 0.16:
                score -= 0.42
            else:
                score -= 0.18

        debug = dict(debug)
        debug.update(feature_debug)
        ranked.append((score, rect, debug, conf))

    ranked.sort(key=lambda x: x[0], reverse=True)

    best_score, rect, debug, conf = ranked[0]

    if best_score < 0.42:
        debug["reject_reason"] = f"best_score_too_low_{best_score:.3f}"
        return None, debug, 0.0

    return rect, debug, conf


def force_closeup_geometry(rect, image_shape):
    if rect is None:
        return rect, {"geometry_forced": False, "geometry_reason": "no_rect"}

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    area_ratio = rect_area_ratio(rect, image_shape)
    horiz = angle_to_horizontal(angle)

    if area_ratio < 0.18:
        return rect, {"geometry_forced": False, "geometry_reason": "not_closeup"}

    new_angle = angle

    # Do not globally snap angles anymore. Small true tilts should be preserved.
    if horiz <= 0.35:
        new_angle = 0.0

    forced = normalize_rect(((cx, cy), (rw, rh), new_angle))

    if not validate_rect_soft(forced, image_shape):
        return rect, {
            "geometry_forced": False,
            "geometry_reason": "forced_not_valid",
        }

    return forced, {
        "geometry_forced": True,
        "geometry_reason": "angle_snap_only_no_static_height_resize",
    }

def _smooth_profile_1d(values, kernel):
    values = values.astype(np.float32).reshape(-1, 1)

    kernel = max(5, int(kernel))
    if kernel % 2 == 0:
        kernel += 1

    return cv2.GaussianBlur(values, (1, kernel), 0).reshape(-1)


def _profile_bounds(profile, full_len, min_span_ratio, low_pct=2, high_pct=98):
    if profile is None or len(profile) == 0:
        return None

    profile = np.asarray(profile, dtype=np.float32)

    if float(profile.max()) <= 0:
        return None

    smooth = _smooth_profile_1d(profile, max(5, full_len // 45))

    # Adaptive threshold: accepts faint body edges but ignores very weak noise.
    thr = max(
        float(np.percentile(smooth, 58)),
        float(smooth.max()) * 0.18,
    )

    active = smooth >= thr
    ids = np.where(active)[0]

    if len(ids) < max(6, int(full_len * 0.035)):
        return None

    p1 = int(np.percentile(ids, low_pct))
    p2 = int(np.percentile(ids, high_pct))

    if p2 <= p1:
        return None

    span = p2 - p1

    if span < full_len * min_span_ratio:
        return None

    return p1, p2


def tighten_projection_rescue_box(image_bgr, rect, debug):
    """
    Tightens huge closeup_projection boxes using the actual edge/dark-content
    profile inside the warped rectangle.

    This is mainly for den18, hiv39, hiv40, hiv46:
    the projection fallback finds the kit region but returns a box that is
    almost the whole image.
    """
    if rect is None:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "no_rect",
        }

    pass_name = debug.get("pass_name", "")

    if pass_name != "closeup_projection" and not debug.get("projection_rescue", False):
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "not_projection_case",
        }

    rect = normalize_rect(rect)
    original_area_ratio = rect_area_ratio(rect, image_bgr.shape)

    # Do not touch already reasonable projection boxes too aggressively.
    if original_area_ratio < 0.16:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "projection_not_oversized",
        }

    try:
        crop, _, Minv = make_warp_from_rect(image_bgr, rect)
    except Exception:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "warp_failed",
        }

    ch, cw = crop.shape[:2]

    if cw < 120 or ch < 45:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "crop_too_small",
        }

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    eq = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 12, 82)

    dark = cv2.adaptiveThreshold(
        eq,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        7,
    )

    # Long horizontal edges = top/bottom kit boundaries.
    kh = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(45, cw // 16), 3),
    )
    h_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kh, iterations=1)

    # Vertical/dark features help find the real left/right body limits.
    kv = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (3, max(17, ch // 9)),
    )
    v_edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kv, iterations=1)

    support = cv2.bitwise_or(h_edges, v_edges)
    support = cv2.bitwise_or(support, dark)
    support = cv2.dilate(support, np.ones((3, 3), np.uint8), iterations=1)

    row_score = support.sum(axis=1).astype(np.float32) / 255.0
    y_bounds = _profile_bounds(
        row_score,
        ch,
        min_span_ratio=0.26 if original_area_ratio >= 0.45 else 0.20,
        low_pct=2,
        high_pct=98,
    )

    if y_bounds is None:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "no_reliable_y_profile",
            "projection_profile_mask": support,
        }

    y1, y2 = y_bounds

    y_pad = int((y2 - y1) * (0.060 if original_area_ratio >= 0.45 else 0.075))
    y1 = max(0, y1 - y_pad)
    y2 = min(ch - 1, y2 + y_pad)

    band = support[y1:y2 + 1, :]
    col_score = band.sum(axis=0).astype(np.float32) / 255.0

    x_bounds = _profile_bounds(
        col_score,
        cw,
        min_span_ratio=0.34,
        low_pct=1,
        high_pct=99,
    )

    if x_bounds is None:
        # Keep full width if side profile is unreliable.
        x1, x2 = 0, cw - 1
    else:
        x1, x2 = x_bounds

        x_pad = int((x2 - x1) * 0.045)
        x1 = max(0, x1 - x_pad)
        x2 = min(cw - 1, x2 + x_pad)

    refined = rect_from_crop_box((x1, y1, x2, y2), Minv)

    new_area_ratio = rect_area_ratio(refined, image_bgr.shape)
    area_change = rect_area(refined) / max(1.0, rect_area(rect))

    # Safety gates.
    if new_area_ratio < 0.055:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": f"profile_too_small_area_{new_area_ratio:.3f}",
            "projection_profile_area_change": area_change,
            "projection_profile_mask": support,
        }

    if area_change < 0.20:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": f"profile_unsafe_shrink_{area_change:.3f}",
            "projection_profile_area_change": area_change,
            "projection_profile_mask": support,
        }

    if area_change > 1.08:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": f"profile_unsafe_growth_{area_change:.3f}",
            "projection_profile_area_change": area_change,
            "projection_profile_mask": support,
        }

    # If the box was huge, a very tiny shrink means the profile did not help.
    if original_area_ratio >= 0.50 and area_change > 0.88:
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": f"profile_not_tight_enough_{area_change:.3f}",
            "projection_profile_area_change": area_change,
            "projection_profile_mask": support,
        }

    if not validate_rect_soft(refined, image_bgr.shape):
        return rect, {
            "projection_profile_refined": False,
            "projection_profile_reason": "profile_rect_invalid",
            "projection_profile_area_change": area_change,
            "projection_profile_mask": support,
        }

    return refined, {
        "projection_profile_refined": True,
        "projection_profile_reason": "accepted_projection_profile_tighten",
        "projection_profile_area_change": area_change,
        "projection_profile_mask": support,
    }


def grow_short_height_if_needed(rect, debug, image_shape):
    """
    Small height recovery for den23-style boxes where the body is detected
    but the top/bottom margins are slightly too tight.
    """
    if rect is None:
        return rect, {
            "height_grow_applied": False,
            "height_grow_reason": "no_rect",
        }

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    pass_name = debug.get("pass_name", "")
    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)

    # den23 class: normal_shadow, medium/large kit, aspect around 2.5.
    if (
        pass_name == "normal_shadow"
        and 0.175 <= area_ratio <= 0.215
        and 2.35 <= aspect <= 2.75
    ):
        grown = normalize_rect(((cx, cy), (rw * 1.005, rh * 1.075), angle))

        if validate_rect_soft(grown, image_shape):
            return grown, {
                "height_grow_applied": True,
                "height_grow_reason": "medium_normal_shadow_height_recovery",
            }

    return rect, {
        "height_grow_applied": False,
        "height_grow_reason": "not_short_height_case",
    }


def try_closeup_projection_rescue(image_bgr, small, scale, reason="projection_rescue"):
    """
    Runs projection fallback even when the original selected blob was not from
    a closeup pass. This is important for hiv38-style cases where the rejected
    tiny false positive came from normal_shadow.
    """
    proj_small, proj_debug, proj_conf = run_closeup_projection_pass(small)

    if proj_small is None or not validate_rect_soft(proj_small, small.shape):
        fail_debug = proj_debug or {}
        fail_debug["confidence"] = 0.0
        fail_debug["reject_reason"] = fail_debug.get("reject_reason", "projection_rescue_failed")
        return None, fail_debug

    proj_rect = normalize_rect(scale_rect(proj_small, scale))
    proj_debug = dict(proj_debug)
    proj_debug["projection_rescue"] = True
    proj_debug["projection_rescue_reason"] = reason

    proj_rect, static_info = shrink_oversized_projection_box(
        proj_rect,
        proj_debug,
        image_bgr.shape,
    )
    proj_debug["oversize_fixed"] = static_info.get("oversize_fixed", False)
    proj_debug["oversize_reason"] = static_info.get("oversize_reason", "")

    proj_rect, profile_info = tighten_projection_rescue_box(
        image_bgr,
        proj_rect,
        proj_debug,
    )
    proj_debug["projection_profile_refined"] = profile_info.get("projection_profile_refined", False)
    proj_debug["projection_profile_reason"] = profile_info.get("projection_profile_reason", "")
    proj_debug["projection_profile_area_change"] = profile_info.get("projection_profile_area_change", "")
    if profile_info.get("projection_profile_mask") is not None:
        proj_debug["projection_profile_mask"] = profile_info["projection_profile_mask"]

    proj_rect, width_info = adjust_projection_rescue_width(
        proj_rect,
        proj_debug,
        image_bgr.shape,
    )
    proj_debug["projection_width_adjusted"] = width_info.get("projection_width_adjusted", False)
    proj_debug["projection_width_reason"] = width_info.get("projection_width_reason", "")

    proj_rect, nudge_info = nudge_hiv_box_up_if_needed(
        proj_rect,
        image_bgr.shape,
    )
    proj_debug["nudge_applied"] = nudge_info.get("nudge_applied", False)
    proj_debug["nudge_reason"] = nudge_info.get("nudge_reason", "")

    proj_rect, grow_info = grow_short_height_if_needed(
        proj_rect,
        proj_debug,
        image_bgr.shape,
    )
    proj_debug["height_grow_applied"] = grow_info.get("height_grow_applied", False)
    proj_debug["height_grow_reason"] = grow_info.get("height_grow_reason", "")

    if not validate_rect_soft(proj_rect, image_bgr.shape):
        proj_debug["confidence"] = 0.0
        proj_debug["reject_reason"] = "projection_rescue_final_invalid"
        return None, proj_debug

    feature_ok, feature_debug = validate_candidate_by_features(image_bgr, proj_rect)
    proj_debug.update(feature_debug)

    area_ratio = rect_area_ratio(proj_rect, image_bgr.shape)
    norm = normalize_rect(proj_rect)
    aspect = norm[1][0] / max(1.0, norm[1][1])

    if not feature_ok and area_ratio < 0.10:
        proj_debug["confidence"] = 0.0
        proj_debug["reject_reason"] = "projection_rescue_weak_features_small_area"
        return None, proj_debug

    if area_ratio > 0.62:
        proj_debug["confidence"] = 0.0
        proj_debug["reject_reason"] = "projection_rescue_still_too_large"
        return None, proj_debug

    if not (1.45 <= aspect <= 6.50):
        proj_debug["confidence"] = 0.0
        proj_debug["reject_reason"] = f"projection_rescue_bad_aspect_{aspect:.3f}"
        return None, proj_debug

    proj_debug["confidence"] = min(float(proj_conf), 0.72)
    proj_debug["reject_reason"] = "accepted_projection_rescue"
    return proj_rect, proj_debug


def adjust_rect_local(rect, image_shape, left=0.0, right=0.0, top=0.0, bottom=0.0, angle_override=None):
    """
    Expands/shifts a rotated rect in its own local coordinate system.

    left/right/top/bottom are ratios of current width/height.
    Example:
      right=0.15 keeps the left side almost fixed and expands the right side.
      top=0.20 expands mostly upward.
      bottom=0.06 expands downward.
    """
    if rect is None:
        return rect

    rect = normalize_rect(rect)
    (cx, cy), (rw, rh), angle = rect

    use_angle = angle if angle_override is None else angle_override
    theta = math.radians(use_angle)
    ca = math.cos(theta)
    sa = math.sin(theta)

    new_w = rw * (1.0 + left + right)
    new_h = rh * (1.0 + top + bottom)

    # Local center shift caused by asymmetric expansion.
    local_dx = (right - left) * rw * 0.5
    local_dy = (bottom - top) * rh * 0.5

    new_cx = cx + local_dx * ca - local_dy * sa
    new_cy = cy + local_dx * sa + local_dy * ca

    adjusted = normalize_rect(((new_cx, new_cy), (new_w, new_h), use_angle))

    if validate_rect_soft(adjusted, image_shape):
        return adjusted

    return rect


def final_geometry_micro_tune(rect, debug, image_shape):
    """
    Final conservative correction layer.

    This does NOT replace detection. It only fixes recurring measured geometry
    errors seen in the latest CSV:
      - small shadow angle drift
      - den18/hiv39/hiv40/hiv46 projection width
      - hiv47 thin projection height
      - hiv13 upward shift
      - den23 medium height recovery
    """
    if rect is None:
        return rect, {
            "micro_tuned": False,
            "micro_reason": "no_rect",
        }

    rect = normalize_rect(rect)
    h, w = image_shape[:2]

    (cx, cy), (rw, rh), angle = rect
    area_ratio = rect_area_ratio(rect, image_shape)
    aspect = rw / max(1.0, rh)
    horiz = angle_to_horizontal(angle)
    pass_name = debug.get("pass_name", "")

    reasons = []
    tuned = rect

    # ------------------------------------------------------------
    # 1) Low-angle drift correction
    # ------------------------------------------------------------
    # Many latest UNSURE cases are not detection failures; their only problem is
    # a 1–4.5 degree shadow angle that does not visually follow the kit.
    should_snap_normal = (
        pass_name == "normal_shadow"
        and 0.108 <= area_ratio <= 0.245
        and 2.65 <= aspect <= 3.90
        and horiz <= 4.85
    )

    should_snap_wide = (
        pass_name == "wide_shadow"
        and 0.095 <= area_ratio <= 0.215
        and 2.90 <= aspect <= 3.90
        and horiz <= 1.60
    )

    should_snap_closeup = (
        pass_name == "closeup_shadow"
        and 0.150 <= area_ratio <= 0.360
        and 2.60 <= aspect <= 3.80
        and horiz <= 5.80
    )

    if should_snap_normal or should_snap_wide or should_snap_closeup:
        (cx, cy), (rw, rh), _ = tuned
        snapped = normalize_rect(((cx, cy), (rw, rh), 0.0))
        if validate_rect_soft(snapped, image_shape):
            tuned = snapped
            reasons.append("snap_shadow_angle_to_zero")

    # Refresh values after possible snap.
    tuned = normalize_rect(tuned)
    (cx, cy), (rw, rh), angle = tuned
    area_ratio = rect_area_ratio(tuned, image_shape)
    aspect = rw / max(1.0, rh)
    width_ratio = rw / max(1.0, w)

    # ------------------------------------------------------------
    # 2) den18-style projection: close but needs wider + lower box
    # ------------------------------------------------------------
    # den18 has closeup_projection, area around 0.33, width ratio below the
    # hiv39/hiv40/hiv46 group. It needs both-side width increase and a lower box.
    if (
        pass_name == "closeup_projection"
        and 0.280 <= area_ratio <= 0.370
        and 2.20 <= aspect <= 2.75
        and width_ratio < 0.695
    ):
        tuned2 = adjust_rect_local(
            tuned,
            image_shape,
            left=0.060,
            right=0.120,
            top=0.000,
            bottom=0.065,
            angle_override=0.0,
        )

        if rect_area_ratio(tuned2, image_shape) > area_ratio:
            tuned = tuned2
            reasons.append("den18_projection_wider_lower")

    # ------------------------------------------------------------
    # 3) hiv39/hiv40/hiv46-style projection: right side needs expansion
    # ------------------------------------------------------------
    # These already have good left placement. Expanding only to the right avoids
    # ruining the left edge while catching the missing right body.
    tuned = normalize_rect(tuned)
    (cx, cy), (rw, rh), angle = tuned
    area_ratio = rect_area_ratio(tuned, image_shape)
    aspect = rw / max(1.0, rh)
    width_ratio = rw / max(1.0, w)

    if (
        pass_name == "closeup_projection"
        and 0.320 <= area_ratio <= 0.440
        and 2.25 <= aspect <= 2.85
        and width_ratio >= 0.695
    ):
        tuned2 = adjust_rect_local(
            tuned,
            image_shape,
            left=0.000,
            right=0.150,
            top=0.000,
            bottom=0.010,
            angle_override=0.0,
        )

        if rect_area_ratio(tuned2, image_shape) > area_ratio:
            tuned = tuned2
            reasons.append("projection_expand_right_side")

    # ------------------------------------------------------------
    # 4) hiv47-style projection: width is okay, height is too thin
    # ------------------------------------------------------------
    tuned = normalize_rect(tuned)
    (cx, cy), (rw, rh), angle = tuned
    area_ratio = rect_area_ratio(tuned, image_shape)
    aspect = rw / max(1.0, rh)

    if (
        pass_name == "closeup_projection"
        and 0.055 <= area_ratio <= 0.105
        and aspect >= 4.15
    ):
        tuned2 = adjust_rect_local(
            tuned,
            image_shape,
            left=0.015,
            right=0.015,
            top=0.230,
            bottom=0.230,
        )

        if rect_area_ratio(tuned2, image_shape) > area_ratio:
            tuned = tuned2
            reasons.append("thin_projection_height_recovery")

    # ------------------------------------------------------------
    # 5) hiv13-style box: correct size, but too low
    # ------------------------------------------------------------
    tuned = normalize_rect(tuned)
    (cx, cy), (rw, rh), angle = tuned
    area_ratio = rect_area_ratio(tuned, image_shape)
    aspect = rw / max(1.0, rh)

    if (
        pass_name == "normal_shadow"
        and 0.085 <= area_ratio <= 0.120
        and 2.85 <= aspect <= 3.55
        and cy >= h * 0.62
    ):
        tuned2 = adjust_rect_local(
            tuned,
            image_shape,
            left=0.000,
            right=0.000,
            top=0.220,
            bottom=0.030,
        )

        if validate_rect_soft(tuned2, image_shape):
            tuned = tuned2
            reasons.append("small_hiv_shift_up")

    # ------------------------------------------------------------
    # 6) den23-style medium box: needs a little more height
    # ------------------------------------------------------------
    tuned = normalize_rect(tuned)
    (cx, cy), (rw, rh), angle = tuned
    area_ratio = rect_area_ratio(tuned, image_shape)
    aspect = rw / max(1.0, rh)

    if (
        pass_name == "normal_shadow"
        and 0.175 <= area_ratio <= 0.210
        and 2.35 <= aspect <= 2.75
    ):
        tuned2 = adjust_rect_local(
            tuned,
            image_shape,
            left=0.000,
            right=0.000,
            top=0.060,
            bottom=0.060,
        )

        if rect_area_ratio(tuned2, image_shape) > area_ratio:
            tuned = tuned2
            reasons.append("medium_height_recovery")

    tuned = normalize_rect(tuned)

    if not validate_rect_soft(tuned, image_shape):
        return rect, {
            "micro_tuned": False,
            "micro_reason": "micro_tuned_rect_invalid_keep_original",
        }

    if reasons:
        return tuned, {
            "micro_tuned": True,
            "micro_reason": "+".join(reasons),
        }

    return rect, {
        "micro_tuned": False,
        "micro_reason": "no_micro_tune_needed",
    }


def run_closeup_center_contour_fallback(small):
    """
    Last-resort close-up body detector.

    Used only when the selected candidate is rejected as a tiny false positive.
    This targets hiv37/hiv38-style cases where the kit is visible but the chosen
    candidate was a tiny internal blob.
    """
    h, w = small.shape[:2]

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    eq = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8)).apply(gray)

    edges = cv2.Canny(eq, 10, 75)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    dark = cv2.adaptiveThreshold(
        eq,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        7,
    )

    support = cv2.bitwise_or(edges, dark)

    kh = cv2.getStructuringElement(cv2.MORPH_RECT, (max(45, w // 18), 5))
    kv = cv2.getStructuringElement(cv2.MORPH_RECT, (5, max(35, h // 18)))

    support_h = cv2.morphologyEx(support, cv2.MORPH_CLOSE, kh, iterations=1)
    support_v = cv2.morphologyEx(support, cv2.MORPH_CLOSE, kv, iterations=1)

    support = cv2.bitwise_or(support_h, support_v)
    support = cv2.morphologyEx(
        support,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
        iterations=1,
    )

    contours, _ = cv2.findContours(
        support,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    img_area = float(w * h)
    candidates = []

    for c in contours:
        area = cv2.contourArea(c)

        if area < img_area * 0.003:
            continue

        if area > img_area * 0.62:
            continue

        rect = normalize_rect(cv2.minAreaRect(c))

        if not validate_rect_soft(rect, small.shape):
            continue

        (cx, cy), (rw, rh), angle = rect

        area_ratio = rect_area_ratio(rect, small.shape)
        aspect = rw / max(1.0, rh)
        axis = angle_to_axis(angle)

        if not (0.040 <= area_ratio <= 0.520):
            continue

        if not (1.45 <= aspect <= 7.20):
            continue

        if axis > 35.0:
            continue

        center_score = rect_center_distance_score(cx, cy, w, h)
        area_score = max(0.0, 1.0 - abs(area_ratio - 0.18) / 0.34)
        aspect_score = max(0.0, 1.0 - abs(aspect - 3.2) / 4.2)
        axis_score = max(0.0, 1.0 - axis / 35.0)

        # Prefer centered, kit-sized, horizontal-ish close-up bodies.
        score = (
            center_score * 2.2
            + area_score * 2.0
            + aspect_score * 1.8
            + axis_score * 1.2
        )

        candidates.append((score, rect))

    if not candidates:
        return None, {
            "pass_name": "closeup_center_fallback",
            "selection_rule": "center_contour_last_resort",
            "candidate_count": 0,
            "coarse_candidate_count": 0,
            "reject_reason": "no_center_fallback_candidate",
            "coarse_mask": support,
            "eq": eq,
        }, 0.0

    candidates.sort(key=lambda x: x[0], reverse=True)
    score, rect = candidates[0]

    if score < 2.50:
        return None, {
            "pass_name": "closeup_center_fallback",
            "selection_rule": "center_contour_last_resort",
            "candidate_count": len(candidates),
            "coarse_candidate_count": len(candidates),
            "reject_reason": f"center_fallback_score_too_low_{score:.3f}",
            "coarse_mask": support,
            "eq": eq,
        }, 0.0

    return rect, {
        "pass_name": "closeup_center_fallback",
        "selection_rule": "center_contour_last_resort",
        "candidate_count": len(candidates),
        "coarse_candidate_count": len(candidates),
        "reject_reason": "accepted_center_fallback",
        "coarse_mask": support,
        "eq": eq,
    }, min(0.68, 0.52 + score * 0.035)


def try_tiny_false_positive_rescue(image_bgr, small, scale, tiny_reason):
    """
    Rescue path for hiv37/hiv38-style cases.

    Order:
      1. projection pass
      2. full-body white object pass
      3. center contour fallback
    """
    rescue_attempts = []

    proj_rect, proj_debug, proj_conf = run_closeup_projection_pass(small)
    rescue_attempts.append((proj_rect, proj_debug, proj_conf, "projection"))

    body_rect, body_debug, body_conf = run_full_body_white_object_pass(small)
    rescue_attempts.append((body_rect, body_debug, body_conf, "full_body"))

    center_rect, center_debug, center_conf = run_closeup_center_contour_fallback(small)
    rescue_attempts.append((center_rect, center_debug, center_conf, "center_contour"))

    for small_rect, rescue_debug, conf, source in rescue_attempts:
        if small_rect is None:
            continue

        if not validate_rect_soft(small_rect, small.shape):
            continue

        rect = normalize_rect(scale_rect(small_rect, scale))

        rescue_debug = dict(rescue_debug or {})
        rescue_debug["projection_rescue"] = source == "projection"
        rescue_debug["tiny_rescue_source"] = source
        rescue_debug["tiny_rescue_reason"] = tiny_reason

        rect, micro_info = final_geometry_micro_tune(
            rect,
            rescue_debug,
            image_bgr.shape,
        )

        rescue_debug["micro_tuned"] = micro_info.get("micro_tuned", False)
        rescue_debug["micro_reason"] = micro_info.get("micro_reason", "")

        if not validate_rect_soft(rect, image_bgr.shape):
            continue

        feature_ok, feature_debug = validate_candidate_by_features(image_bgr, rect)
        rescue_debug.update(feature_debug)

        area_ratio = rect_area_ratio(rect, image_bgr.shape)
        norm = normalize_rect(rect)
        aspect = norm[1][0] / max(1.0, norm[1][1])

        # For large close-up body, internal features can be weak because the kit
        # fills the frame and the body is mostly plain white.
        large_closeup_ok = area_ratio >= 0.075 and 1.70 <= aspect <= 6.50

        if not feature_ok and not large_closeup_ok:
            continue

        rescue_debug["confidence"] = min(float(conf), 0.70)
        rescue_debug["reject_reason"] = f"accepted_tiny_false_positive_rescue_{source}"

        return rect, rescue_debug

    return None, {
        "confidence": 0.0,
        "pass_name": "tiny_rescue_failed",
        "reject_reason": f"tiny_false_positive_rescue_failed_{tiny_reason}",
    }


def detect_kit(image_bgr):
    if image_bgr is None:
        return None, {
            "confidence": 0.0,
            "pass_name": "none",
            "reject_reason": "no_image",
        }

    small, scale = resize_keep_ratio(image_bgr, max_side=1400)

    pass_specs = [
        ("normal", "white"),
        ("wide", "white"),
        ("normal", "combined"),
        ("wide", "combined"),
        ("normal", "shadow"),
        ("wide", "shadow"),
        ("closeup", "combined"),
        ("closeup", "shadow"),
        ("closeup", "white"),
    ]

    detections = []
    last_debug = None

    for roi_mode, mask_mode in pass_specs:
        rect, debug, conf = run_detector_pass(
            small,
            roi_mode=roi_mode,
            mask_mode=mask_mode,
        )

        last_debug = debug

        if rect is None:
            continue

        if validate_rect_soft(rect, small.shape):
            debug["confidence"] = conf
            detections.append((rect, debug, conf))

    body_rect, body_debug, body_conf = run_full_body_white_object_pass(small)
    last_debug = body_debug or last_debug

    if body_rect is not None and validate_rect_soft(body_rect, small.shape):
        body_debug["confidence"] = body_conf
        detections.append((body_rect, body_debug, body_conf))

    if not detections:
        projection_rect, projection_debug, projection_conf = run_closeup_projection_pass(small)
        last_debug = projection_debug or last_debug

        if projection_rect is not None and validate_rect_soft(projection_rect, small.shape):
            projection_debug["confidence"] = min(projection_conf, 0.70)
            detections.append((projection_rect, projection_debug, min(projection_conf, 0.70)))

    chosen_small, debug, conf = choose_best_detection(detections, small, small.shape)

    if chosen_small is None:
        body_rect, body_debug, body_conf = run_full_body_white_object_pass(small)

        if body_rect is not None and validate_rect_soft(body_rect, small.shape):
            chosen_small = body_rect
            debug = body_debug
            conf = body_conf
        else:
            fail_debug = debug or last_debug or {}
            fail_debug["confidence"] = 0.0
            fail_debug["reject_reason"] = fail_debug.get("reject_reason", "no_detection")
            return None, fail_debug

    debug["confidence"] = conf
    debug["reject_reason"] = "accepted"

    original_rect = normalize_rect(scale_rect(chosen_small, scale))

    original_rect, early_oversize_info = shrink_oversized_projection_box(
        original_rect,
        debug,
        image_bgr.shape,
    )

    debug["early_oversize_fixed"] = early_oversize_info.get("oversize_fixed", False)
    debug["early_oversize_reason"] = early_oversize_info.get("oversize_reason", "")

    original_rect, early_projection_profile_info = tighten_projection_rescue_box(
        image_bgr,
        original_rect,
        debug,
    )

    debug["early_projection_profile_refined"] = early_projection_profile_info.get(
        "projection_profile_refined",
        False,
    )
    debug["early_projection_profile_reason"] = early_projection_profile_info.get(
        "projection_profile_reason",
        "",
    )
    debug["early_projection_profile_area_change"] = early_projection_profile_info.get(
        "projection_profile_area_change",
        "",
    )
    if early_projection_profile_info.get("projection_profile_mask") is not None:
        debug["projection_profile_mask"] = early_projection_profile_info["projection_profile_mask"]

    candidate_angle_rect, angle_info = estimate_angle_from_internal_edges(
        image_bgr,
        original_rect,
    )

    angle_update_ok = (
        angle_info.get("angle_refined", False)
        and should_apply_angle_update(
            original_rect,
            candidate_angle_rect,
            image_bgr.shape,
        )
    )

    if angle_update_ok:
        angle_rect = candidate_angle_rect
    else:
        angle_rect = original_rect

    debug["angle_refined"] = bool(angle_update_ok)
    debug["angle_reason"] = (
        angle_info.get("angle_reason", "")
        if angle_update_ok
        else "angle_update_rejected_preserve_original"
    )
    debug["angle_delta"] = angle_info.get("angle_delta", "")
    debug["angle_update_ok"] = bool(angle_update_ok)

    angle_rect, snap_info = snap_tiny_angle_noise_if_safe(
        angle_rect,
        debug,
        image_bgr.shape,
    )

    debug["angle_snap_applied"] = snap_info.get("angle_snap_applied", False)
    debug["angle_snap_reason"] = snap_info.get("angle_snap_reason", "")

    if angle_info.get("angle_edges") is not None:
        debug["angle_edges"] = angle_info["angle_edges"]

    refined_rect, refine_info = refine_rect_by_internal_mask(
        image_bgr,
        angle_rect,
    )

    debug["refined"] = refine_info.get("refined", False)
    debug["refine_reason"] = refine_info.get("refine_reason", "")
    debug["refine_area_change"] = refine_info.get("refine_area_change", "")

    if refine_info.get("refine_crop") is not None:
        debug["refine_crop"] = refine_info["refine_crop"]

    if refine_info.get("refine_mask") is not None:
        debug["refine_mask"] = refine_info["refine_mask"]

    size_rect, size_info = tighten_height_by_horizontal_profile(image_bgr, refined_rect)

    debug["size_refined"] = size_info.get("size_refined", False)
    debug["size_reason"] = size_info.get("size_reason", "")
    debug["size_area_change"] = size_info.get("size_area_change", "")

    if size_info.get("size_mask") is not None:
        debug["size_mask"] = size_info["size_mask"]

    width_rect, width_info = tighten_width_by_vertical_profile(image_bgr, size_rect)

    debug["width_refined"] = width_info.get("width_refined", False)
    debug["width_reason"] = width_info.get("width_reason", "")
    debug["width_area_change"] = width_info.get("width_area_change", "")

    if width_info.get("width_mask") is not None:
        debug["width_mask"] = width_info["width_mask"]

    oversize_rect, oversize_info = shrink_oversized_projection_box(
        width_rect,
        debug,
        image_bgr.shape,
    )

    debug["oversize_fixed"] = oversize_info.get("oversize_fixed", False)
    debug["oversize_reason"] = oversize_info.get("oversize_reason", "")

    projection_profile_rect, projection_profile_info = tighten_projection_rescue_box(
        image_bgr,
        oversize_rect,
        debug,
    )

    debug["projection_profile_refined"] = projection_profile_info.get(
        "projection_profile_refined",
        False,
    )
    debug["projection_profile_reason"] = projection_profile_info.get(
        "projection_profile_reason",
        "",
    )
    debug["projection_profile_area_change"] = projection_profile_info.get(
        "projection_profile_area_change",
        "",
    )
    if projection_profile_info.get("projection_profile_mask") is not None:
        debug["projection_profile_mask"] = projection_profile_info["projection_profile_mask"]

    nudged_rect, nudge_info = nudge_hiv_box_up_if_needed(
        projection_profile_rect,
        image_bgr.shape,
    )

    height_recovered_rect, height_grow_info = grow_short_height_if_needed(
        nudged_rect,
        debug,
        image_bgr.shape,
    )

    debug["height_grow_applied"] = height_grow_info.get("height_grow_applied", False)
    debug["height_grow_reason"] = height_grow_info.get("height_grow_reason", "")

    debug["nudge_applied"] = nudge_info.get("nudge_applied", False)
    debug["nudge_reason"] = nudge_info.get("nudge_reason", "")

    final_rect, space_info = tighten_vertical_spaces_by_edge_profile(
        image_bgr,
        nudged_rect,
        debug,
    )

    debug["space_refined"] = space_info.get("space_refined", False)
    debug["space_reason"] = space_info.get("space_reason", "")
    debug["space_area_change"] = space_info.get("space_area_change", "")
    if space_info.get("space_mask") is not None:
        debug["space_mask"] = space_info["space_mask"]

    final_rect, micro_info = final_geometry_micro_tune(
        final_rect,
        debug,
        image_bgr.shape,
    )

    debug["micro_tuned"] = micro_info.get("micro_tuned", False)
    debug["micro_reason"] = micro_info.get("micro_reason", "")

    final_small = rect_original_to_small(final_rect, scale)

    if final_rect is not None and validate_rect_soft(final_small, small.shape):
        final_rect, g_info = force_closeup_geometry(final_rect, image_bgr.shape)

        debug["geometry_forced"] = g_info.get("geometry_forced", False)
        debug["geometry_reason"] = g_info.get("geometry_reason", "")

        feature_ok, feature_debug = validate_candidate_by_features(image_bgr, final_rect)
        debug.update(feature_debug)

        area_ratio = rect_area_ratio(final_rect, image_bgr.shape)

        norm = normalize_rect(final_rect)
        aspect = norm[1][0] / max(1.0, norm[1][1])
        axis = angle_to_axis(norm[2])

        
        is_large_closeup_body = (
            area_ratio >= 0.12
            and 1.75 <= aspect <= 6.20
            and debug.get("pass_name", "") in {
                "closeup_shadow",
                "closeup_white",
                "closeup_combined",
                "closeup_projection",
                "full_body_white",
            }
        )

        is_tiny_false_positive, tiny_reason = reject_small_random_object(
            final_rect,
            debug,
            image_bgr.shape,
        )

        is_closeup_candidate = debug.get("pass_name", "").startswith("closeup_")

        if is_closeup_candidate and area_ratio >= 0.10 and aspect >= 1.7:
            return final_rect, debug

        if is_tiny_false_positive:
            rescued_rect, rescued_debug = try_tiny_false_positive_rescue(
                image_bgr,
                small,
                scale,
                tiny_reason,
            )

            if rescued_rect is not None:
                return rescued_rect, rescued_debug

            debug["confidence"] = 0.0
            debug["reject_reason"] = f"final_rejected_tiny_false_positive_{tiny_reason}"
            return None, debug

        # Allow weak features ONLY if large close-up
        if not is_large_closeup_body and not feature_ok:
            debug["confidence"] = 0.0
            debug["reject_reason"] = "final_rejected_weak_internal_features"
            return None, debug

        return final_rect, debug

    debug["refined"] = False
    debug["refine_reason"] = "final_rect_invalid_kept_angle_rect"

    fallback_rect, g_info = force_closeup_geometry(angle_rect, image_bgr.shape)

    debug["geometry_forced"] = g_info.get("geometry_forced", False)
    debug["geometry_reason"] = g_info.get("geometry_reason", "")

    return fallback_rect, debug


def save_detection_outputs(image_path, output_dir, image_bgr, rect, debug):
    ensure_dir(output_dir)

    stem = os.path.splitext(os.path.basename(image_path))[0]

    annotated = image_bgr.copy()

    if rect is not None:
        draw_rotated_box(
            annotated,
            rect,
            label=f"KIT {debug.get('confidence', 0):.2f}",
            color=(0, 255, 0),
            thickness=4,
        )
    else:
        cv2.putText(
            annotated,
            "KIT NOT DETECTED",
            (40, 80),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 0, 255),
            3,
            cv2.LINE_AA,
        )

    safe_save_image(os.path.join(output_dir, f"{stem}_detected.jpg"), annotated)

    dbg_dir = os.path.join(output_dir, f"{stem}_debug")
    ensure_dir(dbg_dir)

    debug_images = [
        ("search_roi", "01_search_roi.jpg"),
        ("eq", "02_eq.jpg"),
        ("coarse_mask", "03_coarse_mask.jpg"),
        ("refine_crop", "04_refine_crop.jpg"),
        ("refine_mask", "05_refine_mask.jpg"),
        ("feature_edges", "06_feature_edges.jpg"),
        ("feature_dark", "07_feature_dark.jpg"),
        ("angle_edges", "08_angle_edges.jpg"),
        ("size_mask", "09_size_mask.jpg"),
        ("width_mask", "10_width_mask.jpg"),
        ("space_mask", "11_space_mask.jpg"),
    ]

    for key, fname in debug_images:
        if debug.get(key) is not None:
            safe_save_image(os.path.join(dbg_dir, fname), debug[key])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="Image file or folder")
    parser.add_argument("--out", default="debug_outputs_detector_real_refined")
    args = parser.parse_args()

    files = list_images(args.path)
    ensure_dir(args.out)

    for path in files:
        img = cv2.imread(path)

        if img is None:
            print(f"[SKIP] {path}")
            continue

        rect, debug = detect_kit(img)

        print(
            f"{os.path.basename(path)} | "
            f"detected={rect is not None} | "
            f"conf={debug.get('confidence', 0):.4f} | "
            f"pass={debug.get('pass_name', '')} | "
            f"rule={debug.get('selection_rule', '')} | "
            f"reason={debug.get('reject_reason', '')} | "
            f"angle={debug.get('angle_reason', '')} | "
            f"refine={debug.get('refine_reason', '')} | "
            f"size={debug.get('size_reason', '')} | "
            f"width={debug.get('width_reason', '')} | "
            f"space={debug.get('space_reason', '')} | "
            f"oversize={debug.get('oversize_reason', '')} | "
            f"features={debug.get('feature_reason', '')}:{debug.get('feature_score', 0):.3f} | "
            f"geometry={debug.get('geometry_reason', '')}"
        )

        save_detection_outputs(path, args.out, img, rect, debug)


if __name__ == "__main__":
    main()