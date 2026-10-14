from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Account, Athlete, MediaAsset, Role
from app.schemas import (
    AccountResponse,
    AthleteResponse,
    LoginRequest,
    MediaResponse,
    PublicRole,
    RegisterRequest,
    SportsAnswer,
    SportsQuestion,
    TokenResponse,
    VerificationUpdate,
)
from app.security import create_access_token, get_current_account, hash_password, require_roles, verify_password
from app.services import answer_sports_question
from app.storage import storage
from app.config import settings

router = APIRouter(prefix="/api/v1")
DbSession = Annotated[Session, Depends(get_db)]
VerifiedAccount = Annotated[Account, Depends(get_current_account)]


@router.post("/auth/register", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DbSession):
    role = payload.role.value
    account = Account(
        full_name=payload.full_name.strip(),
        email=str(payload.email).lower(),
        mobile=payload.mobile,
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=role,
        organization=payload.organization,
        owner_name=payload.owner_name,
        district=payload.district,
        state=payload.state,
        sport=payload.sport,
        sports=payload.sports,
        location=payload.location,
        experience_years=payload.experience_years,
        qualification=payload.qualification,
        is_verified=role == Role.PLAYER.value,
    )
    if role == Role.PLAYER.value:
        account.athlete = Athlete(date_of_birth=payload.date_of_birth, gender=payload.gender)
    try:
        db.add(account)
        db.commit()
        db.refresh(account)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email, mobile, or username is already registered.") from None
    return account


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: DbSession):
    identifier = payload.identifier.strip()
    account = db.scalar(
        select(Account).where(
            or_(Account.email == identifier.lower(), Account.mobile == identifier, Account.username == identifier)
        )
    )
    if account is None or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=401, detail="Invalid sign-in details.", headers={"WWW-Authenticate": "Bearer"})
    if not account.is_active:
        raise HTTPException(status_code=403, detail="This account is disabled.")
    if not account.is_verified:
        raise HTTPException(status_code=403, detail="Your account is awaiting verification.")
    token, expires_in = create_access_token(account)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/auth/me", response_model=AccountResponse)
def current_account(account: VerifiedAccount):
    return account


@router.post("/media", response_model=MediaResponse, status_code=status.HTTP_201_CREATED)
def upload_media(
    account: VerifiedAccount,
    db: DbSession,
    upload: UploadFile = File(...),
):
    allowed_types = {"image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm"}
    content_type = upload.content_type or "application/octet-stream"
    if content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="Upload a JPG, PNG, WEBP, MP4, or WEBM file.")
    upload.file.seek(0)
    signature = upload.file.read(12)
    upload.file.seek(0)
    signatures = {
        "image/jpeg": signature.startswith(b"\xff\xd8\xff"),
        "image/png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": signature.startswith(b"RIFF") and signature[8:12] == b"WEBP",
        "video/mp4": len(signature) >= 8 and signature[4:8] == b"ftyp",
        "video/webm": signature.startswith(b"\x1a\x45\xdf\xa3"),
    }
    if not signatures[content_type]:
        raise HTTPException(status_code=415, detail="The uploaded file content does not match its media type.")
    filename = (upload.filename or "upload").replace("\\", "/").split("/")[-1][:255]
    try:
        storage_key, size_bytes = storage.save(upload.file, filename, settings.max_upload_bytes)
    except ValueError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    asset = MediaAsset(
        owner_id=account.id,
        storage_key=storage_key,
        original_filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    try:
        db.add(asset)
        db.commit()
        db.refresh(asset)
    except Exception:
        db.rollback()
        storage.delete(storage_key)
        raise
    return asset


@router.get("/media/{asset_id}")
def download_media(asset_id: int, account: VerifiedAccount, db: DbSession):
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="File not found.")
    privileged_roles = {Role.SELECTOR.value, Role.COACH.value, Role.ACADEMY.value, Role.ADMIN.value}
    if asset.owner_id != account.id and account.role not in privileged_roles:
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        source = storage.open(asset.storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.") from None

    def stream_file():
        with source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        stream_file(),
        media_type=asset.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(asset.original_filename)}"},
    )


@router.post("/ai/question", response_model=SportsAnswer)
def ask_sports_question(_: VerifiedAccount, payload: SportsQuestion):
    try:
        return SportsAnswer(answer=answer_sports_question(payload.question))
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception:
        raise HTTPException(status_code=503, detail="Sports guidance is temporarily unavailable.") from None


@router.get("/players", response_model=list[AthleteResponse])
def search_players(
    db: DbSession,
    _: Annotated[Account, Depends(require_roles(Role.SELECTOR, Role.COACH, Role.ACADEMY, Role.ADMIN))],
    query: str = Query(default="", max_length=100),
    district: str | None = Query(default=None, max_length=100),
    sport: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
):
    statement = (
        select(Account)
        .options(joinedload(Account.athlete))
        .where(Account.role == Role.PLAYER.value, Account.is_verified.is_(True))
    )
    if query:
        term = f"%{query.strip()}%"
        statement = statement.where(Account.full_name.ilike(term))
    if district:
        statement = statement.where(Account.district.ilike(district.strip()))
    if sport:
        statement = statement.where(Account.sport.ilike(sport.strip()))
    accounts = db.scalars(statement.order_by(Account.id.desc()).limit(limit)).unique().all()
    return [
        AthleteResponse(
            id=account.id,
            full_name=account.full_name,
            sport=account.sport,
            district=account.district,
            state=account.state,
            gender=account.athlete.gender if account.athlete else None,
        )
        for account in accounts
    ]


@router.patch("/admin/accounts/{account_id}/verification", response_model=AccountResponse)
def update_verification(
    account_id: int,
    payload: VerificationUpdate,
    db: DbSession,
    _: Annotated[Account, Depends(require_roles(Role.ADMIN))],
):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    account.is_verified = payload.is_verified
    db.commit()
    db.refresh(account)
    return account