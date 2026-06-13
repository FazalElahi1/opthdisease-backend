"""End the active call as the DOCTOR (Khallad) via the real POST /calls/end."""
import requests
from services.firebase import get_user_doc
from routers.auth import build_token_response

DOCTOR_ID = "qvArYeFd0hUr8cy3HXK3wIg3PM92"
APPT_ID   = "8b74b24b-b409-43ee-a14d-ee5186bcf7cb"
BASE      = "http://127.0.0.1:8000"

doc = get_user_doc(DOCTOR_ID)
assert doc, "doctor user doc not found"
tr = build_token_response({**doc, "user_id": DOCTOR_ID})
token = tr.access_token
print(f"Minted doctor session for {doc.get('name')} ({doc.get('email')})")

r = requests.post(
    f"{BASE}/calls/end",
    json={"appointment_id": APPT_ID},
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    timeout=15,
)
print(f"POST /calls/end -> HTTP {r.status_code}: {r.text}")

# Verify the appointment is now completed
from services.firebase import _col, COL_APPOINTMENTS
appt = _col(COL_APPOINTMENTS).document(APPT_ID).get().to_dict()
print("\nAppointment after end:")
for k in ("status", "completed_at", "call_duration_seconds", "booked_duration_seconds",
          "flagged_short_call", "escrow_status"):
    print(f"  {k}: {appt.get(k)}")
