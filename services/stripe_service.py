"""
stripe_service.py
=================
Thin wrapper around the Stripe SDK for **Hosted Checkout** (patient pays the
platform). Mirrors the role jazzcash_service.py played for JazzCash.

Adapted from D:\\fyp\\stripe\\stripe_service.py — switched from PaymentIntents to
Checkout Sessions, and FIXED the currency unit: PKR is a 2-decimal Stripe
currency, so amounts are sent in paisa (rupees × 100). The original folder code
wrongly treated PKR as zero-decimal.

Env:
  STRIPE_SECRET_KEY      sk_test_… (test mode works from any country)
  STRIPE_WEBHOOK_SECRET  whsec_…   (from the Stripe dashboard webhook)
  STRIPE_CURRENCY        defaults to "pkr"; set "usd" if your test account
                         rejects PKR presentment (demo fallback).
"""

from __future__ import annotations   # defer annotation eval so `dict | None` works on Python 3.9

import os
import logging

import stripe

logger = logging.getLogger(__name__)

stripe.api_key        = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_CURRENCY       = os.getenv("STRIPE_CURRENCY", "pkr").lower()

# Zero-decimal currencies take the amount as-is; everything else is ×100.
_ZERO_DECIMAL = {"jpy", "krw", "vnd", "clp", "pyg", "xof", "xaf"}

if not stripe.api_key:
    logger.warning(
        "STRIPE_SECRET_KEY is empty — set it (sk_test_…) before taking payments."
    )


def _smallest_unit(amount_pkr: int) -> int:
    return int(amount_pkr) if STRIPE_CURRENCY in _ZERO_DECIMAL else int(amount_pkr) * 100


def create_checkout_session(
    amount_pkr:  int,
    txn_ref:     str,
    description: str,
    success_url: str,
    cancel_url:  str,
    metadata:    dict | None = None,
) -> dict:
    """Create a Stripe Hosted Checkout Session. Returns {session_id, checkout_url}."""
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency":     STRIPE_CURRENCY,
                "unit_amount":  _smallest_unit(amount_pkr),
                "product_data": {"name": description[:127] or "Consultation"},
            },
            "quantity": 1,
        }],
        success_url          = success_url,
        cancel_url           = cancel_url,
        client_reference_id  = txn_ref,
        metadata             = {"token": txn_ref, **(metadata or {})},
    )
    return {"session_id": session.id, "checkout_url": session.url}


def retrieve_session(session_id: str) -> dict:
    """Retrieve a Checkout Session. Returns {paid: bool, payment_intent, raw}."""
    s = stripe.checkout.Session.retrieve(session_id)
    return {
        "paid":           s.get("payment_status") == "paid",
        "payment_intent": s.get("payment_intent"),
        "raw":            s,
    }


def verify_webhook(payload: bytes, sig_header: str):
    """Verify a Stripe webhook signature and return the event (raises on bad sig)."""
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)


def refund_payment(payment_intent_id: str, reason: str = "requested_by_customer"):
    """Refund a captured payment by its PaymentIntent id."""
    return stripe.Refund.create(payment_intent=payment_intent_id, reason=reason)
