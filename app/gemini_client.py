"""
Gemini API client wrapper with automatic 503 fallback.

When the primary model returns a 503 (Service Unavailable / overloaded),
the wrapper transparently retries the same request against a list of
fallback models before raising.

Usage:
    from app.gemini_client import gemini_generate, gemini_stream

    # Blocking (sync) generation — run via `run_blocking`
    response = await run_blocking(gemini_generate, contents=prompt, ...)

    # Async streaming — returns an async iterator
    stream = await gemini_stream(contents=prompt, config=..., timeout=45)
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional

from google import genai
from google.genai import types
from google.genai.errors import ServerError

from app.config import settings
from app.runtime import run_blocking
import structlog

logger = structlog.get_logger()

# Shared client instance — reused across all helpers.
client = genai.Client(api_key=settings.GEMINI_API_KEY)


def _model_chain() -> list[str]:
    """Return [primary, *fallbacks] with duplicates removed while preserving order."""
    seen: set[str] = set()
    chain: list[str] = []
    for m in [settings.GEMINI_MODEL] + settings.GEMINI_FALLBACK_MODELS:
        if m not in seen:
            seen.add(m)
            chain.append(m)
    return chain


def _is_503(exc: Exception) -> bool:
    """Check if an exception represents a 503 Service Unavailable error."""
    if isinstance(exc, ServerError):
        # The SDK exposes `.code` on ServerError
        return getattr(exc, "code", None) == 503
    # Fallback: check the string representation for '503'
    return "503" in str(exc)


# ---------------------------------------------------------------------------
# Synchronous (blocking) generation — meant to be called via run_blocking
# ---------------------------------------------------------------------------

def gemini_generate(
    *,
    contents: Any,
    config: Optional[types.GenerateContentConfig] = None,
    timeout: Optional[float] = None,
    model: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """
    Synchronous generate_content with automatic 503 model fallback.

    Call via `await run_blocking(gemini_generate, contents=..., config=...)`.

    Parameters match `client.models.generate_content` — the only addition is
    transparent fallback across models on 503 errors.
    """
    models = _model_chain() if model is None else [model]
    timeout_secs = timeout or settings.GEMINI_TIMEOUT_SECONDS

    last_error: Exception | None = None
    for candidate_model in models:
        try:
            call_kwargs: dict[str, Any] = {
                "model": candidate_model,
                "contents": contents,
            }
            if config is not None:
                call_kwargs["config"] = config
            if timeout_secs is not None:
                call_kwargs["timeout"] = timeout_secs
            call_kwargs.update(kwargs)

            response = client.models.generate_content(**call_kwargs)
            return response

        except Exception as exc:
            last_error = exc
            if _is_503(exc):
                logger.warning(
                    "gemini_503_fallback",
                    failed_model=candidate_model,
                    error=str(exc),
                )
                continue  # try next model
            raise  # non-503 errors propagate immediately

    # All models exhausted — re-raise the last 503
    logger.error(
        "gemini_all_models_exhausted",
        models=models,
        error=str(last_error),
    )
    raise last_error  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Async streaming generation with 503 fallback
# ---------------------------------------------------------------------------

async def gemini_stream(
    *,
    contents: Any,
    config: Optional[types.GenerateContentConfig] = None,
    timeout: Optional[float] = None,
    model: Optional[str] = None,
    **kwargs: Any,
) -> AsyncIterator:
    """
    Async streaming generate_content with automatic 503 model fallback.

    Returns the async iterator from `aio.models.generate_content_stream`.
    On a 503 the call is retried with the next fallback model.

    Usage:
        stream = await gemini_stream(contents=prompt, config=cfg, timeout=45)
        async for chunk in stream:
            ...
    """
    models = _model_chain() if model is None else [model]
    timeout_secs = timeout or settings.GEMINI_TIMEOUT_SECONDS

    last_error: Exception | None = None
    for candidate_model in models:
        try:
            call_kwargs: dict[str, Any] = {
                "model": candidate_model,
                "contents": contents,
            }
            if config is not None:
                call_kwargs["config"] = config
            call_kwargs.update(kwargs)

            stream = await asyncio.wait_for(
                client.aio.models.generate_content_stream(**call_kwargs),
                timeout=timeout_secs,
            )
            return stream

        except Exception as exc:
            last_error = exc
            if _is_503(exc):
                logger.warning(
                    "gemini_stream_503_fallback",
                    failed_model=candidate_model,
                    error=str(exc),
                )
                continue
            raise

    logger.error(
        "gemini_stream_all_models_exhausted",
        models=models,
        error=str(last_error),
    )
    raise last_error  # type: ignore[misc]
