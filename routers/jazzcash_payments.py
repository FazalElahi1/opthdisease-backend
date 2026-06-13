"""
jazzcash_payments.py
====================
Patient-facing payment endpoints using **real** JazzCash sandbox Hosted Checkout.
Replaces safepay_payments.py (no simulation).

Flow:
  1. App calls POST /jazzcash/create-session     → gets a checkout_url
  2. App opens checkout_url in a WebView          → our auto-submit form POSTs to JazzCash
  3. Patient pays on JazzCash's hosted page (card or mobile wallet)
  4. JazzCash POSTs the signed result to /jazzcash/return → we verify + mark paid
  5. /jazzcash/return 302-redirects to eyedisease://payment-complete (WebView intercepts)
  6. App may also poll /jazzcash/status (Firestore-authoritative, inquiry fallback)
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from services.firebase import (
    get_doctor_doc,
    _col,
    COL_PAYMENT_SESSIONS,
    COL_PAYMENT_LOGS,
)
from services.notifications import send_push_notification, get_current_user
from services import jazzcash_service as jc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jazzcash", tags=["Payments"])

import os
ESCROW_COOLING_DAYS = int(os.getenv("ESCROW_COOLING_DAYS", "2"))
APP_BASE_URL        = os.getenv("APP_BASE_URL", "").rstrip("/")


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    doctor_id:       str
    slot_date:       str
    slot_start_time: str
    slot_end_time:   str


class StatusRequest(BaseModel):
    token: str


class RefundRequest(BaseModel):
    token:      str
    amount_pkr: Optional[int] = None


# ── Fee helper (same rule as appointments/slots) ────────────────────────────────

def _resolve_fee(doctor_doc: dict, slot_start_time: str) -> int:
    for window in doctor_doc.get("availabilitySlots", []):
        start     = window.get("startTime", window.get("start_time", ""))
        end       = window.get("endTime",   window.get("end_time",   ""))
        win_price = int(window.get("price", window.get("price_pkr", 0)) or 0)
        if win_price > 0 and start <= slot_start_time < end:
            return win_price
    return int(doctor_doc.get("price", doctor_doc.get("price_pkr", 0)) or 0)


# ── Create session ──────────────────────────────────────────────────────────────

@router.post("/create-session")
async def create_session(
    body:         CreateSessionRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="Only patients can make payments.")

    doctor_doc = get_doctor_doc(body.doctor_id)
    if not doctor_doc:
        raise HTTPException(status_code=404, detail="Doctor not found.")

    fee_pkr = _resolve_fee(doctor_doc, body.slot_start_time)
    if fee_pkr <= 0:
        raise HTTPException(
            status_code=400,
            detail="Could not determine price for this slot. Please try again.",
        )

    doctor_name  = doctor_doc.get("name", "Doctor")
    slot_summary = f"{body.slot_date} {body.slot_start_time}-{body.slot_end_time}"
    txn_ref      = jc.generate_txn_ref()
    bill_ref     = f"appt_{body.doctor_id[:8]}_{body.slot_date}_{body.slot_start_time.replace(':', '')}"

    # Escrow cooling starts when the slot ends.
    try:
        slot_end_dt = datetime.strptime(
            f"{body.slot_date} {body.slot_end_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
        release_at = (slot_end_dt + timedelta(days=ESCROW_COOLING_DAYS)).isoformat()
    except ValueError:
        release_at = None

    _col(COL_PAYMENT_SESSIONS).document(txn_ref).set({
        "token":          txn_ref,
        "patient_id":     current_user["user_id"],
        "doctor_id":      body.doctor_id,
        "doctor_name":    doctor_name,
        "slot_date":      body.slot_date,
        "slot_start":     body.slot_start_time,
        "slot_end":       body.slot_end_time,
        "slot_summary":   slot_summary,
        "amount_pkr":     fee_pkr,
        "status":         "pending",
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "release_at":     release_at,
        "bill_ref":       bill_ref,
        "description":    f"Consultation with {doctor_name}",
    })

    logger.info("JazzCash session created: token=%s patient=%s doctor=%s PKR=%s",
                txn_ref, current_user["user_id"], body.doctor_id, fee_pkr)

    return {
        "success":      True,
        "token":        txn_ref,
        "checkout_url": f"{APP_BASE_URL}/jazzcash/pay?token={txn_ref}",
        "amount_pkr":   fee_pkr,
        "currency":     "PKR",
        "doctor_name":  doctor_name,
        "slot_summary": slot_summary,
        "release_at":   release_at,
    }


# ── Hosted-checkout launcher (auto-submitting POST form) ────────────────────────
# The app's WebView opens this URL; it immediately POSTs to JazzCash's hosted page.

@router.get("/pay", include_in_schema=False)
async def pay(token: str):
    session = _col(COL_PAYMENT_SESSIONS).document(token).get().to_dict()
    if not session:
        raise HTTPException(status_code=404, detail="Payment session not found.")
    if session.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This payment session is no longer payable.")

    # Build the signed JazzCash fields fresh here so the merchant password / hash
    # are never persisted in Firestore. The hash is deterministic over whatever we
    # submit, so a fresh TxnDateTime is fine.
    checkout = jc.build_checkout(
        amount_pkr  = session.get("amount_pkr", 0),
        txn_ref     = token,
        bill_ref    = session.get("bill_ref", token),
        description = session.get("description", "Consultation"),
        return_url  = f"{APP_BASE_URL}/jazzcash/return",
    )
    fields   = checkout["fields"]
    post_url = checkout["post_url"]
    inputs   = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}"/>'
        for k, v in fields.items()
    )
    page = f"""<!doctype html><html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Redirecting to JazzCash…</title>
