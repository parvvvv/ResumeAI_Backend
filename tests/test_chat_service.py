from types import SimpleNamespace
import asyncio

import pytest
from langchain.prompts import PromptTemplate

from app.services.chat_service import ChatService


class _FakeEmbeddings:
    def embed_query(self, query):
        return [0.1, 0.2, len(query)]


class _FakeRPC:
    def execute(self):
        return SimpleNamespace(data=[{"content": "ResumeAI helps tailor resumes.", "metadata": {"source": "docs"}}])


class _FakeSupabase:
    def rpc(self, *_args, **_kwargs):
        return _FakeRPC()


class _FakeLLM:
    def invoke(self, prompt):
        return SimpleNamespace(content=f"answer for: {prompt}")


@pytest.mark.asyncio
async def test_retrieve_and_answer_is_async(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.initialized = True
    service.supabase = _FakeSupabase()
    service.embeddings = _FakeEmbeddings()
    service.llm = _FakeLLM()
    service.prompt = PromptTemplate.from_template("{context} :: {query}")

    async def fake_run_blocking(func, *args, **kwargs):
        return func(*args)

    monkeypatch.setattr("app.services.chat_service.run_blocking", fake_run_blocking)
    monkeypatch.setattr(
        "app.services.chat_service.get_runtime",
        lambda: SimpleNamespace(chat_semaphore=asyncio.Semaphore(1)),
    )

    answer = await service.retrieve_and_answer("help me")

    assert "answer for" in answer
    assert "help me" in answer
