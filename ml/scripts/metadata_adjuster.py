"""
metadata_adjuster.py — Stage-2 metadata MLP: definition, training, and saving.

Architecture (Two-Stage):
  Stage 1: Swin-T image model → (predicted_class_idx, confidence, all_probs[8])
  Stage 2: THIS FILE — MLP takes (image_probs[8] + metadata_vec[32]) → adjusted_probs[8]

The Stage-2 MLP is lightweight (~50K params). It learns:
  - When metadata is all zeros (not provided), pass Stage-1 probs through unchanged
  - When metadata is provided, up-weight or down-weight specific classes

Training:
  - Uses the same dataset as Stage-1 but with metadata CSV
  - Trains ONLY the Stage-2 MLP; Stage-1 weights are frozen
  - Images without metadata are included (metadata vec = zeros, mask = zeros)

Usage:
    python scripts/metadata_adjuster.py \
        --model_path   ./output/eye_disease_model.pt \
        --mapping_path ./output/class_mapping.json \
        --data_dir     ./data/dataset \
        --metadata_csv ./data/metadata.csv \
        --epochs       30 \
        --out_dir      ./output
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from torchvision import datasets, models, transforms
from tqdm import tqdm

try:
    from metadata_encoder import encode_metadata, METADATA_DIM, metadata_from_csv_row
except ImportError:
    from ml.scripts.metadata_encoder import encode_metadata, METADATA_DIM, metadata_from_csv_row

# ─── Constants ────────────────────────────────────────────────────────────────
IMG_SIZE      = 224
DEVICE        = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]


# ════════════════════════════════════════════════════════════════════════════════
#  STAGE-2 MLP MODEL
# ════════════════════════════════════════════════════════════════════════════════

class MetadataAdjuster(nn.Module):
    """
    Lightweight MLP that refines Stage-1 image probabilities using metadata.

    Input:  concat(image_probs[num_classes], metadata_vec[METADATA_DIM])
            = (num_classes + 32) dims
    Output: adjusted logits[num_classes]  (apply softmax for final probs)

    When metadata_vec is all zeros (not provided), the network learns to
    behave as an identity pass-through — it just re-predicts the image probs.
    The binary mask inside metadata_vec (dims 27-31) explicitly signals this.
    """

    def __init__(self, num_classes: int, metadata_dim: int = METADATA_DIM,
                 hidden_dims: list[int] = None):
        super().__init__()
        if hidden_dims is None:
            hidden_dims = [128, 64]

        input_dim = num_classes + metadata_dim
        layers = []
        prev_dim = input_dim
        for h in hidden_dims:
            layers += [
                nn.Linear(prev_dim, h),
                nn.LayerNorm(h),
                nn.GELU(),
                nn.Dropout(0.2),
            ]
            prev_dim = h
        layers.append(nn.Linear(prev_dim, num_classes))

        self.mlp = nn.Sequential(*layers)

        # Residual projection: maps image_probs → num_classes
        # Forces the network to learn corrections, not start from scratch
        self.residual_proj = nn.Linear(num_classes, num_classes, bias=False)
        nn.init.eye_(self.residual_proj.weight)   # initialise as identity

    def forward(self, image_probs: torch.Tensor,
                metadata_vec: torch.Tensor) -> torch.Tensor:
        """
        Parameters
        ----------
        image_probs  : (B, num_classes)  — softmax probs from Stage-1
        metadata_vec : (B, METADATA_DIM) — encoded metadata (zeros if missing)

        Returns
        -------
        adjusted_logits : (B, num_classes)
        """
        x = torch.cat([image_probs, metadata_vec], dim=1)
        correction = self.mlp(x)
        residual   = self.residual_proj(image_probs)
        return residual + correction   # residual connection keeps Stage-1 signal


# ════════════════════════════════════════════════════════════════════════════════
#  DATASET WITH METADATA
# ════════════════════════════════════════════════════════════════════════════════

class FundusWithMetaDataset(Dataset):
    """
    Wraps a standard ImageFolder and attaches per-image metadata vectors.

    metadata_map: dict mapping relative path (e.g. 'AMD/img001.jpg') → meta dict.
    Images without an entry in metadata_map get a zero metadata vector.
    """

    def __init__(self, data_dir: str, transform,
                 metadata_map: dict[str, dict] | None = None):
        self.base = datasets.ImageFolder(data_dir, transform=transform)
        self.metadata_map = metadata_map or {}
        self._root = Path(data_dir)

    def __len__(self):
        return len(self.base)

    def __getitem__(self, idx):
        img_tensor, label = self.base[idx]
        img_path = Path(self.base.samples[idx][0])

        # Build relative key: "ClassName/filename.jpg"
        try:
            rel = img_path.relative_to(self._root)
            key = str(rel).replace("\\", "/")
        except ValueError:
            key = img_path.name

        raw_meta = self.metadata_map.get(key) or self.metadata_map.get(img_path.name)
        meta_vec = encode_metadata(raw_meta)
        return img_tensor, torch.tensor(meta_vec, dtype=torch.float32), label


# ════════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def load_image_model(model_path: str, num_classes: int) -> nn.Module:
    model = models.swin_t(weights=None)
    model.head = nn.Linear(model.head.in_features, num_classes)
    state = torch.load(model_path, map_location=DEVICE)
    model.load_state_dict(state)
    model.to(DEVICE)
    model.eval()
    for p in model.parameters():
        p.requires_grad = False   # Stage-1 is frozen
    return model


def load_metadata_csv(csv_path: str) -> dict[str, dict]:
    """
    Load metadata CSV into a dict keyed by image relative path.
    CSV must have at minimum: image_filename column.
    All other columns are optional.
    """
    import csv
    metadata_map: dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filename = row.get("image_filename", "").strip()
            if not filename:
                continue
            metadata_map[filename] = metadata_from_csv_row(row)
    return metadata_map


@torch.no_grad()
def get_image_probs(image_model: nn.Module,
                    loader: DataLoader) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Run Stage-1 inference on all images.
    Returns (image_probs, metadata_vecs, labels) as numpy arrays.
    """
    all_probs, all_meta, all_labels = [], [], []
    for imgs, meta_vecs, labels in tqdm(loader, desc="Stage-1 inference"):
        imgs = imgs.to(DEVICE)
        logits = image_model(imgs)
        probs  = torch.softmax(logits, dim=1).cpu().numpy()
        all_probs.extend(probs)
        all_meta.extend(meta_vecs.numpy())
        all_labels.extend(labels.numpy())
    return np.array(all_probs), np.array(all_meta), np.array(all_labels)


