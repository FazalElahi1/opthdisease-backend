"""
Recovery script: recreates the admin Firestore user document.

Firebase Auth is NOT affected (passwords are stored there, not in Firestore).
Run once after accidentally deleting Firestore data.

Usage:
    python create_admin.py
"""

import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Bootstrap Firebase (same as the app does at startup) ──────────────────────
from services.firebase import get_firebase_app, _col, COL_USERS
get_firebase_app()

from firebase_admin import auth as firebase_auth
from passlib.context import CryptContext

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_EMAIL = "f.elahi1767@gmail.com"
ADMIN_NAME  = "Admin"


def main():
    print("\n=== Admin Account Recovery ===\n")

    # 1. Look up existing Firebase Auth UID
    try:
        fb_user = firebase_auth.get_user_by_email(ADMIN_EMAIL)
        uid = fb_user.uid
        print(f"Found Firebase Auth account  →  UID: {uid}")
    except firebase_auth.UserNotFoundError:
        print(f"No Firebase Auth account found for {ADMIN_EMAIL}.")
        print("Creating a new Firebase Auth account...")
        password = input("Enter a password for the admin account: ").strip()
        if len(password) < 7:
            print("Error: password must be at least 7 characters.")
            sys.exit(1)
        fb_user = firebase_auth.create_user(
            email        = ADMIN_EMAIL,
            password     = password,
            display_name = ADMIN_NAME,
        )
        uid = fb_user.uid
        print(f"Created Firebase Auth account  →  UID: {uid}")
        _write_admin_doc(uid, password)
        return

    # 2. Firebase Auth user exists — ask for the password so we can store
    #    the hash in Firestore (needed for /auth/login email+password flow).
    password = input(
        f"\nEnter the password for {ADMIN_EMAIL}\n"
        "(this is the password you already use — we just re-store the hash): "
    ).strip()
    if len(password) < 7:
        print("Error: password must be at least 7 characters.")
        sys.exit(1)

    _write_admin_doc(uid, password)


def _write_admin_doc(uid: str, password: str) -> None:
    now = datetime.now(timezone.utc).isoformat()

    doc = {
        "user_id":        uid,
        "email":          ADMIN_EMAIL,
        "name":           ADMIN_NAME,
        "role":           "admin",
        "phone":          "",
        "age":            None,
        "gender":         "",
        "is_verified":    True,
        "is_active":      True,
        "created_at":     now,
        "auth_method":    "email",
        "password_hash":  pwd_ctx.hash(password),
        "license_status": "n/a",
        "session_token":  "",   # cleared — a fresh token is issued on next login
    }

    # Remove None values (set_user_doc already does this, but be explicit)
    doc = {k: v for k, v in doc.items() if v is not None}

    _col(COL_USERS).document(uid).set(doc)
    print(f"\nFirestore document written at users/{uid}")
    print("\nAdmin account ready. Log in through the app using:")
    print(f"  Email:    {ADMIN_EMAIL}")
    print(f"  Password: (what you just entered)")
    print("  Role:     Patient  (the app routes by email, not role)")
    print()


if __name__ == "__main__":
    main()
