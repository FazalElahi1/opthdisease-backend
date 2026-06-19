from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel

# ── CHANGE 1: imports ──────────────────────────────────────────────────────────
# OLD:  from services.firebase import get_firestore_client, get_user_doc,
#                                     set_user_doc, set_doctor_doc
#       from services.jwt import decode_access_token
#
# NEW:  - Add _col, COL_* constants for namespace-safe collection access.
#       - Add get_doctor_doc so approve_doctor reads via the helper.
#       - Keep decode_access_token for get_admin_user (admin has its own gate,
#         not the shared get_current_user, so we keep it separate).
#       - Drop get_firestore_client — no longer called directly.
from google.cloud.firestore_v1.base_query import FieldFilter
from services.firebase import (
    _col,
    COL_USERS, COL_DOCTORS, COL_APPOINTMENTS, COL_SCANS,
    COL_PAYOUTS, COL_REVIEWS, COL_PAYMENT_SESSIONS, COL_PAYMENT_LOGS,
    get_user_doc, set_user_doc,
    get_doctor_doc, set_doctor_doc,
    get_doc, set_doc,
)
from services.notifications import send_push_notification
from services.email_service import send_doctor_approved, send_doctor_rejected
from services.jwt import decode_access_token

router = APIRouter(prefix="/admin", tags=["Admin"])
bearer = HTTPBearer()

ADMIN_EMAILS = [
    "f.elahi1767@gmail.com",
]


# ── CHANGE 3: get_admin_user — add session_token + is_active checks ───────────
# OLD CODE:
#   def get_admin_user(...):
#       payload  = decode_access_token(credentials.credentials)
#       user_id  = payload["sub"]
#       user_doc = get_user_doc(user_id)
#       if not user_doc:
#           raise HTTPException(404, "User not found.")
#       if user_doc.get("email") not in ADMIN_EMAILS and user_doc.get("role") != "admin":
#           raise HTTPException(403, "Admin access required.")
#       return {**user_doc, "user_id": user_id}
#
# NEW CODE: same admin email/role gate, but now also validates session_token
# (so admin logout is respected) and is_active (so a deactivated admin can't
# access the panel even with a still-valid JWT).
def get_admin_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")

    user_doc = get_user_doc(user_id)
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found.")

    # Session token check — logout enforcement
    jwt_session    = payload.get("session_token", "")
    stored_session = user_doc.get("session_token", "")
    if not jwt_session or jwt_session != stored_session:
        raise HTTPException(
            status_code=401,
            detail="Session expired. Please log in again.",
        )

    # is_active check — deactivation enforcement
    if not user_doc.get("is_active", True):
        raise HTTPException(
            status_code=403,
            detail="Your account has been deactivated. Please contact support.",
        )

    # Admin gate
    if user_doc.get("email") not in ADMIN_EMAILS and user_doc.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")

    return {**user_doc, "user_id": user_id}
# ── END CHANGE 3 ───────────────────────────────────────────────────────────────


# ── Schemas ────────────────────────────────────────────────────────────────────

