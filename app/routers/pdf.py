"""
PDF generation and serving router.
PDF generation runs as a background task with SSE notifications.
Uses Supabase Storage when configured, local disk otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, HTTPException, status, Depends, Request, BackgroundTasks
from fastapi.responses import FileResponse
from app.models.generated import GeneratePDFRequest
from app.services.pdf_service import generate_pdf_from_resolved_template
from app.services.storage_service import upload_pdf
from app.services.notification_service import notification_service, Notification
from app.middleware.auth import get_current_user, is_template_platform_admin_email
from app.middleware.rate_limit import limiter
from app.database import get_database
from app.config import settings
from app.services.template_service import increment_template_usage
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/resume", tags=["PDF"])


async def _update_pdf_status(
    db,
    resume_id: str,
    is_generated: bool,
    status_value: str,
    extra_fields: Optional[dict] = None,
):
    """Update PDF status metadata for a base or generated resume."""
    collection = db.generated_resumes if is_generated else db.base_resumes
    update_fields = {"pdfStatus": status_value}
    if extra_fields:
        update_fields.update(extra_fields)

    await collection.update_one(
        {"_id": ObjectId(resume_id)},
        {"$set": update_fields},
    )


async def _generate_pdf_background(
    resume_data: dict,
    template_id: Optional[str],
    template_name: str,
    resume_id: str,
    is_generated: bool,
    user_id: str,
):
    """Background task: generate PDF, upload to storage, notify user via SSE."""
    db = get_database()
    try:
        await _update_pdf_status(db, resume_id, is_generated, "processing")
        logger.info("pdf_request_received", resume_id=resume_id, is_generated=is_generated, template_id=template_id, template_name=template_name)
        pdf_bytes, resolved_template = await generate_pdf_from_resolved_template(
            resume_data,
            template_id=template_id,
            template_name=template_name,
        )

        # Upload PDF to storage (Supabase or local)
        filename = f"{uuid.uuid4().hex}.pdf"
        logger.info("pdf_upload_started", filename=filename, resume_id=resume_id)
        pdf_url = await upload_pdf(pdf_bytes, filename)
        logger.info("pdf_upload_finished", filename=filename, resume_id=resume_id)
        await increment_template_usage(resolved_template.id)

        # Update the resume record with pdfUrl and templateName
        await _update_pdf_status(
            db,
            resume_id,
            is_generated,
            "ready",
            {
                "pdfUrl": pdf_url,
                "templateName": template_name,
                "templateId": resolved_template.id,
                "pdfCompletedAt": datetime.now(timezone.utc),
            },
        )

        logger.info(
            "pdf_generated",
            filename=filename,
            size_kb=len(pdf_bytes) // 1024,
            template_source=resolved_template.source,
            template_key=resolved_template.templateKey,
        )

        # Notify user
        await notification_service.notify(user_id, Notification(
            event="pdf_ready",
            message="Your PDF resume is ready to download!",
            data={"pdfUrl": pdf_url, "resumeId": resume_id},
        ))

    except Exception as e:
        logger.error("pdf_generation_failed_bg", error=str(e), resume_id=resume_id, template_id=template_id, template_name=template_name)
        await _update_pdf_status(
            db,
            resume_id,
            is_generated,
            "failed",
            {"pdfCompletedAt": datetime.now(timezone.utc)},
        )
        await notification_service.notify(user_id, Notification(
            event="pdf_failed",
            message="PDF generation failed. Please try again.",
            data={"resumeId": resume_id, "error": str(e)},
        ))


@router.post("/generate-pdf")
@limiter.limit(settings.RATE_LIMIT_PDF)
async def generate_pdf_endpoint(
    request: Request,
    body: GeneratePDFRequest,
    background_tasks: BackgroundTasks,
    payload: dict = Depends(get_current_user),
):
    """
    Kick off PDF generation as a background task.
    Returns immediately. Client receives SSE notification when done.
    """
    user_id = payload["sub"]
    db = get_database()

    # Fetch the resume data
    try:
        if body.isGenerated:
            doc = await db.generated_resumes.find_one(
                {"_id": ObjectId(body.resumeId), "userId": user_id}
            )
            resume_data = doc["modifiedData"] if doc else None
        else:
            doc = await db.base_resumes.find_one(
                {"_id": ObjectId(body.resumeId), "userId": user_id}
            )
            resume_data = doc["resumeData"] if doc else None
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not resume_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    # Schedule background generation
    await _update_pdf_status(
        db,
        body.resumeId,
        body.isGenerated,
        "processing",
        {
            "pdfRequestedAt": datetime.now(timezone.utc),
            "pdfCompletedAt": None,
        },
    )
    background_tasks.add_task(
        _generate_pdf_background,
        resume_data,
        body.templateId,
        body.templateName,
        body.resumeId,
        body.isGenerated,
        user_id,
    )

    return {
        "message": "PDF generation started. You'll be notified when it's ready.",
        "status": "processing",
    }


@router.get("/pdf/{filename}")
@limiter.limit(settings.RATE_LIMIT_PDF)
async def serve_pdf(request: Request, filename: str):
    """Serve a generated PDF file (local storage fallback)."""
    # Sanitize filename to prevent directory traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")

    filepath = settings.PDF_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found.")

    return FileResponse(
        path=str(filepath),
        media_type="application/pdf",
        filename=filename,
    )


@router.get("/templates/available")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def list_available_templates(
    request: Request,
    payload: dict = Depends(get_current_user),
):
    """
    List templates available for PDF generation.
    Returns system templates + published public templates.
    Each template includes a rendered preview HTML using default seed data.
    """
    from app.services.template_service import render_template_preview, merge_template_preview_data
    db = get_database()

    # Always include system templates + public published templates
    query = {
        "$or": [
            {"sourceType": "system", "status": "published"},
            {"visibility": "public", "status": "published"},
        ]
    }

    # If template platform is enabled, also include their own and shared ones
    if settings.ENABLE_TEMPLATE_PLATFORM:
        query["$or"].append({"ownerUserId": payload["sub"]})
        query["$or"].append({"sharedWithUserIds": payload["sub"]})

    docs = await db.templates.find(query).sort("createdAt", -1).to_list(length=50)

    results = []
    for doc in docs:
        # Render preview HTML with seed data
        preview_data = merge_template_preview_data(doc.get("previewSeedData"))
        try:
            preview_html, _ = render_template_preview(doc["htmlContent"], preview_data)
        except Exception:
            preview_html = "<html><body><p>Preview unavailable</p></body></html>"

        results.append({
            "id": str(doc["_id"]),
            "title": doc.get("title", "Template"),
            "description": doc.get("description", ""),
            "sourceType": doc.get("sourceType", "system"),
            "tags": doc.get("tags", []),
            "usageCount": doc.get("usageCount", 0),
            "previewHtml": preview_html,
            "templateSchema": doc.get("templateSchema", {}),
        })

    return {"templates": results}


@router.post("/templates/preview-with-data")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def preview_template_with_resume_data(
    request: Request,
    payload: dict = Depends(get_current_user),
):
    """
    Render a template with the user's actual resume data for a real preview.
    Body: { resumeId, templateId, isGenerated }
    """
    from app.services.template_service import resolve_template, render_template_preview
    from app.models.template import TemplatePreviewPayload

    body = await request.json()
    resume_id = body.get("resumeId")
    template_id = body.get("templateId")
    is_generated = body.get("isGenerated", True)
    user_id = payload["sub"]
    db = get_database()

    # Fetch resume data
    try:
        if is_generated:
            doc = await db.generated_resumes.find_one(
                {"_id": ObjectId(resume_id), "userId": user_id}
            )
            resume_data = doc["modifiedData"] if doc else None
        else:
            doc = await db.base_resumes.find_one(
                {"_id": ObjectId(resume_id), "userId": user_id}
            )
            resume_data = doc["resumeData"] if doc else None
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not resume_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    # Resolve template
    resolved = await resolve_template(template_id=template_id)
    preview_payload = TemplatePreviewPayload(resume=resume_data, extras={})

    try:
        html, warnings = render_template_preview(resolved.htmlContent, preview_payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Template rendering failed: {str(e)}",
        )

    return {"html": html, "warnings": warnings, "templateTitle": resolved.title}
