"""
jazzcash_payouts.py
==================
Doctor earnings dashboard + payout REQUESTS (replaces safepay_payouts.py).

JazzCash sandbox has no self-service disbursement API for these merchant
credentials, so there is NO automated money-out here. Instead:

  1. Escrow releases funds automatically after the cooling period with no
     complaint (services/escrow.py) → appointment.escrow_status = "released".
  2. The doctor sees released funds as "available" and submits a payout REQUEST.
  3. An admin transfers the money to the doctor's JazzCash number out-of-band
     and marks the request "paid" (routers/admin.py → /admin/payouts/{id}/mark-paid).

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

class SaveBankRequest(BaseModel):
    account_name:    str
    jazzcash_number: str


class WithdrawRequest(BaseModel):
    amount_pkr: int


def _normalise_jazzcash_number(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("92") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits


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


# ── JazzCash account setup ────────────────────────────────────────────────────

@router.post("/bank/save")
async def save_bank_account(body: SaveBankRequest, doctor: dict = Depends(require_doctor)):
    number = _normalise_jazzcash_number(body.jazzcash_number)
    if len(number) != 11 or not number.startswith("03"):
        raise HTTPException(
            status_code=400,
            detail="Invalid JazzCash number. Enter an 11-digit mobile number starting with 03. "
                   "Example: 03001234567",
        )
    if not body.account_name.strip():
        raise HTTPException(status_code=400, detail="Please enter the account holder name.")

    set_doctor_doc(doctor["user_id"], {
        "jazzcash_number":   number,
        "jazzcash_name":     body.account_name.strip(),
        "jazzcash_saved_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("Doctor %s linked JazzCash account ending %s", doctor["user_id"], number[-4:])
    return {
        "success":         True,
        "message":         "Your JazzCash account has been saved.",
        "account_name":    body.account_name.strip(),
        "jazzcash_number": number,
        "number_last4":    number[-4:],
    }


@router.get("/bank/status")
async def bank_status(doctor: dict = Depends(require_doctor)):
    doctor_doc = get_doctor_doc(doctor["user_id"]) or {}
    number     = doctor_doc.get("jazzcash_number")
    return {
        "connected":       bool(number),
        "jazzcash_number": number,
        "number_last4":    number[-4:] if number else None,
        "account_name":    doctor_doc.get("jazzcash_name"),
        "message":         "JazzCash account connected." if number else "Please add your JazzCash account to withdraw.",
    }


# ── Balance ───────────────────────────────────────────────────────────────────

@router.get("/balance")
async def get_balance(doctor: dict = Depends(require_doctor)):
    doctor_doc = get_doctor_doc(doctor["user_id"]) or {}
    bal = _compute_balance(doctor["user_id"])
    bal["bank_connected"] = bool(doctor_doc.get("jazzcash_number"))
    bal["platform_fee_pkr"] = 0
    bal["net_earnings_pkr"] = bal["gross_earnings_pkr"]
    return bal


# ── Request payout ──────────────────────────────────────────────────────────────

@router.post("/withdraw")
async def request_payout(body: WithdrawRequest, doctor: dict = Depends(require_doctor)):
    if body.amount_pkr < MIN_PAYOUT_PKR:
        raise HTTPException(status_code=400, detail=f"Minimum payout is PKR {MIN_PAYOUT_PKR}.")

    doctor_doc   = get_doctor_doc(doctor["user_id"]) or {}
    number       = doctor_doc.get("jazzcash_number")
    account_name = doctor_doc.get("jazzcash_name", doctor.get("name", "Doctor"))
    if not number:
        raise HTTPException(status_code=400, detail="Please add your JazzCash account first before withdrawing.")

    available = _compute_balance(doctor["user_id"])["available_pkr"]
    if body.amount_pkr > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient available balance. Available: PKR {available:,}. Requested: PKR {body.amount_pkr:,}.",
        )

    now = datetime.now(timezone.utc).isoformat()
    payout_ref = _col(COL_PAYOUTS).add({
        "doctor_id":       doctor["user_id"],
        "doctor_name":     doctor.get("name", ""),
        "amount_pkr":      body.amount_pkr,
        "account_name":    account_name,
        "jazzcash_number": number,
        "number_last4":    number[-4:],
        "status":          "requested",
        "created_at":      now,
    })
    payout_id = payout_ref[1].id if isinstance(payout_ref, tuple) else payout_ref.id

    # Notify admins that a payout needs to be processed manually.
    try:
        for admin_doc in _col(COL_USERS).where("role", "==", "admin").stream():
            await send_push_notification(
                user_id = admin_doc.id,
                title   = "Payout Requested",
                body    = f"Dr. {doctor.get('name', '')} requested PKR {body.amount_pkr:,} to JazzCash {number}.",
                data    = {"type": "payout_requested", "payout_id": payout_id},
            )
    except Exception as e:
        logger.error("Failed to notify admins of payout request: %s", e)

    return {
        "message":         "Payout requested. An admin will transfer the funds to your JazzCash account shortly.",
        "payout_id":       payout_id,
        "amount_pkr":      body.amount_pkr,
        "account_name":    account_name,
        "jazzcash_number": number,
        "number_last4":    number[-4:],
        "status":          "requested",
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
