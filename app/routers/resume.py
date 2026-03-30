"""
Resume router: upload, parse, CRUD, and tailoring.
"""

import pdfplumber
import io
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, HTTPException, status, UploadFile, File, Depends, Request, BackgroundTasks
from app.models.resume import ResumeData
from app.models.generated import GenerateResumeRequest
from app.services.ai_service import parse_resume, tailor_resume, generate_summary
from app.services.notification_service import notification_service, Notification
from app.middleware.auth import get_current_user_id
from app.middleware.rate_limit import limiter
from app.security import validate_pdf_upload, sanitize_input
from app.database import get_database
from app.config import settings
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/resume", tags=["Resume"])


@router.post("/upload", status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.RATE_LIMIT_AI)
async def upload_and_parse(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """
    Upload a PDF resume, extract text, parse with AI, and store as base resume.
    Returns the parsed structured JSON.
    """
    # Validate file
    content = await validate_pdf_upload(file)

    # Extract text from PDF
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=1.5, y_tolerance=3)
                if text:
                    pages_text.append(text)
            raw_text = "\n\n".join(pages_text)
    except Exception as e:
        logger.error("pdf_extraction_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Failed to extract text from the PDF. Please ensure it is a valid, text-based PDF.",
        )

    if not raw_text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No text found in PDF. Scanned/image-based PDFs are not supported.",
        )

    # Parse with AI
    try:
        resume_data = await parse_resume(raw_text)
    except ValueError as e:
        logger.error("ai_parse_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI failed to parse the resume. Please try again.",
        )

    # Store as base resume
    db = get_database()
    doc = {
        "userId": user_id,
        "resumeData": resume_data.model_dump(),
        "rawText": raw_text[:10000],  # Store truncated raw text for reference
        "rawTextLength": len(raw_text.strip()),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.base_resumes.insert_one(doc)

    logger.info("resume_uploaded", user_id=user_id, resume_id=str(result.inserted_id))

    return {
        "id": str(result.inserted_id),
        "resumeData": resume_data.model_dump(),
        "message": "Resume parsed and saved successfully.",
    }


@router.get("")
async def get_base_resume(user_id: str = Depends(get_current_user_id)):
    """Get the user's base resume(s)."""
    db = get_database()
    resumes = await db.base_resumes.find(
        {"userId": user_id},
        {"rawText": 0},  # Exclude raw text from response
    ).sort("createdAt", -1).to_list(length=10)

    for r in resumes:
        r["id"] = str(r.pop("_id"))

    return {"resumes": resumes}


@router.get("/{resume_id}")
async def get_resume_by_id(
    resume_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get a specific base resume by ID."""
    db = get_database()
    try:
        resume = await db.base_resumes.find_one(
            {"_id": ObjectId(resume_id), "userId": user_id},
            {"rawText": 0},
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    resume["id"] = str(resume.pop("_id"))
    return resume


@router.put("/{resume_id}")
async def update_base_resume(
    resume_id: str,
    body: ResumeData,
    user_id: str = Depends(get_current_user_id),
):
    """Update a base resume with edited data."""
    db = get_database()
    try:
        result = await db.base_resumes.update_one(
            {"_id": ObjectId(resume_id), "userId": user_id},
            {
                "$set": {
                    "resumeData": body.model_dump(),
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    logger.info("resume_updated", user_id=user_id, resume_id=resume_id)
    return {"message": "Resume updated successfully."}


@router.delete("/{resume_id}")
async def delete_base_resume(
    resume_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Delete a base resume and all its associated generated resumes."""
    db = get_database()
    try:
        result = await db.base_resumes.delete_one(
            {"_id": ObjectId(resume_id), "userId": user_id}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    # Also delete all generated resumes linked to this base
    await db.generated_resumes.delete_many(
        {"baseResumeId": resume_id, "userId": user_id}
    )

    logger.info("resume_deleted", user_id=user_id, resume_id=resume_id)
    return {"message": "Resume and associated tailored resumes deleted."}


@router.post("/tailor", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.RATE_LIMIT_AI)
async def tailor(
    request: Request,
    body: GenerateResumeRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    """Kick off resume tailoring as a background task. Returns immediately."""
    db = get_database()

    # Get base resume
    try:
        base_resume = await db.base_resumes.find_one(
            {"_id": ObjectId(body.baseResumeId), "userId": user_id}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not base_resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Base resume not found.")

    # Sanitize job description
    job_desc = sanitize_input(body.jobDescription)
    raw_text_length = base_resume.get("rawTextLength", 0)

    # Schedule background tailoring
    background_tasks.add_task(
        _tailor_background,
        base_resume["resumeData"],
        job_desc,
        raw_text_length,
        body.baseResumeId,
        user_id,
    )

    return {"message": "Tailoring started. You'll be notified when ready.", "status": "processing"}


async def _tailor_background(
    resume_data: dict,
    job_desc: str,
    raw_text_length: int,
    base_resume_id: str,
    user_id: str,
):
    """Background task: tailor resume with AI, store result + analytics, notify user."""
    try:
        tailored_data, analytics = await tailor_resume(resume_data, job_desc, raw_text_length)
        summary = await generate_summary(tailored_data.model_dump(), job_desc)

        db = get_database()
        gen_doc = {
            "userId": user_id,
            "baseResumeId": base_resume_id,
            "jobDescription": job_desc,
            "modifiedData": tailored_data.model_dump(),
            "summary": summary,
            "analytics": analytics,
            "templateName": "",
            "pdfUrl": "",
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        result = await db.generated_resumes.insert_one(gen_doc)

        logger.info("resume_tailored_bg", user_id=user_id, generated_id=str(result.inserted_id))

        await notification_service.notify(user_id, Notification(
            event="tailor_complete",
            message="Resume tailored! View it on your dashboard.",
            data={
                "resumeId": str(result.inserted_id),
                "analytics": analytics,
            },
        ))

    except Exception as e:
        logger.error("tailor_background_failed", error=str(e))
        await notification_service.notify(user_id, Notification(
            event="tailor_failed",
            message="Tailoring failed. Please try again.",
            data={"error": str(e)},
        ))


