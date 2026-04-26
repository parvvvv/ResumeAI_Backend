from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import main
from app.services.auth_service import create_access_token


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return self.docs[:length] if length else self.docs

    def __aiter__(self):
        self._iter = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    async def count_documents(self, query):
        return len([doc for doc in self.docs if self._matches(doc, query)])

    def find(self, query, projection=None):
        docs = [doc for doc in self.docs if self._matches(doc, query)]
        if projection:
            docs = [self._project(doc, projection) for doc in docs]
        return FakeCursor(docs)

    def aggregate(self, pipeline):
        if pipeline and "$group" in pipeline[0] and "$ifNull" in pipeline[0]["$group"]["_id"]:
            counts = {}
            for doc in self.docs:
                status = doc.get("pdfStatus") or "missing"
                counts[status] = counts.get(status, 0) + 1
            return FakeCursor([{"_id": key, "count": value} for key, value in counts.items()])

        match = pipeline[0].get("$match", {}) if pipeline else {}
        docs = [doc for doc in self.docs if self._matches(doc, match)]
        scores = [
            doc.get("analytics", {}).get("atsScore")
            for doc in docs
            if doc.get("analytics", {}).get("atsScore", 0) > 0
        ]
        return FakeCursor([{"_id": None, "avg": sum(scores) / len(scores)}] if scores else [])

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = self._get_value(doc, key)
            if isinstance(expected, dict):
                if "$gte" in expected and (value is None or value < expected["$gte"]):
                    return False
                if "$gt" in expected and (value is None or value <= expected["$gt"]):
                    return False
                if "$regex" in expected and expected["$regex"].lower() not in str(value).lower():
                    return False
            elif value != expected:
                return False
        return True

    def _project(self, doc, projection):
        if all(value == 0 for value in projection.values()):
            return {key: value for key, value in doc.items() if key not in projection}
        return {
            key: self._get_value(doc, key)
            for key, include in projection.items()
            if include and self._get_value(doc, key) is not None
        } | {key: doc[key] for key in ("_id",) if key in doc}

    def _get_value(self, doc, dotted_key):
        value = doc
        for part in dotted_key.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value


class FakeDB:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.users = FakeCollection([
            {"_id": "admin-id", "email": "admin@example.com", "role": "admin", "createdAt": now, "passwordHash": "secret"},
            {"_id": "user-id", "email": "user@example.com", "role": "user", "createdAt": now - timedelta(days=10), "passwordHash": "secret"},
        ])
        self.base_resumes = FakeCollection([
            {"_id": "base-1", "userId": "user-id", "createdAt": now, "pdfStatus": "ready", "resumeData": {"private": True}},
        ])
        self.generated_resumes = FakeCollection([
            {"_id": "gen-1", "userId": "user-id", "createdAt": now, "pdfStatus": "ready", "analytics": {"atsScore": 80}, "modifiedData": {"private": True}},
            {"_id": "gen-2", "userId": "user-id", "createdAt": now, "analytics": {"atsScore": 60}, "jobDescription": "private"},
        ])
        self.jobs = FakeCollection([
            {"_id": "job-1", "userId": "user-id", "createdAt": now},
        ])


@pytest.mark.asyncio
async def test_admin_routes_require_admin(monkeypatch):
    monkeypatch.setattr(main.admin, "get_database", lambda: FakeDB())
    token = create_access_token("user-id", "user@example.com", "user")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/admin/overview", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_overview_counts(monkeypatch):
    monkeypatch.setattr(main.admin, "get_database", lambda: FakeDB())
    token = create_access_token("admin-id", "admin@example.com", "admin")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/admin/overview", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["users"] == 2
    assert payload["totals"]["originalResumes"] == 1
    assert payload["totals"]["tailoredResumes"] == 2
    assert payload["totals"]["pdfsReady"] == 2
    assert payload["averageAtsScore"] == 70


@pytest.mark.asyncio
async def test_admin_users_excludes_resume_content(monkeypatch):
    monkeypatch.setattr(main.admin, "get_database", lambda: FakeDB())
    token = create_access_token("admin-id", "admin@example.com", "admin")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 2
    serialized = str(payload)
    assert "resumeData" not in serialized
    assert "modifiedData" not in serialized
    assert "jobDescription" not in serialized
