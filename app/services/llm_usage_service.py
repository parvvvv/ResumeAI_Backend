"""
LLM usage telemetry: per-call token counts and estimated cost.

Every Gemini call (blocking or streaming) records a document into the
`llm_usage` collection so spend can be attributed per user / per feature.
Recording is best-effort and fire-and-forget — a telemetry failure must
never fail or slow down the user-facing request.

Attribution is carried via contextvars (`bind_llm_context`), which propagate
through `asyncio.to_thread` into the worker threads used by `run_blocking`.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Request-scoped attribution context
# ---------------------------------------------------------------------------
_llm_user_id: ContextVar[Optional[str]] = ContextVar("llm_user_id", default=None)
_llm_feature: ContextVar[str] = ContextVar("llm_feature", default="unknown")


def bind_llm_context(*, user_id: Optional[str] = None, feature: Optional[str] = None) -> None:
    """Bind user/feature attribution for subsequent LLM calls in this task."""
    if user_id is not None:
        _llm_user_id.set(str(user_id))
    if feature is not None:
        _llm_feature.set(feature)


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens) — estimates; keep in sync with provider pricing
# ---------------------------------------------------------------------------
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model: (input $/1M, output $/1M)
    "gemini-3-flash-preview": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
}


def _estimate_cost_usd(model: str, prompt_tokens: int, output_tokens: int) -> Optional[float]:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_price, output_price = pricing
    return round(
        (prompt_tokens * input_price + output_tokens * output_price) / 1_000_000, 6
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
# Captured at startup so worker threads can schedule inserts onto the loop.
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


async def _insert(doc: dict) -> None:
    try:
        from app.database import get_database

        await get_database().llm_usage.insert_one(doc)
    except Exception as exc:  # telemetry must never break the request
        logger.warning("llm_usage_insert_failed", error=str(exc))


def record_usage(
    *,
    model: str,
    usage_metadata: Any,
    latency_ms: Optional[int] = None,
    streamed: bool = False,
) -> None:
    """
    Record one LLM call. Safe to call from the event loop or a worker thread.

    `usage_metadata` is the google-genai `response.usage_metadata` object
    (may be None — the call is still recorded with zero counts).
    """
    prompt_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0
    thought_tokens = getattr(usage_metadata, "thoughts_token_count", 0) or 0
    total_tokens = getattr(usage_metadata, "total_token_count", 0) or (
        prompt_tokens + output_tokens + thought_tokens
    )

    doc = {
        "userId": _llm_user_id.get(),
        "feature": _llm_feature.get(),
        "model": model,
        "promptTokens": prompt_tokens,
        "outputTokens": output_tokens,
        "thoughtTokens": thought_tokens,
        "totalTokens": total_tokens,
        "estimatedCostUsd": _estimate_cost_usd(model, prompt_tokens, output_tokens + thought_tokens),
        "latencyMs": latency_ms,
        "streamed": streamed,
        "createdAt": datetime.now(timezone.utc),
    }

    logger.info(
        "llm_usage",
        feature=doc["feature"],
        model=model,
        total_tokens=total_tokens,
        cost_usd=doc["estimatedCostUsd"],
    )

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is not None:
        running_loop.create_task(_insert(doc))
    elif _main_loop is not None and _main_loop.is_running():
        # Called from a worker thread (e.g. run_blocking) — hop to the loop.
        asyncio.run_coroutine_threadsafe(_insert(doc), _main_loop)
    else:
        logger.debug("llm_usage_no_loop_skipped", feature=doc["feature"])
