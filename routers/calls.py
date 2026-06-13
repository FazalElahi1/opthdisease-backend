from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone, timedelta

from models.appointment_schemas import (
    JoinCallRequest, CallTokenResponse,
    EndCallRequest, AppointmentStatus,
)
from services.firebase import get_doc, set_doc, COL_APPOINTMENTS, COL_PAYMENT_SESSIONS
from services.agora import generate_call_token
from services.notifications import send_push_notification, get_current_user

router = APIRouter(prefix="/calls", tags=["Video Calls"])

EARLY_JOIN_MINUTES = 5
LATE_JOIN_MINUTES  = 10


def get_appointment_or_404(appointment_id: str) -> dict:
    appt = get_doc(COL_APPOINTMENTS, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    return appt


# ── Join call 

@router.post("/join", response_model=CallTokenResponse)
async def join_call(
    body:         JoinCallRequest,
    current_user: dict = Depends(get_current_user),
):
    
    appt = get_appointment_or_404(body.appointment_id)
    uid  = current_user["user_id"]

    # ── 1. Access check 
    is_patient = appt["patient_id"] == uid
    is_doctor  = appt["doctor_id"]  == uid

    if not is_patient and not is_doctor:
        raise HTTPException(
            status_code=403,
            detail="You are not a participant of this appointment.",
        )

    # ── 2. Status check 
    if appt["status"] == AppointmentStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="This appointment has been cancelled.")
    if appt["status"] == AppointmentStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="This appointment has already been completed.")
    if appt["status"] == AppointmentStatus.MISSED:
        raise HTTPException(status_code=400, detail="This appointment was missed. Please book a new one.")
    if appt["status"] == AppointmentStatus.PENDING:
        raise HTTPException(status_code=402, detail="Payment for this appointment is not confirmed yet.")

    # ── 3. Time window check 
    now = datetime.now(timezone.utc)

    slot_start = datetime.strptime(
        f"{appt['slot_date']} {appt['slot_start_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)

    slot_end = datetime.strptime(
        f"{appt['slot_date']} {appt['slot_end_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)

    earliest_join = slot_start - timedelta(minutes=EARLY_JOIN_MINUTES)

    if now < earliest_join:
        minutes_until = int((earliest_join - now).total_seconds() / 60)
        raise HTTPException(
            status_code=425,
            detail=(
                f"Your appointment starts at {appt['slot_start_time']}. "
                f"You can join {EARLY_JOIN_MINUTES} minutes before. "
                f"Please wait {minutes_until} more minute(s)."
            ),
        )

    if now > slot_end:
        
        set_doc(COL_APPOINTMENTS, body.appointment_id, {"status": AppointmentStatus.MISSED})
        # ── END CHANGE 4 
        raise HTTPException(
            status_code=410,
            detail=(
                f"Your appointment slot ended at {appt['slot_end_time']}. "
                "Please book a new appointment."
            ),
        )

    # ── 4. Patient-specific: check doctor isn't already in another call ────────
    if is_patient:
        # ── CHANGE 5: replace raw db.collection("appointments") query ─────────
        # OLD: db = get_firestore_client()
        #      active = db.collection("appointments").where(...).stream()
        #
        # NEW: We need a raw query here because get_doc only fetches by ID.
        # Use _col() from firebase directly for the compound where query.
        # Import _col and COL_APPOINTMENTS at top — _col is the internal helper
        # that routes through app_data/meta, so this is namespace-safe.
        from services.firebase import _col
        active_docs = (
            _col(COL_APPOINTMENTS)
            .where("doctor_id", "==", appt["doctor_id"])
            .where("status",    "==", AppointmentStatus.ACTIVE)
            .limit(1)
            .stream()
        )
        for active_appt in active_docs:
            active_doc = active_appt.to_dict()
            if active_doc["appointment_id"] != body.appointment_id:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "The doctor is currently in another consultation. "
                        "Please wait — they will be available shortly."
                    ),
                )
        # ── END CHANGE 5 ───────────────────────────────────────────────────────

    # ── 5. Generate Agora token ────────────────────────────────────────────────
    agora_uid = abs(hash(uid)) % (2**31)

    try:
        token_data = generate_call_token(
            channel_name = appt["channel_name"],
            uid          = agora_uid,
        )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── 6. Mark appointment active when patient joins ─────────────────────────
    if is_patient and appt["status"] == AppointmentStatus.CONFIRMED:
        set_doc(COL_APPOINTMENTS, body.appointment_id, {
            "status":         AppointmentStatus.ACTIVE,
            "call_started_at": datetime.now(timezone.utc).isoformat(),
        })
        await send_push_notification(
            user_id = appt["doctor_id"],
            title   = "📞 Patient is Joining",
            body    = f"{appt['patient_name']} has joined the call. Please join now.",
            data    = {
                "type":           "patient_joining",
                "appointment_id": body.appointment_id,
                "channel_name":   appt["channel_name"],
            },
        )

    return CallTokenResponse(
        **token_data,
        appointment_id = body.appointment_id,
    )


# ── End call ───────────────────────────────────────────────────────────────────

@router.post("/end", status_code=200)
async def end_call(
    body:         EndCallRequest,
    current_user: dict = Depends(get_current_user),
):
    appt = get_appointment_or_404(body.appointment_id)
    uid  = current_user["user_id"]

    if appt["patient_id"] != uid and appt["doctor_id"] != uid:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Compute how long the call actually lasted
    call_started_at  = appt.get("call_started_at")
    duration_seconds = 0
    if call_started_at:
        try:
            started = datetime.fromisoformat(call_started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            duration_seconds = int((datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            pass

    # Booked duration from slot times
    try:
        _s = datetime.strptime(f"{appt['slot_date']} {appt['slot_start_time']}", "%Y-%m-%d %H:%M")
        _e = datetime.strptime(f"{appt['slot_date']} {appt['slot_end_time']}",   "%Y-%m-%d %H:%M")
        booked_seconds = int((_e - _s).total_seconds())
    except Exception:
        booked_seconds = 0

    # Flag if doctor delivered less than 50% of the booked slot duration
    flagged_short = (
        duration_seconds > 0
        and booked_seconds > 0
        and duration_seconds < int(booked_seconds * 0.50)
    )

    # Begin escrow: hold the funds until the cooling period elapses with no
    # complaint. release_at was computed at payment time (slot end + cooling).
    payment = get_doc(COL_PAYMENT_SESSIONS, appt.get("safepay_token", "")) or {}
    release_at = payment.get("release_at")

    set_doc(COL_APPOINTMENTS, body.appointment_id, {
        "status":                  AppointmentStatus.COMPLETED,
        "completed_at":            datetime.now(timezone.utc).isoformat(),
        "call_duration_seconds":   duration_seconds,
        "booked_duration_seconds": booked_seconds,
        "flagged_short_call":      flagged_short,
        "escrow_status":           "held",
        "release_at":              release_at,
    })

    notify_id = appt["doctor_id"] if uid == appt["patient_id"] else appt["patient_id"]
    await send_push_notification(
        user_id = notify_id,
        title   = "✅ Consultation Completed",
        body    = f"Your consultation on {appt['slot_date']} has ended.",
        data    = {"type": "call_ended", "appointment_id": body.appointment_id},
    )

    return {"message": "Call ended. Appointment marked as completed."}