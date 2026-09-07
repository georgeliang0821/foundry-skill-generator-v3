from __future__ import annotations

from backend.diagnostics import _sanitize, env_flag


def test_sanitize_masks_sensitive_keys() -> None:
    sanitized = _sanitize(
        {
            "authorization": "Bearer very-long-secret-token-value",
            "nested": {"client_secret": "abcdef1234567890"},
            "has_delegated_token": True,
        }
    )

    assert sanitized["authorization"].startswith("Bearer")
    assert "secret-token-value" not in sanitized["authorization"]
    assert sanitized["nested"]["client_secret"] == "abcdef...7890 (len=16)"
    assert sanitized["has_delegated_token"] is True


def test_env_flag_reports_missing_or_set(monkeypatch) -> None:
    monkeypatch.delenv("SGV2_TEST_ENV_FLAG", raising=False)
    assert env_flag("SGV2_TEST_ENV_FLAG") == "missing"

    monkeypatch.setenv("SGV2_TEST_ENV_FLAG", "abc")
    assert env_flag("SGV2_TEST_ENV_FLAG") == "set(len=3)"
