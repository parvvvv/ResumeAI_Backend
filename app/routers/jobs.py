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
from app.routers.resume import _tailor_background
from bson import ObjectId
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


class TailorForJobRequest(BaseModel):
    job_id: str
    base_resume_id: str


@router.get("/recommendations")
@limiter.limit(settings.RATE_LIMIT_JOBS)
async def recommended_jobs(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    Get job recommendations based on the user's generated resume profiles.
    Fetches India-based jobs posted in the last 24 hours from JSearch API.
    """
    db = get_database()
    result = await get_recommendations(user_id, db)

    logger.info(
        "jobs_recommendations_served",
        user_id=user_id,
        profile=result.get("profile"),
        count=len(result.get("jobs", [])),
    )

    return result


@router.post("/tailor", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.RATE_LIMIT_AI)
async def tailor_for_job(
    request: Request,
    body: TailorForJobRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    """
    Tailor a resume for a specific job.
    Fetches the job description from cache or JSearch API and starts tailoring.
    """
    db = get_database()

    # 1. Verify base resume exists
    try:
        base_resume = await db.base_resumes.find_one(
            {"_id": ObjectId(body.base_resume_id), "userId": user_id}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not base_resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Base resume not found.")

    # 2. Get job description
    job_desc = await get_job_description(body.job_id, db)
    if not job_desc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job description not found. Please refresh job recommendations."
        )

    # 3. Sanitize and start background tailoring
    from app.security import sanitize_input
    job_desc = sanitize_input(job_desc)
    raw_text_length = base_resume.get("rawTextLength", 0)

    background_tasks.add_task(
        _tailor_background,
        base_resume["resumeData"],
        job_desc,
        raw_text_length,
        body.base_resume_id,
        user_id,
    )

    logger.info("job_tailor_started", user_id=user_id, job_id=body.job_id)

    return {
        "message": "Tailoring started. You'll be notified when ready.",
        "status": "processing",
        "job_id": body.job_id,
    }
