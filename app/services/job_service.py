"""
Job recommendation service.
Fetches jobs from JSearch API based on user's generated resume profiles.
Supports fallback API key rotation and intelligent profile classification.
"""

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()

# ─── Profile classification keywords ───

FRESHER_KEYWORDS = {
    "intern", "fresher", "trainee", "entry-level", "entry level",
    "graduate", "apprentice",
}

TECH_KEYWORDS = {
    "developer", "engineer", "software", "devops", "backend", "frontend",
    "fullstack", "full-stack", "full stack", "data", "ml", "ai", "cloud",
    "sde", "swe", "python", "java", "react", "node", "web developer",
    "mobile", "ios", "android", "qa", "test", "automation", "embedded",
    "cybersecurity", "security", "blockchain", "infrastructure",
}

NON_TECH_KEYWORDS = {
    "hr", "human resource", "finance", "marketing", "sales", "business",
    "accounting", "management", "operations", "recruitment", "admin",
    "analyst", "content", "design", "graphic", "communication",
    "consulting", "legal", "supply chain", "logistics",
}

EXPERIENCE_KEYWORDS = {
    "senior", "junior", "lead", "principal", "staff", "architect", "manager",
    "director", "vp", "head",
}


def classify_user_profile(summaries: list[str]) -> str:
    """
    Analyze generated resume summaries to determine user profile type.

    Returns one of: 'fresher-tech', 'experienced-tech', 'non-tech'
    """
    combined = " ".join(summaries).lower()

    has_fresher = any(kw in combined for kw in FRESHER_KEYWORDS)
    has_tech = any(kw in combined for kw in TECH_KEYWORDS)
    has_non_tech = any(kw in combined for kw in NON_TECH_KEYWORDS)
    has_experience = any(kw in combined for kw in EXPERIENCE_KEYWORDS)

    # Priority-based classification
    if has_fresher and has_tech:
        return "fresher-tech"
    if has_fresher and has_non_tech:
        return "non-tech"
    if has_experience and has_tech:
        return "experienced-tech"
    if has_non_tech:
        return "non-tech"
    if has_tech:
        return "experienced-tech"

    # Default fallback
    return "fresher-tech"


def _extract_role_keywords(summaries: list[str], profile: str, max_keywords: int = 1) -> list[str]:
    """Extract role keywords filtered by the classified profile to avoid contradictions."""
    combined = " ".join(summaries).lower()
    found = []

    # Select keyword pool based on profile
    if profile in ("fresher-tech", "experienced-tech"):
        pool = [
            "devops", "backend", "frontend", "full stack",
            "data scientist", "ml engineer", "cloud",
            "software engineer", "web developer",
            "react", "python", "java",
        ]
    else:  # non-tech
        pool = [
            "hr", "finance", "marketing", "sales",
            "content writer", "graphic designer", "recruitment",
        ]

    for role in pool:
        if role in combined and role not in found:
            found.append(role)
            if len(found) >= max_keywords:
                break

    return found


def build_search_query(profile: str, summaries: list[str]) -> str:
    """Build a concise JSearch query — shorter queries get better results."""
    role_keywords = _extract_role_keywords(summaries, profile)
    roles_str = " ".join(role_keywords) if role_keywords else ""

    if profile == "fresher-tech":
        if roles_str:
            return f"{roles_str} intern jobs india"
        return "software intern developer jobs india"

    elif profile == "experienced-tech":
        if roles_str:
            return f"{roles_str} jobs india"
        return "software engineer jobs india"

    elif profile == "non-tech":
        if roles_str:
            return f"{roles_str} intern jobs india"
        return "hr finance intern jobs india"

    return "developer jobs india"


