"""
Authentication router: signup and login with rate limiting.
"""

from fastapi import APIRouter, HTTPException, status, Request
from app.models.user import UserSignup, UserLogin, TokenResponse, UserResponse
from app.services.auth_service import hash_password, verify_password, create_jwt
from app.database import get_database
from app.security import sanitize_input
from app.middleware.rate_limit import limiter
from app.config import settings
from datetime import datetime, timezone
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


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
    user_doc = {
        "email": email,
        "passwordHash": hash_password(body.password),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)

    logger.info("user_created", user_id=user_id, email=email)

    # Issue JWT
    token = create_jwt(user_id, email)
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user_id, email=email),
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(request: Request, body: UserLogin):
    """Authenticate a user and return a JWT."""
    db = get_database()

    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})

    if not user or not verify_password(body.password, user["passwordHash"]):
        # Intentionally vague error to prevent user enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    user_id = str(user["_id"])
    token = create_jwt(user_id, email)

    logger.info("user_logged_in", user_id=user_id)

    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user_id, email=email),
    )
