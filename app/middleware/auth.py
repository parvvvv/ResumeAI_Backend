"""
JWT authentication middleware.
Use as a FastAPI dependency to protect routes.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from app.services.auth_service import decode_jwt

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
