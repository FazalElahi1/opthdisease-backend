from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from enum import Enum


class UserRole(str, Enum):
    PATIENT = "patient"
    DOCTOR  = "doctor"
    ADMIN   = "admin"


# ── Auth schemas ───────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:          EmailStr
    password:       str
    name:           str
    role:           UserRole
    phone:          Optional[str] = None
    age:            Optional[int] = None          # patients only
    gender:         Optional[str] = None          # patients only
    license_number: Optional[str] = None          # doctors only
    specialties:    Optional[List[str]] = None    # doctors only
    experience:     Optional[int] = None          # doctors only — years of experience
    description:    Optional[str] = None          # doctors only — short bio / about

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 7:
            raise ValueError("Password must be at least 7 characters.")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name cannot be empty.")
        return v.strip()


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str
    role:     UserRole

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 7:
            raise ValueError("Password must be at least 7 characters.")
        return v


class GoogleSignInRequest(BaseModel):
    id_token: str          # Firebase ID token from the React Native Google Sign-In
    role:     UserRole


class TokenResponse(BaseModel):
    access_token:  str
    token_type:    str = "bearer"
    user_id:       str
    email:         str
    name:          str
    role:          UserRole
    is_verified:   bool
    is_new_user:   bool = False
    # Full profile fields — always fresh from Firestore
    phone:          Optional[str] = ""
    age:            Optional[int] = None
    gender:         Optional[str] = ""
    image_url:      Optional[str] = ""
    # Doctor-specific — needed so the app can route to the correct screen
    license_status: Optional[str] = None
    license_number: Optional[str] = None
    rejection_reason: Optional[str] = None


class MessageResponse(BaseModel):
    message: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


# ── User / Profile schemas ─────────────────────────────────────────────────────

class UserProfile(BaseModel):
    user_id:       str
    email:         str
    name:          str
    role:          UserRole
    phone:         Optional[str] = None
    age:           Optional[int] = None
    gender:        Optional[str] = None
    is_verified:   bool
    image_url:     Optional[str] = None
    created_at:    Optional[str] = None


class DoctorProfile(BaseModel):
    user_id:            str
    email:              str
    name:               str
    phone:              Optional[str] = None
    specialties:        Optional[List[str]] = []
    experience_years:   Optional[int] = None
    about:              Optional[str] = None
    price:              Optional[int] = None
    license_number:     Optional[str] = None
    is_verified:        bool
    image_url:          Optional[str] = None
    license_status:     Optional[str] = "pending"
    availabilitySlots:  Optional[List[dict]] = []
    avg_rating:         Optional[float] = None
    review_count:       Optional[int] = 0


class UpdateProfileRequest(BaseModel):
    name:   Optional[str] = None
    phone:  Optional[str] = None
    age:    Optional[int] = None
    gender: Optional[str] = None


class UpdateDoctorProfileRequest(BaseModel):
    name:               Optional[str] = None
    phone:              Optional[str] = None
    gender:             Optional[str] = None
    specialties:        Optional[List[str]] = None
    experience_years:   Optional[int] = None
    about:              Optional[str] = None
    price:              Optional[int] = None
    image_url:          Optional[str] = None
    availabilitySlots:  Optional[List[dict]] = None
    license_number:     Optional[str] = None