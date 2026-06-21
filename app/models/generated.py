"""
GeneratedResume Pydantic models for tailored resumes.
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class GenerateResumeRequest(BaseModel):
    """Request to tailor a resume for a job description."""
    baseResumeId: str
    jobDescription: str = Field(..., min_length=10)
    rewriteIntensity: Optional[int] = Field(
        default=None,
        description="User's desired rewrite intensity 0-100. None = AI decides automatically.",
    )

    @field_validator("rewriteIntensity")
    @classmethod
    def validate_intensity(cls, v):
        if v is not None and not (0 <= v <= 100):
            raise ValueError("rewriteIntensity must be between 0 and 100")
        return v


class GeneratePDFRequest(BaseModel):
    """Request to generate a PDF from a resume."""
    resumeId: str
    templateId: Optional[str] = None
    templateName: str = Field(default="modern")
    isGenerated: bool = Field(
        default=True,
        description="True if resumeId refers to a generated resume, False for base resume",
    )


class GeneratedResumeResponse(BaseModel):
    """Response model for a generated resume."""
    id: str
    baseResumeId: str
    jobDescription: str
    summary: str = ""
    templateName: str = ""
    pdfUrl: str = ""
    createdAt: Optional[str] = None
