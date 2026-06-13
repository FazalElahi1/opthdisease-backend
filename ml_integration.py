"""
ml_integration_updated.py — Two-stage backend integration.
Stage-1: Swin-T image model.
Stage-2: MetadataAdjuster MLP (optional, skipped if no weights or no metadata).

Place at: OpthdiseaseAI/ml_integration.py

API body (all metadata fields optional):
{
    "image": "<base64>",
    "age": 54, "gender": "male", "blood_pressure": "150/95",
    "fasting_glucose": 210, "hba1c": 8.5, "smoking": "current",
    "family_history": ["diabetes","glaucoma"],
    "symptom_text": "floaters, blurred vision"
}
"""
from __future__ import annotations
import io, json, logging, os, time
from pathlib import Path
from typing import Any, Optional
import numpy as np
import torch, torch.nn as nn
from PIL import Image
from torchvision import models, transforms
import sys

_ML_DIR = Path(__file__).resolve().parent / "ml" / "scripts"
if _ML_DIR.exists() and str(_ML_DIR) not in sys.path:
    sys.path.insert(0, str(_ML_DIR))

from ml.scripts.metadata_encoder import encode_metadata, is_empty
from ml.scripts.metadata_adjuster import MetadataAdjuster, load_adjuster, adjust_probs

logger = logging.getLogger(__name__)
_BASE = Path(__file__).resolve().parent
_OUT  = _BASE / "output"

MODEL_PATH    = os.environ.get("EYE_MODEL_PATH",    str(_OUT / "eye_disease_model.pt"))
ADJUSTER_PATH = os.environ.get("EYE_ADJUSTER_PATH", str(_OUT / "metadata_adjuster.pt"))
MAPPING_PATH  = os.environ.get("EYE_MAPPING_PATH",  str(_OUT / "class_mapping.json"))
IMG_SIZE = 224
DEVICE   = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]
CONFIDENCE_THRESHOLD = 0.50

def _risk(cls, conf):
    if cls == "Normal":
        return "low" if conf >= 0.80 else ("moderate" if conf >= 0.55 else "high")
    return "high" if conf >= 0.75 else ("moderate" if conf >= 0.50 else "low")

class _Singleton:
    _inst = None
    def __new__(cls):
        if not cls._inst:
            cls._inst = super().__new__(cls)
            cls._inst._ready = False
        return cls._inst

    def initialise(self):
        if self._ready: return
        with open(MAPPING_PATH) as f: raw = json.load(f)
        self.idx_to_class = ({v:k for k,v in raw.items()} if not next(iter(raw)).isdigit()
                             else {int(k):v for k,v in raw.items()})
        self.num_classes = len(self.idx_to_class)
        m = models.swin_t(weights=None)
        m.head = nn.Linear(m.head.in_features, self.num_classes)
        m.load_state_dict(torch.load(MODEL_PATH, map_location=DEVICE))
        m.to(DEVICE).eval()
        self.image_model = m
        self.adjuster: Optional[MetadataAdjuster] = load_adjuster(ADJUSTER_PATH, self.num_classes)
        self.transform = transforms.Compose([
            transforms.Resize((IMG_SIZE, IMG_SIZE)),
            transforms.ToTensor(),
            transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
        ])
        self._ready = True

    def predict(self, image_bytes, metadata=None):
        self.initialise()
        t0 = time.time()
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        tensor = self.transform(img).unsqueeze(0).to(DEVICE)
        with torch.no_grad():
            image_probs = torch.softmax(self.image_model(tensor), dim=1)[0].cpu().numpy()
        meta_vec = encode_metadata(metadata)
        final_probs = adjust_probs(self.adjuster, image_probs, meta_vec)
        idx = int(np.argmax(final_probs))
        conf = float(final_probs[idx])
        cls  = self.idx_to_class[idx]
        return {
            "predicted_class":   cls,
            "confidence":        round(conf, 6),
            "risk_level":        _risk(cls, conf),
            "uncertain":         conf < CONFIDENCE_THRESHOLD,
            "all_probabilities": dict(sorted({self.idx_to_class[i]: round(float(p),6)
                                  for i,p in enumerate(final_probs)}.items(), key=lambda x:x[1], reverse=True)),
            "image_only_probs":  {self.idx_to_class[i]: round(float(p),6) for i,p in enumerate(image_probs)},
            "metadata_provided": not is_empty(meta_vec),
            "stage2_applied":    not is_empty(meta_vec) and self.adjuster is not None,
            "inference_time_ms": round((time.time()-t0)*1000, 2),
            "model_version":     "swin_t_v1_two_stage",
            **({"metadata_note": "No metadata provided. Image-only result."} if is_empty(meta_vec) else {}),
        }

_model = _Singleton()

def analyse_fundus_image(image_bytes: bytes, metadata: dict|None = None) -> dict:
    try:
        return {"success": True, **_model.predict(image_bytes, metadata)}
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception(e)
        return {"success": False, "error": "Internal inference error"}

def get_model_info():
    try:
        _model.initialise()
        return {"loaded": True, "num_classes": _model.num_classes,
                "classes": list(_model.idx_to_class.values()),
                "stage2_available": _model.adjuster is not None, "device": str(DEVICE)}
    except Exception as e:
        return {"loaded": False, "error": str(e)}

def warm_up():
    try:
        _model.initialise()
        buf = io.BytesIO()
        Image.new("RGB",(IMG_SIZE,IMG_SIZE),(128,128,128)).save(buf, format="JPEG")
        _model.predict(buf.getvalue())
        logger.info("Warm-up complete.")
    except Exception as e:
        logger.error("Warm-up failed: %s", e)

def _meta_from(d):
    keys = ["age","gender","blood_pressure","fasting_glucose","hba1c","smoking","family_history","symptom_text"]
    return {k: d[k] for k in keys if k in d and d[k] not in ("", None)} or None

def handle_django_upload(request):
    f = request.FILES.get("image")
    if not f: return {"success": False, "error": "No image provided"}
    data = dict(getattr(request, "data", None) or request.POST)
    return analyse_fundus_image(f.read(), _meta_from(data))

def handle_flask_upload(request):
    f = request.files.get("image")
    if not f: return {"success": False, "error": "No image provided"}
    data = request.get_json(silent=True) or dict(request.form)
    return analyse_fundus_image(f.read(), _meta_from(data))
