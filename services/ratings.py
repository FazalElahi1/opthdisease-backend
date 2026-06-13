"""
services/ratings.py
===================
Weekly doctor-rating snapshot.

The doctor's aggregate rating (shown both on the doctor's own dashboard and in
the patient "search doctors" listing) is NOT updated the instant a new review
arrives. Instead a background APScheduler job recomputes every doctor's average
once a week (Monday 00:00 UTC). This keeps the two surfaces aligned and prevents
a single new review from instantly swinging a doctor's public listing.

Written fields on each doctor doc:
  avg_rating          → rounded mean of all that doctor's review ratings
  review_count        → number of ratings counted
  rating_snapshot_at  → ISO timestamp of the last recompute
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.firebase import _col, set_doctor_doc, COL_REVIEWS

logger = logging.getLogger(__name__)

_scheduler = None


def recompute_all_doctor_ratings() -> int:
    """
    Recompute avg_rating + review_count for every doctor from the full reviews
    collection and write the snapshot onto each doctor doc. Returns the number of
    doctors updated. Safe to run repeatedly (idempotent).
    """
    now = datetime.now(timezone.utc)

    # Aggregate ratings per doctor in a single pass over reviews.
    totals: dict[str, list[int]] = {}
    try:
        for doc in _col(COL_REVIEWS).stream():
            r = doc.to_dict()
            doctor_id = r.get("doctor_id")
            rating = r.get("rating")
            if doctor_id and rating:
                totals.setdefault(doctor_id, []).append(rating)
    except Exception as e:
        logger.error("ratings: failed to read reviews: %s", e)
        return 0

    updated = 0
    for doctor_id, ratings in totals.items():
        if not ratings:
            continue
        try:
            set_doctor_doc(doctor_id, {
                "avg_rating":         round(sum(ratings) / len(ratings), 1),
                "review_count":       len(ratings),
                "rating_snapshot_at": now.isoformat(),
            })
            updated += 1
        except Exception as e:
            logger.error("ratings: failed to update doctor %s: %s", doctor_id, e)

    logger.info("ratings: weekly snapshot updated %d doctor(s)", updated)
    return updated


def start_rating_scheduler() -> None:
    """Start the weekly rating-snapshot job. Called once at app startup."""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error("apscheduler not installed — weekly rating snapshot disabled. "
                     "Run: pip install apscheduler")
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        recompute_all_doctor_ratings,
        CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC"),
        id="weekly_rating_snapshot",
        next_run_time=datetime.now(timezone.utc),   # seed once shortly after boot
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("ratings: weekly snapshot scheduler started (Mon 00:00 UTC)")
