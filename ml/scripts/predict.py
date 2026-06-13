"""
predict.py — Two-stage inference pipeline.
  Stage 1: Swin-T fundus image model → class probabilities
  Stage 2: MetadataAdjuster MLP      → refined probabilities (if metadata provided)
  GradCAM: heatmap generation        (server-side only)

Usage:
    python scripts/predict.py \
        --model_path     ./output/eye_disease_model.pt \
        --adjuster_path  ./output/metadata_adjuster.pt \
        --mapping_path   ./output/class_mapping.json \
        --img_path       ./sample.jpg \
        --age 54 --gender male --blood_pressure 150/95 \
        --fasting_glucose 210 --hba1c 8.5 \
        --smoking current --family_history diabetes,hypertension \
        --symptom_text "floaters, blurred vision"

    # Image-only (no metadata):
    python scripts/predict.py \
        --model_path ./output/eye_disease_model.pt \
        --mapping_path ./output/class_mapping.json \
        --img_path ./sample.jpg
"""

from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import models, transforms

try:
    from metadata_encoder import encode_metadata, METADATA_DIM, is_empty
    from metadata_adjuster import MetadataAdjuster, load_adjuster, adjust_probs
    from camera_preprocessor import camera_to_fundus_like, is_likely_fundus
except ImportError:
    from ml.scripts.metadata_encoder import encode_metadata, METADATA_DIM, is_empty
    from ml.scripts.metadata_adjuster import MetadataAdjuster, load_adjuster, adjust_probs
    from ml.scripts.camera_preprocessor import camera_to_fundus_like, is_likely_fundus

# ─── Constants ────────────────────────────────────────────────────────────────
IMG_SIZE      = 224
DEVICE        = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]
CONFIDENCE_THRESHOLD = 0.50

_TRANSFORM = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
])

# ─── Risk thresholds ──────────────────────────────────────────────────────────
def _risk_level(class_name: str, confidence: float) -> str:
    if class_name == "Normal":
        if confidence >= 0.80: return "low"
        if confidence >= 0.55: return "moderate"
        return "high"
    else:
        if confidence >= 0.75: return "high"
        if confidence >= 0.50: return "moderate"
        return "low"


# ════════════════════════════════════════════════════════════════════════════════
#  STAGE-1: IMAGE MODEL
# ════════════════════════════════════════════════════════════════════════════════

def load_image_model(model_path: str, num_classes: int):
    """Returns (model, heatmap_mode, needs_metadata).

    Handles three kinds of saved models:
      * state-dict (plain Swin-T)          → ('gradcam',  needs_metadata=False)
      * TorchScript, image-only forward    → ('saliency', needs_metadata=False)
      * TorchScript, multimodal forward    → ('saliency', needs_metadata=True)
        i.e. forward(images, metadata) — a self-contained model that consumes
        patient metadata internally (e.g. MultimodalODIRModel).

    heatmap_mode:
      'gradcam'  — Swin-T GradCAM via forward/backward hooks (state-dict only;
                   hooks are NOT supported on TorchScript modules).
      'saliency' — input-gradient saliency (works on any model, incl. scripted).
    """
    try:
        raw = torch.load(model_path, map_location=DEVICE, weights_only=False)
    except TypeError:
        raw = torch.load(model_path, map_location=DEVICE)

    if isinstance(raw, dict):
        model = models.swin_t(weights=None)
        model.head = nn.Linear(model.head.in_features, num_classes)
        model.load_state_dict(raw)
        model.to(DEVICE)
        model.eval()
        return model, "gradcam", False
    else:
        # TorchScript or full-model save — use as-is. GradCAM hooks are not
        # supported on ScriptModules, so we fall back to input-gradient saliency.
        raw.to(DEVICE)
        raw.eval()
        return raw, "saliency", _forward_needs_metadata(raw)


def _forward_needs_metadata(model: nn.Module) -> bool:
    """True if the model's forward expects a second (metadata) tensor argument."""
    # TorchScript modules expose a typed schema we can inspect directly.
    try:
        args = [a for a in model.forward.schema.arguments if a.name != "self"]
        if args:
            return len(args) >= 2
    except Exception:
        pass
    # Fallback: probe with an image-only call. If it fails, assume metadata is needed.
    try:
        with torch.no_grad():
            model(torch.zeros(1, 3, IMG_SIZE, IMG_SIZE, device=DEVICE))
        return False
    except Exception:
        return True


