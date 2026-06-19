from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel

# ── CHANGE 1: imports ──────────────────────────────────────────────────────────
# OLD:  from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
#       from services.firebase import get_firestore_client, get_user_doc
#       from services.jwt import decode_access_token
#
# NEW:  - Drop HTTPBearer, HTTPAuthorizationCredentials, decode_access_token,
#         get_user_doc — all handled by the shared get_current_user.
#       - Drop get_firestore_client — no longer called directly.
#       - Add _col, COL_SCANS for namespace-safe reads/writes on scans collection.
#       - Import shared get_current_user from notifications.
from google.cloud.firestore_v1.base_query import FieldFilter
from services.firebase import _col, COL_SCANS, get_user_doc
from services.notifications import get_current_user
# ── END CHANGE 1 ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/scans", tags=["Scans"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class ScanReview(BaseModel):
    notes:              str
    doctorRiskOverride: Optional[str] = None


class ScanIn(BaseModel):
    id:                  str
    patientId:           str
    imageUrl:            str
    date:                str
    riskLevel:           str
    findings:            str
    bloodPressure:       Optional[str]  = None
    sugarLevel:          Optional[str]  = None
    clinicalInfo:        Optional[str]  = None
    isDirectConsult:     Optional[bool] = False
    assignedDoctorId:    Optional[str]  = None
    assignedDoctorName:  Optional[str]  = None
    consultationFee:     Optional[int]  = None
    patientAge:          Optional[int]  = None


# ── Patient-info enrichment ─────────────────────────────────────────────────────
# Doctors must see WHICH patient sent a scan. The patient's real name and age live
# on their user doc (captured at signup), not necessarily on the scan record, so we
# join them in at read time. This fixes both new scans and any older scans that were
# saved before patientName/patientAge were stored. Lookups are cached per request.

def _enrich_with_patient(scans: List[dict]) -> List[dict]:
    cache: dict = {}
    for scan in scans:
        pid = scan.get("patientId")
        if not pid:
            continue
        if pid not in cache:
            cache[pid] = get_user_doc(pid) or {}
        user = cache[pid]
        name = user.get("name") or scan.get("patientName")
        if name:
            scan["patientName"] = name
        age = user.get("age") if user.get("age") is not None else scan.get("patientAge")
        if age is not None:
            scan["patientAge"] = age
    return scans


# ── CHANGE 2: remove local get_current_user() entirely ────────────────────────
# OLD CODE (lines 39-44):
#   bearer = HTTPBearer()
#   def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
#       payload  = decode_access_token(credentials.credentials)
#       user_id  = payload["sub"]
#       user_doc = get_user_doc(user_id)
#       if not user_doc:
#           raise HTTPException(status_code=404, detail="User not found.")
#       return {**user_doc, "user_id": user_id}
#
# REMOVED. Shared version validates JWT + session_token + is_active.
# ── END CHANGE 2 ───────────────────────────────────────────────────────────────


# ── Save scan ──────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def save_scan(
    body:         ScanIn,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can save scans.")

    scan_doc = {
        **body.model_dump(),
        "patientId":   current_user["user_id"],   # always force auth user id
        "patientName": current_user.get("name", ""),
        "patientAge":  current_user.get("age"),    # registered age, for the doctor's view
        "savedAt":     datetime.now(timezone.utc).isoformat(),
    }

    # ── CHANGE 3: replace raw db.collection("scans").document().set() ─────────
    # OLD:  db = get_firestore_client()
    #       db.collection("scans").document(body.id).set(scan_doc, merge=True)
    #
    # NEW:  _col(COL_SCANS) routes through app_data/meta → scans
    _col(COL_SCANS).document(body.id).set(scan_doc, merge=True)
    # ── END CHANGE 3 ───────────────────────────────────────────────────────────

    return {"message": "Scan saved.", "scan_id": body.id}


# ── Patient: get own scans ─────────────────────────────────────────────────────

@router.get("/my")
async def get_my_scans(current_user: dict = Depends(get_current_user)):
    # ── CHANGE 4: replace raw db.collection("scans") query ────────────────────
    # OLD:  db   = get_firestore_client()
    #       docs = db.collection("scans").where(...).order_by(...).stream()
    # NOTE: no .order_by() here — combining .where() with .order_by() on a
    # different field requires a composite Firestore index. When that index is
    # missing, Firestore raises FailedPrecondition, FastAPI returns plain-text
    # "Internal Server Error", and the app's res.json() throws
    # "Unexpected character: I". Sorting in Python avoids the index entirely.
    docs = (
        _col(COL_SCANS)
        .where(filter=FieldFilter("patientId", "==", current_user["user_id"]))
        .stream()
    )
    # ── END CHANGE 4 ───────────────────────────────────────────────────────────
    scans = [doc.to_dict() for doc in docs]
    scans.sort(key=lambda s: s.get("date", ""), reverse=True)
    return {"scans": scans}


