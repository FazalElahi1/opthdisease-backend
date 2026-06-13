"""
augment.py — Fundus-specific image augmentation pipeline.
Techniques:
  - CLAHE            : contrast enhancement for dark fundus images
  - Green channel    : retinal vessels more visible
  - Circle crop      : remove black border around fundus disc
  - Brightness norm  : standardise luminance across dataset
  - Standard augments: flip, rotation, colour jitter

Usage:
    # Preview augmentations on a folder (no files written)
    python scripts/augment.py \
        --img_dir  ./data/raw_data/Diabetic_Retinopathy \
        --preview

    # Augment the entire raw_data folder and write to augmented_data/
    python scripts/augment.py \
        --img_dir  ./data/raw_data \
        --out_dir  ./data/augmented_data \
        --factor   3          # generate 3 augmented copies per original

    # Augment a single class with custom settings
    python scripts/augment.py \
        --img_dir  ./data/raw_data/Glaucoma \
        --out_dir  ./data/augmented_data/Glaucoma \
        --factor   5 \
        --clahe_clip 3.0
"""

import argparse
import os
import random
from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image, ImageEnhance

# ─── Supported extensions ─────────────────────────────────────────────────────
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}


# ════════════════════════════════════════════════════════════════════════════════
#  FUNDUS-SPECIFIC TRANSFORMS
# ════════════════════════════════════════════════════════════════════════════════

def apply_clahe(img_bgr: np.ndarray, clip_limit: float = 2.0,
                tile_grid: tuple = (8, 8)) -> np.ndarray:
    """
    Apply CLAHE (Contrast Limited Adaptive Histogram Equalisation) to the
    L channel of the LAB colour space.  Improves lesion / vessel visibility
    in dark or unevenly illuminated fundus images.
    """
    lab   = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid)
    l_eq  = clahe.apply(l)
    lab_eq = cv2.merge([l_eq, a, b])
    return cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)


def extract_green_channel(img_bgr: np.ndarray) -> np.ndarray:
    """
    Return a 3-channel BGR image where all channels are set to the green
    channel.  Retinal vessels and haemorrhages are most visible in green.
    """
    green = img_bgr[:, :, 1]
    return cv2.merge([green, green, green])


def circle_crop(img_bgr: np.ndarray, tol: int = 7) -> np.ndarray:
    """
    Remove the black circular border that surrounds most fundus photographs.
    Steps:
      1. Convert to grayscale, threshold at `tol`
      2. Find the bounding rect of the non-black region
      3. Crop and return
    If the disc is not found (fully bright image), return original unchanged.
    """
    gray  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mask  = gray > tol
    rows  = np.any(mask, axis=1)
    cols  = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return img_bgr
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    return img_bgr[rmin:rmax + 1, cmin:cmax + 1]


def normalise_brightness(img_bgr: np.ndarray,
                         target_mean: float = 128.0) -> np.ndarray:
    """
    Scale pixel values so the mean brightness of the green channel equals
    `target_mean`.  Compensates for imaging device luminance differences.
    """
    green      = img_bgr[:, :, 1].astype(np.float32)
    cur_mean   = green.mean()
    if cur_mean < 1e-3:
        return img_bgr
    scale      = target_mean / cur_mean
    normalised = np.clip(img_bgr.astype(np.float32) * scale, 0, 255).astype(np.uint8)
    return normalised


# ════════════════════════════════════════════════════════════════════════════════
#  STANDARD SPATIAL / COLOUR AUGMENTATIONS
# ════════════════════════════════════════════════════════════════════════════════

def random_flip(img_bgr: np.ndarray) -> np.ndarray:
    choice = random.randint(0, 2)
    if choice == 0:
        return cv2.flip(img_bgr, 1)   # horizontal
    if choice == 1:
        return cv2.flip(img_bgr, 0)   # vertical
    return img_bgr                    # no flip


def random_rotate(img_bgr: np.ndarray,
                  max_angle: float = 30.0) -> np.ndarray:
    angle = random.uniform(-max_angle, max_angle)
    h, w  = img_bgr.shape[:2]
    M     = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img_bgr, M, (w, h),
                          borderMode=cv2.BORDER_REFLECT_101)


