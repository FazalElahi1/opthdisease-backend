"""
metadata_encoder.py — Shared utility used by train.py, predict.py, evaluate.py,
                       and ml_integration.py.

Converts a raw metadata dict (all fields optional) into a fixed 32-dim float32
numpy vector + a 5-dim binary mask indicating which field groups were provided.

Usage:
    from scripts.metadata_encoder import encode_metadata, METADATA_DIM

    vec = encode_metadata({
        "age": 54,
        "blood_pressure": "150/95",
        "fasting_glucose": 210.0,
        "hba1c": 8.5,
        "gender": "male",
        "smoking": "current",
        "family_history": ["diabetes", "hypertension"],
        "symptom_text": "floaters, blurred vision"
    })
    # vec.shape == (32,)  dtype float32
"""

from __future__ import annotations

import re
import json
import numpy as np
from pathlib import Path
from typing import Any

# ─── Constants ────────────────────────────────────────────────────────────────

METADATA_DIM = 32          # total feature vector length fed to Stage-2 MLP

# Symptom vocabulary (must match metadata_schema.json)
SYMPTOM_VOCAB = [
    "blurred vision", "double vision", "floaters", "flashes", "eye pain",
    "redness", "headache", "halos", "night blindness", "loss of peripheral vision",
    "distorted vision", "wavy lines", "sensitivity to light", "dry eyes",
    "itching", "discharge", "fever", "nausea", "fatigue", "dizziness",
]
SYMPTOM_DIM = 10   # TF-IDF projected to 10 dims via simple term-match + SVD-like reduction

# Gender categories
GENDER_CATS = ["male", "female", "other"]

# Smoking categories
SMOKING_CATS = ["never", "former", "current"]

# Family history flags (must stay in this order)
FAMILY_FLAGS = ["glaucoma", "amd", "diabetic_retinopathy", "cataract",
                "hypertension", "diabetes"]


# ─── Symptom encoding ─────────────────────────────────────────────────────────

def _encode_symptoms(text: str | None) -> np.ndarray:
    """
    Encodes free-text symptoms into a 10-dim float32 vector.
    Strategy:
      1. Match against SYMPTOM_VOCAB (binary presence)
      2. Reduce 20-dim binary to 10-dim by summing adjacent pairs
         (simple dimensionality reduction without needing sklearn at inference)
    Missing / empty text → zero vector.
    """
    vec = np.zeros(len(SYMPTOM_VOCAB), dtype=np.float32)
    if not text:
        return vec[:SYMPTOM_DIM]

    text_lower = text.lower()
    for i, term in enumerate(SYMPTOM_VOCAB):
        if term in text_lower:
            vec[i] = 1.0

    # Reduce 20 → 10 by pairwise sum (keeps positional semantics)
    reduced = vec.reshape(SYMPTOM_DIM, 2).sum(axis=1)
    # L2-normalise if any term matched
    norm = np.linalg.norm(reduced)
    if norm > 0:
        reduced = reduced / norm
    return reduced.astype(np.float32)


# ─── Blood pressure parser ────────────────────────────────────────────────────

def _parse_bp(bp_str: str | None) -> tuple[float | None, float | None]:
    """Parse '130/85' → (130.0, 85.0). Returns (None, None) on failure."""
    if not bp_str:
        return None, None
    m = re.match(r"(\d+)\s*/\s*(\d+)", str(bp_str).strip())
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


# ─── Main encoder ─────────────────────────────────────────────────────────────

