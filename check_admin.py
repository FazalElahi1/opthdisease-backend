"""Quick check — prints the admin Firestore document (no password hash)."""
from dotenv import load_dotenv
load_dotenv()
from services.firebase import get_firebase_app, _col, COL_USERS
get_firebase_app()
from firebase_admin import auth as firebase_auth

ADMIN_EMAIL = "f.elahi1767@gmail.com"

try:
    fb = firebase_auth.get_user_by_email(ADMIN_EMAIL)
    print(f"Firebase Auth UID : {fb.uid}")
except Exception as e:
    print(f"Firebase Auth     : NOT FOUND ({e})")
    raise SystemExit(1)

doc = _col(COL_USERS).document(fb.uid).get()
if not doc.exists:
    print("Firestore doc     : NOT FOUND — run create_admin.py first")
else:
    d = doc.to_dict()
    d.pop("password_hash", None)
    print("Firestore doc     : OK")
    for k, v in d.items():
        print(f"  {k:<20} {v}")
