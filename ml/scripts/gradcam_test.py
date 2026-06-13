"""
gradcam_test.py — Visually test GradCAM heatmap generation on sample images.
Saves overlay PNGs so you can inspect saliency before deploying.

Usage:
    # Single image
    python scripts/gradcam_test.py \
        --model_path  ./output/eye_disease_model.pt \
        --mapping_path ./output/class_mapping.json \
        --img_path    ./data/raw_data/Glaucoma/sample.jpg

    # Entire folder (saves one overlay per image)
    python scripts/gradcam_test.py \
        --model_path  ./output/eye_disease_model.pt \
        --mapping_path ./output/class_mapping.json \
        --img_dir     ./data/raw_data/Diabetic_Retinopathy \
        --max_images  10
"""

import argparse
import json
from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import models, transforms

# ─── Constants ────────────────────────────────────────────────────────────────
IMG_SIZE      = 224
DEVICE        = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}


# ─── Model loader ─────────────────────────────────────────────────────────────

def load_model(model_path: str, num_classes: int) -> nn.Module:
    model = models.swin_t(weights=None)
    in_features = model.head.in_features
    model.head = nn.Linear(in_features, num_classes)
    state = torch.load(model_path, map_location=DEVICE)
    model.load_state_dict(state)
    model.to(DEVICE)
    model.eval()
    return model


# ─── GradCAM for Swin Transformer ─────────────────────────────────────────────

class SwinGradCAM:
    """
    GradCAM adapted for Swin Transformer.
    Target layer: the last stage's LayerNorm (norm3 or norm).
    For torchvision Swin-T the patch-merging output before the head
    is accessible via model.norm.
    """

    def __init__(self, model: nn.Module):
        self.model    = model
        self.gradients = None
        self.activations = None
        self._register_hooks()

    def _register_hooks(self):
        # Use the final LayerNorm — output shape: (B, H*W, C)
        target_layer = self.model.norm

        def forward_hook(module, input, output):
            self.activations = output.detach()

        def backward_hook(module, grad_input, grad_output):
            self.gradients = grad_output[0].detach()

        target_layer.register_forward_hook(forward_hook)
        target_layer.register_full_backward_hook(backward_hook)

    def generate(self, input_tensor: torch.Tensor, target_class: int = None):
        """
        Returns a (H, W) numpy heatmap in [0, 1].
        input_tensor: (1, 3, H, W)  on DEVICE
        """
        self.model.zero_grad()
        logits = self.model(input_tensor)

        if target_class is None:
            target_class = int(logits.argmax(dim=1).item())

        score = logits[0, target_class]
        score.backward()

        # activations / grads: (1, seq_len, channels)
        grads   = self.gradients[0]   # (seq_len, C)
        acts    = self.activations[0]  # (seq_len, C)

        # Global-average-pool over spatial tokens → weight per channel
        weights = grads.mean(dim=0)   # (C,)

        # Weighted sum over channels → (seq_len,)
        cam = (acts * weights).sum(dim=-1)  # (seq_len,)
        cam = torch.relu(cam)

        # Reshape to 2-D feature map
        seq_len = cam.shape[0]
        side    = int(seq_len ** 0.5)
        if side * side != seq_len:
            # Non-square: try 7x7 for 224 input
            side = 7
        cam_2d = cam[:side * side].reshape(side, side).cpu().numpy()

        # Normalise
        cam_2d = cv2.resize(cam_2d, (IMG_SIZE, IMG_SIZE))
        if cam_2d.max() > cam_2d.min():
            cam_2d = (cam_2d - cam_2d.min()) / (cam_2d.max() - cam_2d.min())
        return cam_2d, target_class, logits


# ─── Image helpers ────────────────────────────────────────────────────────────

_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
])


def preprocess(img_path: str):
    img_pil = Image.open(img_path).convert("RGB")
    tensor  = _transform(img_pil).unsqueeze(0).to(DEVICE)
    # Also get original RGB for overlay
    img_np  = np.array(img_pil.resize((IMG_SIZE, IMG_SIZE)))
    return tensor, img_np


