"""
Lightweight mixed-traffic script for local verification.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
RESUME_ID = os.getenv("RESUME_ID", "")
JOB_DESCRIPTION = os.getenv("JOB_DESCRIPTION", "Software engineer role focused on Python and APIs")


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}


async def call_chat(client: httpx.AsyncClient, idx: int) -> float:
    started = time.perf_counter()
    response = await client.post("/api/chat", json={"query": f"mixed query {idx}"}, headers=_auth_headers())
    response.raise_for_status()
    return time.perf_counter() - started


async def call_dashboard(client: httpx.AsyncClient) -> float:
    started = time.perf_counter()
    response = await client.get("/api/dashboard", headers=_auth_headers())
    response.raise_for_status()
    return time.perf_counter() - started


async def call_tailor(client: httpx.AsyncClient) -> float:
    if not RESUME_ID:
        return 0.0
    started = time.perf_counter()
    response = await client.post(
        "/api/resume/tailor",
        json={"baseResumeId": RESUME_ID, "jobDescription": JOB_DESCRIPTION},
        headers=_auth_headers(),
    )
    response.raise_for_status()
    return time.perf_counter() - started


async def main() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=120.0) as client:
        durations = await asyncio.gather(
            *(call_chat(client, i) for i in range(5)),
            call_dashboard(client),
            call_tailor(client),
        )
    real_durations = [duration for duration in durations if duration > 0]
    print(
        f"completed={len(real_durations)} "
        f"avg={sum(real_durations) / len(real_durations):.2f}s "
        f"max={max(real_durations):.2f}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
