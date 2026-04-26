"""
Template platform routes.
Includes: CRUD, generation uploads, sharing, sessions, favorites, public catalog.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, UploadFile, File

from app.middleware.auth import get_current_user, get_current_user
from app.models.template import (
    AdminTemplateActionRequest,
    TemplateCreateRequest,
    TemplateDuplicateResponse,
    TemplatePreviewRenderResponse,
    TemplatePreviewRequest,
    TemplateResponse,
    TemplateSessionData,
    TemplateSessionResponse,
    TemplateShareRequest,
    TemplateShareResponse,
    TemplateUpdateRequest,
    TemplateJobResponse,
)
from app.services.template_service import (
    accept_share_token,
    create_template,
    delete_template,
    duplicate_template,
    get_or_create_session,
    get_template_analytics,
    get_template_by_id,
    list_public_templates,
    list_templates,
    render_template_preview,
    request_publish,
    share_template,
    toggle_favorite,
    update_session,
    update_template,
)
from app.services.template_gen_service import (
    create_template_job,
    get_template_job,
)
from app.security import (
    validate_template_html_upload,
    validate_template_image_upload,
)
from app.middleware.rate_limit import limiter
from app.config import settings

router = APIRouter(prefix="/api/templates", tags=["Templates"])


# ---------------------------------------------------------------------------
# Public catalog (no admin gate — read-only browsing)
# ---------------------------------------------------------------------------

@router.get("/public")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def list_public_templates_endpoint(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
):
    """Browse the public template catalog."""
    return await list_public_templates(page, limit)


# ---------------------------------------------------------------------------
# Share token acceptance (any authenticated user)
# ---------------------------------------------------------------------------

@router.post("/accept-share")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def accept_share_token_endpoint(
    request: Request,
    token: str = Query(...),
    payload: dict = Depends(get_current_user),
):
    """Accept a share token to gain access to a template."""
    return await accept_share_token(token, payload["sub"])


# ---------------------------------------------------------------------------
# Core CRUD (template platform admin)
# ---------------------------------------------------------------------------

@router.get("", response_model=list[TemplateResponse])
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def list_templates_endpoint(
    request: Request,
    filter: Optional[str] = Query(default=None),
    payload: dict = Depends(get_current_user),
):
    return await list_templates(payload["sub"], filter)


@router.get("/{template_id}", response_model=TemplateResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_template_endpoint(
    request: Request,
    template_id: str,
    payload: dict = Depends(get_current_user),
):
    return await get_template_by_id(template_id, payload["sub"], increment_view=True)


@router.post("", response_model=TemplateResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def create_template_endpoint(
    request: Request,
    body: TemplateCreateRequest,
    payload: dict = Depends(get_current_user),
):
    return await create_template(body, payload["sub"], payload["email"])


@router.post("/preview", response_model=TemplatePreviewRenderResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def preview_unsaved_template_endpoint(
    request: Request,
    body: TemplatePreviewRequest,
    payload: dict = Depends(get_current_user),
):
    html, warnings = render_template_preview(body.htmlContent, body.previewSeedData)
    return TemplatePreviewRenderResponse(html=html, warnings=warnings)


@router.put("/{template_id}", response_model=TemplateResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def update_template_endpoint(
    request: Request,
    template_id: str,
    body: TemplateUpdateRequest,
    payload: dict = Depends(get_current_user),
):
    return await update_template(template_id, body, payload["sub"])


@router.delete("/{template_id}")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def delete_template_endpoint(
    request: Request,
    template_id: str,
    payload: dict = Depends(get_current_user),
):
    await delete_template(template_id, payload["sub"])
    return {"message": "Template deleted successfully."}


@router.post("/{template_id}/preview", response_model=TemplatePreviewRenderResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def preview_template_endpoint(
    request: Request,
    template_id: str,
    body: TemplatePreviewRequest,
    payload: dict = Depends(get_current_user),
):
    await get_template_by_id(template_id, payload["sub"], increment_view=False)
    html, warnings = render_template_preview(body.htmlContent, body.previewSeedData)
    return TemplatePreviewRenderResponse(html=html, warnings=warnings)


@router.post("/{template_id}/duplicate", response_model=TemplateDuplicateResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def duplicate_template_endpoint(
    request: Request,
    template_id: str,
    payload: dict = Depends(get_current_user),
):
    duplicated = await duplicate_template(template_id, payload["sub"], payload["email"])
    return TemplateDuplicateResponse(id=duplicated.id, status=duplicated.status)


# ---------------------------------------------------------------------------
# Phase 5: Sharing
# ---------------------------------------------------------------------------

@router.post("/{template_id}/share")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def share_template_endpoint(
    request: Request,
    template_id: str,
    body: TemplateShareRequest,
    payload: dict = Depends(get_current_user),
):
    """Share a template with users by email/userId or generate a token link."""
    return await share_template(
        template_id,
        payload["sub"],
        emails=body.emails,
        user_ids=body.userIds,
        generate_token=body.generateToken,
    )


@router.post("/{template_id}/request-publish")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def request_publish_endpoint(
    request: Request,
    template_id: str,
    payload: dict = Depends(get_current_user),
):
    """Request a template be reviewed for the public catalog."""
    return await request_publish(template_id, payload["sub"])


# ---------------------------------------------------------------------------
# Phase 6: AI Template Generation
# ---------------------------------------------------------------------------

@router.post("/generate/upload-html")
@limiter.limit(settings.RATE_LIMIT_AI)
async def upload_html_for_generation(
    request: Request,
    file: UploadFile = File(...),
    payload: dict = Depends(get_current_user),
):
    """
    Upload an HTML file to generate a template draft.
    Detects hardcoded resume values and replaces them with Jinja placeholders.
    Always saves as draft only.
    """
    html_content = await validate_template_html_upload(file)
    job_id = await create_template_job(
        owner_user_id=payload["sub"],
        source_type="html_upload",
        raw_content=html_content,
        mime_type=file.content_type or "text/html",
    )
    return {
        "jobId": job_id,
        "status": "queued",
        "message": "Template generation job created. Poll status to track progress.",
    }


@router.post("/generate/upload-image")
@limiter.limit(settings.RATE_LIMIT_AI)
async def upload_image_for_generation(
    request: Request,
    file: UploadFile = File(...),
    payload: dict = Depends(get_current_user),
):
    """
    Upload an image (screenshot of a resume) to generate a template draft.
    Uses AI vision to recreate the layout as HTML, then detects and replaces hardcoded values.
    Always saves as draft only.
    """
    image_bytes = await validate_template_image_upload(file)
    job_id = await create_template_job(
        owner_user_id=payload["sub"],
        source_type="image_upload",
        raw_content=image_bytes,
        mime_type=file.content_type or "image/png",
    )
    return {
        "jobId": job_id,
        "status": "queued",
        "message": "Template generation job created. Poll status to track progress.",
    }


@router.get("/generate/{job_id}/status")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def poll_generation_status(
    request: Request,
    job_id: str,
    payload: dict = Depends(get_current_user),
):
    """Poll the status of a template generation job."""
    job = await get_template_job(job_id, payload["sub"])
    if not job:
        return {"error": "Job not found.", "status": "not_found"}
    return job


# ---------------------------------------------------------------------------
# Phase 7: Template Sessions (Dynamic Missing Data Forms)
# ---------------------------------------------------------------------------

@router.get("/{template_id}/session", response_model=TemplateSessionResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_session_endpoint(
    request: Request,
    template_id: str,
    resumeId: str = Query(...),
    payload: dict = Depends(get_current_user),
):
    """Get or create a template session for user+resume+template."""
    return await get_or_create_session(payload["sub"], resumeId, template_id)


@router.put("/{template_id}/session", response_model=TemplateSessionResponse)
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def update_session_endpoint(
    request: Request,
    template_id: str,
    resumeId: str = Query(...),
    body: TemplateSessionData = ...,
    payload: dict = Depends(get_current_user),
):
    """Save session data (extras) for a template+resume combo."""
    return await update_session(payload["sub"], resumeId, template_id, body.extras)


# ---------------------------------------------------------------------------
# Phase 8: Favorites & Analytics
# ---------------------------------------------------------------------------

@router.post("/{template_id}/favorite")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def toggle_favorite_endpoint(
    request: Request,
    template_id: str,
    payload: dict = Depends(get_current_user),
):
    """Toggle a template as favorited."""
    return await toggle_favorite(template_id, payload["sub"])


@router.get("/{template_id}/analytics")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_template_analytics_endpoint(
    request: Request,
    template_id: str,
    payload: dict = Depends(get_current_user),
):
    """Get analytics for a specific template."""
    return await get_template_analytics(template_id)
