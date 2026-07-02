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


# ---------------------------------------------------------------------------
# Action tokens (email verification / password reset)
# ---------------------------------------------------------------------------

def test_action_token_round_trip():
    from app.services.auth_service import create_action_token

    token = create_action_token("user-9", "email_verify", hours=24)
    payload = decode_jwt(token, expected_type="email_verify")

    assert payload["sub"] == "user-9"
    assert payload["type"] == "email_verify"


def test_action_token_cannot_be_used_as_access_token():
    from jose import JWTError
    from app.services.auth_service import create_action_token

    token = create_action_token("user-9", "password_reset", hours=1)
    with pytest.raises(JWTError):
        decode_jwt(token, expected_type="access")


def test_access_token_cannot_be_used_for_reset():
    from jose import JWTError

    token = create_access_token("user-9", "u@example.com")
    with pytest.raises(JWTError):
        decode_jwt(token, expected_type="password_reset")


def test_reset_token_carries_password_version():
    from app.services.auth_service import create_action_token, password_version

    pv = password_version("$2b$12$abcdefghijklmnopqrstuv")
    token = create_action_token("user-9", "password_reset", hours=1, extra={"pv": pv})
    payload = decode_jwt(token, expected_type="password_reset")

    assert payload["pv"] == pv
    # Version changes when the hash changes -> old tokens become invalid
    assert password_version("$2b$12$differenthashvaluehere") != pv
