"""
MongoDB async connection using Motor.
Provides a singleton database client with startup/shutdown lifecycle hooks.
"""

from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from app.config import settings

_client: Optional[AsyncIOMotorClient] = None


def get_database():
    """Get the MongoDB database instance. Must be called after startup."""
    if _client is None:
        raise RuntimeError("Database not initialized. Call connect_db() first.")
    return _client[settings.MONGO_DB_NAME]


async def connect_db() -> None:
    """Initialize MongoDB connection and create indexes."""
    global _client
    _client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    await _client.admin.command("ping")

    db = _client[settings.MONGO_DB_NAME]

    # Create indexes for performance and uniqueness
    await db.users.create_index("email", unique=True)
    await db.users.create_index("createdAt")
    await db.users.create_index("role")
    await db.base_resumes.create_index("userId")
    await db.base_resumes.create_index("createdAt")
    await db.base_resumes.create_index([("userId", ASCENDING), ("createdAt", DESCENDING)])
    await db.generated_resumes.create_index("userId")
    await db.generated_resumes.create_index("baseResumeId")
    await db.generated_resumes.create_index("createdAt")
    await db.generated_resumes.create_index("pdfStatus")
    await db.generated_resumes.create_index([("userId", ASCENDING), ("createdAt", DESCENDING)])
    await db.generated_resumes.create_index([("userId", ASCENDING), ("baseResumeId", ASCENDING)])
    await db.templates.create_index("ownerUserId")
    await db.templates.create_index("visibility")
    await db.templates.create_index("status")
    await db.templates.create_index("sourceType")
    await db.templates.create_index("templateKey", unique=True, sparse=True)
    await db.templates.create_index("slug", sparse=True)
    await db.templates.create_index("createdAt")
    await db.templates.create_index([("ownerUserId", ASCENDING), ("createdAt", DESCENDING)])
    await db.template_jobs.create_index("ownerUserId")
    await db.template_jobs.create_index("status")
    await db.template_jobs.create_index("createdAt")
    await db.resume_template_sessions.create_index("userId")
    await db.resume_template_sessions.create_index("resumeId")
    await db.resume_template_sessions.create_index("templateId")
    await db.resume_template_sessions.create_index("updatedAt")
    await db.resume_template_sessions.create_index(
        [("userId", ASCENDING), ("resumeId", ASCENDING), ("templateId", ASCENDING)],
        unique=True,
    )

    # Jobs collection with TTL index (auto-delete after 24 hours)
    await db.jobs.create_index("createdAt", expireAfterSeconds=86400)
    await db.jobs.create_index("job_id", unique=True)
    await db.jobs.create_index("userId")
    await db.jobs.create_index([("userId", ASCENDING), ("profile", ASCENDING), ("createdAt", DESCENDING)])

    # Template favorites
    await db.template_favorites.create_index("templateId")
    await db.template_favorites.create_index("userId")
    await db.template_favorites.create_index(
        [("templateId", ASCENDING), ("userId", ASCENDING)],
        unique=True,
    )

    # Template jobs — compound indexes for efficient queries
    await db.template_jobs.create_index([("ownerUserId", ASCENDING), ("status", ASCENDING)])
    await db.template_jobs.create_index([("status", ASCENDING), ("createdAt", DESCENDING)])

    # Templates — compound index for public catalog queries
    await db.templates.create_index([("visibility", ASCENDING), ("status", ASCENDING)])


async def disconnect_db() -> None:
    """Close MongoDB connection."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
