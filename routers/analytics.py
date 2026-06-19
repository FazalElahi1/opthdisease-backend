from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter
from services.firebase import _col, COL_APPOINTMENTS, COL_SCANS
from services.notifications import get_current_user

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/doctor")
async def doctor_analytics(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Only doctors can access analytics.")

    docs      = _col(COL_APPOINTMENTS).where(filter=FieldFilter("doctor_id", "==", current_user["user_id"])).stream()
    all_appts = [doc.to_dict() for doc in docs]

    completed      = [a for a in all_appts if a.get("status") == "completed"]
    total_earnings = sum(a.get("price_pkr", 0) for a in completed)

    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly    = [a for a in completed if a.get("slot_date", "").startswith(this_month)]

    status_counts: dict = {}
    for a in all_appts:
        s = a.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    recent = sorted(
        all_appts,
        key     = lambda a: a.get("slot_date", "") + a.get("slot_start_time", ""),
        reverse = True,
    )[:5]

    scan_docs      = _col(COL_SCANS).where(filter=FieldFilter("assignedDoctorId", "==", current_user["user_id"])).stream()
    scans          = [doc.to_dict() for doc in scan_docs]
    reviewed_count = sum(1 for s in scans if s.get("review"))

    return {
        "total_earnings_pkr":     total_earnings,
        "total_appointments":     len(all_appts),
        "completed_appointments": len(completed),
        "monthly_patients":       len(monthly),
        "monthly_earnings_pkr":   sum(a.get("price_pkr", 0) for a in monthly),
        "status_breakdown":       status_counts,
        "reviewed_scans":         reviewed_count,
        "total_assigned_scans":   len(scans),
        "recent_appointments":    recent,
    }


@router.get("/patient")
async def patient_analytics(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can access this.")

    scan_docs = _col(COL_SCANS).where(filter=FieldFilter("patientId", "==", current_user["user_id"])).stream()
    scans     = [doc.to_dict() for doc in scan_docs]

    risk_counts: dict = {}
    for s in scans:
        r = s.get("riskLevel", "Unknown")
        risk_counts[r] = risk_counts.get(r, 0) + 1

    appt_docs = _col(COL_APPOINTMENTS).where(filter=FieldFilter("patient_id", "==", current_user["user_id"])).stream()
    appts     = [doc.to_dict() for doc in appt_docs]

    total_spent             = sum(a.get("price_pkr", 0) for a in appts if a.get("status") == "completed")
    completed_consultations = sum(1 for a in appts if a.get("status") == "completed")

    return {
        "total_scans":             len(scans),
        "risk_distribution":       risk_counts,
        "total_consultations":     len(appts),
        "completed_consultations": completed_consultations,
        "total_spent_pkr":         total_spent,
        "scans_with_review":       sum(1 for s in scans if s.get("review")),
    }