class ApproveDoctorRequest(BaseModel):
    doctor_id: str
    approved:  bool
    notes:     Optional[str] = None   # sent by frontend
    reason:    Optional[str] = None   # legacy alias


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(admin: dict = Depends(get_admin_user)):
    users_docs    = list(_col(COL_USERS).stream())
    doctors_docs  = list(_col(COL_DOCTORS).stream())
    scans_docs    = list(_col(COL_SCANS).stream())
    appts_docs    = list(_col(COL_APPOINTMENTS).stream())
    payment_docs  = list(_col(COL_PAYMENT_SESSIONS).stream())
    payout_docs   = list(_col(COL_PAYOUTS).stream())

    # ── Users ──────────────────────────────────────────────────────────────────
    total_users    = len(users_docs)
    total_patients = sum(1 for d in users_docs if d.to_dict().get("role") == "patient")
    active_users   = sum(1 for d in users_docs if d.to_dict().get("is_active", True))

    # ── Doctors ────────────────────────────────────────────────────────────────
    total_doctors  = len(doctors_docs)
    verified_docs  = sum(1 for d in doctors_docs if d.to_dict().get("license_status") == "verified")
    pending_docs   = sum(1 for d in doctors_docs if d.to_dict().get("license_status") == "pending")
    rejected_docs  = sum(1 for d in doctors_docs if d.to_dict().get("license_status") == "rejected")

    # ── Appointments (single pass) ─────────────────────────────────────────────
    total_appointments     = 0
    completed_appointments = 0
    cancelled_appointments = 0
    active_appointments    = 0
    missed_appointments    = 0
    pending_appointments   = 0
    flagged_short_calls    = 0
    total_revenue          = 0
    total_dur_seconds      = 0
    dur_count              = 0

    for doc in appts_docs:
        a      = doc.to_dict()
        status = a.get("status", "")
        total_appointments += 1

        if status == "completed":
            completed_appointments += 1
            total_revenue += a.get("price_pkr", 0)
            dur = a.get("call_duration_seconds", 0)
            if dur > 0:
                total_dur_seconds += dur
                dur_count += 1
            if a.get("flagged_short_call"):
                flagged_short_calls += 1
        elif status == "cancelled":
            cancelled_appointments += 1
        elif status == "active":
            active_appointments += 1
        elif status == "missed":
            missed_appointments += 1
        elif status == "pending":
            pending_appointments += 1

    avg_call_dur_min = round(total_dur_seconds / 60 / dur_count, 1) if dur_count else 0

    # ── Payments ───────────────────────────────────────────────────────────────
    total_payments      = len(payment_docs)
    successful_payments = 0
    refunded_payments   = 0
    pending_payments    = 0
    failed_payments     = 0

    for doc in payment_docs:
        s = doc.to_dict().get("status", "")
        if s == "paid":
            successful_payments += 1
        elif s == "refunded":
            refunded_payments += 1
        elif s == "pending":
            pending_payments += 1
        elif s == "failed":
            failed_payments += 1

    payment_success_rate = (
        round(successful_payments / total_payments * 100, 1) if total_payments else 0
    )
    refund_rate = (
        round(refunded_payments / successful_payments * 100, 1) if successful_payments else 0
    )

    # ── Payouts ────────────────────────────────────────────────────────────────
    total_withdrawn_pkr = sum(d.to_dict().get("amount_pkr", 0) for d in payout_docs)

    return {
        # Users & Doctors
        "total_users":              total_users,
        "total_patients":           total_patients,
        "active_users":             active_users,
        "total_doctors":            total_doctors,
        "verified_doctors":         verified_docs,
        "pending_doctors":          pending_docs,
        "rejected_doctors":         rejected_docs,
        # Scans
        "total_scans":              len(scans_docs),
        # Appointments
        "total_appointments":       total_appointments,
        "completed_appointments":   completed_appointments,
        "cancelled_appointments":   cancelled_appointments,
        "active_appointments":      active_appointments,
        "missed_appointments":      missed_appointments,
        "pending_appointments":     pending_appointments,
        "flagged_short_calls":      flagged_short_calls,
        "avg_call_duration_min":    avg_call_dur_min,
        # Revenue
        "total_revenue_pkr":        total_revenue,
        "total_withdrawn_pkr":      total_withdrawn_pkr,
        "pending_withdrawal_pkr":   max(0, total_revenue - total_withdrawn_pkr),
        # Payments
        "total_payment_sessions":   total_payments,
        "successful_payments":      successful_payments,
        "refunded_payments":        refunded_payments,
        "pending_payments":         pending_payments,
        "failed_payments":          failed_payments,
        "payment_success_rate_pct": payment_success_rate,
        "refund_rate_pct":          refund_rate,
    }


# ── List all users ─────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    role:  Optional[str] = Query(None),
    admin: dict = Depends(get_admin_user),
):
    # ── CHANGE 6: replace raw db.collection("users") ──────────────────────────
    # OLD:  db = get_firestore_client()
    #       docs = db.collection("users").stream()
    docs = _col(COL_USERS).stream()
    # ── END CHANGE 6 ───────────────────────────────────────────────────────────
    users = []
    for doc in docs:
        d = doc.to_dict()
        d["user_id"] = doc.id
        if role and d.get("role") != role:
            continue
        d.pop("password_hash", None)
        d.pop("session_token", None)     # ← never expose session_token to admin UI
        users.append(d)
    return {"users": users, "total": len(users)}


# ── List doctors ───────────────────────────────────────────────────────────────