def _detect_meta_dim(model: nn.Module) -> int:
    """Infer the metadata feature length from the model's metadata branch.

    Returns the in_features of the first metadata linear layer (e.g. 2 for the
    [age, sex] MultimodalODIRModel). Defaults to 2 if it can't be determined.
    """
    cands: list[tuple[str, int]] = []
    try:
        for name, p in model.named_parameters():
            if "metadata" in name.lower() and name.endswith(".weight") and p.dim() == 2:
                cands.append((name, int(p.shape[1])))
    except Exception:
        pass
    if not cands:
        return 2
    cands.sort(key=lambda x: x[0])   # 'metadata_branch.0.weight' sorts first → input layer
    return cands[0][1]


def _build_meta_tensor(metadata: Optional[dict], meta_dim: int) -> torch.Tensor:
    """Encode patient metadata into the fixed-length tensor the model expects.

    For the MultimodalODIRModel this is 2 features: [age_normalised, sex].
      age_normalised : age / 100   (≈0.5 ≈ 50yo when unknown)
      sex            : male=1.0, female=0.0, unknown=0.5
    Predictions are dominated by the image, so neutral defaults are safe.
    """
    vec = [0.5] * meta_dim
    if metadata:
        age = metadata.get("age")
        try:
            age = float(age) if age is not None and str(age).strip() != "" else None
        except (TypeError, ValueError):
            age = None
        if age is not None and meta_dim >= 1:
            vec[0] = max(0.0, min(age / 100.0, 1.2))
        if meta_dim >= 2:
            g = str(metadata.get("gender", "")).strip().lower()
            if g.startswith("m"):
                vec[1] = 1.0
            elif g.startswith("f"):
                vec[1] = 0.0
    return torch.tensor([vec], dtype=torch.float32, device=DEVICE)


def preprocess_image(image_bytes: bytes) -> tuple[torch.Tensor, np.ndarray]:
    """Returns (input_tensor (1,3,H,W), original_rgb_np (H,W,3))."""
    img_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_np  = np.array(img_pil.resize((IMG_SIZE, IMG_SIZE)))
    tensor  = _TRANSFORM(img_pil).unsqueeze(0).to(DEVICE)
    return tensor, img_np


# ════════════════════════════════════════════════════════════════════════════════
#  GRADCAM (Swin-T)
# ════════════════════════════════════════════════════════════════════════════════

class SwinGradCAM:
    def __init__(self, model: nn.Module):
        self.model = model
        self.gradients = None
        self.activations = None
        model.norm.register_forward_hook(
            lambda m, i, o: setattr(self, 'activations', o.detach()))
        model.norm.register_full_backward_hook(
            lambda m, gi, go: setattr(self, 'gradients', go[0].detach()))

    def generate(self, tensor: torch.Tensor,
                 target_class: int = None) -> tuple[np.ndarray, int]:
        self.model.zero_grad()
        logits = self.model(tensor)
        if target_class is None:
            target_class = int(logits.argmax(1).item())
        logits[0, target_class].backward()

        grads = self.gradients[0]    # (seq_len, C)
        acts  = self.activations[0]  # (seq_len, C)
        weights = grads.mean(dim=0)
        cam = torch.relu((acts * weights).sum(dim=-1))

        side = int(cam.shape[0] ** 0.5)
        cam_2d = cam[:side*side].reshape(side, side).cpu().numpy()
        cam_2d = cv2.resize(cam_2d, (IMG_SIZE, IMG_SIZE))
        if cam_2d.max() > cam_2d.min():
            cam_2d = (cam_2d - cam_2d.min()) / (cam_2d.max() - cam_2d.min())
        return cam_2d, target_class


def heatmap_to_base64(heatmap: np.ndarray, img_np: np.ndarray) -> str:
    heat_bgr    = cv2.applyColorMap((heatmap * 255).astype(np.uint8), cv2.COLORMAP_JET)
    heat_rgb    = cv2.cvtColor(heat_bgr, cv2.COLOR_BGR2RGB)
    blended     = cv2.addWeighted(img_np, 0.55, heat_rgb, 0.45, 0)
    pil_img     = Image.fromarray(blended)
    buf         = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ════════════════════════════════════════════════════════════════════════════════
#  INPUT-GRADIENT SALIENCY  (model-agnostic heatmap; used for TorchScript models)
# ════════════════════════════════════════════════════════════════════════════════

