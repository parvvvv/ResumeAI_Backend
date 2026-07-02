"""
Authentication router: signup and login with rate limiting.
"""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException, status, Request
from app.models.user import (
    UserSignup, UserLogin, TokenResponse, UserResponse,
    RefreshTokenRequest, RefreshTokenResponse,
    VerifyEmailRequest, ResendVerificationRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
)
from app.services.auth_service import (
    hash_password_async, verify_password_async,
    create_access_token, create_refresh_token, decode_jwt,
    create_action_token, password_version,
)
from app.services import email_service
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


def _new_referral_code() -> str:
    """Short, shareable, unambiguous referral code."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I/L/O/0/1
    return "".join(secrets.choice(alphabet) for _ in range(8))


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def signup(request: Request, body: UserSignup):
    """Register a new user and return a JWT."""
    # When self-signup is disabled, registration requires the admin API key.
    if not settings.ENABLE_SELF_SIGNUP:
        admin_key = request.headers.get("X-Admin-API-Key")
        if not settings.ADMIN_API_KEY or admin_key != settings.ADMIN_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is restricted to administrators. A valid X-Admin-API-Key header is required.",
            )

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

    # Resolve referral (invalid/unknown codes are ignored, not an error)
    referred_by = None
    if body.referralCode:
        referrer = await db.users.find_one(
            {"referralCode": body.referralCode.strip().upper()}, {"_id": 1}
        )
        if referrer:
            referred_by = str(referrer["_id"])

    # Without an email provider configured, accounts auto-verify (dev mode).
    email_verified = not email_service.is_email_configured()

    # Create user document
    role = _role_for_email(email)
    user_doc = {
        "email": email,
        "passwordHash": await hash_password_async(body.password),
        "role": role,
        "parsedCount": 0,
        "maxParses": 10,
        "bypassAttemptsLeft": 3,
        "lastBypassDate": datetime.now(timezone.utc).date().isoformat(),
        "emailVerified": email_verified,
        "referralCode": _new_referral_code(),
        "referredBy": referred_by,
        "createdAt": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)

    logger.info("user_created", user_id=user_id, email=email, referred=bool(referred_by))

    # Send verification email (best-effort, never blocks signup)
    if not email_verified:
        token = create_action_token(user_id, "email_verify", settings.EMAIL_VERIFY_TOKEN_HOURS)
        asyncio.create_task(email_service.send_verification_email(email, token))

    # Issue JWT
    access_token = create_access_token(user_id, email, role)
    refresh_token = create_refresh_token(user_id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse(
            id=user_id,
            email=email,
            role=role,
            parsedCount=0,
            maxParses=10,
            bypassAttemptsLeft=3,
            emailVerified=email_verified,
            referralCode=user_doc["referralCode"],
        ),
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
    
    # Check and perform daily attempts reset on login
    current_date_str = datetime.now(timezone.utc).date().isoformat()
    last_bypass_date = user.get("lastBypassDate", "")
    bypass_attempts = user.get("bypassAttemptsLeft", 3)
    parsed_count = user.get("parsedCount", 0)
    max_parses = user.get("maxParses", 10)
    
    update_fields = {}
    if role != user.get("role"):
        update_fields["role"] = role
        
    if current_date_str != last_bypass_date and role != "admin":
        bypass_attempts = 3
        update_fields["bypassAttemptsLeft"] = 3
        update_fields["lastBypassDate"] = current_date_str
        
    if update_fields:
        await db.users.update_one({"_id": user["_id"]}, {"$set": update_fields})

    access_token = create_access_token(user_id, email, role)
    refresh_token = create_refresh_token(user_id)

    logger.info("user_logged_in", user_id=user_id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse(
            id=user_id,
            email=email,
            role=role,
            parsedCount=parsed_count,
            maxParses=max_parses,
            bypassAttemptsLeft=bypass_attempts,
            emailVerified=user.get("emailVerified", True),
            referralCode=user.get("referralCode"),
        ),
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


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@router.post("/verify-email")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def verify_email(request: Request, body: VerifyEmailRequest):
    """Confirm a user's email address using the token from their inbox."""
    try:
        payload = decode_jwt(body.token, expected_type="email_verify")
        user_id = payload["sub"]
    except (JWTError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link. Please request a new one.",
        )

    db = get_database()
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"emailVerified": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

    logger.info("email_verified", user_id=user_id)
    return {"message": "Email verified. You're all set!"}


@router.post("/resend-verification")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def resend_verification(request: Request, body: ResendVerificationRequest):
    """Resend the verification email. Response is intentionally vague."""
    generic = {"message": "If an unverified account exists for this email, a new link has been sent."}

    if not email_service.is_email_configured():
        return generic

    db = get_database()
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if user and not user.get("emailVerified", True):
        token = create_action_token(
            str(user["_id"]), "email_verify", settings.EMAIL_VERIFY_TOKEN_HOURS
        )
        asyncio.create_task(email_service.send_verification_email(email, token))

    return generic


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@router.post("/forgot-password")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def forgot_password(request: Request, body: ForgotPasswordRequest):
    """Start a password reset. Response is intentionally vague (no enumeration)."""
    generic = {"message": "If an account exists for this email, a reset link has been sent."}

    if not email_service.is_email_configured():
        # No email provider — surface a clear operational error instead of a dead end.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password reset is not available right now. Please contact support.",
        )

    db = get_database()
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if user:
        # Bind the token to the current password so it dies after one use.
        token = create_action_token(
            str(user["_id"]),
            "password_reset",
            settings.PASSWORD_RESET_TOKEN_HOURS,
            extra={"pv": password_version(user["passwordHash"])},
        )
        asyncio.create_task(email_service.send_password_reset_email(email, token))
        logger.info("password_reset_requested", user_id=str(user["_id"]))

    return generic


@router.post("/reset-password")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def reset_password(request: Request, body: ResetPasswordRequest):
    """Complete a password reset with the token from the email."""
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired reset link. Please request a new one.",
    )

    try:
        payload = decode_jwt(body.token, expected_type="password_reset")
        user_id = payload["sub"]
    except (JWTError, KeyError):
        raise invalid

    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise invalid

    # Reject tokens minted before the last password change (single-use)
    if payload.get("pv") != password_version(user["passwordHash"]):
        raise invalid

    new_hash = await hash_password_async(body.new_password)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"passwordHash": new_hash, "emailVerified": True}},
    )

    logger.info("password_reset_completed", user_id=user_id)
    return {"message": "Password updated. You can now log in with your new password."}