@router.get("/doctors")
async def list_all_doctors(
    status: Optional[str] = Query(None),
    admin:  dict = Depends(get_admin_user),
):
    # ── CHANGE 7: replace raw db.collection("doctors") ────────────────────────
    # OLD:  db = get_firestore_client()
    #       docs = db.collection("doctors").stream()
    docs = _col(COL_DOCTORS).stream()
    # ── END CHANGE 7 ───────────────────────────────────────────────────────────
    doctors = []
    for doc in docs:
        d = doc.to_dict()
        d["user_id"] = doc.id
        if status and d.get("license_status") != status:
            continue
        d.pop("password_hash", None)
        d.pop("session_token", None)     # ← never expose session_token to admin UI
        doctors.append(d)
    doctors.sort(key=lambda d: 0 if d.get("license_status") == "pending" else 1)
    return {"doctors": doctors, "total": len(doctors)}


# ── Approve or reject doctor ───────────────────────────────────────────────────

@router.post("/doctors/approve")
async def approve_doctor(
    body:  ApproveDoctorRequest,
    admin: dict = Depends(get_admin_user),
):
    # ── CHANGE 8: replace raw db.collection("doctors") + doctor_ref.update() ──
    # OLD:  db         = get_firestore_client()
    #       doctor_ref = db.collection("doctors").document(body.doctor_id)
    #       doctor_doc = doctor_ref.get()
    #       if not doctor_doc.exists: raise HTTPException(404, ...)
    #       doc_data   = doctor_doc.to_dict()
    #       doctor_ref.update({...})   ← raw update bypasses namespace
    #
    # NEW:  get_doctor_doc reads via the namespace helper.
    #       set_doctor_doc writes via the namespace helper.
    doc_data = get_doctor_doc(body.doctor_id)
    if not doc_data:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    # ── END CHANGE 8 ───────────────────────────────────────────────────────────

    new_status   = "verified" if body.approved else "rejected"
    now          = datetime.now(timezone.utc).isoformat()
    review_notes = body.notes or body.reason or ""

    # Update doctor collection via helper (namespace-safe)
    set_doctor_doc(body.doctor_id, {
        "license_status": new_status,
        "is_verified":    body.approved,
        "reviewed_at":    now,
        "review_notes":   review_notes,
        "reviewed_by":    admin["user_id"],
    })

    # Update users collection via helper (namespace-safe)
    set_user_doc(body.doctor_id, {
        "is_verified":    body.approved,
        "license_status": new_status,
        "reviewed_at":    now,
    })

    doctor_email = doc_data.get("email", "")
    doctor_name  = doc_data.get("name", "Doctor")

    if body.approved:
        send_doctor_approved(doctor_email, doctor_name)
    else:
        send_doctor_rejected(
            doctor_email = doctor_email,
            doctor_name  = doctor_name,
            reason       = review_notes or "Credentials could not be verified.",
        )

    if body.approved:
        await send_push_notification(
            user_id = body.doctor_id,
            title   = "✅ Application Approved!",
            body    = "Your medical license has been verified. You can now log in to the Clinician Portal.",
            data    = {"type": "license_approved"},
        )
    else:
        await send_push_notification(
            user_id = body.doctor_id,
            title   = "❌ Application Rejected",
            body    = review_notes or "Your application was not approved. Please contact support.",
            data    = {"type": "license_rejected", "reason": review_notes},
        )

    return {
        "message":    f"Doctor {new_status} successfully.",
        "doctor_id":  body.doctor_id,
        "status":     new_status,
        "email_sent": bool(doctor_email),
    }


# ── All appointments ───────────────────────────────────────────────────────────

@router.get("/appointments")
async def list_all_appointments(
    status: Optional[str] = Query(None),
    admin:  dict = Depends(get_admin_user),
):
    docs  = _col(COL_APPOINTMENTS).stream()
    appts = []
    for doc in docs:
        a = doc.to_dict()
        if status and a.get("status") != status:
            continue

        # Compute booked duration in minutes for easy admin comparison
        try:
            from datetime import datetime as _dt
            _s = _dt.strptime(f"{a['slot_date']} {a['slot_start_time']}", "%Y-%m-%d %H:%M")
            _e = _dt.strptime(f"{a['slot_date']} {a['slot_end_time']}",   "%Y-%m-%d %H:%M")
            a["booked_duration_minutes"] = round((_e - _s).total_seconds() / 60, 1)
        except Exception:
            a["booked_duration_minutes"] = None

        dur_sec = a.get("call_duration_seconds", 0)
        a["actual_duration_minutes"] = round(dur_sec / 60, 1) if dur_sec else None

        appts.append(a)

    appts.sort(
        key     = lambda a: a.get("slot_date", "") + a.get("slot_start_time", ""),
        reverse = True,
    )
    return {"appointments": appts, "total": len(appts)}


