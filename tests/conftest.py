from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest


TEST_UPN = "test@example.com"


def _skill_key(skill_name: str, owner_upn=None) -> str:
    owner = (owner_upn or "").strip()
    return f"{skill_name}_{owner}" if owner else skill_name


class _FakeSqlState:
    """In-memory replacement for dbo.skills + dbo.user_skill_grants used in tests.

    Mirrors schema v2.2: grants key off skill_key, and there is no description /
    version / blob_path column on dbo.skills.
    """

    def __init__(self) -> None:
        # skill_key -> {skill_name, owner_upn, is_public, is_internal, enabled, created_at, updated_at}
        self.skills: dict[str, dict] = {}
        # (user_upn, skill_key) -> {granted_at, granted_by, expires_at}
        self.grants: dict[tuple[str, str], dict] = {}

    def upsert_skill(
        self,
        name: str,
        *,
        owner_upn=None,
        is_public: bool = False,
        enabled: bool = True,
        is_internal: bool | None = None,
    ) -> bool:
        key = _skill_key(name, owner_upn)
        was_new = key not in self.skills
        now = datetime.now(timezone.utc)
        if was_new:
            self.skills[key] = {
                "skill_name": name,
                "owner_upn": owner_upn or None,
                "is_public": is_public,
                "is_internal": bool(is_internal),
                "enabled": enabled,
                "created_at": now,
                "updated_at": now,
            }
        else:
            self.skills[key].update({
                "is_public": is_public,
                "enabled": enabled,
                "updated_at": now,
            })
            if is_internal is not None:
                self.skills[key]["is_internal"] = is_internal
        return was_new

    def set_skill_visibility(self, name, is_public, owner_upn=None) -> int:
        meta = self.skills.get(_skill_key(name, owner_upn))
        if not meta:
            return 0
        meta["is_public"] = is_public
        meta["updated_at"] = datetime.now(timezone.utc)
        return 1

    def add_grant(self, skill_name, user_upn, granted_by=None, expires_at=None, owner_upn=None):
        key = (user_upn, _skill_key(skill_name, owner_upn))
        was_new = key not in self.grants
        self.grants[key] = {
            "granted_at": datetime.now(timezone.utc),
            "granted_by": granted_by,
            "expires_at": expires_at,
        }
        return was_new

    def remove_grant(self, skill_name, user_upn, owner_upn=None):
        return 1 if self.grants.pop((user_upn, _skill_key(skill_name, owner_upn)), None) else 0

    def _visible(self, user_upn, key, meta) -> bool:
        if not meta.get("enabled"):
            return False
        return (user_upn, key) in self.grants or bool(meta.get("is_public"))

    def list_grants_for_user(self, user_upn) -> set:
        return {
            meta["skill_name"]
            for key, meta in self.skills.items()
            if self._visible(user_upn, key, meta)
        }

    def user_has_access(self, user_upn, skill_name, owner_upn=None) -> bool:
        key = _skill_key(skill_name, owner_upn)
        meta = self.skills.get(key)
        return bool(meta) and self._visible(user_upn, key, meta)