def input_gradient_heatmap(
    model:        nn.Module,
    tensor:       torch.Tensor,
    meta_tensor:  Optional[torch.Tensor] = None,
    target_class: int = None,
) -> tuple[np.ndarray, int]:
    """Compute a smoothed input-gradient saliency map for the predicted class.

    Works on ANY model (including TorchScript modules, where forward/backward
    hooks — and therefore GradCAM — are not supported). One forward + one
    backward pass w.r.t. the input pixels, reduced over channels and blurred to
    look like an attention map. Returns (heatmap[H,W] in [0,1], target_class).
    """
    inp = tensor.clone().detach().requires_grad_(True)
    out = model(inp, meta_tensor) if meta_tensor is not None else model(inp)
    if target_class is None:
        target_class = int(out.argmax(1).item())
    out[0, target_class].backward()

    grad = inp.grad.detach()[0].abs().amax(0).cpu().numpy()   # (H, W)
    grad = cv2.GaussianBlur(grad, (0, 0), sigmaX=7)
    if grad.max() > grad.min():
        grad = (grad - grad.min()) / (grad.max() - grad.min())
    return grad.astype(np.float32), target_class


# ════════════════════════════════════════════════════════════════════════════════
#  MAIN PREDICT FUNCTION
# ════════════════════════════════════════════════════════════════════════════════

def predict(
    image_bytes:    bytes,
    image_model:    nn.Module,
    idx_to_class:   dict[int, str],
    adjuster:       Optional[MetadataAdjuster] = None,
    metadata:       Optional[dict[str, Any]]   = None,
    generate_gradcam: bool = True,
    allow_camera_image: bool = True,
    camera_preprocess: bool = False,    # convert phone-camera photos to fundus-like
    model_mode:     str  = "gradcam",   # 'gradcam' | 'saliency'
    needs_metadata: bool = False,       # model.forward(images, metadata)
    meta_dim:       int  = 2,
) -> dict[str, Any]:
    """
    Full two-stage prediction.

    Parameters
    ----------
    image_bytes        : raw image file bytes
    image_model        : loaded Stage-1 Swin-T (frozen)
    idx_to_class       : {0: 'Normal', 1: 'AMD', ...}
    adjuster           : Stage-2 MLP (optional; None = image-only mode)
    metadata           : dict with optional patient fields (see metadata_schema.json)
    generate_gradcam   : whether to produce GradCAM overlay (slow, server-side only)
    allow_camera_image : if True, auto-convert mobile camera images to fundus-like
                         before inference. Confidence will be lower; result will
                         include a warning. If False, raises ValueError on non-fundus.

    Returns
    -------
    dict with all prediction fields
    """
    num_classes = len(idx_to_class)

    # ── Domain detection & preprocessing ──
    # The model is trained on fundus photographs, so by default we feed the image
    # straight to the model. The camera→fundus conversion is OFF by default: it is
    # destructive (crop/mask/recolour) and the heuristic that triggers it was
    # mislabelling real fundus images as camera photos, which collapsed accuracy.
    camera_image_preprocessed = False
    image_is_fundus = True

    if camera_preprocess:
        image_is_fundus = is_likely_fundus(image_bytes)
        if not image_is_fundus:
            if not allow_camera_image:
                raise ValueError(
                    "Image does not appear to be a fundus photograph. "
                    "Please upload a fundus image taken with an ophthalmoscope."
                )
            image_bytes = camera_to_fundus_like(image_bytes)
            camera_image_preprocessed = True

    # ── Preprocess ──
    tensor, img_np = preprocess_image(image_bytes)

    # Self-contained multimodal models consume patient metadata internally via a
    # fixed-length tensor passed to forward(images, metadata).
    meta_tensor = _build_meta_tensor(metadata, meta_dim) if needs_metadata else None

    def _run_logits(t: torch.Tensor) -> torch.Tensor:
        return image_model(t, meta_tensor) if needs_metadata else image_model(t)

    # ── Stage-1: image inference (+ optional heatmap) ──
    heatmap = None
    if generate_gradcam and model_mode == "gradcam":
        # Swin-T GradCAM (state-dict models only).
        heatmap, _ = SwinGradCAM(image_model).generate(tensor)
        with torch.no_grad():
            image_probs_np = torch.softmax(_run_logits(tensor), dim=1)[0].cpu().numpy()
    elif generate_gradcam and model_mode == "saliency":
        # Input-gradient saliency (works on TorchScript / multimodal models).
        heatmap, _ = input_gradient_heatmap(image_model, tensor, meta_tensor)
        with torch.no_grad():
            image_probs_np = torch.softmax(_run_logits(tensor), dim=1)[0].cpu().numpy()
    else:
        with torch.no_grad():
            image_probs_np = torch.softmax(_run_logits(tensor), dim=1)[0].cpu().numpy()

    # ── Encode metadata (for the optional external Stage-2 adjuster) ──
    metadata_vec = encode_metadata(metadata)
    metadata_provided = not is_empty(metadata_vec)

    # ── Stage-2: metadata adjustment ──
    # Skipped when the model already consumes metadata internally (needs_metadata).
    if metadata_provided and adjuster is not None and not needs_metadata:
        final_probs_np = adjust_probs(adjuster, image_probs_np, metadata_vec)
        stage2_applied = True
    else:
        final_probs_np = image_probs_np
        stage2_applied = False

    # ── Build result ──
    pred_idx   = int(np.argmax(final_probs_np))
    confidence = float(final_probs_np[pred_idx])
    class_name = idx_to_class[pred_idx]
    risk       = _risk_level(class_name, confidence)

    all_probs = {
        idx_to_class[i]: round(float(p), 6)
        for i, p in enumerate(final_probs_np)
    }
    # Also include Stage-1 probs for transparency
    image_only_probs = {
        idx_to_class[i]: round(float(p), 6)
        for i, p in enumerate(image_probs_np)
    }

    result: dict[str, Any] = {
        "predicted_class":      class_name,
        "confidence":           round(confidence, 6),
        "risk_level":           risk,
        "uncertain":            confidence < CONFIDENCE_THRESHOLD,
        "all_probabilities":    all_probs,
        "image_only_probs":     image_only_probs,
        "metadata_provided":    metadata_provided,
        "stage2_applied":       stage2_applied,
        "metadata_fields_used": _summarise_fields(metadata) if metadata else {},
        "image_type":           "fundus" if image_is_fundus else "camera",
        "camera_preprocessed":  camera_image_preprocessed,
    }

    if heatmap is not None:
        result["gradcam_base64"] = heatmap_to_base64(heatmap, img_np)

    if camera_image_preprocessed:
        result["camera_image_warning"] = (
            "This image appears to be a mobile camera photo, not a fundus photograph. "
            "It has been preprocessed to approximate fundus appearance, but the model "
            "was trained on retinal images. Predictions may be inaccurate. "
            "For reliable results, this model's classification head should be "
            "fine-tuned on anterior-segment images."
        )

    if not metadata_provided:
        result["metadata_note"] = (
            "No patient metadata provided. Result is based on image only. "
            "Providing age, blood pressure, glucose, and symptoms can improve accuracy."
        )

    return result