# ── All scans ──────────────────────────────────────────────────────────────────

@router.get("/scans")
async def list_all_scans(
    risk_level: Optional[str] = Query(None),
    admin:      dict = Depends(get_admin_user),
):
    # ── CHANGE 10: replace raw db.collection("scans") ─────────────────────────
    # OLD:  db = get_firestore_client()
    #       docs = db.collection("scans").stream()
    docs = _col(COL_SCANS).stream()
    # ── END CHANGE 10 ──────────────────────────────────────────────────────────
    scans = []
    for doc in docs:
        s = doc.to_dict()
        if risk_level and s.get("riskLevel") != risk_level:
            continue
        scans.append(s)
    scans.sort(key=lambda s: s.get("date", ""), reverse=True)
    return {"scans": scans, "total": len(scans)}


# ── Revenue report ─────────────────────────────────────────────────────────────

@router.get("/revenue")
async def revenue_report(admin: dict = Depends(get_admin_user)):
    # ── CHANGE 11: replace raw db.collection("appointments") ──────────────────
    # OLD:  db = get_firestore_client()
    #       docs = db.collection("appointments").stream()
    docs = _col(COL_APPOINTMENTS).stream()
    # ── END CHANGE 11 ──────────────────────────────────────────────────────────

    by_doctor: dict = {}
    total = 0

    for doc in docs:
        a = doc.to_dict()
        if a.get("status") != "completed":
            continue
        fee      = a.get("price_pkr", 0)
        total   += fee
        doc_id   = a.get("doctor_id", "unknown")
        doc_name = a.get("doctor_name", "Unknown")
        if doc_id not in by_doctor:
            # Admin pays doctors out manually, so surface the doctor's phone number
            # here. It lives on the doctor doc (falling back to the user doc).
            doctor_doc = get_doctor_doc(doc_id) or {}
            phone = doctor_doc.get("phone") or (get_user_doc(doc_id) or {}).get("phone") or ""
            by_doctor[doc_id] = {
                "doctor_name":  doc_name,
                "doctor_phone": phone,
                "total_pkr":    0,
                "appointments": 0,
            }
        by_doctor[doc_id]["total_pkr"]    += fee
        by_doctor[doc_id]["appointments"] += 1

    sorted_doctors = sorted(by_doctor.values(), key=lambda d: d["total_pkr"], reverse=True)
    return {
        "total_revenue_pkr":    total,
        "platform_revenue_pkr": 0,
        "doctor_payouts_pkr":   total,
        "by_doctor":            sorted_doctors,
    }


# ── Payout records ─────────────────────────────────────────────────────────────

@router.get("/payouts")
async def list_all_payouts(admin: dict = Depends(get_admin_user)):
    # ── CHANGE 12: replace raw db.collection("payouts") ───────────────────────
    # OLD:  db = get_firestore_client()
    #       docs = db.collection("payouts").stream()
    docs = _col(COL_PAYOUTS).stream()
    # ── END CHANGE 12 ──────────────────────────────────────────────────────────
    payouts = []
    for d in docs:
        item = d.to_dict()
        item["payout_id"] = d.id
        payouts.append(item)
    payouts.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return {"payouts": payouts, "total": len(payouts)}


# ── Mark a payout request as paid (manual disbursement) ─────────────────────────
# There is no automated disbursement API, so the admin transfers the money to the
# doctor's payout account out-of-band and records it here.

