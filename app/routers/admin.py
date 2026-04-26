"""
Admin router: platform analytics and user activity summaries.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil

from fastapi import APIRouter, Depends, Query, Request

from app.config import settings
from app.database import get_database
from app.middleware.auth import get_current_admin_user
from app.middleware.rate_limit import limiter
from app.models.template import AdminTemplateActionRequest
from app.services.template_service import (
    admin_approve_template,
    admin_archive_template,
    admin_list_templates,
    admin_reject_template,
)

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value) -> datetime | None:
    """Normalize Mongo timestamps that may be stored as datetimes or ISO strings."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _day_key(value) -> str:
    coerced = _coerce_datetime(value)
    return coerced.date().isoformat() if coerced else ""


def _empty_series(days: int) -> list[dict]:
    today = _now().date()
    start = today - timedelta(days=days - 1)
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "count": 0}
        for i in range(days)
    ]


async def _count_recent(collection, field: str, days: int) -> int:
    since = _now() - timedelta(days=days)
    count = 0
    async for doc in collection.find({}, {field: 1}):
        value = _coerce_datetime(doc.get(field))
        if value and value >= since:
            count += 1
    return count


async def _pdf_status_counts(db) -> dict:
    statuses = {"ready": 0, "processing": 0, "failed": 0, "missing": 0}
    for collection in (db.base_resumes, db.generated_resumes):
        pipeline = [
            {
                "$group": {
                    "_id": {"$ifNull": ["$pdfStatus", "missing"]},
                    "count": {"$sum": 1},
                }
            }
        ]
        async for row in collection.aggregate(pipeline):
            status = row.get("_id") or "missing"
            if status not in statuses:
                status = "missing"
            statuses[status] += row.get("count", 0)
    return statuses


async def _average_ats(db, user_id: str | None = None) -> int | None:
    match = {"analytics.atsScore": {"$gt": 0}}
    if user_id:
        match["userId"] = user_id

    rows = await db.generated_resumes.aggregate([
        {"$match": match},
        {"$group": {"_id": None, "avg": {"$avg": "$analytics.atsScore"}}},
    ]).to_list(length=1)

    if not rows:
        return None
    return round(rows[0]["avg"])


@router.get("/overview")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_admin_overview(
    request: Request,
    admin: dict = Depends(get_current_admin_user),
):
    """Return platform-level counts and health metrics for admins."""
    db = get_database()
    del admin

    total_users = await db.users.count_documents({})
    total_base = await db.base_resumes.count_documents({})
    total_generated = await db.generated_resumes.count_documents({})
    total_jobs = await db.jobs.count_documents({})
    base_pdf_ready = await db.base_resumes.count_documents({"pdfStatus": "ready"})
    generated_pdf_ready = await db.generated_resumes.count_documents({"pdfStatus": "ready"})

    return {
        "totals": {
            "users": total_users,
            "originalResumes": total_base,
            "tailoredResumes": total_generated,
            "pdfsReady": base_pdf_ready + generated_pdf_ready,
            "jobCacheRecords": total_jobs,
        },
        "recent": {
            "users7d": await _count_recent(db.users, "createdAt", 7),
            "users30d": await _count_recent(db.users, "createdAt", 30),
            "resumes7d": await _count_recent(db.base_resumes, "createdAt", 7),
            "resumes30d": await _count_recent(db.base_resumes, "createdAt", 30),
            "tailored7d": await _count_recent(db.generated_resumes, "createdAt", 7),
            "tailored30d": await _count_recent(db.generated_resumes, "createdAt", 30),
        },
        "averageAtsScore": await _average_ats(db),
        "pdfStatusCounts": await _pdf_status_counts(db),
    }


