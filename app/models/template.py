"""
Template platform models for DB-backed resume templates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


TemplateVisibility = Literal["private", "public", "selective"]
TemplateStatus = Literal["draft", "published", "archived", "pending_review"]
TemplateSourceType = Literal["system", "manual", "ai_generated", "uploaded", "html_generated", "image_generated"]
TemplateJobStatus = Literal["queued", "processing", "needs_review", "completed", "failed"]


class TemplateFieldDefinition(BaseModel):
    key: str
    label: str
    type: Literal["text", "textarea", "list", "select", "date", "image"] = "text"
    required: bool = False
    description: str = ""
    options: list[str] = Field(default_factory=list)
    imageAspectRatio: str = ""


class TemplateSchema(BaseModel):
    sections: list[str] = Field(default_factory=list)
    fields: list[TemplateFieldDefinition] = Field(default_factory=list)


class TemplatePreviewPayload(BaseModel):
    resume: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class TemplateBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: str = ""
    htmlContent: str = Field(..., min_length=1)
    templateSchema: TemplateSchema = Field(default_factory=TemplateSchema)
    previewSeedData: TemplatePreviewPayload = Field(default_factory=TemplatePreviewPayload)
    tags: list[str] = Field(default_factory=list)
    visibility: TemplateVisibility = "private"

    @model_validator(mode="after")
    def normalize_tags(self) -> "TemplateBase":
        self.tags = [tag.strip().lower() for tag in self.tags if tag.strip()]
        return self


class TemplateCreateRequest(TemplateBase):
    pass


class TemplateUpdateRequest(TemplateBase):
    status: TemplateStatus = "draft"


class TemplateResponse(TemplateBase):
    id: str
    ownerUserId: Optional[str] = None
    ownerEmail: Optional[str] = None
    sourceType: TemplateSourceType = "manual"
    status: TemplateStatus = "draft"
    isEditable: bool = True
    templateKey: str = ""
    usageCount: int = 0
    favoriteCount: int = 0
    viewCount: int = 0
    downloadCount: int = 0
    basedOnTemplateId: Optional[str] = None
    version: int = 1
    sharedWithUserIds: list[str] = Field(default_factory=list)
    shareTokens: list[str] = Field(default_factory=list)
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class TemplatePreviewRequest(BaseModel):
    previewSeedData: TemplatePreviewPayload
    htmlContent: str


class TemplatePreviewRenderResponse(BaseModel):
    html: str
    warnings: list[str] = Field(default_factory=list)


class TemplateResolverResult(BaseModel):
    id: Optional[str] = None
    templateKey: str
    title: str
    htmlContent: str
    source: Literal["legacy_name", "db_template"]
    isEditable: bool = False
    sourceType: TemplateSourceType = "system"


class GenerateTemplatePDFRequest(BaseModel):
    resumeId: str
    templateId: str
    isGenerated: bool = True


class TemplateDuplicateResponse(BaseModel):
    id: str
    status: TemplateStatus = "draft"


# ---------------------------------------------------------------------------
# Template Generation Job models (Phase 6)
# ---------------------------------------------------------------------------

class TemplateJobResponse(BaseModel):
    """Response model for a template generation job."""
    id: str
    status: TemplateJobStatus
    sourceType: Literal["html_upload", "image_upload"]
    templateId: Optional[str] = None
    htmlContent: Optional[str] = None
    schema_result: Optional[TemplateSchema] = Field(default=None, alias="schemaResult")
    fieldMappings: Optional[dict[str, str]] = None
    warnings: list[str] = Field(default_factory=list)
    errorMessage: Optional[str] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Sharing models (Phase 5)
# ---------------------------------------------------------------------------

class TemplateShareRequest(BaseModel):
    """Invite users by email/userId or generate a share token."""
    emails: list[str] = Field(default_factory=list)
    userIds: list[str] = Field(default_factory=list)
    generateToken: bool = False


class TemplateShareResponse(BaseModel):
    sharedWithUserIds: list[str] = Field(default_factory=list)
    shareTokens: list[str] = Field(default_factory=list)
    message: str = ""


class TemplatePublishRequest(BaseModel):
    """User requests their template be reviewed for public catalog."""
    pass


# ---------------------------------------------------------------------------
# Template Session models (Phase 7 — Dynamic Missing Data Forms)
# ---------------------------------------------------------------------------

class TemplateSessionData(BaseModel):
    """User-provided extra field values for a specific resume+template combo."""
    extras: dict[str, Any] = Field(default_factory=dict)


class TemplateSessionResponse(BaseModel):
    id: str
    userId: str
    resumeId: str
    templateId: str
    extras: dict[str, Any] = Field(default_factory=dict)
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Analytics models (Phase 8)
# ---------------------------------------------------------------------------

class TemplateAnalyticsResponse(BaseModel):
    templateId: str
    title: str
    usageCount: int = 0
    downloadCount: int = 0
    favoriteCount: int = 0
    viewCount: int = 0
    conversionRate: float = 0.0


# ---------------------------------------------------------------------------
# Admin governance models (Phase 5)
# ---------------------------------------------------------------------------

class AdminTemplateActionRequest(BaseModel):
    """Admin approve/reject with optional reason."""
    reason: str = ""
