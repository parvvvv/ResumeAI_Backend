"""
JWT authentication middleware.
Use as a FastAPI dependency to protect routes.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from app.services.auth_service import decode_jwt
from app.config import settings

_bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """
    FastAPI dependency: extracts and validates the JWT from the Authorization header.
    Returns the decoded payload dict with 'sub' (userId) and 'email'.
    """
    token = credentials.credentials
    try:
        payload = decode_jwt(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def get_current_user_id(
    payload: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency that returns just the user ID string."""
    return payload["sub"]


async def get_current_admin_user(
    payload: dict = Depends(get_current_user),
) -> dict:
    """Require an authenticated admin user."""
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return payload


def is_template_platform_admin_email(email: Optional[str]) -> bool:
    """Check whether an email is allowed to access the template platform."""
    if not email:
        return False
    allowed_emails = set(settings.ADMIN_EMAILS or [])
    return email.lower() in allowed_emails


async def get_template_platform_admin(
    payload: dict = Depends(get_current_user),
) -> dict:
    """Require template platform access for the rollout admin and feature flag."""
    if not settings.ENABLE_TEMPLATE_PLATFORM:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template platform is not enabled.",
        )

    if not is_template_platform_admin_email(payload.get("email")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Template platform access required.",
        )

    return payload


async def require_admin_or_api_key(request: Request) -> dict:
    """
    Security dependency: Permits admin actions via either:
    1. A valid X-Admin-API-Key header matching the backend config settings.
    2. A valid admin JWT Bearer token in the Authorization header.
    """
    # 1. Check for Admin API Key header
    api_key = request.headers.get("X-Admin-API-Key")
    if settings.ADMIN_API_KEY and api_key == settings.ADMIN_API_KEY:
        return {"role": "admin", "email": "admin_api@system_backend", "sub": "system_admin"}

    # 2. Check for standard Authorization header (JWT Bearer)
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header or X-Admin-API-Key required",
        )

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization type. Must be Bearer token.",
        )

    token = parts[1]
    from jose import JWTError
    from app.services.auth_service import decode_jwt

    try:
        payload = decode_jwt(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    return payload
