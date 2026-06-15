from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone, timedelta
import uuid

from models.appointment_schemas import (
    BookAppointmentRequest, AppointmentResponse,
    AppointmentListResponse, AppointmentStatus,
    DoctorSlotsResponse, SaveFCMTokenRequest,
)
from services.firebase import (
    get_doctor_doc,
    _col, COL_APPOINTMENTS, COL_PAYMENT_SESSIONS,
)
from services.notifications import send_push_notification, save_fcm_token, get_current_user

router = APIRouter(prefix="/appointments", tags=["Appointments"])

CALL_EARLY_JOIN_MINUTES = 5


def can_join_call(slot_date: str, start_time: str, end_time: str) -> bool:
    now = datetime.now(timezone.utc)
    slot_start = datetime.strptime(
        f"{slot_date} {start_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)
    slot_end = datetime.strptime(
        f"{slot_date} {end_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)
    earliest_join = slot_start - timedelta(minutes=CALL_EARLY_JOIN_MINUTES)
    return earliest_join <= now <= slot_end


def appointment_to_response(doc: dict) -> AppointmentResponse:
    # Normalise field names — old docs used slot_start/slot_end
    slot_start = doc.get("slot_start_time") or doc.get("slot_start", "00:00")
    slot_end   = doc.get("slot_end_time")   or doc.get("slot_end",   "00:00")
    joinable = (
        doc.get("status") in (AppointmentStatus.CONFIRMED, AppointmentStatus.ACTIVE)
        and can_join_call(doc.get("slot_date", ""), slot_start, slot_end)
    )
    return AppointmentResponse(**{
        **doc,
        "slot_start_time": slot_start,
        "slot_end_time":   slot_end,
        "channel_name":    doc.get("channel_name") or doc.get("appointment_id", ""),
        "can_join":        joinable,
    })


# ── Save FCM token ─────────────────────────────────────────────────────────────

@router.post("/fcm-token", status_code=200)
async def update_fcm_token(
    body:         SaveFCMTokenRequest,
    current_user: dict = Depends(get_current_user),
):
    save_fcm_token(current_user["user_id"], body.fcm_token)
    return {"message": "FCM token saved."}


# ── Get available slots for a doctor on a date ─────────────────────────────────

@router.get("/slots/{doctor_id}", response_model=DoctorSlotsResponse)
async def get_doctor_slots(
    doctor_id:    str,
    date:         str,
    current_user: dict = Depends(get_current_user),
):
    doctor_doc = get_doctor_doc(doctor_id)
    if not doctor_doc:
        raise HTTPException(status_code=404, detail="Doctor not found.")

    try:
        slot_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD.")

    day_name     = slot_date.strftime("%A")
    availability = doctor_doc.get("availabilitySlots", [])
    day_windows  = [a for a in availability if a.get("day") == day_name]

    if not day_windows:
        return DoctorSlotsResponse(
            doctor_id   = doctor_id,
            doctor_name = doctor_doc.get("name", ""),
            slots       = [],
        )

    booked_query = (
        _col(COL_APPOINTMENTS)
        .where("doctor_id", "==", doctor_id)
        .where("slot_date", "==", date)
        .where("status", "in", [
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.ACTIVE,
            AppointmentStatus.PENDING,
        ])
        .stream()
    )
    booked_times = {doc.to_dict()["slot_start_time"] for doc in booked_query}

    # The consultation fee is stored once at the top level of the doctor doc
    # (set via the profile form's "Consultation Fee" field). Availability
    # windows only carry {day, startTime, endTime}, so a per-window price is
    # the rare exception — fall back to the flat doctor fee.
    doctor_fee = int(doctor_doc.get("price", doctor_doc.get("price_pkr", 0)) or 0)

    all_slots = []
    for window in day_windows:
        duration_min = int(window.get("slotDuration", window.get("slot_duration_min", 20)))
        win_price    = int(window.get("price", window.get("price_pkr", 0)) or 0)
        price_pkr    = win_price if win_price > 0 else doctor_fee
        start_str    = window.get("startTime", window.get("start_time", "09:00"))
        end_str      = window.get("endTime",   window.get("end_time",   "10:00"))

        current = datetime.strptime(f"{date} {start_str}", "%Y-%m-%d %H:%M")
        end_dt  = datetime.strptime(f"{date} {end_str}",   "%Y-%m-%d %H:%M")
        now     = datetime.now(timezone.utc).replace(tzinfo=None)

        while current + timedelta(minutes=duration_min) <= end_dt:
            slot_start = current.strftime("%H:%M")
            slot_end   = (current + timedelta(minutes=duration_min)).strftime("%H:%M")
            if current > now:
                all_slots.append({
                    "slot_date":       date,
                    "slot_start_time": slot_start,
                    "slot_end_time":   slot_end,
                    "duration_min":    duration_min,
                    "price_pkr":       price_pkr,
                    "status":          "booked" if slot_start in booked_times else "available",
                })
            current += timedelta(minutes=duration_min)

    return DoctorSlotsResponse(
        doctor_id   = doctor_id,
        doctor_name = doctor_doc.get("name", ""),
        slots       = all_slots,
    )


# ── Book appointment ───────────────────────────────────────────────────────────

@router.post("/book", response_model=AppointmentResponse, status_code=201)
async def book_appointment(
    body:         BookAppointmentRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can book appointments.")

    # 1. Verify payment is confirmed
    session = _col(COL_PAYMENT_SESSIONS).document(body.safepay_token).get().to_dict()
    if not session:
        raise HTTPException(status_code=400, detail="Payment session not found.")
    if session.get("status") != "paid":
        raise HTTPException(status_code=402, detail="Payment not completed. Please pay first.")
    if session.get("patient_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="This payment does not belong to you.")

    # 2. Race-condition guard — check slot is still available
    conflict = (
        _col(COL_APPOINTMENTS)
        .where("doctor_id",       "==", body.doctor_id)
        .where("slot_date",       "==", body.slot_date)
        .where("slot_start_time", "==", body.slot_start_time)
        .where("status", "in", [
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.ACTIVE,
            AppointmentStatus.PENDING,
        ])
        .limit(1)
        .stream()
    )
    if any(True for _ in conflict):
        raise HTTPException(
            status_code=409,
            detail="This slot was just booked by another patient. Please choose a different time.",
        )

    # 3. Get doctor info
    doctor_doc = get_doctor_doc(body.doctor_id)
    if not doctor_doc:
        raise HTTPException(status_code=404, detail="Doctor not found.")

    patient_id   = current_user["user_id"]
    patient_name = current_user.get("name", "Patient")
    doctor_name  = doctor_doc.get("name", "Doctor")
    price_pkr    = session.get("amount_pkr", 0)

    # 4. Create appointment document
    appointment_id = str(uuid.uuid4())
    now            = datetime.now(timezone.utc).isoformat()

    appt_doc = {
        "appointment_id":  appointment_id,
        "doctor_id":       body.doctor_id,
        "doctor_name":     doctor_name,
        "patient_id":      patient_id,
        "patient_name":    patient_name,
        "slot_date":       body.slot_date,
        "slot_start_time": body.slot_start_time,
        "slot_end_time":   body.slot_end_time,
        "status":          AppointmentStatus.CONFIRMED,
        "price_pkr":       price_pkr,
        "safepay_token":   body.safepay_token,
        "created_at":      now,
        "channel_name":    appointment_id,
    }
    _col(COL_APPOINTMENTS).document(appointment_id).set(appt_doc)

    # Mark payment session consumed so it cannot book a second appointment
    _col(COL_PAYMENT_SESSIONS).document(body.safepay_token).update({
        "status":         "booked",
        "appointment_id": appointment_id,
    })

    # 5. Notify doctor
    await send_push_notification(
        user_id = body.doctor_id,
        title   = "📅 New Appointment Booked",
        body    = f"{patient_name} booked a consultation on {body.slot_date} at {body.slot_start_time}.",
        data    = {
            "type":           "new_appointment",
            "appointment_id": appointment_id,
            "slot_date":      body.slot_date,
            "slot_time":      body.slot_start_time,
        },
    )

    return appointment_to_response(appt_doc)


# ── List appointments ──────────────────────────────────────────────────────────

@router.get("/my", response_model=AppointmentListResponse)
async def list_my_appointments(current_user: dict = Depends(get_current_user)):
    role  = current_user.get("role")
    uid   = current_user["user_id"]
    field = "patient_id" if role == "patient" else "doctor_id"

    docs = _col(COL_APPOINTMENTS).where(field, "==", uid).stream()
    appointments = [appointment_to_response(d.to_dict()) for d in docs]
    appointments.sort(
        key     = lambda a: f"{a.slot_date} {a.slot_start_time}",
        reverse = True,
    )
    return AppointmentListResponse(appointments=appointments)


# ── Get single appointment ─────────────────────────────────────────────────────

@router.get("/{appointment_id}", response_model=AppointmentResponse)
async def get_appointment(
    appointment_id: str,
    current_user:   dict = Depends(get_current_user),
):
    doc = _col(COL_APPOINTMENTS).document(appointment_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    appt = doc.to_dict()
    uid  = current_user["user_id"]
    if appt["patient_id"] != uid and appt["doctor_id"] != uid:
        raise HTTPException(status_code=403, detail="Access denied.")

    return appointment_to_response(appt)


# ── Cancel appointment ─────────────────────────────────────────────────────────

@router.post("/{appointment_id}/cancel", response_model=AppointmentResponse)
async def cancel_appointment(
    appointment_id: str,
    current_user:   dict = Depends(get_current_user),
):
    ref = _col(COL_APPOINTMENTS).document(appointment_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    appt = doc.to_dict()
    uid  = current_user["user_id"]
    if appt["patient_id"] != uid and appt["doctor_id"] != uid:
        raise HTTPException(status_code=403, detail="Access denied.")

    if appt["status"] in (AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Appointment is already {appt['status']}.")
    if appt["status"] == AppointmentStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Cannot cancel an active call.")

    ref.update({"status": AppointmentStatus.CANCELLED})

    notify_id   = appt["doctor_id"] if uid == appt["patient_id"] else appt["patient_id"]
    notify_name = current_user.get("name", "User")
    await send_push_notification(
        user_id = notify_id,
        title   = "❌ Appointment Cancelled",
        body    = f"{notify_name} cancelled the appointment on {appt['slot_date']} at {appt['slot_start_time']}.",
        data    = {"type": "cancelled", "appointment_id": appointment_id},
    )

    return appointment_to_response({**appt, "status": AppointmentStatus.CANCELLED})


# ── Complete appointment ───────────────────────────────────────────────────────

@router.post("/{appointment_id}/complete", response_model=AppointmentResponse)
async def complete_appointment(
    appointment_id: str,
    current_user:   dict = Depends(get_current_user),
):
    ref = _col(COL_APPOINTMENTS).document(appointment_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Appointment not found.")

    appt = doc.to_dict()
    if appt["doctor_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Only the doctor can complete this appointment.")
    if appt["status"] == AppointmentStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Appointment already completed.")

    now = datetime.now(timezone.utc).isoformat()
    payment    = _col(COL_PAYMENT_SESSIONS).document(appt.get("safepay_token", "")).get().to_dict() or {}
    ref.update({
        "status":        AppointmentStatus.COMPLETED,
        "completed_at":  now,
        "escrow_status": appt.get("escrow_status", "held"),
        "release_at":    appt.get("release_at") or payment.get("release_at"),
    })

    await send_push_notification(
        user_id = appt["patient_id"],
        title   = "Consultation Complete",
        body    = f"Your consultation with {appt['doctor_name']} is complete. Results will appear in your History tab.",
        data    = {"type": "consultation_complete", "appointment_id": appointment_id},
    )

    return appointment_to_response({**appt, "status": AppointmentStatus.COMPLETED, "completed_at": now})
