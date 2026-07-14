"""Email/password auth with opaque bearer tokens.

Registration is restricted to an allowlist of email domains (configured, not
disclosed to clients — other domains get a neutral "contact us" message).
Passwords use PBKDF2-SHA256; sessions are opaque random tokens, one row per
login in auth_sessions so multiple devices coexist (the legacy single-token
columns on users are still honoured for sessions issued before the change).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuthSession, User

_PBKDF2_ITERATIONS = 240_000

REGISTRATION_CLOSED_MESSAGE = "Registration is not available for this email. Please contact us."
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password."


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$", 3)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def email_domain_allowed(email: str) -> bool:
    settings = get_settings()
    allowed = {
        d.strip().lower().lstrip("@")
        for d in (settings.auth_allowed_email_domains or "").split(",")
        if d.strip()
    }
    if not allowed:
        return True
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain in allowed


def issue_token(db: Session, user: User) -> str:
    settings = get_settings()
    token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    db.add(AuthSession(user_id=user.id, token=token, expires_at=now + timedelta(days=settings.auth_token_ttl_days)))
    user.last_login_at = now
    # Opportunistic cleanup so the table doesn't accumulate dead sessions.
    db.query(AuthSession).filter(AuthSession.user_id == user.id, AuthSession.expires_at < now).delete()
    db.commit()
    return token


def revoke_token(db: Session, token: str) -> None:
    if not token:
        return
    db.query(AuthSession).filter(AuthSession.token == token).delete()
    user = db.scalar(select(User).where(User.auth_token == token))
    if user is not None:
        user.auth_token = None
        user.token_expires_at = None
    db.commit()


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


def _expired(expires: datetime | None) -> bool:
    if expires is None:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < datetime.now(timezone.utc)


def get_user_by_token(db: Session, token: str) -> User | None:
    if not token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token == token))
    if session is not None:
        if _expired(session.expires_at):
            return None
        return db.get(User, session.user_id)
    # Legacy single-token sessions issued before auth_sessions existed.
    user = db.scalar(select(User).where(User.auth_token == token))
    if user is None or _expired(user.token_expires_at):
        return None
    return user


def token_is_valid(db: Session, token: str) -> bool:
    return get_user_by_token(db, token) is not None
