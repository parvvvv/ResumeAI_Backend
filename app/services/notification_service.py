"""
Notification service using Server-Sent Events (SSE).
Allows the backend to push real-time notifications to connected clients.
"""

import asyncio
from typing import Dict, Set
from dataclasses import dataclass, field
import structlog

logger = structlog.get_logger()


@dataclass
class Notification:
    """A notification payload."""
    event: str          # e.g. "pdf_ready", "pdf_failed", "tailor_complete"
    message: str        # Human-readable message
    data: dict = field(default_factory=dict)  # Extra data (pdfUrl, resumeId, etc.)


class NotificationService:
    """
    In-memory notification hub.
    Each user_id maps to a set of asyncio.Queues (one per SSE connection).
    When a notification is sent, it's pushed to all active queues for that user.
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, user_id: str) -> asyncio.Queue:
        """Create a new queue for a user and return it."""
        if user_id not in self._subscribers:
            self._subscribers[user_id] = set()
        queue = asyncio.Queue()
        self._subscribers[user_id].add(queue)
        logger.info("sse_subscribed", user_id=user_id, connections=len(self._subscribers[user_id]))
        return queue

    def unsubscribe(self, user_id: str, queue: asyncio.Queue):
        """Remove a queue when client disconnects."""
        if user_id in self._subscribers:
            self._subscribers[user_id].discard(queue)
            if not self._subscribers[user_id]:
                del self._subscribers[user_id]
            logger.info("sse_unsubscribed", user_id=user_id)

    async def notify(self, user_id: str, notification: Notification):
        """Push a notification to all of a user's active connections."""
        if user_id not in self._subscribers:
            logger.debug("sse_no_subscribers", user_id=user_id)
            return

        dead_queues = set()
        for queue in self._subscribers[user_id]:
            try:
                queue.put_nowait(notification)
            except asyncio.QueueFull:
                dead_queues.add(queue)

        # Clean up dead queues
        for q in dead_queues:
            self._subscribers[user_id].discard(q)

        logger.info("sse_notification_sent", user_id=user_id, event_type=notification.event)


# Singleton instance
notification_service = NotificationService()
