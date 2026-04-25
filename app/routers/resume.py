"""
Resume router: upload, parse, CRUD, and tailoring.
"""

import json
import pdfplumber
import io
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, HTTPException, status, UploadFile, File, Depends, Request
from fastapi.responses import StreamingResponse
from app.models.resume import ResumeData
from app.models.generated import GenerateResumeRequest
from app.services.ai_service import parse_resume, analyze_alignment, optimize_skills, rewrite_experience, final_polish, generate_summary
from app.services.notification_service import notification_service, Notification
from app.runtime import get_runtime, run_blocking
from app.middleware.auth import get_current_user_id
from app.middleware.rate_limit import limiter
from app.security import validate_pdf_upload, sanitize_input
from app.database import get_database
from app.config import settings
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/resume", tags=["Resume"])


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from a PDF file."""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages_text = []
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1.5, y_tolerance=3)
            if text:
                pages_text.append(text)
        return "\n\n".join(pages_text)


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
        raw_text = await run_blocking(_extract_pdf_text, content)
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
    runtime = get_runtime()
    try:
        async with runtime.ai_semaphore:
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
        "createdAt": datetime.now(timezone.utc),
        "updatedAt": datetime.now(timezone.utc),
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
                    "updatedAt": datetime.now(timezone.utc),
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


@router.delete("/generated/{resume_id}")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def delete_generated_resume(
    request: Request,
    resume_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Delete a generated (tailored) resume and its PDF from storage."""
    from app.services.storage_service import delete_pdf
    db = get_database()
    try:
        # Fetch the doc first to get pdfUrl before deleting
        doc = await db.generated_resumes.find_one(
            {"_id": ObjectId(resume_id), "userId": user_id}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    # Delete PDF from storage (Supabase or local) if it exists
    pdf_url = doc.get("pdfUrl")
    if pdf_url:
        await delete_pdf(pdf_url)

    # Delete the DB record
    await db.generated_resumes.delete_one(
        {"_id": ObjectId(resume_id), "userId": user_id}
    )

    logger.info("generated_resume_deleted", user_id=user_id, resume_id=resume_id)
    return {"message": "Tailored resume deleted."}


@router.post("/tailor")
@limiter.limit(settings.RATE_LIMIT_AI)
async def tailor(
    request: Request,
    body: GenerateResumeRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Stream resume tailoring progress as SSE events."""
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

    return StreamingResponse(
        _tailor_stream(
            base_resume["resumeData"],
            job_desc,
            raw_text_length,
            body.baseResumeId,
            user_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    """Format a single SSE event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _tailor_stream(
    resume_data: dict,
    job_desc: str,
    raw_text_length: int,
    base_resume_id: str,
    user_id: str,
):
    """Async generator: yields SSE events as each AI step completes."""
    try:
        total_chars = 0
        EXPECTED_CHARS = 7000
        runtime = get_runtime()

        async def proxy_stream(gen, stage_idx, base_pct, max_pct, result_container):
            nonlocal total_chars
            async for evt, data in gen:
                if evt == "chunk":
                    total_chars += data["chars"]
                    pct = min(max_pct, int((total_chars / EXPECTED_CHARS) * 100))
                    pct = max(base_pct, pct)
                    yield _sse("tailor_progress", {
                        "message": "Generating...",
                        "data": {"percent": pct, "stage": stage_idx, "baseResumeId": base_resume_id}
                    })
                elif evt == "result":
                    result_container.append(data)

        # ── Stage 1: Gap Analysis ──────────────────────────────────────
        yield _sse("tailor_progress", {
            "message": "Analyzing job description and identifying skill gaps...",
            "data": {"percent": 5, "stage": 1, "baseResumeId": base_resume_id},
        })

        async with runtime.ai_semaphore:
            align_res = []
            async for chunk in proxy_stream(analyze_alignment(resume_data, job_desc), 1, 5, 25, align_res):
                yield chunk
            alignment = align_res[0]

            yield _sse("tailor_progress", {
                "message": "Gap analysis complete — optimizing skills section...",
                "data": {
                    "percent": 25, "stage": 2,
                    "baseResumeId": base_resume_id,
                    "earlyAtsScore": alignment.get("atsScore", 0),
                    "matchedKeywords": alignment.get("matchedKeywords", []),
                    "missingKeywords": alignment.get("missingKeywords", []),
                },
            })

            # ── Stage 2: Skills Optimization ───────────────────────────────
            skills_res = []
            async for chunk in proxy_stream(optimize_skills(resume_data, job_desc, alignment), 2, 25, 45, skills_res):
                yield chunk
            optimized_skills = skills_res[0]

            yield _sse("tailor_progress", {
                "message": "Skills optimized — rewriting experience & projects...",
                "data": {"percent": 45, "stage": 3, "baseResumeId": base_resume_id},
            })

            # ── Stage 3: Experience & Projects Rewrite ─────────────────────
            exp_res = []
            async for chunk in proxy_stream(rewrite_experience(resume_data, job_desc, alignment, optimized_skills), 3, 45, 75, exp_res):
                yield chunk
            experience = exp_res[0]

            yield _sse("tailor_progress", {
                "message": "Final polish pass and scoring...",
                "data": {"percent": 75, "stage": 4, "baseResumeId": base_resume_id},
            })

            # ── Stage 4: Final Polish + Analytics ──────────────────────────
            assembled = {
                "personalInfo": resume_data.get("personalInfo", {}),
                "workExperience": experience.get("workExperience", []),
                "skills": optimized_skills,
                "projects": experience.get("projects", []),
                "education": resume_data.get("education", []),
            }

            polish_res = []
            async for chunk in proxy_stream(final_polish(resume_data, assembled, job_desc, alignment, raw_text_length), 4, 75, 95, polish_res):
                yield chunk
            tailored_data, analytics = polish_res[0]

            # ── Summary generation ─────────────────────────────────────────
            yield _sse("tailor_progress", {
                "message": "Generating dashboard summary...",
                "data": {"percent": 95, "stage": 5, "baseResumeId": base_resume_id},
            })

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
            "createdAt": datetime.now(timezone.utc),
        }
        result = await db.generated_resumes.insert_one(gen_doc)

        logger.info("resume_tailored", user_id=user_id, generated_id=str(result.inserted_id))

        yield _sse("tailor_complete", {
            "message": "Resume tailored! View it on your dashboard.",
            "data": {
                "resumeId": str(result.inserted_id),
                "baseResumeId": base_resume_id,
                "analytics": analytics,
            },
        })

    except Exception as e:
        logger.error("tailor_stream_failed", error=str(e))
        yield _sse("tailor_failed", {
            "message": "Tailoring failed. Please try again.",
            "data": {"error": str(e), "baseResumeId": base_resume_id},
        })

