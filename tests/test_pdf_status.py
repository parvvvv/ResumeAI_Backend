from app.routers.pdf import _update_pdf_status


class FakeCollection:
    def __init__(self):
        self.calls = []

    async def update_one(self, query, update):
        self.calls.append((query, update))


class FakeDB:
    def __init__(self):
        self.generated_resumes = FakeCollection()
        self.base_resumes = FakeCollection()


import pytest


@pytest.mark.asyncio
async def test_update_pdf_status_targets_generated_collection():
    db = FakeDB()

    await _update_pdf_status(db, "507f1f77bcf86cd799439011", True, "processing", {"pdfRequestedAt": "now"})

    assert len(db.generated_resumes.calls) == 1
    assert db.base_resumes.calls == []