@router.get("/users")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_admin_users(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = "",
    sort: str = Query("newest", pattern="^(newest|most_active|most_tailored)$"),
    admin: dict = Depends(get_current_admin_user),
):
    """Return paginated user activity rows without resume bodies."""
    db = get_database()
    del admin

    match = {}
    if search.strip():
        match["email"] = {"$regex": re.escape(search.strip()), "$options": "i"}

    users = await db.users.find(
        match,
        {"passwordHash": 0},
    ).to_list(length=1000)

    rows = []
    for user in users:
        user_id = str(user["_id"])
        base_docs = await db.base_resumes.find(
            {"userId": user_id},
            {"createdAt": 1, "pdfStatus": 1},
        ).to_list(length=1000)
        generated_docs = await db.generated_resumes.find(
            {"userId": user_id},
            {"createdAt": 1, "pdfStatus": 1, "analytics.atsScore": 1},
        ).to_list(length=1000)

        ats_scores = [
            doc.get("analytics", {}).get("atsScore")
            for doc in generated_docs
            if doc.get("analytics", {}).get("atsScore", 0) > 0
        ]
        last_base = max((_coerce_datetime(doc.get("createdAt")) for doc in base_docs if doc.get("createdAt")), default=None)
        last_generated = max((_coerce_datetime(doc.get("createdAt")) for doc in generated_docs if doc.get("createdAt")), default=None)
        created_at = _coerce_datetime(user.get("createdAt"))

        rows.append({
            "id": user_id,
            "email": user.get("email", ""),
            "role": user.get("role", "user"),
            "createdAt": created_at,
            "originalResumeCount": len(base_docs),
            "tailoredResumeCount": len(generated_docs),
            "pdfReadyCount": len([doc for doc in [*base_docs, *generated_docs] if doc.get("pdfStatus") == "ready"]),
            "lastResumeCreatedAt": last_base,
            "lastTailoredCreatedAt": last_generated,
            "averageAtsScore": round(sum(ats_scores) / len(ats_scores)) if ats_scores else None,
        })

    if sort == "most_active":
        rows.sort(key=lambda row: row["originalResumeCount"] + row["tailoredResumeCount"], reverse=True)
    elif sort == "most_tailored":
        rows.sort(key=lambda row: row["tailoredResumeCount"], reverse=True)
    else:
        rows.sort(key=lambda row: row.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    total = len(rows)
    start = (page - 1) * limit
    end = start + limit

    return {
        "users": rows[start:end],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": ceil(total / limit) if total else 0,
        },
    }


@router.get("/activity")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_admin_activity(
    request: Request,
    range: str = Query("30d", pattern="^(7d|30d|90d)$"),
    admin: dict = Depends(get_current_admin_user),
):
    """Return chart-ready daily activity series."""
    db = get_database()
    del admin

    days = int(range.removesuffix("d"))
    since = _now() - timedelta(days=days - 1)

    async def collect(collection, field: str) -> list[dict]:
        counts = defaultdict(int)
        cursor = collection.find({}, {field: 1})
        async for doc in cursor:
            value = _coerce_datetime(doc.get(field))
            if value and value >= since:
                counts[_day_key(value)] += 1

        series = _empty_series(days)
        for item in series:
            item["count"] = counts[item["date"]]
        return series

    pdf_counts = defaultdict(int)
    for collection in (db.base_resumes, db.generated_resumes):
        cursor = collection.find({}, {"pdfCompletedAt": 1})
        async for doc in cursor:
            value = _coerce_datetime(doc.get("pdfCompletedAt"))
            if value and value >= since:
                pdf_counts[_day_key(value)] += 1

    pdf_series = _empty_series(days)
    for item in pdf_series:
        item["count"] = pdf_counts[item["date"]]

    return {
        "range": range,
        "usersCreated": await collect(db.users, "createdAt"),
        "resumesUploaded": await collect(db.base_resumes, "createdAt"),
        "tailoredCreated": await collect(db.generated_resumes, "createdAt"),
        "pdfsCompleted": pdf_series,
    }


# ---------------------------------------------------------------------------
# Admin Template Governance (Phase 5)
# ---------------------------------------------------------------------------

@router.get("/templates")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def admin_list_templates_endpoint(
    request: Request,
    status_filter: str = Query(default=None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    admin: dict = Depends(get_current_admin_user),
):
    """List all templates with optional status filter (pending_review, published, archived, draft)."""
    del admin
    return await admin_list_templates(filter_status=status_filter, page=page, limit=limit)


@router.post("/templates/{template_id}/approve")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def admin_approve_endpoint(
    request: Request,
    template_id: str,
    admin: dict = Depends(get_current_admin_user),
):
    """Approve a template for the public catalog."""
    del admin
    return await admin_approve_template(template_id)


@router.post("/templates/{template_id}/reject")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def admin_reject_endpoint(
    request: Request,
    template_id: str,
    body: AdminTemplateActionRequest = AdminTemplateActionRequest(),
    admin: dict = Depends(get_current_admin_user),
):
    """Reject a template with optional reason."""
    del admin
    return await admin_reject_template(template_id, reason=body.reason)


@router.post("/templates/{template_id}/archive")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def admin_archive_endpoint(
    request: Request,
    template_id: str,
    admin: dict = Depends(get_current_admin_user),
):
    """Archive a template."""
    del admin
    return await admin_archive_template(template_id)