@pytest.fixture()
def fake_sql(monkeypatch) -> _FakeSqlState:
    """Replace backend.skills_repo + backend.acl + backend.db with in-memory equivalents."""
    state = _FakeSqlState()
    from backend import acl as acl_mod
    from backend import skills_repo

    def _row(key, meta):
        return skills_repo.SkillRow(
            skill_name=meta["skill_name"],
            skill_key=key,
            owner_upn=meta["owner_upn"],
            is_public=meta["is_public"],
            is_internal=meta["is_internal"],
            enabled=meta["enabled"],
            created_at=meta["created_at"],
            updated_at=meta["updated_at"],
        )

    def _list_skills_for_user(upn):
        return [
            _row(key, meta)
            for key, meta in sorted(state.skills.items())
            if state._visible(upn, key, meta)
        ]

    def _get_skill(name, owner_upn=None):
        key = _skill_key(name, owner_upn)
        meta = state.skills.get(key)
        return _row(key, meta) if meta else None

    def _list_grants(skill_name, owner_upn=None):
        key = _skill_key(skill_name, owner_upn)
        return [
            skills_repo.GrantRow(
                user_upn=upn,
                skill_name=skill_name,
                skill_key=key,
                granted_at=g["granted_at"],
                granted_by=g["granted_by"],
                expires_at=g["expires_at"],
            )
            for (upn, sk), g in state.grants.items() if sk == key
        ]

    def _delete_skill(name, owner_upn=None):
        key = _skill_key(name, owner_upn)
        state.skills.pop(key, None)
        # Mirrors FK_user_skill_grants_skill ... ON DELETE CASCADE.
        for grant_key in [candidate for candidate in state.grants if candidate[1] == key]:
            state.grants.pop(grant_key, None)

    monkeypatch.setattr(skills_repo, "list_skills_for_user", _list_skills_for_user)
    monkeypatch.setattr(skills_repo, "get_skill", _get_skill)
    monkeypatch.setattr(skills_repo, "upsert_skill", state.upsert_skill)
    monkeypatch.setattr(skills_repo, "set_skill_visibility", state.set_skill_visibility)
    monkeypatch.setattr(skills_repo, "delete_skill", _delete_skill)
    monkeypatch.setattr(skills_repo, "list_grants", _list_grants)
    monkeypatch.setattr(skills_repo, "list_grants_for_user", state.list_grants_for_user)
    monkeypatch.setattr(skills_repo, "add_grant", state.add_grant)
    monkeypatch.setattr(skills_repo, "remove_grant", state.remove_grant)
    monkeypatch.setattr(skills_repo, "user_has_access", state.user_has_access)

    # Clear ACL cache so the fake skills_repo is reached every time.
    acl_mod.get_cache().invalidate()
    return state


@pytest.fixture()
def backend_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_sql: _FakeSqlState):
    """Import backend.main with local stores and reset process globals per test."""

    monkeypatch.setenv("SGV2_SESSION_DIR", str(tmp_path / "sessions"))
    # Opt into the in-memory local skill store so import does not require Azure Blob.
    monkeypatch.setenv("SGV2_SKILL_STORE", "local")
    # Dummy Microsoft Entra config so oauth_config() succeeds during tests.
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "test-tenant")
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "test-client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MICROSOFT_OBO_SCOPE", "api://test/access_as_user")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/foundry")
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "skill-generator-agent")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "2")
    # E2E_MODE enables the X-Test-UPN header bypass for tests.
    monkeypatch.setenv("E2E_MODE", "1")

    module = importlib.import_module("backend.main")

    from backend.blob_store import CachedSkillStore, LocalSkillStore
    from backend.session_store import LocalSessionStore

    module.store = CachedSkillStore(LocalSkillStore())
    module.session_store = LocalSessionStore(tmp_path / "sessions")
    module.sessions = {}
    module.auth_tokens = {}
    module.oauth_states = {}
    yield module
    # PREPARE-entry work runs in a daemon thread; drain it so it cannot race the
    # assertions of a later test or log into a torn-down interpreter.
    module.await_prepare_entry()


@pytest.fixture()
def client(backend_main):
    from fastapi.testclient import TestClient

    c = TestClient(backend_main.app)
    # All authenticated test calls get this UPN via the v7 RLS bypass header.
    c.headers["X-Test-UPN"] = TEST_UPN
    return c


@pytest.fixture()
def anon_client(backend_main):
    """Client WITHOUT the X-Test-UPN header (for 401 tests)."""
    from fastapi.testclient import TestClient
    return TestClient(backend_main.app)


@pytest.fixture()
def grant_skill(fake_sql):
    """Helper to grant TEST_UPN access to a given skill name (creates skill row too)."""

    def _grant(skill_name: str, description: str = "") -> None:
        fake_sql.upsert_skill(skill_name)
        fake_sql.add_grant(skill_name, TEST_UPN, granted_by=TEST_UPN)
        # Invalidate ACL cache after each grant.
        from backend import acl as acl_mod
        acl_mod.get_cache().invalidate(TEST_UPN)

    return _grant