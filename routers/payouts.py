"""
payouts.py
==========
Doctor earnings dashboard + manual payout REQUESTS.

There is NO automated money-out API (Stripe does not operate in Pakistan, so it
cannot pay out to PK banks/wallets). Instead payouts are manual:

  1. Escrow releases funds automatically after the cooling period with no
     complaint (services/escrow.py) → appointment.escrow_status = "released".
  2. The doctor saves a payout account (where to send the money) and submits a
     payout REQUEST.
  3. An admin transfers the money to the doctor's account out-of-band and marks
     the request "paid" (routers/admin.py → /admin/payouts/{id}/mark-paid). The
     doctor's account details are surfaced to the admin there.

Available balance = released earnings − (requested + paid payout amounts), so a
doctor can never request the same funds twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.firebase import (
    get_doctor_doc,
    set_doctor_doc,
    _col,
    COL_APPOINTMENTS,
    COL_PAYOUTS,
    COL_USERS,
)
from services.notifications import send_push_notification, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payouts", tags=["Payouts"])

MIN_PAYOUT_PKR = 500


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_doctor(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "doctor":
        raise HTTPException(status_code=403, detail="Only doctors can access payouts.")
    return current_user


# ── Schemas ───────────────────────────────────────────────────────────────────

class SaveAccountRequest(BaseModel):
    account_holder: str   # name on the account
    bank_name:      str   # bank or wallet provider, e.g. "Meezan Bank", "Easypaisa"
    account_number: str   # IBAN / account number / wallet number


class WithdrawRequest(BaseModel):
    amount_pkr: int


# ── Earnings computation ────────────────────────────────────────────────────────

def _compute_balance(doctor_id: str) -> dict:
    appt_docs = (
        _col(COL_APPOINTMENTS)
        .where("doctor_id", "==", doctor_id)
        .where("status", "==", "completed")
        .stream()
    )
    completed = [a.to_dict() for a in appt_docs]

    released_total = 0
    pending_total  = 0     # held (cooling) + disputed
    transactions   = []
    for appt in completed:
        price  = appt.get("price_pkr", 0)
        escrow = appt.get("escrow_status", "held")
        if escrow == "released":
            released_total += price
        elif escrow in ("held", "disputed"):
            pending_total += price
        transactions.append({
            "appointment_id": appt.get("appointment_id", ""),
            "patient_name":   appt.get("patient_name", "Patient"),
            "completed_at":   appt.get("completed_at"),
            "amount_pkr":     price,
            "escrow_status":  escrow,
            "is_available":   escrow == "released",
        })

    # Funds already claimed by payout requests (requested OR paid) can't be re-requested.
    payout_docs = _col(COL_PAYOUTS).where("doctor_id", "==", doctor_id).stream()
    payouts = [p.to_dict() for p in payout_docs]
    claimed = sum(p.get("amount_pkr", 0) for p in payouts if p.get("status") in ("requested", "paid"))
    paid    = sum(p.get("amount_pkr", 0) for p in payouts if p.get("status") == "paid")

    available = max(0, released_total - claimed)

    return {
        "gross_earnings_pkr":     sum(a.get("price_pkr", 0) for a in completed),
        "released_pkr":           released_total,
        "available_pkr":          available,
        "pending_pkr":            pending_total,
        "requested_pkr":          max(0, claimed - paid),
        "withdrawn_pkr":          paid,
        "completed_appointments": len(completed),
        "transactions":           transactions,
    }


# ── Payout account setup ──────────────────────────────────────────────────────

@router.post("/bank/save")
async def save_payout_account(body: SaveAccountRequest, doctor: dict = Depends(require_doctor)):
    holder = body.account_holder.strip()
    bank   = body.bank_name.strip()
    number = body.account_number.strip()

    if not holder:
        raise HTTPException(status_code=400, detail="Please enter the account holder name.")
    if not bank:
        raise HTTPException(status_code=400, detail="Please enter your bank or wallet name.")
    if len(number) < 5:
        raise HTTPException(status_code=400, detail="Please enter a valid account or wallet number.")

    set_doctor_doc(doctor["user_id"], {
        "payout_account_holder": holder,
        "payout_bank_name":      bank,
        "payout_account_number": number,
        "payout_saved_at":       datetime.now(timezone.utc).isoformat(),
    })
    logger.info("Doctor %s saved payout account ending %s", doctor["user_id"], number[-4:])
    return {
        "success":        True,
        "message":        "Your payout account has been saved.",
        "account_holder": holder,
        "bank_name":      bank,
        "account_number": number,
        "account_last4":  number[-4:],
    }


@router.get("/bank/status")
async def account_status(doctor: dict = Depends(require_doctor)):
    doctor_doc = get_doctor_doc(doctor["user_id"]) or {}
    number     = doctor_doc.get("payout_account_number")
    return {
        "connected":      bool(number),
        "account_holder": doctor_doc.get("payout_account_holder"),
        "bank_name":      doctor_doc.get("payout_bank_name"),
        "account_number": number,
        "account_last4":  number[-4:] if number else None,
        "message":        "Payout account connected." if number else "Please add a payout account to withdraw.",
    }


# ── Balance ───────────────────────────────────────────────────────────────────

@router.get("/balance")
async def get_balance(doctor: dict = Depends(require_doctor)):
    doctor_doc = get_doctor_doc(doctor["user_id"]) or {}
    bal = _compute_balance(doctor["user_id"])
    bal["bank_connected"] = bool(doctor_doc.get("payout_account_number"))
    bal["platform_fee_pkr"] = 0
    bal["net_earnings_pkr"] = bal["gross_earnings_pkr"]
    return bal


# ── Request payout ──────────────────────────────────────────────────────────────

@router.post("/withdraw")
async def request_payout(body: WithdrawRequest, doctor: dict = Depends(require_doctor)):
    if body.amount_pkr < MIN_PAYOUT_PKR:
        raise HTTPException(status_code=400, detail=f"Minimum payout is PKR {MIN_PAYOUT_PKR}.")

    doctor_doc = get_doctor_doc(doctor["user_id"]) or {}
    number     = doctor_doc.get("payout_account_number")
    bank       = doctor_doc.get("payout_bank_name", "")
    holder     = doctor_doc.get("payout_account_holder", doctor.get("name", "Doctor"))
    if not number:
        raise HTTPException(status_code=400, detail="Please add a payout account first before withdrawing.")

    available = _compute_balance(doctor["user_id"])["available_pkr"]
    if body.amount_pkr > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient available balance. Available: PKR {available:,}. Requested: PKR {body.amount_pkr:,}.",
        )

    now = datetime.now(timezone.utc).isoformat()
    payout_ref = _col(COL_PAYOUTS).add({
        "doctor_id":      doctor["user_id"],
        "doctor_name":    doctor.get("name", ""),
        "amount_pkr":     body.amount_pkr,
        "account_holder": holder,
        "bank_name":      bank,
        "account_number": number,
        "account_last4":  number[-4:],
        "status":         "requested",
        "created_at":     now,
    })
    payout_id = payout_ref[1].id if isinstance(payout_ref, tuple) else payout_ref.id

    # Notify admins that a payout needs to be processed manually.
    try:
        for admin_doc in _col(COL_USERS).where("role", "==", "admin").stream():
            await send_push_notification(
                user_id = admin_doc.id,
                title   = "Payout Requested",
                body    = f"Dr. {doctor.get('name', '')} requested PKR {body.amount_pkr:,} to {bank} {number}.",
                data    = {"type": "payout_requested", "payout_id": payout_id},
            )
    except Exception as e:
        logger.error("Failed to notify admins of payout request: %s", e)

    return {
        "message":        "Payout requested. An admin will transfer the funds to your account shortly.",
        "payout_id":      payout_id,
        "amount_pkr":     body.amount_pkr,
        "account_holder": holder,
        "bank_name":      bank,
        "account_number": number,
        "account_last4":  number[-4:],
        "status":         "requested",
    }


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/history")
async def payout_history(doctor: dict = Depends(require_doctor)):
    docs    = _col(COL_PAYOUTS).where("doctor_id", "==", doctor["user_id"]).stream()
    history = []
    for d in docs:
        item = d.to_dict()
        item["payout_id"] = d.id
        history.append(item)
    history.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"history": history}