@router.post("/payouts/{payout_id}/mark-paid")
async def mark_payout_paid(payout_id: str, admin: dict = Depends(get_admin_user)):
    ref = _col(COL_PAYOUTS).document(payout_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Payout request not found.")
    payout = snap.to_dict()
    if payout.get("status") == "paid":
        raise HTTPException(status_code=400, detail="This payout is already marked paid.")

    now = datetime.now(timezone.utc).isoformat()
    ref.update({"status": "paid", "paid_at": now, "paid_by": admin["user_id"]})

    # Track lifetime paid-out total on the doctor doc (display only).
    doctor_id = payout.get("doctor_id", "")
    if doctor_id:
        doctor_doc = get_doctor_doc(doctor_id) or {}
        set_doctor_doc(doctor_id, {
            "total_withdrawn_pkr": doctor_doc.get("total_withdrawn_pkr", 0) + payout.get("amount_pkr", 0),
            "last_withdrawal_at":  now,
        })
        await send_push_notification(
            user_id = doctor_id,
            title   = "Payout Sent",
            body    = f"PKR {payout.get('amount_pkr', 0):,} has been transferred to your "
                      f"{payout.get('bank_name', 'payout')} account "
                      f"{payout.get('account_number', '')}.",
            data    = {"type": "payout_paid", "payout_id": payout_id},
        )

    return {"success": True, "payout_id": payout_id, "status": "paid", "paid_at": now}


# ── All payment transactions ───────────────────────────────────────────────────

@router.get("/payments")
async def list_all_payments(
    status: Optional[str] = Query(None, description="Filter: pending, paid, refunded, failed"),
    admin:  dict = Depends(get_admin_user),
):
    """Show every Safepay payment session — which patient paid which doctor, when, how much."""
    docs     = _col(COL_PAYMENT_SESSIONS).stream()
    payments = []
    for doc in docs:
        p = doc.to_dict()
        if status and p.get("status") != status:
            continue
        payments.append(p)
    payments.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return {"payments": payments, "total": len(payments)}


# ── Flagged short calls ────────────────────────────────────────────────────────

@router.get("/flagged-calls")
async def list_flagged_calls(admin: dict = Depends(get_admin_user)):
    """
    List all completed appointments where the doctor delivered less than
    50% of the booked slot duration. Includes booked vs actual duration
    and completion percentage so admin can decide whether to refund.
    """
    docs  = _col(COL_APPOINTMENTS).where(filter=FieldFilter("flagged_short_call", "==", True)).stream()
    calls = []
    for doc in docs:
        a       = doc.to_dict()
        dur_sec = a.get("call_duration_seconds", 0)
        bkd_sec = a.get("booked_duration_seconds", 0)

        # Recompute booked seconds from slot times if not stored
        if not bkd_sec:
            try:
                from datetime import datetime as _dt
                _s = _dt.strptime(f"{a['slot_date']} {a['slot_start_time']}", "%Y-%m-%d %H:%M")
                _e = _dt.strptime(f"{a['slot_date']} {a['slot_end_time']}",   "%Y-%m-%d %H:%M")
                bkd_sec = int((_e - _s).total_seconds())
            except Exception:
                bkd_sec = 0

        completion_pct = round(dur_sec / bkd_sec * 100, 1) if bkd_sec else 0

        calls.append({
            **a,
            "booked_duration_minutes": round(bkd_sec / 60, 1),
            "actual_duration_minutes": round(dur_sec / 60, 1),
            "completion_pct":          completion_pct,
            "refund_issued":           a.get("refund_issued", False),
        })

    calls.sort(key=lambda c: c.get("completed_at", ""), reverse=True)
    return {"flagged_calls": calls, "total": len(calls)}


# ── Admin direct refund ────────────────────────────────────────────────────────

class AdminRefundRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/appointments/{appointment_id}/refund")
async def admin_refund_appointment(
    appointment_id: str,
    body:           AdminRefundRequest,
    admin:          dict = Depends(get_admin_user),
):
    """
    Admin issues a direct refund on any paid appointment — no complaint required.
    Typically used after reviewing a flagged short call.
    """
    appt = get_doc(COL_APPOINTMENTS, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    if appt.get("refund_issued"):
        raise HTTPException(status_code=400, detail="A refund has already been issued for this appointment.")

    safepay_token = appt.get("safepay_token")
    if not safepay_token:
        raise HTTPException(status_code=400, detail="No payment token on this appointment. Cannot refund.")

    session = _col(COL_PAYMENT_SESSIONS).document(safepay_token).get().to_dict()
    if not session:
        raise HTTPException(status_code=404, detail="Payment session not found.")
    if session.get("status") == "refunded":
        raise HTTPException(status_code=400, detail="This payment has already been refunded.")
    if session.get("status") != "paid":
        raise HTTPException(status_code=400, detail="Only paid appointments can be refunded.")

    now = datetime.now(timezone.utc).isoformat()
    amount_pkr = session.get("amount_pkr", 0)

    # Record the refund. JazzCash sandbox has no self-service reversal API, so the
    # refund is recorded here and the actual money is returned by the admin
    # out-of-band (consistent with the manual-payout model).
    _col(COL_PAYMENT_SESSIONS).document(safepay_token).update({
        "status":      "refunded",
        "refunded_at": now,
    })

    set_doc(COL_APPOINTMENTS, appointment_id, {
        "refund_issued":  True,
        "refund_reason":  body.reason or "Admin-initiated refund",
        "refunded_at":    now,
        "refunded_by":    admin["user_id"],
        "escrow_status":  "refunded",
    })

    patient_id = appt.get("patient_id", "")
    if patient_id:
        await send_push_notification(
            user_id = patient_id,
            title   = "Refund Issued",
            body    = (
                f"Your refund of PKR {amount_pkr:,} for your appointment with "
                f"{appt.get('doctor_name', 'the doctor')} has been processed. "
                "Funds arrive within 3–5 business days."
            ),
            data    = {"type": "refund_issued", "appointment_id": appointment_id},
        )

    # Notify the doctor their payment was withheld for this appointment.
    doctor_id = appt.get("doctor_id", "")
    if doctor_id:
        await send_push_notification(
            user_id = doctor_id,
            title   = "Payment Withheld",
            body    = (
                f"PKR {amount_pkr:,} for appointment {appointment_id[:8]} was refunded to the "
                f"patient and will not be paid to you. Reason: {body.reason or 'Admin-initiated refund'}."
            ),
            data    = {"type": "payment_withheld", "appointment_id": appointment_id},
        )

    return {
        "success":        True,
        "appointment_id": appointment_id,
        "amount_pkr":     amount_pkr,
        "refunded_at":    now,
        "reason":         body.reason or "Admin-initiated refund",
    }


# ── Deactivate user ────────────────────────────────────────────────────────────

@router.post("/users/deactivate")
async def deactivate_user(
    user_id: str,
    admin:   dict = Depends(get_admin_user),
):
    # ── CHANGE 13: add session_token wipe so deactivation takes effect NOW ─────
    # OLD CODE:
    #   set_user_doc(user_id, {"is_active": False})
    #   return {"message": "User deactivated.", "user_id": user_id}
    #
    # PROBLEM: is_active was set to False, but the user's JWT was still valid
    # for up to 7 days. get_current_user() now checks is_active, but only if
    # it also reads fresh data from Firestore. Wiping session_token here means
    # the NEXT request from this user fails the session_token check immediately,
    # even before is_active is read. Belt-and-suspenders: both checks fail.
    user_doc = get_user_doc(user_id)
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found.")

    set_user_doc(user_id, {
        "is_active":     False,
        "session_token": "",       # ← invalidates all existing JWTs immediately
    })
    # ── END CHANGE 13 ──────────────────────────────────────────────────────────

    return {"message": "User deactivated.", "user_id": user_id}


# ── Reactivate user (new — symmetric to deactivate) ───────────────────────────
# ── CHANGE 14: NEW endpoint ────────────────────────────────────────────────────
# The old code had deactivate but no reactivate. Without this, a deactivated
# user can never log in again — they can't even get a new session_token because
# the login endpoint checks is_active before issuing a token.
# Admin must be able to reverse a deactivation.

@router.post("/users/reactivate")
async def reactivate_user(
    user_id: str,
    admin:   dict = Depends(get_admin_user),
):
    user_doc = get_user_doc(user_id)
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found.")

    set_user_doc(user_id, {"is_active": True})
    return {"message": "User reactivated.", "user_id": user_id}


# ── Complaints / reviews ───────────────────────────────────────────────────────

@router.get("/complaints")
async def list_complaints(
    status: Optional[str] = Query(None),
    admin:  dict = Depends(get_admin_user),
):
    """
    List all complaints. Each complaint is enriched with the appointment's
    call duration vs booked duration so admin can make a data-driven refund decision
    without needing to look up the appointment separately.
    """
    docs = _col(COL_REVIEWS).where(filter=FieldFilter("is_complaint", "==", True)).stream()
    complaints = []
    for doc in docs:
        c = doc.to_dict()
        c["review_id"] = doc.id
        if status and c.get("complaint_status") != status:
            continue

        # Join call duration data from the appointment
        appt_id = c.get("appointment_id")
        if appt_id:
            appt = get_doc(COL_APPOINTMENTS, appt_id)
            if appt:
                dur_sec = appt.get("call_duration_seconds", 0)
                bkd_sec = appt.get("booked_duration_seconds", 0)
                if not bkd_sec:
                    try:
                        from datetime import datetime as _dt
                        _s = _dt.strptime(f"{appt['slot_date']} {appt['slot_start_time']}", "%Y-%m-%d %H:%M")
                        _e = _dt.strptime(f"{appt['slot_date']} {appt['slot_end_time']}",   "%Y-%m-%d %H:%M")
                        bkd_sec = int((_e - _s).total_seconds())
                    except Exception:
                        bkd_sec = 0
                c["call_data"] = {
                    "slot_date":               appt.get("slot_date"),
                    "slot_start_time":         appt.get("slot_start_time"),
                    "slot_end_time":           appt.get("slot_end_time"),
                    "booked_duration_minutes": round(bkd_sec / 60, 1),
                    "actual_duration_minutes": round(dur_sec / 60, 1) if dur_sec else None,
                    "completion_pct":          round(dur_sec / bkd_sec * 100, 1) if bkd_sec and dur_sec else None,
                    "flagged_short_call":      appt.get("flagged_short_call", False),
                    "amount_pkr":              appt.get("price_pkr", 0),
                    "safepay_token":           appt.get("safepay_token"),
                    "refund_issued":           appt.get("refund_issued", False),
                }

        complaints.append(c)

    complaints.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return {"complaints": complaints, "total": len(complaints)}


class PayDoctorRequest(BaseModel):
    doctor_id:  str
    amount_pkr: int


@router.get("/doctors/payable")
async def list_payable_doctors(admin: dict = Depends(get_admin_user)):
    """List verified doctors with available balance and a saved payout account."""
    from routers.payouts import _compute_balance
    docs   = _col(COL_DOCTORS).stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        if not d.get("payout_account_number"):
            continue
        bal = _compute_balance(doc.id)
        if bal["available_pkr"] <= 0:
            continue
        number = d.get("payout_account_number", "")
        result.append({
            "doctor_id":      doc.id,
            "doctor_name":    d.get("name", ""),
            "bank_name":      d.get("payout_bank_name", ""),
            "account_number": number,
            "account_holder": d.get("payout_account_holder", ""),
            "account_last4":  number[-4:] if number else "",
            "available_pkr":  bal["available_pkr"],
        })
    result.sort(key=lambda d: d["available_pkr"], reverse=True)
    return {"doctors": result, "total": len(result)}


@router.post("/payouts/pay-doctor")
async def pay_doctor_directly(body: PayDoctorRequest, admin: dict = Depends(get_admin_user)):
    """Admin initiates and immediately marks a payout as paid — no doctor request needed."""
    from routers.payouts import _compute_balance

    doctor_doc = get_doctor_doc(body.doctor_id)
    if not doctor_doc:
        raise HTTPException(status_code=404, detail="Doctor not found.")

    number = doctor_doc.get("payout_account_number")
    if not number:
        raise HTTPException(status_code=400, detail="Doctor has no payout account saved.")

    if body.amount_pkr < 1:
        raise HTTPException(status_code=400, detail="Amount must be at least PKR 1.")

    bal = _compute_balance(body.doctor_id)
    if body.amount_pkr > bal["available_pkr"]:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient available balance. Available: PKR {bal['available_pkr']:,}.",
        )

    now    = datetime.now(timezone.utc).isoformat()
    bank   = doctor_doc.get("payout_bank_name", "")
    holder = doctor_doc.get("payout_account_holder", doctor_doc.get("name", "Doctor"))

    payout_ref = _col(COL_PAYOUTS).add({
        "doctor_id":      body.doctor_id,
        "doctor_name":    doctor_doc.get("name", ""),
        "amount_pkr":     body.amount_pkr,
        "account_holder": holder,
        "bank_name":      bank,
        "account_number": number,
        "account_last4":  number[-4:],
        "status":         "paid",
        "created_at":     now,
        "paid_at":        now,
        "paid_by":        admin["user_id"],
        "initiated_by":   "admin",
    })
    payout_id = payout_ref[1].id if isinstance(payout_ref, tuple) else payout_ref.id

    set_doctor_doc(body.doctor_id, {
        "total_withdrawn_pkr": doctor_doc.get("total_withdrawn_pkr", 0) + body.amount_pkr,
        "last_withdrawal_at":  now,
    })

    await send_push_notification(
        user_id = body.doctor_id,
        title   = "Payout Sent",
        body    = f"PKR {body.amount_pkr:,} has been transferred to your {bank} account {number}.",
        data    = {"type": "payout_paid", "payout_id": payout_id},
    )

    return {
        "success":    True,
        "payout_id":  payout_id,
        "amount_pkr": body.amount_pkr,
        "status":     "paid",
        "paid_at":    now,
    }


class ResolveComplaintRequest(BaseModel):
    resolution:   str            # "refunded", "dismissed"
    notes:        Optional[str] = None
    issue_refund: bool = False   # True → call Safepay refund API


@router.post("/complaints/{appointment_id}/resolve")
async def resolve_complaint(
    appointment_id: str,
    body:           ResolveComplaintRequest,
    admin:          dict = Depends(get_admin_user),
):
    # Find the complaint review
    docs = (
        _col(COL_REVIEWS)
        .where(filter=FieldFilter("appointment_id", "==", appointment_id))
        .where(filter=FieldFilter("is_complaint",   "==", True))
        .limit(1)
        .stream()
    )
    review_doc = None
    review_ref = None
    for doc in docs:
        review_doc = doc.to_dict()
        review_ref = doc.reference
        break

    if not review_doc:
        raise HTTPException(status_code=404, detail="Complaint not found for this appointment.")
    if review_doc.get("complaint_status") not in ("pending",):
        raise HTTPException(
            status_code=400,
            detail=f"Complaint already resolved as '{review_doc['complaint_status']}'.",
        )

    now = datetime.now(timezone.utc).isoformat()

    # Resolve the escrow based on the decision:
    #   refund    → patient refunded, doctor not paid  (escrow_status=refunded)
    #   dismissed → complaint rejected, doctor paid     (escrow_status=released)
    if body.issue_refund:
        safepay_token = review_doc.get("safepay_token", "")
        if not safepay_token:
            raise HTTPException(
                status_code=400,
                detail="No payment token on this appointment. Cannot refund automatically.",
            )
        _col(COL_PAYMENT_SESSIONS).document(safepay_token).update({
            "status": "refunded", "refunded_at": now,
        })
        set_doc(COL_APPOINTMENTS, appointment_id, {
            "escrow_status": "refunded", "refunded_at": now,
        })
    else:
        # Complaint dismissed — release the held funds to the doctor.
        set_doc(COL_APPOINTMENTS, appointment_id, {
            "escrow_status": "released", "released_at": now,
        })

    # Mark the review resolved
    review_ref.update({
        "complaint_status": body.resolution,
        "resolution_notes": body.notes or "",
        "resolved_by":      admin["user_id"],
        "resolved_at":      now,
        "refund_issued":    body.issue_refund,
    })

    # Notify patient
    patient_id = review_doc.get("patient_id", "")
    if patient_id:
        if body.issue_refund:
            notif_body = (
                "Your refund has been approved. "
                "The amount will be returned within 3–5 business days."
            )
        else:
            notif_body = (
                "Your complaint has been reviewed. "
                + (body.notes or "Thank you for your feedback.")
            )
        await send_push_notification(
            user_id = patient_id,
            title   = "Complaint Update",
            body    = notif_body,
            data    = {
                "type":           "complaint_resolved",
                "appointment_id": appointment_id,
                "refund_issued":  str(body.issue_refund),
            },
        )

    # Notify the doctor of the outcome — whether their payment was withheld
    # (refund issued) or released (complaint dismissed). Without this the doctor
    # would just see the money silently vanish from / appear in their balance.
    doctor_id  = review_doc.get("doctor_id", "")
    amount_pkr = review_doc.get("amount_pkr", 0)
    if doctor_id:
        if body.issue_refund:
            doc_title = "Payment Withheld"
            doc_body  = (
                f"A complaint on appointment {appointment_id[:8]} was upheld, so "
                f"PKR {amount_pkr:,} was refunded to the patient and will not be paid to you. "
                + (body.notes or "Contact support if you believe this is a mistake.")
            )
        else:
            doc_title = "Complaint Dismissed"
            doc_body  = (
                f"A complaint on appointment {appointment_id[:8]} was dismissed. "
                f"Your PKR {amount_pkr:,} has been released and is now available to withdraw."
            )
        await send_push_notification(
            user_id = doctor_id,
            title   = doc_title,
            body    = doc_body,
            data    = {
                "type":           "complaint_resolved_doctor",
                "appointment_id": appointment_id,
                "refund_issued":  str(body.issue_refund),
            },
        )

    return {
        "success":        True,
        "resolution":     body.resolution,
        "refund_issued":  body.issue_refund,
        "appointment_id": appointment_id,
    }