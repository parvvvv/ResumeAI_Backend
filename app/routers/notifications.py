"""
SSE (Server-Sent Events) notification router.
Streams real-time notifications to authenticated clients.
"""

import asyncio
import json
from fastapi import APIRouter, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from jose import JWTError
from app.services.auth_service import decode_jwt
from app.services.notification_service import notification_service
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


@router.get("/stream")
async def notification_stream(
    request: Request,
    token: str = Query(..., description="JWT token (EventSource can't send headers)"),
):
    """
    SSE endpoint — client connects and receives real-time notifications.
    Token is passed as a query parameter since EventSource doesn't support custom headers.
    """
    # Validate token manually (can't use Depends with EventSource)
    try:
        payload = decode_jwt(token)
        user_id = payload["sub"]
    except (JWTError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    queue = notification_service.subscribe(user_id)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    notification = await asyncio.wait_for(queue.get(), timeout=30.0)
                    payload = json.dumps({
                        "event": notification.event,
                        "message": notification.message,
                        "data": notification.data,
                    })
                    yield f"event: {notification.event}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            notification_service.unsubscribe(user_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
