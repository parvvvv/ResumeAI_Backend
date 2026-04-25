"""
Shared runtime resources for the application lifecycle.
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

import httpx

from app.config import settings

T = TypeVar("T")


@dataclass
class ConcurrencyConfig:
    """Instance-local concurrency controls."""

    ai_pipeline_limit: int
    chat_limit: int
    pdf_render_limit: int
    blocking_limit: int


@dataclass
class RuntimeResources:
    """Shared resources initialized during app startup."""

    http_client: httpx.AsyncClient
    concurrency: ConcurrencyConfig
    ai_semaphore: asyncio.Semaphore
    chat_semaphore: asyncio.Semaphore
    pdf_semaphore: asyncio.Semaphore
    blocking_semaphore: asyncio.Semaphore


_runtime: Optional[RuntimeResources] = None


async def init_runtime() -> RuntimeResources:
    """Create the shared runtime resources for the process."""
    global _runtime
    if _runtime is not None:
        return _runtime

    concurrency = ConcurrencyConfig(
        ai_pipeline_limit=settings.AI_PIPELINE_CONCURRENCY,
        chat_limit=settings.CHAT_CONCURRENCY,
        pdf_render_limit=settings.PDF_RENDER_CONCURRENCY,
        blocking_limit=settings.BLOCKING_IO_CONCURRENCY,
    )

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.JSEARCH_TIMEOUT_SECONDS, connect=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )

    _runtime = RuntimeResources(
        http_client=http_client,
        concurrency=concurrency,
        ai_semaphore=asyncio.Semaphore(concurrency.ai_pipeline_limit),
        chat_semaphore=asyncio.Semaphore(concurrency.chat_limit),
        pdf_semaphore=asyncio.Semaphore(concurrency.pdf_render_limit),
        blocking_semaphore=asyncio.Semaphore(concurrency.blocking_limit),
    )
    return _runtime


def get_runtime() -> RuntimeResources:
    """Return the initialized runtime resources."""
    if _runtime is None:
        raise RuntimeError("Runtime resources not initialized. Call init_runtime() first.")
    return _runtime


def try_get_runtime() -> Optional[RuntimeResources]:
    """Return runtime resources when initialized, else None."""
    return _runtime


async def shutdown_runtime() -> None:
    """Close shared runtime resources."""
    global _runtime
    if _runtime is None:
        return

    await _runtime.http_client.aclose()
    _runtime = None


async def run_blocking(
    func: Callable[..., T],
    *args: Any,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> T:
    """
    Run a blocking callable without occupying the event loop.

    A semaphore keeps the instance from saturating its thread offload capacity.
    """
    bound = functools.partial(func, *args, **kwargs)
    timeout_secs = timeout or settings.BLOCKING_OPERATION_TIMEOUT_SECONDS
    runtime = try_get_runtime()

    if runtime is None:
        return await asyncio.wait_for(asyncio.to_thread(bound), timeout=timeout_secs)

    async with runtime.blocking_semaphore:
        return await asyncio.wait_for(asyncio.to_thread(bound), timeout=timeout_secs)


async def run_with_timeout(awaitable: Awaitable[T], timeout: float) -> T:
    """Apply a timeout to an awaitable."""
    return await asyncio.wait_for(awaitable, timeout=timeout)
