from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth as firebase_auth
from passlib.context import CryptContext
from datetime import datetime, timezone
import secrets
import os
import httpx

from models.schemas import (
    RegisterRequest, LoginRequest, GoogleSignInRequest,
    TokenResponse, MessageResponse, UserRole, ForgotPasswordRequest,
)
from services.firebase import (
    get_firestore_client,
    get_user_doc, set_user_doc,
    get_doctor_doc, set_doctor_doc,
    get_user_by_email,
    get_doctor_by_license, normalise_license,
)
from services.jwt import create_access_token, decode_access_token
from services.email_service import (
    send_doctor_application_received,
    send_admin_new_doctor_application,
    send_password_reset_email,
    send_patient_verification_email,
)
from services.notifications import send_push_notification

router  = APIRouter(prefix="/auth", tags=["Authentication"])
bearer  = HTTPBearer()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY", "AIzaSyBmkbN1VBW3Z_sXGwddjMhFDZKymWIq0g4")
GMAIL_USER           = os.getenv("GMAIL_USER", "")
ADMIN_EMAIL          = os.getenv("ADMIN_EMAIL", "")

async def _firebase_verify_password(email: str, password: str) -> bool:
    """Verify email/password against Firebase Auth REST API (used after a password reset)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}",
                json={"email": email, "password": password, "returnSecureToken": False},
            )
        return r.status_code == 200
    except Exception:
        return False


# ── Helpers ────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


# ── CHANGE 2: build_token_response now generates + saves a session_token ───────
# OLD CODE WAS:
#   def build_token_response(user_doc: dict, is_new_user: bool = False) -> TokenResponse:
#       latest = get_user_doc(user_doc["user_id"])
#       if latest:
#           user_doc = {**user_doc, **latest, "user_id": user_doc["user_id"]}
#       token = create_access_token({
#           "sub":   user_doc["user_id"],
#           "email": user_doc["email"],
#           "role":  user_doc["role"],
#       })
#       return TokenResponse(...)
#
# NEW CODE: generates a session_token, saves it to Firestore, embeds it in JWT.
# This is the ONLY place tokens are issued — covers register, login, and Google.
def build_token_response(user_doc: dict, is_new_user: bool = False) -> TokenResponse:
    latest = get_user_doc(user_doc["user_id"])
    if latest:
        user_doc = {**user_doc, **latest, "user_id": user_doc["user_id"]}

    # Generate a fresh random session token on every login/register
    session_token = secrets.token_hex(32)

    # Persist it to Firestore so protected endpoints can validate against it
    set_user_doc(user_doc["user_id"], {"session_token": session_token})

    token = create_access_token({
        "sub":           user_doc["user_id"],
        "email":         user_doc["email"],
        "role":          user_doc["role"],
        "session_token": session_token,        # ← embedded in JWT
    })
    return TokenResponse(
        access_token = token,
        user_id      = user_doc["user_id"],
        email        = user_doc["email"],
        name         = user_doc.get("name", ""),
        role         = user_doc["role"],
        is_verified  = user_doc.get("is_verified", False),
        is_new_user  = is_new_user,
        phone        = user_doc.get("phone", user_doc.get("phoneNumber", "")),
        age          = user_doc.get("age"),
        gender       = user_doc.get("gender", ""),
        image_url    = user_doc.get("image_url", user_doc.get("imageUrl", "")),
        license_status   = user_doc.get("license_status"),
        license_number   = user_doc.get("license_number"),
        rejection_reason = user_doc.get("review_notes") or user_doc.get("rejection_reason"),
    )


# ── Register ───────────────────────────────────────────────────────────────────
# NO CHANGES in this function body — it calls build_token_response which now
# handles session_token automatically.

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    if body.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin accounts cannot be created through registration.",
        )

    # 1. Check duplicate email
    existing = get_user_by_email(body.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please log in.",
        )

    # 1b. Doctors: license number must be unique (names may legitimately repeat).
    if body.role == UserRole.DOCTOR:
        if not (body.license_number or "").strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A medical license number is required to register as a doctor.",
            )
        if get_doctor_by_license(body.license_number):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A doctor with this license number is already registered.",
            )

    # 2. Create Firebase Auth user
    try:
        fb_user = firebase_auth.create_user(
            email        = body.email,
            password     = body.password,
            display_name = body.name,
        )
    except firebase_auth.EmailAlreadyExistsError:
        # Firebase Auth has this email but Firestore may not (partial registration).
        # If there's no Firestore doc, complete it now so the user can log in.
        try:
            orphan = firebase_auth.get_user_by_email(body.email)
            if not get_user_by_email(body.email):
                fb_user = orphan   # repair: fall through to Firestore write below
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account with this email already exists. Please log in.",
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists. Please log in.",
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create account: {str(e)}",
        )

    # 3. Send email verification for patients (non-blocking — registration succeeds even if email fails)
    if body.role == UserRole.PATIENT:
        try:
            verify_link = firebase_auth.generate_email_verification_link(body.email)
            sent = send_patient_verification_email(body.email, body.name, verify_link)
            if not sent:
                print(f"[Register] Verification email failed to deliver to {body.email}")
        except Exception as e:
            print(f"[Register] Could not generate verification link for {body.email}: {e}")

    now = datetime.now(timezone.utc).isoformat()

    # 4. Build user document
    user_doc = {
        "user_id":        fb_user.uid,
        "email":          body.email,
        "name":           body.name,
        "role":           body.role.value,
        "phone":          body.phone or "",
        "age":            body.age,
        "gender":         body.gender or "",
        "is_verified":    False,
        "is_active":      True,
        "created_at":     now,
        "auth_method":    "email",
        "password_hash":  hash_password(body.password),
        "license_status": "pending" if body.role == UserRole.DOCTOR else "n/a",
    }
    set_user_doc(fb_user.uid, user_doc)

    # 5. Doctor-specific: create doctor doc + send emails
    if body.role == UserRole.DOCTOR:
        doctor_doc = {
            "user_id":          fb_user.uid,
            "email":            body.email,
            "name":             body.name,
            "phone":            body.phone or "",
            "license_number":   body.license_number or "",
            "license_number_norm": normalise_license(body.license_number or ""),
            "license_status":   "pending",
            "specialties":      body.specialties or [],
            "experience_years": body.experience or 0,
            "about":            body.description or "",
            "price":            0,
            "is_verified":      False,
            "created_at":       now,
        }
        set_doctor_doc(fb_user.uid, doctor_doc)

        sent1 = send_doctor_application_received(
            doctor_email   = body.email,
            doctor_name    = body.name,
            license_number = body.license_number or "Not provided",
        )
        sent2 = send_admin_new_doctor_application(
            doctor_name    = body.name,
            doctor_email   = body.email,
            license_number = body.license_number or "Not provided",
            specialties    = body.specialties or [],
            phone          = body.phone or "",
            gender         = body.gender or "",
            experience     = body.experience or 0,
            description    = body.description or "",
        )
        if not sent1:
            print(f"[Register] Doctor application email failed to deliver to {body.email}")
        if not sent2:
            print(f"[Register] Admin notification email failed to deliver to {ADMIN_EMAIL}")
        return build_token_response({**user_doc}, is_new_user=True)

    return build_token_response({**user_doc}, is_new_user=True)


# ── Login ──────────────────────────────────────────────────────────────────────
# CHANGE 4: add is_active check after password verification.
# Location: insert after step 4 (verify password), before step 5 (doctor gate).

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    # 1. Find user by email
    user_doc = get_user_by_email(body.email)
    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email. Please register first.",
        )

    # 2. Role must match registration (admins bypass — they log in from either portal)
    if user_doc.get("role") != "admin" and user_doc.get("role") != body.role.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This account is registered as a {user_doc.get('role')}. Please select the correct role.",
        )

    # 3. Google-auth users cannot use password login
    if user_doc.get("auth_method") == "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account uses Google Sign-In. Please use the Google button.",
        )

    # 4. Verify password — bcrypt first, then Firebase REST fallback for post-reset logins
    if not verify_password(body.password, user_doc.get("password_hash", "")):
        # bcrypt failed — user may have reset via Firebase link; check Firebase Auth
        firebase_ok = await _firebase_verify_password(body.email, body.password)
        if not firebase_ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect password.",
            )
        # Firebase accepted the new password — sync the hash back to Firestore
        set_user_doc(user_doc["user_id"], {"password_hash": hash_password(body.password)})

    # ── CHANGE 4: is_active check ──────────────────────────────────────────────
    # OLD CODE: nothing here — deactivated users could log in freely.
    # NEW CODE: block login if admin has deactivated this account.
    if not user_doc.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated. Please contact support.",
        )
    # ── END CHANGE 4 ───────────────────────────────────────────────────────────

    # 5. Doctor access gate (admin bypasses this)
    if body.role == UserRole.DOCTOR and user_doc.get("role") != "admin":
        license_status = user_doc.get("license_status", "pending")
        if license_status == "pending":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Your application is under review. "
                    "This process takes up to 2 business days. "
                    "You will receive an email once approved."
                ),
            )
        if license_status == "rejected":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Your application has been rejected. "
                    "Please contact support at support@opthdiseaseai.com for more information."
                ),
            )

    # 6. Sync email verification status for patients
    if body.role == UserRole.PATIENT:
        try:
            fb_user = firebase_auth.get_user(user_doc["user_id"])
            if fb_user.email_verified and not user_doc.get("is_verified"):
                set_user_doc(user_doc["user_id"], {"is_verified": True})
                user_doc["is_verified"] = True
        except Exception:
            pass

    return build_token_response(user_doc)


# ── Google Sign-In ─────────────────────────────────────────────────────────────
# CHANGE 5: add is_active check for existing Google users (same location pattern as login).

@router.post("/google", response_model=TokenResponse)
async def google_sign_in(body: GoogleSignInRequest):
    if body.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin accounts cannot use Google Sign-In.",
        )

    try:
        decoded = firebase_auth.verify_id_token(body.id_token)
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token expired.")
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token.")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Token verification failed: {str(e)}")

    uid   = decoded["uid"]
    email = decoded.get("email", "")
    name  = decoded.get("name", email.split("@")[0])

    user_doc = get_user_doc(uid)
    is_new   = user_doc is None

    if is_new:
        now = datetime.now(timezone.utc).isoformat()
        user_doc = {
            "user_id":        uid,
            "email":          email,
            "name":           name,
            "role":           body.role.value,
            "phone":          "",
            "is_verified":    True,
            "is_active":      True,            # ← CHANGE 5a: set is_active=True on first Google login
            "created_at":     now,
            "auth_method":    "google",
            "image_url":      decoded.get("picture", ""),
            "license_status": "pending" if body.role.value == "doctor" else "n/a",
        }
        set_user_doc(uid, user_doc)

        if body.role.value == "doctor":
            set_doctor_doc(uid, {
                "user_id":        uid,
                "email":          email,
                "name":           name,
                "license_status": "pending",
                "specialties":    [],
                "is_verified":    False,
                "created_at":     now,
            })
            send_doctor_application_received(email, name, "Not provided — please update in profile")
            send_admin_new_doctor_application(name, email, "Not provided", [], "")
    else:
        if user_doc.get("role") != body.role.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This Google account is registered as a {user_doc.get('role')}.",
            )

        # ── CHANGE 5b: is_active check for existing Google users ──────────────
        # OLD CODE: nothing — deactivated Google users could sign in freely.
        # NEW CODE: block if deactivated.
        if not user_doc.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has been deactivated. Please contact support.",
            )
        # ── END CHANGE 5b ─────────────────────────────────────────────────────

        if body.role.value == "doctor":
            license_status = user_doc.get("license_status", "pending")
            if license_status == "pending":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Your application is under review. You will receive an email once approved.",
                )
            if license_status == "rejected":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Your application has been rejected. Please contact support.",
                )

    return build_token_response({**user_doc, "user_id": uid}, is_new_user=is_new)


# ── CHANGE 6: NEW logout endpoint ─────────────────────────────────────────────
# OLD CODE: no logout endpoint existed on the backend at all.
# NEW CODE: clears session_token from Firestore so all existing JWTs for this
# user become invalid immediately. The frontend also calls clearAuth() as before.
#
# INSERT THIS ENTIRE FUNCTION — it does not replace anything.

@router.post("/logout", response_model=MessageResponse)
async def logout(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id:
        # Wipe the session token — any JWT still holding the old session_token
        # will fail validation on their next request to any protected endpoint.
        set_user_doc(user_id, {"session_token": ""})
    return MessageResponse(message="Logged out successfully.")

# ── END CHANGE 6 ───────────────────────────────────────────────────────────────


# ── Resend verification email ──────────────────────────────────────────────────
# NO CHANGES

@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(email: str):
    user_doc = get_user_by_email(email)
    if not user_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user_doc.get("is_verified"):
        return MessageResponse(message="Email is already verified.")
    try:
        verify_link = firebase_auth.generate_email_verification_link(email)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not generate verification link. Please try again.")
    sent = send_patient_verification_email(email, user_doc.get("name", ""), verify_link)
    if not sent:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not send verification email — mail delivery failed. Please try again later.")
    return MessageResponse(message="Verification email sent. Please check your inbox.")


# ── Check email verification status ───────────────────────────────────────────
# NO CHANGES

@router.get("/verify-status", response_model=MessageResponse)
async def check_verification(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    payload = decode_access_token(credentials.credentials)
    user_id = payload["sub"]
    try:
        fb_user = firebase_auth.get_user(user_id)
        if fb_user.email_verified:
            set_user_doc(user_id, {"is_verified": True})
            return MessageResponse(message="Email verified successfully.")
        return MessageResponse(message="Email not yet verified. Please check your inbox.")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# ── Forgot password ────────────────────────────────────────────────────────────

@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(body: ForgotPasswordRequest):
    user_doc = get_user_by_email(body.email)

    # Always return the same message — don't reveal whether the email exists
    if not user_doc:
        return MessageResponse(
            message="If this email is registered, a password reset link has been sent."
        )

    if user_doc.get("auth_method") == "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account uses Google Sign-In. Please sign in with Google — there is no password to reset.",
        )

    try:
        reset_link = firebase_auth.generate_password_reset_link(body.email)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not generate reset link. Please try again later.",
        )

    sent = send_password_reset_email(
        to_email   = body.email,
        name       = user_doc.get("name", ""),
        reset_link = reset_link,
    )
    if not sent:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send reset email — mail delivery failed. Please try again later.",
        )

    return MessageResponse(
        message="Password reset link has been sent to your email. Please check your inbox."
    )


# ── Debug: test email delivery (remove before production) ────────────────────

@router.get("/test-email")
async def test_email(to: str = ""):
    import httpx as _httpx, smtplib as _smtp, os as _os
    recipient    = to or GMAIL_USER or ADMIN_EMAIL
    gmail_user   = _os.getenv("GMAIL_USER", "")
    gmail_pass   = _os.getenv("GMAIL_APP_PASSWORD", "")
    brevo_key    = _os.getenv("BREVO_API_KEY", "")
    resend_key   = _os.getenv("RESEND_API_KEY", "")

    # SMTP
    smtp_err = None
    try:
        with _smtp.SMTP("smtp.gmail.com", 587, timeout=5) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(gmail_user, gmail_pass)
            s.sendmail(gmail_user, recipient, "Subject: test\n\ntest")
        smtp_ok = True
    except Exception as e:
        smtp_ok = False; smtp_err = str(e)

    # Brevo
    brevo_err = None
    try:
        r = _httpx.post("https://api.brevo.com/v3/smtp/email",
            headers={"api-key": brevo_key, "Content-Type": "application/json"},
            json={"sender": {"name": "OpthdiseaseAI", "email": gmail_user or "noreply@opthdiseaseai.com"},
                  "to": [{"email": recipient}], "subject": "Brevo test", "htmlContent": "<p>test</p>"},
            timeout=15)
        brevo_ok = r.status_code in (200, 201)
        if not brevo_ok: brevo_err = f"{r.status_code}: {r.text}"
    except Exception as e:
        brevo_ok = False; brevo_err = str(e)

    # Resend
    resend_err = None
    try:
        r = _httpx.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={"from": "OpthdiseaseAI <onboarding@resend.dev>", "to": [recipient],
                  "subject": "Resend test", "html": "<p>test</p>"},
            timeout=15)
        resend_ok = r.status_code in (200, 201)
        if not resend_ok: resend_err = f"{r.status_code}: {r.text}"
    except Exception as e:
        resend_ok = False; resend_err = str(e)

    return {
        "sent_to":    recipient,
        "smtp":       {"ok": smtp_ok,   "error": smtp_err},
        "brevo":      {"ok": brevo_ok,  "error": brevo_err,  "key_set": bool(brevo_key)},
        "resend":     {"ok": resend_ok, "error": resend_err, "key_set": bool(resend_key)},
    }


# ── Get current user ───────────────────────────────────────────────────────────
# NO CHANGES

@router.get("/me", response_model=dict)
async def get_me(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    payload  = decode_access_token(credentials.credentials)
    user_id  = payload["sub"]
    user_doc = get_user_doc(user_id)
    if not user_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    user_doc.pop("password_hash", None)
    return user_doc