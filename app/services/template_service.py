"""
Services for DB-backed resume templates.
Includes: CRUD, sharing, sessions, favorites, public catalog, admin governance.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
import secrets

from fastapi import HTTPException, status
from jinja2 import BaseLoader, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from app.database import get_database
from app.models.resume import ResumeData
from app.models.template import (
    TemplateCreateRequest,
    TemplatePreviewPayload,
    TemplateResolverResult,
    TemplateResponse,
    TemplateSchema,
    TemplateUpdateRequest,
    TemplateSessionResponse,
)
from app.security import validate_jinja_safety
import structlog

logger = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_SYSTEM_TEMPLATE_META: dict[str, dict[str, Any]] = {
    "modern-system": {
        "templateKey": "modern-system",
        "title": "Modern",
        "description": "Clean serif resume for general professional use.",
        "filename": "modern.html",
        "tags": ["developer", "minimal"],
    },
    "ats-system": {
        "templateKey": "ats-system",
        "title": "ATS-Friendly",
        "description": "Minimal structure optimized for clean parsing.",
        "filename": "ats.html",
        "tags": ["ats", "minimal"],
    },
}

_DEFAULT_PREVIEW_RESUME = ResumeData.model_validate(
    {
        "personalInfo": {
            "fullName": "Avery Patel",
            "phone": "+1 (555) 010-2244",
            "email": "avery.patel@example.com",
            "linkedin": "linkedin.com/in/averypatel",
            "github": "github.com/averypatel",
        },
        "workExperience": [
            {
                "company": "Northstar Labs",
                "role": "Senior Software Engineer",
                "location": "San Francisco, CA",
                "startDate": "2022",
                "endDate": "Present",
                "points": [
                    "Led resume generation platform improvements that cut PDF generation latency by 32%.",
                    "Built admin tooling for template governance and safe rollout controls.",
                    "Improved analytics quality across personalized resume workflows.",
                ],
                "techStack": ["React", "FastAPI", "MongoDB", "Playwright"],
            }
        ],
        "skills": [
            {"name": "Languages", "items": ["JavaScript", "Python", "TypeScript"]},
            {"name": "Frameworks", "items": ["React", "FastAPI", "Node.js"]},
        ],
        "projects": [
            {
                "name": "Template Platform",
                "description": "DB-backed resume template management platform",
                "points": [
                    "Created manual template editor with live preview.",
                    "Added access controls and draft-based publishing workflow.",
                ],
                "techStack": ["Jinja", "MongoDB", "React"],
            }
        ],
        "education": [
            {
                "institution": "University of Illinois",
                "degree": "B.S.",
                "field": "Computer Science",
                "startYear": "2016",
                "endYear": "2020",
                "score": "3.8 GPA",
            }
        ],
    }
).model_dump()

_DEFAULT_TEMPLATE_SCHEMA = TemplateSchema(
    sections=["personalInfo", "workExperience", "skills", "projects", "education"],
    fields=[],
)


def _template_env(strict: bool = False) -> SandboxedEnvironment:
    """Use SandboxedEnvironment to prevent SSTI attacks."""
    options: dict[str, Any] = dict(
        loader=BaseLoader(),
        autoescape=False,
    )
    if strict:
        options["undefined"] = StrictUndefined
    return SandboxedEnvironment(**options)


def _serialize_template(doc: dict[str, Any]) -> TemplateResponse:
    payload = dict(doc)
    payload["id"] = str(payload.pop("_id"))
    return TemplateResponse.model_validate(payload)


def _template_accessible_query(user_id: str, filter_value: Optional[str]) -> dict[str, Any]:
    if filter_value == "system":
        return {"sourceType": "system", "status": "published"}
    if filter_value == "mine":
        return {"ownerUserId": user_id}
    if filter_value == "shared":
        return {"sharedWithUserIds": user_id}
    if filter_value == "public":
        return {"visibility": "public", "status": "published"}
    return {
        "$or": [
            {"ownerUserId": user_id},
            {"sharedWithUserIds": user_id},
            {"sourceType": "system", "status": "published"},
            {"visibility": "public", "status": "published"},
        ]
    }


async def seed_system_templates() -> None:
    db = get_database()
    now = datetime.now(timezone.utc)

    for template_id, meta in _SYSTEM_TEMPLATE_META.items():
        html_content = (_TEMPLATES_DIR / meta["filename"]).read_text()
        doc = {
            "_id": template_id,
            "templateKey": meta["templateKey"],
            "title": meta["title"],
            "description": meta["description"],
            "sourceType": "system",
            "visibility": "public",
            "status": "published",
            "isEditable": False,
            "htmlContent": html_content,
            "templateSchema": _DEFAULT_TEMPLATE_SCHEMA.model_dump(),
            "previewSeedData": {"resume": _DEFAULT_PREVIEW_RESUME, "extras": {}},
            "tags": meta["tags"],
            "usageCount": 0,
            "favoriteCount": 0,
            "viewCount": 0,
            "downloadCount": 0,
            "version": 1,
            "ownerUserId": None,
            "ownerEmail": None,
            "sharedWithUserIds": [],
            "shareTokens": [],
            "createdAt": now,
            "updatedAt": now,
        }
        await db.templates.update_one(
            {"_id": template_id},
            {"$setOnInsert": doc},
            upsert=True,
        )


class PermissiveDict(dict):
    """A dict subclass that supports dot-notation access and returns
    empty PermissiveDict for any missing key — so chains like
    resume.personalInfo.fullName never crash, they just render as ''.

    IMPORTANT: Uses __getattribute__ (not __getattr__) so that data keys
    like 'items' take priority over built-in dict methods like dict.items().
    """

    def __getattribute__(self, key):
        # Dunder/private attributes use normal resolution
        if key.startswith('_'):
            return super().__getattribute__(key)
        # Data keys take priority over dict methods
        if dict.__contains__(self, key):
            return make_permissive(dict.__getitem__(self, key))
        # Fall back to dict methods (keys, values, get, etc.)
        try:
            return super().__getattribute__(key)
        except AttributeError:
            return PermissiveDict()

    def __str__(self):
        # When Jinja renders {{ resume.personalInfo.fullName }} and it's
        # an empty PermissiveDict, we want it to render as "" not "{}".
        return "" if not self else super().__str__()

    def __repr__(self):
        return super().__repr__()

    def __bool__(self):
        return len(self) > 0

    def __iter__(self):
        return super().__iter__()

    def __html__(self):
        """Jinja2 calls __html__() for safe rendering."""
        return self.__str__()


def make_permissive(obj):
    """Recursively wrap dicts/lists so nested attribute access never crashes."""
    if isinstance(obj, PermissiveDict):
        return obj
    if isinstance(obj, dict):
        return PermissiveDict({k: make_permissive(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [make_permissive(item) for item in obj]
    return obj


def render_template_preview(html_content: str, preview_data: TemplatePreviewPayload) -> tuple[str, list[str]]:
    from app.services.template_gen_service import _DEFAULT_PREVIEW_SEED

    warnings: list[str] = []

    # Use default seed data as fallback when the provided data is empty
    resume_data = preview_data.resume if preview_data.resume else _DEFAULT_PREVIEW_SEED["resume"]
    extras_data = preview_data.extras if preview_data.extras else _DEFAULT_PREVIEW_SEED["extras"]

    try:
        # Use lenient mode for preview — missing fields render as empty strings
        # rather than crashing the entire preview.
        template = _template_env(strict=False).from_string(html_content)
        html = template.render(
            resume=make_permissive(resume_data),
            extras=make_permissive(extras_data),
            templateMeta=PermissiveDict(),
        )
        return html, warnings
    except TemplateError as exc:
        warnings.append(str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Template preview failed.", "warnings": warnings},
        )



async def list_templates(user_id: str, filter_value: Optional[str]) -> list[TemplateResponse]:
    db = get_database()
    query = _template_accessible_query(user_id, filter_value)
    docs = await db.templates.find(query).sort("createdAt", -1).to_list(length=100)
    return [_serialize_template(doc) for doc in docs]


async def list_public_templates(page: int = 1, limit: int = 20) -> dict[str, Any]:
    """Browse the public template catalog (no auth required for listing)."""
    db = get_database()
    query = {"visibility": "public", "status": "published"}
    total = await db.templates.count_documents(query)
    skip = (page - 1) * limit
    docs = await db.templates.find(query).sort("usageCount", -1).skip(skip).limit(limit).to_list(length=limit)
    return {
        "templates": [_serialize_template(doc) for doc in docs],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit if total else 0,
        },
    }


async def get_template_by_id(template_id: str, user_id: str, increment_view: bool = True) -> TemplateResponse:
    db = get_database()
    query = {
        "_id": template_id,
        **_template_accessible_query(user_id, None),
    }
    doc = await db.templates.find_one(query)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    if increment_view:
        await db.templates.update_one({"_id": template_id}, {"$inc": {"viewCount": 1}})
        doc["viewCount"] = int(doc.get("viewCount", 0)) + 1

    return _serialize_template(doc)


async def create_template(body: TemplateCreateRequest, user_id: str, owner_email: str) -> TemplateResponse:
    db = get_database()

    # Validate Jinja safety before saving
    is_safe, violations = validate_jinja_safety(body.htmlContent)
    if not is_safe:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Template contains unsafe Jinja expressions.", "violations": violations},
        )

    now = datetime.now(timezone.utc)
    template_id = uuid4().hex
    doc = {
        "_id": template_id,
        "ownerUserId": user_id,
        "ownerEmail": owner_email,
        "title": body.title,
        "description": body.description,
        "htmlContent": body.htmlContent,
        "templateSchema": body.templateSchema.model_dump(),
        "previewSeedData": body.previewSeedData.model_dump(),
        "tags": body.tags,
        "visibility": body.visibility,
        "sourceType": "manual",
        "status": "draft",
        "isEditable": True,
        "templateKey": f"user_{template_id}",
        "usageCount": 0,
        "favoriteCount": 0,
        "viewCount": 0,
        "downloadCount": 0,
        "basedOnTemplateId": None,
        "version": 1,
        "sharedWithUserIds": [],
        "shareTokens": [],
        "createdAt": now,
        "updatedAt": now,
    }
    await db.templates.insert_one(doc)
    return _serialize_template(doc)


async def update_template(template_id: str, body: TemplateUpdateRequest, user_id: str) -> TemplateResponse:
    db = get_database()
    existing = await db.templates.find_one({"_id": template_id, "ownerUserId": user_id, "isEditable": True})
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Editable template not found.")

    # Validate Jinja safety
    is_safe, violations = validate_jinja_safety(body.htmlContent)
    if not is_safe:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Template contains unsafe Jinja expressions.", "violations": violations},
        )

    update_doc = {
        "title": body.title,
        "description": body.description,
        "htmlContent": body.htmlContent,
        "templateSchema": body.templateSchema.model_dump(),
        "previewSeedData": body.previewSeedData.model_dump(),
        "tags": body.tags,
        "visibility": body.visibility,
        "status": body.status,
        "updatedAt": datetime.now(timezone.utc),
    }
    await db.templates.update_one({"_id": template_id}, {"$set": update_doc})
    refreshed = await db.templates.find_one({"_id": template_id})
    return _serialize_template(refreshed)


async def delete_template(template_id: str, user_id: str) -> None:
    db = get_database()
    result = await db.templates.delete_one({"_id": template_id, "ownerUserId": user_id, "isEditable": True})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Editable template not found.")


async def duplicate_template(template_id: str, user_id: str, owner_email: str) -> TemplateResponse:
    source = await get_template_by_id(template_id, user_id, increment_view=False)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    body = TemplateCreateRequest(
        title=f"{source.title} Copy",
        description=source.description,
        htmlContent=source.htmlContent,
        templateSchema=source.templateSchema,
        previewSeedData=source.previewSeedData,
        tags=source.tags,
        visibility="private",
    )
    duplicated = await create_template(body, user_id, owner_email)
    db = get_database()
    await db.templates.update_one(
        {"_id": duplicated.id},
        {
            "$set": {
                "basedOnTemplateId": source.id,
                "version": 1,
            }
        },
    )
    refreshed = await db.templates.find_one({"_id": duplicated.id})
    return _serialize_template(refreshed)


async def resolve_template(template_id: Optional[str] = None, template_name: Optional[str] = None) -> TemplateResolverResult:
    db = get_database()

    if template_id:
        doc = await db.templates.find_one({"_id": template_id})
        if not doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
        return TemplateResolverResult(
            id=str(doc["_id"]),
            templateKey=doc.get("templateKey") or str(doc["_id"]),
            title=doc.get("title", "Template"),
            htmlContent=doc["htmlContent"],
            source="db_template",
            isEditable=bool(doc.get("isEditable", False)),
            sourceType=doc.get("sourceType", "manual"),
        )

    template_map = {
        "modern": "modern-system",
        "ats": "ats-system",
    }
    mapped_id = template_map.get(template_name or "modern", "modern-system")
    doc = await db.templates.find_one({"_id": mapped_id})
    if doc:
        return TemplateResolverResult(
            id=str(doc["_id"]),
            templateKey=doc.get("templateKey") or mapped_id,
            title=doc.get("title", mapped_id),
            htmlContent=doc["htmlContent"],
            source="legacy_name",
            isEditable=bool(doc.get("isEditable", False)),
            sourceType=doc.get("sourceType", "system"),
        )

    html_path = _TEMPLATES_DIR / f"{template_name or 'modern'}.html"
    if not html_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    return TemplateResolverResult(
        templateKey=template_name or "modern",
        title=(template_name or "modern").title(),
        htmlContent=html_path.read_text(),
        source="legacy_name",
        isEditable=False,
        sourceType="system",
    )


async def increment_template_usage(template_id: Optional[str]) -> None:
    if not template_id:
        return
    db = get_database()
    await db.templates.update_one({"_id": template_id}, {"$inc": {"usageCount": 1}})


async def increment_template_downloads(template_id: Optional[str]) -> None:
    if not template_id:
        return
    db = get_database()
    await db.templates.update_one({"_id": template_id}, {"$inc": {"downloadCount": 1}})


def merge_template_preview_data(preview_seed_data: Optional[dict[str, Any]]) -> TemplatePreviewPayload:
    seed = deepcopy(preview_seed_data or {})
    seed.setdefault("resume", _DEFAULT_PREVIEW_RESUME)
    seed.setdefault("extras", {})
    return TemplatePreviewPayload.model_validate(seed)


# ---------------------------------------------------------------------------
# Phase 5: Sharing
# ---------------------------------------------------------------------------

async def share_template(
    template_id: str,
    user_id: str,
    emails: list[str] | None = None,
    user_ids: list[str] | None = None,
    generate_token: bool = False,
) -> dict[str, Any]:
    """Share a template with specific users or generate a share token."""
    db = get_database()
    doc = await db.templates.find_one({"_id": template_id, "ownerUserId": user_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found or not owned by you.")

    update: dict[str, Any] = {"updatedAt": datetime.now(timezone.utc)}
    add_to_set: dict[str, Any] = {}

    # Resolve emails to user IDs
    resolved_ids: list[str] = list(user_ids or [])
    if emails:
        for email in emails:
            user_doc = await db.users.find_one({"email": email.strip().lower()})
            if user_doc:
                resolved_ids.append(str(user_doc["_id"]))

    if resolved_ids:
        add_to_set["sharedWithUserIds"] = {"$each": resolved_ids}

    token = None
    if generate_token:
        token = secrets.token_urlsafe(32)
        add_to_set["shareTokens"] = token

    ops: dict[str, Any] = {"$set": update}
    if add_to_set:
        ops["$addToSet"] = add_to_set

    # Set visibility to selective if currently private
    if doc.get("visibility") == "private":
        update["visibility"] = "selective"

    await db.templates.update_one({"_id": template_id}, ops)

    refreshed = await db.templates.find_one({"_id": template_id})
    return {
        "sharedWithUserIds": refreshed.get("sharedWithUserIds", []),
        "shareTokens": refreshed.get("shareTokens", []),
        "newToken": token,
        "message": "Template shared successfully.",
    }


async def accept_share_token(token: str, user_id: str) -> dict[str, Any]:
    """Accept a share token and gain access to the template."""
    db = get_database()
    doc = await db.templates.find_one({"shareTokens": token})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid or expired share token.")

    await db.templates.update_one(
        {"_id": doc["_id"]},
        {"$addToSet": {"sharedWithUserIds": user_id}},
    )

    return {"templateId": str(doc["_id"]), "title": doc.get("title", ""), "message": "Access granted."}


async def request_publish(template_id: str, user_id: str) -> dict[str, str]:
    """User requests their template be reviewed for the public catalog."""
    db = get_database()
    doc = await db.templates.find_one({"_id": template_id, "ownerUserId": user_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    if doc.get("status") == "published":
        return {"message": "Template is already published."}

    await db.templates.update_one(
        {"_id": template_id},
        {"$set": {
            "status": "pending_review",
            "visibility": "public",
            "updatedAt": datetime.now(timezone.utc),
        }},
    )
    return {"message": "Template submitted for review. An admin will review it shortly."}


# ---------------------------------------------------------------------------
# Phase 5: Admin governance
# ---------------------------------------------------------------------------

async def admin_approve_template(template_id: str) -> dict[str, str]:
    """Admin approves a template for the public catalog."""
    db = get_database()
    doc = await db.templates.find_one({"_id": template_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    await db.templates.update_one(
        {"_id": template_id},
        {"$set": {
            "status": "published",
            "visibility": "public",
            "updatedAt": datetime.now(timezone.utc),
        }},
    )
    return {"message": "Template approved and published."}


async def admin_reject_template(template_id: str, reason: str = "") -> dict[str, str]:
    """Admin rejects a template."""
    db = get_database()
    doc = await db.templates.find_one({"_id": template_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    await db.templates.update_one(
        {"_id": template_id},
        {"$set": {
            "status": "draft",
            "visibility": "private",
            "updatedAt": datetime.now(timezone.utc),
        }},
    )
    return {"message": f"Template rejected.{(' Reason: ' + reason) if reason else ''}"}


async def admin_archive_template(template_id: str) -> dict[str, str]:
    """Admin archives a template."""
    db = get_database()
    doc = await db.templates.find_one({"_id": template_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    await db.templates.update_one(
        {"_id": template_id},
        {"$set": {
            "status": "archived",
            "updatedAt": datetime.now(timezone.utc),
        }},
    )
    return {"message": "Template archived."}


async def admin_list_templates(
    filter_status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    """Admin view of all templates with optional status filter."""
    db = get_database()
    query: dict[str, Any] = {}
    if filter_status:
        query["status"] = filter_status

    total = await db.templates.count_documents(query)
    skip = (page - 1) * limit
    docs = await db.templates.find(query).sort("updatedAt", -1).skip(skip).limit(limit).to_list(length=limit)

    return {
        "templates": [_serialize_template(doc) for doc in docs],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit if total else 0,
        },
    }


# ---------------------------------------------------------------------------
# Phase 7: Template Sessions (Dynamic Missing Data Forms)
# ---------------------------------------------------------------------------

async def get_or_create_session(
    user_id: str,
    resume_id: str,
    template_id: str,
) -> TemplateSessionResponse:
    """Get or create a template session for a user+resume+template combo."""
    db = get_database()
    now = datetime.now(timezone.utc)

    existing = await db.resume_template_sessions.find_one({
        "userId": user_id,
        "resumeId": resume_id,
        "templateId": template_id,
    })

    if existing:
        return TemplateSessionResponse(
            id=str(existing["_id"]),
            userId=existing["userId"],
            resumeId=existing["resumeId"],
            templateId=existing["templateId"],
            extras=existing.get("extras", {}),
            createdAt=existing.get("createdAt"),
            updatedAt=existing.get("updatedAt"),
        )

    session_id = uuid4().hex
    doc = {
        "_id": session_id,
        "userId": user_id,
        "resumeId": resume_id,
        "templateId": template_id,
        "extras": {},
        "createdAt": now,
        "updatedAt": now,
    }
    await db.resume_template_sessions.insert_one(doc)

    return TemplateSessionResponse(
        id=session_id,
        userId=user_id,
        resumeId=resume_id,
        templateId=template_id,
        extras={},
        createdAt=now,
        updatedAt=now,
    )


async def update_session(
    user_id: str,
    resume_id: str,
    template_id: str,
    extras: dict[str, Any],
) -> TemplateSessionResponse:
    """Update the extras data for a template session."""
    db = get_database()
    now = datetime.now(timezone.utc)

    result = await db.resume_template_sessions.update_one(
        {
            "userId": user_id,
            "resumeId": resume_id,
            "templateId": template_id,
        },
        {
            "$set": {"extras": extras, "updatedAt": now},
            "$setOnInsert": {"createdAt": now, "_id": uuid4().hex},
        },
        upsert=True,
    )

    doc = await db.resume_template_sessions.find_one({
        "userId": user_id,
        "resumeId": resume_id,
        "templateId": template_id,
    })

    return TemplateSessionResponse(
        id=str(doc["_id"]),
        userId=doc["userId"],
        resumeId=doc["resumeId"],
        templateId=doc["templateId"],
        extras=doc.get("extras", {}),
        createdAt=doc.get("createdAt"),
        updatedAt=doc.get("updatedAt"),
    )


# ---------------------------------------------------------------------------
# Phase 8: Favorites
# ---------------------------------------------------------------------------

async def toggle_favorite(template_id: str, user_id: str) -> dict[str, Any]:
    """Toggle a template as favorited by the user."""
    db = get_database()

    # Check template exists and is accessible
    doc = await db.templates.find_one({"_id": template_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    # Check if user already favorited
    fav = await db.template_favorites.find_one({"templateId": template_id, "userId": user_id})

    if fav:
        # Un-favorite
        await db.template_favorites.delete_one({"_id": fav["_id"]})
        await db.templates.update_one({"_id": template_id}, {"$inc": {"favoriteCount": -1}})
        return {"favorited": False, "message": "Removed from favorites."}
    else:
        # Favorite
        await db.template_favorites.insert_one({
            "_id": uuid4().hex,
            "templateId": template_id,
            "userId": user_id,
            "createdAt": datetime.now(timezone.utc),
        })
        await db.templates.update_one({"_id": template_id}, {"$inc": {"favoriteCount": 1}})
        return {"favorited": True, "message": "Added to favorites."}


# ---------------------------------------------------------------------------
# Phase 8: Template Analytics
# ---------------------------------------------------------------------------

async def get_template_analytics(template_id: str) -> dict[str, Any]:
    """Get analytics for a specific template."""
    db = get_database()
    doc = await db.templates.find_one({"_id": template_id})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    usage = doc.get("usageCount", 0)
    views = doc.get("viewCount", 0)
    conversion_rate = round((usage / views * 100), 1) if views > 0 else 0.0

    return {
        "templateId": str(doc["_id"]),
        "title": doc.get("title", ""),
        "usageCount": usage,
        "downloadCount": doc.get("downloadCount", 0),
        "favoriteCount": doc.get("favoriteCount", 0),
        "viewCount": views,
        "conversionRate": conversion_rate,
    }
