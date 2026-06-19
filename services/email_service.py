import os
import smtplib
import httpx
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL", "")
APP_NAME       = "OpthdiseaseAI"


def _send_smtp(to: str, subject: str, html: str) -> bool:
    """Try Gmail SMTP on port 587 (STARTTLS). Works on most Render instances."""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{APP_NAME} <{GMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, to, msg.as_string())
        print(f"[Email/SMTP] Sent '{subject}' to {to}")
        return True
    except Exception as e:
        print(f"[Email/SMTP] Failed: {e}")
        return False


def _send_resend(to: str, subject: str, html: str) -> bool:
    """Fallback: Resend HTTP API (always works regardless of Render instance)."""
    if not RESEND_API_KEY:
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": f"{APP_NAME} <onboarding@resend.dev>", "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        if r.status_code in (200, 201):
            print(f"[Email/Resend] Sent '{subject}' to {to}")
            return True
        print(f"[Email/Resend] Error {r.status_code}: {r.text}")
        return False
    except Exception as e:
        print(f"[Email/Resend] Failed: {e}")
        return False


def _send(to: str, subject: str, html: str) -> bool:
    """Send email: try SMTP first, fall back to Resend if SMTP is blocked."""
    if _send_smtp(to, subject, html):
        return True
    print(f"[Email] SMTP failed, trying Resend fallback for {to}")
    return _send_resend(to, subject, html)


# ── Doctor registration emails ─────────────────────────────────────────────────

def send_doctor_application_received(doctor_email: str, doctor_name: str, license_number: str) -> bool:
    """Email sent to doctor immediately after registration."""
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #1E40AF; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">{APP_NAME}</h1>
        <p style="color: #BFDBFE; margin: 4px 0 0;">Clinician Portal</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <h2 style="color: #0F172A;">Application Received</h2>
        <p style="color: #475569;">Dear Dr. {doctor_name},</p>
        <p style="color: #475569; line-height: 1.6;">
          Thank you for registering with {APP_NAME}. Your application has been received and is currently under review by our admin team.
        </p>
        <div style="background: #FEF3C7; border-left: 4px solid #F59E0B; padding: 16px; border-radius: 8px; margin: 20px 0;">
          <p style="color: #92400E; margin: 0; font-weight: bold;">⏳ Review Timeline: Up to 2 business days</p>
        </div>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
          <tr>
            <td style="padding: 10px; background: #F8FAFC; border: 1px solid #E2E8F0; font-weight: bold; color: #475569;">Name</td>
            <td style="padding: 10px; border: 1px solid #E2E8F0; color: #0F172A;">Dr. {doctor_name}</td>
          </tr>
          <tr>
            <td style="padding: 10px; background: #F8FAFC; border: 1px solid #E2E8F0; font-weight: bold; color: #475569;">License ID</td>
            <td style="padding: 10px; border: 1px solid #E2E8F0; color: #0F172A;">{license_number}</td>
          </tr>
          <tr>
            <td style="padding: 10px; background: #F8FAFC; border: 1px solid #E2E8F0; font-weight: bold; color: #475569;">Status</td>
            <td style="padding: 10px; border: 1px solid #E2E8F0; color: #D97706; font-weight: bold;">Pending Review</td>
          </tr>
        </table>
        <p style="color: #475569; line-height: 1.6;">
          You will receive another email once your application has been reviewed. Please do not attempt to log in until you receive confirmation of approval.
        </p>
        <p style="color: #94A3B8; font-size: 13px; margin-top: 32px;">
          If you have any questions, please contact us at {GMAIL_USER}
        </p>
      </div>
      <div style="background: #F8FAFC; padding: 16px; border-radius: 0 0 12px 12px; text-align: center;">
        <p style="color: #94A3B8; font-size: 12px; margin: 0;">&copy; {APP_NAME}. All rights reserved.</p>
      </div>
    </div>
    """
    return _send(doctor_email, f"{APP_NAME} — Application Received", html)


def send_admin_new_doctor_application(
    doctor_name:    str,
    doctor_email:   str,
    license_number: str,
    specialties:    list,
    phone:          str,
    gender:         str = "",
    experience:     int = 0,
    description:    str = "",
    admin_review_url: str = "",
) -> bool:
    """Email sent to admin when a new doctor registers."""
    spec_str = ", ".join(specialties) if specialties else "Not specified"
    gender_str = (gender or "Not specified").capitalize()
    exp_str    = f"{experience} year(s)" if experience else "Not specified"
    desc_str   = description.strip() or "Not provided"
    review_btn = f"""
      <a href="{admin_review_url}" style="
        display: inline-block; background: #1E40AF; color: white;
        padding: 12px 24px; border-radius: 8px; text-decoration: none;
        font-weight: bold; margin-top: 16px;">
        Review Application in Admin Panel
      </a>
    """ if admin_review_url else ""

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #DC2626; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">⚕️ New Doctor Application</h1>
        <p style="color: #FCA5A5; margin: 4px 0 0;">Action Required — {APP_NAME} Admin</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <p style="color: #475569;">A new doctor has registered and requires license verification:</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569;width:40%">Name</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{doctor_name}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">Email</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{doctor_email}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">License ID</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A;font-weight:bold">{license_number}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">Specialties</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{spec_str}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">Phone</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{phone}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">Gender</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{gender_str}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">Experience</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{exp_str}</td></tr>
          <tr><td style="padding:10px;background:#F8FAFC;border:1px solid #E2E8F0;font-weight:bold;color:#475569">Description</td>
              <td style="padding:10px;border:1px solid #E2E8F0;color:#0F172A">{desc_str}</td></tr>
        </table>
        {review_btn}
        <p style="color: #64748B; font-size: 13px; margin-top: 24px;">
          Please verify the license number with the Pakistan Medical Commission (PMC) before approving.
        </p>
      </div>
    </div>
    """
    return _send(ADMIN_EMAIL, f"[ACTION REQUIRED] New Doctor Application — {doctor_name}", html)


