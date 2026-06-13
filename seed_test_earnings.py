"""
seed_test_earnings.py  (DEV UTILITY — not part of app startup)
================================================================
Seeds a few COMPLETED appointments for a doctor so the Earnings tab shows
real, withdrawable money. The /payouts/balance endpoint sums completed
appointments: those completed >= 2 days ago are "available", newer ones are
"pending".

Usage (from the backend folder, with the venv active):
    python seed_test_earnings.py doctor@example.com
    python seed_test_earnings.py doctor@example.com 3000   # custom fee per visit
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone

from services.firebase import (
    get_firebase_app,
    _col,
    COL_APPOINTMENTS,
    get_user_by_email,
)


def seed(doctor_email: str, fee: int = 2500) -> None:
    get_firebase_app()

    doctor = get_user_by_email(doctor_email)
    if not doctor:
        print(f"No user found with email {doctor_email}. Register/approve the doctor first.")
        return
    if doctor.get("role") != "doctor":
        print(f"{doctor_email} is a '{doctor.get('role')}', not a doctor.")
        return

    doctor_id   = doctor["user_id"]
    doctor_name = doctor.get("name", "Doctor")
    now         = datetime.now(timezone.utc)

    # (days_ago_completed, patient_name) — >=2 days ago => available, else pending
    samples = [
        (5, "Ahmed Khan"),
        (4, "Sara Ali"),
        (3, "Bilal Hussain"),
        (0, "Fatima Noor"),   # completed today => pending for ~2 days
    ]

    created = 0
    for days_ago, patient_name in samples:
        completed_at = now - timedelta(days=days_ago)
        slot_date    = completed_at.strftime("%Y-%m-%d")
        appt_id      = str(uuid.uuid4())
        _col(COL_APPOINTMENTS).document(appt_id).set({
            "appointment_id":  appt_id,
            "doctor_id":       doctor_id,
            "doctor_name":     doctor_name,
            "patient_id":      f"seed_patient_{created}",
            "patient_name":    patient_name,
            "slot_date":       slot_date,
            "slot_start_time": "10:00",
            "slot_end_time":   "10:30",
            "status":          "completed",
            "price_pkr":       fee,
            "created_at":      (completed_at - timedelta(hours=1)).isoformat(),
            "completed_at":    completed_at.isoformat(),
            "channel_name":    appt_id,
            "seed":            True,   # marker so these are easy to find/remove later
        })
        created += 1

    available = fee * sum(1 for d, _ in samples if d >= 2)
    pending   = fee * sum(1 for d, _ in samples if d < 2)
    print(f"Seeded {created} completed appointments for {doctor_name} ({doctor_email}).")
    print(f"Gross earnings: PKR {fee * created:,}")
    print(f"Available now:  PKR {available:,}")
    print(f"Pending:        PKR {pending:,}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python seed_test_earnings.py <doctor_email> [fee_pkr]")
        sys.exit(1)
    email = sys.argv[1]
    fee   = int(sys.argv[2]) if len(sys.argv) > 2 else 2500
    seed(email, fee)
