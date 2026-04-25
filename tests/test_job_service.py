from types import SimpleNamespace

import httpx
import pytest

from app.services import job_service


class FakeClient:
    def __init__(self, response):
        self._response = response

    async def get(self, *_args, **_kwargs):
        return self._response


@pytest.mark.asyncio
async def test_fetch_jobs_uses_shared_http_client(monkeypatch):
    response = httpx.Response(
        200,
        json={"data": [{"job_id": "1", "job_title": "Backend Engineer"}]},
        request=httpx.Request("GET", "https://example.com"),
    )
    monkeypatch.setattr(
        job_service,
        "get_runtime",
        lambda: SimpleNamespace(http_client=FakeClient(response)),
    )

    jobs = await job_service.fetch_jobs("backend jobs india", ["api-key"])

    assert len(jobs) == 1
    assert jobs[0]["job_title"] == "Backend Engineer"
