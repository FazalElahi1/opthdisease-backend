"""
stripe_payments.py
==================
Patient-facing payment endpoints using **Stripe Hosted Checkout**. Mirrors the
JazzCash router (jazzcash_payments.py) 1:1 so the escrow / payouts / admin /
appointments code is untouched — same COL_PAYMENT_SESSIONS doc shape (incl.
`release_at` escrow cooling), same `eyedisease://payment-complete` deep-link.

Flow:
  1. App  POST /stripe/create-session  → creates a Stripe Checkout Session, returns checkout_url
  2. App opens checkout_url in a WebView → Stripe's hosted card page
  3. On pay, Stripe redirects the browser to success_url = /stripe/return?...&status=success
  4. /stripe/return retrieves the session (authoritative), marks the doc paid, then
     302-redirects to eyedisease://payment-complete (WebView intercepts)
  5. Stripe also POSTs checkout.session.completed to /stripe/webhook (authoritative, async)
  6. App polls POST /stripe/status (Firestore-authoritative, Stripe-retrieve fallback)
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from services.firebase import (
    get_doctor_doc,
    _col,
    COL_PAYMENT_SESSIONS,
    COL_PAYMENT_LOGS,
)
from services.notifications import send_push_notification, get_current_user
from services import stripe_service as stripe_svc
from services.payment_common import resolve_fee, generate_txn_ref

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["Payments"])

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


# ── Shared: mark a session paid (idempotent) ────────────────────────────────────

async def _mark_session_paid(session_ref, session: dict, token: str, via: str) -> None:
    if session.get("status") == "paid":
        return
    session_ref.update({
        "status":  "paid",
        "paid_at": datetime.now(timezone.utc).isoformat(),
    })
    _col(COL_PAYMENT_LOGS).add({
        "token":         token,
        "patient_id":    session.get("patient_id"),
        "doctor_id":     session.get("doctor_id"),
        "amount_pkr":    session.get("amount_pkr", 0),
        "status":        "paid",
        "confirmed_via": via,
        "logged_at":     datetime.now(timezone.utc).isoformat(),
    })
    try:
        await send_push_notification(
            user_id = session["patient_id"],
            title   = "Payment Confirmed",
            body    = f"Payment of PKR {session.get('amount_pkr', 0):,} confirmed. Your consultation is booked!",
            data    = {"type": "payment_confirmed", "token": token},
        )
    except Exception as e:
        logger.warning("payment_confirmed push failed: %s", e)


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

    fee_pkr = resolve_fee(doctor_doc, body.slot_start_time)
    if fee_pkr <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{doctor_doc.get('name', 'This doctor')} has not set a consultation fee yet. "
                "Please ask the doctor to complete their profile, or choose a different time slot."
            ),
        )

    doctor_name  = doctor_doc.get("name", "Doctor")
    slot_summary = f"{body.slot_date} {body.slot_start_time}-{body.slot_end_time}"
    txn_ref      = generate_txn_ref()
    bill_ref     = f"appt_{body.doctor_id[:8]}_{body.slot_date}_{body.slot_start_time.replace(':', '')}"

    # Escrow cooling starts when the slot ends.
    try:
        slot_end_dt = datetime.strptime(
            f"{body.slot_date} {body.slot_end_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
        release_at = (slot_end_dt + timedelta(days=ESCROW_COOLING_DAYS)).isoformat()
    except ValueError:
        release_at = None

    # Fail fast if the key is missing — gives a clear error instead of a Stripe SDK crash.
    if not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(
                "Stripe payments are not configured: STRIPE_SECRET_KEY is not set. "
                "Add it (sk_test_…) in your Render environment variables, then redeploy."
            ),
        )

    # Create the Stripe Checkout Session. success/cancel URLs route back through
    # /stripe/return, which 302s to the eyedisease:// scheme the WebView intercepts.
    try:
        checkout = stripe_svc.create_checkout_session(
            amount_pkr  = fee_pkr,
            txn_ref     = txn_ref,
            description = f"Consultation with {doctor_name}",
            success_url = f"{APP_BASE_URL}/stripe/return?token={txn_ref}&status=success",
            cancel_url  = f"{APP_BASE_URL}/stripe/return?token={txn_ref}&status=cancel",
        )
    except Exception as e:
        logger.exception("Stripe create-session failed: %s", e)
        err_str = str(e)
        if "No API key" in err_str or "authentication" in err_str.lower():
            detail = (
                "Stripe authentication failed — verify STRIPE_SECRET_KEY (sk_test_…) "
                "is set correctly in Render environment variables."
            )
        elif "currency" in err_str.lower():
            detail = (
                f"Stripe rejected currency '{os.getenv('STRIPE_CURRENCY', 'pkr')}'. "
                "Enable this currency in your Stripe Dashboard → Settings → Currencies, "
                "or change STRIPE_CURRENCY to 'usd' in Render env vars."
            )
        else:
            detail = f"Could not start payment: {e}"
        raise HTTPException(status_code=502, detail=detail)

    _col(COL_PAYMENT_SESSIONS).document(txn_ref).set({
        "token":             txn_ref,
        "stripe_session_id": checkout["session_id"],
        "patient_id":        current_user["user_id"],
        "doctor_id":         body.doctor_id,
        "doctor_name":       doctor_name,
        "slot_date":         body.slot_date,
        "slot_start":        body.slot_start_time,
        "slot_end":          body.slot_end_time,
        "slot_summary":      slot_summary,
        "amount_pkr":        fee_pkr,
        "status":            "pending",
        "created_at":        datetime.now(timezone.utc).isoformat(),
        "release_at":        release_at,
        "bill_ref":          bill_ref,
        "description":       f"Consultation with {doctor_name}",
    })

    logger.info("Stripe session created: token=%s patient=%s doctor=%s PKR=%s",
                txn_ref, current_user["user_id"], body.doctor_id, fee_pkr)

    return {
        "success":      True,
        "token":        txn_ref,
        "checkout_url": checkout["checkout_url"],
        "amount_pkr":   fee_pkr,
        "currency":     stripe_svc.STRIPE_CURRENCY.upper(),  # driven by STRIPE_CURRENCY env var
        # "currency":   "USD",                               # rollback: set STRIPE_CURRENCY=usd on Render
        "doctor_name":  doctor_name,
        "slot_summary": slot_summary,
        "release_at":   release_at,
    }


# ── Return handler (Stripe redirects the browser here) ──────────────────────────
# Stripe's success/cancel redirect is NOT signed, so we don't trust the query
# alone — on "success" we retrieve the session from Stripe (authoritative) before
# marking paid. Then we 302 to the deep link the WebView intercepts.

@router.get("/return", include_in_schema=False)
async def stripe_return(token: str = "", status: str = ""):
    session_ref = _col(COL_PAYMENT_SESSIONS).document(token)
    session     = session_ref.get().to_dict() if token else None

    if not session:
        return Response(status_code=302,
                        headers={"Location": "eyedisease://payment-cancelled?token="})

    if status == "success":
        try:
            res = stripe_svc.retrieve_session(session.get("stripe_session_id", ""))
            if res["paid"]:
                await _mark_session_paid(session_ref, session, token, via="return")
                return Response(status_code=302,
                                headers={"Location": f"eyedisease://payment-complete?token={token}"})
        except Exception as e:
            logger.warning("Stripe return retrieve failed for token=%s: %s", token, e)
        # Not confirmed paid yet — the webhook/poll will catch it; still send the
        # user to the complete screen, which polls /status before booking.
        return Response(status_code=302,
                        headers={"Location": f"eyedisease://payment-complete?token={token}"})

    return Response(status_code=302,
                    headers={"Location": f"eyedisease://payment-cancelled?token={token}"})


# ── Webhook (authoritative; Stripe calls this unauthenticated) ──────────────────

@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    try:
        event = stripe_svc.verify_webhook(payload, sig)
    except Exception as e:
        logger.error("Stripe webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature.")

    if event["type"] == "checkout.session.completed":
        obj   = event["data"]["object"]
        token = (obj.get("metadata") or {}).get("token") or obj.get("client_reference_id") or ""
        if token:
            session_ref = _col(COL_PAYMENT_SESSIONS).document(token)
            session     = session_ref.get().to_dict()
            if session:
                await _mark_session_paid(session_ref, session, token, via="webhook")

    return {"received": True}


# ── Status (Firestore-authoritative, Stripe-retrieve fallback) ──────────────────

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

    # Still pending locally — ask Stripe directly.
    try:
        res = stripe_svc.retrieve_session(session.get("stripe_session_id", ""))
        if res["paid"]:
            await _mark_session_paid(session_ref, session, body.token, via="status")
            return {"success": True, "status": "paid",
                    "amount_pkr": session["amount_pkr"], "token": body.token}
    except Exception as e:
        logger.warning("Stripe status retrieve failed for token=%s: %s", body.token, e)

    return {"success": True, "status": session["status"],
            "amount_pkr": session["amount_pkr"], "token": body.token}


# ── Refund ──────────────────────────────────────────────────────────────────────

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

    # Stripe supports real refunds. Look up the PaymentIntent from the session.
    try:
        res = stripe_svc.retrieve_session(session.get("stripe_session_id", ""))
        if res.get("payment_intent"):
            stripe_svc.refund_payment(res["payment_intent"])
    except Exception as e:
        logger.warning("Stripe refund call failed for token=%s (recording anyway): %s", body.token, e)

    session_ref.update({
        "status":            "refunded",
        "refunded_at":       datetime.now(timezone.utc).isoformat(),
        "refund_amount_pkr": body.amount_pkr or session.get("amount_pkr", 0),
    })
    _col(COL_PAYMENT_LOGS).add({
        "token":         body.token,
        "patient_id":    session.get("patient_id"),
        "doctor_id":     session.get("doctor_id"),
        "amount_pkr":    body.amount_pkr or session.get("amount_pkr", 0),
        "status":        "refunded",
        "confirmed_via": current_user.get("role"),
        "logged_at":     datetime.now(timezone.utc).isoformat(),
    })
    try:
        await send_push_notification(
            user_id = session["patient_id"],
            title   = "Refund Approved",
            body    = f"A refund of PKR {(body.amount_pkr or session.get('amount_pkr', 0)):,} has been approved for your consultation.",
            data    = {"type": "refund_approved", "token": body.token},
        )
    except Exception as e:
        logger.warning("refund_approved push failed: %s", e)
    return {"success": True, "message": "Refund recorded."}
