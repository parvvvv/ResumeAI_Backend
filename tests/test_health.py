from types import SimpleNamespace

import httpx
import pytest

from app import main


@pytest.mark.asyncio
async def test_ready_health_reports_ok(monkeypatch):
    class FakeDB:
        async def command(self, command_name):
            assert command_name == "ping"
            return {"ok": 1}

    monkeypatch.setattr(main, "get_database", lambda: FakeDB())
    monkeypatch.setattr(main, "get_runtime", lambda: SimpleNamespace(http_client=object()))

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["dependencies"]["mongo"] == "ok"
    assert payload["dependencies"]["runtime"] == "ok"
