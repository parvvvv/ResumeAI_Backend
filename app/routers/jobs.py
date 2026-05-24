"""
Jobs router: recommended jobs based on user's generated resumes.
"""

from fastapi import APIRouter, Depends, Request, HTTPException, status, BackgroundTasks
from pydantic import BaseModel
from app.middleware.auth import get_current_user_id
from app.middleware.rate_limit import limiter
from app.database import get_database
from app.config import settings
from app.services.job_service import get_recommendations, get_job_description
from app.routers.resume import _tailor_stream, _sse
from fastapi.responses import StreamingResponse
from bson import ObjectId
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


class TailorForJobRequest(BaseModel):
    job_id: str
    base_resume_id: str


from datetime import datetime, timezone
from fastapi import Query

@router.get("/recommendations")
@limiter.limit(settings.RATE_LIMIT_JOBS)
async def recommended_jobs(
    request: Request,
    bypass_cache: bool = Query(False),
    user_id: str = Depends(get_current_user_id),
):
    """
    Get job recommendations based on the user's generated resume profiles.
    Optionally bypasses the 24-hour cache (limited daily quota for non-admins).
    """
    db = get_database()

    # Get user to validate quota limits & date reset
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    role = user.get("role", "user")
    current_date_str = datetime.now(timezone.utc).date().isoformat()
    last_bypass_date = user.get("lastBypassDate", "")
    bypass_attempts = user.get("bypassAttemptsLeft", 3)

    # Perform daily calendar reset check
    if current_date_str != last_bypass_date and role != "admin":
        bypass_attempts = 3
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"bypassAttemptsLeft": 3, "lastBypassDate": current_date_str}}
        )

    # Enforce quota check if they are trying to bypass cache
    if bypass_cache and role != "admin":
        if bypass_attempts <= 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You have exhausted your daily cache bypass attempts (3 refreshes per day max). Please try again tomorrow.",
            )

    # Call recommendations service
    result = await get_recommendations(user_id, db, bypass_cache=bypass_cache)

    # Handle quota consumption (skip if it was duplicate or cache hit)
    is_duplicate = result.get("is_duplicate", False)
    if bypass_cache and role != "admin":
        if is_duplicate:
            # Duplicate, do not decrement. Detail this in message to user
            result["message"] = "No new jobs found. Refresh attempt was not consumed."
        else:
            # Decrement bypass attempt
            bypass_attempts -= 1
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"bypassAttemptsLeft": bypass_attempts, "lastBypassDate": current_date_str}}
            )
            result["message"] = "Refresh successful."

    # Return updated attempts
    result["bypassAttemptsLeft"] = "unlimited" if role == "admin" else bypass_attempts

    logger.info(
        "jobs_recommendations_served",
        user_id=user_id,
        profile=result.get("profile"),
        count=len(result.get("jobs", [])),
        bypass_cache=bypass_cache,
        is_duplicate=is_duplicate,
    )

    return result


async def _tailor_from_job_id_stream(job_id: str, base_resume_id: str, user_id: str):
    """
    Streaming flow: fetch job description, then yield progress from AI tailor.
    """
    db = get_database()
    try:
        # yield some initial progress
        yield _sse("tailor_progress", {
            "message": "Fetching job description...",
            "data": {"percent": 2, "stage": 0, "baseResumeId": base_resume_id},
        })

        # 1. Get job description
        job_desc = await get_job_description(job_id, db)
        if not job_desc:
            yield _sse("tailor_failed", {
                "message": "We couldn't fetch the job details. Please try another job.",
                "data": {"job_id": job_id, "baseResumeId": base_resume_id},
            })
            return

        # 2. Get base resume
        base_resume = await db.base_resumes.find_one(
            {"_id": ObjectId(base_resume_id), "userId": user_id}
        )
        if not base_resume:
            yield _sse("tailor_failed", {
                "message": "Base resume not found.",
                "data": {"baseResumeId": base_resume_id},
            })
            return

        # 3. Sanitize and proceed with existing tailoring logic
        from app.security import sanitize_input
        job_desc = sanitize_input(job_desc)
        raw_text_length = base_resume.get("rawTextLength", 0)

        # Delegate to _tailor_stream from resume router
        async for chunk in _tailor_stream(
            base_resume["resumeData"],
            job_desc,
            raw_text_length,
            base_resume_id,
            user_id,
        ):
            yield chunk

    except Exception as e:
        logger.error("tailor_from_id_stream_failed", error=str(e))
        yield _sse("tailor_failed", {
            "message": "Tailoring failed due to an internal error.",
            "data": {"error": str(e), "baseResumeId": base_resume_id},
        })


@router.post("/tailor")
@limiter.limit(settings.RATE_LIMIT_AI)
async def tailor_for_job(
    request: Request,
    body: TailorForJobRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Tailor a resume for a specific job.
    Streams progress as SSE.
    """
    db = get_database()

    # Verify base resume exists before accepting
    try:
        base_exists = await db.base_resumes.find_one(
            {"_id": ObjectId(body.base_resume_id), "userId": user_id},
            {"_id": 1}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not base_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Base resume not found.")

    logger.info("job_tailor_started", user_id=user_id, job_id=body.job_id)

    return StreamingResponse(
        _tailor_from_job_id_stream(body.job_id, body.base_resume_id, user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
