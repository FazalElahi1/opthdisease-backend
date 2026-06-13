
"""
export_mobile.py — Export the trained eye disease model to mobile formats.

Produces:
  mobile_model/eye_disease_model.ptl          TorchScript  → React Native (.ptl)
  mobile_model/eye_disease_model.onnx         ONNX         → cross-platform
  mobile_model/eye_disease_model_quantized.pt Dynamic-quantized (4× smaller)
  mobile_model/class_mapping.json             Copy of class map
  mobile_model/offline_recommendations.json   Copy of recommendations

Usage:
    python scripts/export_mobile.py \
        --model_path   ./output/eye_disease_model.pt \
        --mapping_path ./output/class_mapping.json \
        --out_dir      ./mobile_model
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import torch
import torch.nn as nn
from torchvision import models

DEVICE   = torch.device("cpu")   # mobile export always on CPU
IMG_SIZE = 224


# ─── Load model ───────────────────────────────────────────────────────────────

def load_model(model_path: str, num_classes: int) -> nn.Module:
    model = models.swin_t(weights=None)
    model.head = nn.Linear(model.head.in_features, num_classes)
    state = torch.load(model_path, map_location=DEVICE)
    model.load_state_dict(state)
    model.to(DEVICE)
    model.eval()
    return model


# ─── Export helpers ───────────────────────────────────────────────────────────

def export_torchscript(model: nn.Module, out_path: Path):
    """TorchScript via torch.jit.trace — required for React Native PyTorch Core."""
    dummy = torch.zeros(1, 3, IMG_SIZE, IMG_SIZE)
    with torch.no_grad():
        traced = torch.jit.trace(model, dummy)
    # _save_for_lite_interpreter produces the .ptl format
    traced._save_for_lite_interpreter(str(out_path))
    size_mb = out_path.stat().st_size / 1e6
    print(f"  TorchScript (.ptl) → {out_path}  [{size_mb:.1f} MB]")


def export_onnx(model: nn.Module, out_path: Path):
    """ONNX export for cross-platform deployment."""
    dummy = torch.zeros(1, 3, IMG_SIZE, IMG_SIZE)
    torch.onnx.export(
        model,
        dummy,
        str(out_path),
        export_params=True,
        opset_version=14,
        do_constant_folding=True,
        input_names=["fundus_image"],
        output_names=["disease_logits"],
        dynamic_axes={
            "fundus_image":   {0: "batch_size"},
            "disease_logits": {0: "batch_size"},
        },
    )
    size_mb = out_path.stat().st_size / 1e6
    print(f"  ONNX               → {out_path}  [{size_mb:.1f} MB]")


def export_executorch(model: nn.Module, out_path: Path):
    """Export to ExecuTorch .pte format — required for react-native-executorch."""
    try:
        from torch.export import export as torch_export
        from executorch.exir import to_edge_transform_and_lower
    except ImportError:
        print("  ExecuTorch skipped — install with: pip install executorch")
        return

    dummy = torch.zeros(1, 3, IMG_SIZE, IMG_SIZE)
    with torch.no_grad():
        exported = torch_export(model, (dummy,))

    edge = to_edge_transform_and_lower(exported)
    et_program = edge.to_executorch()
    with open(str(out_path), "wb") as f:
        f.write(et_program.buffer)
    size_mb = out_path.stat().st_size / 1e6
    print(f"  ExecuTorch (.pte)   → {out_path}  [{size_mb:.1f} MB]")


def export_quantized(model: nn.Module, out_path: Path):
    """
    Dynamic post-training quantization (weights int8, activations float32).
    ~4× smaller, ~2× faster on CPU, negligible accuracy drop.
    """
    quantized = torch.quantization.quantize_dynamic(
        model,
        {nn.Linear},
        dtype=torch.qint8,
    )
    torch.save(quantized.state_dict(), str(out_path))
    size_mb = out_path.stat().st_size / 1e6
    print(f"  Quantized (.pt)    → {out_path}  [{size_mb:.1f} MB]")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export eye disease model to mobile formats")
    parser.add_argument("--model_path",   required=True,
                        help="Path to eye_disease_model.pt")
    parser.add_argument("--mapping_path", required=True,
                        help="Path to class_mapping.json")
    parser.add_argument("--out_dir",      default="./mobile_model")
    parser.add_argument("--skip_onnx",        action="store_true",
                        help="Skip ONNX export (requires onnx package)")
    parser.add_argument("--skip_executorch",  action="store_true",
                        help="Skip ExecuTorch export (requires executorch package)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load class mapping
    with open(args.mapping_path) as f:
        mapping = json.load(f)
    num_classes = len(mapping)
    print(f"\nLoading model ({num_classes} classes) from {args.model_path} …")

    model = load_model(args.model_path, num_classes)

    print("\nExporting:")

    # 1. TorchScript (.ptl)
    try:
        export_torchscript(model, out_dir / "eye_disease_model.ptl")
    except Exception as e:
        print(f"  ⚠️  TorchScript export failed: {e}")

    # 2. ONNX
    if not args.skip_onnx:
        try:
            import onnx  # noqa: F401
            export_onnx(model, out_dir / "eye_disease_model.onnx")
        except ImportError:
            print("  ONNX skipped — install with: pip install onnx onnxruntime")
        except Exception as e:
            print(f"  ⚠️  ONNX export failed: {e}")

    # 3. ExecuTorch (.pte) — for react-native-executorch
    if not args.skip_executorch:
        try:
            export_executorch(model, out_dir / "eye_disease_model.pte")
        except Exception as e:
            print(f"  ⚠️  ExecuTorch export failed: {e}")

    # 4. Quantized
    try:
        export_quantized(model, out_dir / "eye_disease_model_quantized.pt")
    except Exception as e:
        print(f"  ⚠️  Quantization failed: {e}")

    # 4. Copy class mapping
    dst_mapping = out_dir / "class_mapping.json"
    shutil.copy2(args.mapping_path, dst_mapping)
    print(f"  class_mapping.json → {dst_mapping}")

    # 5. Copy offline recommendations if it exists
    recs_src = Path(__file__).resolve().parent.parent / "assets" / "offline_recommendations.json"
    if recs_src.exists():
        dst_recs = out_dir / "offline_recommendations.json"
        shutil.copy2(recs_src, dst_recs)
        print(f"  offline_recommendations.json → {dst_recs}")
    else:
        print(f"  ⚠️  offline_recommendations.json not found at {recs_src} — copy manually.")

    print(f"\n✅ Export complete → {out_dir}")
    print("\nNext steps:")
    print("  Copy to React Native assets:")
    print(f"    cp {out_dir}/eye_disease_model.pte          EyeDiseaseApp/src/assets/")
    print(f"    cp {out_dir}/class_mapping.json             EyeDiseaseApp/src/assets/")
    print(f"    cp {out_dir}/offline_recommendations.json  EyeDiseaseApp/src/assets/")
    print("\n  Copy weights to backend:")
    print(f"    cp {args.model_path}  OpthdiseaseAI/output/eye_disease_model.pt")
    print(f"    cp {args.mapping_path} OpthdiseaseAI/output/class_mapping.json")


if __name__ == "__main__":
    main()