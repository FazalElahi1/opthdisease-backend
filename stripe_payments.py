# from __future__ import annotations

# import os
# import logging
# from datetime import datetime
# from typing import Optional, Literal

# import stripe
# from fastapi import APIRouter, Depends, Header, HTTPException, Request
# from fastapi.responses import Response
# from pydantic import BaseModel, Field
# from sqlalchemy.orm import Session

# from database import get_db
# from models import DoctorProfile, ScanResult, PaymentStatus

# logger = logging.getLogger(__name__)

# stripe.api_key   = os.environ["STRIPE_SECRET_KEY"]
# WEBHOOK_SECRET   = os.environ["STRIPE_WEBHOOK_SECRET"]
# # Platform fee set to 0 — full amount goes to doctor
# PLATFORM_FEE_PCT = float(os.environ.get("STRIPE_PLATFORM_FEE_PCT", "0.0"))
# CURRENCY         = "pkr"

# router = APIRouter()


# # ── Pydantic models ───────────────────────────────────────────────────────────

# class GetFeeRequest(BaseModel):
#     scan_id:   str
#     doctor_id: str

# class CreateIntentRequest(BaseModel):
#     scan_id:        str
#     patient_id:     str
#     doctor_id:      str
#     amount_pkr:     Optional[int]                      = Field(default=None)
#     # NEW: "card" keeps original behaviour; "wallet" enables JazzCash/EasyPaisa
#     payment_method: Literal["card", "wallet"]          = Field(default="card")

# class ScanIdRequest(BaseModel):
#     scan_id: str

# class CancelRequest(BaseModel):
#     scan_id: str
#     reason:  Optional[str] = "requested_by_customer"

# class RefundRequest(BaseModel):
#     scan_id:    str
#     amount_pkr: Optional[int] = None

# class OnboardRequest(BaseModel):
#     doctor_id:   str
#     email:       str
#     return_url:  Optional[str] = "eyedisease://stripe-return"
#     refresh_url: Optional[str] = "eyedisease://stripe-refresh"

# class WithdrawRequest(BaseModel):
#     doctor_id:  str
#     amount_pkr: Optional[int] = None


# # ── Fee helpers ───────────────────────────────────────────────────────────────

# def _resolve_fee(scan_id: str, doctor_id: str, db: Session) -> tuple[int, str]:
#     """
#     Always resolves the authoritative fee from the database.
#     Returns (fee_pkr, 'locked') if the booking already has a locked fee,
#     or (fee_pkr, 'current') using the doctor's current price for new bookings.
#     """
#     scan = db.query(ScanResult).filter(ScanResult.id == scan_id).first()
#     if scan and scan.consultation_fee_locked is not None:
#         return int(scan.consultation_fee_locked), "locked"

#     doctor = db.query(DoctorProfile).filter(DoctorProfile.id == doctor_id).first()
#     if not doctor:
#         raise HTTPException(status_code=404, detail=f"Doctor not found: {doctor_id}")

#     return int(doctor.price), "current"


# def _validate_client_amount(
#     client_amount: Optional[int], server_fee: int, fee_source: str
# ) -> None:
#     """Raises HTTPException if client amount doesn't match server fee."""
#     if client_amount is None:
#         return
#     if client_amount != server_fee:
#         if fee_source == "locked":
#             raise HTTPException(
#                 status_code=409,
#                 detail=(
#                     f"This consultation was booked at PKR {server_fee:,}. "
#                     f"You submitted PKR {client_amount:,}. "
#                     f"The fee for an existing booking cannot change."
#                 ),
#             )
#         else:
#             raise HTTPException(
#                 status_code=409,
#                 detail=(
#                     f"Fee mismatch: server fee is PKR {server_fee:,}, "
#                     f"you submitted PKR {client_amount:,}. "
#                     f"Please refresh and try again."
#                 ),
#             )


# # ── Payment method type resolver ─────────────────────────────────────────────
# #
# # This is the only new logic. Everything else is unchanged.
# #
# def _get_payment_method_types(method: str) -> list[str]:
#     """
#     Returns the Stripe payment_method_types list based on patient's choice.

#     "card"   → standard card payment (original behaviour)
#     "wallet" → JazzCash and EasyPaisa (Stripe-native, no extra account needed)

#     Stripe automatically shows only the methods the customer has available.
#     If the customer has both JazzCash and EasyPaisa, Stripe shows both.
#     """
#     if method == "wallet":
#         return ["jazzcash", "easypaisa"]
#     return ["card"]


# # ── Endpoints ─────────────────────────────────────────────────────────────────

# @router.post("/stripe/get-fee")
# async def get_fee(body: GetFeeRequest, db: Session = Depends(get_db)):
#     """
#     Returns the authoritative fee for a scan/doctor pair.
#     Called by the app before showing the payment screen.
#     Unchanged from original.
#     """
#     try:
#         fee_pkr, fee_source = _resolve_fee(body.scan_id, body.doctor_id, db)
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error("get_fee error: %s", e)
#         raise HTTPException(status_code=500, detail="Could not resolve fee.")

