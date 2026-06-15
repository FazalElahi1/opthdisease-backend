"""
seed_test.py
============
Seeds Firestore with minimal test data so you can verify all 4 fixed issues
without waiting for real user activity.

Run (from the backend root, with FIREBASE_CREDENTIALS_JSON set):
  python scripts/seed_test.py

What it creates:
  • A "patient_test" user  (role=patient,  email=patient_test@eye.local)
  • A "doctor_test"  user  (role=doctor,   email=doctor_test@eye.local)
  • A completed appointment between them (status=completed, escrow_status=released)
  • A payout request from doctor_test (so admin can see it in the payouts tab)
  • A scan doc (so we can test the payment-gate on /xai/scan/{id}/share)

After seeding, run smoke_test.py to hit the live API and verify each endpoint.
"""

import os, json, sys
from datetime import datetime, timezone, timedelta

# ── Firebase Admin ────────────────────────────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    sys.exit("pip install firebase-admin")

creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON", "")
if not creds_json:
    # fall back to local file
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase-credentials.json")
    cred = credentials.Certificate(cred_path)
else:
    cred = credentials.Certificate(json.loads(creds_json))

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ── Config ────────────────────────────────────────────────────────────────────
APP_ROOT = os.getenv("FIRESTORE_APP_ROOT", "opthdisease")

def col(name: str):
    return db.collection(f"{APP_ROOT}/{name}")

NOW      = datetime.now(timezone.utc).isoformat()
TWO_DAYS_AGO = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

PATIENT_ID  = "seed_patient_test"
DOCTOR_ID   = "seed_doctor_test"
APPT_ID     = "seed_appt_completed_001"
PAYOUT_ID   = "seed_payout_001"
SCAN_ID     = f"{PATIENT_ID}_1700000000"

# ── Seed users ─────────────────────────────────────────────────────────────────

print("Seeding patient user …")
col("users").document(PATIENT_ID).set({
    "user_id":      PATIENT_ID,
    "email":        "patient_test@eye.local",
    "name":         "Test Patient",
    "role":         "patient",
    "age":          30,
    "is_active":    True,
    "created_at":   NOW,
}, merge=True)

print("Seeding doctor user …")
col("users").document(DOCTOR_ID).set({
    "user_id":            DOCTOR_ID,
    "email":              "doctor_test@eye.local",
    "name":               "Dr. Test Doctor",
    "role":               "doctor",
    "specialties":        ["Ophthalmology"],
    "experience_years":   5,
    "price":              1500,
    "license_status":     "verified",
    "is_active":          True,
    "created_at":         NOW,
    # payout account (so /payouts/bank/status works)
    "payout_account_holder": "Dr. Test Doctor",
    "payout_bank_name":      "Meezan Bank",
    "payout_account_number": "PK12MEZN0001234567890101",
    "payout_saved_at":       NOW,
}, merge=True)

col("doctors").document(DOCTOR_ID).set({
    "user_id":            DOCTOR_ID,
    "email":              "doctor_test@eye.local",
    "name":               "Dr. Test Doctor",
    "role":               "doctor",
    "specialties":        ["Ophthalmology"],
    "experience_years":   5,
    "price":              1500,
    "license_status":     "verified",
    "is_active":          True,
    "payout_account_holder": "Dr. Test Doctor",
    "payout_bank_name":      "Meezan Bank",
    "payout_account_number": "PK12MEZN0001234567890101",
    "payout_saved_at":       NOW,
}, merge=True)

# ── Seed completed appointment ─────────────────────────────────────────────────

print("Seeding completed appointment …")
col("appointments").document(APPT_ID).set({
    "appointment_id":  APPT_ID,
    "patient_id":      PATIENT_ID,
    "patient_name":    "Test Patient",
    "doctor_id":       DOCTOR_ID,
    "doctor_name":     "Dr. Test Doctor",
    "slot_date":       "2026-06-13",
    "slot_start":      "10:00",
    "slot_end":        "10:30",
    "price_pkr":       1500,
    "status":          "completed",
    "escrow_status":   "released",   # funds available for withdrawal
    "completed_at":    TWO_DAYS_AGO,
    "created_at":      TWO_DAYS_AGO,
}, merge=True)

# ── Seed payout request ────────────────────────────────────────────────────────

print("Seeding payout request …")
col("payouts").document(PAYOUT_ID).set({
    "doctor_id":      DOCTOR_ID,
    "doctor_name":    "Dr. Test Doctor",
    "amount_pkr":     1500,
    "account_holder": "Dr. Test Doctor",
    "bank_name":      "Meezan Bank",
    "account_number": "PK12MEZN0001234567890101",
    "account_last4":  "0101",
    "status":         "requested",
    "created_at":     NOW,
}, merge=True)

# ── Seed scan (for payment-gate test) ─────────────────────────────────────────

print("Seeding scan …")
col("scans").document(SCAN_ID).set({
    "id":               SCAN_ID,
    "patientId":        PATIENT_ID,
    "patientName":      "Test Patient",
    "date":             NOW,
    "riskLevel":        "Low",
    "findings":         "No significant pathology detected.",
    "primaryCondition": "Normal",
    "confidence":       0.92,
    "probabilities":    {"Normal": 0.92},
    "affectedRegions":  [],
    "recommendation":   "Continue regular check-ups.",
    "heatmapBase64":    "",
    "savedAt":          NOW,
}, merge=True)

print()
print("=" * 60)
print("Seed data created. IDs:")
print(f"  Patient:     {PATIENT_ID}")
print(f"  Doctor:      {DOCTOR_ID}")
print(f"  Appointment: {APPT_ID}")
print(f"  Payout:      {PAYOUT_ID}")
print(f"  Scan:        {SCAN_ID}")
print()
print("Next: run  python scripts/smoke_test.py  to verify all endpoints.")
