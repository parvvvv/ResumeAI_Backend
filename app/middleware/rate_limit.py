"""
Rate limiting configuration using slowapi.
Provides per-endpoint rate limiters keyed by IP or user ID.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse
from app.config import settings


def _get_user_or_ip(request: Request) -> str:
    """
    Rate limit key: use authenticated user ID if available, else fall back to IP.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return str(user_id)
    return get_remote_address(request)


# Global limiter instance — attach to app in main.py
limiter = Limiter(
    key_func=_get_user_or_ip,
    default_limits=[settings.RATE_LIMIT_GENERAL],
    storage_uri="memory://",
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Custom 429 handler with Retry-After header."""
    retry_after = exc.detail.split("per")[-1].strip() if exc.detail else "60"
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please slow down.",
            "retry_after": retry_after,
        },
        headers={"Retry-After": retry_after},
    )
