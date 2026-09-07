"""ACL helpers: extract UPN from auth, enforce grants, cache for hot path."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import HTTPException, Request

from . import skills_repo
from .diagnostics import log_event


_CACHE_TTL_SECONDS = 60


class AclCache:
    """Per-user grant cache. Each entry stores a frozenset of skill_name."""

    def __init__(self, ttl_seconds: int = _CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[frozenset[str], float]] = {}

    def get_or_load(self, user_upn: str) -> frozenset[str]:
        if not user_upn:
            return frozenset()
        now = time.time()
        with self._lock:
            cached = self._entries.get(user_upn)
            if cached and cached[1] > now:
                return cached[0]
        # Network call outside lock.
        skills = frozenset(skills_repo.list_grants_for_user(user_upn))
        with self._lock:
            self._entries[user_upn] = (skills, now + self._ttl)
        log_event("acl.cache.refresh", user_upn=user_upn, count=len(skills))
        return skills

    def invalidate(self, user_upn: str | None = None) -> None:
        with self._lock:
            if user_upn is None:
                self._entries.clear()
                log_event("acl.cache.invalidate_all")
            else:
                self._entries.pop(user_upn, None)
                log_event("acl.cache.invalidate", user_upn=user_upn)


_cache = AclCache()


def get_cache() -> AclCache:
    return _cache


# ---------------------------------------------------------------------------
# Auth extraction
# ---------------------------------------------------------------------------


def _e2e_bypass_enabled() -> bool:
    return os.getenv("E2E_MODE", "").strip().lower() in {"1", "true", "yes"}


def e2e_default_upn() -> str | None:
    """Identity for browser-driven E2E, which cannot send the X-Test-UPN header.

    Opt-in and separate from E2E_MODE so the pytest 401 cases keep working.
    """
    if not _e2e_bypass_enabled():
        return None
    return (os.getenv("E2E_DEFAULT_UPN", "").strip().lower()) or None


def extract_upn_from_request(request: Request, auth_tokens: dict[str, dict[str, Any]], auth_store: Any | None = None) -> str | None:
    """Best-effort UPN extraction.

    Order:
      1. ``X-Test-UPN`` header (only when ``E2E_MODE=1``).
      2. Microsoft OBO cookie ``sgv2_auth`` -> auth_tokens[id]["profile"]["email"].
      3. fallback to profile.username.
      4. ``E2E_DEFAULT_UPN`` when there is no cookie at all (browser-driven E2E).
    """
    if _e2e_bypass_enabled():
        test_upn = request.headers.get("x-test-upn") or request.headers.get("X-Test-UPN")
        if test_upn:
            return test_upn.strip().lower()
    auth_id = request.cookies.get("sgv2_auth")
    if not auth_id:
        return e2e_default_upn()
    record = auth_tokens.get(auth_id)
    if not record and auth_store is not None:
        try:
            record = auth_store.get_auth_token(auth_id)
            if record:
                auth_tokens[auth_id] = record
        except Exception:
            record = None
    if not record:
        return None
    if float(record.get("expires_at", 0)) <= time.time() + 30:
        return None
    profile = record.get("profile") or {}
    upn = (profile.get("email") or profile.get("username") or "").strip().lower()
    return upn or None


def make_upn_dependency(auth_tokens: dict[str, dict[str, Any]], auth_store: Any | None = None):
    """Build a FastAPI dependency bound to the live auth_tokens dict."""

    def _dep(request: Request) -> str:
        upn = extract_upn_from_request(request, auth_tokens, auth_store)
        if not upn:
            raise HTTPException(
                status_code=401,
                detail="Sign in required. Use Microsoft sign-in to access this resource.",
            )
        return upn

    return _dep


def make_optional_upn_dependency(auth_tokens: dict[str, dict[str, Any]], auth_store: Any | None = None):
    """Same as required, but returns '' instead of raising."""

    def _dep(request: Request) -> str:
        return extract_upn_from_request(request, auth_tokens, auth_store) or ""

    return _dep


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------


def assert_can_access(user_upn: str, skill_name: str) -> None:
    """Raise 403 unless the user has a grant on the skill, or it is public."""
    if not user_upn:
        raise HTTPException(status_code=401, detail="Sign in required.")
    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name required.")
    skills = _cache.get_or_load(user_upn)
    if skill_name not in skills:
        # Confirm against the DB once (cache miss) before refusing -- mitigates
        # the race where a grant was just added by another process.
        if skills_repo.user_has_access(user_upn, skill_name):
            _cache.invalidate(user_upn)
            return
        log_event(
            "acl.access_denied",
            user_upn=user_upn,
            skill_name=skill_name,
            level="warning",
        )
        raise HTTPException(
            status_code=403,
            detail=f"You do not have permission to access skill '{skill_name}'.",
        )


def can_access(user_upn: str, skill_name: str) -> bool:
    if not user_upn or not skill_name:
        return False
    return skill_name in _cache.get_or_load(user_upn)


def visible_skill_names(user_upn: str) -> frozenset[str]:
    return _cache.get_or_load(user_upn)