#     locked  = fee_source == "locked"
#     message = (
#         "Fee locked at booking time." if locked
#         else "Current doctor fee — may change until payment is made."
#     )

#     return {
#         "success":    True,
#         "fee_pkr":    fee_pkr,
#         "fee_source": fee_source,
#         "locked":     locked,
#         "message":    message,
#     }


# @router.post("/stripe/create-payment-intent")
# async def create_payment_intent(
#     body: CreateIntentRequest,
#     db:   Session = Depends(get_db),
# ):
#     """
#     Creates a Stripe PaymentIntent.

#     UPDATED: accepts `payment_method` field ("card" | "wallet").
#     When "wallet" is passed, the PaymentIntent is created with
#     JazzCash and EasyPaisa as allowed payment methods.
#     The Stripe Payment Sheet on the app then shows the wallet option.

#     No other logic changes — fee resolution, amount validation, and
#     metadata are all unchanged.
#     """
#     # 1. Resolve authoritative fee
#     try:
#         server_fee, fee_source = _resolve_fee(body.scan_id, body.doctor_id, db)
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error("create_payment_intent fee error: %s", e)
#         raise HTTPException(status_code=500, detail="Could not resolve fee.")

#     # 2. Validate client-submitted amount against server fee
#     _validate_client_amount(body.amount_pkr, server_fee, fee_source)

#     # 3. Get doctor info for metadata
#     doctor = db.query(DoctorProfile).filter(DoctorProfile.id == body.doctor_id).first()
#     if not doctor:
#         raise HTTPException(status_code=404, detail="Doctor not found.")

#     # 4. Determine payment method types (NEW)
#     payment_method_types = _get_payment_method_types(body.payment_method)

#     # 5. Create Stripe PaymentIntent
#     try:
#         intent = stripe.PaymentIntent.create(
#             amount               = server_fee,          # PKR — zero-decimal currency
#             currency             = CURRENCY,
#             payment_method_types = payment_method_types,  # ← card or wallet
#             metadata             = {
#                 "scan_id":        body.scan_id,
#                 "patient_id":     body.patient_id,
#                 "doctor_id":      body.doctor_id,
#                 "doctor_name":    doctor.name,
#                 "payment_method": body.payment_method,  # for webhook logs
#             },
#             description          = f"Consultation with Dr. {doctor.name}",
#             capture_method       = "manual",            # pre-auth; capture after review
#         )
#     except stripe.error.StripeError as e:
#         logger.error("Stripe PaymentIntent creation failed: %s", e)
#         raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

#     # 6. Lock the fee on the scan so it can't change after intent is created
#     scan = db.query(ScanResult).filter(ScanResult.id == body.scan_id).first()
#     if scan:
#         scan.consultation_fee_locked    = server_fee
#         scan.stripe_payment_intent_id   = intent.id
#         scan.payment_status             = PaymentStatus.pending
#         db.commit()

#     logger.info(
#         "PaymentIntent created: %s | PKR %s | method: %s | scan: %s",
#         intent.id, server_fee, body.payment_method, body.scan_id,
#     )

#     return {
#         "success":           True,
#         "client_secret":     intent.client_secret,
#         "payment_intent_id": intent.id,
#         "amount_pkr":        server_fee,
#         "currency":          CURRENCY,
#         "payment_method":    body.payment_method,
#     }


# # ── Webhook (unchanged) ───────────────────────────────────────────────────────

# @router.post("/stripe/webhook", include_in_schema=False)
# async def stripe_webhook(
#     request:          Request,
#     stripe_signature: str = Header(None, alias="stripe-signature"),
#     db:               Session = Depends(get_db),
# ):
#     """
#     Handles Stripe webhook events.
#     Unchanged — works for both card and wallet payments because Stripe
#     fires the same events regardless of payment method used.
#     """
#     payload = await request.body()

#     try:
#         event = stripe.Webhook.construct_event(payload, stripe_signature, WEBHOOK_SECRET)
#     except stripe.error.SignatureVerificationError:
#         raise HTTPException(status_code=400, detail="Invalid webhook signature.")
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=str(e))

#     event_type = event["type"]
#     logger.info("Stripe webhook received: %s", event_type)

#     if event_type == "payment_intent.amount_capturable_updated":
#         # Payment authorised — ready to capture after doctor completes review
#         intent = event["data"]["object"]
#         scan = (
#             db.query(ScanResult)
#             .filter(ScanResult.stripe_payment_intent_id == intent["id"])
#             .first()
#         )
#         if scan:
#             scan.payment_status = PaymentStatus.authorised
#             db.commit()
#             logger.info("Scan %s payment authorised (method: %s)",
#                         scan.id, intent["metadata"].get("payment_method", "card"))

#     elif event_type == "payment_intent.succeeded":
#         intent = event["data"]["object"]
#         scan = (
#             db.query(ScanResult)
#             .filter(ScanResult.stripe_payment_intent_id == intent["id"])
#             .first()
#         )
#         if scan:
#             scan.payment_status = PaymentStatus.captured
#             db.commit()

