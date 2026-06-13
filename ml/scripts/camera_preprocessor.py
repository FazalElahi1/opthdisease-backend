"""
camera_preprocessor.py — Transform mobile camera eye images into fundus-like images.

    IMPORTANT LIMITATION:
    This preprocessing bridges the *visual* domain gap (camera → fundus appearance)
    but CANNOT bridge the *anatomical* gap. Fundus images show the RETINA (back of
    the eye). Camera images show the ANTERIOR SEGMENT (front of the eye — conjunctiva,
    iris, sclera). No image transform can reveal retinal features that are not present.

    Predictions produced after this preprocessing will have reduced accuracy for
    retinal diseases and are NOT suitable for clinical diagnosis.

    Recommended solution: fine-tune only the classification head (nn.Linear, last
    layer) of the existing .pt model on anterior-segment images. The Swin-T backbone
    is already strong — only the head needs adapting.

Usage (standalone):
    python scripts/camera_preprocessor.py --img_path ./eye_photo.jpg --out ./processed.jpg
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


# ─── Tuneable defaults ────────────────────────────────────────────────────────
CLAHE_CLIP      = 3.0
TARGET_MEAN     = 128.0
OUTPUT_SIZE     = 224
GREEN_WEIGHT    = 1.3   # boost green channel (retinal vessels visible in green)
RED_WEIGHT      = 0.7   # suppress red channel (camera images are red-dominant)
BLUE_WEIGHT     = 0.9


# ════════════════════════════════════════════════════════════════════════════════
#  STEP 1 — Detect & crop the eye region
# ════════════════════════════════════════════════════════════════════════════════

def _detect_eye_region(img_bgr: np.ndarray) -> np.ndarray:
    """
    Try to isolate the eye. Uses two strategies:
      1. Haar cascade eye detector (fast, built into OpenCV)
      2. Fallback: assume image is already a close-up and take centre crop
    Returns a cropped BGR image of just the eye area.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    cascade_paths = [
        cv2.data.haarcascades + "haarcascade_eye.xml",
        cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml",
    ]

    for cascade_path in cascade_paths:
        if not Path(cascade_path).exists():
            continue
        detector = cv2.CascadeClassifier(cascade_path)
        eyes = detector.detectMultiScale(
            gray,
            scaleFactor=1.05,
            minNeighbors=3,
            minSize=(40, 40),
        )
        if len(eyes) > 0:
            # Take the largest detected eye
            x, y, w, h = max(eyes, key=lambda r: r[2] * r[3])
            # Add 20% padding
            pad = int(max(w, h) * 0.20)
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(img_bgr.shape[1], x + w + pad)
            y2 = min(img_bgr.shape[0], y + h + pad)
            return img_bgr[y1:y2, x1:x2]

    # Fallback: assume the image is a tight eye close-up.
    # Take the central 80% to remove edges/eyelids
    h, w = img_bgr.shape[:2]
    margin_y = int(h * 0.10)
    margin_x = int(w * 0.10)
    return img_bgr[margin_y:h - margin_y, margin_x:w - margin_x]


# ════════════════════════════════════════════════════════════════════════════════
#  STEP 2 — Apply circular fundus-style mask
# ════════════════════════════════════════════════════════════════════════════════

def _apply_circular_mask(img_bgr: np.ndarray) -> np.ndarray:
    """
    Fundus images have a circular boundary on black background.
    Simulate that by masking out corners — this also removes eyelid/skin.
    """
    h, w = img_bgr.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    cx, cy = w // 2, h // 2
    radius = int(min(cx, cy) * 0.92)
    cv2.circle(mask, (cx, cy), radius, 255, -1)
    result = img_bgr.copy()
    result[mask == 0] = 0
    return result


# ════════════════════════════════════════════════════════════════════════════════
#  STEP 3 — Colour channel rebalancing
# ════════════════════════════════════════════════════════════════════════════════

def _rebalance_channels(img_bgr: np.ndarray) -> np.ndarray:
    """
    Fundus images are greenish; mobile camera eye photos are red-dominant.
    Rebalance channels to shift colour distribution toward fundus statistics.
    """
    b, g, r = cv2.split(img_bgr.astype(np.float32))
    b = np.clip(b * BLUE_WEIGHT,  0, 255)
    g = np.clip(g * GREEN_WEIGHT, 0, 255)
    r = np.clip(r * RED_WEIGHT,   0, 255)
    return cv2.merge([b, g, r]).astype(np.uint8)


