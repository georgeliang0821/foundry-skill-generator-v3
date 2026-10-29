from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi import HTTPException

from backend.auth_store import AzureBlobAuthStore, LocalAuthStore


def record(**changes):
    return {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": time.time() - 60,
        "session_expires_at": time.time() + 3600,
        "profile": {"email": "user@example.com"},
        **changes,
    }


@pytest.mark.parametrize("remaining_seconds", [-60, 29])
def test_expired_access_token_is_refreshed_without_extending_session(remaining_seconds):
    original = record(expires_at=time.time() + remaining_seconds)
    tokens = {"id": original}
    store = LocalAuthStore(tokens, {})
    calls = []

    def refresh(current):
        calls.append(current)
        return {**current, "access_token": "new-access", "expires_at": time.time() + 3600}

    store.refresh_auth = refresh
    result = store.get_auth_token("id")

    assert result is not None
    assert result["access_token"] == "new-access"
    assert result["session_expires_at"] == original["session_expires_at"]
    assert len(calls) == 1
    assert tokens["id"] == result


def test_parallel_requests_refresh_once():
    store = LocalAuthStore({"id": record()}, {})
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def refresh(current):
        calls.append(current)
        entered.set()
        assert release.wait(5)
        return {**current, "expires_at": time.time() + 3600}

    store.refresh_auth = refresh
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(store.get_auth_token, "id") for _ in range(8)]
        assert entered.wait(5)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert all(result is not None for result in results)
    assert len(calls) == 1


@pytest.mark.parametrize("changes", [
    {"session_expires_at": time.time() - 1, "expires_at": time.time() + 3600},
    {"refresh_token": ""},
])
def test_expired_session_or_missing_refresh_token_requires_login(changes):
    tokens = {"id": record(**changes)}
    store = LocalAuthStore(tokens, {})
    store.refresh_auth = lambda current: pytest.fail("Must not refresh")

    assert store.get_auth_token("id") is None
    assert "id" not in tokens


def test_transient_refresh_failure_preserves_record_for_retry():
    original = record()
    tokens = {"id": original}
    store = LocalAuthStore(tokens, {})

    def unavailable(current):
        raise HTTPException(503, "Temporarily unavailable")

    store.refresh_auth = unavailable
    with pytest.raises(HTTPException) as exc:
        store.get_auth_token("id")
    assert exc.value.status_code == 503
    assert tokens["id"] == original


def test_rejected_refresh_deletes_record():
    tokens = {"id": record()}
    store = LocalAuthStore(tokens, {})
    store.refresh_auth = lambda current: None

    assert store.get_auth_token("id") is None
    assert "id" not in tokens


def test_legacy_record_still_expires_without_refresh():
    tokens = {"id": {"expires_at": time.time() - 1}}
    store = LocalAuthStore(tokens, {})
    assert store.get_auth_token("id") is None
    assert not tokens


def test_valid_access_token_does_not_refresh():
    original = record(expires_at=time.time() + 300)
    store = LocalAuthStore({"id": original}, {})
    store.refresh_auth = lambda current: pytest.fail("Must not refresh")
    assert store.get_auth_token("id") == original


def test_logout_waits_for_refresh_then_removes_rotated_tokens():
    tokens = {"id": record()}
    store = LocalAuthStore(tokens, {})
    entered = threading.Event()
    release = threading.Event()

    def refresh(current):
        entered.set()
        assert release.wait(5)
        return {**current, "refresh_token": "rotated", "expires_at": time.time() + 3600}

    store.refresh_auth = refresh
    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshing = pool.submit(store.get_auth_token, "id")
        assert entered.wait(5)
        logout = pool.submit(store.delete_auth_token, "id")
        release.set()
        refreshing.result(timeout=5)
        logout.result(timeout=5)
    assert not tokens
    assert store.get_auth_token("id") is None


def test_blob_store_persists_rotated_refresh_token_without_azure():
    persisted = {"id": record()}

    class FakeBlobAuthStore(AzureBlobAuthStore):
        def __init__(self):
            LocalAuthStore.__init__(self, {}, {})

        def _load_blob(self, kind, key):
            return persisted.get(key)

        def _save_blob(self, kind, key, current):
            persisted[key] = dict(current)

        def _delete_blob(self, kind, key):
            persisted.pop(key, None)

    store = FakeBlobAuthStore()
    store.refresh_auth = lambda current: {
        **current, "refresh_token": "rotated", "expires_at": time.time() + 3600,
    }
    assert store.get_auth_token("id")["refresh_token"] == "rotated"
    assert persisted["id"]["refresh_token"] == "rotated"
    assert FakeBlobAuthStore().get_auth_token("id")["refresh_token"] == "rotated"
    store.delete_auth_token("id")
    assert not persisted and not store.auth_tokens
