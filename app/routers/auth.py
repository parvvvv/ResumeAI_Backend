"""
Authentication router: signup and login with rate limiting.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status, Request
from app.models.user import UserSignup, UserLogin, TokenResponse, UserResponse, RefreshTokenRequest, RefreshTokenResponse
from app.services.auth_service import hash_password_async, verify_password_async, create_access_token, create_refresh_token, decode_jwt
from jose import JWTError
from app.database import get_database
from app.security import sanitize_input
from app.middleware.rate_limit import limiter
from app.config import settings
from datetime import datetime, timezone
from bson.objectid import ObjectId
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def _role_for_email(email: str, existing_role: str | None = None) -> str:
    """Resolve a user's role, allowing env-based admin bootstrap."""
    if email.lower() in settings.ADMIN_EMAILS:
        return "admin"
    return existing_role or "user"


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def signup(request: Request, body: UserSignup):
    """Register a new user and return a JWT."""
    db = get_database()

    # Sanitize email
    email = sanitize_input(body.email.lower().strip())

    # Check if user already exists
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    # Create user document
    role = _role_for_email(email)
    user_doc = {
        "email": email,
        "passwordHash": await hash_password_async(body.password),
        "role": role,
        "createdAt": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)

    logger.info("user_created", user_id=user_id, email=email)

    # Issue JWT
    access_token = create_access_token(user_id, email, role)
    refresh_token = create_refresh_token(user_id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse(id=user_id, email=email, role=role),
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(request: Request, body: UserLogin):
    """Authenticate a user and return a JWT."""
    db = get_database()

    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})

    if not user or not await verify_password_async(body.password, user["passwordHash"]):
        # Intentionally vague error to prevent user enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    user_id = str(user["_id"])
    role = _role_for_email(email, user.get("role", "user"))
    if role != user.get("role"):
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"role": role}})

    access_token = create_access_token(user_id, email, role)
    refresh_token = create_refresh_token(user_id)

    logger.info("user_logged_in", user_id=user_id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse(id=user_id, email=email, role=role),
    )


@router.post("/refresh", response_model=RefreshTokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def refresh_token_endpoint(request: Request, body: RefreshTokenRequest):
    """Exchange a valid refresh token for a new access & refresh token pair."""
    try:
        payload = decode_jwt(body.refresh_token, expected_type="refresh")
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
        
    email = user["email"]
    role = _role_for_email(email, user.get("role", "user"))
    if role != user.get("role"):
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"role": role}})
    
    new_access_token = create_access_token(user_id, email, role)
    new_refresh_token = create_refresh_token(user_id)
    
    logger.info("token_refreshed", user_id=user_id)
    
    return RefreshTokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token
    )
