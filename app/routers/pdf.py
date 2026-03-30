"""
PDF generation and serving router.
PDF generation runs as a background task with SSE notifications.
Uses Supabase Storage when configured, local disk otherwise.
"""

import uuid
from bson import ObjectId
from fastapi import APIRouter, HTTPException, status, Depends, Request, BackgroundTasks
from fastapi.responses import FileResponse
from app.models.generated import GeneratePDFRequest
from app.services.pdf_service import generate_pdf
from app.services.storage_service import upload_pdf
from app.services.notification_service import notification_service, Notification
from app.middleware.auth import get_current_user_id
from app.middleware.rate_limit import limiter
from app.database import get_database
from app.config import settings
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/resume", tags=["PDF"])


async def _generate_pdf_background(
    resume_data: dict,
    template_name: str,
    resume_id: str,
    is_generated: bool,
    user_id: str,
):
    """Background task: generate PDF, upload to storage, notify user via SSE."""
    try:
        pdf_bytes = await generate_pdf(resume_data, template_name)

        # Upload PDF to storage (Supabase or local)
        filename = f"{uuid.uuid4().hex}.pdf"
        pdf_url = await upload_pdf(pdf_bytes, filename)

        # Update the resume record with pdfUrl and templateName
        db = get_database()
        if is_generated:
            await db.generated_resumes.update_one(
                {"_id": ObjectId(resume_id)},
                {"$set": {"pdfUrl": pdf_url, "templateName": template_name}},
            )

        logger.info("pdf_generated", filename=filename, size_kb=len(pdf_bytes) // 1024)

        # Notify user
        await notification_service.notify(user_id, Notification(
            event="pdf_ready",
            message="Your PDF resume is ready to download!",
            data={"pdfUrl": pdf_url, "resumeId": resume_id},
        ))

    except Exception as e:
        logger.error("pdf_generation_failed_bg", error=str(e))
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
    user_id: str = Depends(get_current_user_id),
):
    """
    Kick off PDF generation as a background task.
    Returns immediately. Client receives SSE notification when done.
    """
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
    background_tasks.add_task(
        _generate_pdf_background,
        resume_data,
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
async def serve_pdf(filename: str):
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
