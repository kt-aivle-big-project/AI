from datetime import UTC, datetime

from app.services.audit import sanitize_log_details


def test_sanitize_log_details_redacts_sensitive_values() -> None:
    value = {
        "OPENAI_API_KEY": "sk-secret",
        "nested": {
            "password": "password-value",
            "Authorization": "Bearer token-value",
            "safe": "postgresql://user:password@db.example/warehouse",
            "text": "OPENAI_API_KEY=sk-secret password: visible-secret",
        },
    }

    sanitized = sanitize_log_details(value)

    assert sanitized["OPENAI_API_KEY"] == "***REDACTED***"
    assert sanitized["nested"]["password"] == "***REDACTED***"
    assert sanitized["nested"]["Authorization"] == "***REDACTED***"
    assert "password@" not in sanitized["nested"]["safe"]
    assert "***@" in sanitized["nested"]["safe"]
    assert "sk-secret" not in sanitized["nested"]["text"]
    assert "visible-secret" not in sanitized["nested"]["text"]


def test_sanitize_log_details_preserves_database_timestamp_type() -> None:
    timestamp = datetime.now(UTC)

    assert sanitize_log_details(timestamp) is timestamp
