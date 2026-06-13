"""
evaluate.py — Evaluate Stage-1 and Stage-2 separately and compare.
Outputs confusion matrices, ROC curves, per-class metrics, and a
side-by-side comparison of image-only vs image+metadata accuracy.

Usage:
    python scripts/evaluate.py \
        --model_path     ./output/eye_disease_model.pt \
        --adjuster_path  ./output/metadata_adjuster.pt \
        --mapping_path   ./output/class_mapping.json \
        --data_dir       ./data/dataset \
        --metadata_csv   ./data/dataset/test/metadata.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import (
    classification_report, confusion_matrix,
    roc_auc_score, roc_curve,
)
from torch.utils.data import DataLoader
from torchvision import models, transforms
from tqdm import tqdm

try:
    from metadata_encoder import encode_metadata, METADATA_DIM, metadata_from_csv_row
    from metadata_adjuster import (
        MetadataAdjuster, FundusWithMetaDataset,
        load_adjuster, adjust_probs, load_metadata_csv
    )
except ImportError:
    from ml.scripts.metadata_encoder import encode_metadata, METADATA_DIM, metadata_from_csv_row
    from ml.scripts.metadata_adjuster import (
        MetadataAdjuster, FundusWithMetaDataset,
        load_adjuster, adjust_probs, load_metadata_csv
    )

IMG_SIZE      = 224
DEVICE        = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]


def load_image_model(model_path, num_classes):
    model = models.swin_t(weights=None)
    model.head = nn.Linear(model.head.in_features, num_classes)
    model.load_state_dict(torch.load(model_path, map_location=DEVICE))
    model.to(DEVICE).eval()
    return model


def build_loader(data_dir, split, metadata_map):
    tf = transforms.Compose([
        transforms.Resize((IMG_SIZE, IMG_SIZE)),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    ds = FundusWithMetaDataset(str(Path(data_dir) / split), tf, metadata_map)
    return DataLoader(ds, batch_size=32, shuffle=False, num_workers=4)


@torch.no_grad()
def run_inference(image_model, adjuster, loader):
    all_labels, all_img_preds, all_adj_preds = [], [], []
    all_img_probs, all_adj_probs, all_meta_mask = [], [], []

    for imgs, meta_vecs, labels in tqdm(loader, desc="Evaluating"):
        imgs = imgs.to(DEVICE)
        logits = image_model(imgs)
        img_probs = torch.softmax(logits, dim=1).cpu().numpy()

        for i in range(len(labels)):
            meta_np = meta_vecs[i].numpy()
            has_meta = meta_np.sum() > 0
            all_meta_mask.append(has_meta)
            adj_probs = adjust_probs(adjuster, img_probs[i], meta_np)
            all_img_preds.append(int(np.argmax(img_probs[i])))
            all_adj_preds.append(int(np.argmax(adj_probs)))
            all_img_probs.append(img_probs[i])
            all_adj_probs.append(adj_probs)

        all_labels.extend(labels.numpy())

    return (np.array(all_labels), np.array(all_img_preds),
            np.array(all_adj_preds), np.array(all_img_probs),
            np.array(all_adj_probs), np.array(all_meta_mask, dtype=bool))


def plot_cm(cm, class_names, title, out_path):
    fig, ax = plt.subplots(figsize=(10, 8))
    im = ax.imshow(cm, cmap=plt.cm.Blues)
    plt.colorbar(im, ax=ax)
    ax.set(xticks=np.arange(len(class_names)), yticks=np.arange(len(class_names)),
           xticklabels=class_names, yticklabels=class_names,
           xlabel="Predicted", ylabel="True", title=title)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    thresh = cm.max() / 2
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, str(cm[i, j]), ha="center", va="center",
                    color="white" if cm[i, j] > thresh else "black", fontsize=9)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def plot_roc(labels, probs, class_names, title, out_path):
    n = len(class_names)
    oh = np.eye(n)[labels]
    fig, ax = plt.subplots(figsize=(10, 8))
    colors = plt.cm.tab10(np.linspace(0, 1, n))
    aucs = {}
    for i, (cls, col) in enumerate(zip(class_names, colors)):
        fpr, tpr, _ = roc_curve(oh[:, i], probs[:, i])
        auc = roc_auc_score(oh[:, i], probs[:, i])
        aucs[cls] = round(float(auc), 4)
        ax.plot(fpr, tpr, color=col, lw=2, label=f"{cls} (AUC={auc:.3f})")
    ax.plot([0,1],[0,1],"k--")
    ax.set(xlim=[0,1], ylim=[0,1.05], xlabel="FPR", ylabel="TPR", title=title)
    ax.legend(loc="lower right", fontsize=8)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    return aucs


def plot_comparison(img_acc, adj_acc, img_acc_meta, adj_acc_meta, out_path):
    labels = ["All samples\n(image only)", "All samples\n(+ metadata adj.)",
              "Samples WITH\nmetadata (image)", "Samples WITH\nmetadata (adj.)"]
    values = [img_acc, adj_acc, img_acc_meta, adj_acc_meta]
    colors = ["#6366f1", "#22c55e", "#f59e0b", "#0ea5e9"]
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(labels, [v*100 for v in values], color=colors, width=0.5)
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                f"{val*100:.1f}%", ha="center", va="bottom", fontsize=11)
    ax.set(ylabel="Accuracy (%)", ylim=[0, 105],
           title="Stage-1 (Image only) vs Stage-2 (Image + Metadata)")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_path",    required=True)
    parser.add_argument("--mapping_path",  required=True)
    parser.add_argument("--data_dir",      required=True)
    parser.add_argument("--adjuster_path", default="./output/metadata_adjuster.pt")
    parser.add_argument("--metadata_csv",  default=None)
    parser.add_argument("--split",         default="test", choices=["train","val","test"])
    parser.add_argument("--out_dir",       default="./output/evaluation")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(args.mapping_path) as f:
        mapping = json.load(f)
    if not next(iter(mapping.keys())).isdigit():
        idx_to_class = {v: k for k, v in mapping.items()}
    else:
        idx_to_class = {int(k): v for k, v in mapping.items()}
    class_names = [idx_to_class[i] for i in range(len(idx_to_class))]
    num_classes = len(class_names)

    image_model = load_image_model(args.model_path, num_classes)
    adjuster    = load_adjuster(args.adjuster_path, num_classes)
    if adjuster:
        print(f"Stage-2 adjuster loaded.")
    else:
        print("No Stage-2 adjuster — evaluating Stage-1 only.")

    metadata_map: dict = {}
    csv_path = args.metadata_csv or str(Path(args.data_dir) / args.split / "metadata.csv")
    if Path(csv_path).exists():
        metadata_map = load_metadata_csv(csv_path)
        print(f"Metadata: {len(metadata_map)} entries")

    loader = build_loader(args.data_dir, args.split, metadata_map)
    (labels, img_preds, adj_preds,
     img_probs, adj_probs, meta_mask) = run_inference(image_model, adjuster, loader)

    meta_coverage = meta_mask.mean() * 100
    print(f"\nMetadata coverage: {meta_coverage:.1f}%")

    img_acc = float((labels == img_preds).mean())
    adj_acc = float((labels == adj_preds).mean())
    img_acc_meta = adj_acc_meta = img_acc

    print(f"Stage-1 accuracy: {img_acc*100:.2f}%")
    print(f"Stage-2 accuracy: {adj_acc*100:.2f}%")

    if meta_mask.any():
        img_acc_meta = float((labels[meta_mask] == img_preds[meta_mask]).mean())
        adj_acc_meta = float((labels[meta_mask] == adj_preds[meta_mask]).mean())
        print(f"\nOn {meta_mask.sum()} samples WITH metadata:")
        print(f"  Stage-1: {img_acc_meta*100:.2f}%")
        print(f"  Stage-2: {adj_acc_meta*100:.2f}%  (Δ +{(adj_acc_meta-img_acc_meta)*100:.2f}%)")

    plot_comparison(img_acc, adj_acc, img_acc_meta, adj_acc_meta,
                    out_dir / "stage1_vs_stage2_accuracy.png")
    plot_cm(confusion_matrix(labels, img_preds), class_names,
            "Confusion Matrix — Stage-1", out_dir / "confusion_matrix_stage1.png")
    if adjuster and meta_mask.any():
        plot_cm(confusion_matrix(labels, adj_preds), class_names,
                "Confusion Matrix — Stage-2", out_dir / "confusion_matrix_stage2.png")

    aucs_img = plot_roc(labels, img_probs, class_names, "ROC — Stage-1",
                        out_dir / "roc_stage1.png")
    aucs_adj = plot_roc(labels, adj_probs, class_names, "ROC — Stage-2",
                        out_dir / "roc_stage2.png") if adjuster else aucs_img

    (out_dir / "classification_report_stage1.txt").write_text(
        classification_report(labels, img_preds, target_names=class_names, digits=4))
    if adjuster:
        (out_dir / "classification_report_stage2.txt").write_text(
            classification_report(labels, adj_preds, target_names=class_names, digits=4))

    summary = {
        "split": args.split,
        "metadata_coverage_pct": round(meta_coverage, 1),
        "stage1": {"overall_accuracy": round(img_acc, 4),
                   "macro_auc": round(float(np.mean(list(aucs_img.values()))), 4),
                   "auc_per_class": aucs_img},
        "stage2": {"overall_accuracy": round(adj_acc, 4),
                   "macro_auc": round(float(np.mean(list(aucs_adj.values()))), 4),
                   "auc_per_class": aucs_adj,
                   "accuracy_on_meta_samples": round(adj_acc_meta, 4),
                   "stage1_accuracy_on_meta": round(img_acc_meta, 4),
                   "improvement_on_meta_pct": round((adj_acc_meta-img_acc_meta)*100, 2)},
    }
    (out_dir / "evaluation_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\n✅ Evaluation complete → {out_dir}")


if __name__ == "__main__":
    main()