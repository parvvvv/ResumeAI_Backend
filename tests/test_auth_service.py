import pytest

from app.services.auth_service import hash_password_async, verify_password_async


@pytest.mark.asyncio
async def test_hash_and_verify_password_async():
    hashed = await hash_password_async("super-secret-123")

    assert hashed != "super-secret-123"
    assert await verify_password_async("super-secret-123", hashed) is True
    assert await verify_password_async("wrong-password", hashed) is False