def random_zoom(img_bgr: np.ndarray,
                min_scale: float = 0.85, max_scale: float = 1.15) -> np.ndarray:
    scale = random.uniform(min_scale, max_scale)
    h, w  = img_bgr.shape[:2]
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(img_bgr, (nw, nh))
    # Crop or pad back to original size
    if scale >= 1.0:
        y0 = (nh - h) // 2
        x0 = (nw - w) // 2
        return resized[y0:y0 + h, x0:x0 + w]
    else:
        out = np.zeros_like(img_bgr)
        y0  = (h - nh) // 2
        x0  = (w - nw) // 2
        out[y0:y0 + nh, x0:x0 + nw] = resized
        return out


def random_colour_jitter(img_bgr: np.ndarray) -> np.ndarray:
    """Randomly adjust brightness, contrast, saturation via PIL."""
    img_pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    img_pil = ImageEnhance.Brightness(img_pil).enhance(random.uniform(0.7, 1.3))
    img_pil = ImageEnhance.Contrast(img_pil).enhance(random.uniform(0.8, 1.2))
    img_pil = ImageEnhance.Color(img_pil).enhance(random.uniform(0.8, 1.2))
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)


def random_gaussian_noise(img_bgr: np.ndarray,
                          std: float = 10.0) -> np.ndarray:
    noise  = np.random.normal(0, std, img_bgr.shape).astype(np.float32)
    noisy  = np.clip(img_bgr.astype(np.float32) + noise, 0, 255)
    return noisy.astype(np.uint8)


# ════════════════════════════════════════════════════════════════════════════════
#  FULL PIPELINE
# ════════════════════════════════════════════════════════════════════════════════

def fundus_preprocess(img_bgr: np.ndarray,
                      clahe_clip: float = 2.0,
                      target_brightness: float = 128.0,
                      use_green: bool = False,
                      do_crop: bool = True) -> np.ndarray:
    """
    Deterministic fundus pre-processing (applied before random augmentation).
    """
    if do_crop:
        img_bgr = circle_crop(img_bgr)
    img_bgr = normalise_brightness(img_bgr, target_mean=target_brightness)
    img_bgr = apply_clahe(img_bgr, clip_limit=clahe_clip)
    if use_green:
        img_bgr = extract_green_channel(img_bgr)
    return img_bgr


def random_augment(img_bgr: np.ndarray) -> np.ndarray:
    """Apply a random combination of spatial + colour augmentations."""
    if random.random() > 0.5:
        img_bgr = random_flip(img_bgr)
    if random.random() > 0.5:
        img_bgr = random_rotate(img_bgr)
    if random.random() > 0.4:
        img_bgr = random_zoom(img_bgr)
    if random.random() > 0.5:
        img_bgr = random_colour_jitter(img_bgr)
    if random.random() > 0.7:
        img_bgr = random_gaussian_noise(img_bgr, std=random.uniform(3, 15))
    return img_bgr


def full_pipeline(img_bgr: np.ndarray,
                  clahe_clip: float = 2.0,
                  target_brightness: float = 128.0) -> np.ndarray:
    """Preprocess + random augmentation in one call."""
    img_bgr = fundus_preprocess(img_bgr, clahe_clip, target_brightness)
    return random_augment(img_bgr)


# ════════════════════════════════════════════════════════════════════════════════
#  PREVIEW MODE
# ════════════════════════════════════════════════════════════════════════════════

def preview_augmentations(img_dir: str, n_samples: int = 4,
                           clahe_clip: float = 2.0):
    img_dir = Path(img_dir)
    paths   = sorted(
        p for p in img_dir.rglob("*") if p.suffix.lower() in SUPPORTED_EXT
    )[:n_samples]

    if not paths:
        print("No images found in", img_dir)
        return

    for img_path in paths:
        original = cv2.imread(str(img_path))
        if original is None:
            continue
        original = cv2.cvtColor(original, cv2.COLOR_BGR2RGB)

        bgr      = cv2.cvtColor(original, cv2.COLOR_RGB2BGR)
        cropped  = cv2.cvtColor(circle_crop(bgr), cv2.COLOR_BGR2RGB)
        bright   = cv2.cvtColor(normalise_brightness(bgr), cv2.COLOR_BGR2RGB)
        clahe_im = cv2.cvtColor(apply_clahe(bgr, clahe_clip), cv2.COLOR_BGR2RGB)
        green_im = cv2.cvtColor(extract_green_channel(bgr), cv2.COLOR_BGR2RGB)
        aug1     = cv2.cvtColor(full_pipeline(bgr.copy(), clahe_clip), cv2.COLOR_BGR2RGB)
        aug2     = cv2.cvtColor(full_pipeline(bgr.copy(), clahe_clip), cv2.COLOR_BGR2RGB)

        fig, axes = plt.subplots(2, 4, figsize=(18, 9))
        fig.suptitle(img_path.name, fontsize=12)
        panels = [
            (original,  "Original"),
            (cropped,   "Circle Crop"),
            (bright,    "Brightness Norm"),
            (clahe_im,  "CLAHE"),
            (green_im,  "Green Channel"),
            (aug1,      "Augmented v1"),
            (aug2,      "Augmented v2"),
        ]
        for ax, (img_show, title) in zip(axes.flatten(), panels):
            ax.imshow(img_show)
            ax.set_title(title, fontsize=10)
            ax.axis("off")
        axes.flatten()[-1].axis("off")   # hide last empty cell
        plt.tight_layout()
        plt.show()


