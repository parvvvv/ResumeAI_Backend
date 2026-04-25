"""
Backfill legacy ISO-string timestamps into BSON datetime values.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings

TIMESTAMP_FIELDS = (
    "createdAt",
    "updatedAt",
    "pdfRequestedAt",
    "pdfCompletedAt",
)
COLLECTIONS = ("base_resumes", "generated_resumes", "jobs")


def parse_datetime_value(value: Any) -> Any:
    """Convert an ISO timestamp string into a timezone-aware datetime."""
    if value is None or isinstance(value, datetime):
        return value

    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    return value


def build_timestamp_update(document: Dict[str, Any]) -> Dict[str, datetime]:
    """Build a partial update for any legacy timestamp strings."""
    updates: Dict[str, datetime] = {}
    for field in TIMESTAMP_FIELDS:
        current = document.get(field)
        parsed = parse_datetime_value(current)
        if isinstance(current, str) and isinstance(parsed, datetime):
            updates[field] = parsed
    return updates


async def migrate_collection(db, collection_name: str) -> int:
    """Migrate one collection and return the number of updated documents."""
    projection = {"_id": 1}
    for field in TIMESTAMP_FIELDS:
        projection[field] = 1

    updated_count = 0
    cursor = db[collection_name].find({}, projection)
    async for document in cursor:
        updates = build_timestamp_update(document)
        if not updates:
            continue

        await db[collection_name].update_one({"_id": document["_id"]}, {"$set": updates})
        updated_count += 1

    return updated_count


async def main() -> None:
    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        db = client[settings.MONGO_DB_NAME]
        for collection_name in COLLECTIONS:
            updated = await migrate_collection(db, collection_name)
            print(f"{collection_name}: updated {updated} documents")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
