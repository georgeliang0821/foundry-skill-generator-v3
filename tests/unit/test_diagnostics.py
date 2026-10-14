from __future__ import annotations

import json

import pytest

from backend.diagnostics import _sanitize, env_flag, log_context, log_event, log_timing


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


def test_timing_logs_duration_and_scoped_request_id(monkeypatch, capsys) -> None:
    from backend import diagnostics

    monkeypatch.setenv("SGV2_DEBUG_LOG", "1")
    ticks = iter([100, 325])
    monkeypatch.setattr(diagnostics, "now_ms", lambda: next(ticks))
    with log_context(request_id="chat-test", session_id="session-test"):
        with log_timing("chat.phase", phase="neighbor_load"):
            log_event("nested")
    log_event("outside")

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["event"] for record in records] == ["chat.phase.start", "nested", "chat.phase.done", "outside"]
    assert records[2]["duration_ms"] == 225
    assert all(record["request_id"] == "chat-test" for record in records[:3])
    assert records[0]["phase"] == "neighbor_load"
    assert "request_id" not in records[-1]


def test_timing_logs_failure_and_preserves_exception(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SGV2_DEBUG_LOG", "1")
    failure = ValueError("read failed")
    with pytest.raises(ValueError) as caught:
        with log_context(request_id="chat-failed"):
            with log_timing("chat.phase", phase="neighbor_load"):
                raise failure
    log_event("outside")

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert caught.value is failure
    assert records[1]["event"] == "chat.phase.failed"
    assert records[1]["error_type"] == "ValueError"
    assert records[1]["duration_ms"] >= 0
    assert records[1]["request_id"] == "chat-failed"
    assert "request_id" not in records[-1]
