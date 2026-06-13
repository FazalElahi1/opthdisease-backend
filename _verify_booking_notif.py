"""
Verify #5: booking an appointment dispatches a doctor notification with the
exact patient name + date/time. Invokes the REAL book_appointment flow with a
captured push function, then cleans up everything it created.
"""
import asyncio
from datetime import datetime, timezone, timedelta

import routers.appointments as appt_mod
from models.appointment_schemas import BookAppointmentRequest
from services.firebase import (
    _col, set_doc, get_doc, get_doctor_doc, set_doctor_doc,
    COL_APPOINTMENTS, COL_PAYMENT_SESSIONS,
)

PATIENT_ID   = "BNuYRnNNwWfj7kgr6u47KsgUl8w1"   # FAZAL
PATIENT_NAME = "FAZAL"
DOCTOR_ID    = "qvArYeFd0hUr8cy3HXK3wIg3PM92"   # Khallad
TOKEN        = "TEST_BOOKING_NOTIF"

# Capture push notifications instead of sending them
captured = []
async def _capture(user_id, title, body, data=None):
    captured.append({"user_id": user_id, "title": title, "body": body, "data": data})
    return True
appt_mod.send_push_notification = _capture

created_doctor = False

def ascii_safe(s):
    return str(s).encode("ascii", "replace").decode("ascii")

async def main():
    global created_doctor
    # Clean up any orphaned test appointments from a previous failed run
    for d in _col(COL_APPOINTMENTS).where("safepay_token", "==", TOKEN).stream():
        _col(COL_APPOINTMENTS).document(d.id).delete()

    # Ensure the doctor doc exists (Khallad's may not yet); create a temp one if needed
    if not get_doctor_doc(DOCTOR_ID):
        set_doctor_doc(DOCTOR_ID, {"user_id": DOCTOR_ID, "name": "Khallad", "email": "fazal.elahi9123@gmail.com",
                                   "license_status": "verified", "price": 1000, "specialties": ["Glaucoma"]})
        created_doctor = True

    # Seed a PAID payment session owned by the patient
    set_doc(COL_PAYMENT_SESSIONS, TOKEN, {"status": "paid", "patient_id": PATIENT_ID, "amount_pkr": 1000})

    # A slot a few days out (avoids clashing with the call-test slot)
    day = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    body = BookAppointmentRequest(
        doctor_id=DOCTOR_ID, slot_date=day,
        slot_start_time="14:00", slot_end_time="14:20", safepay_token=TOKEN,
    )
    current_user = {"user_id": PATIENT_ID, "role": "patient", "name": PATIENT_NAME}

    appt_id = None
    try:
        resp = await appt_mod.book_appointment(body, current_user)
        appt_id = resp.appointment_id
        print("Booking created appointment:", appt_id[:8], "for", day, "14:00")
        print("\nCaptured notifications dispatched:")
        for c in captured:
            who = "DOCTOR" if c["user_id"] == DOCTOR_ID else c["user_id"][:8]
            print(f"  -> {who}: title={ascii_safe(c['title'])!r}")
            print(f"           body={ascii_safe(c['body'])!r}")
            print(f"           data.type={(c['data'] or {}).get('type')!r}")
    finally:
        # Cleanup no matter what
        if appt_id:
            _col(COL_APPOINTMENTS).document(appt_id).delete()
        _col(COL_PAYMENT_SESSIONS).document(TOKEN).delete()
        if created_doctor:
            from services.firebase import COL_DOCTORS
            _col(COL_DOCTORS).document(DOCTOR_ID).delete()
        print("\n(cleaned up appointment, payment session" + (", temp doctor doc" if created_doctor else "") + ")")

asyncio.run(main())
