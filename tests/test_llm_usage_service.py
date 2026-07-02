"""Tests for LLM usage telemetry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import llm_usage_service
from app.services.llm_usage_service import (
    _estimate_cost_usd,
    bind_llm_context,
    record_usage,
)


class TestCostEstimation:
    def test_known_model(self):
        # 1M input + 1M output on gemini-2.0-flash = $0.10 + $0.40
        assert _estimate_cost_usd("gemini-2.0-flash", 1_000_000, 1_000_000) == 0.50

    def test_unknown_model_returns_none(self):
        assert _estimate_cost_usd("some-future-model", 1000, 1000) is None

    def test_zero_tokens(self):
        assert _estimate_cost_usd("gemini-2.5-flash", 0, 0) == 0.0


class TestRecordUsage:
    @pytest.mark.asyncio
    async def test_records_document_with_context(self, monkeypatch):
        inserted = {}

        mock_db = MagicMock()
        mock_db.llm_usage.insert_one = AsyncMock(
            side_effect=lambda doc: inserted.update(doc)
        )
        monkeypatch.setattr("app.database.get_database", lambda: mock_db)

        bind_llm_context(user_id="user-123", feature="resume_parse")
        usage = SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=50,
            thoughts_token_count=0,
            total_token_count=150,
        )
        record_usage(model="gemini-2.0-flash", usage_metadata=usage, latency_ms=42)

        # Drain the fire-and-forget insert task
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert inserted["userId"] == "user-123"
        assert inserted["feature"] == "resume_parse"
        assert inserted["model"] == "gemini-2.0-flash"
        assert inserted["promptTokens"] == 100
        assert inserted["outputTokens"] == 50
        assert inserted["totalTokens"] == 150
        assert inserted["estimatedCostUsd"] == pytest.approx(0.00003)
        assert inserted["latencyMs"] == 42

    @pytest.mark.asyncio
    async def test_none_usage_metadata_still_records(self, monkeypatch):
        inserted = {}
        mock_db = MagicMock()
        mock_db.llm_usage.insert_one = AsyncMock(
            side_effect=lambda doc: inserted.update(doc)
        )
        monkeypatch.setattr("app.database.get_database", lambda: mock_db)

        record_usage(model="gemini-2.5-flash", usage_metadata=None, streamed=True)

        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert inserted["totalTokens"] == 0
        assert inserted["streamed"] is True

    @pytest.mark.asyncio
    async def test_insert_failure_never_raises(self, monkeypatch):
        mock_db = MagicMock()
        mock_db.llm_usage.insert_one = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr("app.database.get_database", lambda: mock_db)

        # Must not raise, even though the insert fails
        record_usage(model="gemini-2.5-flash", usage_metadata=None)

        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def test_no_loop_is_noop(self):
        # Called from a plain sync context with no captured loop: skip silently
        original = llm_usage_service._main_loop
        llm_usage_service._main_loop = None
        try:
            record_usage(model="gemini-2.5-flash", usage_metadata=None)
        finally:
            llm_usage_service._main_loop = original