# ════════════════════════════════════════════════════════════════════════════════
#  TRAINING LOOP
# ════════════════════════════════════════════════════════════════════════════════

class ProbeDataset(Dataset):
    """In-memory dataset of (image_probs, meta_vec, label) for Stage-2 training."""
    def __init__(self, probs, meta, labels):
        self.probs  = torch.tensor(probs,   dtype=torch.float32)
        self.meta   = torch.tensor(meta,    dtype=torch.float32)
        self.labels = torch.tensor(labels,  dtype=torch.long)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return self.probs[idx], self.meta[idx], self.labels[idx]


def train_adjuster(
    image_model:    nn.Module,
    num_classes:    int,
    data_dir:       str,
    metadata_map:   dict[str, dict],
    epochs:         int = 30,
    batch_size:     int = 64,
    lr:             float = 1e-3,
    out_dir:        str  = "./output",
) -> MetadataAdjuster:

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    transform = transforms.Compose([
        transforms.Resize((IMG_SIZE, IMG_SIZE)),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])

    # ── Build datasets for train and val ──
    splits = {}
    for split in ("train", "val"):
        split_path = Path(data_dir) / split
        if not split_path.exists():
            continue
        ds = FundusWithMetaDataset(str(split_path), transform, metadata_map)
        splits[split] = DataLoader(ds, batch_size=batch_size, shuffle=False,
                                   num_workers=4, pin_memory=True)

    # ── Stage-1 inference to get image_probs ──
    print("\n[Stage-1] Running image model to collect probabilities …")
    split_data = {}
    for split, loader in splits.items():
        probs, meta, labels = get_image_probs(image_model, loader)
        split_data[split] = ProbeDataset(probs, meta, labels)

    train_ds = split_data.get("train")
    val_ds   = split_data.get("val")

    # ── Compute metadata coverage stats ──
    meta_present = (split_data["train"].meta.sum(dim=1) > 0).float().mean().item()
    print(f"\n  Metadata coverage in training set: {meta_present*100:.1f}%")
    if meta_present < 0.05:
        print("  ⚠️  Very few training images have metadata. "
              "Stage-2 will still train but gains will be minimal until you add metadata.")

    # ── Build Stage-2 adjuster ──
    adjuster = MetadataAdjuster(num_classes=num_classes).to(DEVICE)

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader   = DataLoader(val_ds,   batch_size=batch_size, shuffle=False) if val_ds else None

    criterion = nn.CrossEntropyLoss(label_smoothing=0.1)
    optimizer = optim.AdamW(adjuster.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_val_acc = 0.0
    history = {"train_loss": [], "train_acc": [], "val_acc": []}

    print("\n[Stage-2] Training MetadataAdjuster …\n")
    for epoch in range(1, epochs + 1):
        adjuster.train()
        running_loss = 0.0
        correct = 0

        for probs, meta, labels in train_loader:
            probs, meta, labels = probs.to(DEVICE), meta.to(DEVICE), labels.to(DEVICE)
            optimizer.zero_grad()
            logits = adjuster(probs, meta)
            loss   = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            running_loss += loss.item() * len(labels)
            correct      += (logits.argmax(1) == labels).sum().item()

        scheduler.step()
        train_loss = running_loss / len(train_ds)
        train_acc  = correct / len(train_ds)

        val_acc = 0.0
        if val_loader:
            adjuster.eval()
            val_correct = 0
            with torch.no_grad():
                for probs, meta, labels in val_loader:
                    probs, meta, labels = probs.to(DEVICE), meta.to(DEVICE), labels.to(DEVICE)
                    logits = adjuster(probs, meta)
                    val_correct += (logits.argmax(1) == labels).sum().item()
            val_acc = val_correct / len(val_ds)

        history["train_loss"].append(round(train_loss, 4))
        history["train_acc"].append(round(train_acc, 4))
        history["val_acc"].append(round(val_acc, 4))

        print(f"  Epoch {epoch:3d}/{epochs}  "
              f"loss={train_loss:.4f}  train_acc={train_acc*100:.1f}%  "
              f"val_acc={val_acc*100:.1f}%")

        if val_acc >= best_val_acc:
            best_val_acc = val_acc
            torch.save(adjuster.state_dict(),
                       out_dir / "metadata_adjuster.pt")

    # Save training history
    history_path = out_dir / "adjuster_training_history.json"
    history_path.write_text(json.dumps(history, indent=2))
    print(f"\n✅ Best val accuracy: {best_val_acc*100:.1f}%")
    print(f"   Weights saved → {out_dir}/metadata_adjuster.pt")
    print(f"   History saved → {history_path}")

    # Load best weights back
    adjuster.load_state_dict(torch.load(out_dir / "metadata_adjuster.pt",
                                        map_location=DEVICE))
    return adjuster


# ════════════════════════════════════════════════════════════════════════════════
#  INFERENCE HELPER (used by predict.py and ml_integration.py)
# ════════════════════════════════════════════════════════════════════════════════

def load_adjuster(adjuster_path: str, num_classes: int) -> Optional[MetadataAdjuster]:
    """Load a saved MetadataAdjuster. Returns None if file doesn't exist."""
    path = Path(adjuster_path)
    if not path.exists():
        return None
    adj = MetadataAdjuster(num_classes=num_classes)
    adj.load_state_dict(torch.load(str(path), map_location=DEVICE))
    adj.to(DEVICE)
    adj.eval()
    return adj


@torch.no_grad()
def adjust_probs(adjuster: Optional[MetadataAdjuster],
                 image_probs_np: np.ndarray,
                 metadata_vec_np: np.ndarray) -> np.ndarray:
    """
    Apply Stage-2 adjustment.

    Parameters
    ----------
    adjuster        : MetadataAdjuster or None (if None, returns image_probs unchanged)
    image_probs_np  : (num_classes,) numpy float32
    metadata_vec_np : (METADATA_DIM,) numpy float32  (zeros = no metadata)

    Returns
    -------
    adjusted_probs  : (num_classes,) numpy float32
    """
    if adjuster is None:
        return image_probs_np

    # If metadata is entirely zero → no adjustment, return Stage-1 probs
    if metadata_vec_np.sum() == 0.0:
        return image_probs_np

    probs_t = torch.tensor(image_probs_np, dtype=torch.float32).unsqueeze(0).to(DEVICE)
    meta_t  = torch.tensor(metadata_vec_np, dtype=torch.float32).unsqueeze(0).to(DEVICE)
    logits  = adjuster(probs_t, meta_t)
    adjusted = torch.softmax(logits, dim=1)[0].cpu().numpy()
    return adjusted


# ════════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Train Stage-2 MetadataAdjuster MLP")
    parser.add_argument("--model_path",   required=True, help="Path to eye_disease_model.pt")
    parser.add_argument("--mapping_path", required=True, help="Path to class_mapping.json")
    parser.add_argument("--data_dir",     required=True, help="Dataset root (train/val/test)")
    parser.add_argument("--metadata_csv", default=None,
                        help="Optional CSV with patient metadata (see metadata_schema.json)")
    parser.add_argument("--epochs",       type=int, default=30)
    parser.add_argument("--batch_size",   type=int, default=64)
    parser.add_argument("--lr",           type=float, default=1e-3)
    parser.add_argument("--out_dir",      default="./output")
    args = parser.parse_args()

    with open(args.mapping_path) as f:
        mapping = json.load(f)
    if not next(iter(mapping.keys())).isdigit():
        idx_to_class = {v: k for k, v in mapping.items()}
    else:
        idx_to_class = {int(k): v for k, v in mapping.items()}
    num_classes = len(idx_to_class)

    print(f"\nLoading Stage-1 image model ({num_classes} classes) …")
    image_model = load_image_model(args.model_path, num_classes)

    metadata_map: dict[str, dict] = {}
    if args.metadata_csv and Path(args.metadata_csv).exists():
        metadata_map = load_metadata_csv(args.metadata_csv)
        print(f"Loaded metadata for {len(metadata_map)} images from {args.metadata_csv}")
    else:
        print("No metadata CSV provided — training Stage-2 with zero metadata vectors.")
        print("The adjuster will learn to pass Stage-1 probs through unchanged.")

    train_adjuster(
        image_model=image_model,
        num_classes=num_classes,
        data_dir=args.data_dir,
        metadata_map=metadata_map,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        out_dir=args.out_dir,
    )


if __name__ == "__main__":
    main()