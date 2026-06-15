"""
seed_test.py — creates deterministic test data in Firestore.

Run from the backend root:
  python scripts/seed_test.py

Creates:
  • patient_smoke   (role=patient, email=patient_smoke@test.local, pw=SmokePa$$1)
  • doctor_smoke    (role=doctor,  email=doctor_smoke@test.local,  pw=SmokePa$$1)
  • admin_smoke     (role=admin,   email=admin_smoke@test.local,   pw=SmokePa$$1)
  • a completed appointment between patient and doctor (escrow released)
  • a disputed appointment with a pending complaint (smoke_appt_002)
  • a complaint review tied to smoke_appt_002
  • a scan belonging to patient_smoke (for payment-gate test)
"""

import os, sys
from datetime import datetime, timezone, timedelta

# ── Firebase Admin ────────────────────────────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    sys.exit("pip install firebase-admin")

cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH",
                       os.path.join(os.path.dirname(__file__), "..", "firebase-credentials.json"))
cred = credentials.Certificate(os.path.abspath(cred_path))
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ── bcrypt password hashing (same as the backend) ─────────────────────────────
try:
    from passlib.context import CryptContext
except ImportError:
    sys.exit("pip install passlib[bcrypt]")

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_pw(plain: str) -> str:
    return _pwd_ctx.hash(plain)

# ── Firestore helpers — MUST match services/firebase.py _col() ───────────────
# Actual path: db.document("app_data/meta").collection("<sub>")
def col(sub: str):
    return db.document("app_data/meta").collection(sub)

# ── IDs & credentials ─────────────────────────────────────────────────────────
NOW          = datetime.now(timezone.utc).isoformat()
TWO_DAYS_AGO = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
YESTERDAY    = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

PATIENT_ID   = "smoke_patient_001"
DOCTOR_ID    = "smoke_doctor_001"
DOCTOR2_ID   = "smoke_doctor_002"   # second doctor — no appointment with patient (for payment-gate test)
ADMIN_ID     = "smoke_admin_001"
APPT_ID      = "smoke_appt_001"
APPT2_ID     = "smoke_appt_002"     # disputed appointment — used for complaint seed
PAYOUT_ID    = "smoke_payout_001"   # legacy — deleted below so available_pkr stays clean
SCAN_ID      = f"{PATIENT_ID}_1700000000"

TEST_PW      = "SmokePa$$1"
PW_HASH      = hash_pw(TEST_PW)

print("Using Firestore collection path: app_data/meta/<sub>")
print()

# ── Patient ───────────────────────────────────────────────────────────────────
print(f"[1/6] Seeding patient  ({PATIENT_ID}) ...")
col("users").document(PATIENT_ID).set({
    "user_id":       PATIENT_ID,
    "email":         "smoke.patient001@gmail.com",
    "name":          "Smoke Patient",
    "role":          "patient",
    "age":           28,
    "is_active":     True,
    "is_verified":   True,
    "auth_method":   "email",
    "password_hash": PW_HASH,
    "created_at":    NOW,
    "session_token": "smoke_session_patient",
}, merge=True)

# ── Doctor ────────────────────────────────────────────────────────────────────
print(f"[2/6] Seeding doctor   ({DOCTOR_ID}) ...")
doctor_data = {
    "user_id":            DOCTOR_ID,
    "email":              "smoke.doctor001@gmail.com",
    "name":               "Dr. Smoke Test",
    "role":               "doctor",
    "specialties":        ["Ophthalmology"],
    "experience_years":   4,
    "price":              1200,
    "license_status":     "verified",
    "is_active":          True,
    "auth_method":        "email",
    "password_hash":      PW_HASH,
    "created_at":         NOW,
    "session_token":      "smoke_session_doctor",
    "payout_account_holder": "Dr. Smoke Test",
    "payout_bank_name":      "HBL",
    "payout_account_number": "PK36HABB0000049501279101",
    "payout_saved_at":       NOW,
}
col("users").document(DOCTOR_ID).set(doctor_data, merge=True)
col("doctors").document(DOCTOR_ID).set(doctor_data, merge=True)

# ── Doctor 2 (no appointment with patient — used for payment-gate 402 test) ───
print(f"[3/7] Seeding doctor2  ({DOCTOR2_ID}) ...")
doctor2_data = {
    "user_id":        DOCTOR2_ID,
    "email":          "smoke.doctor002@gmail.com",
    "name":           "Dr. Smoke Two",
    "role":           "doctor",
    "specialties":    ["Ophthalmology"],
    "price":          1000,
    "license_status": "verified",
    "is_active":      True,
    "auth_method":    "email",
    "password_hash":  PW_HASH,
    "created_at":     NOW,
    "session_token":  "smoke_session_doctor2",
}
col("users").document(DOCTOR2_ID).set(doctor2_data, merge=True)
col("doctors").document(DOCTOR2_ID).set(doctor2_data, merge=True)