#     elif event_type == "payment_intent.payment_failed":
#         intent = event["data"]["object"]
#         scan = (
#             db.query(ScanResult)
#             .filter(ScanResult.stripe_payment_intent_id == intent["id"])
#             .first()
#         )
#         if scan:
#             scan.payment_status = PaymentStatus.failed
#             db.commit()

#     elif event_type == "charge.refunded":
#         charge    = event["data"]["object"]
#         intent_id = charge.get("payment_intent", "")
#         scan = (
#             db.query(ScanResult)
#             .filter(ScanResult.stripe_payment_intent_id == intent_id)
#             .first()
#         )
#         if scan:
#             scan.payment_status = PaymentStatus.refunded
#             db.commit()

#     return Response(status_code=200)


# # ── Capture (called when doctor submits review) — unchanged ───────────────────

# @router.post("/stripe/capture-payment")
# async def capture_payment(body: ScanIdRequest, db: Session = Depends(get_db)):
#     """
#     Captures the pre-authorised payment after the doctor completes the review.
#     Works identically for card and wallet payments.
#     Called by the backend review-submission flow, not the patient app.
#     """
#     scan = db.query(ScanResult).filter(ScanResult.id == body.scan_id).first()
#     if not scan:
#         raise HTTPException(status_code=404, detail="Scan not found.")
#     if not scan.stripe_payment_intent_id:
#         raise HTTPException(status_code=400, detail="No payment intent for this scan.")
#     if scan.payment_status != PaymentStatus.authorised:
#         raise HTTPException(
#             status_code=400,
#             detail=f"Cannot capture payment — current status: {scan.payment_status}",
#         )

#     try:
#         intent = stripe.PaymentIntent.capture(scan.stripe_payment_intent_id)
#     except stripe.error.StripeError as e:
#         raise HTTPException(status_code=502, detail=f"Stripe capture error: {e.user_message}")

#     scan.payment_status = PaymentStatus.captured
#     db.commit()

#     # Credit doctor's balance in DB (full amount — 0% platform fee)
#     doctor = db.query(DoctorProfile).filter(DoctorProfile.id == scan.doctor_id).first()
#     if doctor:
#         doctor.pending_balance = (doctor.pending_balance or 0) + int(scan.consultation_fee_locked)
#         db.commit()

#     logger.info("Captured PKR %s for scan %s", scan.consultation_fee_locked, scan.id)
#     return {"success": True, "captured_amount_pkr": int(scan.consultation_fee_locked)}


# # ── Cancel / refund — unchanged ───────────────────────────────────────────────

# @router.post("/stripe/cancel-payment")
# async def cancel_payment(body: CancelRequest, db: Session = Depends(get_db)):
#     scan = db.query(ScanResult).filter(ScanResult.id == body.scan_id).first()
#     if not scan or not scan.stripe_payment_intent_id:
#         raise HTTPException(status_code=404, detail="Scan or payment not found.")

#     try:
#         stripe.PaymentIntent.cancel(
#             scan.stripe_payment_intent_id,
#             cancellation_reason=body.reason,
#         )
#     except stripe.error.StripeError as e:
#         raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

#     scan.payment_status = PaymentStatus.cancelled
#     db.commit()
#     return {"success": True}


# @router.post("/stripe/refund-payment")
# async def refund_payment(body: RefundRequest, db: Session = Depends(get_db)):
#     scan = db.query(ScanResult).filter(ScanResult.id == body.scan_id).first()
#     if not scan or not scan.stripe_payment_intent_id:
#         raise HTTPException(status_code=404, detail="Scan or payment not found.")
#     if scan.payment_status != PaymentStatus.captured:
#         raise HTTPException(status_code=400, detail="Payment has not been captured yet.")

#     intent = stripe.PaymentIntent.retrieve(scan.stripe_payment_intent_id)
#     if not intent.latest_charge:
#         raise HTTPException(status_code=400, detail="No charge found to refund.")

#     refund_kwargs: dict = {"charge": intent.latest_charge}
#     if body.amount_pkr:
#         refund_kwargs["amount"] = body.amount_pkr

#     try:
#         stripe.Refund.create(**refund_kwargs)
#     except stripe.error.StripeError as e:
#         raise HTTPException(status_code=502, detail=f"Stripe refund error: {e.user_message}")

#     scan.payment_status = PaymentStatus.refunded
#     db.commit()
#     return {"success": True}


# # ─────────────────────────────────────────────────────────────────────────────
# #  SANDBOX TESTING — JazzCash / EasyPaisa test credentials
# # ─────────────────────────────────────────────────────────────────────────────
# #
# #  Use these test phone numbers in Stripe's sandbox to simulate wallet payments:
# #
# #  JazzCash success:   +92 300 0000000   MPIN: 1234
# #  JazzCash decline:   +92 300 1111111   MPIN: 1234
# #  EasyPaisa success:  +92 321 0000000   MPIN: 1234
# #  EasyPaisa decline:  +92 321 1111111   MPIN: 1234
# #
# #  Stripe Dashboard → Developers → Test mode must be ON.
# #  Payment Methods → JazzCash and EasyPaisa must be ENABLED (even in test mode).
# #
# # ─────────────────────────────────────────────────────────────────────────────