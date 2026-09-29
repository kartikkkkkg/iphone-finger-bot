"""Orange-target detector for the six fixed button ROIs.

Pipeline (per frame):
    game-region frame (BGR)
        -> slice 6 fixed ROIs (NumPy views, zero copy)
        -> stack into one (6, H, W, 3) array
        -> ONE cv2.cvtColor to HSV
        -> ONE cv2.inRange orange mask
        -> countNonZero per ROI -> 6 normalized scores
        -> argmax + threshold + ambiguity margin -> target or None

Deliberately NO:
    - whole-screen processing (only the 6 ROIs are ever inspected, so orange
      UI anywhere else on screen can never trigger a click)
    - contour detection, OCR, ML models, cloud APIs
    - per-pixel Python loops

All functions here are pure NumPy/OpenCV: platform-independent and unit
tested on any OS.
"""

import cv2
import numpy as np

NUM_BUTTONS = 6


def roi_bounds(buttons, roi_size, index):
    """Exact pixel bounds of ROI `index` (1-based), exclusive upper edges.

    Returns (x0, y0, x1, y1) with the same centering convention used by
    extract_rois: the ROI is roi_size wide, centered on the calibrated
    button center (integer rounding identical in both places). Bounds may
    extend past the frame edges; extract_rois clamps them when slicing.
    """
    half = roi_size // 2
    cx = int(round(buttons[str(index)]["x"]))
    cy = int(round(buttons[str(index)]["y"]))
    x0, y0 = cx - half, cy - half
    return x0, y0, x0 + roi_size, y0 + roi_size


def find_overlapping_rois(buttons, roi_size):
    """Return [(i, j), ...] of 1-based ROI pairs whose rectangles overlap.

    Empty list means the six ROIs are pairwise disjoint (touching edges
    do not count as overlap).
    """
    rects = [roi_bounds(buttons, roi_size, i) for i in range(1, NUM_BUTTONS + 1)]
    pairs = []
    for a in range(NUM_BUTTONS):
        ax0, ay0, ax1, ay1 = rects[a]
        for b in range(a + 1, NUM_BUTTONS):
            bx0, by0, bx1, by1 = rects[b]
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                pairs.append((a + 1, b + 1))
    return pairs


def extract_rois(frame, buttons, roi_size):
    """Slice the 6 button ROIs out of a game-region frame.

    frame:   HxWx3 BGR uint8 (the captured game region).
    buttons: {"1": {"x":..,"y":..}, ...} button centers in CAPTURE PIXELS
             relative to the frame's top-left.
    roi_size: square side length in capture pixels.

    Returns (6, roi_size, roi_size, 3) uint8. ROIs are clamped to the frame;
    short edges are zero-padded (black never matches the orange mask).
    """
    h, w = frame.shape[:2]
    rois = np.zeros((NUM_BUTTONS, roi_size, roi_size, 3), dtype=np.uint8)
    for i in range(1, NUM_BUTTONS + 1):
        x0, y0, x1, y1 = roi_bounds(buttons, roi_size, i)
        # Source window clamped to the frame.
        sx0, sy0 = max(x0, 0), max(y0, 0)
        sx1, sy1 = min(x1, w), min(y1, h)
        # Destination offset inside the padded ROI.
        dx0, dy0 = sx0 - x0, sy0 - y0
        if sx1 > sx0 and sy1 > sy0:
            rois[i - 1, dy0:dy0 + (sy1 - sy0), dx0:dx0 + (sx1 - sx0)] = \
                frame[sy0:sy1, sx0:sx1]
    return rois


def score_rois(rois, orange_hsv):
    """Vectorized orange-pixel fraction per ROI.

    rois:       (6, H, W, 3) BGR uint8.
    orange_hsv: {"h_min","h_max","s_min","v_min"} (OpenCV H: 0-179).

    Returns np.float64 array of 6 scores in [0, 1].
    """
    n, rh, rw = rois.shape[0], rois.shape[1], rois.shape[2]
    # cvtColor needs a 3-D image: flatten the batch, convert, restore.
    flat = rois.reshape(n * rh, rw, 3)
    hsv = cv2.cvtColor(flat, cv2.COLOR_BGR2HSV).reshape(n, rh, rw, 3)
    lower = np.array([orange_hsv["h_min"], orange_hsv["s_min"],
                      orange_hsv["v_min"]], dtype=np.uint8)
    upper = np.array([orange_hsv["h_max"], 255, 255], dtype=np.uint8)
    # inRange also needs a 3-D image: flatten, mask, restore.
    mask = cv2.inRange(hsv.reshape(n * rh, rw, 3), lower, upper)
    mask = mask.reshape(n, rh, rw)                       # (6, H, W), 0/255
    counts = np.count_nonzero(mask, axis=(1, 2)).astype(np.float64)
    return counts / (rois.shape[1] * rois.shape[2])


def detect(frame, cfg):
    """Detect the orange target in a game-region frame.

    Returns (target, scores, info):
        target: int 1..6, or None when no unambiguous target is present.
        scores: np array of the 6 orange fractions.
        info:   dict with 'best', 'second_best', 'reason'.
    """
    rois = extract_rois(frame, cfg["buttons"], cfg["roi_size"])
    scores = score_rois(rois, cfg["orange_hsv"])

    order = np.argsort(scores)[::-1]
    best_i, best = int(order[0]), float(scores[order[0]])
    second = float(scores[order[1]])
    min_score = cfg["min_score"]
    margin = cfg["ambiguity_margin"]

    info = {"best": best, "second_best": second, "reason": ""}
    if best < min_score:
        info["reason"] = f"best score {best:.3f} < min_score {min_score}"
        return None, scores, info
    if best - second < margin:
        info["reason"] = (f"ambiguous: best {best:.3f} vs second {second:.3f} "
                          f"(margin {margin})")
        return None, scores, info
    return best_i + 1, scores, info


def detect_from_capture(capture_frame_fn, cfg):
    """Convenience: capture one frame and detect. Used by --test / calibrate."""
    frame = capture_frame_fn()
    if frame is None:
        return None, None, {"reason": "capture returned None"}
    return detect(frame, cfg)
