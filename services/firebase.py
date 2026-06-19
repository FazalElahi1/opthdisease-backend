"""
services/firebase.py
────────────────────
Central Firebase Admin SDK initialisation + Firestore / Storage helpers.

Firestore collection layout
────────────────────────────
  app_data/                          ← top-level "folder" document (never written to directly)
    users/{user_id}                  ← patient & doctor shared auth fields
    doctors/{user_id}                ← doctor-specific profile / license fields
    appointments/{appointment_id}
    scans/{scan_id}
    notifications/{notification_id}
    calls/{call_id}

Firebase Storage layout
───────────────────────
  profile-images/users/{user_id}/avatar.jpg
  profile-images/doctors/{user_id}/avatar.jpg
  scans/{user_id}/{scan_id}/original.jpg
  scans/{user_id}/{scan_id}/heatmap.jpg
"""

import os
import logging
from functools import lru_cache
from typing import Optional

import firebase_admin
from firebase_admin import credentials, auth, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Root "namespace" prefix – every collection lives under this path segment.
# Changing APP_ROOT is the only thing you need to do to move everything into
# a different logical folder (e.g. "v2_data" for a migration).
APP_ROOT = "app_data"

# Collection paths (keep them central so a rename is a one-line change)
COL_USERS             = f"{APP_ROOT}/users"         # NOT a real sub-collection path in Firestore;
COL_DOCTORS           = f"{APP_ROOT}/doctors"       # see _col() below for the correct approach.
COL_APPOINTMENTS      = f"{APP_ROOT}/appointments"
COL_SCANS             = f"{APP_ROOT}/scans"
COL_NOTIFICATIONS     = f"{APP_ROOT}/notifications"
COL_CALLS             = f"{APP_ROOT}/calls"
COL_PAYMENT_SESSIONS  = f"{APP_ROOT}/payment_sessions"
COL_PAYMENT_LOGS      = f"{APP_ROOT}/payment_logs"
COL_PAYOUTS           = f"{APP_ROOT}/payouts"
COL_REVIEWS           = f"{APP_ROOT}/reviews"


_app: Optional[firebase_admin.App] = None


# ── SDK init ──────────────────────────────────────────────────────────────────

def get_firebase_app() -> firebase_admin.App:
    """Initialise Firebase Admin SDK once and reuse (thread-safe via global lock)."""
    global _app
    if _app is None:
        import json as _json

        cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
        if cred_json:
            # Render/cloud deployment: full JSON stored as env var
            cred = credentials.Certificate(_json.loads(cred_json))
        else:
            # Local development: read from file
            cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "./firebase-credentials.json")
            if not os.path.exists(cred_path):
                raise FileNotFoundError(
                    f"Firebase credentials not found at '{cred_path}'. "
                    "Set FIREBASE_CREDENTIALS_PATH or FIREBASE_CREDENTIALS_JSON in your .env."
                )
            cred = credentials.Certificate(cred_path)

        _app = firebase_admin.initialize_app(cred)
        _init_cloudinary()
        logger.info("Firebase Admin SDK initialised (project: %s)", _app.project_id)
    return _app


@lru_cache(maxsize=1)
def get_firestore_client():
    """Return a cached Firestore client."""
    get_firebase_app()
    return firestore.client()


def get_auth_client():
    """Return Firebase Auth module."""
    get_firebase_app()
    return auth


def _init_cloudinary():
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )


# ── Namespace anchor ───────────────────────────────────────────────────────────

def ensure_namespace_anchor() -> None:
    """
    Write a lightweight stub to 'app_data/meta' so the APP_ROOT folder
    appears in the Firebase console immediately, before any user data exists.
    Uses set(merge=True) so it never overwrites existing fields.
    Call once at application startup.
    """
    db = get_firestore_client()
    db.document(f"{APP_ROOT}/meta").set(
        {"_created_by": "firebase.py", "_note": "namespace anchor – do not delete"},
        merge=True,
    )
    logger.debug("Firestore namespace anchor ensured at '%s/meta'", APP_ROOT)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _col(path: str):
    """
    Return a Firestore CollectionReference for a namespaced sub-collection.

    Because Firestore sub-collections require an alternating
    document → collection → document path, we root everything under
    the anchor document 'app_data/meta':

        app_data/meta  (document)
          └── users    (sub-collection)
          └── doctors  (sub-collection)
          └── ...

    Usage:
        _col(COL_USERS).document(uid).set(data)
    """
    db = get_firestore_client()
    # COL_USERS == "app_data/users"  →  anchor = "app_data/meta",  sub = "users"
    parts     = path.split("/")          # ["app_data", "users"]
    namespace = parts[0]                 # "app_data"
    sub       = "/".join(parts[1:])      # "users"
    return db.document(f"{namespace}/meta").collection(sub)


# ── User helpers ───────────────────────────────────────────────────────────────

