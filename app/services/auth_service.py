"""
Authentication service: password hashing and JWT management.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import bcrypt
from jose import JWTError, jwt
from app.config import settings
from app.runtime import run_blocking


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


async def hash_password_async(password: str) -> str:
    """Hash a password without blocking the event loop."""
    return await run_blocking(hash_password, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Verify a password without blocking the event loop."""
    return await run_blocking(verify_password, plain_password, hashed_password)


def create_access_token(user_id: str, email: str) -> str:
    """Create a signed JWT access token with userId and email in payload."""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRY_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """Create a signed JWT refresh token."""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_EXPIRY_DAYS)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_jwt(token: str, expected_type: str = "access") -> dict:
    """
    Decode and validate a JWT token.
    Returns the payload dict or raises JWTError.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("sub") is None:
            raise JWTError("Token missing 'sub' claim")
        if payload.get("type", "access") != expected_type:
            raise JWTError(f"Invalid token type. Expected {expected_type}")
        return payload
    except JWTError:
        raise
