"""
prepare_dataset.py — Split raw images into train/val/test.
Now also handles an optional metadata CSV: copies and filters it per split.

Usage:
    python scripts/prepare_dataset.py \
        --raw_dir   ./data/raw_data \
        --out_dir   ./data/dataset

    # With metadata CSV:
    python scripts/prepare_dataset.py \
        --raw_dir      ./data/raw_data \
        --out_dir      ./data/dataset \
        --metadata_csv ./data/metadata.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import random
from collections import defaultdict
from pathlib import Path

# ─── Constants ────────────────────────────────────────────────────────────────
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}
SPLIT_RATIOS  = {"train": 0.70, "val": 0.15, "test": 0.15}
RANDOM_SEED   = 42


def collect_images(raw_dir: Path) -> dict[str, list[Path]]:
    """Returns {class_name: [image_paths]} for all subdirectories."""
    class_images: dict[str, list[Path]] = defaultdict(list)
    for class_dir in sorted(raw_dir.iterdir()):
        if not class_dir.is_dir():
            continue
        imgs = sorted(
            p for p in class_dir.rglob("*")
            if p.suffix.lower() in SUPPORTED_EXT
        )
        if imgs:
            class_images[class_dir.name] = imgs
    return dict(class_images)


def split_images(images: list[Path], seed: int = RANDOM_SEED
                 ) -> dict[str, list[Path]]:
    """Stratified split of a single class image list."""
    rng = random.Random(seed)
    shuffled = images[:]
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_train = max(1, int(n * SPLIT_RATIOS["train"]))
    n_val   = max(1, int(n * SPLIT_RATIOS["val"]))
    return {
        "train": shuffled[:n_train],
        "val":   shuffled[n_train:n_train + n_val],
        "test":  shuffled[n_train + n_val:],
    }


def copy_split(split_images: dict[str, list[Path]],
               class_name: str, out_dir: Path) -> dict[str, list[str]]:
    """
    Copy images to out_dir/{split}/{class_name}/.
    Returns dict of split → list of relative paths (for metadata filtering).
    """
    rel_paths: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    for split, imgs in split_images.items():
        dst_dir = out_dir / split / class_name
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in imgs:
            dst = dst_dir / src.name
            shutil.copy2(src, dst)
            rel_paths[split].append(f"{src.name}")
            # {class_name}/
    return rel_paths


def split_metadata_csv(csv_path: Path, split_rel_paths: dict[str, dict[str, list[str]]],
                       out_dir: Path):
    """
    Read the full metadata CSV and write per-split CSVs to out_dir/{split}/metadata.csv.
    Images not present in the CSV are silently skipped (metadata is optional).
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        all_rows   = list(reader)

    # Build lookup: relative_path → row
    row_lookup = {
        row.get("image_filename", "").strip(): row
        for row in all_rows
        if row.get("image_filename", "").strip()
    }

    for split in ("train", "val", "test"):
        split_paths: list[str] = []
        for class_paths in split_rel_paths.values():
            split_paths.extend(class_paths.get(split, []))

        dst = out_dir / split / "metadata.csv"
        matched_rows = [
            row_lookup[p] for p in split_paths if p in row_lookup
        ]
        coverage = len(matched_rows) / max(len(split_paths), 1) * 100
        print(f"  {split:5s}: {len(matched_rows)}/{len(split_paths)} images "
              f"have metadata ({coverage:.0f}%) → {dst}")

        with open(dst, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(matched_rows)


def main():
    parser = argparse.ArgumentParser(
        description="Split raw fundus images into train/val/test dataset")
    parser.add_argument("--raw_dir",      required=True,
                        help="Root directory with class subfolders")
    parser.add_argument("--out_dir",      required=True,
                        help="Output directory for dataset splits")
    parser.add_argument("--metadata_csv", default=None,
                        help="Optional CSV with patient metadata per image")
    parser.add_argument("--seed",         type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nSource:  {raw_dir}")
    print(f"Output:  {out_dir}")
    print(f"Splits:  train={SPLIT_RATIOS['train']:.0%}  "
          f"val={SPLIT_RATIOS['val']:.0%}  test={SPLIT_RATIOS['test']:.0%}\n")

    class_images = collect_images(raw_dir)
    if not class_images:
        raise ValueError(f"No image subdirectories found in {raw_dir}")

    print(f"Found {len(class_images)} classes:")
    all_split_rel: dict[str, dict[str, list[str]]] = {}

    for class_name, images in class_images.items():
        splits  = split_images(images, seed=args.seed)
        rel     = copy_split(splits, class_name, out_dir)
        all_split_rel[class_name] = rel
        counts  = {s: len(v) for s, v in splits.items()}
        print(f"  {class_name:35s}  total={len(images):4d}  "
              f"train={counts['train']:4d}  val={counts['val']:3d}  test={counts['test']:3d}")

    # Handle metadata CSV
    if args.metadata_csv:
        csv_path = Path(args.metadata_csv)
        if csv_path.exists():
            print(f"\nSplitting metadata CSV: {csv_path}")
            split_metadata_csv(csv_path, all_split_rel, out_dir)
        else:
            print(f"\n⚠️  metadata_csv not found: {csv_path} — skipping.")
    else:
        print("\nNo metadata CSV provided.")
        print("To add metadata later, create a CSV matching metadata_schema.json")
        print("and re-run with --metadata_csv path/to/metadata.csv")

    # Print totals
    print("\n── Summary ──")
    for split in ("train", "val", "test"):
        total = sum(
            len(v.get(split, []))
            for v in all_split_rel.values()
        )
        print(f"  {split:5s}: {total} images")

    print(f"\n✅ Dataset ready at: {out_dir}")


if __name__ == "__main__":
    main()
    
    