def _summarise_fields(meta: dict) -> dict:
    """Returns which metadata fields were actually provided (non-empty)."""
    if not meta:
        return {}
    return {k: True for k, v in meta.items()
            if v is not None and str(v).strip() != ""}


# ════════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Two-stage eye disease prediction")
    parser.add_argument("--model_path",    required=True)
    parser.add_argument("--mapping_path",  required=True)
    parser.add_argument("--adjuster_path", default="./output/metadata_adjuster.pt")
    parser.add_argument("--img_path",      required=True)
    # Metadata args (all optional)
    parser.add_argument("--age",              type=float, default=None)
    parser.add_argument("--gender",           default=None)
    parser.add_argument("--blood_pressure",   default=None)
    parser.add_argument("--fasting_glucose",  type=float, default=None)
    parser.add_argument("--hba1c",            type=float, default=None)
    parser.add_argument("--smoking",          default=None)
    parser.add_argument("--family_history",   default=None,
                        help="Comma-separated e.g. diabetes,glaucoma")
    parser.add_argument("--symptom_text",     default=None)
    parser.add_argument("--no_gradcam",       action="store_true")
    args = parser.parse_args()

    # Load class mapping
    with open(args.mapping_path) as f:
        mapping = json.load(f)
    if not next(iter(mapping.keys())).isdigit():
        idx_to_class = {v: k for k, v in mapping.items()}
    else:
        idx_to_class = {int(k): v for k, v in mapping.items()}
    num_classes = len(idx_to_class)

    image_model, heatmap_mode, needs_meta = load_image_model(args.model_path, num_classes)
    meta_dim    = _detect_meta_dim(image_model) if needs_meta else 2
    adjuster    = load_adjuster(args.adjuster_path, num_classes)
    if adjuster:
        print(f"Stage-2 adjuster loaded from {args.adjuster_path}")
    else:
        print("No Stage-2 adjuster found — running image-only mode.")

    # Build metadata dict from CLI args
    metadata: dict[str, Any] = {}
    if args.age is not None:            metadata["age"]              = args.age
    if args.gender:                     metadata["gender"]           = args.gender
    if args.blood_pressure:             metadata["blood_pressure"]   = args.blood_pressure
    if args.fasting_glucose is not None:metadata["fasting_glucose"]  = args.fasting_glucose
    if args.hba1c is not None:          metadata["hba1c"]            = args.hba1c
    if args.smoking:                    metadata["smoking"]          = args.smoking
    if args.family_history:             metadata["family_history"]   = args.family_history
    if args.symptom_text:               metadata["symptom_text"]     = args.symptom_text

    image_bytes = Path(args.img_path).read_bytes()

    result = predict(
        image_bytes=image_bytes,
        image_model=image_model,
        idx_to_class=idx_to_class,
        adjuster=adjuster,
        metadata=metadata or None,
        generate_gradcam=not args.no_gradcam,
        model_mode=heatmap_mode,
        needs_metadata=needs_meta,
        meta_dim=meta_dim,
    )

    # Print summary (exclude base64)
    summary = {k: v for k, v in result.items() if k != "gradcam_base64"}
    print(json.dumps(summary, indent=2))

    if "gradcam_base64" in result:
        out = Path(args.img_path).stem + "_gradcam.jpg"
        import base64
        with open(out, "wb") as f:
            f.write(base64.b64decode(result["gradcam_base64"]))
        print(f"\nGradCAM overlay saved → {out}")


