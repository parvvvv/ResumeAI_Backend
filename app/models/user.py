"""
User Pydantic models for request/response validation.
"""

from pydantic import BaseModel, EmailStr, Field


class UserSignup(BaseModel):
    """Request body for user registration."""
    email: EmailStr
    password: str = Field(..., min_length=8, description="Minimum 8 characters")
    referralCode: str | None = Field(None, max_length=32, description="Optional referral code")


class UserLogin(BaseModel):
    """Request body for user login."""
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    """Public user info returned in responses."""
    id: str
    email: str
    role: str = "user"
    parsedCount: int = 0
    maxParses: int = 10
    bypassAttemptsLeft: int = 3
    emailVerified: bool = True
    referralCode: str | None = None


class VerifyEmailRequest(BaseModel):
    """Request body for email verification."""
    token: str


class ResendVerificationRequest(BaseModel):
    """Request body to resend a verification email."""
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    """Request body to start a password reset."""
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Request body to complete a password reset."""
    token: str
    new_password: str = Field(..., min_length=8, description="Minimum 8 characters")


class TokenResponse(BaseModel):
    """JWT token response after login/signup."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class RefreshTokenRequest(BaseModel):
    """Client payload to request a new access token."""
    refresh_token: str
    
class RefreshTokenResponse(BaseModel):
    """Refreshed token response."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