async def fetch_jobs(
    query: str,
    api_keys: list[str],
    num_pages: int = 1,
) -> list[dict]:
    """
    Fetch jobs from JSearch API with fallback key rotation
    and progressive date fallback (today → 3days → week).
    """
    if not api_keys:
        logger.warning("jsearch_no_api_keys", message="No JSearch API keys configured")
        return []

    url = f"https://{settings.JSEARCH_HOST}/search"

    # Try progressively wider date ranges
    date_filters = ["today", "3days", "week"]

    async with httpx.AsyncClient(timeout=15.0) as client:
        for date_posted in date_filters:
            params = {
                "query": query,
                "page": "1",
                "num_pages": str(num_pages),
                "country": "in",
                "date_posted": date_posted,
            }

            for i, key in enumerate(api_keys):
                try:
                    headers = {
                        "Content-Type": "application/json",
                        "x-rapidapi-host": settings.JSEARCH_HOST,
                        "x-rapidapi-key": key,
                    }

                    logger.info("jsearch_request", key_index=i, query=query[:60], date_posted=date_posted)
                    response = await client.get(url, params=params, headers=headers)

                    if response.status_code in (429, 403):
                        logger.warning("jsearch_key_exhausted", key_index=i, status=response.status_code)
                        continue  # Try next key

                    response.raise_for_status()
                    data = response.json()
                    jobs = data.get("data", [])

                    if jobs:
                        logger.info("jsearch_success", key_index=i, job_count=len(jobs), date_posted=date_posted)
                        return jobs

                    # No jobs with this date range, try wider
                    logger.info("jsearch_empty", key_index=i, date_posted=date_posted)
                    break  # Don't try other keys, try wider date range

                except httpx.HTTPStatusError as e:
                    logger.error("jsearch_http_error", key_index=i, error=str(e))
                    continue
                except httpx.RequestError as e:
                    logger.error("jsearch_request_error", key_index=i, error=str(e))
                    continue

    logger.error("jsearch_all_attempts_failed", total_keys=len(api_keys))
    return []


def _format_job(job: dict) -> dict:
    """Transform raw JSearch job data into a clean frontend-friendly object."""
    return {
        "job_id": job.get("job_id", ""),
        "title": job.get("job_title", "Untitled"),
        "company": job.get("employer_name", "Unknown Company"),
        "company_logo": job.get("employer_logo"),
        "location": _build_location(job),
        "employment_type": job.get("job_employment_type", ""),
        "posted_at": job.get("job_posted_at_datetime_utc", ""),
        "posted_at_label": job.get("job_posted_at", ""),
        "apply_link": job.get("job_apply_link", ""),
        "is_remote": job.get("job_is_remote", False),
        "description_snippet": (job.get("job_description", "")[:200] + "...") if job.get("job_description") else "",
        "salary_min": job.get("job_min_salary"),
        "salary_max": job.get("job_max_salary"),
        "salary_period": job.get("job_salary_period"),
        "highlights": job.get("job_highlights", {}),
    }


def _build_location(job: dict) -> str:
    """Build a readable location string from job data."""
    parts = []
    if job.get("job_city"):
        parts.append(job["job_city"])
    if job.get("job_state"):
        parts.append(job["job_state"])
    if not parts and job.get("job_country"):
        parts.append(job["job_country"])
    return ", ".join(parts) if parts else "Remote"


async def get_recommendations(user_id: str, db) -> dict:
    """
    Main orchestrator: reads user's generated resumes from DB,
    classifies profile, builds query, fetches and formats jobs.
    """
    # 1. Fetch all generated resumes for the user
    generated = await db.generated_resumes.find(
        {"userId": user_id},
        {"summary": 1, "jobDescription": 1, "_id": 0},
    ).sort("createdAt", -1).to_list(length=50)

    if not generated:
        return {"jobs": [], "profile": None, "query_used": ""}

    # 2. Collect summaries (fallback to jobDescription snippet)
    summaries = []
    for doc in generated:
        if doc.get("summary"):
            summaries.append(doc["summary"])
        elif doc.get("jobDescription"):
            summaries.append(doc["jobDescription"][:200])

    if not summaries:
        return {"jobs": [], "profile": None, "query_used": ""}

    # 3. Classify profile
    profile = classify_user_profile(summaries)

    # 4. Build search query
    query = build_search_query(profile, summaries)

    # 5. Fetch jobs from JSearch API
    raw_jobs = await fetch_jobs(query, settings.JSEARCH_API_KEYS)

    # 6. Format for frontend
    jobs = [_format_job(j) for j in raw_jobs]

    logger.info(
        "job_recommendations_ready",
        user_id=user_id,
        profile=profile,
        query=query,
        job_count=len(jobs),
    )

    return {
        "jobs": jobs,
        "profile": profile,
        "query_used": query,
    }
