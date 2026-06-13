"""
train.py — Two-phase training:
  Phase 1: Fine-tune Swin-T image model (Stage-1)
  Phase 2: Train MetadataAdjuster MLP  (Stage-2)

Phase 2 runs automatically after Phase 1 if a metadata CSV exists.
You can also skip Phase 1 and only retrain Phase 2 with --skip_image_training.

Usage:
    # Full pipeline (image + metadata adjuster)
    python scripts/train.py \
        --data_dir     ./data/dataset \
        --metadata_csv ./data/metadata.csv \
        --epochs       30 \
        --batch_size   16

    # Image training only (no metadata CSV available yet)
    python scripts/train.py \
        --data_dir   ./data/dataset \
        --epochs     30

    # Retrain Stage-2 adjuster only (after you collect metadata)
    python scripts/train.py \
        --data_dir            ./data/dataset \
        --metadata_csv        ./data/metadata.csv \
        --skip_image_training \
        --model_path          ./output/eye_disease_model.pt
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms
from tqdm import tqdm

# ─── Constants ────────────────────────────────────────────────────────────────
IMG_SIZE      = 224
DEVICE        = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]


# ════════════════════════════════════════════════════════════════════════════════
#  PHASE 1 — IMAGE MODEL TRAINING
# ════════════════════════════════════════════════════════════════════════════════

def build_dataloaders(data_dir: str, batch_size: int
                      ) -> tuple[DataLoader, DataLoader, dict]:
    train_tf = transforms.Compose([
        transforms.RandomResizedCrop(IMG_SIZE, scale=(0.8, 1.0)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomVerticalFlip(),
        transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.1),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    val_tf = transforms.Compose([
        transforms.Resize((IMG_SIZE, IMG_SIZE)),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    train_ds = datasets.ImageFolder(str(Path(data_dir) / "train"), transform=train_tf)
    val_ds   = datasets.ImageFolder(str(Path(data_dir) / "val"),   transform=val_tf)
    train_dl = DataLoader(train_ds, batch_size=batch_size, shuffle=True,
                          num_workers=4, pin_memory=True)
    val_dl   = DataLoader(val_ds,   batch_size=batch_size, shuffle=False,
                          num_workers=4, pin_memory=True)
    class_to_idx = train_ds.class_to_idx
    return train_dl, val_dl, class_to_idx


def build_model(num_classes: int) -> nn.Module:
    model = models.swin_t(weights=models.Swin_T_Weights.IMAGENET1K_V1)
    # Freeze all backbone layers initially
    for p in model.parameters():
        p.requires_grad = False
    # Unfreeze head
    model.head = nn.Linear(model.head.in_features, num_classes)
    return model.to(DEVICE)


def unfreeze_backbone(model: nn.Module, unfreeze_last_n_stages: int = 2):
    """Gradually unfreeze last N Swin stages for fine-tuning."""
    stages = list(model.features.children())
    for stage in stages[-unfreeze_last_n_stages:]:
        for p in stage.parameters():
            p.requires_grad = True
    # Always keep head trainable
    for p in model.head.parameters():
        p.requires_grad = True


def train_image_model(data_dir: str, epochs: int, batch_size: int,
                      out_dir: Path) -> tuple[nn.Module, dict]:
    print("\n" + "="*60)
    print("PHASE 1 — Image Model Training (Swin-T)")
    print("="*60)

    train_dl, val_dl, class_to_idx = build_dataloaders(data_dir, batch_size)
    num_classes = len(class_to_idx)
    idx_to_class = {v: k for k, v in class_to_idx.items()}
    print(f"Classes ({num_classes}): {list(class_to_idx.keys())}")
    print(f"Device: {DEVICE}\n")

    model = build_model(num_classes)

    criterion = nn.CrossEntropyLoss(label_smoothing=0.1)
    # Phase A: only train head
    optimizer = optim.AdamW(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=1e-3, weight_decay=1e-4
    )

    warmup_epochs  = max(3, epochs // 10)
    unfreeze_epoch = max(5, epochs // 5)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=epochs - warmup_epochs)

    best_val_acc = 0.0
    history = {"train_loss": [], "train_acc": [], "val_acc": []}

    for epoch in range(1, epochs + 1):
        # Gradually unfreeze backbone at epoch `unfreeze_epoch`
        if epoch == unfreeze_epoch:
            print(f"\n  [Epoch {epoch}] Unfreezing last 2 Swin stages …")
            unfreeze_backbone(model, unfreeze_last_n_stages=2)
            optimizer = optim.AdamW([
                {"params": model.head.parameters(),  "lr": 1e-3},
                {"params": filter(lambda p: p.requires_grad and
                                  id(p) not in {id(q) for q in model.head.parameters()},
                                  model.parameters()), "lr": 5e-5},
            ], weight_decay=1e-4)
            scheduler = optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=epochs - epoch)

        model.train()
        running_loss, correct, total = 0.0, 0, 0
        for imgs, labels in tqdm(train_dl, desc=f"Epoch {epoch}/{epochs} train",
                                  leave=False):
            imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
            optimizer.zero_grad()
            logits = model(imgs)
            loss   = criterion(logits, labels)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            running_loss += loss.item() * len(labels)
            correct      += (logits.argmax(1) == labels).sum().item()
            total        += len(labels)

        if epoch >= warmup_epochs:
            scheduler.step()

        train_loss = running_loss / total
        train_acc  = correct / total

        # Validation
        model.eval()
        val_correct, val_total = 0, 0
        with torch.no_grad():
            for imgs, labels in val_dl:
                imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
                logits = model(imgs)
                val_correct += (logits.argmax(1) == labels).sum().item()
                val_total   += len(labels)
        val_acc = val_correct / val_total

        history["train_loss"].append(round(train_loss, 4))
        history["train_acc"].append(round(train_acc, 4))
        history["val_acc"].append(round(val_acc, 4))

        print(f"  Epoch {epoch:3d}/{epochs}  loss={train_loss:.4f}  "
              f"train={train_acc*100:.1f}%  val={val_acc*100:.1f}%")

        if val_acc >= best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), out_dir / "eye_disease_model.pt")
            # Also save full model object
            torch.save(model, out_dir / "eye_disease_model_full.pt")

    # Save class mapping and history
    mapping_path = out_dir / "class_mapping.json"
    mapping_path.write_text(json.dumps(class_to_idx, indent=2))
    history_path = out_dir / "training_history.json"
    history_path.write_text(json.dumps(history, indent=2))

    print(f"\n✅ Phase 1 complete. Best val accuracy: {best_val_acc*100:.1f}%")
    print(f"   Model  → {out_dir}/eye_disease_model.pt")
    print(f"   Mapping → {mapping_path}")

    # Load best weights
    model.load_state_dict(torch.load(out_dir / "eye_disease_model.pt",
                                     map_location=DEVICE))
    model.eval()
    return model, idx_to_class


# ════════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Train eye disease model (two-stage)")
    parser.add_argument("--data_dir",             required=True)
    parser.add_argument("--out_dir",              default="./output")
    parser.add_argument("--epochs",               type=int, default=30)
    parser.add_argument("--batch_size",           type=int, default=16)
    # Metadata / Stage-2 args
    parser.add_argument("--metadata_csv",         default=None,
                        help="CSV with patient metadata (optional)")
    parser.add_argument("--adjuster_epochs",      type=int, default=30)
    parser.add_argument("--adjuster_batch_size",  type=int, default=64)
    # Skip flags
    parser.add_argument("--skip_image_training",  action="store_true",
                        help="Skip Phase 1; load existing model_path instead")
    parser.add_argument("--model_path",           default=None,
                        help="Required when --skip_image_training is set")
    parser.add_argument("--skip_adjuster",        action="store_true",
                        help="Skip Phase 2 even if metadata CSV is provided")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase 1 ──
    if args.skip_image_training:
        if not args.model_path:
            raise ValueError("--model_path is required when using --skip_image_training")
        print("\nSkipping Phase 1 — loading existing image model …")
        import torch.nn as nn
        from torchvision import models as tvm
        with open(out_dir / "class_mapping.json") as f:
            mapping = json.load(f)
        num_classes = len(mapping)
        idx_to_class = {v: k for k, v in mapping.items()}
        img_model = tvm.swin_t(weights=None)
        img_model.head = nn.Linear(img_model.head.in_features, num_classes)
        img_model.load_state_dict(torch.load(args.model_path, map_location=DEVICE))
        img_model.to(DEVICE).eval()
    else:
        img_model, idx_to_class = train_image_model(
            data_dir=args.data_dir,
            epochs=args.epochs,
            batch_size=args.batch_size,
            out_dir=out_dir,
        )
        num_classes = len(idx_to_class)

    # ── Phase 2: MetadataAdjuster ──
    if args.skip_adjuster:
        print("\nSkipping Phase 2 (--skip_adjuster set).")
        return

    # Determine metadata CSV path
    metadata_csv = args.metadata_csv
    if not metadata_csv:
        # Check if prepare_dataset.py already split it
        auto_csv = Path(args.data_dir) / "train" / "metadata.csv"
        if auto_csv.exists():
            metadata_csv = str(auto_csv)
            print(f"\nAuto-detected split metadata CSV: {metadata_csv}")

    print("\n" + "="*60)
    print("PHASE 2 — Metadata Adjuster Training (Stage-2 MLP)")
    print("="*60)

    # Freeze image model
    for p in img_model.parameters():
        p.requires_grad = False

    from metadata_adjuster import train_adjuster, load_metadata_csv
    metadata_map = {}
    if metadata_csv and Path(metadata_csv).exists():
        metadata_map = load_metadata_csv(metadata_csv)
        print(f"Metadata CSV loaded: {len(metadata_map)} entries")
    else:
        print("No metadata CSV available. Training adjuster with zero metadata.")
        print("It will learn to be a pass-through. Add metadata later and retrain Phase 2.")

    train_adjuster(
        image_model=img_model,
        num_classes=num_classes,
        data_dir=args.data_dir,
        metadata_map=metadata_map,
        epochs=args.adjuster_epochs,
        batch_size=args.adjuster_batch_size,
        out_dir=str(out_dir),
    )

    print("\n✅ Full training pipeline complete.")
    print(f"   Stage-1 weights  → {out_dir}/eye_disease_model.pt")
    print(f"   Stage-2 weights  → {out_dir}/metadata_adjuster.pt")
    print(f"   Class mapping    → {out_dir}/class_mapping.json")


if __name__ == "__main__":
    main()