from __future__ import annotations

import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from backend import mcp_jsonrpc


MCP_URL = "https://mcp.example.com/mcp"
ACA_ARGS = {"app_name": "app", "resource_group": "rg", "subscription_id": "sub"}


@pytest.fixture(autouse=True)
def _clear_env_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MCP_OAUTH_AUDIENCE", "MICROSOFT_OBO_SCOPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")
    mcp_jsonrpc._token_cache.update(audience="", value="", expires_at=0.0)


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _Recorder:
    """Stand-in for urlopen that answers both the Entra and MCP endpoints."""

    def __init__(self, aca_payload: dict | None = None) -> None:
        self.requests: list = []
        self.aca_payload = aca_payload or {"revision": "r1", "variables": ["A"]}

    def __call__(self, request, timeout: float = 0):  # noqa: ANN001
        self.requests.append(request)
        if "login.microsoftonline.com" in request.full_url:
            body = json.dumps({"access_token": "tok-123", "expires_in": 3600})
        else:
            inner = json.dumps({"status": "completed", "data": self.aca_payload})
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": inner}]}})
        return _FakeResponse(body.encode("utf-8"))

    @property
    def token_requests(self) -> list:
        return [r for r in self.requests if "login.microsoftonline.com" in r.full_url]

    @property
    def mcp_requests(self) -> list:
        return [r for r in self.requests if "login.microsoftonline.com" not in r.full_url]


def _call() -> tuple[dict | None, str | None]:
    return mcp_jsonrpc.list_aca_environment_variables_jsonrpc(MCP_URL, **ACA_ARGS)


def test_audience_derived_from_obo_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://app-id/user_impersonation")

    assert mcp_jsonrpc.mcp_audience() == "api://app-id"


def test_audience_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://app-id/user_impersonation")
    monkeypatch.setenv("MCP_OAUTH_AUDIENCE", "api://other-id/")

    assert mcp_jsonrpc.mcp_audience() == "api://other-id"


def test_audience_empty_when_nothing_configured() -> None:
    assert mcp_jsonrpc.mcp_audience() == ""


def test_call_attaches_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://app-id/user_impersonation")
    recorder = _Recorder()
    monkeypatch.setattr(mcp_jsonrpc, "urlopen", recorder)

    result, err = _call()

    assert err is None
    assert result["revision"] == "r1"
    assert recorder.mcp_requests[0].get_header("Authorization") == "Bearer tok-123"
    token_body = recorder.token_requests[0].data.decode()
    assert "scope=api%3A%2F%2Fapp-id%2F.default" in token_body
    assert "grant_type=client_credentials" in token_body


def test_call_is_anonymous_without_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(mcp_jsonrpc, "urlopen", recorder)

    _result, err = _call()

    assert err is None
    assert recorder.token_requests == []
    assert recorder.mcp_requests[0].get_header("Authorization") is None


def test_token_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://app-id/user_impersonation")
    recorder = _Recorder()
    monkeypatch.setattr(mcp_jsonrpc, "urlopen", recorder)

    _call()
    _call()

    assert len(recorder.token_requests) == 1
    assert len(recorder.mcp_requests) == 2


def test_missing_credentials_reports_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://app-id/user_impersonation")
    monkeypatch.delenv("MICROSOFT_CLIENT_SECRET", raising=False)
    recorder = _Recorder()
    monkeypatch.setattr(mcp_jsonrpc, "urlopen", recorder)

    result, err = _call()

    assert result is None
    assert "MICROSOFT_CLIENT_SECRET" in err
    assert recorder.requests == []


def test_http_error_detail_includes_body_and_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://app-id/user_impersonation")
    headers = Message()
    headers["WWW-Authenticate"] = 'Bearer resource_metadata="https://mcp.example.com/.well-known"'

    def _fake_urlopen(request, timeout: float = 0):  # noqa: ANN001
        if "login.microsoftonline.com" in request.full_url:
            return _FakeResponse(json.dumps({"access_token": "tok-123", "expires_in": 3600}).encode())
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            headers,
            io.BytesIO(b'{"error":"invalid_request","error_description":"Authorization header is required."}'),
        )

    monkeypatch.setattr(mcp_jsonrpc, "urlopen", _fake_urlopen)

    result, err = _call()

    assert result is None
    assert "HTTP 401 Unauthorized" in err
    assert "resource_metadata" in err
    assert "Authorization header is required." in err
