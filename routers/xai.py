"""
xai.py
======
/xai/analyze endpoint — uses the real trained model via ml/scripts/predict.py.

Two modes:
  ONLINE:  ML + GradCAM + Gemini findings + Firestore save
  OFFLINE: ML + GradCAM + local recommendations only (no Gemini, no Firestore)
"""

import os

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path

from google.cloud.firestore_v1.base_query import FieldFilter
from services.firebase import (
    _col,
    COL_SCANS,
    COL_APPOINTMENTS,
    COL_NOTIFICATIONS,
    get_doctor_doc,
)
from services.notifications import get_current_user

router = APIRouter(prefix="/xai", tags=["Explainable AI"])

MODEL_PATH   = "./output/eye_disease_model.pt"
MAPPING_PATH = "./output/class_mapping.json"

_predictor = None

def get_predictor():
    global _predictor
    if _predictor is None:
        if not Path(MODEL_PATH).exists():
            raise RuntimeError(
                "ML model not available on this server. "
                "Eye scan analysis requires the model file eye_disease_model.pt in ./output/"
            )
        try:
            from ml.scripts.predict import EyeDiseasePredictor
            _predictor = EyeDiseasePredictor(MODEL_PATH, MAPPING_PATH)
        except ImportError:
            raise RuntimeError(
                "ML dependencies (torch, cv2) are not installed on this server. "
                "Eye scan analysis is unavailable."
            )
    return _predictor


# ── ML inference: remote service (prod) or in-process model (local dev) ──────────
# In production the API is torch-free and offloads inference to the Hugging Face
# Space (ML_SERVICE_URL). When ML_SERVICE_URL is unset (local dev), we fall back to
# the in-process model via get_predictor(). Both return the SAME dict shape.
ML_SERVICE_URL   = os.getenv("ML_SERVICE_URL", "").rstrip("/")
ML_SERVICE_TOKEN = os.getenv("ML_SERVICE_TOKEN", "")


async def run_ml_prediction(
    image_base64:   str,
    blood_pressure: Optional[str],
    sugar_level:    Optional[str],
    age:            Optional[int],
) -> dict:
    if ML_SERVICE_URL:
        headers = {"x-ml-token": ML_SERVICE_TOKEN} if ML_SERVICE_TOKEN else {}
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{ML_SERVICE_URL}/predict",
                json={
                    "image_base64":   image_base64,
                    "blood_pressure": blood_pressure,
                    "sugar_level":    sugar_level,
                    "age":            age,
                    "return_heatmap": True,
                },
                headers=headers,
            )
        resp.raise_for_status()
        return resp.json()

    # Local fallback — run the model in-process (requires torch installed locally).
    predictor = get_predictor()
    return predictor.predict(
        image_input    = image_base64,
        blood_pressure = blood_pressure,
        sugar_level    = sugar_level,
        age            = age,
        return_heatmap = True,
    )


# ── Schemas ────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    image_base64:   str
    blood_pressure: Optional[str] = None
    sugar_level:    Optional[str] = None
    clinical_info:  Optional[str] = None
    patient_age:    Optional[int] = None
    offline_mode:   bool = False    # if True: skip Gemini + Firestore


class QuestionRequest(BaseModel):
    question: str
    scan_id:  Optional[str] = None


class ShareReportRequest(BaseModel):
    scan_id:   str
    doctor_id: str
    message:   Optional[str] = None


