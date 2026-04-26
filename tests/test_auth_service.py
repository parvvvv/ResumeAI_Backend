import pytest

from app.services.auth_service import create_access_token, decode_jwt, hash_password_async, verify_password_async


@pytest.mark.asyncio
async def test_hash_and_verify_password_async():
    hashed = await hash_password_async("super-secret-123")

    assert hashed != "super-secret-123"
    assert await verify_password_async("super-secret-123", hashed) is True
    assert await verify_password_async("wrong-password", hashed) is False


def test_access_token_includes_role():
    token = create_access_token("user-1", "admin@example.com", "admin")
    payload = decode_jwt(token)

    assert payload["sub"] == "user-1"
    assert payload["email"] == "admin@example.com"
    assert payload["role"] == "admin"
