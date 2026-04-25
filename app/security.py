"""
Security utilities: input sanitization, file validation, security headers, request ID.
"""

import uuid
from fastapi import UploadFile, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from jose import JWTError
import bleach
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

_ALLOWED_TAGS: list[str] = []  # Strip ALL HTML tags
_ALLOWED_ATTRIBUTES: dict = {}


def sanitize_input(text: str) -> str:
    """Strip all HTML/JS from user-provided text."""
    if not text:
        return text
    return bleach.clean(text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, strip=True)


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------

_ALLOWED_MIME_TYPES = {"application/pdf"}
_MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


async def validate_pdf_upload(file: UploadFile) -> bytes:
    """
    Validate an uploaded file is a PDF within size limits.
    Returns the file contents as bytes.
    """
    # Check MIME type
    if file.content_type not in _ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type '{file.content_type}'. Only PDF files are accepted.",
        )

    # Read and check size
    content = await file.read()
    if len(content) > _MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {_MAX_SIZE_BYTES // (1024 * 1024)} MB.",
        )

    return content


# ---------------------------------------------------------------------------
# Security Headers Middleware
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        return response


class AuthContextMiddleware(BaseHTTPMiddleware):
    """Attach authenticated user context when a valid bearer token is present."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        auth_header = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :].strip()
            if token:
                from app.services.auth_service import decode_jwt

                try:
                    payload = decode_jwt(token)
                    request.state.user_id = payload.get("sub")
                except JWTError:
                    request.state.user_id = None
        else:
            request.state.user_id = None

        return await call_next(request)


# ---------------------------------------------------------------------------
# Request ID Middleware
# ---------------------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request for tracing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Bind to structlog context for this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        logger.info(
            "request_started",
            method=request.method,
            path=str(request.url.path),
        )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "request_completed",
            status_code=response.status_code,
        )
        return response
