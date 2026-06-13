"""
Seed a CONFIRMED, joinable appointment (phone = patient) and mint the
web-second-device Agora token for the SAME channel.

Window math matches routers/calls.py: now(UTC) must be within
[slot_start - 5min, slot_end]. We set start = now-2min, end = now+58min.
"""
import json
import uuid
from datetime import datetime, timezone, timedelta

from services.firebase import _col, COL_APPOINTMENTS
from services.agora import generate_call_token, AGORA_APP_ID

PATIENT_ID   = "BNuYRnNNwWfj7kgr6u47KsgUl8w1"   # FAZAL  (faz.elahi1928@gmail.com) — log in as THIS on the phone
PATIENT_NAME = "FAZAL"
DOCTOR_ID    = "qvArYeFd0hUr8cy3HXK3wIg3PM92"   # Khallad (fazal.elahi9123@gmail.com)
DOCTOR_NAME  = "Khallad"
WEB_UID      = 12345                             # numeric uid for the headless web "doctor" device

now   = datetime.now(timezone.utc)
start = now - timedelta(minutes=2)
end   = now + timedelta(minutes=58)

appt_id = str(uuid.uuid4())
appt = {
    "appointment_id":  appt_id,
    "doctor_id":       DOCTOR_ID,
    "doctor_name":     DOCTOR_NAME,
    "patient_id":      PATIENT_ID,
    "patient_name":    PATIENT_NAME,
    "slot_date":       start.strftime("%Y-%m-%d"),
    "slot_start_time": start.strftime("%H:%M"),
    "slot_end_time":   end.strftime("%H:%M"),
    "status":          "confirmed",
    "price_pkr":       1000,
    "safepay_token":   f"TEST_{appt_id[:8]}",
    "created_at":      now.isoformat(),
    "channel_name":    appt_id,
    "_seeded_test":    True,
}
_col(COL_APPOINTMENTS).document(appt_id).set(appt)

tok = generate_call_token(channel_name=appt_id, uid=WEB_UID)

cfg = {
    "appId":   AGORA_APP_ID,
    "channel": appt_id,
    "token":   tok["token"],
    "uid":     WEB_UID,
}
with open("_webclient_config.json", "w") as f:
    json.dump(cfg, f, indent=2)

print("SEEDED appointment:")
print(f"  appointment_id / channel : {appt_id}")
print(f"  slot (UTC)               : {appt['slot_date']} {appt['slot_start_time']}-{appt['slot_end_time']}")
print(f"  patient (phone login)    : {PATIENT_NAME}  faz.elahi1928@gmail.com")
print(f"  doctor (web device)      : {DOCTOR_NAME}")
print(f"  web client uid           : {WEB_UID}")
print(f"  web token (v{tok['token'][:3]})         : {tok['token'][:24]}...")
print("\nWrote _webclient_config.json for the headless web second-device.")