<style>body{{font-family:-apple-system,Roboto,sans-serif;display:flex;min-height:100vh;
align-items:center;justify-content:center;background:#7A0C1F;color:#fff;margin:0;text-align:center}}</style>
</head><body onload="document.forms[0].submit()">
<form method="POST" action="{html.escape(post_url)}">{inputs}
<noscript><button type="submit">Continue to JazzCash</button></noscript></form>
<div>Securely redirecting to JazzCash…</div>
</body></html>"""
    return HTMLResponse(content=page)


# ── Return handler (JazzCash posts the signed result here) ──────────────────────

async def _handle_return(params: dict) -> Response:
    token = params.get("pp_TxnRefNo") or params.get("ppmpf_1") or ""
    code  = str(params.get("pp_ResponseCode", ""))

    session_ref = _col(COL_PAYMENT_SESSIONS).document(token)
    session     = session_ref.get().to_dict() if token else None

    if not session:
        logger.warning("JazzCash return for unknown token=%s code=%s", token, code)
        return Response(status_code=302,
                        headers={"Location": "eyedisease://payment-cancelled?token="})

    if not jc.verify_secure_hash(params):
        logger.error("JazzCash return FAILED hash check: token=%s", token)
        session_ref.update({"status": "failed", "fail_reason": "invalid_signature"})
        return Response(status_code=302,
                        headers={"Location": f"eyedisease://payment-cancelled?token={token}"})

    if code == "000":
        if session.get("status") != "paid":
            session_ref.update({
                "status":          "paid",
                "paid_at":         datetime.now(timezone.utc).isoformat(),
                "jazzcash_retrieval_ref": params.get("pp_RetreivalReferenceNo", ""),
            })
            _col(COL_PAYMENT_LOGS).add({
                "token":         token,
                "patient_id":    session.get("patient_id"),
                "doctor_id":     session.get("doctor_id"),
                "amount_pkr":    session.get("amount_pkr", 0),
                "status":        "paid",
                "confirmed_via": "return",
                "response_code": code,
                "logged_at":     datetime.now(timezone.utc).isoformat(),
            })
            await send_push_notification(
                user_id = session["patient_id"],
                title   = "Payment Confirmed",
                body    = f"Payment of PKR {session.get('amount_pkr', 0):,} confirmed. Your consultation is booked!",
                data    = {"type": "payment_confirmed", "token": token},
            )
        return Response(status_code=302,
                        headers={"Location": f"eyedisease://payment-complete?token={token}"})

    # Any non-000 response = failed / cancelled.
    session_ref.update({"status": "failed", "fail_reason": f"code_{code}"})
    return Response(status_code=302,
                    headers={"Location": f"eyedisease://payment-cancelled?token={token}"})


@router.post("/return", include_in_schema=False)
async def jazzcash_return_post(request: Request):
    form = await request.form()
    return await _handle_return({k: v for k, v in form.items()})


@router.get("/return", include_in_schema=False)
async def jazzcash_return_get(request: Request):
    return await _handle_return({k: v for k, v in request.query_params.items()})


# ── Status (Firestore-authoritative, inquiry fallback) ──────────────────────────

@router.post("/status")
async def check_payment_status(
    body:         StatusRequest,
    current_user: dict = Depends(get_current_user),
):
    session_ref = _col(COL_PAYMENT_SESSIONS).document(body.token)
    session     = session_ref.get().to_dict()
    if not session:
        raise HTTPException(status_code=404, detail="Payment session not found.")
    if session["patient_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied.")

    if session["status"] in ("paid", "failed", "cancelled", "refunded"):
        return {"success": True, "status": session["status"],
                "amount_pkr": session["amount_pkr"], "token": body.token}

    # Still pending locally — ask JazzCash directly.
    live = jc.payment_inquiry(body.token)
    if live["status"] == "paid":
        session_ref.update({"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()})
        _col(COL_PAYMENT_LOGS).add({
            "token": body.token, "patient_id": session["patient_id"],
            "doctor_id": session["doctor_id"], "amount_pkr": session["amount_pkr"],
            "status": "paid", "confirmed_via": "inquiry",
            "logged_at": datetime.now(timezone.utc).isoformat(),
        })
    elif live["status"] == "failed":
        session_ref.update({"status": "failed"})

    return {"success": True, "status": live["status"] if live["status"] != "pending" else session["status"],
            "amount_pkr": session["amount_pkr"], "token": body.token}


# ── Refund ──────────────────────────────────────────────────────────────────────
# JazzCash sandbox does not reliably support self-service refunds with these
# checkout credentials, so a refund is recorded in Firestore (and the escrow is
# marked refunded by admin complaint-resolution); the actual reversal is handled
# by the admin out-of-band, consistent with the manual-payout model.

@router.post("/refund")
async def refund(
    body:         RefundRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") not in ("doctor", "admin"):
        raise HTTPException(status_code=403, detail="Only doctors or admins can issue refunds.")

    session_ref = _col(COL_PAYMENT_SESSIONS).document(body.token)
    session     = session_ref.get().to_dict()
    if not session:
        raise HTTPException(status_code=404, detail="Payment session not found.")
    if session["status"] != "paid":
        raise HTTPException(status_code=400, detail="Can only refund paid sessions.")

    session_ref.update({
        "status":      "refunded",
        "refunded_at": datetime.now(timezone.utc).isoformat(),
        "refund_amount_pkr": body.amount_pkr or session.get("amount_pkr", 0),
    })
    _col(COL_PAYMENT_LOGS).add({
        "token": body.token, "patient_id": session.get("patient_id"),
        "doctor_id": session.get("doctor_id"),
        "amount_pkr": body.amount_pkr or session.get("amount_pkr", 0),
        "status": "refunded", "confirmed_via": current_user.get("role"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    })
    await send_push_notification(
        user_id = session["patient_id"],
        title   = "Refund Approved",
        body    = f"A refund of PKR {(body.amount_pkr or session.get('amount_pkr', 0)):,} has been approved for your consultation.",
        data    = {"type": "refund_approved", "token": body.token},
    )
    return {"success": True, "message": "Refund recorded."}
