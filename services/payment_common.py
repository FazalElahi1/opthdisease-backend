"""
payment_common.py
=================
Shared payment helpers used by the payment routers. Extracted from
jazzcash_payments.py so the Stripe router (stripe_payments.py) can reuse the fee
resolution + transaction-ref generation without importing the (now-unregistered)
JazzCash router.
"""

import secrets
from datetime import datetime, timezone


def resolve_fee(doctor_doc: dict, slot_start_time: str) -> int:
    """Consultation fee for a slot — a per-window price overrides the doctor's
    default price (same rule used by appointments/slots)."""
    for window in doctor_doc.get("availabilitySlots", []):
        start     = window.get("startTime", window.get("start_time", ""))
        end       = window.get("endTime",   window.get("end_time",   ""))
        win_price = int(window.get("price", window.get("price_pkr", 0)) or 0)
        if win_price > 0 and start <= slot_start_time < end:
            return win_price
    return int(doctor_doc.get("price", doctor_doc.get("price_pkr", 0)) or 0)


def generate_txn_ref() -> str:
    """Unique payment-session token. 'T' + 14-digit UTC timestamp + 6 hex chars."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"T{ts}{secrets.token_hex(3)}"
