"""
Lightweight chat load script for local verification.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
CONCURRENCY = 10


async def hit_chat(client: httpx.AsyncClient, idx: int) -> float:
    started = time.perf_counter()
    response = await client.post(
        "/api/chat",
        json={"query": f"test query {idx}"},
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {},
    )
    response.raise_for_status()
    return time.perf_counter() - started


async def main() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=60.0) as client:
        durations = await asyncio.gather(*(hit_chat(client, i) for i in range(CONCURRENCY)))
    print(f"completed={len(durations)} avg={sum(durations) / len(durations):.2f}s max={max(durations):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
