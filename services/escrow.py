"""
services/escrow.py
==================
Escrow lifecycle for consultation payments.

Money is held by the platform after a call completes and only becomes payable
to the doctor once a cooling period passes WITH NO complaint. A background
APScheduler job runs periodically and performs the automatic release.

Appointment escrow_status values
  held      → completed call, money held, cooling not elapsed (or just elapsed, awaiting release)
  disputed  → patient said the call didn't happen / filed a complaint → release blocked
  released  → cooling elapsed, no complaint → payable to doctor (admin pays out manually)
  refunded  → admin refunded the patient

Release rule: status == "completed" AND escrow_status == "held" AND now >= release_at.
Disputed/refunded appointments are excluded because their escrow_status != "held".
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from services.firebase import (
    _col, set_doc,
    COL_APPOINTMENTS, COL_USERS,
)
from services.notifications import send_push_notification

logger = logging.getLogger(__name__)

RELEASE_INTERVAL_MINUTES = int(os.getenv("ESCROW_RELEASE_INTERVAL_MINUTES", "15"))

_scheduler = None


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _admin_user_ids() -> list[str]:
    try:
        docs = _col(COL_USERS).where("role", "==", "admin").stream()
        return [d.id for d in docs]
    except Exception as e:
        logger.error("escrow: could not list admins: %s", e)
        return []


def release_due_escrows() -> int:
    """
    Release all held escrows whose cooling period has elapsed and that have no
    complaint. Returns the number released. Safe to run repeatedly (idempotent).
    """
    now = datetime.now(timezone.utc)
    released = 0

    try:
        docs = (
            _col(COL_APPOINTMENTS)
            .where("status", "==", "completed")
            .where("escrow_status", "==", "held")
            .stream()
        )
    except Exception as e:
        logger.error("escrow: query failed: %s", e)
        return 0

    admins = _admin_user_ids()

    for doc in docs:
        appt = doc.to_dict()
        release_at = _parse_iso(appt.get("release_at"))
        if release_at is None or now < release_at:
            continue

        appt_id = appt.get("appointment_id", doc.id)
        set_doc(COL_APPOINTMENTS, appt_id, {
            "escrow_status": "released",
            "released_at":   now.isoformat(),
        })
        released += 1

        amount = appt.get("price_pkr", 0)
        # Notify the doctor (fire-and-forget).
        _notify(
            appt.get("doctor_id"),
            "Earnings Released",
            f"PKR {amount:,} from your consultation with {appt.get('patient_name', 'a patient')} "
            f"is now available to withdraw.",
            {"type": "earnings_released", "appointment_id": appt_id},
        )
        for admin_id in admins:
            _notify(
                admin_id,
                "Payout Due",
                f"PKR {amount:,} released to Dr. {appt.get('doctor_name', '')} "
                f"(appointment {appt_id[:8]}).",
                {"type": "payout_due", "appointment_id": appt_id},
            )

    if released:
        logger.info("escrow: released %d escrow(s)", released)
    return released


def _notify(user_id, title, body, data):
    """Run the async push helper from this sync (scheduler-thread) context."""
    if not user_id:
        return
    try:
        asyncio.run(send_push_notification(user_id=user_id, title=title, body=body, data=data))
    except RuntimeError:
        # An event loop is already running in this thread — schedule it instead.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(send_push_notification(user_id=user_id, title=title, body=body, data=data))
        finally:
            loop.close()
    except Exception as e:
        logger.error("escrow: notify failed for %s: %s", user_id, e)


def start_scheduler() -> None:
    """Start the periodic auto-release job. Called once at app startup."""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.error("apscheduler not installed — escrow auto-release disabled. "
                     "Run: pip install apscheduler")
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        release_due_escrows,
        "interval",
        minutes=RELEASE_INTERVAL_MINUTES,
        id="escrow_auto_release",
        next_run_time=datetime.now(timezone.utc),   # run once shortly after boot
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("escrow: auto-release scheduler started (every %d min)", RELEASE_INTERVAL_MINUTES)