def overlay_heatmap(img_np: np.ndarray, heatmap: np.ndarray, alpha: float = 0.45):
    """Blend jet heatmap onto the original image."""
    heat_color = cv2.applyColorMap(
        (heatmap * 255).astype(np.uint8), cv2.COLORMAP_JET
    )
    heat_color = cv2.cvtColor(heat_color, cv2.COLOR_BGR2RGB)
    blended    = cv2.addWeighted(img_np, 1 - alpha, heat_color, alpha, 0)
    return blended


def save_overlay(img_path: str, heatmap: np.ndarray, img_np: np.ndarray,
                 class_name: str, confidence: float, out_path: str):
    overlay = overlay_heatmap(img_np, heatmap)

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    axes[0].imshow(img_np)
    axes[0].set_title("Original Image", fontsize=12)
    axes[0].axis("off")

    axes[1].imshow(heatmap, cmap="jet")
    axes[1].set_title("GradCAM Heatmap", fontsize=12)
    axes[1].axis("off")
    plt.colorbar(
        plt.cm.ScalarMappable(cmap="jet"),
        ax=axes[1], fraction=0.046, pad=0.04
    )

    axes[2].imshow(overlay)
    axes[2].set_title(
        f"Overlay\nPredicted: {class_name}\nConfidence: {confidence:.1%}",
        fontsize=11,
    )
    axes[2].axis("off")

    fig.suptitle(Path(img_path).name, fontsize=10, y=1.02)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()


# ─── Core logic ───────────────────────────────────────────────────────────────

def process_image(img_path: str, gradcam: SwinGradCAM,
                  idx_to_class: dict, out_dir: Path):
    img_path_obj = Path(img_path)
    print(f"  Processing: {img_path_obj.name}")

    tensor, img_np = preprocess(img_path)
    heatmap, pred_idx, logits = gradcam.generate(tensor)

    probs      = torch.softmax(logits, dim=1)[0]
    confidence = float(probs[pred_idx].item())
    class_name = idx_to_class.get(pred_idx, str(pred_idx))

    out_name = img_path_obj.stem + "_gradcam.png"
    out_path = str(out_dir / out_name)
    save_overlay(img_path, heatmap, img_np, class_name, confidence, out_path)

    print(f"    → {class_name} ({confidence:.1%})  saved: {out_path}")
    return {"image": img_path_obj.name, "predicted": class_name, "confidence": confidence}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test GradCAM on sample fundus images")
    parser.add_argument("--model_path",   required=True)
    parser.add_argument("--mapping_path", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--img_path",  help="Single image path")
    group.add_argument("--img_dir",   help="Folder of images")
    parser.add_argument("--max_images", type=int, default=20,
                        help="Max images when using --img_dir")
    parser.add_argument("--out_dir", default="./output/gradcam_results")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load class mapping
    with open(args.mapping_path) as f:
        mapping = json.load(f)
    if isinstance(next(iter(mapping.keys())), str) and not next(iter(mapping.keys())).isdigit():
        idx_to_class = {v: k for k, v in mapping.items()}
    else:
        idx_to_class = {int(k): v for k, v in mapping.items()}
    num_classes = len(idx_to_class)

    print(f"\nLoading model ({num_classes} classes) from {args.model_path} …")
    model  = load_model(args.model_path, num_classes)
    gradcam = SwinGradCAM(model)

    # Collect images
    if args.img_path:
        img_paths = [args.img_path]
    else:
        img_dir   = Path(args.img_dir)
        img_paths = sorted(
            p for p in img_dir.iterdir()
            if p.suffix.lower() in SUPPORTED_EXT
        )[:args.max_images]

    print(f"Processing {len(img_paths)} image(s) …\n")
    results = []
    for ip in img_paths:
        try:
            r = process_image(str(ip), gradcam, idx_to_class, out_dir)
            results.append(r)
        except Exception as e:
            print(f"  ERROR on {ip}: {e}")

    print(f"\n✅ Done! {len(results)} overlays saved to: {out_dir}")

    # Quick summary
    from collections import Counter
    counts = Counter(r["predicted"] for r in results)
    print("\nPrediction distribution:")
    for cls, cnt in counts.most_common():
        print(f"  {cls}: {cnt}")


if __name__ == "__main__":
    main()