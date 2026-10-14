from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Account, Role

password_hasher = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(account: Account) -> tuple[str, int]:
    if not settings.signing_secret:
        raise HTTPException(status_code=503, detail="Authentication is not configured on this server.")
    expires_in = settings.access_token_minutes * 60
    payload = {
        "sub": str(account.id),
        "role": account.role,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.signing_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def get_current_account(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> Account:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Please sign in to continue.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or not settings.signing_secret:
        raise unauthorized
    try:
        payload = jwt.decode(credentials.credentials, settings.signing_secret, algorithms=[settings.jwt_algorithm])
        account_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        raise unauthorized from None
    account = db.get(Account, account_id)
    if account is None or not account.is_active:
        raise unauthorized
    if not account.is_verified:
        raise HTTPException(status_code=403, detail="Your account is awaiting verification.")
    return account


def require_roles(*roles: Role):
    allowed_roles = {role.value for role in roles}

    def dependency(account: Annotated[Account, Depends(get_current_account)]) -> Account:
        if account.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="You do not have permission to access this resource.")
        return account

    return dependency