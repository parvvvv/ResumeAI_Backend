"""
MongoDB async connection using Motor.
Provides a singleton database client with startup/shutdown lifecycle hooks.
"""

from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
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
    _client = AsyncIOMotorClient(settings.MONGO_URI)

    db = _client[settings.MONGO_DB_NAME]

    # Create indexes for performance and uniqueness
    await db.users.create_index("email", unique=True)
    await db.base_resumes.create_index("userId")
    await db.generated_resumes.create_index("userId")
    await db.generated_resumes.create_index("baseResumeId")


async def disconnect_db() -> None:
    """Close MongoDB connection."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