def send_doctor_approved(doctor_email: str, doctor_name: str) -> bool:
    """Email sent to doctor when admin approves them."""
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #059669; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">✅ Application Approved!</h1>
        <p style="color: #A7F3D0; margin: 4px 0 0;">{APP_NAME} — Clinician Portal</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <h2 style="color: #059669;">Congratulations, Dr. {doctor_name}!</h2>
        <p style="color: #475569; line-height: 1.6;">
          Your medical license has been verified and your account has been <strong>approved</strong>.
          You can now log in to the {APP_NAME} Clinician Portal and start seeing patients.
        </p>
        <div style="background: #ECFDF5; border: 1px solid #A7F3D0; padding: 20px; border-radius: 12px; margin: 24px 0; text-align: center;">
          <p style="color: #065F46; font-size: 18px; font-weight: bold; margin: 0;">You are now an active clinician on {APP_NAME}</p>
        </div>
        <p style="color: #475569; line-height: 1.6;">You can now:</p>
        <ul style="color: #475569; line-height: 2;">
          <li>Set your availability and consultation fees</li>
          <li>Receive patient bookings and video consultations</li>
          <li>Review AI eye scan results</li>
          <li>Withdraw your earnings via Safepay</li>
        </ul>
      </div>
    </div>
    """
    return _send(doctor_email, f"✅ {APP_NAME} — Your Application Has Been Approved!", html)


def send_doctor_rejected(doctor_email: str, doctor_name: str, reason: str = "") -> bool:
    """Email sent to doctor when admin rejects them."""
    reason_block = f"""
      <div style="background: #FEF2F2; border-left: 4px solid #EF4444; padding: 16px; border-radius: 8px; margin: 20px 0;">
        <p style="color: #991B1B; margin: 0; font-weight: bold;">Reason:</p>
        <p style="color: #DC2626; margin: 6px 0 0;">{reason}</p>
      </div>
    """ if reason else ""

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #DC2626; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">Application Status Update</h1>
        <p style="color: #FCA5A5; margin: 4px 0 0;">{APP_NAME} — Clinician Portal</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <h2 style="color: #DC2626;">Dear Dr. {doctor_name},</h2>
        <p style="color: #475569; line-height: 1.6;">
          After reviewing your application, we were unable to verify your medical credentials at this time.
        </p>
        {reason_block}
        <p style="color: #475569; line-height: 1.6;">
          If you believe this is an error or wish to reapply with updated credentials, please contact us at {GMAIL_USER}.
        </p>
      </div>
    </div>
    """
    return _send(doctor_email, f"{APP_NAME} — Application Status Update", html)


