"""
Jobs router: recommended jobs based on user's generated resumes.
"""

from fastapi import APIRouter, Depends, Request
from app.middleware.auth import get_current_user_id
from app.middleware.rate_limit import limiter
from app.database import get_database
from app.config import settings
from app.services.job_service import get_recommendations
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


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