def encode_metadata(meta: dict[str, Any] | None) -> np.ndarray:
    """
    Convert a raw metadata dict to a 32-dim float32 numpy vector.

    All fields are optional. Missing fields → zeros. The final 5 dims are a
    binary mask: [age_present, bp_present, glucose_present,
                  smoking_present, family_history_present].

    Parameters
    ----------
    meta : dict or None
        Keys (all optional):
          age             int/float
          gender          str: 'male'|'female'|'other'
          blood_pressure  str: 'systolic/diastolic'
          fasting_glucose float (mg/dL)
          hba1c           float (%)
          smoking         str: 'never'|'former'|'current'
          family_history  list[str] or comma-sep str
          symptom_text    str free text

    Returns
    -------
    np.ndarray  shape (32,)  dtype float32
    """
    if meta is None:
        meta = {}

    vec = np.zeros(METADATA_DIM, dtype=np.float32)
    mask = np.zeros(5, dtype=np.float32)   # will go into indices 27-31

    # ── 0: age ──
    age = meta.get("age")
    if age is not None:
        try:
            vec[0] = float(age) / 100.0
            mask[0] = 1.0
        except (ValueError, TypeError):
            pass

    # ── 1-3: gender one-hot ──
    gender = str(meta.get("gender", "")).strip().lower()
    if gender in GENDER_CATS:
        vec[1 + GENDER_CATS.index(gender)] = 1.0

    # ── 4-5: blood pressure ──
    systolic, diastolic = _parse_bp(meta.get("blood_pressure"))
    if systolic is not None:
        vec[4] = np.clip(systolic / 200.0, 0, 1)
        vec[5] = np.clip(diastolic / 120.0, 0, 1)
        mask[1] = 1.0

    # ── 6: fasting glucose ──
    glucose_provided = False
    glucose = meta.get("fasting_glucose")
    if glucose is not None:
        try:
            vec[6] = np.clip(float(glucose) / 400.0, 0, 1)
            glucose_provided = True
        except (ValueError, TypeError):
            pass

    # ── 7: HbA1c ──
    hba1c = meta.get("hba1c")
    if hba1c is not None:
        try:
            vec[7] = np.clip(float(hba1c) / 15.0, 0, 1)
            glucose_provided = True
        except (ValueError, TypeError):
            pass
    if glucose_provided:
        mask[2] = 1.0

    # ── 8-10: smoking one-hot ──
    smoking = str(meta.get("smoking", "")).strip().lower()
    if smoking in SMOKING_CATS:
        vec[8 + SMOKING_CATS.index(smoking)] = 1.0
        mask[3] = 1.0

    # ── 11-16: family history binary flags ──
    fh = meta.get("family_history")
    if fh is not None:
        if isinstance(fh, str):
            fh = [x.strip().lower() for x in fh.split(",") if x.strip()]
        elif isinstance(fh, (list, tuple)):
            fh = [str(x).strip().lower() for x in fh]
        else:
            fh = []
        for flag in fh:
            if flag in FAMILY_FLAGS:
                vec[11 + FAMILY_FLAGS.index(flag)] = 1.0
        if any(vec[11:17]):
            mask[4] = 1.0

    # ── 17-26: symptom TF-IDF (10 dims) ──
    symptom_vec = _encode_symptoms(meta.get("symptom_text"))
    vec[17:27] = symptom_vec

    # ── 27-31: mask ──
    vec[27:32] = mask

    return vec


def is_empty(meta_vec: np.ndarray) -> bool:
    """Returns True if the metadata vector carries no information (all zeros)."""
    return float(meta_vec.sum()) == 0.0


def metadata_from_csv_row(row: dict) -> dict:
    """
    Convert a CSV row dict (from pandas or csv.DictReader) into the
    standard metadata dict expected by encode_metadata().

    CSV columns (all optional except image_filename):
        image_filename, age, gender, blood_pressure,
        fasting_glucose, hba1c, smoking, family_history, symptom_text
    """
    def _get(key):
        val = row.get(key, "")
        if val is None:
            return None
        val = str(val).strip()
        return val if val else None

    meta: dict[str, Any] = {}

    age = _get("age")
    if age:
        try:
            meta["age"] = float(age)
        except ValueError:
            pass

    gender = _get("gender")
    if gender:
        meta["gender"] = gender.lower()

    bp = _get("blood_pressure")
    if bp:
        meta["blood_pressure"] = bp

    glucose = _get("fasting_glucose")
    if glucose:
        try:
            meta["fasting_glucose"] = float(glucose)
        except ValueError:
            pass

    hba1c = _get("hba1c")
    if hba1c:
        try:
            meta["hba1c"] = float(hba1c)
        except ValueError:
            pass

    smoking = _get("smoking")
    if smoking:
        meta["smoking"] = smoking.lower()

    fh = _get("family_history")
    if fh:
        meta["family_history"] = [x.strip().lower() for x in fh.split(",") if x.strip()]

    symptom = _get("symptom_text")
    if symptom:
        meta["symptom_text"] = symptom

    return meta