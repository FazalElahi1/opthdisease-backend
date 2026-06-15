from fastapi import APIRouter, HTTPException, status, Depends, Query, UploadFile, File
from typing import List
import os
import cloudinary
import cloudinary.uploader

from models.schemas import (
    UserProfile, DoctorProfile,
    UpdateProfileRequest, UpdateDoctorProfileRequest,
    UserRole,
)
from services.firebase import (
    _col, COL_DOCTORS,
    set_user_doc,
    get_doctor_doc, set_doctor_doc,
    get_doctor_by_license, normalise_license,
)

# Doctor fields that must NEVER be exposed to patients.
_DOCTOR_PRIVATE_FIELDS = ("password_hash", "session_token", "phone", "phoneNumber",
                          "payout_account_holder", "payout_bank_name", "payout_account_number",
                          "jazzcash_number", "jazzcash_name", "fcm_token", "email")


def _public_doctor(d: dict) -> dict:
    """Strip private fields from a doctor doc before sending it to a patient."""
    pub = {k: v for k, v in d.items() if k not in _DOCTOR_PRIVATE_FIELDS}
    # Expose an explicit boolean the app can rely on (the stored field is
    # `license_status`; the client used to filter on a non-existent `is_verified`).
    pub["is_verified"] = d.get("license_status") == "verified"
    return pub
from services.notifications import get_current_user
# ── END CHANGE 1 ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/users", tags=["Users"])


# ── CHANGE 2: remove local get_current_user() entirely ────────────────────────
# OLD CODE (lines 20-27):
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


# ── List all verified doctors (for patient search) ────────────────────────────

@router.get("/doctors", response_model=List[dict])
async def list_doctors(
    specialty: str = Query(None, description="Filter by specialty"),
    current_user: dict = Depends(get_current_user),
):
    # ── CHANGE 3: replace raw db.collection("doctors") ────────────────────────
    # OLD:  db    = get_firestore_client()
    #       query = db.collection("doctors")
    #       docs  = query.stream()
    #
    # NEW:  _col(COL_DOCTORS) routes through app_data/meta → doctors
    query = _col(COL_DOCTORS).where("license_status", "==", "verified")
    doctors = []
    for doc in query.stream():
        d = doc.to_dict()
        d["user_id"] = doc.id
        if specialty:
            specialties = [s.lower() for s in d.get("specialties", [])]
            if specialty.lower() not in specialties:
                continue
        # Never expose the doctor's phone / JazzCash / contact details to patients.
        doctors.append(_public_doctor(d))
    return doctors


# ── Patient profile ────────────────────────────────────────────────────────────

@router.get("/profile", response_model=UserProfile)
async def get_profile(current_user: dict = Depends(get_current_user)):
    # NO CHANGES — current_user already comes fresh from Firestore via the shared dep
    current_user.pop("password_hash", None)
    current_user.pop("session_token", None)  # ← never expose session_token
    return UserProfile(**current_user)


@router.patch("/profile", response_model=UserProfile)
async def update_profile(
    body: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user),
):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")
    set_user_doc(current_user["user_id"], updates)
    updated = {**current_user, **updates}
    updated.pop("password_hash", None)
    updated.pop("session_token", None)
    return UserProfile(**updated)


# ── Doctor profile ─────────────────────────────────────────────────────────────

@router.get("/doctor-profile", response_model=DoctorProfile)
async def get_doctor_profile(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != UserRole.DOCTOR.value:
        raise HTTPException(status_code=403, detail="Only doctors can access this.")
    doc = get_doctor_doc(current_user["user_id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Doctor profile not found.")
    return DoctorProfile(**{**doc, "user_id": current_user["user_id"],
                            "is_verified": doc.get("license_status") == "verified"})


@router.patch("/doctor-profile", response_model=DoctorProfile)
async def update_doctor_profile(
    body: UpdateDoctorProfileRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != UserRole.DOCTOR.value:
        raise HTTPException(status_code=403, detail="Only doctors can access this.")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    # License number must stay unique across doctors (names may repeat).
    if updates.get("license_number"):
        existing = get_doctor_by_license(updates["license_number"])
        if existing and existing.get("user_id") != current_user["user_id"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A doctor with this license number is already registered.",
            )
        updates["license_number_norm"] = normalise_license(updates["license_number"])

    set_doctor_doc(current_user["user_id"], updates)
    user_updates = {k: v for k, v in updates.items() if k in ("name", "phone", "gender")}
    if user_updates:
        set_user_doc(current_user["user_id"], user_updates)
    doc = get_doctor_doc(current_user["user_id"])
    return DoctorProfile(**{**doc, "user_id": current_user["user_id"],
                            "is_verified": doc.get("license_status") == "verified"})


@router.post("/doctor-profile/image")
async def upload_doctor_image(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != UserRole.DOCTOR.value:
        raise HTTPException(status_code=403, detail="Only doctors can upload a profile image.")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key    = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    if not all([cloud_name, api_key, api_secret]):
        raise HTTPException(
            status_code=503,
            detail="Image upload service is temporarily unavailable. Please contact support.",
        )

    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret)

    try:
        contents = await file.read()
        result = cloudinary.uploader.upload(
            contents,
            folder="doctor_profiles",
            public_id=current_user["user_id"],
            overwrite=True,
            resource_type="image",
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Image upload failed. Please try again.")

    secure_url: str = result["secure_url"]
    set_doctor_doc(current_user["user_id"], {"image_url": secure_url})
    set_user_doc(current_user["user_id"], {"image_url": secure_url})

    return {"url": secure_url}