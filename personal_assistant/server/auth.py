"""Authentication helpers — cookie session, password hashing, user queries."""

import os
from datetime import datetime

import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import select

from personal_assistant.db.models import User, get_session

# ── Secret key ────────────────────────────────────────────────────────────────
# In production set SECRET_KEY env var. Falls back to a random key (sessions
# won't survive restarts without it).
_SECRET = os.environ.get("SECRET_KEY", os.urandom(32).hex())
_serializer = URLSafeTimedSerializer(_SECRET)
_COOKIE_NAME = "pa_session"
_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── User queries ──────────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> User | None:
    session = get_session()
    try:
        return session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
    finally:
        session.close()


def get_user_by_id(user_id: int) -> User | None:
    session = get_session()
    try:
        return session.get(User, user_id)
    finally:
        session.close()


def create_user(username: str, password: str, is_approved: bool = False, is_admin: bool = False) -> User:
    session = get_session()
    try:
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_approved=is_approved,
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    finally:
        session.close()


def approve_user(user_id: int) -> bool:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user:
            user.is_approved = True
            session.commit()
            return True
        return False
    finally:
        session.close()


def reject_user(user_id: int) -> bool:
    """Delete a pending (unapproved) user."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user and not user.is_approved:
            session.delete(user)
            session.commit()
            return True
        return False
    finally:
        session.close()


def get_pending_users() -> list[User]:
    session = get_session()
    try:
        result = session.execute(
            select(User).where(User.is_approved == False)
        ).scalars().all()
        session.expunge_all()
        return list(result)
    finally:
        session.close()


def user_count() -> int:
    session = get_session()
    try:
        return session.execute(select(User)).scalars().all().__len__()
    finally:
        session.close()


# ── Session / cookie helpers ──────────────────────────────────────────────────

def create_session_cookie(user_id: int) -> str:
    """Create a signed cookie value for the given user ID."""
    return _serializer.dumps({"uid": user_id})


def read_session_cookie(cookie_value: str) -> int | None:
    """Read and verify a session cookie. Returns user_id or None."""
    try:
        data = _serializer.loads(cookie_value, max_age=_MAX_AGE)
        return data.get("uid")
    except (BadSignature, SignatureExpired):
        return None


def get_current_user_from_request(request) -> User | None:
    """Extract the authenticated user from a FastAPI request, or None."""
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    uid = read_session_cookie(cookie)
    if uid is None:
        return None
    user = get_user_by_id(uid)
    if user and user.is_approved:
        return user
    return None
