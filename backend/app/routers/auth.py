"""Login / registration endpoints."""

import re

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)


class AuthResponse(BaseModel):
    token: str
    email: str


class MeResponse(BaseModel):
    email: str


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization[7:].strip()


@router.post("/register", response_model=AuthResponse)
def register(body: Credentials, db: Session = Depends(get_db)) -> AuthResponse:
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email address.")
    if not auth_service.email_domain_allowed(email):
        # Intentionally does not disclose the allowlist rule.
        raise HTTPException(status_code=403, detail=auth_service.REGISTRATION_CLOSED_MESSAGE)
    if auth_service.get_user_by_email(db, email) is not None:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    user = User(email=email, password_hash=auth_service.hash_password(body.password))
    db.add(user)
    db.flush()
    token = auth_service.issue_token(db, user)
    return AuthResponse(token=token, email=user.email)


@router.post("/login", response_model=AuthResponse)
def login(body: Credentials, db: Session = Depends(get_db)) -> AuthResponse:
    user = auth_service.get_user_by_email(db, body.email)
    if user is None or not auth_service.verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=auth_service.INVALID_CREDENTIALS_MESSAGE,
        )
    token = auth_service.issue_token(db, user)
    return AuthResponse(token=token, email=user.email)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    # Revokes only this device's session; other logins stay valid.
    auth_service.revoke_token(db, _bearer_token(authorization))


@router.get("/me", response_model=MeResponse)
def me(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> MeResponse:
    user = auth_service.get_user_by_token(db, _bearer_token(authorization))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return MeResponse(email=user.email)