# ── Admin (role=admin — backend gate: role=="admin" OR email in ADMIN_EMAILS) ─
print(f"[4/7] Seeding admin    ({ADMIN_ID}) ...")
col("users").document(ADMIN_ID).set({
    "user_id":       ADMIN_ID,
    "email":         "smoke.admin001@gmail.com",
    "name":          "Smoke Admin",
    "role":          "admin",
    "is_active":     True,
    "auth_method":   "email",
    "password_hash": PW_HASH,
    "created_at":    NOW,
    "session_token": "smoke_session_admin",
}, merge=True)

# ── Completed appointment (patient <-> doctor, escrow released) ───────────────
print(f"[5/7] Seeding appointment ({APPT_ID}) ...")
col("appointments").document(APPT_ID).set({
    "appointment_id":  APPT_ID,
    "patient_id":      PATIENT_ID,
    "patient_name":    "Smoke Patient",
    "doctor_id":       DOCTOR_ID,
    "doctor_name":     "Dr. Smoke Test",
    "slot_date":       "2026-06-13",
    "slot_start_time": "10:00",
    "slot_end_time":   "10:30",
    "channel_name":    APPT_ID,
    "price_pkr":       1200,
    "status":          "completed",
    "escrow_status":   "released",
    "completed_at":    TWO_DAYS_AGO,
    "created_at":      TWO_DAYS_AGO,
}, merge=True)

# ── Delete legacy payout request (it was seeded with status="requested" which
#    consumed the doctor's available_pkr — delete it so available_pkr = 1200) ──
print(f"[6/8] Deleting stale payout ({PAYOUT_ID}) so available_pkr is clean ...")
col("payouts").document(PAYOUT_ID).delete()

# ── Second completed appointment — disputed (complaint pending) ───────────────
print(f"[7/8] Seeding disputed appointment ({APPT2_ID}) ...")
col("appointments").document(APPT2_ID).set({
    "appointment_id":        APPT2_ID,
    "patient_id":            PATIENT_ID,
    "patient_name":          "Smoke Patient",
    "doctor_id":             DOCTOR_ID,
    "doctor_name":           "Dr. Smoke Test",
    "slot_date":             "2026-06-14",
    "slot_start_time":       "14:00",
    "slot_end_time":         "14:30",
    "channel_name":          APPT2_ID,
    "price_pkr":             1200,
    "status":                "completed",
    "escrow_status":         "disputed",
    "completed_at":          YESTERDAY,
    "created_at":            YESTERDAY,
    "has_review":            True,
    "safepay_token":         "smoke_token_002",
    "call_duration_seconds": 120,
    "flagged_short_call":    True,
}, merge=True)

# Payment session for the disputed appointment
col("payment_sessions").document("smoke_token_002").set({
    "token":      "smoke_token_002",
    "patient_id": PATIENT_ID,
    "doctor_id":  DOCTOR_ID,
    "amount_pkr": 1200,
    "status":     "booked",
    "created_at": YESTERDAY,
}, merge=True)

# ── Complaint review tied to smoke_appt_002 ───────────────────────────────────
print(f"[8/9] Seeding complaint review for {APPT2_ID} ...")
# Fixed doc ID so re-runs don't duplicate
col("reviews").document("smoke_complaint_001").set({
    "appointment_id":        APPT2_ID,
    "doctor_id":             DOCTOR_ID,
    "doctor_name":           "Dr. Smoke Test",
    "patient_id":            PATIENT_ID,
    "patient_name":          "Smoke Patient",
    "rating":                2,
    "comment":               "The call ended very quickly.",
    "complaint":             "The doctor disconnected after only 2 minutes without addressing my concern.",
    "request_refund":        True,
    "is_complaint":          True,
    "call_happened":         True,
    "call_duration_seconds": 120,
    "flagged_short_call":    True,
    "amount_pkr":            1200,
    "safepay_token":         "smoke_token_002",
    "complaint_status":      "pending",
    "created_at":            YESTERDAY,
}, merge=True)

# ── Scan belonging to patient_smoke ───────────────────────────────────────────
print(f"[9/9] Seeding scan     ({SCAN_ID}) ...")
col("scans").document(SCAN_ID).set({
    "id":               SCAN_ID,
    "patientId":        PATIENT_ID,
    "patientName":      "Smoke Patient",
    "date":             NOW,
    "riskLevel":        "Low",
    "findings":         "No pathology detected.",
    "primaryCondition": "Normal",
    "confidence":       0.95,
    "probabilities":    {"Normal": 0.95},
    "affectedRegions":  [],
    "recommendation":   "Regular check-ups recommended.",
    "heatmapBase64":    "",
    "savedAt":          NOW,
}, merge=True)

print()
print("=" * 60)
print("Seed complete. Test credentials:")
print(f"  Emails: smoke.patient001@gmail.com / smoke.doctor001@gmail.com / smoke.admin001@gmail.com")
print(f"  Password:      {TEST_PW}")
print(f"  Patient ID:    {PATIENT_ID}")
print(f"  Doctor ID:     {DOCTOR_ID}")
print(f"  Scan ID:       {SCAN_ID}")
print(f"  Complaint:     smoke_complaint_001 => {APPT2_ID} (pending, disputed escrow)")
print()
print("Run smoke test:")
print("  BASE_URL=https://opthdisease-backend.onrender.com python scripts/smoke_test.py")
