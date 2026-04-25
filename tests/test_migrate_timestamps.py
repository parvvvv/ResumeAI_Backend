from datetime import datetime, timezone

from scripts.migrate_timestamps import build_timestamp_update, parse_datetime_value


def test_parse_datetime_value_handles_iso_strings():
    parsed = parse_datetime_value("2026-04-25T12:30:00+00:00")

    assert isinstance(parsed, datetime)
    assert parsed.tzinfo == timezone.utc


def test_build_timestamp_update_is_idempotent_for_existing_datetimes():
    now = datetime.now(timezone.utc)
    document = {
        "_id": "abc",
        "createdAt": now,
        "updatedAt": "2026-04-25T12:30:00+00:00",
    }

    updates = build_timestamp_update(document)

    assert "createdAt" not in updates
    assert isinstance(updates["updatedAt"], datetime)