if __name__ == "__main__":
    main()


# ════════════════════════════════════════════════════════════════════════════════
#  EyeDiseasePredictor — high-level class used by routers/xai.py
# ════════════════════════════════════════════════════════════════════════════════

_CLASS_RECOMMENDATIONS: dict[str, dict] = {
    "Normal": {
        "summary":   "No eye disease detected.",
        "actions":   ["Annual routine eye examination recommended.",
                      "Maintain healthy lifestyle and UV protection."],
        "followUp":  "12 months",
    },
    "Diabetes": {
        "summary":   "Diabetic Retinopathy detected.",
        "actions":   ["Urgent ophthalmology referral.",
                      "Strict blood glucose control (target HbA1c < 7%).",
                      "Blood pressure management below 130/80 mmHg."],
        "followUp":  "3 months",
    },
    "Glaucoma": {
        "summary":   "Glaucoma detected.",
        "actions":   ["Immediate specialist referral for intraocular pressure measurement.",
                      "Visual field testing and OCT of optic nerve recommended.",
                      "Do not delay — untreated glaucoma causes irreversible vision loss."],
        "followUp":  "1 month",
    },
    "Cataract": {
        "summary":   "Cataract detected.",
        "actions":   ["Ophthalmology referral for surgical evaluation.",
                      "Avoid driving at night until reviewed by a specialist."],
        "followUp":  "2 months",
    },
    "AMD": {
        "summary":   "Age-related Macular Degeneration (AMD) detected.",
        "actions":   ["Urgent ophthalmology referral.",
                      "Anti-VEGF treatment evaluation may be required.",
                      "Daily Amsler grid monitoring at home."],
        "followUp":  "1 month",
    },
    "Hypertension": {
        "summary":   "Hypertensive Retinopathy detected.",
        "actions":   ["Blood pressure must be controlled urgently.",
                      "Cardiology and ophthalmology co-management recommended.",
                      "Regular monitoring every 3 months."],
        "followUp":  "3 months",
    },
    "Myopia": {
        "summary":   "Pathological Myopia detected.",
        "actions":   ["Annual retinal examination for complications (tears, detachment).",
                      "Avoid high-impact activities with risk of retinal detachment.",
                      "Discuss myopia control options with your ophthalmologist."],
        "followUp":  "6 months",
    },
    "Others": {
        "summary":   "Unspecified ocular condition detected.",
        "actions":   ["Ophthalmology referral for detailed examination.",
                      "Further diagnostic tests may be required."],
        "followUp":  "4 weeks",
    },
}

