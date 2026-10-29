from __future__ import annotations

import base64
import io
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

from backend.auth_store import AUTH_SESSION_SECONDS


class TokenResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture()
def signed_in(backend_main, anon_client, monkeypatch):
    monkeypatch.setenv("E2E_MODE", "0")
    monkeypatch.setenv("E2E_DEFAULT_UPN", "")
    deadline = time.time() + 7200
    backend_main.auth_store.save_auth_token("auth-id", {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": time.time() - 1,
        "session_expires_at": deadline,
        "scope": "api://test/access_as_user",
        "profile": {"email": "test@example.com", "name": "Test"},
    })
    anon_client.cookies.set("sgv2_auth", "auth-id")
    return anon_client, deadline


@pytest.mark.parametrize("rotated", [True, False])
def test_status_refreshes_tokens_and_keeps_fixed_session(signed_in, backend_main, monkeypatch, rotated):
    client, deadline = signed_in
    calls = []

    def exchange(request, timeout):
        calls.append(parse_qs(request.data.decode()))
        assert timeout == 30
        payload = {"access_token": "new-access", "expires_in": 3600}
        if rotated:
            payload["refresh_token"] = "new-refresh"
        return TokenResponse(payload)

    monkeypatch.setattr(backend_main, "urlopen", exchange)
    before = time.time()
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["session_expires_at"] == deadline
    assert before + 3600 <= body["expires_at"] <= time.time() + 3600
    assert "access_token" not in body and "refresh_token" not in body
    assert "set-cookie" not in response.headers
    saved = backend_main.auth_tokens["auth-id"]
    assert saved["refresh_token"] == ("new-refresh" if rotated else "old-refresh")
    assert saved["profile"]["email"] == "test@example.com"
    assert calls[0]["grant_type"] == ["refresh_token"]
    assert calls[0]["refresh_token"] == ["old-refresh"]
    assert calls[0]["client_secret"] == ["test-secret"]
    assert client.get("/api/neighbor-notices/summary").status_code == 200
    request = Request({"type": "http", "headers": [(b"cookie", b"sgv2_auth=auth-id")]})
    assert backend_main.get_delegated_token(request) == "new-access"
    assert len(calls) == 1


def test_protected_api_refreshes_before_acl(signed_in, backend_main, monkeypatch):
    client, _ = signed_in
    monkeypatch.setattr(backend_main, "urlopen", lambda *args, **kwargs: TokenResponse({
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
    }))
    assert client.get("/api/neighbor-notices/summary").status_code == 200
    assert backend_main.auth_tokens["auth-id"]["access_token"] == "new-access"


@pytest.mark.parametrize("error_code", ["invalid_grant", "interaction_required", "login_required", "consent_required"])
def test_revoked_refresh_requires_sign_in(signed_in, backend_main, monkeypatch, error_code):
    client, _ = signed_in

    def revoked(*args, **kwargs):
        raise HTTPError("https://example.test/token", 400, "Bad request", {}, io.BytesIO(
            json.dumps({"error": error_code}).encode(),
        ))

    monkeypatch.setattr(backend_main, "urlopen", revoked)
    assert client.get("/api/neighbor-notices/summary").status_code == 401
    assert client.get("/api/auth/status").json()["authenticated"] is False
    assert "auth-id" not in backend_main.auth_tokens


@pytest.mark.parametrize("failure", ["network", "server", "throttle", "client_config"])
def test_temporary_or_configuration_failure_is_not_logout(signed_in, backend_main, monkeypatch, failure):
    client, _ = signed_in

    def unavailable(*args, **kwargs):
        if failure == "network":
            raise URLError("unavailable")
        status = {"server": 500, "throttle": 429, "client_config": 401}[failure]
        raise HTTPError("https://example.test/token", status, "Unavailable", {}, io.BytesIO(
            b'{"error":"invalid_client"}',
        ))

    monkeypatch.setattr(backend_main, "urlopen", unavailable)
    for path in ("/api/auth/status", "/api/neighbor-notices/summary"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        assert "auth-id" in backend_main.auth_tokens
    monkeypatch.setattr(backend_main, "urlopen", lambda *args, **kwargs: TokenResponse({
        "access_token": "recovered", "expires_in": 3600,
    }))
    assert client.get("/api/neighbor-notices/summary").status_code == 200


@pytest.mark.parametrize("payload", [
    {}, [], {"access_token": "new"}, {"access_token": "new", "expires_in": 0},
    {"access_token": "new", "expires_in": "bad"},
    {"access_token": "new", "expires_in": 3600, "refresh_token": ""},
])
def test_invalid_refresh_response_is_explicit_error_not_logout(signed_in, backend_main, monkeypatch, payload):
    client, _ = signed_in
    monkeypatch.setattr(backend_main, "urlopen", lambda *args, **kwargs: TokenResponse(payload))
    response = client.get("/api/auth/status")
    assert response.status_code == 502
    assert backend_main.auth_tokens["auth-id"]["refresh_token"] == "old-refresh"


def test_session_expiration_requires_login_even_with_valid_access(signed_in, backend_main, monkeypatch):
    client, _ = signed_in
    backend_main.auth_tokens["auth-id"].update({
        "session_expires_at": time.time() - 1,
        "expires_at": time.time() + 3600,
    })
    monkeypatch.setattr(backend_main, "urlopen", lambda *args, **kwargs: pytest.fail("Must not refresh"))
    assert client.get("/api/neighbor-notices/summary").status_code == 401
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_callback_stores_refresh_token_and_sets_eight_hour_cookie(anon_client, backend_main, monkeypatch):
    encoded = base64.urlsafe_b64encode(json.dumps({"email": "Test@Example.com"}).encode()).decode().rstrip("=")
    access = f"header.{encoded}.signature"
    monkeypatch.setattr(backend_main, "urlopen", lambda *args, **kwargs: TokenResponse({
        "access_token": access, "refresh_token": "refresh-secret", "expires_in": 3600,
    }))
    login = anon_client.get("/api/auth/login", follow_redirects=False)
    params = parse_qs(urlparse(login.headers["location"]).query)
    assert "offline_access" in params["scope"][0].split()
    before = time.time()
    callback = anon_client.get(
        "/api/auth/callback", params={"state": params["state"][0], "code": "code"},
        follow_redirects=False,
    )
    assert callback.status_code == 307
    cookie = callback.headers["set-cookie"]
    assert f"Max-Age={AUTH_SESSION_SECONDS}" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert access not in cookie and "refresh-secret" not in cookie
    saved = backend_main.auth_tokens[anon_client.cookies.get("sgv2_auth")]
    assert saved["refresh_token"] == "refresh-secret"
    assert saved["profile"]["email"] == "test@example.com"
    assert before + AUTH_SESSION_SECONDS <= saved["session_expires_at"] <= time.time() + AUTH_SESSION_SECONDS
    assert anon_client.post("/api/auth/logout", follow_redirects=False).status_code == 303
    assert not backend_main.auth_tokens