def send_patient_verification_email(to_email: str, name: str, verify_link: str) -> bool:
    """Email sent to patient after registration with an email verification link."""
    display_name = name or to_email.split("@")[0]
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #1E40AF; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">{APP_NAME}</h1>
        <p style="color: #BFDBFE; margin: 4px 0 0;">Patient Portal — Verify Your Email</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <h2 style="color: #0F172A;">Welcome, {display_name}!</h2>
        <p style="color: #475569; line-height: 1.6;">
          Thank you for creating your {APP_NAME} account. Please verify your email address by clicking the button below.
        </p>
        <div style="text-align: center; margin: 32px 0;">
          <a href="{verify_link}" style="
            display: inline-block; background: #1E40AF; color: white;
            padding: 14px 32px; border-radius: 8px; text-decoration: none;
            font-weight: bold; font-size: 16px;">
            Verify Email Address
          </a>
        </div>
        <div style="background: #FEF3C7; border-left: 4px solid #F59E0B; padding: 16px; border-radius: 8px; margin: 20px 0;">
          <p style="color: #92400E; margin: 0; font-size: 13px;">
            This link expires in 24 hours. After verifying, return to the app and log in.
          </p>
        </div>
        <p style="color: #94A3B8; font-size: 13px; margin-top: 32px;">
          If the button does not work, copy and paste this link into your browser:<br/>
          <a href="{verify_link}" style="color: #1E40AF; word-break: break-all;">{verify_link}</a>
        </p>
      </div>
      <div style="background: #F8FAFC; padding: 16px; border-radius: 0 0 12px 12px; text-align: center;">
        <p style="color: #94A3B8; font-size: 12px; margin: 0;">&copy; {APP_NAME}. All rights reserved.</p>
      </div>
    </div>
    """
    return _send(to_email, f"{APP_NAME} — Verify Your Email Address", html)


def send_password_reset_email(to_email: str, name: str, reset_link: str) -> bool:
    """Email sent to any user (patient, doctor, or admin) who requests a password reset."""
    display_name = name or to_email.split("@")[0]
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #1E40AF; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0; font-size: 22px;">{APP_NAME}</h1>
        <p style="color: #BFDBFE; margin: 4px 0 0;">Password Reset Request</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <h2 style="color: #0F172A;">Reset Your Password</h2>
        <p style="color: #475569;">Hi {display_name},</p>
        <p style="color: #475569; line-height: 1.6;">
          We received a request to reset the password for your {APP_NAME} account.
          Click the button below to set a new password.
        </p>
        <div style="text-align: center; margin: 32px 0;">
          <a href="{reset_link}" style="
            display: inline-block; background: #1E40AF; color: white;
            padding: 14px 32px; border-radius: 8px; text-decoration: none;
            font-weight: bold; font-size: 16px;">
            Reset Password
          </a>
        </div>
        <div style="background: #FEF3C7; border-left: 4px solid #F59E0B; padding: 16px; border-radius: 8px; margin: 20px 0;">
          <p style="color: #92400E; margin: 0; font-size: 13px;">
            This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email — your password will not change.
          </p>
        </div>
        <p style="color: #94A3B8; font-size: 13px; margin-top: 32px;">
          If the button does not work, copy and paste this link into your browser:<br/>
          <a href="{reset_link}" style="color: #1E40AF; word-break: break-all;">{reset_link}</a>
        </p>
      </div>
      <div style="background: #F8FAFC; padding: 16px; border-radius: 0 0 12px 12px; text-align: center;">
        <p style="color: #94A3B8; font-size: 12px; margin: 0;">&copy; {APP_NAME}. All rights reserved.</p>
      </div>
    </div>
    """
    return _send(to_email, f"{APP_NAME} — Password Reset Request", html)


def send_payout_confirmation(doctor_email: str, doctor_name: str, amount_pkr: int, transfer_id: str) -> bool:
    """Email sent to doctor when a payout is processed."""
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #059669; padding: 24px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0;">💰 Payout Processed</h1>
        <p style="color: #A7F3D0; margin: 4px 0 0;">{APP_NAME}</p>
      </div>
      <div style="background: #FFFFFF; padding: 32px; border: 1px solid #E2E8F0;">
        <p style="color: #475569;">Dear Dr. {doctor_name},</p>
        <p style="color: #475569;">Your withdrawal has been processed successfully.</p>
        <div style="background: #ECFDF5; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0;">
          <p style="color: #64748B; margin: 0;">Amount Transferred</p>
          <p style="color: #059669; font-size: 36px; font-weight: bold; margin: 8px 0;">Rs {amount_pkr:,}</p>
          <p style="color: #94A3B8; font-size: 12px; margin: 0;">Transfer ID: {transfer_id}</p>
        </div>
        <p style="color: #475569; font-size: 13px;">Funds typically arrive in your bank account within 2–5 business days depending on your bank.</p>
      </div>
    </div>
    """
    return _send(doctor_email, f"{APP_NAME} — Payout of Rs {amount_pkr:,} Processed", html)