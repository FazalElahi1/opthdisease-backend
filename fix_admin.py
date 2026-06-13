"""Direct admin recovery — writes the Firestore doc using the known UID."""
from dotenv import load_dotenv
load_dotenv()
from services.firebase import get_firebase_app, _col, COL_USERS
get_firebase_app()
from passlib.context import CryptContext
from datetime import datetime, timezone

pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
UID      = "jmIsHQucIMZaPNOlkzy1NnPKWHV2"
EMAIL    = "f.elahi1767@gmail.com"

password = input("Set a password for the admin account: ").strip()
if not password:
    print("Password cannot be empty.")
    raise SystemExit(1)

now = datetime.now(timezone.utc).isoformat()
doc = {
    "user_id":        UID,
    "email":          EMAIL,
    "name":           "Admin",
    "role":           "admin",
    "phone":          "",
    "is_verified":    True,
    "is_active":      True,
    "created_at":     now,
    "auth_method":    "email",
    "password_hash":  pwd_ctx.hash(password),
    "license_status": "n/a",
    "session_token":  "",
}

_col(COL_USERS).document(UID).set(doc)
print(f"\nDone. Log in with:\n  Email:    {EMAIL}\n  Password: {password}\n  Role:     Patient (app routes by email)")
