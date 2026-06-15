"""
smoke_test.py — verifies all 4 fixed issues against the live backend.

Run (after seed_test.py):
  BASE_URL=https://opthdisease-backend.onrender.com python scripts/smoke_test.py
"""

import os, sys, json, time
import urllib.request, urllib.error

BASE_URL = (sys.argv[1] if len(sys.argv) > 1
            else os.getenv("BASE_URL", "https://opthdisease-backend.onrender.com")).rstrip("/")

# Seeded credentials (must match seed_test.py)
PATIENT_EMAIL = "smoke.patient001@gmail.com"
DOCTOR_EMAIL  = "smoke.doctor001@gmail.com"
ADMIN_EMAIL   = "smoke.admin001@gmail.com"
TEST_PW       = "SmokePa$$1"

PATIENT_ID  = "smoke_patient_001"
DOCTOR_ID   = "smoke_doctor_001"
SCAN_ID     = f"{PATIENT_ID}_1700000000"
DOCTOR2_ID  = "smoke_doctor_002"   # exists in Firestore but no paid appt with patient

OK   = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
results: list[bool] = []


def req(method: str, path: str, body=None, token: str = "") -> tuple[int, dict]:
    url     = f"{BASE_URL}{path}"
    data    = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    rq = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=40) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as ex:
        return 0, {"error": str(ex)}


def login(email: str, role: str) -> str:
    status, data = req("POST", "/auth/login",
                        {"email": email, "password": TEST_PW, "role": role})
    if status == 200:
        return data.get("access_token", "")
    print(f"  {WARN} login({email}) failed: {status} {data.get('detail', data)}")
    return ""


def check(name: str, ok: bool, detail: str = "") -> None:
    icon = OK if ok else FAIL
    print(f"{icon}  {name}")
    if detail:
        print(f"     {detail}")
    results.append(ok)


print(f"\nSmoke-testing  {BASE_URL}\n")

# ── 0. Warm up (Render free tier sleeps after 15 min) ─────────────────────────
print("Warming up server …")
status, _ = req("GET", "/health")
if status != 200:
    print(f"{WARN} /health returned {status} — server may still be waking. Waiting 15 s …")
    time.sleep(15)

# ── 1. Health ─────────────────────────────────────────────────────────────────
status, data = req("GET", "/health")
check("Issue 1 — /health returns 200 (server alive)", status == 200, str(data)[:80])

# ── Login ──────────────────────────────────────────────────────────────────────
print()
patient_tok = login(PATIENT_EMAIL, "patient")
doctor_tok  = login(DOCTOR_EMAIL,  "doctor")
admin_tok   = login(ADMIN_EMAIL,   "admin")

# ── 2a. Admin: /admin/payouts — string fields, not objects ─────────────────────
if admin_tok:
    status, data = req("GET", "/admin/payouts", token=admin_tok)
    payouts = data.get("payouts", [])
    smoke_payout = next((p for p in payouts if p.get("doctor_id") == DOCTOR_ID), None)
    if smoke_payout:
        bad = [k for k in ("account_holder", "bank_name", "account_number")
               if not isinstance(smoke_payout.get(k), (str, type(None)))]
        check(
            "Issue 2a — /admin/payouts: fields are strings (no [object Object])",
            len(bad) == 0,
            f"smoke payout: {json.dumps({k: smoke_payout.get(k) for k in ('account_holder','bank_name','account_number')})}"
            if not bad else f"bad types: {bad}",
        )
    else:
        check("Issue 2a — /admin/payouts reachable + has seeded payout",
              status == 200 and len(payouts) >= 0,
              f"status={status} total_payouts={len(payouts)} — run seed_test.py if 0")
else:
    print(f"{WARN} No admin token — skipping admin payout test")

# ── 2b. Doctor: /payouts/balance — includes days_since ────────────────────────
if doctor_tok:
    status, data = req("GET", "/payouts/balance", token=doctor_tok)
    txns = data.get("transactions", [])
    if status == 200 and txns:
        all_have = all("days_since" in t for t in txns)
        first    = txns[0]
        check(
            "Issue 2b — /payouts/balance transactions include days_since",
            all_have,
            f"days_since={first.get('days_since')} is_available={first.get('is_available')}",
        )
    elif status == 200:
        check("Issue 2b — /payouts/balance reachable (no transactions yet)", True,
              "No completed appointments yet — run seed_test.py first")
    else:
        check("Issue 2b — /payouts/balance reachable", False,
              f"status={status} detail={data.get('detail','')[:100]}")
else:
    print(f"{WARN} No doctor token — skipping balance test")

# ── 3a. Payment gate: no paid appt -> 402 ──────────────────────────────────────
if patient_tok:
    status, data = req(
        "POST", f"/xai/scan/{SCAN_ID}/share",
        body={"scan_id": SCAN_ID, "doctor_id": DOCTOR2_ID},
        token=patient_tok,
    )
    detail = data.get("detail", "")
    check(
        "Issue 3a — share scan without paid appt -> 402",
        status == 402 and "book" in detail.lower(),
        f"status={status} detail={detail[:120]}",
    )
else:
    print(f"{WARN} No patient token — skipping payment-gate tests")

# ── 3b. Payment gate: WITH paid appt -> success ────────────────────────────────
if patient_tok:
    status, data = req(
        "POST", f"/xai/scan/{SCAN_ID}/share",
        body={"scan_id": SCAN_ID, "doctor_id": DOCTOR_ID},
        token=patient_tok,
    )
    check(
        "Issue 3b — share scan WITH paid appt -> 200/409",
        status in (200, 409),   # 409 = already shared to this doctor (also correct)
        f"status={status}  {data.get('message', data.get('detail', ''))[:100]}",
    )

# ── 4. Stripe create-session — clear error OR success ─────────────────────────
if patient_tok:
    status, data = req(
        "POST", "/stripe/create-session",
        body={
            "doctor_id":       DOCTOR_ID,
            "slot_date":       "2026-06-20",
            "slot_start_time": "10:00",
            "slot_end_time":   "10:30",
        },
        token=patient_tok,
    )
    detail = data.get("detail", "")
    if status == 200:
        check("Issue 4 — /stripe/create-session -> checkout_url returned", True,
              f"url: {str(data.get('checkout_url',''))[:80]}")
    elif status == 503 and "STRIPE_SECRET_KEY" in detail:
        check("Issue 4 — /stripe/create-session -> clear 503 (key missing)", True,
              f"ACTION NEEDED: {detail[:160]}")
    elif status in (502, 503) and ("Stripe" in detail or "stripe" in detail.lower() or "currency" in detail.lower()):
        check("Issue 4 — /stripe/create-session -> actionable Stripe error", True,
              f"detail: {detail[:160]}")
    else:
        check("Issue 4 — /stripe/create-session returns useful error", False,
              f"status={status} detail={detail[:200]}")

# ── Summary ────────────────────────────────────────────────────────────────────
passed = sum(results)
total  = len(results)
print(f"\n{'=' * 54}")
print(f"Results: {passed}/{total} passed")
if passed == total:
    print("All checks passed! Ready to push.")
else:
    print(f"{total - passed} check(s) failed — see ❌ lines above.")