# ════════════════════════════════════════════════════════════════════════════════
#  STEP 4 — CLAHE contrast enhancement
# ════════════════════════════════════════════════════════════════════════════════

def _apply_clahe(img_bgr: np.ndarray, clip: float = CLAHE_CLIP) -> np.ndarray:
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
    l_eq = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l_eq, a, b]), cv2.COLOR_LAB2BGR)


# ════════════════════════════════════════════════════════════════════════════════
#  STEP 5 — Brightness normalisation
# ════════════════════════════════════════════════════════════════════════════════

def _normalise_brightness(img_bgr: np.ndarray,
                          target: float = TARGET_MEAN) -> np.ndarray:
    green = img_bgr[:, :, 1].astype(np.float32)
    mean = green[green > 5].mean() if (green > 5).any() else green.mean()
    if mean < 1e-3:
        return img_bgr
    scale = target / mean
    return np.clip(img_bgr.astype(np.float32) * scale, 0, 255).astype(np.uint8)


# ════════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ════════════════════════════════════════════════════════════════════════════════

def camera_to_fundus_like(image_bytes: bytes,
                          output_size: int = OUTPUT_SIZE) -> bytes:
    """
    Convert raw camera eye image bytes → fundus-like image bytes (JPEG).

    Pipeline:
      1. Detect & crop eye region
      2. Apply circular mask
      3. Rebalance colour channels (reduce red dominance, boost green)
      4. CLAHE contrast enhancement
      5. Brightness normalisation
      6. Resize to output_size × output_size

    Returns JPEG bytes ready to pass into predict.preprocess_image().
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        raise ValueError("Could not decode image bytes")

    img_bgr = _detect_eye_region(img_bgr)
    img_bgr = cv2.resize(img_bgr, (output_size, output_size))
    img_bgr = _apply_circular_mask(img_bgr)
    img_bgr = _rebalance_channels(img_bgr)
    img_bgr = _apply_clahe(img_bgr)
    img_bgr = _normalise_brightness(img_bgr)

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(img_rgb)
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def is_likely_fundus(image_bytes: bytes) -> bool:
    """
    Heuristic check: fundus images have a dark circular boundary (>30% black pixels)
    and a greenish dominant channel. Camera images are bright and red-dominant.
    Returns True if the image looks like a fundus photograph.
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        return False

    h, w = img_bgr.shape[:2]
    total_pixels = h * w

    # Fundus photographs have a dark circular boundary (black corners).
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    black_ratio = (gray < 20).sum() / total_pixels

    # Fundus photographs are warm — the retina/choroid is red/orange dominant,
    # with blue the weakest channel. (Earlier code wrongly required green dominance,
    # which is false for real fundus images and mislabelled them as camera photos.)
    mean_b = img_bgr[:, :, 0].mean()
    mean_g = img_bgr[:, :, 1].mean()
    mean_r = img_bgr[:, :, 2].mean()
    warm_dominant = (mean_r >= mean_g) and (mean_r >= mean_b)

    return warm_dominant and black_ratio > 0.10


# ════════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Convert camera eye image to fundus-like image"
    )
    parser.add_argument("--img_path", required=True, help="Input camera image path")
    parser.add_argument("--out",      default=None,  help="Output path (default: <stem>_funduslike.jpg)")
    parser.add_argument("--size",     type=int, default=OUTPUT_SIZE)
    args = parser.parse_args()

    img_path = Path(args.img_path)
    image_bytes = img_path.read_bytes()

    detected = is_likely_fundus(image_bytes)
    print(f"Image type detected: {'fundus' if detected else 'camera/anterior-segment'}")
    if detected:
        print("Image already looks like a fundus image — preprocessing skipped.")
        out_bytes = image_bytes
    else:
        print("Converting camera image to fundus-like …")
        out_bytes = camera_to_fundus_like(image_bytes, output_size=args.size)

    out_path = Path(args.out) if args.out else img_path.parent / (img_path.stem + "_funduslike.jpg")
    out_path.write_bytes(out_bytes)
    print(f"Saved → {out_path}")

    print("\n⚠️  Reminder: predictions on preprocessed camera images are approximate.")
    print("   For reliable results, fine-tune only the model head on anterior-segment data.")


if __name__ == "__main__":
    main()
