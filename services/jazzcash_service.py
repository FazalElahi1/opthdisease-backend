"""
jazzcash_service.py
===================
Real JazzCash **sandbox** integration (replaces safepay_service.py).

Money-in only — JazzCash's HTTP-POST "Page Redirection" (Hosted Checkout).
The patient is shown JazzCash's own hosted page where they can pay with a
card OR their JazzCash mobile-wallet account; they choose there. The doctor's
phone number is never involved.

Flow
----
  1. create_checkout(...) builds the signed pp_* field set + the JazzCash POST URL.
  2. The /jazzcash router stores those fields and hosts a tiny auto-submitting
     HTML form (so the app's WebView just opens a URL, never a raw POST).
  3. Patient pays on JazzCash's hosted page.
  4. JazzCash POSTs the result back to pp_ReturnURL → verify_secure_hash() +
     pp_ResponseCode == "000" ⇒ paid.
  5. payment_inquiry() is a real status check used as a polling fallback.

NOTE: JazzCash sandbox has **no** self-service disbursement/payout API for these
merchant credentials, so there is intentionally no "send money to doctor" call
here — doctor payouts are released in Firestore and paid out manually by the
admin (see routers/jazzcash_payouts.py).

ENV (.env)
----------
  JAZZCASH_MERCHANT_ID
  JAZZCASH_PASSWORD
  JAZZCASH_INTEGRITY_SALT
  JAZZCASH_ENV           = sandbox | production
  APP_BASE_URL           = public backend base URL (ngrok in dev)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────

MERCHANT_ID    = os.getenv("JAZZCASH_MERCHANT_ID", "")
PASSWORD       = os.getenv("JAZZCASH_PASSWORD", "")
INTEGRITY_SALT = os.getenv("JAZZCASH_INTEGRITY_SALT", "")
JAZZCASH_ENV   = os.getenv("JAZZCASH_ENV", "sandbox").lower()
APP_BASE_URL   = os.getenv("APP_BASE_URL", "").rstrip("/")
# pp_TxnType for the hosted checkout. Empty "" = JazzCash's dual card+wallet page,
# but some sandbox merchants are only enabled for a single type and then reject the
# transaction with "insufficient merchant information". "MWALLET" forces the JazzCash
# mobile-wallet flow (usually enabled on sandbox). Override via env to experiment:
#   MWALLET (wallet), MPAY / MIGS (card), or "" (dual page).
TXN_TYPE       = os.getenv("JAZZCASH_TXN_TYPE", "MWALLET")

CURRENCY = "PKR"
# JazzCash uses Pakistan Standard Time (UTC+5) for txn timestamps.
PKT = timezone(timedelta(hours=5))

# Page-redirection (Hosted Checkout) post target + inquiry API.
if JAZZCASH_ENV == "production":
    CHECKOUT_POST_URL = "https://payments.jazzcash.com.pk/CustomerPortal/transactionmanagement/merchantform/"
    INQUIRY_URL       = "https://payments.jazzcash.com.pk/ApplicationAPI/API/PaymentInquiry/Inquire"
else:
    CHECKOUT_POST_URL = "https://sandbox.jazzcash.com.pk/CustomerPortal/transactionmanagement/merchantform/"
    INQUIRY_URL       = "https://sandbox.jazzcash.com.pk/ApplicationAPI/API/PaymentInquiry/Inquire"

PP_VERSION = "1.1"

if not (MERCHANT_ID and PASSWORD and INTEGRITY_SALT):
    logger.warning(
        "JazzCash credentials missing — set JAZZCASH_MERCHANT_ID / JAZZCASH_PASSWORD / "
        "JAZZCASH_INTEGRITY_SALT in .env before taking payments."
    )


# ── Secure hash ──────────────────────────────────────────────────────────────────

def _secure_hash(fields: dict) -> str:
    """
    JazzCash HMAC-SHA256 secure hash.

    Algorithm (matches JazzCash's official HTTP-POST sample):
      - take every parameter whose key starts with 'pp_' or 'ppmpf' that has a
        non-empty value,
      - sort those by key (ascending),
      - build  IntegritySalt + '&' + v1 + '&' + v2 + ... (values only, in key order),
      - HMAC-SHA256 keyed with the IntegritySalt, hex digest.
    """
    keys = sorted(
        k for k, v in fields.items()
        if (k.startswith("pp_") or k.startswith("ppmpf")) and str(v) != ""
    )
    message = INTEGRITY_SALT + "".join("&" + str(fields[k]) for k in keys)
    return hmac.new(
        INTEGRITY_SALT.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_secure_hash(fields: dict) -> bool:
    """Verify a hash returned by JazzCash (constant-time, case-insensitive hex)."""
    received = str(fields.get("pp_SecureHash", ""))
    if not received:
        return False
    expected = _secure_hash({k: v for k, v in fields.items() if k != "pp_SecureHash"})
    return hmac.compare_digest(expected.lower(), received.lower())


# ── Reference generator ──────────────────────────────────────────────────────────

def generate_txn_ref() -> str:
    """
    A unique JazzCash transaction reference (also used as our session token).
    JazzCash limits pp_TxnRefNo to 20 characters, so: 'T' + 14-digit timestamp
    + 4 hex chars = 19 chars.
    """
    return "T" + datetime.now(PKT).strftime("%Y%m%d%H%M%S") + secrets.token_hex(2).upper()


# ── Checkout (money in) ──────────────────────────────────────────────────────────

def build_checkout(
    *,
    amount_pkr:   int,
    txn_ref:      str,
    bill_ref:     str,
    description:  str,
    return_url:   str,
    expiry_hours: int = 1,
) -> dict:
    """
    Build the signed pp_* field set for the JazzCash Hosted Checkout page.
    Returns {post_url, fields} — the router renders an auto-submitting form.

    pp_TxnType is left empty so JazzCash's hosted page offers BOTH card and
    mobile-wallet; the patient picks one there.
    """
    now    = datetime.now(PKT)
    expiry = now + timedelta(hours=expiry_hours)

    fields = {
        "pp_Version":          PP_VERSION,
        "pp_TxnType":          TXN_TYPE,                 # default MWALLET; "" = dual card+wallet page (see TXN_TYPE)
        "pp_Language":         "EN",
        "pp_MerchantID":       MERCHANT_ID,
        "pp_SubMerchantID":    "",
        "pp_Password":         PASSWORD,
        "pp_BankID":           "",
        "pp_ProductID":        "",
        "pp_TxnRefNo":         txn_ref,
        "pp_Amount":           str(int(amount_pkr) * 100),   # paisa, no decimals
        "pp_TxnCurrency":      CURRENCY,
        "pp_TxnDateTime":      now.strftime("%Y%m%d%H%M%S"),
        "pp_BillReference":    bill_ref,
        "pp_Description":      description[:255],
        "pp_TxnExpiryDateTime": expiry.strftime("%Y%m%d%H%M%S"),
        "pp_ReturnURL":        return_url,
        "ppmpf_1":             bill_ref,                 # echoed back on return
    }
    fields["pp_SecureHash"] = _secure_hash(fields)
    return {"post_url": CHECKOUT_POST_URL, "fields": fields}


# ── Payment inquiry (real status check) ──────────────────────────────────────────

def payment_inquiry(txn_ref: str) -> dict:
    """
    Real JazzCash Payment Inquiry. Returns {status, response_code, raw}.
    status ∈ {"paid", "pending", "failed"}.
    Used as a polling fallback — the authoritative confirmation is the signed
    return POST handled by the router.
    """
    req = {
        "pp_TxnRefNo":   txn_ref,
        "pp_MerchantID": MERCHANT_ID,
        "pp_Password":   PASSWORD,
    }
    req["pp_SecureHash"] = _secure_hash(req)

    try:
        with httpx.Client(timeout=15) as client:
            res = client.post(INQUIRY_URL, json=req)
        res.raise_for_status()
        data = res.json()
    except httpx.HTTPStatusError as e:
        logger.error("JazzCash inquiry HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return {"status": "pending", "response_code": None, "raw": {}}
    except httpx.RequestError as e:
        logger.error("JazzCash inquiry connection error: %s", e)
        return {"status": "pending", "response_code": None, "raw": {}}

    code = str(data.get("pp_ResponseCode", ""))
    status = "paid" if code == "000" else ("pending" if code in ("", "124", "157") else "failed")
    return {"status": status, "response_code": code, "raw": data}
