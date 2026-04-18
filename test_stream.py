import asyncio
from google import genai
from app.config import settings

async def main():
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    response = await client.aio.models.generate_content_stream(
        model=settings.GEMINI_MODEL,
        contents="Say hello in 5 words"
    )
    async for chunk in response:
        print("CHUNK:", chunk.text)

asyncio.run(main())
