"""
User Pydantic models for request/response validation.
"""

from pydantic import BaseModel, EmailStr, Field


class UserSignup(BaseModel):
    """Request body for user registration."""
    email: EmailStr
    password: str = Field(..., min_length=8, description="Minimum 8 characters")


class UserLogin(BaseModel):
    """Request body for user login."""
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    """Public user info returned in responses."""
    id: str
    email: str


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
