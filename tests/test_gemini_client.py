"""
Tests for the Gemini 503 fallback logic in app.gemini_client.

Covers:
  - gemini_generate: sync generation with model fallback on 503
  - gemini_stream: async streaming with model fallback on 503
  - _is_503: helper function edge cases
  - _model_chain: deduplication and ordering
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch, call
import asyncio

import pytest

from google.genai.errors import ServerError


# ---------------------------------------------------------------------------
# Helpers — we avoid importing the module at the top level so monkeypatch
# can override settings *before* the module-level `client` is created.
# ---------------------------------------------------------------------------


def _make_503_error(message: str = "The model is overloaded") -> ServerError:
    """Create a fake ServerError with code=503."""
    exc = ServerError.__new__(ServerError)
    exc.code = 503
    exc.args = (message,)
    return exc


def _make_500_error(message: str = "Internal server error") -> ServerError:
    exc = ServerError.__new__(ServerError)
    exc.code = 500
    exc.args = (message,)
    return exc


# ---------------------------------------------------------------------------
# _is_503
# ---------------------------------------------------------------------------


class TestIs503:
    def test_server_error_with_503_code(self):
        from app.gemini_client import _is_503

        assert _is_503(_make_503_error()) is True

    def test_server_error_with_non_503_code(self):
        from app.gemini_client import _is_503

        assert _is_503(_make_500_error()) is False

    def test_generic_exception_with_503_in_message(self):
        from app.gemini_client import _is_503

        assert _is_503(Exception("503 Service Unavailable")) is True

    def test_generic_exception_without_503(self):
        from app.gemini_client import _is_503

        assert _is_503(Exception("Connection refused")) is False


# ---------------------------------------------------------------------------
# _model_chain
# ---------------------------------------------------------------------------


class TestModelChain:
    def test_deduplication(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "gemini-3-flash-preview")
        monkeypatch.setattr(
            "app.gemini_client.settings.GEMINI_FALLBACK_MODELS",
            ["gemini-3-flash-preview", "gemini-2.5-flash"],  # duplicate primary
        )
        from app.gemini_client import _model_chain

        chain = _model_chain()
        assert chain == ["gemini-3-flash-preview", "gemini-2.5-flash"]

    def test_preserves_order(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "model-a")
        monkeypatch.setattr(
            "app.gemini_client.settings.GEMINI_FALLBACK_MODELS",
            ["model-b", "model-c"],
        )
        from app.gemini_client import _model_chain

        assert _model_chain() == ["model-a", "model-b", "model-c"]

    def test_empty_fallbacks(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "only-model")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", [])
        from app.gemini_client import _model_chain

        assert _model_chain() == ["only-model"]


# ---------------------------------------------------------------------------
# gemini_generate (sync, blocking)
# ---------------------------------------------------------------------------


class TestGeminiGenerate:
    def test_success_on_first_model(self, monkeypatch):
        """Primary model succeeds — no fallback triggered."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["fallback-1"])

        fake_response = SimpleNamespace(text="Hello from primary")
        mock_generate = MagicMock(return_value=fake_response)
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        result = gemini_generate(contents="test prompt", timeout=10)

        assert result.text == "Hello from primary"
        assert mock_generate.call_count == 1
        # Verify it used the primary model
        assert mock_generate.call_args.kwargs["model"] == "primary"

    def test_fallback_on_503(self, monkeypatch):
        """Primary model returns 503 → falls back to the next model."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr(
            "app.gemini_client.settings.GEMINI_FALLBACK_MODELS",
            ["fallback-1", "fallback-2"],
        )

        success_response = SimpleNamespace(text="Hello from fallback-1")

        def side_effect(**kwargs):
            if kwargs["model"] == "primary":
                raise _make_503_error()
            return success_response

        mock_generate = MagicMock(side_effect=side_effect)
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        result = gemini_generate(contents="test prompt", timeout=10)

        assert result.text == "Hello from fallback-1"
        assert mock_generate.call_count == 2
        # First call was primary, second was fallback-1
        assert mock_generate.call_args_list[0].kwargs["model"] == "primary"
        assert mock_generate.call_args_list[1].kwargs["model"] == "fallback-1"

    def test_cascading_fallback(self, monkeypatch):
        """All models 503 except the last one."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "m1")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["m2", "m3"])

        success_response = SimpleNamespace(text="Hello from m3")

        def side_effect(**kwargs):
            if kwargs["model"] in ("m1", "m2"):
                raise _make_503_error()
            return success_response

        mock_generate = MagicMock(side_effect=side_effect)
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        result = gemini_generate(contents="test", timeout=10)

        assert result.text == "Hello from m3"
        assert mock_generate.call_count == 3

    def test_all_models_503_raises(self, monkeypatch):
        """When every model returns 503, the last error is raised."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "m1")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["m2"])

        mock_generate = MagicMock(side_effect=_make_503_error("overloaded"))
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        with pytest.raises(ServerError):
            gemini_generate(contents="test", timeout=10)

        # Both models were tried
        assert mock_generate.call_count == 2

    def test_non_503_error_raises_immediately(self, monkeypatch):
        """A non-503 error (e.g. 400 Bad Request) should NOT trigger fallback."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["fallback-1"])

        mock_generate = MagicMock(side_effect=_make_500_error("internal"))
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        with pytest.raises(ServerError):
            gemini_generate(contents="test", timeout=10)

        # Only the primary model was tried — no fallback
        assert mock_generate.call_count == 1

    def test_explicit_model_skips_fallback_chain(self, monkeypatch):
        """When `model` is explicitly passed, only that model is used."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["fallback-1"])

        mock_generate = MagicMock(side_effect=_make_503_error())
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        with pytest.raises(ServerError):
            gemini_generate(contents="test", model="explicit-model", timeout=10)

        # Only the explicitly specified model was tried
        assert mock_generate.call_count == 1
        assert mock_generate.call_args.kwargs["model"] == "explicit-model"

    def test_config_is_forwarded(self, monkeypatch):
        """Verify that config and extra kwargs are passed through to the SDK."""
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", [])

        from google.genai import types

        cfg = types.GenerateContentConfig(temperature=0.7)
        mock_generate = MagicMock(return_value=SimpleNamespace(text="ok"))
        monkeypatch.setattr("app.gemini_client.client.models.generate_content", mock_generate)

        from app.gemini_client import gemini_generate

        gemini_generate(contents="prompt", config=cfg)

        call_kwargs = mock_generate.call_args.kwargs
        assert call_kwargs["config"] is cfg


# ---------------------------------------------------------------------------
# gemini_stream (async streaming)
# ---------------------------------------------------------------------------


class TestGeminiStream:
    @pytest.mark.asyncio
    async def test_success_on_first_model(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["fallback-1"])

        fake_stream = AsyncMock()

        async def fake_generate_stream(**kwargs):
            return fake_stream

        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = fake_generate_stream
        monkeypatch.setattr("app.gemini_client.client", mock_client)

        from app.gemini_client import gemini_stream

        result = await gemini_stream(contents="prompt", timeout=10)
        assert result is fake_stream

    @pytest.mark.asyncio
    async def test_fallback_on_503(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["fallback-1"])

        fake_stream = AsyncMock()
        call_log = []

        async def fake_generate_stream(**kwargs):
            call_log.append(kwargs["model"])
            if kwargs["model"] == "primary":
                raise _make_503_error()
            return fake_stream

        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = fake_generate_stream
        monkeypatch.setattr("app.gemini_client.client", mock_client)

        from app.gemini_client import gemini_stream

        result = await gemini_stream(contents="prompt", timeout=10)

        assert result is fake_stream
        assert call_log == ["primary", "fallback-1"]

    @pytest.mark.asyncio
    async def test_all_models_503_raises(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "m1")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["m2"])

        async def fake_generate_stream(**kwargs):
            raise _make_503_error()

        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = fake_generate_stream
        monkeypatch.setattr("app.gemini_client.client", mock_client)

        from app.gemini_client import gemini_stream

        with pytest.raises(ServerError):
            await gemini_stream(contents="prompt", timeout=10)

    @pytest.mark.asyncio
    async def test_non_503_raises_immediately(self, monkeypatch):
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_MODEL", "primary")
        monkeypatch.setattr("app.gemini_client.settings.GEMINI_FALLBACK_MODELS", ["fallback-1"])

        call_log = []

        async def fake_generate_stream(**kwargs):
            call_log.append(kwargs["model"])
            raise _make_500_error()

        mock_client = MagicMock()
        mock_client.aio.models.generate_content_stream = fake_generate_stream
        monkeypatch.setattr("app.gemini_client.client", mock_client)

        from app.gemini_client import gemini_stream

        with pytest.raises(ServerError):
            await gemini_stream(contents="prompt", timeout=10)

        # Only primary was tried
        assert call_log == ["primary"]
