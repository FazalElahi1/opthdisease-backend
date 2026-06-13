"""
reviews.py
==========
Patient ratings and complaints for completed telehealth appointments.

Flow:
  1. Appointment completes (call ends) → backend sets call_duration_seconds
  2. Patient opens RatingModal in app → submits 1-5 stars + optional complaint
  3. If complaint + request_refund → flagged for admin review
  4. Admin reviews → resolves via PATCH /admin/complaints/{id}/resolve
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from services.firebase import (
    _col, get_doc, set_doc,
    COL_APPOINTMENTS, COL_REVIEWS, COL_USERS,
)
from services.notifications import send_push_notification, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reviews", tags=["Reviews"])

SHORT_CALL_SECONDS = 120  # calls under 2 minutes trigger auto-complaint-eligibility

# Patients have this many hours after the call completes to rate or file a
# complaint. After the window closes the "Rate" button disappears in the app and
# the escrow auto-release proceeds (no late disputes can block the doctor's pay).
COMPLAINT_WINDOW_HOURS = int(os.getenv("COMPLAINT_WINDOW_HOURS", "2"))


def _parse_dt(value) -> Optional[datetime]:
    """Parse an ISO timestamp (or datetime) into a tz-aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Schemas ───────────────────────────────────────────────────────────────────

class SubmitReviewRequest(BaseModel):
    rating:         int
    comment:        Optional[str] = None
    complaint:      Optional[str] = None   # filled when patient reports an issue
    request_refund: bool = False
    call_happened:  bool = True            # "Did the consultation actually take place?"

    @field_validator("rating")
    @classmethod
    def rating_range(cls, v: int) -> int:
        if not (1 <= v <= 5):
            raise ValueError("Rating must be between 1 and 5.")
        return v


# ── Submit review ─────────────────────────────────────────────────────────────