# ── Analyze ────────────────────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_with_xai(
    body:         AnalyzeRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can submit scans.")

    try:
        ml_result = await run_ml_prediction(
            image_base64   = body.image_base64,
            blood_pressure = body.blood_pressure,
            sugar_level    = body.sugar_level,
            age            = body.patient_age or current_user.get("age"),
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"ML service unavailable: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {str(e)}")

    scan_id = f"{current_user['user_id']}_{int(datetime.now().timestamp())}"
    now     = datetime.now(timezone.utc).isoformat()

    findings         = ""
    affected_regions = ml_result.get("affected_regions", [])
    # Recommendation is ONLINE-ONLY and personalized per scan by Gemini.
    # Offline scans intentionally carry no recommendation.
    recommendation   = ""

    if not body.offline_mode:
        import asyncio
        from services.gemini_service import (
            analyze_eye_image_with_restriction,
            generate_personalized_recommendation,
        )
        _offline_findings = _build_offline_findings(ml_result)

        # Run findings + recommendation concurrently — saves 3-8 s vs sequential.
        # Recommendation uses offline findings as its context so it doesn't need
        # to wait for the Gemini findings call to finish first.
        _findings_res, _rec_res = await asyncio.gather(
            analyze_eye_image_with_restriction(
                image_base64   = body.image_base64,
                blood_pressure = body.blood_pressure,
                sugar_level    = body.sugar_level,
                clinical_info  = body.clinical_info,
            ),
            generate_personalized_recommendation(
                disease        = ml_result["predicted_class"],
                risk_level     = ml_result["risk_level"],
                findings       = _offline_findings,
                blood_pressure = body.blood_pressure,
                sugar_level    = body.sugar_level,
                patient_age    = body.patient_age or current_user.get("age"),
            ),
            return_exceptions=True,
        )

        if isinstance(_findings_res, Exception):
            print(f"[XAI] Gemini findings unavailable: {_findings_res}")
            findings = _offline_findings
        else:
            findings         = _findings_res.get("findings", "")
            affected_regions = _findings_res.get("affectedRegions", affected_regions)

        if isinstance(_rec_res, Exception):
            print(f"[XAI] Gemini recommendation unavailable: {_rec_res}")
            recommendation = ""
        else:
            recommendation = str(_rec_res)

        try:
            _col(COL_SCANS).document(scan_id).set({
                "id":               scan_id,
                "patientId":        current_user["user_id"],
                "patientName":      current_user.get("name", ""),
                "date":             now,
                "riskLevel":        ml_result["risk_level"],
                "findings":         findings,
                "primaryCondition": ml_result["predicted_class"],
                "confidence":       ml_result["confidence"],
                "probabilities":    ml_result["probabilities"],
                "affectedRegions":  affected_regions,
                "recommendation":   recommendation,
                "heatmapBase64":    ml_result["heatmap_base64"],
                "bloodPressure":    body.blood_pressure or "",
                "sugarLevel":       body.sugar_level    or "",
                "clinicalInfo":     body.clinical_info  or "",
                "mlSource":         ml_result.get("model_type", "swin_transformer"),
                "savedAt":          now,
            })
        except Exception as e:
            print(f"[XAI] Firestore save failed: {e}")
    else:
        # OFFLINE: on-device-style prediction only. No recommendation generated.
        findings       = _build_offline_findings(ml_result)
        recommendation = ""

    return {
        "scan_id":           scan_id,
        "risk_level":        ml_result["risk_level"],
        "findings":          findings,
        "primary_condition": ml_result["predicted_class"],
        "confidence":        ml_result["confidence"],
        "probabilities":     ml_result["probabilities"],
        "affected_regions":  affected_regions,
        "urgency":           _get_urgency(ml_result["risk_level"]),
        "recommendation":    recommendation,
        "heatmap_base64":    ml_result["heatmap_base64"],
        "predicted_class":   ml_result["predicted_class"],
        "offline_mode":      body.offline_mode,
        "telemedicine_available": not body.offline_mode,
        "ml_source":         ml_result.get("model_type", "swin_transformer"),
    }


# ── Heatmap retrieval ──────────────────────────────────────────────────────────

