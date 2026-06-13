"""
services/agora.py
─────────────────
Generates Agora RTC tokens for video calls using Agora's official token
builder (`agora-token-builder`), which produces spec-compliant RTC tokens
that the react-native-agora SDK accepts.

NOTE: a previous hand-rolled "007" builder produced non-spec tokens (custom
zlib/CRC framing, no spec AccessToken2 structure) that the Agora SDK rejects
with an invalid-token error — the call never connects. This uses the official
builder instead.
"""

import os
import time
from enum import IntEnum

from dotenv import load_dotenv
from agora_token_builder import RtcTokenBuilder

load_dotenv()

AGORA_APP_ID          = os.getenv("AGORA_APP_ID")
AGORA_APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE")
TOKEN_EXPIRY_SECONDS  = int(os.getenv("TOKEN_EXPIRY_SECONDS", "3600"))


class Role(IntEnum):
    PUBLISHER  = 1   # Role_Attendee / host — can publish audio+video (doctor & patient)
    SUBSCRIBER = 2


def generate_call_token(channel_name: str, uid: int = 0) -> dict:
    """
    Generate an Agora RTC token for a video call.
    Returns token + channel + uid + app_id + expiry.

    Both the patient and the doctor call this with the SAME channel_name
    (= appointment_id) and DIFFERENT uids, so they join the same call.
    """
    if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE:
        raise ValueError("AGORA_APP_ID and AGORA_APP_CERTIFICATE must be set in .env")

    expire_ts = int(time.time()) + TOKEN_EXPIRY_SECONDS

    token = RtcTokenBuilder.buildTokenWithUid(
        AGORA_APP_ID,
        AGORA_APP_CERTIFICATE,
        channel_name,
        uid,
        Role.PUBLISHER,
        expire_ts,
    )

    return {
        "token":        token,
        "channel_name": channel_name,
        "uid":          uid,
        "app_id":       AGORA_APP_ID,
        "expires_at":   expire_ts,
    }
