"""
Lightweight upload load script for local verification.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
PDF_PATH = Path(os.getenv("PDF_PATH", "sample.pdf"))
CONCURRENCY = 5


async def upload_resume(client: httpx.AsyncClient, pdf_bytes: bytes, idx: int) -> float:
    started = time.perf_counter()
    files = {"file": (f"resume-{idx}.pdf", pdf_bytes, "application/pdf")}
    response = await client.post(
        "/api/resume/upload",
        files=files,
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {},
    )
    response.raise_for_status()
    return time.perf_counter() - started


async def main() -> None:
    pdf_bytes = PDF_PATH.read_bytes()
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=120.0) as client:
        durations = await asyncio.gather(*(upload_resume(client, pdf_bytes, i) for i in range(CONCURRENCY)))
    print(f"completed={len(durations)} avg={sum(durations) / len(durations):.2f}s max={max(durations):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
