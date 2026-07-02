"""
Notification service using Server-Sent Events (SSE).
Allows the backend to push real-time notifications to connected clients.

Two delivery modes, selected by REDIS_URL:
- In-memory (default): notifications reach SSE connections on this process
  only. Correct for a single instance.
- Redis pub/sub: notifications are published to Redis and fanned out to the
  instance(s) actually holding the user's SSE connection, so the service can
  scale horizontally. Falls back to local delivery if Redis is unreachable.

The public API is unchanged: subscribe() / unsubscribe() / notify().
"""

import asyncio
import json
from typing import Dict, Optional, Set
from dataclasses import dataclass, field, asdict
import structlog

from app.config import settings

logger = structlog.get_logger()

_CHANNEL_PREFIX = "notify:"


@dataclass
class Notification:
    """A notification payload."""
    event: str          # e.g. "pdf_ready", "pdf_failed", "tailor_complete"
    message: str        # Human-readable message
    data: dict = field(default_factory=dict)  # Extra data (pdfUrl, resumeId, etc.)


class NotificationService:
    """
    Notification hub.
    Each user_id maps to a set of asyncio.Queues (one per SSE connection).
    When a notification is sent, it's pushed to all active queues for that
    user — via Redis pub/sub when configured, else directly in-process.
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._redis = None
        self._reader_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle (called from app lifespan)
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Connect the Redis fan-out when REDIS_URL is configured."""
        if not settings.REDIS_URL:
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self._redis.ping()
            self._reader_task = asyncio.create_task(self._reader_loop())
            logger.info("sse_redis_fanout_enabled")
        except Exception as exc:
            logger.warning("sse_redis_unavailable_using_memory", error=str(exc))
            self._redis = None

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def _reader_loop(self) -> None:
        """Receive published notifications and deliver to local queues."""
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(f"{_CHANNEL_PREFIX}*")
        try:
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                try:
                    user_id = message["channel"][len(_CHANNEL_PREFIX):]
                    payload = json.loads(message["data"])
                    self._deliver_local(user_id, Notification(**payload))
                except Exception as exc:
                    logger.warning("sse_redis_message_malformed", error=str(exc))
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
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
        if self._redis is not None:
            try:
                # Delivered back to this (and every) instance via _reader_loop.
                await self._redis.publish(
                    f"{_CHANNEL_PREFIX}{user_id}", json.dumps(asdict(notification))
                )
                logger.info("sse_notification_published", user_id=user_id, event_type=notification.event)
                return
            except Exception as exc:
                logger.warning("sse_redis_publish_failed_local_delivery", error=str(exc))

        self._deliver_local(user_id, notification)
        logger.info("sse_notification_sent", user_id=user_id, event_type=notification.event)

    # ------------------------------------------------------------------
    # Local delivery
    # ------------------------------------------------------------------
    def _deliver_local(self, user_id: str, notification: Notification) -> None:
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


# Singleton instance
notification_service = NotificationService()