_CLASS_REGIONS: dict[str, list[str]] = {
    "Normal":       ["optic disc", "macula", "peripheral retina"],
    "Diabetes":     ["macula", "optic disc", "peripheral retina", "retinal vessels"],
    "Glaucoma":     ["optic disc", "optic cup", "peripheral retina"],
    "Cataract":     ["lens", "anterior segment"],
    "AMD":          ["macula", "fovea"],
    "Hypertension": ["retinal vessels", "optic disc", "macula"],
    "Myopia":       ["peripheral retina", "optic disc", "posterior pole"],
    "Others":       ["retina"],
}

_DEFAULT_ADJUSTER_PATH = "./output/metadata_adjuster.pt"


def _parse_sugar(value) -> Optional[float]:
    """Parse blood sugar string like '120', '120 mg/dL' → float."""
    if value is None:
        return None
    import re
    m = re.search(r"[\d.]+", str(value))
    return float(m.group()) if m else None


class EyeDiseasePredictor:
    """
    High-level wrapper used by routers/xai.py.

    Loads the Swin-T image model and optional MetadataAdjuster once,
    then exposes .predict() with the interface xai.py expects.
    """

    def __init__(self, model_path: str, mapping_path: str,
                 adjuster_path: str = _DEFAULT_ADJUSTER_PATH):
        with open(mapping_path) as f:
            raw = json.load(f)
        if not next(iter(raw.keys())).isdigit():
            self.idx_to_class: dict[int, str] = {v: k for k, v in raw.items()}
        else:
            self.idx_to_class = {int(k): v for k, v in raw.items()}
        self.num_classes = len(self.idx_to_class)
        self.image_model, self._heatmap_mode, self._needs_metadata = load_image_model(
            model_path, self.num_classes
        )
        self._meta_dim = _detect_meta_dim(self.image_model) if self._needs_metadata else 2
        self.adjuster  = load_adjuster(adjuster_path, self.num_classes)

    def predict(
        self,
        image_input:    str,
        blood_pressure: Optional[str] = None,
        sugar_level:    Optional[str] = None,
        age:            Optional[int]  = None,
        return_heatmap: bool = True,
    ) -> dict[str, Any]:
        """
        Parameters
        ----------
        image_input    : base64-encoded image string
        blood_pressure : e.g. "130/85" (optional)
        sugar_level    : fasting glucose e.g. "120" or "120 mg/dL" (optional)
        age            : patient age in years (optional)
        return_heatmap : whether to generate GradCAM overlay

        Returns
        -------
        dict with keys: predicted_class, confidence, risk_level, probabilities,
                        heatmap_base64, affected_regions, recommendation,
                        model_type, uncertain, stage2_applied
        """
        image_bytes = base64.b64decode(image_input)

        metadata: dict[str, Any] = {}
        if age is not None:
            metadata["age"] = age
        if blood_pressure:
            metadata["blood_pressure"] = blood_pressure
        glucose = _parse_sugar(sugar_level)
        if glucose is not None:
            metadata["fasting_glucose"] = glucose

        raw = predict(
            image_bytes      = image_bytes,
            image_model      = self.image_model,
            idx_to_class     = self.idx_to_class,
            adjuster         = self.adjuster,
            metadata         = metadata or None,
            generate_gradcam = return_heatmap,
            model_mode       = self._heatmap_mode,
            needs_metadata   = self._needs_metadata,
            meta_dim         = self._meta_dim,
        )

        cls = raw["predicted_class"]
        return {
            "predicted_class":  cls,
            "confidence":       raw["confidence"],
            "risk_level":       raw["risk_level"],
            "probabilities":    raw["all_probabilities"],
            "heatmap_base64":   raw.get("gradcam_base64", ""),
            "affected_regions": _CLASS_REGIONS.get(cls, ["retina"]),
            "recommendation":   _CLASS_RECOMMENDATIONS.get(cls, {
                "summary":  "Please consult an ophthalmologist.",
                "actions":  [],
                "followUp": "4 weeks",
            }),
            "model_type":       "swin_transformer",
            "uncertain":        raw["uncertain"],
            "stage2_applied":   raw["stage2_applied"],
        }
        