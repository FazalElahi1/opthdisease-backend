"""
Recreates a Firestore user document for any patient/doctor whose
Firebase Auth account still exists but whose Firestore doc was deleted.

Usage:
    python recover_user.py
"""
from dotenv import load_dotenv
load_dotenv()
from services.firebase import get_firebase_app, _col, COL_USERS
get_firebase_app()
from firebase_admin import auth as firebase_auth
from passlib.context import CryptContext
from datetime import datetime, timezone

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

email = input("Enter the user's email: ").strip().lower()

# 1. Check Firebase Auth
try:
    fb = firebase_auth.get_user_by_email(email)
    uid = fb.uid
    print(f"Firebase Auth found  →  UID: {uid}")
except firebase_auth.UserNotFoundError:
    print(f"No Firebase Auth account found for {email}. They must register fresh.")
    raise SystemExit(0)

# 2. Check if Firestore doc already exists
existing = _col(COL_USERS).document(uid).get()
if existing.exists:
    d = existing.to_dict()
    print(f"Firestore doc already exists  →  role: {d.get('role')}, email: {d.get('email')}")
    confirm = input("Overwrite it? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        raise SystemExit(0)

# 3. Collect info
role = input("Role (patient/doctor): ").strip().lower()
if role not in ("patient", "doctor"):
    print("Role must be patient or doctor.")
    raise SystemExit(1)

name     = input("Name: ").strip() or email.split("@")[0]
password = input("New password (min 7 chars): ").strip()
if len(password) < 7:
    print("Password must be at least 7 characters.")
    raise SystemExit(1)

now = datetime.now(timezone.utc).isoformat()
doc = {
    "user_id":        uid,
    "email":          email,
    "name":           name,
    "role":           role,
    "phone":          "",
    "is_verified":    True,
    "is_active":      True,
    "created_at":     now,
    "auth_method":    "email",
    "password_hash":  pwd_ctx.hash(password),
    "license_status": "pending" if role == "doctor" else "n/a",
    "session_token":  "",
}

_col(COL_USERS).document(uid).set(doc)
print(f"\nDone. {name} ({email}) can now log in as {role} with the password you set.")
