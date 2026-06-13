from pydantic import BaseModel, field_validator
from typing import Optional, List
from enum import Enum
from datetime import datetime


class AppointmentStatus(str, Enum):
    PENDING   = "pending"      # payment not yet confirmed
    CONFIRMED = "confirmed"    # paid and confirmed
    ACTIVE    = "active"       # call is currently happening
    COMPLETED = "completed"    # call ended
    CANCELLED = "cancelled"    # cancelled before call
    MISSED    = "missed"       # patient didn't join in time


class SlotStatus(str, Enum):
    AVAILABLE = "available"
    BOOKED    = "booked"
    BLOCKED   = "blocked"      # doctor blocked this slot


# ── Doctor slot schemas ────────────────────────────────────────────────────────

class AvailabilitySlot(BaseModel):
    """A time window the doctor is available (e.g. Mon 9AM–11AM)."""
    day:              str          # "Monday", "Tuesday" etc.
    start_time:       str          # "09:00"
    end_time:         str          # "11:00"
    slot_duration_min: int = 20    # each bookable slot length in minutes
    price_pkr:        int          # consultation fee for this slot


class DoctorSlotsResponse(BaseModel):
    doctor_id:   str
    doctor_name: str
    slots:       List[dict]        # list of bookable sub-slots with status


# ── Appointment schemas ────────────────────────────────────────────────────────

class BookAppointmentRequest(BaseModel):
    doctor_id:       str
    slot_date:       str        # "2024-12-25"
    slot_start_time: str        # "09:00"
    slot_end_time:   str        # "09:20"
    safepay_token:   str        # Safepay payment token, confirmed before booking

    @field_validator("slot_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("slot_date must be in YYYY-MM-DD format.")
        return v


class AppointmentResponse(BaseModel):
    appointment_id:  str
    doctor_id:       str
    doctor_name:     str
    patient_id:      str
    patient_name:    str
    slot_date:       str
    slot_start_time: str
    slot_end_time:   str
    status:          AppointmentStatus
    price_pkr:       int
    safepay_token:   Optional[str] = ""
    created_at:      str
    channel_name:    str        # Agora channel name = appointment_id
    can_join:        bool = False  # True only during the appointment window
    completed_at:    Optional[str] = None  # set when the call ends; gates the rating window
    has_review:      bool = False
    call_duration_seconds:  Optional[int] = None
    flagged_short_call:     bool = False


class AppointmentListResponse(BaseModel):
    appointments: List[AppointmentResponse]


# ── Call schemas ───────────────────────────────────────────────────────────────

class JoinCallRequest(BaseModel):
    appointment_id: str


class CallTokenResponse(BaseModel):
    token:        str
    channel_name: str
    uid:          int
    app_id:       str
    expires_at:   int
    appointment_id: str


class EndCallRequest(BaseModel):
    appointment_id: str


# ── FCM token ─────────────────────────────────────────────────────────────────

class SaveFCMTokenRequest(BaseModel):
    fcm_token: str