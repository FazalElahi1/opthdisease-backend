import logging
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import messaging

from services.firebase import get_user_doc, set_user_doc
from services.jwt import decode_access_token

logger = logging.getLogger(__name__)

bearer = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
) -> dict:
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")

    user_doc = get_user_doc(user_id)
    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    jwt_session    = payload.get("session_token", "")
    stored_session = user_doc.get("session_token", "")
    if not jwt_session or jwt_session != stored_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
        )

    if not user_doc.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated. Please contact support.",
        )

    return {**user_doc, "user_id": user_id}


async def send_push_notification(
    user_id: str,
    title:   str,
    body:    str,
    data:    dict = None,
) -> bool:
    user_doc = get_user_doc(user_id)
    if not user_doc:
        return False

    fcm_token = user_doc.get("fcm_token")
    if not fcm_token:
        logger.info("[FCM] No FCM token for user %s — skipping notification", user_id)
        return False

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data={k: str(v) for k, v in (data or {}).items()},
        token=fcm_token,
        android=messaging.AndroidConfig(priority="high"),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(sound="default"),
            )
        ),
    )

    try:
        messaging.send(message)
        return True
    except messaging.UnregisteredError:
        logger.warning("[FCM] Token unregistered for user %s — clearing token", user_id)
        set_user_doc(user_id, {"fcm_token": ""})
        return False
    except Exception as e:
        logger.error("[FCM] Failed to send notification to user %s: %s", user_id, e)
        return False


def save_fcm_token(user_id: str, token: str) -> None:
    set_user_doc(user_id, {"fcm_token": token})