# ════════════════════════════════════════════════════════════════════════════════
#  BATCH AUGMENTATION
# ════════════════════════════════════════════════════════════════════════════════

def augment_directory(img_dir: str, out_dir: str,
                      factor: int = 3,
                      clahe_clip: float = 2.0,
                      target_brightness: float = 128.0,
                      output_size: int = 224):
    """
    For every image in img_dir (recursively), generate `factor` augmented
    copies and write them to out_dir, preserving subdirectory structure.
    """
    img_dir = Path(img_dir)
    out_dir = Path(out_dir)

    all_images = sorted(
        p for p in img_dir.rglob("*") if p.suffix.lower() in SUPPORTED_EXT
    )
    print(f"Found {len(all_images)} images under {img_dir}")
    print(f"Generating {factor} copies each → {len(all_images) * factor} total\n")

    for i, img_path in enumerate(all_images):
        rel    = img_path.relative_to(img_dir)
        dst_dir = out_dir / rel.parent
        dst_dir.mkdir(parents=True, exist_ok=True)

        bgr = cv2.imread(str(img_path))
        if bgr is None:
            print(f"  SKIP (unreadable): {img_path.name}")
            continue

        # Save the pre-processed (but non-augmented) original
        pre = fundus_preprocess(bgr, clahe_clip, target_brightness)
        pre = cv2.resize(pre, (output_size, output_size))
        out_orig = dst_dir / (img_path.stem + "_pre" + img_path.suffix)
        cv2.imwrite(str(out_orig), pre)

        # Save augmented copies
        for k in range(factor):
            aug     = full_pipeline(bgr.copy(), clahe_clip, target_brightness)
            aug     = cv2.resize(aug, (output_size, output_size))
            out_aug = dst_dir / (img_path.stem + f"_aug{k:02d}" + img_path.suffix)
            cv2.imwrite(str(out_aug), aug)

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(all_images)}] processed …")

    total = len(list(out_dir.rglob("*.*")))
    print(f"\n✅ Augmentation complete. {total} files written to: {out_dir}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fundus image augmentation pipeline")
    parser.add_argument("--img_dir",    required=True,
                        help="Source directory (raw images or class subfolder)")
    parser.add_argument("--out_dir",    default="./data/augmented_data",
                        help="Destination directory (ignored in --preview mode)")
    parser.add_argument("--factor",     type=int, default=3,
                        help="Augmented copies per image (default: 3)")
    parser.add_argument("--clahe_clip", type=float, default=2.0,
                        help="CLAHE clip limit (default: 2.0)")
    parser.add_argument("--brightness", type=float, default=128.0,
                        help="Target mean brightness (default: 128)")
    parser.add_argument("--size",       type=int, default=224,
                        help="Output image size in pixels (default: 224)")
    parser.add_argument("--preview",    action="store_true",
                        help="Show augmentation previews without saving files")
    parser.add_argument("--n_preview",  type=int, default=4,
                        help="Number of images to preview (default: 4)")
    args = parser.parse_args()

    if args.preview:
        print("Preview mode — no files will be written.\n")
        preview_augmentations(
            args.img_dir,
            n_samples=args.n_preview,
            clahe_clip=args.clahe_clip,
        )
    else:
        augment_directory(
            img_dir=args.img_dir,
            out_dir=args.out_dir,
            factor=args.factor,
            clahe_clip=args.clahe_clip,
            target_brightness=args.brightness,
            output_size=args.size,
        )


if __name__ == "__main__":
    main()