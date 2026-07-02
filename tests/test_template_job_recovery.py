"""Tests for template job crash recovery."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import template_gen_service


def _make_db(jobs):
    async def _cursor():
        for job in jobs:
            yield job

    db = MagicMock()
    db.template_jobs.find = MagicMock(return_value=_cursor())
    db.template_jobs.update_one = AsyncMock()
    return db


class TestRecoverStaleTemplateJobs:
    @pytest.mark.asyncio
    async def test_requeues_recent_stuck_jobs(self, monkeypatch):
        now = datetime.now(timezone.utc)
        jobs = [
            {"_id": "job-1", "ownerUserId": "u1", "status": "processing", "updatedAt": now - timedelta(minutes=5)},
            {"_id": "job-2", "ownerUserId": "u2", "status": "queued", "updatedAt": now - timedelta(hours=1)},
        ]
        db = _make_db(jobs)
        monkeypatch.setattr(template_gen_service, "get_database", lambda: db)
        processed = AsyncMock()
        monkeypatch.setattr(template_gen_service, "_process_template_job", processed)

        count = await template_gen_service.recover_stale_template_jobs()
        await asyncio.sleep(0)  # let created tasks start

        assert count == 2
        assert processed.call_count == 2

    @pytest.mark.asyncio
    async def test_expires_old_jobs(self, monkeypatch):
        now = datetime.now(timezone.utc)
        jobs = [
            {"_id": "old-job", "ownerUserId": "u1", "status": "queued", "updatedAt": now - timedelta(hours=48)},
        ]
        db = _make_db(jobs)
        monkeypatch.setattr(template_gen_service, "get_database", lambda: db)
        processed = AsyncMock()
        monkeypatch.setattr(template_gen_service, "_process_template_job", processed)

        count = await template_gen_service.recover_stale_template_jobs()
        await asyncio.sleep(0)

        assert count == 0
        processed.assert_not_called()
        # Marked failed
        update = db.template_jobs.update_one.call_args.args
        assert update[0] == {"_id": "old-job"}
        assert update[1]["$set"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_handles_naive_datetimes(self, monkeypatch):
        # pymongo returns naive UTC datetimes — must not crash or misclassify
        naive_recent = datetime.utcnow() - timedelta(minutes=10)
        jobs = [
            {"_id": "job-naive", "ownerUserId": "u1", "status": "processing", "updatedAt": naive_recent},
        ]
        db = _make_db(jobs)
        monkeypatch.setattr(template_gen_service, "get_database", lambda: db)
        processed = AsyncMock()
        monkeypatch.setattr(template_gen_service, "_process_template_job", processed)

        count = await template_gen_service.recover_stale_template_jobs()
        await asyncio.sleep(0)

        assert count == 1