def get_user_doc(user_id: str) -> Optional[dict]:
    """Fetch a user document; returns None if it doesn't exist."""
    if not user_id:
        raise ValueError("user_id must not be empty")
    doc = _col(COL_USERS).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def set_user_doc(user_id: str, data: dict) -> None:
    """Create or merge-update a user document. Never overwrites unset fields."""
    if not user_id:
        raise ValueError("user_id must not be empty")
    if not data:
        raise ValueError("data must not be empty")
    # Scrub any accidental None values before writing
    clean = {k: v for k, v in data.items() if v is not None}
    _col(COL_USERS).document(user_id).set(clean, merge=True)


def delete_user_doc(user_id: str) -> None:
    """Hard-delete a user document (use sparingly; prefer soft-delete)."""
    _col(COL_USERS).document(user_id).delete()


# ── Doctor helpers ─────────────────────────────────────────────────────────────

def get_doctor_doc(user_id: str) -> Optional[dict]:
    """Fetch a doctor document; returns None if it doesn't exist."""
    if not user_id:
        raise ValueError("user_id must not be empty")
    doc = _col(COL_DOCTORS).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def set_doctor_doc(user_id: str, data: dict) -> None:
    """Create or merge-update a doctor document."""
    if not user_id:
        raise ValueError("user_id must not be empty")
    if not data:
        raise ValueError("data must not be empty")
    clean = {k: v for k, v in data.items() if v is not None}
    _col(COL_DOCTORS).document(user_id).set(clean, merge=True)


def delete_doctor_doc(user_id: str) -> None:
    _col(COL_DOCTORS).document(user_id).delete()


# ── Query helpers ──────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[dict]:
    """Find the first user document matching the given email."""
    if not email:
        raise ValueError("email must not be empty")
    docs = (
        _col(COL_USERS)
        .where(filter=FieldFilter("email", "==", email))
        .limit(1)
        .stream()
    )
    for doc in docs:
        return {**doc.to_dict(), "user_id": doc.id}
    return None


def normalise_license(license_number: str) -> str:
    """Canonical form used for storage + uniqueness checks (case/space-insensitive)."""
    return "".join((license_number or "").split()).upper()


def get_doctor_by_license(license_number: str) -> Optional[dict]:
    """
    Find a doctor by (normalised) license number. Returns None if none exists.
    Two doctors may share a name, but never the same license number.
    """
    norm = normalise_license(license_number)
    if not norm:
        return None
    docs = (
        _col(COL_DOCTORS)
        .where(filter=FieldFilter("license_number_norm", "==", norm))
        .limit(1)
        .stream()
    )
    for doc in docs:
        return {**doc.to_dict(), "user_id": doc.id}
    return None


def list_verified_doctors(specialty: Optional[str] = None) -> list[dict]:
    """
    Return all doctors with license_status == 'verified'.
    Optionally filter by specialty (case-insensitive).
    """
    query = _col(COL_DOCTORS).where(filter=FieldFilter("license_status", "==", "verified"))
    doctors = []
    for doc in query.stream():
        d = {**doc.to_dict(), "user_id": doc.id}
        d.pop("password_hash", None)
        if specialty:
            specs = [s.lower() for s in d.get("specialties", [])]
            if specialty.lower() not in specs:
                continue
        doctors.append(d)
    return doctors


# ── Image / Storage helpers (Cloudinary) ──────────────────────────────────────

def upload_profile_image(user_id: str, file_bytes: bytes, role: str = "users") -> str:
    import io
    result = cloudinary.uploader.upload(
        io.BytesIO(file_bytes),
        public_id=f"profile-images/{role}/{user_id}/avatar",
        overwrite=True,
        resource_type="image",
    )
    url = result["secure_url"]
    logger.info("Uploaded profile image for %s/%s → %s", role, user_id, url)
    return url


def get_profile_image_url(user_id: str, role: str = "users") -> Optional[str]:
    doc = get_user_doc(user_id) if role == "users" else get_doctor_doc(user_id)
    if doc and doc.get("image_url"):
        return doc["image_url"]
    return None


# ── Generic collection helpers (used by appointments, scans, etc.) ─────────────

def get_doc(collection_path: str, doc_id: str) -> Optional[dict]:
    doc = _col(collection_path).document(doc_id).get()
    return doc.to_dict() if doc.exists else None


def set_doc(collection_path: str, doc_id: str, data: dict) -> None:
    if not data:
        raise ValueError("data must not be empty")
    clean = {k: v for k, v in data.items() if v is not None}
    _col(collection_path).document(doc_id).set(clean, merge=True)


def add_doc(collection_path: str, data: dict) -> str:
    """Add a new document with an auto-generated ID. Returns the new document ID."""
    if not data:
        raise ValueError("data must not be empty")
    clean = {k: v for k, v in data.items() if v is not None}
    ref = _col(collection_path).add(clean)
    # firebase_admin returns (update_time, DocumentReference)
    doc_ref = ref[1] if isinstance(ref, tuple) else ref
    return doc_ref.id


def delete_doc(collection_path: str, doc_id: str) -> None:
    _col(collection_path).document(doc_id).delete()