"""
smoke_test.py
=============
Hits the live backend and verifies all 4 fixed issues behave correctly.

Prerequisites: seed_test.py must have been run first.

Run:
  BASE_URL=https://opthdisease-backend.onrender.com python scripts/smoke_test.py
  (or with the Render URL as the first argument)

What is tested:
  1. /health           — server is alive (Issue 1 / cold-start)
  2. /admin/payouts    — returns flat string fields, not [object Object] (Issue 2)
  3. /payouts/balance  — returns days_since in transactions (Issue 2 / withdrawal timer)
  4. /xai/scan/{id}/share without a paid appt → 402 with clear message (Issue 3)
  5. /xai/scan/{id}/share WITH  a paid appt  → 200 success              (Issue 3)
  6. /stripe/create-session     → either 200 OR a clear error message   (Issue 4)

Seeded IDs (from seed_test.py):
  PATIENT_ID  = "seed_patient_test"
  DOCTOR_ID   = "seed_doctor_test"
  SCAN_ID     = "seed_patient_test_1700000000"
  APPT_ID     = "seed_appt_completed_001"

NOTE: Tests 2, 3b (admin payouts / doctor balance / scan-share-ok) require a
JWT for an actual admin / doctor / patient account. This script uses a JWT
obtained from /auth/login if you provide credentials via env vars.
"""

import os, sys, json, textwrap
import urllib.request, urllib.error

BASE_URL   = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BASE_URL", "http://localhost:8000")).rstrip("/")
ADMIN_EMAIL  = os.getenv("ADMIN_EMAIL",   "f.elahi1767@gmail.com")
ADMIN_PASS   = os.getenv("ADMIN_PASS",    "")
DOCTOR_EMAIL = os.getenv("DOCTOR_EMAIL",  "doctor_test@eye.local")
DOCTOR_PASS  = os.getenv("DOCTOR_PASS",   "")
PATIENT_EMAIL= os.getenv("PATIENT_EMAIL", "patient_test@eye.local")
PATIENT_PASS = os.getenv("PATIENT_PASS",  "")

PATIENT_ID   = "seed_patient_test"
DOCTOR_ID    = "seed_doctor_test"
SCAN_ID      = f"{PATIENT_ID}_1700000000"
OTHER_DOCTOR_ID = "some_other_doctor_id"   # a doctor the patient has NOT paid

PASS_ICON = "✅"
FAIL_ICON = "❌"
WARN_ICON = "⚠️ "

results = []

def request(method: str, path: str, body=None, token: str = "") -> tuple[int, dict]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as e:
        return 0, {"error": str(e)}


def login(email: str, password: str) -> str:
    if not password:
        return ""
    status, data = request("POST", "/auth/login", {"email": email, "password": password})
    if status == 200:
        return data.get("access_token", "")
    return ""


def check(name: str, ok: bool, detail: str = ""):
    icon = PASS_ICON if ok else FAIL_ICON
    line = f"{icon}  {name}"
    if detail:
        line += f"\n     {detail}"
    print(line)
    results.append(ok)


# ── 0. Login ─────────────────────────────────────────────────────────────────
print(f"\nSmoke-testing  {BASE_URL}\n")

admin_token  = login(ADMIN_EMAIL,   ADMIN_PASS)
doctor_token = login(DOCTOR_EMAIL,  DOCTOR_PASS)
patient_token= login(PATIENT_EMAIL, PATIENT_PASS)

if not admin_token:
    print(WARN_ICON + " ADMIN_PASS not set — skipping admin-only tests")
if not doctor_token:
    print(WARN_ICON + " DOCTOR_PASS not set — skipping doctor-only tests")
if not patient_token:
    print(WARN_ICON + " PATIENT_PASS not set — skipping patient-only tests")

# ── 1. Health ─────────────────────────────────────────────────────────────────
status, data = request("GET", "/health")
check(
    "Issue 1 — /health returns 200 (server alive)",
    status == 200,
    str(data),
)

# ── 2a. Admin payouts — flat string fields, not objects ────────────────────────
if admin_token:
    status, data = request("GET", "/admin/payouts", token=admin_token)
    payouts = data.get("payouts", [])
    if status == 200 and payouts:
        p = payouts[0]
        bad_fields = [k for k in ("account_holder", "bank_name", "account_number")
                      if not isinstance(p.get(k), (str, type(None)))]
        check(
            "Issue 2 — /admin/payouts fields are strings (not [object Object])",
            len(bad_fields) == 0,
            f"bad fields: {bad_fields}" if bad_fields else f"payout: {json.dumps(p)[:120]}",
        )
    else:
        check("Issue 2 — /admin/payouts reachable", status == 200,
              f"status={status} — no payouts yet? Run seed_test.py first.")

# ── 2b. Doctor balance — includes days_since ────────────────────────────────────
if doctor_token:
    status, data = request("GET", "/payouts/balance", token=doctor_token)
    txns = data.get("transactions", [])
    if status == 200 and txns:
        all_have = all("days_since" in t for t in txns)
        check(
            "Issue 2 — /payouts/balance transactions include days_since",
            all_have,
            f"first txn keys: {list(txns[0].keys())}",
        )
    else:
        check("Issue 2 — /payouts/balance reachable", status == 200,
              f"status={status} data={str(data)[:120]}")

# ── 3a. Payment gate: share without paid appointment → 402 ────────────────────
if patient_token:
    # Try to share with a doctor the patient has never paid
    status, data = request(
        "POST", f"/xai/scan/{SCAN_ID}/share",
        body={"scan_id": SCAN_ID, "doctor_id": OTHER_DOCTOR_ID},
        token=patient_token,
    )
    is_402 = status == 402
    detail  = data.get("detail", "")
    check(
        "Issue 3 — share scan without paid appt → 402 payment gate",
        is_402 and "book" in detail.lower(),
        f"status={status} detail={detail[:120]}",
    )

    # Try to share with the seeded doctor (has completed appointment)
    status2, data2 = request(
        "POST", f"/xai/scan/{SCAN_ID}/share",
        body={"scan_id": SCAN_ID, "doctor_id": DOCTOR_ID},
        token=patient_token,
    )
    check(
        "Issue 3 — share scan WITH  paid appt → 200 success",
        status2 in (200, 409),   # 409 = already shared (also fine)
        f"status={status2} detail={data2.get('detail', data2.get('message', ''))[:120]}",
    )

# ── 4. Stripe create-session — expect 200 or a clear error (not a crash 500) ──
if patient_token:
    status, data = request(
        "POST", "/stripe/create-session",
        body={
            "doctor_id":       DOCTOR_ID,
            "slot_date":       "2026-06-20",
            "slot_start_time": "10:00",
            "slot_end_time":   "10:30",
        },
        token=patient_token,
    )
    detail = data.get("detail", "")
    if status == 200:
        check("Issue 4 — /stripe/create-session returns checkout_url", True,
              f"checkout_url: {str(data.get('checkout_url',''))[:80]}")
    elif status in (502, 503) and ("STRIPE_SECRET_KEY" in detail or "Stripe" in detail or "currency" in detail.lower()):
        check("Issue 4 — /stripe/create-session returns actionable error",
              True, f"(needs Stripe config) detail: {detail[:120]}")
    else:
        check("Issue 4 — /stripe/create-session error is clear", False,
              f"status={status} detail={detail[:200]}")

# ── Summary ────────────────────────────────────────────────────────────────────
total   = len(results)
passed  = sum(results)
print(f"\n{'=' * 50}")
print(f"Results: {passed}/{total} passed")
if passed == total:
    print("All checks passed!")
else:
    print(f"{total - passed} check(s) failed — see details above.")
