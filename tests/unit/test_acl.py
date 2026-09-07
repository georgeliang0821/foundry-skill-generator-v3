"""Unit tests for backend.acl: cache, request UPN extraction, access checks."""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from backend import acl as acl_mod
from backend import skills_repo


def test_acl_cache_ttl_returns_cached_value(monkeypatch):
    cache = acl_mod.AclCache(ttl_seconds=60)
    calls = {"n": 0}

    def fake(user_upn):
        calls["n"] += 1
        return {"alpha", "beta"}

    monkeypatch.setattr(skills_repo, "list_grants_for_user", fake)
    assert cache.get_or_load("u@x") == frozenset({"alpha", "beta"})
    assert cache.get_or_load("u@x") == frozenset({"alpha", "beta"})
    assert calls["n"] == 1


def test_acl_cache_expires(monkeypatch):
    cache = acl_mod.AclCache(ttl_seconds=1)
    counter = {"n": 0}

    def fake(user_upn):
        counter["n"] += 1
        return {f"v{counter['n']}"}

    monkeypatch.setattr(skills_repo, "list_grants_for_user", fake)
    cache.get_or_load("u@x")
    # Force expiry
    cache._entries["u@x"] = (frozenset({"v1"}), time.time() - 5)
    cache.get_or_load("u@x")
    assert counter["n"] == 2


def test_acl_cache_invalidate(monkeypatch):
    cache = acl_mod.AclCache()
    monkeypatch.setattr(skills_repo, "list_grants_for_user", lambda u: {"a"})
    cache.get_or_load("u@x")
    cache.invalidate("u@x")
    assert "u@x" not in cache._entries


def test_acl_cache_invalidate_all(monkeypatch):
    cache = acl_mod.AclCache()
    monkeypatch.setattr(skills_repo, "list_grants_for_user", lambda u: {"a"})
    cache.get_or_load("u1@x")
    cache.get_or_load("u2@x")
    cache.invalidate(None)
    assert cache._entries == {}


def test_assert_can_access_grants(monkeypatch):
    monkeypatch.setattr(skills_repo, "list_grants_for_user", lambda u: {"sk"})
    monkeypatch.setattr(skills_repo, "user_has_access", lambda u, s: s == "sk")
    acl_mod.get_cache().invalidate()
    # Should not raise.
    acl_mod.assert_can_access("u@x", "sk")


def test_assert_can_access_no_grant_raises_403(monkeypatch):
    monkeypatch.setattr(skills_repo, "list_grants_for_user", lambda u: set())
    monkeypatch.setattr(skills_repo, "user_has_access", lambda u, s: False)
    acl_mod.get_cache().invalidate()
    with pytest.raises(HTTPException) as info:
        acl_mod.assert_can_access("u@x", "denied-skill")
    assert info.value.status_code == 403


def test_assert_can_access_no_upn_raises_401(monkeypatch):
    with pytest.raises(HTTPException) as info:
        acl_mod.assert_can_access("", "anything")
    assert info.value.status_code == 401


def test_assert_can_access_recovers_from_stale_cache(monkeypatch):
    # Cache says no, but DB says yes -> we should re-check DB and pass.
    monkeypatch.setattr(skills_repo, "list_grants_for_user", lambda u: set())
    monkeypatch.setattr(skills_repo, "user_has_access", lambda u, s: True)
    acl_mod.get_cache().invalidate()
    # Should not raise (DB recheck succeeds).
    acl_mod.assert_can_access("u@x", "fresh-grant")


def test_extract_upn_from_request_e2e_header(monkeypatch):
    monkeypatch.setenv("E2E_MODE", "1")

    class Req:
        cookies = {}
        headers = {"x-test-upn": "Test@Example.com"}

    upn = acl_mod.extract_upn_from_request(Req(), {})
    assert upn == "test@example.com"


def test_extract_upn_from_request_no_e2e_header(monkeypatch):
    monkeypatch.delenv("E2E_MODE", raising=False)

    class Req:
        cookies = {}
        headers = {"X-Test-UPN": "ignored@test"}

    assert acl_mod.extract_upn_from_request(Req(), {}) is None


def test_extract_upn_from_request_via_cookie():
    class Req:
        cookies = {"sgv2_auth": "abc"}
        headers = {}

    auth_tokens = {
        "abc": {
            "expires_at": time.time() + 600,
            "profile": {"email": "Bob@x.test", "username": "ignore"},
        }
    }
    assert acl_mod.extract_upn_from_request(Req(), auth_tokens) == "bob@x.test"


def test_extract_upn_from_request_expired_cookie():
    class Req:
        cookies = {"sgv2_auth": "abc"}
        headers = {}

    auth_tokens = {
        "abc": {
            "expires_at": time.time() - 1,
            "profile": {"email": "bob@x.test"},
        }
    }
    assert acl_mod.extract_upn_from_request(Req(), auth_tokens) is None


def test_require_upn_dependency_raises_401_when_anonymous():
    dep = acl_mod.make_upn_dependency({})

    class Req:
        cookies = {}
        headers = {}

    with pytest.raises(HTTPException) as info:
        dep(Req())
    assert info.value.status_code == 401