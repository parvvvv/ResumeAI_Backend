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
from app.services.ai_service import parse_resume, analyze_alignment, tailor_content, generate_summary
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

    # Stage 1 – text extraction
    await notification_service.notify(user_id, Notification(
        event="parse_progress",
        message="Extracting text from your PDF...",
        data={"percent": 15},
    ))

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

    # Stage 2 – AI parsing
    await notification_service.notify(user_id, Notification(
        event="parse_progress",
        message="AI is identifying and mapping resume sections...",
        data={"percent": 45},
    ))

    # Parse with AI
    try:
        resume_data = await parse_resume(raw_text)
    except ValueError as e:
        logger.error("ai_parse_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI failed to parse the resume. Please try again.",
        )

    # Stage 3 – persist
    await notification_service.notify(user_id, Notification(
        event="parse_progress",
        message="Finalizing structured data and saving your resume...",
        data={"percent": 85},
    ))

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

    await notification_service.notify(user_id, Notification(
        event="parse_progress",
        message="Resume parsed successfully!",
        data={"percent": 100},
    ))

    return {
        "id": str(result.inserted_id),
        "resumeData": resume_data.model_dump(),
        "message": "Resume parsed and saved successfully.",
    }


@router.get("")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_base_resume(request: Request, user_id: str = Depends(get_current_user_id)):
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
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_resume_by_id(
    request: Request,
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
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def update_base_resume(
    request: Request,
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
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def delete_base_resume(
    request: Request,
    resume_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Delete a base resume and all its associated generated resumes (including PDFs)."""
    from app.services.storage_service import delete_pdf

    db = get_database()
    try:
        result = await db.base_resumes.delete_one(
            {"_id": ObjectId(resume_id), "userId": user_id}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    # Find all generated resumes to delete their PDFs
    generated = await db.generated_resumes.find(
        {"baseResumeId": resume_id, "userId": user_id},
        {"pdfUrl": 1}
    ).to_list(length=100)

    # Delete PDFs from storage
    for doc in generated:
        pdf_url = doc.get("pdfUrl")
        if pdf_url:
            await delete_pdf(pdf_url)

    # Delete all generated resumes linked to this base
    await db.generated_resumes.delete_many(
        {"baseResumeId": resume_id, "userId": user_id}
    )

    logger.info("resume_deleted", user_id=user_id, resume_id=resume_id, deleted_pdfs=len(generated))
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
    """Background task: two-step AI tailor with SSE progress events between each stage."""
    try:
        # ── Stage 1: Gap Analysis (fast call) ──────────────────────────
        await notification_service.notify(user_id, Notification(
            event="tailor_progress",
            message="🔍 Analyzing job description and extracting focus keywords...",
            data={"percent": 10, "baseResumeId": base_resume_id},
        ))

        alignment = await analyze_alignment(resume_data, job_desc)

        # ── Stage 2: Broadcast early ATS insight ───────────────────────
        await notification_service.notify(user_id, Notification(
            event="tailor_progress",
            message="📊 Gap analysis complete. Starting resume optimization...",
            data={
                "percent": 30,
                "baseResumeId": base_resume_id,
                "earlyAtsScore": alignment.get("atsScore", 0),
                "matchedKeywords": alignment.get("matchedKeywords", []),
                "missingKeywords": alignment.get("missingKeywords", []),
            },
        ))

        # ── Stage 3: Full rewrite (heavy call) ─────────────────────────
        await notification_service.notify(user_id, Notification(
            event="tailor_progress",
            message="✍️ Reframing experience bullets and projects (AI at work)...",
            data={"percent": 55, "baseResumeId": base_resume_id},
        ))

        tailored_data, analytics = await tailor_content(
            resume_data, job_desc, alignment, raw_text_length
        )

        # ── Stage 4: Summary generation ────────────────────────────────
        await notification_service.notify(user_id, Notification(
            event="tailor_progress",
            message="✨ Finalizing tailored resume and generating dashboard summary...",
            data={"percent": 90, "baseResumeId": base_resume_id},
        ))

        summary = await generate_summary(tailored_data.model_dump(), job_desc)

        # ── Persist ────────────────────────────────────────────────────
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
                "baseResumeId": base_resume_id,
                "analytics": analytics,
            },
        ))

    except Exception as e:
        logger.error("tailor_background_failed", error=str(e))
        await notification_service.notify(user_id, Notification(
            event="tailor_failed",
            message="Tailoring failed. Please try again.",
            data={"error": str(e), "baseResumeId": base_resume_id},
        ))


