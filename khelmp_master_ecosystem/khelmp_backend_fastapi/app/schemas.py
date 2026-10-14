from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class PublicRole(StrEnum):
    PLAYER = "PLAYER"
    SELECTOR = "SELECTOR"
    COACH = "COACH"
    ACADEMY = "ACADEMY"
    ORGANIZER = "ORGANIZER"


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    mobile: str = Field(min_length=10, max_length=20)
    username: str | None = Field(default=None, min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=128)
    confirm_password: str
    role: PublicRole
    date_of_birth: date | None = None
    gender: str | None = Field(default=None, max_length=40)
    state: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    organization: str | None = Field(default=None, max_length=180)
    experience_years: int | None = Field(default=None, ge=0, le=80)
    qualification: str | None = Field(default=None, max_length=180)
    sport: str | None = Field(default=None, max_length=100)
    sports: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=180)
    owner_name: str | None = Field(default=None, max_length=160)

    @field_validator("full_name", "gender", "state", "district", "organization", "qualification", "sport", "sports", "location", "owner_name", mode="before")
    @classmethod
    def normalize_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        normalized = value.strip().replace(" ", "").replace("-", "")
        if not normalized.removeprefix("+").isdigit():
            raise ValueError("Enter a valid mobile number.")
        return normalized

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        if not (any(char.islower() for char in value) and any(char.isupper() for char in value) and any(char.isdigit() for char in value)):
            raise ValueError("Password needs an uppercase letter, a lowercase letter, and a number.")
        return value

    @model_validator(mode="after")
    def validate_role_fields(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        required = {
            PublicRole.PLAYER: ("date_of_birth", "gender", "state", "district", "sport"),
            PublicRole.SELECTOR: ("organization", "experience_years", "state", "district"),
            PublicRole.COACH: ("qualification", "experience_years", "sport", "district"),
            PublicRole.ACADEMY: ("organization", "owner_name", "sports", "location", "district"),
            PublicRole.ORGANIZER: ("organization", "district"),
        }[self.role]
        missing = [field for field in required if getattr(self, field) in (None, "")]
        if missing:
            raise ValueError(f"Required for {self.role.value.lower()}: {', '.join(missing)}.")
        if self.date_of_birth and self.date_of_birth >= date.today():
            raise ValueError("Date of birth must be in the past.")
        return self


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AccountResponse(BaseModel):
    id: int
    full_name: str
    email: EmailStr
    mobile: str
    username: str | None
    role: str
    is_verified: bool

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AthleteResponse(BaseModel):
    id: int
    full_name: str
    sport: str | None
    district: str | None
    state: str | None
    gender: str | None


class VerificationUpdate(BaseModel):
    is_verified: bool


class MediaResponse(BaseModel):
    id: int
    original_filename: str
    content_type: str
    size_bytes: int


class SportsQuestion(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class SportsAnswer(BaseModel):
    answer: str