@router.post("/{appointment_id}")
async def submit_review(
    appointment_id: str,
    body:           SubmitReviewRequest,
    current_user:   dict = Depends(get_current_user),
):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can submit reviews.")

    appt = get_doc(COL_APPOINTMENTS, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt["patient_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="This appointment does not belong to you.")
    if appt.get("status") != "completed":
        raise HTTPException(status_code=400, detail="You can only review completed appointments.")

    # Enforce the rating/complaint window. Patients have COMPLAINT_WINDOW_HOURS
    # after the call completes to rate or report an issue; afterwards the window
    # is closed and the escrow auto-releases to the doctor.
    completed_at = _parse_dt(appt.get("completed_at"))
    if completed_at is not None:
        deadline = completed_at + timedelta(hours=COMPLAINT_WINDOW_HOURS)
        if datetime.now(timezone.utc) > deadline:
            raise HTTPException(
                status_code=400,
                detail="The window to rate or report this consultation has expired.",
            )

    # Prevent duplicate reviews
    existing = (
        _col(COL_REVIEWS)
        .where("appointment_id", "==", appointment_id)
        .limit(1)
        .stream()
    )
    if any(True for _ in existing):
        raise HTTPException(status_code=409, detail="You have already reviewed this appointment.")

    now              = datetime.now(timezone.utc).isoformat()
    duration_seconds = appt.get("call_duration_seconds", 0)
    flagged_short    = appt.get("flagged_short_call", False) or (
        0 < duration_seconds < SHORT_CALL_SECONDS
    )

    # A complaint is raised when: the patient says the call did NOT happen,
    # OR explicit complaint text, OR a refund is requested on a short call.
    is_complaint = (
        not body.call_happened
        or bool(body.complaint)
        or (body.request_refund and flagged_short)
    )

    review_doc = {
        "call_happened":         body.call_happened,
        "appointment_id":        appointment_id,
        "doctor_id":             appt["doctor_id"],
        "doctor_name":           appt.get("doctor_name", ""),
        "patient_id":            current_user["user_id"],
        "patient_name":          current_user.get("name", "Patient"),
        "rating":                body.rating,
        "comment":               body.comment or "",
        "complaint":             body.complaint or ("Patient reported the consultation did not take place." if not body.call_happened else ""),
        "request_refund":        body.request_refund,
        "is_complaint":          is_complaint,
        "call_duration_seconds": duration_seconds,
        "flagged_short_call":    flagged_short,
        "amount_pkr":            appt.get("price_pkr", 0),
        "safepay_token":         appt.get("safepay_token", ""),
        "complaint_status":      "pending" if is_complaint else "na",
        "created_at":            now,
    }

    _col(COL_REVIEWS).add(review_doc)

    # NOTE: the doctor's public/aggregate rating is NOT recomputed here. It is
    # refreshed on a weekly schedule (services/ratings.py, every Monday 00:00 UTC)
    # so the doctor dashboard and patient search show the same aligned snapshot
    # and a single review can't instantly swing a doctor's listing.

    # Mark appointment so the "Rate" button disappears in the app. If a complaint
    # was filed, freeze the escrow as "disputed" so the auto-release scheduler
    # will NOT pay the doctor until an admin resolves it.
    appt_update = {"has_review": True}
    if is_complaint:
        appt_update["escrow_status"] = "disputed"
    set_doc(COL_APPOINTMENTS, appointment_id, appt_update)

    # Notify admins so a complaint doesn't sit unseen.
    if is_complaint:
        try:
            for admin_doc in _col(COL_USERS).where("role", "==", "admin").stream():
                await send_push_notification(
                    user_id = admin_doc.id,
                    title   = "New Complaint Filed",
                    body    = f"{current_user.get('name', 'A patient')} filed a complaint on appointment {appointment_id[:8]}.",
                    data    = {"type": "complaint_filed", "appointment_id": appointment_id},
                )
        except Exception:
            pass

    # Push notification to doctor
    if body.rating >= 4:
        notif = f"{current_user.get('name', 'A patient')} gave you {body.rating} stars!"
    elif body.rating <= 2:
        notif = f"{current_user.get('name', 'A patient')} left a review — check your dashboard."
    else:
        notif = f"{current_user.get('name', 'A patient')} completed a review for your consultation."

    await send_push_notification(
        user_id = appt["doctor_id"],
        title   = "New Review",
        body    = notif,
        data    = {
            "type":           "new_review",
            "appointment_id": appointment_id,
            "rating":         str(body.rating),
        },
    )

    if is_complaint:
        logger.warning(
            "Complaint filed: appointment=%s doctor=%s duration=%ds refund=%s",
            appointment_id, appt["doctor_id"], duration_seconds, body.request_refund,
        )

    return {
        "success":          True,
        "message": (
            "Review submitted. Your complaint has been filed and will be reviewed within 24 hours."
            if is_complaint
            else "Review submitted. Thank you for your feedback."
        ),
        "rating":           body.rating,
        "is_complaint":     is_complaint,
        "refund_requested": body.request_refund,
    }


# ── Get review for one appointment ───────────────────────────────────────────

@router.get("/{appointment_id}")
async def get_review(
    appointment_id: str,
    current_user:   dict = Depends(get_current_user),
):
    appt = get_doc(COL_APPOINTMENTS, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    uid = current_user["user_id"]
    if appt["patient_id"] != uid and appt["doctor_id"] != uid:
        raise HTTPException(status_code=403, detail="Access denied.")

    docs = (
        _col(COL_REVIEWS)
        .where("appointment_id", "==", appointment_id)
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.to_dict()
    return None


# ── Get all reviews for a doctor (public — any authenticated user) ────────────

@router.get("/doctor/{doctor_id}")
async def get_doctor_reviews(
    doctor_id:    str,
    current_user: dict = Depends(get_current_user),
):
    docs    = _col(COL_REVIEWS).where("doctor_id", "==", doctor_id).stream()
    reviews = [d.to_dict() for d in docs]
    reviews.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    avg = round(sum(r["rating"] for r in reviews) / len(reviews), 1) if reviews else 0.0

    return {
        "doctor_id":  doctor_id,
        "reviews":    reviews,
        "total":      len(reviews),
        "avg_rating": avg,
    }
