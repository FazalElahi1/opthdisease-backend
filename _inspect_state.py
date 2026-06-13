"""Throwaway: inspect users/doctors/appointments so we can seed a joinable call."""
from datetime import datetime, timezone
from services.firebase import _col, COL_USERS, COL_DOCTORS, COL_APPOINTMENTS

print("NOW (UTC):", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

print("\n=== USERS ===")
for d in _col(COL_USERS).stream():
    u = d.to_dict()
    print(f"  id={d.id}  email={u.get('email')!r}  role={u.get('role')!r}  name={u.get('name')!r}")

print("\n=== DOCTORS ===")
for d in _col(COL_DOCTORS).stream():
    u = d.to_dict()
    print(f"  id={d.id}  name={u.get('name')!r}  license_status={u.get('license_status')!r}  price={u.get('price', u.get('price_pkr'))}")

print("\n=== APPOINTMENTS (status != completed/cancelled) ===")
for d in _col(COL_APPOINTMENTS).stream():
    a = d.to_dict()
    st = a.get("status")
    if st in ("completed", "cancelled"):
        continue
    print(f"  id={d.id[:8]}  status={st!r}  date={a.get('slot_date')} {a.get('slot_start_time')}-{a.get('slot_end_time')}"
          f"  patient={a.get('patient_id','')[:8]}({a.get('patient_name')!r})  doctor={a.get('doctor_id','')[:8]}({a.get('doctor_name')!r})")

print("\n=== ACTIVE appointments (would block patient-join 503) ===")
n = 0
for d in _col(COL_APPOINTMENTS).where("status", "==", "active").stream():
    a = d.to_dict(); n += 1
    print(f"  id={d.id}  doctor={a.get('doctor_id')}  patient={a.get('patient_name')}")
print("  (none)" if n == 0 else f"  total active: {n}")