@router.get("/scan/{scan_id}/heatmap")
async def get_heatmap(
    scan_id:      str,
    current_user: dict = Depends(get_current_user),
):
    doc = _col(COL_SCANS).document(scan_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Scan not found.")
    scan = doc.to_dict()
    uid  = current_user["user_id"]
    if scan.get("patientId") != uid and scan.get("assignedDoctorId") != uid:
        raise HTTPException(status_code=403, detail="Access denied.")
    return {
        "scan_id":           scan_id,
        "heatmap_base64":    scan.get("heatmapBase64", ""),
        "risk_level":        scan.get("riskLevel", ""),
        "findings":          scan.get("findings", ""),
        "recommendation":    scan.get("recommendation", ""),
        "primary_condition": scan.get("primaryCondition", ""),
        "confidence":        scan.get("confidence", 0),
        "date":              scan.get("date", ""),
    }


# ── Share report ───────────────────────────────────────────────────────────────

@router.post("/scan/{scan_id}/share")
async def share_report(
    scan_id:      str,
    body:         ShareReportRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can share reports.")

    ref = _col(COL_SCANS).document(scan_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Scan not found.")

    scan = doc.to_dict()
    if scan.get("patientId") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="This scan does not belong to you.")

    doctor = get_doctor_doc(body.doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found.")

    # ── Payment gate: patient must have a paid/confirmed/completed appointment ────
    # with this specific doctor before they can share a scan with them.
    paid_appts = list(
        _col(COL_APPOINTMENTS)
        .where(filter=FieldFilter("patient_id", "==", current_user["user_id"]))
        .where(filter=FieldFilter("doctor_id", "==", body.doctor_id))
        .stream()
    )
    has_paid = any(
        a.to_dict().get("status") in ("paid", "confirmed", "completed")
        for a in paid_appts
    )
    if not has_paid:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Please book and pay for a consultation with Dr. {doctor.get('name', '')} "
                "before sharing a scan with them."
            ),
        )
    # ── END payment gate ─────────────────────────────────────────────────────────

    # ── Limit: one PENDING scan per doctor ───────────────────────────────────────
    # A patient may not send a second scan to the same doctor while a previously
    # shared scan is still awaiting that doctor's review. Once the doctor reviews
    # it, the patient can share a new one. We query by patientId only (single
    # equality filter = no composite index needed) and filter in Python, matching
    # the pattern used elsewhere in this codebase.
    for d in _col(COL_SCANS).where(filter=FieldFilter("patientId", "==", current_user["user_id"])).stream():
        if d.id == scan_id:
            continue  # re-sharing this same scan is allowed
        other = d.to_dict()
        if other.get("assignedDoctorId") == body.doctor_id and not other.get("review"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"You already have a scan awaiting Dr. {doctor.get('name', '')}'s "
                    "review. Please wait for their response before sending another scan "
                    "to this doctor."
                ),
            )
    # ── END limit ────────────────────────────────────────────────────────────────

    ref.update({
        "assignedDoctorId":   body.doctor_id,
        "assignedDoctorName": doctor.get("name", ""),
        "sharedAt":           datetime.now(timezone.utc).isoformat(),
        "shareMessage":       body.message or "",
    })

    _col(COL_NOTIFICATIONS).add({
        "userId":    body.doctor_id,
        "title":     "New XAI Report Shared",
        "body":      f"{current_user.get('name', 'A patient')} shared an eye scan for your review.",
        "type":      "report_shared",
        "scanId":    scan_id,
        "patientId": current_user["user_id"],
        "read":      False,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    })

    from services.notifications import send_push_notification
    await send_push_notification(
        user_id = body.doctor_id,
        title   = "New XAI Report Shared",
        body    = f"{current_user.get('name', 'A patient')} shared an eye scan for your review.",
        data    = {"type": "report_shared", "scan_id": scan_id},
    )

    return {
        "message":     f"Report shared with Dr. {doctor.get('name', '')}.",
        "doctor_name": doctor.get("name", ""),
        "scan_id":     scan_id,
    }


# ── Eye-only restricted question ──────────────────────────────────────────────

@router.post("/ask")
async def ask_eye_question(
    body:         QuestionRequest,
    current_user: dict = Depends(get_current_user),
):
    from services.gemini_service import (
        is_eye_related_question,
        generate_personalized_recommendation,
    )

    if not await is_eye_related_question(body.question):
        return {
            "answer": (
                "I can only answer questions about eye health, vision, and ophthalmology. "
                "Please ask about topics like diabetic retinopathy, glaucoma, cataracts, "
                "macular degeneration, or your scan results."
            ),
            "restricted": True,
        }

    scan_context = ""
    if body.scan_id:
        doc = _col(COL_SCANS).document(body.scan_id).get()
        if doc.exists:
            s = doc.to_dict()
            scan_context = (
                f"Patient's recent scan: {s.get('primaryCondition', '')} detected. "
                f"Risk level: {s.get('riskLevel', '')}. "
                f"Findings: {s.get('findings', '')}"
            )

    answer = await generate_personalized_recommendation(
        disease      = "based on question",
        risk_level   = "unknown",
        findings     = body.question,
        patient_age  = current_user.get("age"),
        is_question  = True,
        scan_context = scan_context,
    )

    return {"answer": answer, "restricted": False}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_offline_findings(ml_result: dict) -> str:
    cls     = ml_result["predicted_class"].replace("_", " ")
    conf    = ml_result["confidence"] * 100
    regions = ", ".join(ml_result.get("affected_regions", [])) or "retina"
    return (
        f"AI detected {cls} with {conf:.0f}% confidence. "
        f"Attention focused on: {regions}. "
        f"This result was generated offline without internet connectivity. "
        f"For detailed clinical findings, please consult an ophthalmologist."
    )


def _get_urgency(risk_level: str) -> str:
    return {"High": "Urgent", "Medium": "Soon", "Low": "Routine"}.get(risk_level, "Routine")