# ── Doctor: get scans assigned to them ────────────────────────────────────────

@router.get("/assigned")
async def get_assigned_scans(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Only doctors can access this.")

    # ── CHANGE 5: replace raw db.collection("scans") query ────────────────────
    # OLD:  db   = get_firestore_client()
    #       docs = db.collection("scans").where(...).stream()
    docs = (
        _col(COL_SCANS)
        .where(filter=FieldFilter("assignedDoctorId", "==", current_user["user_id"]))
        .stream()
    )
    # ── END CHANGE 5 ───────────────────────────────────────────────────────────
    scans = [doc.to_dict() for doc in docs]
    scans = _enrich_with_patient(scans)
    scans.sort(key=lambda s: s.get("date", ""), reverse=True)
    return {"scans": scans}


# ── Doctor: get open (unassigned) scans ───────────────────────────────────────

@router.get("/open")
async def get_open_scans(current_user: dict = Depends(get_current_user)):
    """Unassigned patient scans awaiting review. These are the doctor's "Open
    Cases". Served from the backend (not local storage) so every doctor sees the
    patient's real name + age, regardless of device."""
    if current_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Only doctors can access this.")

    # Firestore can't query "field is null/absent" reliably, so pull recent scans
    # and filter out the assigned ones in Python.
    docs  = _col(COL_SCANS).stream()
    scans = [
        s for s in (doc.to_dict() for doc in docs)
        if not s.get("assignedDoctorId")
    ]
    scans = _enrich_with_patient(scans)
    scans.sort(key=lambda s: s.get("date", ""), reverse=True)
    return {"scans": scans}


# ── Doctor: add review to a scan ──────────────────────────────────────────────

@router.post("/{scan_id}/review", status_code=200)
async def add_review(
    scan_id:      str,
    body:         ScanReview,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Only doctors can add reviews.")

    # ── CHANGE 6: replace raw db.collection("scans") read + update ────────────
    # OLD:  db  = get_firestore_client()
    #       ref = db.collection("scans").document(scan_id)
    #       doc = ref.get()
    #       if not doc.exists: raise HTTPException(404, ...)
    #       scan = doc.to_dict()
    #       ...
    #       ref.update({...})
    ref = _col(COL_SCANS).document(scan_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Scan not found.")
    # ── END CHANGE 6 ───────────────────────────────────────────────────────────

    scan = doc.to_dict()
    assigned = scan.get("assignedDoctorId")
    if assigned and assigned != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="This scan is assigned to another doctor.")

    update = {
        "review": {
            "notes":              body.notes,
            "doctorRiskOverride": body.doctorRiskOverride,
            "reviewedAt":         datetime.now(timezone.utc).isoformat(),
            "doctorId":           current_user["user_id"],
            "doctorName":         current_user.get("name", ""),
        }
    }

    # If the scan was an unassigned "Open Case", the reviewing doctor CLAIMS it by
    # reviewing it: set assignedDoctorId/Name so it leaves the shared Open Cases
    # pool (/scans/open) and moves into THIS doctor's Patient History
    # (/scans/assigned needs both assignedDoctorId and review set). Open Cases is a
    # shared pool, so the first doctor to review an open case removes it from every
    # other doctor's Open Cases.
    if not assigned:
        update["assignedDoctorId"]   = current_user["user_id"]
        update["assignedDoctorName"] = current_user.get("name", "")

    ref.update(update)
    return {"message": "Review saved."}


# ── Get single scan ────────────────────────────────────────────────────────────

@router.get("/{scan_id}")
async def get_scan(
    scan_id:      str,
    current_user: dict = Depends(get_current_user),
):
    # ── CHANGE 7: replace raw db.collection("scans").document().get() ─────────
    # OLD:  db  = get_firestore_client()
    #       doc = db.collection("scans").document(scan_id).get()
    doc = _col(COL_SCANS).document(scan_id).get()
    # ── END CHANGE 7 ───────────────────────────────────────────────────────────

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Scan not found.")
    scan = doc.to_dict()
    uid  = current_user["user_id"]
    if scan.get("patientId") != uid and scan.get("assignedDoctorId") != uid:
        raise HTTPException(status_code=403, detail="Access denied.")
    return scan